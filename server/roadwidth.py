"""Street widths from the buildings that line them (docs/ARCHITECTURE.md "Roads"; the user, 2026-10-11:
Thames St at Fells Point, a 25-30 m cobbled square, was drawn as a 10.6 m street - Overture has no
width for most streets, so every street of a class got the same default).

First tried from aerial imagery (NAIP, 0.6 m): asphalt, concrete sidewalks and many roofs are the
same light grey there (brightness ~0.6, saturation < 0.05), so cross-sections of one street read
4-35 m. Building footprints (Overture) are exact and need no imagery: in a built-up street the
buildings stand at the back of the sidewalks, so the distance between the building faces across the
street is its right-of-way.

measure_row(): every STEP m along the street (END_SKIP m kept clear of its ends: junctions), a ray
each way up to REACH m to the first building wall. The street is "walled" when both rays hit in at
least WALLED of the cross-sections and the face-to-face distances agree (IQR within AGREE of the
median); its right-of-way is then that median, split by split(): two sidewalks of WALK_SHARE of it
(clamped to WALK_MIN..WALK_MAX) and the carriageway between. Elsewhere (front yards, open land): None,
and the class defaults stay. Pure; RoadWidths memoises per road for the life of the server, so the
physics block, grading, the road meshes and the surface masks always agree.
"""
import math
import threading

import numpy as np

import geodesy
import mapcells

STEP = 6.0              # m between cross-sections
END_SKIP = 10.0         # m kept clear of each end of a road (junctions)
REACH = 25.0            # m searched each side
MIN_SAMPLES = 4
WALLED = 0.6            # share of cross-sections with a building on both sides
AGREE = 0.3             # IQR / median at most this
WALK_SHARE = 0.2        # each sidewalk this share of the right-of-way ...
WALK_MIN, WALK_MAX = 1.8, 4.5   # ... within these (m)
CARRIAGE_MIN = 5.0      # m: narrower measured carriageways are not trusted
BUCKET = 16.0           # m, wall segment buckets


class Walls:
    """Building wall segments (metres, a local frame) with a bucket index for ray casts."""

    def __init__(self, segs):
        self.segs = np.asarray(segs, np.float64).reshape(-1, 4)       # x0 y0 x1 y1
        self.index = {}
        for k, (x0, y0, x1, y1) in enumerate(self.segs):
            for bx in range(int(math.floor(min(x0, x1) / BUCKET)), int(math.floor(max(x0, x1) / BUCKET)) + 1):
                for by in range(int(math.floor(min(y0, y1) / BUCKET)), int(math.floor(max(y0, y1) / BUCKET)) + 1):
                    self.index.setdefault((bx, by), []).append(k)

    def ray(self, p, d, reach=REACH):
        """Distance from p (2,) along unit d (2,) to the first wall, or None within reach."""
        q = p + d * reach
        ks = set()
        for bx in range(int(math.floor(min(p[0], q[0]) / BUCKET)), int(math.floor(max(p[0], q[0]) / BUCKET)) + 1):
            for by in range(int(math.floor(min(p[1], q[1]) / BUCKET)), int(math.floor(max(p[1], q[1]) / BUCKET)) + 1):
                ks.update(self.index.get((bx, by), ()))
        if not ks:
            return None
        s = self.segs[sorted(ks)]
        a, b = s[:, :2], s[:, 2:]
        e = b - a
        den = d[0] * e[:, 1] - d[1] * e[:, 0]
        ok = np.abs(den) > 1e-12
        w = a - p
        t = np.where(ok, (w[:, 0] * e[:, 1] - w[:, 1] * e[:, 0]) / np.where(ok, den, 1), -1)
        u = np.where(ok, (w[:, 0] * d[1] - w[:, 1] * d[0]) / np.where(ok, den, 1), -1)
        hit = ok & (t > 0) & (t <= reach) & (u >= 0) & (u <= 1)
        return float(t[hit].min()) if hit.any() else None


def measure_row(centreline, walls):
    """The right-of-way (m) of a street (centreline (k, 2), metres) between the building walls, or None
    where it is not walled on both sides consistently. Pure."""
    xy = np.asarray(centreline, np.float64)[:, :2]
    seg = np.hypot(*np.diff(xy, axis=0).T)
    s = np.r_[0.0, np.cumsum(seg)]
    if s[-1] < 2 * END_SKIP + STEP:
        return None
    rows, n = [], 0
    for dd in np.arange(END_SKIP, s[-1] - END_SKIP + 1e-9, STEP):
        k = max(1, min(int(np.searchsorted(s, dd)), len(s) - 1))
        t = xy[k] - xy[k - 1]
        L = math.hypot(*t)
        if L < 1e-6:
            continue
        nrm = np.array([-t[1], t[0]]) / L
        c = np.array([np.interp(dd, s, xy[:, 0]), np.interp(dd, s, xy[:, 1])])
        n += 1
        l, r = walls.ray(c, nrm), walls.ray(c, -nrm)
        if l is not None and r is not None:
            rows.append(l + r)
    if n == 0 or len(rows) < MIN_SAMPLES or len(rows) / n < WALLED:
        return None
    w = np.array(rows)
    med = float(np.median(w))
    q1, q3 = np.percentile(w, [25, 75])
    if (q3 - q1) / med > AGREE:
        return None
    return med


def split(row):
    """(carriageway m, sidewalk m) of a right-of-way, or None when the carriageway would be implausible."""
    walk = min(WALK_MAX, max(WALK_MIN, WALK_SHARE * row))
    cw = row - 2 * walk
    if cw < CARRIAGE_MIN:
        return None
    return cw, walk


def polygon_walls(feats, frame):
    """Wall segments of building polygons (Features) in frame's metres."""
    segs = []
    for i in range(len(feats)):
        for part in feats.parts(i):
            for ring in part:
                if len(ring) < 3:
                    continue
                e, n, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
                p = np.c_[e, n]
                segs.append(np.c_[p[:-1], p[1:]])
    return np.concatenate(segs) if segs else np.zeros((0, 4))


class RoadWidths:
    """Measured street widths per road id, decided once per server run (thread-safe). cell_fn(key) ->
    the buildings Features of a map cell (None / raises when unavailable: the road keeps its default)."""

    def __init__(self, cell_fn):
        self.cell_fn = cell_fn
        self.memo = {}                          # fid -> (carriageway, walk) or None
        self.walls = {}                         # cell key -> (frame, Walls)
        self.lock = threading.Lock()

    def _walls(self, key):
        """The walls of the buildings in cell key and the 8 around it (a street near a cell edge sees
        both sides), in a frame at the cell's centre."""
        with self.lock:
            got = self.walls.get(str(key))
        if got is not None:
            return got
        lon0, lat0, lon1, lat1 = key.bounds()
        frame = geodesy.EnuFrame(0.5 * (lat0 + lat1), 0.5 * (lon0 + lon1))
        dlat, dlon = 0.5 * (lat1 - lat0), 0.5 * (lon1 - lon0)
        segs = []
        for k2 in mapcells.cells_for_bbox(lat0 - dlat * 0.1, lon0 - dlon * 0.1, lat1 + dlat * 0.1, lon1 + dlon * 0.1):
            try:
                feats = self.cell_fn(k2)
            except Exception:
                feats = None
            if feats is not None and len(feats):
                segs.append(polygon_walls(feats, frame))
        walls = Walls(np.concatenate(segs) if segs else np.zeros((0, 4)))
        with self.lock:
            self.walls.setdefault(str(key), (frame, walls))
            return self.walls[str(key)]

    def get(self, fid, lonlat):
        """(carriageway m, sidewalk m) of road fid (geometry lonlat (k, 2)), or None (defaults)."""
        with self.lock:
            if fid in self.memo:
                return self.memo[fid]
        ll = np.asarray(lonlat, np.float64)
        mid = ll[len(ll) // 2]
        key = mapcells.cell_of(float(mid[1]), float(mid[0]))
        frame, walls = self._walls(key)
        res = None
        if len(walls.segs):
            e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.0)
            row = measure_row(np.c_[e, n], walls)
            res = split(row) if row is not None else None
        with self.lock:
            self.memo.setdefault(fid, res)
            return self.memo[fid]
