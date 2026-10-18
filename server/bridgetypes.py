"""Bridge structure types and the steel trusses drawn over (or under) a bridge's decks.

The map data says where a bridge is, not what it is: the Queensboro Bridge, a steel cantilever truss, was a plain
concrete deck (the user, 2026-10-17: "Bridges need a lot of work in general"; Mapillary: the truss all along it).
A bridge outline in the map tagged bridge:structure (OSM, carried in Overture's infrastructure "tags") decides
first, anywhere. Where the US National Bridge Inventory covers (bridges_us.npz, tools/build_bridge_db.py: FHWA, public domain) its
main span type decides (item 43B: through or deck truss, arch, suspension, cable-stayed, movable); a bridge there
with no such record is a plain deck. Elsewhere a long span over water is taken for a through truss (the commonest
long steel span of the 20th century; the user's choice, 2026-10-18: "OSM tag + size rule").

truss_members() builds the truss of one chain of deck parts as beams (p, q, width, height) in the anchor frame:
two vertical Warren trusses (chords, verticals, alternating diagonals) at the outer edges of the decks running side
by side, cross beams over the top for a through truss; roads.BridgeProfiles hands each tile those whose middle lies
in it, roadmesh.deck draws them. Pure given the inputs."""
import os

import numpy as np

# NBI item 43B, the main span's design
NBI_DECK_TRUSS, NBI_THROUGH_TRUSS = 9, 10
NBI_DECK_ARCH, NBI_THROUGH_ARCH, NBI_SUSPENSION, NBI_STAYED = 11, 12, 13, 14
NBI_LIFT, NBI_BASCULE, NBI_SWING = 15, 16, 17
TRUSS_TYPES = (NBI_DECK_TRUSS, NBI_THROUGH_TRUSS)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridges_us.npz")
COVER_M = 25000.0       # an NBI record (any notable type) this close: the inventory covers here (no size rule)
MATCH_M = 60.0          # an NBI record's point this close to a chain's deck lines is that chain's
WET_SPAN_M = 150.0      # (size rule, outside the inventory) a run over water this long: a through truss
PANEL_M = 14.0          # m between a truss's verticals
MIN_RUN_M = 24.0        # a truss shorter than this is left out
HIGH_M = 9.0            # m of deck over the ground: the long spans a big truss stands over (its approaches are plain)
LOW_SPAN_M = 60.0       # an NBI span shorter than this: a small truss, over all of its bridge (HIGH_LOW_M)
HIGH_LOW_M = 2.0
SIDE_GAP_M = 0.8        # m from the outermost deck's edge (parapet) to a truss's plane
PARALLEL_COS = 0.94     # decks within ~20 degrees of each other run side by side
SIDE_REACH_M = 30.0     # m sideways a deck is taken as beside another
HEADROOM_M = 6.5        # m from the highest deck inside a through truss to its top chord
PONY_SPAN_M = 35.0      # a through truss with a shorter span is a pony truss: low, no cross beams
CHORD = (0.9, 1.1)      # (width, height) m of the chords
POST = (0.6, 0.7)       # verticals
DIAG = (0.45, 0.6)      # diagonals
BRACE = (0.4, 0.5)      # cross beams over the top

OSM_TYPES = {"truss": NBI_THROUGH_TRUSS, "cantilever": NBI_THROUGH_TRUSS, "arch": NBI_DECK_ARCH,
             "suspension": NBI_SUSPENSION, "cable-stayed": NBI_STAYED}    # bridge:structure; others: a plain deck
OSM_INSIDE = 0.2        # this share of a chain's deck points inside a tagged outline: the chain is that bridge

_db = None


def osm_structure(frame, parts, feats_list):
    """The NBI-style type code of a chain from the bridge outlines tagged bridge:structure among the infrastructure
    Features (feats_list), 0 for one tagged a plain kind (beam, ...), None if none covers the chain."""
    import json
    from polygon import Path
    pts = np.concatenate([np.asarray(xy, np.float64) for xy, _, _ in parts])
    best = None
    for f in feats_list:
        if f is None or not len(f):
            continue
        tags, sub = f.attrs.get("tags"), f.attrs.get("subtype")
        if tags is None:
            continue
        for i in range(len(f.kind)):
            t = str(tags[i])
            if "bridge:structure" not in t or (sub is not None and str(sub[i]) != "bridge") or f.kind[i] != 3:
                continue
            try:
                kind = json.loads(t).get("bridge:structure")
            except ValueError:
                continue
            for part in f.parts(i):
                ring = np.asarray(part[0], np.float64)
                if len(ring) < 4:
                    continue
                e, n, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
                share = float(Path(np.c_[e, n]).contains_points(pts).mean())
                if share >= OSM_INSIDE and (best is None or share > best[0]):
                    best = (share, OSM_TYPES.get(str(kind), 0))
    return None if best is None else best[1]


def _load():
    global _db
    if _db is None:
        try:
            d = np.load(DB_PATH)
            _db = {k: d[k] for k in d.files}
        except OSError:
            _db = {"lat": np.zeros(0, np.float32)}
    return _db


def nbi_near(lat, lon, r_m):
    """Indices into the inventory of the records within r_m of (lat, lon) and their distances (m)."""
    db = _load()
    la = db["lat"]
    if not len(la):
        return np.zeros(0, np.int64), np.zeros(0)
    dlat = r_m / 110540.0
    i0, i1 = np.searchsorted(la, lat - dlat), np.searchsorted(la, lat + dlat)
    idx = np.arange(i0, i1)
    if not len(idx):
        return idx, np.zeros(0)
    kx = 111320.0 * np.cos(np.radians(lat))
    d = np.hypot((db["lon"][idx] - lon) * kx, (la[idx] - lat) * 110540.0)
    keep = d <= r_m
    return idx[keep], d[keep]


def record(i):
    db = _load()
    return {"type": int(db["type"][i]), "span": float(db["span"][i]), "length": float(db["length"][i]),
            "width": float(db["width"][i])}


def _seg_dist(q, xy):
    """Distance (m) from point q (2,) to the polyline xy (k, 2)."""
    a, b = xy[:-1], xy[1:]
    ab = b - a
    t = np.clip(((q - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-9), 0, 1)
    return float(np.min(np.hypot(*(a + t[:, None] * ab - q).T)))


def chain_structure(frame, parts, wet_fn=None, infra=()):
    """The structure of a chain of deck parts [(xy (k, 2), z (k,), width)]: {"type": NBI code, "span": m, "length":
    m, "width": m, "src": "osm" | "nbi" | "size"} or None (a plain deck). wet_fn(xy) -> bool per point; infra: the
    infrastructure Features around it (osm_structure)."""
    pts = np.concatenate([np.asarray(xy, np.float64) for xy, _, _ in parts])
    osm = osm_structure(frame, parts, infra) if infra else None
    if osm == 0:
        return None
    longest = _longest_wet(parts, wet_fn)
    c = pts.mean(0)
    la, lo, _ = frame.enu_to_geodetic(np.array([c[0]]), np.array([c[1]]), 0.0)
    lat, lon = float(la[0]), float(lo[0])
    reach = float(np.hypot(*(pts.max(0) - pts.min(0)))) / 2 + MATCH_M
    idx, _ = nbi_near(lat, lon, reach)
    best = None
    if len(idx):
        db = _load()
        e, n, _ = frame.geodetic_to_enu(db["lat"][idx].astype(np.float64), db["lon"][idx].astype(np.float64), 0.0)
        for i, q in zip(idx, np.c_[e, n]):
            d = min(_seg_dist(q, np.asarray(xy, np.float64)) for xy, _, _ in parts if len(xy) >= 2)
            if d <= MATCH_M and (best is None or d < best[0]):
                best = (d, int(i))
    if osm is not None:                         # the map's tag; the inventory's span if it has the bridge
        span = record(best[1])["span"] if best is not None else (min(longest, 250.0) or 100.0)
        return {"type": osm, "span": span, "length": 0.0, "width": 0.0, "src": "osm"}
    if best is not None:
        return {**record(best[1]), "src": "nbi"}
    if len(nbi_near(lat, lon, COVER_M)[0]):
        return None                             # inside the inventory: no notable record, a plain deck
    if longest >= WET_SPAN_M:
        return {"type": NBI_THROUGH_TRUSS, "span": float(min(longest, 250.0)), "length": 0.0, "width": 0.0,
                "src": "size"}
    return None


def _longest_wet(parts, wet_fn):
    """The longest run (m) of any part over water."""
    if wet_fn is None:
        return 0.0
    longest = 0.0
    for xy, z, w in parts:
        xy = np.asarray(xy, np.float64)
        if len(xy) < 2:
            continue
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
        wet = np.asarray(wet_fn(xy), bool)
        i = 0
        while i < len(wet):
            if not wet[i]:
                i += 1
                continue
            j = i
            while j + 1 < len(wet) and wet[j + 1]:
                j += 1
            longest = max(longest, s[j] - s[i])
            i = j + 1
    return longest


def _runs(mask, s, min_len):
    """[(i, j)] index runs of mask at least min_len long (arc s)."""
    out, i = [], 0
    while i < len(mask):
        if not mask[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(mask) and mask[j + 1]:
            j += 1
        if s[j] - s[i] >= min_len:
            out.append((i, j))
        i = j + 1
    return out


def truss_members(parts, info, ground_fn, wet_fn=None):
    """The truss beams [(p (3,), q (3,), w, h)] of a chain of deck parts [(xy (k, 2), z (k,), width)] whose structure
    (chain_structure) is a truss. ground_fn(xy) -> heights, wet_fn(xy) -> bool. Stations every PANEL_M along each
    part's high runs (longest parts first; a station beside one already placed is skipped: decks side by side share
    one truss); at each, the decks beside it (parallel, within SIDE_REACH_M) give the truss's width and heights. A big
    truss over water stands over the water and a span either side (the Queensboro Bridge's chain reached Queens
    Plaza's ramps: trussed too), one elsewhere over the longest parts' high runs."""
    if info is None or info["type"] not in TRUSS_TYPES:
        return []
    span = info["span"] or 100.0
    small = span < LOW_SPAN_M
    high_m = HIGH_LOW_M if small else HIGH_M
    deck = info["type"] == NBI_DECK_TRUSS
    pony = not deck and span < PONY_SPAN_M
    depth = float(np.clip(span / 8.0, 3.0, 24.0))
    P = [(np.asarray(xy, np.float64), np.asarray(z, np.float64), float(w)) for xy, z, w in parts if len(xy) >= 2]
    if not P:
        return []
    arcs = [np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))] for xy, _, _ in P]
    tans = []
    for xy, _, _ in P:
        t = np.gradient(xy, axis=0)
        tans.append(t / np.maximum(np.hypot(t[:, 0], t[:, 1]), 1e-9)[:, None])
    order = sorted(range(len(P)), key=lambda k: -arcs[k][-1])
    wets = [np.asarray(wet_fn(xy), bool) if wet_fn is not None else np.zeros(len(xy), bool) for xy, _, _ in P]
    any_wet = any(w.any() for w in wets)
    longest = max(a[-1] for a in arcs)
    placed = []                                 # (centre (2,), unit along (2,), half width)
    groups = []                                 # per run: the stations [(L (3,) bottom, R bottom, top z)]
    from roadmesh import DECK_DEPTH, PARAPET_W
    for k in order:
        xy, z, w = P[k]
        s = arcs[k]
        g = np.asarray(ground_fn(xy), np.float64)
        mask = z - g >= high_m
        if not small:
            if any_wet:
                ws = s[wets[k]]
                near = np.min(np.abs(s[:, None] - ws[None, :]), 1) <= span if len(ws) else np.zeros(len(s), bool)
                mask &= near
            elif s[-1] < 0.5 * longest:
                continue
        for i, j in _runs(mask, s, MIN_RUN_M):
            n_st = max(int(round((s[j] - s[i]) / PANEL_M)), 1)
            run = []
            for sc in np.linspace(s[i], s[j], n_st + 1):
                p = np.array([np.interp(sc, s, xy[:, 0]), np.interp(sc, s, xy[:, 1])])
                ii = min(int(np.searchsorted(s, sc)), len(s) - 1)
                t = tans[k][ii]
                nrm = np.array([-t[1], t[0]])
                if any(abs((p - c) @ u) < PANEL_M / 2 and abs((p - c) @ np.array([-u[1], u[0]])) < hw
                       for c, u, hw in placed):
                    if run:
                        groups.append(run)
                        run = []
                    continue
                lo, hi = -w / 2, w / 2
                zlo = zhi = float(np.interp(sc, s, z))
                for k2, (xy2, z2, w2) in enumerate(P):
                    if k2 == k:
                        continue
                    d = np.hypot(*(xy2 - p).T)
                    m = int(np.argmin(d))
                    if d[m] > SIDE_REACH_M or abs(tans[k2][m] @ t) < PARALLEL_COS:
                        continue
                    off = float((xy2[m] - p) @ nrm)
                    if abs(float((xy2[m] - p) @ t)) > PANEL_M:
                        continue
                    lo, hi = min(lo, off - w2 / 2), max(hi, off + w2 / 2)
                    zlo, zhi = min(zlo, float(z2[m])), max(zhi, float(z2[m]))
                lo -= PARAPET_W + SIDE_GAP_M
                hi += PARAPET_W + SIDE_GAP_M
                L, R = p + nrm * hi, p + nrm * lo
                if deck:
                    top, bot = zhi - DECK_DEPTH, zlo - DECK_DEPTH - depth
                elif pony:
                    top, bot = zhi + float(np.clip(span / 10.0, 1.8, 3.5)), zlo - DECK_DEPTH
                else:
                    top, bot = max(zlo - DECK_DEPTH + depth, zhi + HEADROOM_M), zlo - DECK_DEPTH
                run.append((L, R, bot, top))
                placed.append((p, t, (hi - lo) / 2))
            if run:
                groups.append(run)
    beams = []
    for run in groups:
        if len(run) < 2:
            continue
        for side in (0, 1):
            for a in range(len(run)):
                e = run[a][side]
                bot, top = run[a][2], run[a][3]
                beams.append((np.r_[e, bot], np.r_[e, top], *POST))
                if a + 1 < len(run):
                    e2, bot2, top2 = run[a + 1][side], run[a + 1][2], run[a + 1][3]
                    beams.append((np.r_[e, top], np.r_[e2, top2], *CHORD))
                    beams.append((np.r_[e, bot], np.r_[e2, bot2], *CHORD))
                    if a % 2 == 0:
                        beams.append((np.r_[e, bot], np.r_[e2, top2], *DIAG))
                    else:
                        beams.append((np.r_[e, top], np.r_[e2, bot2], *DIAG))
        if not pony and not deck:               # cross beams over the decks, X bracing between them
            for a in range(len(run)):
                Lt, Rt = np.r_[run[a][0], run[a][3]], np.r_[run[a][1], run[a][3]]
                beams.append((Lt, Rt, *BRACE))
                if a + 1 < len(run) and a % 2 == 0:
                    Lt2, Rt2 = np.r_[run[a + 1][0], run[a + 1][3]], np.r_[run[a + 1][1], run[a + 1][3]]
                    beams.append((Lt, Rt2, *BRACE))
                    beams.append((Rt, Lt2, *BRACE))
    return beams
