"""Street intersections: curb returns (rounded corners), crosswalks and curb ramps (docs/ARCHITECTURE.md
"Raised sidewalks"; the user, 2026-10-11: "the sidewalks have sharp corners at the intersections and
don't join together seamlessly, also there are no crosswalks or ramps leading to the crosswalks").

Overture splits roads at their junctions, so a junction is a node shared by several road centrelines.
At a node the branches (each road leaving it) are sorted by angle; between two neighbouring branches
i (its left side) and j (its right side) whose roads both have sidewalks and which meet at an angle
within CORNER_ANGLES, the corner is:
    - a curb return: an arc of radius RADIUS tangent to both curb lines (curb line = the carriageway
      edge); the sidewalks are cut exactly at the tangent points (a cross-section square to their
      road), and a corner piece fills between: the curb along the arc, the slab out to the corner of
      the two sidewalks' outer edges;
    - the asphalt between the arc and the curb lines' corner (the old square corner) filled.
On each branch with sidewalks on both sides, a crosswalk (WCUSA crossing_white) crosses the road just
outside its corners, and both sidewalks get a curb ramp there (the curb down to the road, the slab
rising back over RAMP_RUN, a tactile pad on the slope).
Pure functions of the road centrelines in the tile frame; deterministic (sorted inputs).
"""
import math

import numpy as np

RADIUS = 5.0                    # m, curb return radius (US urban 3-9 m)
CORNER_ANGLES = (35.0, 150.0)   # degrees between two branches for a curb return
BRANCH_M = 10.0                 # m of a branch used for its direction
CROSSWALK = {"m": "crossing_white", "tl": 4.8, "p": 3, "w": 3.0}
CW_GAP = 0.5                    # m between a corner's tangent point and its crosswalk
NODE_TOL = 0.5                  # m: centreline vertices this close are one node


def _unit(v):
    n = math.hypot(v[0], v[1])
    return v / n if n > 1e-9 else v


def _left(u):
    return np.array([-u[1], u[0]])


def _line_x(p, d, q, e):
    """Intersection of lines p + t d and q + s e, or None if parallel."""
    den = d[0] * e[1] - d[1] * e[0]
    if abs(den) < 1e-9:
        return None
    w = q - p
    t = (w[0] * e[1] - w[1] * e[0]) / den
    return p + t * d


def _branch_dir(pts, vi, step):
    """Unit direction leaving vertex vi along pts (step +1 forward, -1 backward), over BRANCH_M."""
    p0 = pts[vi, :2]
    k, acc, q = vi, 0.0, p0
    while 0 <= k + step < len(pts) and acc < BRANCH_M:
        k += step
        acc += math.hypot(*(pts[k, :2] - pts[k - step, :2]))
        q = pts[k, :2]
    return _unit(q - p0)


def _z_along(pts, vi, step, t):
    """The centreline height t m from vertex vi along the branch."""
    k, acc = vi, 0.0
    while 0 <= k + step < len(pts):
        seg = math.hypot(*(pts[k + step, :2] - pts[k, :2]))
        if acc + seg >= t:
            f = (t - acc) / seg if seg > 0 else 0.0
            return float(pts[k, 2] + f * (pts[k + step, 2] - pts[k, 2]))
        acc += seg
        k += step
    return float(pts[k, 2])


def find(roads, has_walk, walk_w):
    """Corners, cuts and crosswalks of the junctions among `roads` [(fid, spec, pts (k, 3), lon)].
    has_walk(spec) -> bool; walk_w: sidewalk width. Returns
        corners: [dict(arc (n, 3) curb points, inward (n, 2) unit vectors toward the sidewalk,
                       outer [(x, y, z) ...] the slab's outer corner points, fill (m, 3) asphalt polygon)]
        cuts: {(fid, side): [(node xy, branch u, t)]}: that sidewalk starts t m from the node
        crosswalks: [dict(a (3,), b (3,), w)]: across a road, curb to curb
        ramps: {(fid, side): [(node xy, branch u, t0, t1)]}: curb ramp along that sidewalk
        line_cuts: {fid: [(node xy, branch u, t)]}: that road's markings start t m from the node
                   (beyond its crosswalk)"""
    nodes = {}
    for ri, (fid, spec, pts, lon) in enumerate(roads):
        for vi, p in enumerate(pts):
            key = (round(float(p[0]) / NODE_TOL), round(float(p[1]) / NODE_TOL))
            nodes.setdefault(key, []).append((ri, vi))
    corners, cuts, crosswalks, ramps, line_cuts = [], {}, [], {}, {}
    for key in sorted(nodes):
        inc = nodes[key]
        if len({ri for ri, _ in inc}) < 2:
            continue
        X = roads[inc[0][0]][2][inc[0][1], :2]
        branches = []                           # (angle, ri, vi, step, u, side of the road on u's left)
        for ri, vi in inc:
            pts = roads[ri][2]
            for step in (1, -1):
                if 0 <= vi + step < len(pts):
                    u = _branch_dir(pts, vi, step)
                    branches.append((math.atan2(u[1], u[0]), ri, vi, step, u, step))
        branches.sort(key=lambda b: (b[0], roads[b[1]][0]))
        n = len(branches)
        if n < 2:
            continue
        side_t = {}                             # (branch index, "L"/"R") -> cut t
        for i in range(n):
            j = (i + 1) % n
            ai, ri, vi, si, ui, li = branches[i]
            aj, rj, vj, sj, uj, lj = branches[j]
            theta = math.degrees((aj - ai) % (2 * math.pi))
            if ri == rj and n == 2:
                continue
            spi, spj = roads[ri][1], roads[rj][1]
            if not (has_walk(spi) and has_walk(spj)) or not (CORNER_ANGLES[0] < theta < CORNER_ANGLES[1]):
                continue
            ni, nj = _left(ui), _left(uj)
            wi, wj = spi.width / 2, spj.width / 2
            Z = _line_x(X + ni * (wi + RADIUS), ui, X - nj * (wj + RADIUS), uj)
            if Z is None:
                continue
            Ti, Tj = Z - ni * RADIUS, Z + nj * RADIUS
            ti, tj = float((Ti - X) @ ui), float((Tj - X) @ uj)
            if ti <= 0 or tj <= 0 or ti > 40 or tj > 40:
                continue
            O = _line_x(X + ni * (wi + walk_w), ui, X - nj * (wj + walk_w), uj)
            if O is None:
                continue
            zi = _z_along(roads[ri][2], vi, si, ti)
            zj = _z_along(roads[rj][2], vj, sj, tj)
            a0 = math.atan2(*(Ti - Z)[::-1])
            a1 = math.atan2(*(Tj - Z)[::-1])
            da = (a1 - a0 + math.pi) % (2 * math.pi) - math.pi      # the short way round
            k = max(3, int(abs(da) * RADIUS / 1.0) + 1)
            ang = a0 + da * np.linspace(0.0, 1.0, k)
            arc = np.c_[Z[0] + RADIUS * np.cos(ang), Z[1] + RADIUS * np.sin(ang), np.linspace(zi, zj, k)]
            inward = (Z[None] - arc[:, :2]) / RADIUS
            zo = 0.5 * (zi + zj)
            outer = [(*(Tj - nj * walk_w), zj), (*O, zo), (*(Ti + ni * walk_w), zi)]
            Cc = _line_x(X + ni * wi, ui, X - nj * wj, uj)
            fill = np.vstack([[*Cc, zo]] + [arc[m] for m in range(k)]) if Cc is not None else None
            corners.append({"arc": arc, "inward": inward, "outer": outer, "fill": fill})
            cuts.setdefault((roads[ri][0], li), []).append((X.copy(), ui, ti))
            cuts.setdefault((roads[rj][0], -lj), []).append((X.copy(), uj, tj))
            side_t[(i, "L")] = ti
            side_t[(j, "R")] = tj
        for i in range(n):                      # crosswalk + ramps on a branch with both corners
            if (i, "L") in side_t and (i, "R") in side_t:
                ai, ri, vi, si, ui, li = branches[i]
                w = roads[ri][1].width / 2
                t = max(side_t[(i, "L")], side_t[(i, "R")]) + CW_GAP + CROSSWALK["w"] / 2
                c = X + ui * t
                z = _z_along(roads[ri][2], vi, si, t)
                nl = _left(ui)
                crosswalks.append({"a": np.r_[c + nl * w, z], "b": np.r_[c - nl * w, z], "w": CROSSWALK["w"]})
                t0, t1 = t - CROSSWALK["w"] / 2, t + CROSSWALK["w"] / 2
                ramps.setdefault((roads[ri][0], li), []).append((X.copy(), ui, t0, t1))
                ramps.setdefault((roads[ri][0], -li), []).append((X.copy(), ui, t0, t1))
                line_cuts.setdefault(roads[ri][0], []).append((X.copy(), ui, t1))
    return corners, cuts, crosswalks, ramps, line_cuts


def apply_cuts(line, cuts, near=40.0):
    """A sidewalk centreline (k, 3) with the parts before its junction cuts removed: points within
    `near` m of a node X along branch u with (p - X) . u < t go, and the line is closed at exactly t
    (a cross-section square to the road). Returns the remaining pieces [(pts, (start_cut, end_cut))]
    (whether each end is a junction cut). Pure."""
    pts = np.asarray(line, np.float64)
    if not cuts or len(pts) < 2:
        return [(pts, (False, False))]
    # densify to 0.5 m so a cut lands within the resolution, then exact ends
    seg = np.hypot(*np.diff(pts[:, :2], axis=0).T)
    s = np.r_[0.0, np.cumsum(seg)]
    if s[-1] <= 0:
        return []
    t = np.linspace(0.0, s[-1], max(2, int(s[-1] / 0.5) + 1))
    d = np.stack([np.interp(t, s, pts[:, c]) for c in range(pts.shape[1])], axis=1)
    keep = np.ones(len(d), bool)
    alongs = []
    for X, u, tc in cuts:
        rel = d[:, :2] - X
        along = rel @ u
        lateral = np.abs(rel @ _left(u))
        inside = (along < tc) & (along > -1.0) & (lateral < near)
        keep &= ~inside
        alongs.append((along - tc, inside))

    def exact(k_out, k_in):
        """The point between a removed sample and a kept one where its cut's cross-section lies."""
        for f, inside in alongs:
            if inside[k_out] and not inside[k_in] and f[k_in] != f[k_out]:
                w = f[k_in] / (f[k_in] - f[k_out])
                return d[k_in] + w * (d[k_out] - d[k_in])
        return d[k_in]
    out, start = [], None
    for k in range(len(d) + 1):
        if k < len(d) and keep[k]:
            if start is None:
                start = k
        elif start is not None:
            run = d[start:k].copy()
            cut0 = start > 0
            cut1 = k < len(d)
            if cut0:
                run[0] = exact(start - 1, start)
            if cut1:
                run[-1] = exact(k, k - 1)
            if len(run) >= 2:
                out.append((run, (cut0, cut1)))
            start = None
    return out


def ramp_ranges(run, ramps):
    """Arc-length ranges [(s0, s1)] along a sidewalk run (k, >=2) of its curb ramps. Pure."""
    if not ramps:
        return []
    seg = np.hypot(*np.diff(run[:, :2], axis=0).T)
    s = np.r_[0.0, np.cumsum(seg)]
    out = []
    for X, u, t0, t1 in ramps:
        along = (run[:, :2] - X) @ u
        lateral = np.abs((run[:, :2] - X) @ _left(u))
        m = (lateral < 40.0)
        if not m.any():
            continue
        a = np.interp(t0, along[m], s[m]) if np.all(np.diff(along[m]) > 0) else None
        b = np.interp(t1, along[m], s[m]) if np.all(np.diff(along[m]) > 0) else None
        if a is None and np.all(np.diff(along[m]) < 0):
            a = np.interp(t0, along[m][::-1], s[m][::-1])
            b = np.interp(t1, along[m][::-1], s[m][::-1])
        if a is None or b is None:
            continue
        lo, hi = min(a, b), max(a, b)
        if hi > 0 and lo < s[-1] and hi - lo > 0.5:
            out.append((max(lo, 0.0), min(hi, s[-1])))
    return out
