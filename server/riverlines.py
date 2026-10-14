"""Rivers and streams as River objects along their centrelines (the mod's sea.lua; user 2026-10-13: like
the "Downhill Mayhem" map's rivers, whose River objects run down a valley with a node every ~35 m and
push cars along with flowMagnitudePhysics).

    rivers = plan(frame, x, y, cell_water, water_at, dem_at)     # [River] around (x, y)

Overture's water lines (subtype river / stream, class river / stream / canal; not drains, ditches or
intermittent ones) are drawn in the direction of flow, as OSM ways. Ways that continue one another
(one's last point is the next one's first, and only those two meet there) are chained. A chain is
resampled every SPACING_M, and cut into pieces on a PIECE_M lattice of the anchor frame (stable keys:
a piece is made again only when its chain changes). Where the sea, a lake or a river the DEM flattens is
already water as tiles (the sea, lakes; seacarve), the chain is left out. Every river is drawn this way,
wide ones too (flat tiles left holes and steps on sloping rivers, 2026-10-13); seacarve still carves the
beds of the rivers the DEM flattens, so those are deep.

The surface: the DEM in the channel (the lowest of the node and four points CHANNEL_PROBE_M around it),
a running median over MEDIAN_N nodes, never rising downstream (a running minimum along the flow), plus
SURFACE_ABOVE_M; where seacarve carved the bed (level_at), its level + CARVED_ABOVE_M, so the water fills
the cut channel. The width: measured
across the river's outline where Overture has one (seacarve's outline mask), the node moved midway between
the banks, else WIDTH_M by class.
"""
import hashlib
import math
from dataclasses import dataclass

import numpy as np

import mapcells

SUBTYPES = ("river", "stream", "canal")
SKIP_CLASSES = ("drain", "ditch", "fish_pass")
RADIUS_M = 2500.0           # rivers within this of the player
PIECE_M = 1024.0            # pieces on this lattice of the anchor frame
SPACING_M = {"river": 20.0, "stream": 10.0, "canal": 15.0}
WIDTH_M = {"river": 12.0, "stream": 4.0, "canal": 8.0}         # without an outline
WIDTH_MAX_M = 2000.0        # m: wide rivers (the Susquehanna at Harrisburg: ~1 km)
WIDTH_STEP_M = 4.0          # the banks searched this finely
CHANNEL_PROBE_M = 3.0
MEDIAN_N = 5
SMOOTH_N = 7                # the centre and width also averaged over this many nodes
SURFACE_ABOVE_M = 0.25      # m: the water's surface above the channel's DEM (where nothing is carved)
CARVED_ABOVE_M = 0.05       # m: ... above the carved level (seacarve cuts the bed below it)
DEPTH_M = (1.0, 8.0)        # the River's depth (its volume below the surface): width / 8, clamped
FLOW_MS = {"river": 1.0, "stream": 0.6, "canal": 0.3}          # flowMagnitudePhysics (m/s), downstream
DEM_ZOOM = 15


@dataclass(frozen=True)
class River:
    key: str
    kind: str               # river / stream / canal
    flow: float             # m/s along the node order
    nodes: tuple            # ((x, y, z, width, depth), ...) in the anchor frame, downstream order

    def to_msg(self):
        return [self.key, self.kind, self.flow,
                [[round(x, 2), round(y, 2), round(z, 2), round(w, 1), round(d, 1)] for x, y, z, w, d in self.nodes]]


def _kind(sub, cls):
    if cls in SKIP_CLASSES or sub not in SUBTYPES:
        return None
    if sub == "canal":
        return "canal" if cls == "canal" else None
    return "river" if sub == "river" else "stream"


def chains(ways):
    """Ways [(id, kind, lonlat (k, 2))] chained where one continues another (its last point is the
    next one's first and no other way starts or ends there), in flow order: [(ids, kind, lonlat)]."""
    def key(p):
        return (round(float(p[0]), 7), round(float(p[1]), 7))
    starts, ends = {}, {}
    for w in ways:
        starts.setdefault(key(w[2][0]), []).append(w)
        ends.setdefault(key(w[2][-1]), []).append(w)

    def next_of(w):
        k = key(w[2][-1])
        s = starts.get(k, [])
        if len(s) == 1 and len(ends.get(k, [])) == 1 and s[0][1] == w[1] and s[0] is not w:
            return s[0]
        return None
    has_prev = set()
    for w in ways:
        n = next_of(w)
        if n is not None:
            has_prev.add(n[0])
    out, used = [], set()
    for w in sorted(ways, key=lambda w: w[0]):
        if w[0] in has_prev or w[0] in used:
            continue
        ids, pts, cur = [], [], w
        while cur is not None and cur[0] not in used:
            used.add(cur[0])
            ids.append(cur[0])
            pts.append(cur[2] if not pts else cur[2][1:])
            cur = next_of(cur)
        out.append((tuple(ids), w[1], np.concatenate(pts)))
    for w in ways:                      # loops (every way has a predecessor): each way alone
        if w[0] not in used:
            out.append(((w[0],), w[1], w[2]))
    return out


def _resample(xy, step):
    seg = np.hypot(*np.diff(xy, axis=0).T)
    s = np.r_[0.0, np.cumsum(seg)]
    if s[-1] <= 0:
        return xy[:1], s[:1]
    n = max(2, int(math.ceil(s[-1] / step)) + 1)
    t = np.linspace(0.0, s[-1], n)
    return np.stack([np.interp(t, s, xy[:, 0]), np.interp(t, s, xy[:, 1])], -1), t


def _running_median(v, n):
    if len(v) < 3:
        return v.copy()
    h = n // 2
    p = np.pad(v, h, mode="edge")
    return np.median(np.stack([p[i:i + len(v)] for i in range(n)]), axis=0)


def plan(frame, x, y, cell_water, water_at, dem_at, outline_at=None, radius=None, level_at=None):
    """The River pieces around (x, y) in `frame`. cell_water(key) -> the water Features of a map-data cell
    or None (not at hand); water_at(lat, lon) -> depth > 0 where seacarve's tiles already are water;
    dem_at(lat, lon) -> the DEM; outline_at(lat, lon) -> True inside a river outline (None: widths by
    class); level_at(lat, lon) -> the carved river level there (seacarve: the same the bed is cut to;
    NaN where none: the DEM rule)."""
    r = RADIUS_M if radius is None else radius
    la0, lo0, _ = frame.enu_to_geodetic(x - r, y - r, 0.0)
    la1, lo1, _ = frame.enu_to_geodetic(x + r, y + r, 0.0)
    try:
        keys = mapcells.cells_for_bbox(float(la0), float(lo0), float(la1), float(lo1), max_cells=256)
    except ValueError:
        return []
    ways, seen = [], set()
    for k in keys:
        f = cell_water(k)
        if f is None:
            continue
        for i in np.flatnonzero(f.kind == 2):
            kind = _kind(str(f.attrs["subtype"][i]), str(f.attrs.get("class", [""] * len(f.kind))[i]))
            if kind is None or bool(f.attrs.get("is_intermittent", np.zeros(len(f.kind), bool))[i]):
                continue
            gid = str(f.ids[i])
            if gid in seen:
                continue
            seen.add(gid)
            for part in f.parts(i):
                for ring in part:
                    if len(ring) >= 2:
                        ways.append((gid, kind, np.asarray(ring, np.float64)))
    out = []
    for ids, kind, ll in chains(ways):
        e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], np.zeros(len(ll)))
        xy = np.stack([np.asarray(e), np.asarray(n)], -1)
        if np.hypot(xy[:, 0] - x, xy[:, 1] - y).min() > r + PIECE_M:
            continue
        pts, _ = _resample(xy, SPACING_M[kind])
        out += _pieces(frame, ids, kind, pts, x, y, r, water_at, dem_at, outline_at, level_at)
    return out


def _pieces(frame, ids, kind, pts, x, y, r, water_at, dem_at, outline_at, level_at=None):
    la, lo, _ = frame.enu_to_geodetic(pts[:, 0], pts[:, 1], np.zeros(len(pts)))
    la, lo = np.asarray(la), np.asarray(lo)
    wet = np.asarray(water_at(la, lo)) > 0          # already water (tiles): left out there
    # the channel: the lowest of the node and four points around it
    d = CHANNEL_PROBE_M
    off = [(0, 0), (d, 0), (-d, 0), (0, d), (0, -d)]
    ex = np.concatenate([pts[:, 0] + a for a, _ in off])
    ny = np.concatenate([pts[:, 1] + b for _, b in off])
    pla, plo, _ = frame.enu_to_geodetic(ex, ny, np.zeros(len(ex)))
    dem = np.asarray(dem_at(np.asarray(pla), np.asarray(plo)), np.float64).reshape(len(off), -1).min(axis=0)
    level = np.minimum.accumulate(_running_median(dem, MEDIAN_N)) + SURFACE_ABOVE_M
    width, centre = _widths(frame, kind, pts, outline_at)
    if level_at is not None:            # the carved bed's level: the water's surface, just above
        cla, clo = _lalo(frame, centre)
        carved = np.asarray(level_at(cla, clo), np.float64)
        level = np.where(np.isfinite(carved), carved + CARVED_ABOVE_M, level)
    depth = np.clip(width / 8.0, *DEPTH_M)
    # the surface in the anchor frame (the curve included)
    _, _, z = frame.geodetic_to_enu(la, lo, level)
    z = np.asarray(z, np.float64)
    near = np.hypot(pts[:, 0] - x, pts[:, 1] - y) <= r + PIECE_M / 2
    keep = ~wet & near
    sq = np.floor(pts / PIECE_M).astype(np.int64)
    out = []
    base = hashlib.sha1("|".join(ids).encode()).hexdigest()[:10]
    i, n = 0, len(pts)
    while i < n:
        if not keep[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and keep[j + 1] and tuple(sq[j + 1]) == tuple(sq[i]):
            j += 1
        # the piece and the first node of the next: they share a node, never a segment (two Rivers over
        # the same stretch drew by turns as the camera moved, 2026-10-13)
        a, b = i, (j + 1 if (j + 1 < n and keep[j + 1]) else j)
        if b > a:
            nodes = tuple((float(centre[k, 0]), float(centre[k, 1]), float(z[k]), float(width[k]), float(depth[k]))
                          for k in range(a, b + 1))
            # the key names the piece and its nodes: a piece whose nodes changed is made again by the mod
            h = hashlib.sha1(repr([tuple(round(v, 1) for v in nd) for nd in nodes]).encode()).hexdigest()[:6]
            out.append(River(f"R{base}_{sq[i][0]}_{sq[i][1]}_{i}_{h}", kind, FLOW_MS[kind], nodes))
        i = j + 1
    return out


def _widths(frame, kind, pts, outline_at):
    """(width, centre) at every node: across the river's outline where the node is in one (its centre
    moved midway between the banks: a centreline need not be in the middle), else WIDTH_M by class."""
    w = np.full(len(pts), WIDTH_M[kind])
    if outline_at is None or len(pts) < 2:
        return w, pts
    t = np.gradient(pts, axis=0)
    t /= np.maximum(np.hypot(t[:, 0], t[:, 1]), 1e-9)[:, None]
    nrm = np.stack([-t[:, 1], t[:, 0]], -1)
    steps = np.arange(WIDTH_STEP_M / 2, WIDTH_MAX_M / 2, WIDTH_STEP_M)
    here = np.asarray(outline_at(*_lalo(frame, pts)), bool)
    side = []
    for sgn in (1.0, -1.0):
        q = pts[:, None, :] + sgn * nrm[:, None, :] * steps[None, :, None]       # [n, s, 2]
        inside = np.asarray(outline_at(*_lalo(frame, q.reshape(-1, 2))), bool).reshape(len(pts), len(steps))
        # the distance to the first sample outside the outline
        first_out = np.where(inside.all(axis=1), len(steps) - 1, np.argmin(inside, axis=1))
        side.append(np.where(here, steps[first_out], 0.0))
    left, right = side
    measured = left + right
    w = np.where(here & (measured > 0), np.maximum(measured, WIDTH_M[kind] / 2), w)
    shift = np.where(here, (left - right) / 2, 0.0)
    shift = _smooth(shift)
    centre = pts + nrm * shift[:, None]
    return np.clip(_smooth(w), 1.0, WIDTH_MAX_M), centre


def _smooth(v):
    """A running median, then a running mean over SMOOTH_N nodes: a centre or a width that changes from
    node to node folds a River's surface over itself (stretched, streaky texture, 2026-10-13)."""
    v = _running_median(v, MEDIAN_N)
    if len(v) < 3:
        return v
    h = SMOOTH_N // 2
    p = np.pad(v, h, mode="edge")
    return np.convolve(p, np.ones(SMOOTH_N) / SMOOTH_N, mode="valid")


def _lalo(frame, xy):
    la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    return np.asarray(la), np.asarray(lo)
