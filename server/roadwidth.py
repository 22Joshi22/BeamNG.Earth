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
        # bucket index, vectorised (a Python loop per segment took ~18 s for a city's 9 cells, 2026-10-12):
        # one (segment, bucket) pair per bucket a segment's box touches, sorted by bucket key
        s = self.segs
        bx0 = np.floor(np.minimum(s[:, 0], s[:, 2]) / BUCKET).astype(np.int64)
        bx1 = np.floor(np.maximum(s[:, 0], s[:, 2]) / BUCKET).astype(np.int64)
        by0 = np.floor(np.minimum(s[:, 1], s[:, 3]) / BUCKET).astype(np.int64)
        by1 = np.floor(np.maximum(s[:, 1], s[:, 3]) / BUCKET).astype(np.int64)
        nx, ny = bx1 - bx0 + 1, by1 - by0 + 1
        cnt = nx * ny
        seg = np.repeat(np.arange(len(s)), cnt)
        k = np.arange(cnt.sum()) - np.repeat(np.cumsum(cnt) - cnt, cnt)
        bx = np.repeat(bx0, cnt) + k % np.repeat(nx, cnt)
        by = np.repeat(by0, cnt) + k // np.repeat(nx, cnt)
        key = self._key(bx, by)
        order = np.argsort(key, kind="stable")
        self.keys, first, counts = np.unique(key[order], return_index=True, return_counts=True)
        self.first, self.counts, self.seg_of = first, counts, seg[order]

    @staticmethod
    def _key(bx, by):
        return (np.asarray(bx, np.int64) + (1 << 30)) * (1 << 31) + (np.asarray(by, np.int64) + (1 << 30))

    def ray(self, p, d, reach=REACH):
        """Distance from p (2,) along unit d (2,) to the first wall, or None within reach."""
        t = self.rays(np.asarray(p, np.float64)[None], np.asarray(d, np.float64)[None], reach)[0]
        return None if np.isnan(t) else float(t)

    def rays(self, p, d, reach=REACH):
        """Distances from the points p (m, 2) along the unit directions d (m, 2) to the first wall, NaN
        where none is within reach. All rays at once (one Python call per ray took ~3 s a block,
        2026-10-12): every (ray, bucket) pair of the rays' boxes, then every (ray, segment) pair of those
        buckets, intersected together."""
        p = np.asarray(p, np.float64).reshape(-1, 2)
        d = np.asarray(d, np.float64).reshape(-1, 2)
        out = np.full(len(p), np.nan)
        if not len(p) or not len(self.keys):
            return out
        q = p + d * reach
        bx0 = np.floor(np.minimum(p[:, 0], q[:, 0]) / BUCKET).astype(np.int64)
        bx1 = np.floor(np.maximum(p[:, 0], q[:, 0]) / BUCKET).astype(np.int64)
        by0 = np.floor(np.minimum(p[:, 1], q[:, 1]) / BUCKET).astype(np.int64)
        by1 = np.floor(np.maximum(p[:, 1], q[:, 1]) / BUCKET).astype(np.int64)
        nx, ny = bx1 - bx0 + 1, by1 - by0 + 1
        cnt = nx * ny
        ray = np.repeat(np.arange(len(p)), cnt)
        k = np.arange(cnt.sum()) - np.repeat(np.cumsum(cnt) - cnt, cnt)
        bx = np.repeat(bx0, cnt) + k % np.repeat(nx, cnt)
        by = np.repeat(by0, cnt) + k // np.repeat(nx, cnt)
        key = self._key(bx, by)
        i = np.minimum(np.searchsorted(self.keys, key), len(self.keys) - 1)
        found = self.keys[i] == key
        ray, i = ray[found], i[found]
        c = self.counts[i]
        ray = np.repeat(ray, c)
        j = np.arange(c.sum()) - np.repeat(np.cumsum(c) - c, c) + np.repeat(self.first[i], c)
        s = self.segs[self.seg_of[j]]
        pr, dr = p[ray], d[ray]
        a, e = s[:, :2], s[:, 2:] - s[:, :2]
        den = dr[:, 0] * e[:, 1] - dr[:, 1] * e[:, 0]
        ok = np.abs(den) > 1e-12
        sden = np.where(ok, den, 1.0)
        w = a - pr
        t = np.where(ok, (w[:, 0] * e[:, 1] - w[:, 1] * e[:, 0]) / sden, -1)
        u = np.where(ok, (w[:, 0] * dr[:, 1] - w[:, 1] * dr[:, 0]) / sden, -1)
        hit = ok & (t > 0) & (t <= reach) & (u >= 0) & (u <= 1)
        best = np.full(len(p), np.inf)
        np.minimum.at(best, ray[hit], t[hit])
        out[np.isfinite(best)] = best[np.isfinite(best)]
        return out


def measure_row(centreline, walls):
    """The right-of-way (m) of a street (centreline (k, 2), metres) between the building walls, or None
    where it is not walled on both sides consistently. Pure."""
    import fast
    if fast.AVAILABLE and len(walls.keys):     # the Rust loop (the same result; ~3000 streets a 9-tile set)
        k = walls.__dict__.get("_rs")
        if k is None:
            k = walls._rs = tuple(np.ascontiguousarray(a, t) for a, t in
                                  ((walls.keys, np.int64), (walls.first, np.int64), (walls.counts, np.int64),
                                   (walls.seg_of, np.int64), (walls.segs, np.float64)))
        return fast._ef.measure_row(np.ascontiguousarray(np.asarray(centreline, np.float64)[:, :2]), *k, BUCKET,
                                    STEP, END_SKIP, REACH, MIN_SAMPLES, WALLED, AGREE)
    xy = np.asarray(centreline, np.float64)[:, :2]
    seg = np.hypot(*np.diff(xy, axis=0).T)
    s = np.r_[0.0, np.cumsum(seg)]
    if s[-1] < 2 * END_SKIP + STEP:
        return None
    dd = np.arange(END_SKIP, s[-1] - END_SKIP + 1e-9, STEP)
    k = np.clip(np.searchsorted(s, dd), 1, len(s) - 1)
    t = xy[k] - xy[k - 1]
    L = np.hypot(t[:, 0], t[:, 1])
    good = L >= 1e-6
    dd, t, L = dd[good], t[good], L[good]
    n = len(dd)
    if n == 0:
        return None
    nrm = np.stack([-t[:, 1], t[:, 0]], 1) / L[:, None]
    c = np.stack([np.interp(dd, s, xy[:, 0]), np.interp(dd, s, xy[:, 1])], 1)
    hits = walls.rays(np.r_[c, c], np.r_[nrm, -nrm])
    w = hits[:n] + hits[n:]
    w = w[~np.isnan(w)]
    if len(w) < MIN_SAMPLES or len(w) / n < WALLED:
        return None
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
    """Wall segments of building polygons (Features) in frame's metres: consecutive vertices of every ring
    (rings of fewer than 3 points skipped). One coordinate transform for the whole set (one per ring took
    ~22 s for a city's 9 cells, 2026-10-12)."""
    ll = np.asarray(feats.lonlat, np.float64)
    off = np.asarray(feats.coord_off, np.int64)
    if len(ll) < 2 or len(off) < 2:
        return np.zeros((0, 4))
    e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.0)
    p = np.stack([e, n], 1)
    lengths = np.diff(off)
    ring = np.repeat(np.arange(len(lengths)), lengths)
    keep_ring = lengths >= 3
    i = np.arange(len(ll) - 1)
    ok = (ring[:-1] == ring[1:]) & keep_ring[ring[:-1]]
    i = i[ok]
    return np.concatenate([p[i], p[i + 1]], 1)


class RoadWidths:
    """Measured street widths per road id, decided once per server run (thread-safe). cell_fn(key) ->
    the buildings Features of a map cell (None / raises when unavailable: the road keeps its default)."""

    def __init__(self, cell_fn):
        self.cell_fn = cell_fn
        self.memo = {}                          # fid -> (carriageway, walk) or None
        self.walls = {}                         # cell key -> (frame, Walls)
        self.lock = threading.Lock()
        self.key_locks = {}                     # cell key -> Lock: one thread builds a cell's walls

    def _walls(self, key):
        """The walls of the buildings in cell key and the 8 around it (a street near a cell edge sees
        both sides), in a frame at the cell's centre."""
        with self.lock:
            got = self.walls.get(str(key))
            kl = self.key_locks.setdefault(str(key), threading.Lock())
        if got is not None:
            return got
        with kl:
            return self._build_walls(key)

    def _build_walls(self, key):
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
