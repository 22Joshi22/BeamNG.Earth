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
LAMP_SPACING = {"primary": (30.0, True), "secondary": (32.0, True), "tertiary": (36.0, True),
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


def place(roads, walks, box, curb_h=0.15):
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
        rule = LAMP_SPACING.get(spec.cls)
        if rule is None:
            continue
        spacing, both = rule
        side = wk["side"]
        lamp_side = 1 if _h01(fid, "side") < 0.5 else -1
        if not both and side != lamp_side:
            continue
        lit.add(fid)
        run = np.asarray(wk["pts"], np.float64)
        if len(run) < 2:
            continue
        phase = _h01(fid, "phase") * spacing + (spacing / 2 if (both and side < 0) else 0.0)
        s_run = _project(line, run[:, :2])
        lo, hi = min(s_run[0], s_run[-1]), max(s_run[0], s_run[-1])
        k0, k1 = math.ceil((lo - phase) / spacing), math.floor((hi - phase) / spacing)
        if k1 < k0:
            continue
        s_road = phase + spacing * np.arange(k0, k1 + 1)
        # back to the run: the run point nearest each road station
        p_road, _ = _at(line, s_road)
        s_on_run = _project(run, p_road[:, :2])
        L = _arclen(run)[-1]
        for s in s_on_run:
            if s < END_CLEAR_M or s > L - END_CLEAR_M:
                continue
            p, d = _at(run, np.array([s]))
            p, d = p[0], d[0]
            out = np.array([-d[1], d[0]]) * side            # away from the road (sidewalks.run_geometry)
            w = wk.get("w", 2.5)
            q = p[:2] - out * (w / 2 - LAMP_CURB_M)
            if not (x0 <= q[0] < x1 and y0 <= q[1] < y1):
                continue
            rows.append([G_LAMP, round(float(q[0]), 2), round(float(q[1]), 2), round(float(p[2] + curb_h), 2),
                         round(_yaw(-out[0], -out[1]), 1), 1.0, 9.0, 0.0])
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
        for p, d in zip(P, D):
            n = np.array([-d[1], d[0]]) * side
            q = p[:2] + n * (spec.width / 2 + POLE_OFF_M)
            if not (x0 <= q[0] < x1 and y0 <= q[1] < y1):
                continue
            if not _clear(np.array([q[0], q[1], p[2]]), others, fid):
                continue
            # the cross-arm (local x) across the road, the wires (local y) along it
            rows.append([G_POLE, round(float(q[0]), 2), round(float(q[1]), 2), round(float(p[2]), 2),
                         round(_yaw(n[0], n[1]), 1), 1.0, 9.2, 0.0])
    return rows
