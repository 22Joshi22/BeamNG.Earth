"""Traffic islands inside small road loops: roundabouts, turnaround loops, teardrop islands (the user's report
2026-10-14, Harbor Point: a one-way loop and the link closing it each had sidewalks on both sides, and the inner
ones overlapped into a D-shaped slab with stray corner pieces).

find(roads) looks for short cycles of the road graph (endpoints joined within JOIN_M) that enclose a small area,
mostly bounded by one-way roads (a ring is one-way; two-way loops this small are usually city blocks of alleys,
which keep their sidewalks). For each: the side of each bounding road that faces the inside (no sidewalk, gutter
or curb-return corner there) and the island's curb line, the loop inset by each road's half width: the tile gets
a curbed island along it (a sidewalk run of ISLAND_BAND_M inward) and the inside shows the ground (the photo's
planting). Pure.
"""
import math

import numpy as np

JOIN_M = 1.5            # road ends this close are one graph node
MAX_EDGES = 5           # roads in a loop
MAX_LOOP_M = 400.0      # loop length
MAX_AREA_M2 = 1200.0     # a roundabout ~40 m across; city blocks round one-way streets are 2000 m2+ (Fells Point)
MIN_AREA_M2 = 25.0
ONEWAY_SHARE = 0.4      # at least this share of the loop's length one-way
ISLAND_BAND_M = 1.2     # the island's curb band (a sidewalk run this wide, inward)
MIN_INSET_AREA_M2 = 6.0  # an inset loop smaller than this (a thin median): no island ring, only no inner sidewalks


def _area(xy):
    x, y = xy[:, 0], xy[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def _inside(xy, p):
    x, y = p
    xs, ys = xy[:, 0], xy[:, 1]
    xj, yj = np.roll(xs, 1), np.roll(ys, 1)
    cross = ((ys > y) != (yj > y)) & (x < (xj - xs) * (y - ys) / np.where(yj - ys == 0, 1e-12, yj - ys) + xs)
    return bool(np.count_nonzero(cross) % 2)


def _length(pts):
    return float(np.sum(np.hypot(*np.diff(pts[:, :2], axis=0).T)))


def _offset(pts, d):
    """pts (k, >=2) moved d m to its left (mitred joints, as roads.offset_polyline)."""
    import roads
    return roads.offset_polyline(pts, d)


def find(roads_list, is_oneway=lambda spec: bool(getattr(spec, "oneway", False)), structures=()):
    """[{"poly": (k, 2) loop centreline (counter-clockwise), "inner": {fid: side (+1 left / -1 right of its
    geometry)}, "ring": (k, 3) curb line counter-clockwise or None}] of roads_list [(fid, spec, pts (k, 3), lon)].
    structures: building centres (x, y); a loop round one is a block, not an island."""
    nodes = []                       # node xy
    def node_of(p):
        for i, q in enumerate(nodes):
            if math.hypot(p[0] - q[0], p[1] - q[1]) < JOIN_M:
                return i
        nodes.append((float(p[0]), float(p[1])))
        return len(nodes) - 1
    edges = []                       # (fid, spec, pts, a, b, length)
    for fid, spec, pts, _ in roads_list:
        if len(pts) < 2:
            continue
        a, b = node_of(pts[0]), node_of(pts[-1])
        L = _length(pts)
        if L <= 0:
            continue
        edges.append((fid, spec, pts, a, b, L))
    adj = {}
    for k, (_, _, _, a, b, _) in enumerate(edges):
        adj.setdefault(a, []).append(k)
        adj.setdefault(b, []).append(k)
    found, seen = [], set()
    for k0, (fid0, _, _, a0, b0, L0) in enumerate(edges):
        if a0 == b0:                                    # a closed road: a loop by itself
            cycles = [[(k0, True)]]
        else:
            cycles = []
            stack = [(b0, [(k0, True)], L0)]            # walk from b0 back to a0
            while stack:
                n, path, L = stack.pop()
                if len(path) > MAX_EDGES or L > MAX_LOOP_M:
                    continue
                for k in adj.get(n, []):
                    if any(k == q for q, _ in path):
                        continue
                    _, _, _, a, b, Lk = edges[k]
                    fwd = (a == n)
                    nxt = b if fwd else a
                    if nxt == a0:
                        cycles.append(path + [(k, fwd)])
                    elif len(path) < MAX_EDGES:
                        stack.append((nxt, path + [(k, fwd)], L + Lk))
        for cyc in cycles:
            key = frozenset(k for k, _ in cyc)
            if key in seen:
                continue
            seen.add(key)
            L = sum(edges[k][5] for k, _ in cyc)
            ow = sum(edges[k][5] for k, _ in cyc if is_oneway(edges[k][1]))
            if L > MAX_LOOP_M or ow < ONEWAY_SHARE * L:
                continue
            parts = [edges[k][2] if fwd else edges[k][2][::-1] for k, fwd in cyc]
            poly = np.concatenate([p[:-1, :2] for p in parts])
            area = _area(poly)
            if not (MIN_AREA_M2 <= abs(area) <= MAX_AREA_M2):
                continue
            if any(_inside(poly, c) for c in structures):
                continue                                # a building inside: a city block, not an island
            ccw = area > 0
            inner = {}
            for (k, fwd), p in zip(cyc, parts):
                fid = edges[k][0]
                # the traversed part's left is the inside on a counter-clockwise loop
                side_trav = 1 if ccw else -1
                inner[fid] = side_trav if fwd else -side_trav
            if not ccw:
                poly = poly[::-1]
            z = float(np.mean(np.concatenate([edges[k][2][:, 2] for k, _ in cyc])))
            ring = _island_ring(poly, [(edges[k][2][:, :2], edges[k][1].width / 2.0) for k, _ in cyc], z)
            found.append({"poly": poly, "inner": inner, "ring": ring})
    return found


RAYS = 72               # the island's curb traced along this many rays from its centre
RAY_STEP_M = 0.2


def _seg_dist(pts, p):
    """Distance from p to polyline pts (k, 2)."""
    a, b = pts[:-1], pts[1:]
    d = b - a
    L2 = np.maximum((d * d).sum(1), 1e-12)
    t = np.clip(((p - a) * d).sum(1) / L2, 0.0, 1.0)
    q = a + d * t[:, None]
    return float(np.min(np.hypot(*(q - p).T)))


def _island_ring(poly, roads_hw, z):
    """The island's curb line (k, 3) counter-clockwise: from the loop's centre along RAYS rays, the last point
    still clear of every bounding road's carriageway (its centreline +- half width). None for a sliver.
    Ray-traced instead of offsetting each road (offsets spiked where roads meet at sharp angles)."""
    c = poly.mean(0)
    if not _inside(poly, c) or any(_seg_dist(pts, c) <= hw for pts, hw in roads_hw):
        return None
    rmax = float(np.max(np.hypot(*(poly - c).T)))
    out = []
    for a in np.linspace(0.0, 2 * math.pi, RAYS, endpoint=False):
        u = np.array([math.cos(a), math.sin(a)])
        r = 0.0
        while r < rmax:
            q = c + u * (r + RAY_STEP_M)
            if not _inside(poly, q) or any(_seg_dist(pts, q) <= hw for pts, hw in roads_hw):
                break
            r += RAY_STEP_M
        out.append(c + u * r)
    ring = np.array(out)
    # a light smoothing (the rays' steps), the curb keeps its shape
    ring = (np.roll(ring, 1, 0) + 2 * ring + np.roll(ring, -1, 0)) / 4.0
    if abs(_area(ring)) < MIN_INSET_AREA_M2:
        return None
    return np.column_stack([ring, np.full(len(ring), z)])


def inner_sides(found):
    """{(fid, side)} of the road sides facing an island."""
    return {(fid, side) for f in found for fid, side in f["inner"].items()}


def in_island(found, p):
    """Is point p (x, y) inside one of the loops?"""
    return any(_inside(f["poly"], p) for f in found)
