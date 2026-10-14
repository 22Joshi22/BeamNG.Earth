"""Street furniture (the user, 2026-10-13: "make the world look alive"): street lights along the sidewalks
of town streets and wooden utility poles along country roads, placed per road tile with the road tile's
geometry (roads.build_tile) and shown by the mod as Forest items (trees.lua groups "streetlight", "pole").

Spacing from the stock maps and US practice: West Coast USA's lamps stand ~17 m from their nearest
neighbour (often across the street; ~30-40 m along one side), residential streets in US towns light one
side every ~35-45 m, arterials both sides staggered every ~30 m; utility poles every ~40-50 m on one side.
Each road's lamps sit at fixed distances along the road from its first vertex with a phase hashed from the
road's id, so every tile (and every anchor) puts them in the same places (ROADMAP "deterministic
generation"). Pure.
"""
import hashlib
import math

import numpy as np

# class: (spacing m, both sides staggered)
LAMP_SPACING = {"trunk": (30.0, True), "primary": (30.0, True), "secondary": (32.0, True), "tertiary": (36.0, True),
                "residential": (40.0, False), "unclassified": (42.0, False), "living_street": (36.0, False)}
LAMP_CURB_M = 0.5           # m from the curb onto the sidewalk
POLE_SPACING = 45.0
POLE_CLASSES = {"secondary", "tertiary", "residential", "unclassified"}
POLE_OFF_M = 2.4            # m beyond the carriageway's edge
END_CLEAR_M = 4.0           # m kept clear of a run's ends (corners, crosswalk ramps)
CLEAR_OTHER_M = 1.5         # m kept off another road's carriageway
G_LAMP, G_POLE = "streetlight", "pole"


def _h01(*vals):
    h = hashlib.blake2b(repr(vals).encode(), digest_size=8).digest()
    return int.from_bytes(h, "little") / 2.0 ** 64


def _arclen(p):
    d = np.hypot(*np.diff(p[:, :2], axis=0).T)
    return np.concatenate([[0.0], np.cumsum(d)])


def _project(line, q):
    """Arc length along polyline line (k, 2+) of the nearest point to each q (m, 2)."""
    a, b = line[:-1, :2], line[1:, :2]
    d = b - a
    L2 = np.maximum((d * d).sum(1), 1e-12)
    t = np.clip(((q[:, None, :] - a[None]) * d[None]).sum(2) / L2[None], 0, 1)
    p = a[None] + t[..., None] * d[None]
    dist = np.hypot(*(q[:, None, :] - p).transpose(2, 0, 1))
    k = dist.argmin(1)
    s = _arclen(line)
    return s[k] + t[np.arange(len(q)), k] * np.sqrt(L2[k])


def _at(line, s):
    """Points (and unit directions) at arc lengths s along line (k, 3)."""
    ss = _arclen(line)
    s = np.clip(s, 0, ss[-1])
    k = np.clip(np.searchsorted(ss, s, side="right") - 1, 0, len(line) - 2)
    seg = line[k + 1] - line[k]
    L = np.maximum(ss[k + 1] - ss[k], 1e-9)
    t = (s - ss[k]) / L
    p = line[k] + seg * t[:, None]
    d = seg[:, :2] / L[:, None]
    return p, d


def _yaw(dx, dy):
    """Degrees of a Forest item's rotation that turns its local +x toward (dx, dy) (trees.lua: setFromEuler
    with -yaw about z, the engine's clockwise headings)."""
    return math.degrees(math.atan2(dy, dx)) % 360.0


def _clear(p, others, own):
    for fid, half, line in others:
        if fid == own:
            continue
        a, b = line[:-1, :2], line[1:, :2]
        d = b - a
        L2 = np.maximum((d * d).sum(1), 1e-12)
        t = np.clip(((p[:2] - a) * d).sum(1) / L2, 0, 1)
        q = a + t[:, None] * d
        if np.hypot(*(q - p[:2]).T).min() < half + CLEAR_OTHER_M:
            return False
    return True


WIRE_ARMS = (-0.8, 0.0, 0.8)    # m along the cross-arm (the pole's local x) of the three wires
WIRE_Z = 7.9                    # m above the pole's base (s_powerpole_3wire: 8.2 m tall above its origin)
WIRE_SAG = 0.012                # sag per metre of span
WIRE_SEG_M = 5.0
WIRE_R = 0.012                  # m half width of a wire's ribbons


def wire_lines(poles):
    """Polylines (k, 3) of the wires between consecutive poles [(x, y, z, yaw_deg)] of one road side."""
    out = []
    for (x0, y0, z0, w0), (x1, y1, z1, w1) in zip(poles[:-1], poles[1:]):
        span = math.hypot(x1 - x0, y1 - y0)
        if span < 5.0 or span > 80.0:
            continue
        k = max(2, int(math.ceil(span / WIRE_SEG_M)))
        t = np.linspace(0.0, 1.0, k + 1)
        for off in WIRE_ARMS:
            a = np.array([x0 + math.cos(math.radians(w0)) * off, y0 + math.sin(math.radians(w0)) * off, z0 + WIRE_Z])
            b = np.array([x1 + math.cos(math.radians(w1)) * off, y1 + math.sin(math.radians(w1)) * off, z1 + WIRE_Z])
            p = a[None] + (b - a)[None] * t[:, None]
            p[:, 2] -= 4.0 * WIRE_SAG * span * t * (1.0 - t)
            out.append(p)
    return out


def wire_mesh(lines, origin):
    """A mesh of the wires: two crossed ribbons per segment (seen from any side): (positions, tris, uvs)."""
    P, T = [], []
    ox, oy, oz = origin
    for line in lines:
        for a, b in zip(line[:-1], line[1:]):
            d = b - a
            L = float(np.linalg.norm(d))
            if L < 1e-6:
                continue
            d /= L
            side = np.cross(d, [0.0, 0.0, 1.0])
            side = side / (np.linalg.norm(side) or 1.0) * WIRE_R
            up = np.cross(side, d)
            up = up / (np.linalg.norm(up) or 1.0) * WIRE_R
            for v in (side, up):
                i = len(P)
                P += [a - v, b - v, b + v, a + v]
                T += [(i, i + 1, i + 2), (i, i + 2, i + 3), (i, i + 2, i + 1), (i, i + 3, i + 2)]
    if not P:
        return None
    P = np.array(P) - np.array([ox, oy, oz])
    return P, np.array(T, np.int64), np.zeros((len(P), 2))


MAJOR = {"primary", "secondary", "tertiary", "trunk"}
MINOR = {"residential", "unclassified", "living_street"}
G_SIGNAL, G_STOP = "signal", "stop"
JUNCTION_SNAP_M = 1.0       # road ends this close are one junction


def _arms(roads):
    """Junctions: {(x, y) rounded: [(fid, spec, unit vector away from the junction), ...]} of road ends that
    meet (Overture splits segments at their connectors)."""
    ends = {}
    for fid, spec, pts, _ in roads:
        if len(pts) < 2:
            continue
        for p, q in ((pts[0], pts[1]), (pts[-1], pts[-2])):
            d = q[:2] - p[:2]
            L = float(np.hypot(*d))
            if L < 1e-3:
                continue
            k = (round(float(p[0]) / JUNCTION_SNAP_M), round(float(p[1]) / JUNCTION_SNAP_M))
            ends.setdefault(k, []).append((fid, spec, d / L, p))
    return ends


def junction_controls(arms, k):
    """[(arm, G_SIGNAL | G_STOP)] of an urban junction's arms (fid, spec, unit vector away, point): signals on
    every arm where two or more major roads meet, stop signs on the minor arms meeting a major road, on the
    stem of a minor T and on half the minor four-ways (hashed by k, the junction's key). Shared with
    navgraph.py, so the AI's signals and stop signs are where the props stand."""
    major = [a for a in arms if a[1].cls in MAJOR]
    minor = [a for a in arms if a[1].cls in MINOR]
    signalled = len(major) >= 3 or (len(major) >= 2 and len(arms) >= 4)
    if signalled:
        return [(a, G_SIGNAL) for a in arms if a[1].cls in MAJOR | MINOR]
    if major:
        return [(a, G_STOP) for a in minor]
    if len(arms) == 3:
        # the stem: the arm least in line with another
        def straightest(a):
            return max(-float(a[2] @ b[2]) for b in arms if b is not a)
        return [(min(arms, key=straightest), G_STOP)] if all(a[1].cls in MINOR for a in arms) else []
    return [(a, G_STOP) for a in minor] if _h01("allway", k) < 0.5 else []


def junction_props(roads, box, curb_h=0.15, left_hand=False):
    """Signals at urban junctions of two or more major roads (every arm), stop signs at urban junctions
    of minor roads (the minor arms meeting a major road; the stem of a T; half the four-way ones, hashed).
    Each on the approach's right (its left where traffic drives on the left), at the junction's edge, facing
    the drivers who come in."""
    x0, y0, x1, y1 = box
    rows = []
    for k, arms in _arms(roads).items():
        if len(arms) < 3 or not all(a[1].urban for a in arms):
            continue
        p = arms[0][3]
        if not (x0 <= p[0] < x1 and y0 <= p[1] < y1):
            continue
        put = junction_controls(arms, k)
        if not put:
            continue
        wide = max(a[1].width for a in arms) / 2
        for (fid, spec, u, c), what in put:
            # the approach comes in along -u; its right is (-u_y, u_x)... rotated: right of travel (-u)
            inc = -u
            right = np.array([inc[1], -inc[0]]) * (-1 if left_hand else 1)     # the kerb side of the approach
            q = c[:2] + u * (wide + 2.5) + right * (spec.width / 2 + 1.0)
            if not (x0 - 20 <= q[0] < x1 + 20 and y0 - 20 <= q[1] < y1 + 20):
                continue
            rows.append([what, round(float(q[0]), 2), round(float(q[1]), 2), round(float(c[2] + curb_h), 2),
                         round(_yaw(right[0], right[1]), 1), 1.0, 9.4 if what == G_SIGNAL else 3.0, 0.0])
    return rows


G_HYDRANT, G_BENCH, G_BIN = "hydrant", "bench", "bin"
G_STREET_TREE = "deciduous"     # trees.lua's broadleaf group (the variant nearest each tree's height)
STREET_TREE_CLASSES = {"residential", "tertiary", "secondary", "living_street"}
STREET_TREE_SHARE = 0.5         # streets with a row of trees (hashed per street)
STREET_TREE_SPACING = 11.0
STREET_TREE_CURB_M = 0.9
G_POLELAMP = "polelamp"     # a cobra-head arm on a utility pole (East Coast USA's electrical_pole_lamp)
TOWN_POLE_CLASSES = {"residential", "tertiary", "unclassified", "secondary", "living_street", "primary", "trunk"}
TOWN_POLE_SPACING = 40.0
POLE_LAMP_Z = 7.0           # m above the pole's base
HYDRANT_SPACING = 120.0
BENCH_SPACING = 140.0
BIN_SPACING = 90.0
BENCH_YAW = 90.0            # the bench model's front is its local +y (the yaw puts +x toward the road)


def _stations(run, line, spacing, phase, clear, side):
    """(point (3,), away-from-the-road unit (2,)) on a sidewalk run at the road's stations phase + k spacing
    (arc length along the road line, so every tile and anchor gets the same ones), END_CLEAR_M from the run's
    ends."""
    s_run = _project(line, run[:, :2])
    lo, hi = min(s_run[0], s_run[-1]), max(s_run[0], s_run[-1])
    k0, k1 = math.ceil((lo - phase) / spacing), math.floor((hi - phase) / spacing)
    if k1 < k0:
        return []
    p_road, _ = _at(line, phase + spacing * np.arange(k0, k1 + 1))
    s_on_run = _project(run, p_road[:, :2])
    L = _arclen(run)[-1]
    out = []
    for s in s_on_run:
        if s < clear or s > L - clear:
            continue
        p, d = _at(run, np.array([s]))
        out.append((p[0], np.array([-d[0][1], d[0][0]]) * side))   # away from the road (sidewalks.run_geometry)
    return out


def place(roads, walks, box, curb_h=0.15, wires=None, town=False, left_hand=False):
    """Rows [group, x, y, z, yaw, scale, height, rank] (trees.lua's tile rows) of the lamps and poles of a
    tile box (x0, y0, x1, y1). roads: build_tile's (fid, spec, centreline (k, 3), lon); walks: its sidewalk
    runs (dicts with fid, pts, side, w)."""
    x0, y0, x1, y1 = box
    by_fid = {fid: (spec, pts) for fid, spec, pts, _ in roads}
    others = [(fid, spec.width / 2, pts) for fid, spec, pts, _ in roads]
    rows = []
    lit = set()
    for wk in walks:
        fid = wk.get("fid")
        if fid is None or "pts" not in wk or fid not in by_fid:
            continue
        spec, line = by_fid[fid]
        run = np.asarray(wk["pts"], np.float64)
        if len(run) < 2:
            continue
        side = wk["side"]
        w = wk.get("w", 2.5)

        def put(group, spacing, phase, curb_off, height, yaw_fn, clear=END_CLEAR_M):
            for p, out in _stations(run, line, spacing, phase, clear, side):
                q = p[:2] - out * (w / 2 - curb_off)
                if x0 <= q[0] < x1 and y0 <= q[1] < y1:
                    rows.append([group, round(float(q[0]), 2), round(float(q[1]), 2), round(float(p[2] + curb_h), 2),
                                 round(yaw_fn(out), 1), 1.0, height, 0.0])
        rule = LAMP_SPACING.get(spec.cls)
        if town and spec.cls in TOWN_POLE_CLASSES:
            # towns and suburbs: wooden utility poles along one side, wired, a cobra-head lamp on every other
            pole_side = 1 if _h01(fid, "side") < 0.5 else -1
            if side == pole_side:
                lit.add(fid)
                run_poles = []
                ph = _h01(fid, "tphase") * TOWN_POLE_SPACING
                for p, out in _stations(run, line, TOWN_POLE_SPACING, ph, END_CLEAR_M, side):
                    q = p[:2] - out * (w / 2 - 0.6)
                    yaw = _yaw(out[0], out[1])
                    run_poles.append((float(q[0]), float(q[1]), float(p[2]), yaw))
                    if x0 <= q[0] < x1 and y0 <= q[1] < y1:
                        rows.append([G_POLE, round(float(q[0]), 2), round(float(q[1]), 2), round(float(p[2]), 2),
                                     round(yaw, 1), 1.0, 9.2, 0.0])
                        k = int(round((float(_project(line, q[None])[0]) - ph) / TOWN_POLE_SPACING))
                        if k % 2 == 0:
                            rows.append([G_POLELAMP, round(float(q[0]), 2), round(float(q[1]), 2),
                                         round(float(p[2]) + POLE_LAMP_Z, 2), round(yaw, 1), 1.0, 1.1, 0.0])
                if wires is not None and len(run_poles) > 1:
                    wires.extend(_own_spans(run_poles, box))
            rule = None
        if rule is not None:
            spacing, both = rule
            lamp_side = 1 if _h01(fid, "side") < 0.5 else -1
            if both or side == lamp_side:
                lit.add(fid)
                phase = _h01(fid, "phase") * spacing + (spacing / 2 if (both and side < 0) else 0.0)
                put(G_LAMP, spacing, phase, LAMP_CURB_M, 9.0, lambda out: _yaw(-out[0], -out[1]))
        if spec.urban and spec.cls in STREET_TREE_CLASSES and _h01(fid, "trees") < STREET_TREE_SHARE:
            # street trees in pits along the curb (the photos' dense streets have rows of them; the canopy
            # model has few there): small broadleaves, both sides, clear of the junctions
            for p, out in _stations(run, line, STREET_TREE_SPACING, _h01(fid, side, "tph") * STREET_TREE_SPACING,
                                    END_CLEAR_M + 4.0, side):
                q = p[:2] - out * (w / 2 - STREET_TREE_CURB_M)
                if x0 <= q[0] < x1 and y0 <= q[1] < y1:
                    h = 6.5 + 3.5 * _h01(fid, round(float(q[0]), 1), round(float(q[1]), 1))
                    rows.append([G_STREET_TREE, round(float(q[0]), 2), round(float(q[1]), 2), round(float(p[2] + curb_h), 2),
                                 round(_h01(q[0], q[1], "ty") * 360.0, 1), 1.0, round(h, 1),
                                 round(_h01(fid, q[0], "tr"), 3)])
        if spec.urban:
            # fire hydrants at the curb (US practice ~90-150 m apart), alternating sides
            put(G_HYDRANT, HYDRANT_SPACING, _h01(fid, "hyd") * HYDRANT_SPACING + (HYDRANT_SPACING / 2 if side < 0 else 0),
                0.45, 0.9, lambda out: _h01(fid, "hy") * 360.0)
        if spec.urban and spec.cls in MAJOR:
            # benches at the back of the sidewalk facing the street, bins at the curb
            put(G_BENCH, BENCH_SPACING, _h01(fid, side, "bench") * BENCH_SPACING, w - 0.45, 0.9,
                lambda out: _yaw(-out[0], -out[1]) + BENCH_YAW)
            put(G_BIN, BIN_SPACING, _h01(fid, side, "bin") * BIN_SPACING, 0.7, 1.1, lambda out: _yaw(-out[0], -out[1]))
    rows.extend(junction_props(roads, box, curb_h, left_hand))
    for fid, spec, line, _ in roads:
        if spec.urban or fid in lit or spec.cls not in POLE_CLASSES or len(line) < 2:
            continue
        side = 1 if _h01(fid, "pside") < 0.5 else -1
        phase = _h01(fid, "pphase") * POLE_SPACING
        L = _arclen(line)[-1]
        s = phase + POLE_SPACING * np.arange(0, int((L - phase) // POLE_SPACING) + 1)
        if not len(s):
            continue
        P, D = _at(line, s)
        run = []
        for p, d in zip(P, D):
            n = np.array([-d[1], d[0]]) * side
            q = p[:2] + n * (spec.width / 2 + POLE_OFF_M)
            inside = x0 <= q[0] < x1 and y0 <= q[1] < y1
            if not _clear(np.array([q[0], q[1], p[2]]), others, fid):
                if len(run) > 1 and wires is not None:
                    wires.extend(_own_spans(run, box))
                run = []
                continue
            # the cross-arm (local x) across the road, the wires (local y) along it
            yaw = _yaw(n[0], n[1])
            run.append((float(q[0]), float(q[1]), float(p[2]), yaw))
            if inside:
                rows.append([G_POLE, round(float(q[0]), 2), round(float(q[1]), 2), round(float(p[2]), 2),
                             round(yaw, 1), 1.0, 9.2, 0.0])
        if len(run) > 1 and wires is not None:
            wires.extend(_own_spans(run, box))
    return rows


def _own_spans(run, box):
    """The wires of the spans of a run of poles that start in the tile (each span drawn by one tile)."""
    x0, y0, x1, y1 = box
    out = []
    for a, b in zip(run[:-1], run[1:]):
        if x0 <= a[0] < x1 and y0 <= a[1] < y1:
            out.extend(wire_lines([a, b]))
    return out
