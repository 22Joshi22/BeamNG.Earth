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
ARCH_ANY = 111          # (not NBI) an arch over or under its deck as its height allows: OSM's bridge:structure=arch
TRUSS_TYPES = (NBI_DECK_TRUSS, NBI_THROUGH_TRUSS)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridges_us.npz")
COVER_M = 25000.0       # an NBI record (any notable type) this close: the inventory covers here (no size rule)
MATCH_M = 60.0          # an NBI record's point this close to a chain's deck lines is that chain's
WET_SPAN_M = 150.0      # (size rule, outside the inventory) a run over water this long: a through truss
PANEL_M = 14.0          # m between a truss's verticals
ANCHOR_ARM = 0.75       # a big truss reaches this share of its main span past the water (the Queensboro Bridge's
                        # anchor arms: 143 and 140 m for its 182 and 299 m channel spans)
ROAD_GAP_M = 3.0        # m an anchor pier keeps off a road's centre line under it
ANCHOR_T = 6.0          # m: the thickness along the bridge of the solid pier under each end of a big truss
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

OSM_TYPES = {"truss": NBI_THROUGH_TRUSS, "cantilever": NBI_THROUGH_TRUSS, "arch": ARCH_ANY,
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


def matches(frame, lines):
    """[(distance m, index)] of the inventory's records within MATCH_M of the deck lines (k, 2+), nearest first."""
    lines = [np.asarray(ln, np.float64)[:, :2] for ln in lines if len(ln) >= 2]
    if not lines:
        return []
    pts = np.concatenate(lines)
    c = pts.mean(0)
    la, lo, _ = frame.enu_to_geodetic(np.array([c[0]]), np.array([c[1]]), 0.0)
    reach = float(np.hypot(*(pts.max(0) - pts.min(0)))) / 2 + MATCH_M
    idx, _ = nbi_near(float(la[0]), float(lo[0]), reach)
    if not len(idx):
        return []
    db = _load()
    e, n, _ = frame.geodetic_to_enu(db["lat"][idx].astype(np.float64), db["lon"][idx].astype(np.float64), 0.0)
    out = []
    for i, q in zip(idx, np.c_[e, n]):
        d = min(_seg_dist(q, ln) for ln in lines)
        if d <= MATCH_M:
            out.append((d, int(i)))
    return sorted(out)


def nav_clearance(frame, lines):
    """m: the largest navigation clearance (NBI item 39) of the inventory's records on these deck lines, 0 if none."""
    db = _load()
    if "nav" not in db:
        return 0.0
    return max((float(db["nav"][i]) for _, i in matches(frame, lines)), default=0.0)


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
    found = matches(frame, [xy for xy, _, _ in parts])
    notable = [m for m in found if NBI_DECK_TRUSS <= int(_load()["type"][m[1]]) <= NBI_SWING]
    best = notable[0] if notable else (found[0] if found else None)
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


def _clear_of_roads(foot, road_lines, gap):
    """Whether no road line (k, 2) comes within gap m of any of the points foot (n, 2)."""
    for ln in road_lines:
        ln = np.asarray(ln, np.float64)[:, :2]
        if len(ln) >= 2 and min(_seg_dist(q, ln) for q in foot) < gap:
            return False
    return True


def truss_members(parts, info, ground_fn, wet_fn=None, with_piers=False, road_lines=()):
    """The truss beams [(p (3,), q (3,), w, h)] of a chain of deck parts [(xy (k, 2), z (k,), width)] whose structure
    (chain_structure) is a truss. ground_fn(xy) -> heights, wet_fn(xy) -> bool. Stations every PANEL_M along each
    part's high runs (longest parts first; a station beside one already placed is skipped: decks side by side share
    one truss); at each, the decks beside it (parallel, within SIDE_REACH_M) give the truss's width and heights. A big
    truss over water stands over the water and a span either side (the Queensboro Bridge's chain reached Queens
    Plaza's ramps: trussed too), one elsewhere over the longest parts' high runs. with_piers: (beams, piers): the solid
    piers under a big truss's ends (roads.pier_sites tuples + their thickness, ANCHOR_T), moved along the bridge off
    the roads under it (road_lines: [(k, 2)], or a function of the pier's foot points (n, 2) giving them) or left
    out."""
    piers = []
    if info is None or info["type"] not in TRUSS_TYPES:
        return ([], []) if with_piers else []
    span = info["span"] or 100.0
    small = span < LOW_SPAN_M
    high_m = HIGH_LOW_M if small else HIGH_M
    deck = info["type"] == NBI_DECK_TRUSS
    pony = not deck and span < PONY_SPAN_M
    depth = float(np.clip(span / 8.0, 3.0, 24.0))
    P = [(np.asarray(xy, np.float64), np.asarray(z, np.float64), float(w)) for xy, z, w in parts if len(xy) >= 2]
    if not P:
        return ([], []) if with_piers else []
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
                near = (np.min(np.abs(s[:, None] - ws[None, :]), 1) <= ANCHOR_ARM * span if len(ws)
                        else np.zeros(len(s), bool))
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
                run.append((L, R, bot, top, t))
                placed.append((p, t, (hi - lo) / 2))
            if run:
                groups.append(run)
    beams = []
    for run in groups:
        if len(run) < 2:
            continue
        if not small:                           # a solid pier under each end (the anchor piers)
            for (L, R, bot, top, t), inward in ((run[0], 1.0), (run[-1], -1.0)):
                wd = float(np.hypot(*(L - R)))
                for off in (0.0, 0.5, 1.0, 1.5, 2.0):              # panels toward the truss's middle
                    c = (L + R) / 2 + t * inward * off * PANEL_M
                    nrm = (L - R) / max(wd, 1e-9)
                    foot = np.array([c + nrm * a * wd / 2 + t * b * ANCHOR_T / 2
                                     for a in (-1, 0, 1) for b in (-1, 0, 1)])
                    if not _clear_of_roads(foot, road_lines(foot) if callable(road_lines) else road_lines,
                                           ROAD_GAP_M):
                        continue
                    gz = float(np.asarray(ground_fn(c[None]), np.float64)[0])
                    if bot - gz > 2.0:
                        piers.append((float(c[0]), float(c[1]), float(t[0]), float(t[1]), float(bot), gz - 2.0, wd,
                                      ANCHOR_T))
                    break
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
    return (beams, piers) if with_piers else beams


# --- arches, suspension and cable-stayed spans ------------------------------------------------------------------
# One main span per chain, on its longest part: centred on its longest run over water (else where the deck stands
# highest over the ground), as long as the inventory's span (else most of that run). Sydney Harbour Bridge was a
# plain deck between the city's towers where the photos show its steel arch (Mapillary, 2026-10-18).
ARCH_TYPES = (NBI_DECK_ARCH, NBI_THROUGH_ARCH, ARCH_ANY)
ARCH_SPAN_SHARE = 1.05  # (no inventory span) an arch springs from the banks: a little more than the water
SPAN_SHARE = 0.85       # (no inventory span) the main span: this share of the run over water it stands over
SPAN_MAX = 1300.0       # m
SPAN_MIN = 40.0         # m: shorter main spans are left plain
ARCH_RISE = 1 / 5.0     # a deck arch's rise from its springings: a share of its span
THROUGH_TOP = 1 / 6.5   # a through arch's crown over the deck: a share of its span (Sydney: 503 m, ~75 m)
ARCH_TRUSS_M = 200.0    # an arch at least this long has trussed ribs ...
ARCH_DEPTH_CROWN = 1 / 28.0     # ... this deep at the crown (a share of the span: Sydney 18 m) ...
ARCH_DEPTH_END = 1 / 9.0        # ... and at the bearings (Sydney 57 m)
TIED_SPAN_M = 300.0     # a through arch shorter than this is tied: it springs at the deck (Pittsburgh's)
SUSP_TOWER = 1 / 9.0    # a suspension tower's top over the deck: a share of the main span (Golden Gate: 1280, 150)
SUSP_SIDE = 0.35        # the side spans: a share of the main span past each tower
SUSP_LOW_M = 3.0        # m: the main cable's lowest point over the deck
STAYED_TOWER = 1 / 4.0  # a cable-stayed pylon's top over the deck: a share of the main span
HANGER_M = 12.0         # m between hangers / stays / spandrel posts


def _station(P, tans, k, sc, arcs):
    """At arc sc of part k: (point (2,), unit along, unit left, lo, hi (offsets of the outermost decks' edges beside it,
    PARAPET_W and SIDE_GAP_M outside), z low, z high of those decks)."""
    from roadmesh import PARAPET_W
    xy, z, w = P[k]
    s = arcs[k]
    p = np.array([np.interp(sc, s, xy[:, 0]), np.interp(sc, s, xy[:, 1])])
    ii = min(int(np.searchsorted(s, sc)), len(s) - 1)
    t = tans[k][ii]
    nrm = np.array([-t[1], t[0]])
    lo, hi = -w / 2, w / 2
    zlo = zhi = float(np.interp(sc, s, z))
    for k2, (xy2, z2, w2) in enumerate(P):
        if k2 == k:
            continue
        d = np.hypot(*(xy2 - p).T)
        m = int(np.argmin(d))
        if d[m] > SIDE_REACH_M or abs(tans[k2][m] @ t) < PARALLEL_COS or abs(float((xy2[m] - p) @ t)) > PANEL_M:
            continue
        off = float((xy2[m] - p) @ nrm)
        lo, hi = min(lo, off - w2 / 2), max(hi, off + w2 / 2)
        zlo, zhi = min(zlo, float(z2[m])), max(zhi, float(z2[m]))
    g = PARAPET_W + SIDE_GAP_M
    return p, t, nrm, lo - g, hi + g, zlo, zhi


def _prep(parts):
    P = [(np.asarray(xy, np.float64), np.asarray(z, np.float64), float(w)) for xy, z, w in parts if len(xy) >= 2]
    arcs = [np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))] for xy, _, _ in P]
    tans = []
    for xy, _, _ in P:
        t = np.gradient(xy, axis=0)
        tans.append(t / np.maximum(np.hypot(t[:, 0], t[:, 1]), 1e-9)[:, None])
    return P, arcs, tans


def main_span(P, arcs, info, ground_fn, wet_fn=None):
    """(part index, centre arc, span m) of a chain's main span, or None."""
    if not P:
        return None
    k = int(np.argmax([a[-1] for a in arcs]))
    xy, z, _ = P[k]
    s = arcs[k]
    run = None
    if wet_fn is not None:
        runs = _runs(np.asarray(wet_fn(xy), bool), s, 1.0)
        if runs:
            i, j = max(runs, key=lambda r: s[r[1]] - s[r[0]])
            run = (s[i], s[j])
    if run is None:
        h = z - np.asarray(ground_fn(xy), np.float64)
        m = int(np.argmax(h))
        if h[m] < 8.0:
            return None
        i = j = m                                   # the run around the highest point over half its height
        while i > 0 and h[i - 1] >= 0.5 * h[m]:
            i -= 1
        while j < len(h) - 1 and h[j + 1] >= 0.5 * h[m]:
            j += 1
        run = (s[i], s[j])
    c = (run[0] + run[1]) / 2
    share = ARCH_SPAN_SHARE if info["type"] in ARCH_TYPES else SPAN_SHARE
    span = info.get("span") if info.get("src") == "nbi" and info.get("span") else share * (run[1] - run[0])
    span = float(min(span, SPAN_MAX, s[-1] * 0.9))
    if span < SPAN_MIN:
        return None
    c = float(np.clip(c, span / 2, s[-1] - span / 2))
    return k, c, span


def _ribs(P, tans, arcs, k, s0, s1, n):
    """n + 1 stations from arc s0 to s1 on part k: [(s, p, t, nrm, lo, hi, zlo, zhi)]."""
    out = []
    for sc in np.linspace(s0, s1, n + 1):
        out.append((float(sc),) + _station(P, tans, k, float(sc), arcs))
    return out


def _sz(span, a, lo, hi):
    return float(np.clip(span * a, lo, hi))


def arch_members(P, arcs, tans, ms, info, ground_fn):
    """The ribs, hangers / spandrel posts and bracing of an arch over (through) or under (deck) its main span."""
    k, c, L = ms
    n = max(int(round(L / HANGER_M)), 4)
    st = _ribs(P, tans, arcs, k, c - L / 2, c + L / 2, n)
    deck_c = st[n // 2][7]
    ends = np.array([st[0][1], st[-1][1]])
    g_ends = np.asarray(ground_fn(ends), np.float64)
    g_mid = float(np.asarray(ground_fn(st[n // 2][1][None]), np.float64)[0])
    through = info["type"] == NBI_THROUGH_ARCH or (info["type"] != NBI_DECK_ARCH and deck_c - g_mid < 0.9 * ARCH_RISE * L)
    rw, rh = _sz(L, 1 / 120, 1.0, 4.5), _sz(L, 1 / 60, 1.2, 9.0)        # rib (width, depth)
    beams = []
    if through:
        tied = L < TIED_SPAN_M
        z0 = [st[0][6] - 1.0, st[-1][6] - 1.0] if tied else [max(float(g), st[0][6] - L / 8) for g in g_ends]
        top = deck_c + THROUGH_TOP * L
    else:
        z0 = [float(g) for g in g_ends]
        top = deck_c - 1.5 - rh / 2
    x = np.linspace(-1, 1, n + 1)
    base = z0[0] + (z0[1] - z0[0]) * (x + 1) / 2
    zr = base + (top - base) * (1 - x ** 2)
    trussed = L >= ARCH_TRUSS_M
    dz_t = (ARCH_DEPTH_CROWN + (ARCH_DEPTH_END - ARCH_DEPTH_CROWN) * x ** 2) * L if trussed else np.zeros(n + 1)
    if not through:                                 # a deck arch's upper chord stays under the deck
        dz_t = np.clip(np.minimum(dz_t, np.array([s[6] for s in st]) - 1.5 - rh / 2 - zr), 0.0, None)
    for side in (0, 1):
        pts = [np.r_[(s[1] + s[3] * (s[5] if side else s[4])), z] for s, z in zip(st, zr)]
        for a in range(n):
            beams.append((pts[a], pts[a + 1], rw, rh))
        if trussed:                                 # a trussed rib (Sydney's): an upper chord, posts, diagonals
            up = [p + np.r_[0.0, 0.0, d] for p, d in zip(pts, dz_t)]
            for a in range(n):
                beams.append((up[a], up[a + 1], rw, rh))
                beams.append((pts[a], up[a], rw * 0.6, rw * 0.6))
                beams.append((pts[a], up[a + 1]) if a % 2 == 0 else (up[a], pts[a + 1]))
                beams[-1] = beams[-1] + (rw * 0.5, rw * 0.5)
            beams.append((pts[n], up[n], rw * 0.6, rw * 0.6))
        for a in range(1, n):
            s = st[a]
            e = s[1] + s[3] * (s[5] if side else s[4])
            dz = zr[a] - s[7]
            if through and dz > 1.0:                    # a hanger down to the deck
                beams.append((np.r_[e, zr[a] - rh / 2], np.r_[e, s[6] - 0.8], 0.25, 0.25))
            elif not through and s[6] - zr[a] > 1.5:    # a spandrel post up to the deck
                beams.append((np.r_[e, zr[a] + rh / 2], np.r_[e, s[6] - 1.2], 0.8, 0.8))
    for a in range(1, n):                                # bracing between the ribs, clear of the traffic
        s = st[a]
        if not through or zr[a] - s[7] > 7.0:
            zb = zr[a] + dz_t[a]
            Lp = np.r_[s[1] + s[3] * s[5], zb]
            Rp = np.r_[s[1] + s[3] * s[4], zb]
            beams.append((Lp, Rp, 0.5, 0.7))
    return beams


def suspension_members(P, arcs, tans, ms, info, ground_fn, stayed=False):
    """Towers, main cables and hangers of a suspension span; with stayed, pylons and fans of stays."""
    k, c, L = ms
    s_part = arcs[k]
    towers = [c - L / 2, c + L / 2]
    beams = []
    H = (STAYED_TOWER if stayed else SUSP_TOWER) * L
    leg = (_sz(L, 1 / 250, 2.5, 8.0), _sz(L, 1 / 180, 3.0, 10.0))
    tops = []
    for sc in towers:
        p, t, nrm, lo, hi, zlo, zhi = _station(P, tans, k, sc, arcs)
        gz = float(np.asarray(ground_fn(p[None]), np.float64)[0])
        top = zhi + H
        legs = []
        for off in (lo - leg[0] / 2, hi + leg[0] / 2):
            e = p + nrm * off
            beams.append((np.r_[e, gz - 2.0], np.r_[e, top], leg[0], leg[1]))
            legs.append(e)
        for zz in (top - 1.5, top - 0.3 * H, zlo - 3.0):     # portal beams (one under the decks)
            if zz > gz + 1.0:
                beams.append((np.r_[legs[0], zz], np.r_[legs[1], zz], leg[0] * 0.8, 2.5))
        tops.append((p, t, nrm, lo, hi, top))
    if not stayed:
        side = SUSP_SIDE * L
        s0, s1 = max(towers[0] - side, 0.0), min(towers[1] + side, s_part[-1])
        n = max(int(round((s1 - s0) / HANGER_M)), 4)
        st = _ribs(P, tans, arcs, k, s0, s1, n)
        for which in (0, 1):
            prev = None
            for s in st:
                sc, p, t, nrm, lo, hi, zlo, zhi = s
                e = p + nrm * ((hi + leg[0] / 2) if which else (lo - leg[0] / 2))
                if sc < towers[0]:                      # side span: straight down to the anchorage at the deck
                    f = (sc - s0) / max(towers[0] - s0, 1e-9)
                    zc = (zhi + 1.0) + (tops[0][5] - zhi - 1.0) * f ** 1.3
                elif sc > towers[1]:
                    f = (s1 - sc) / max(s1 - towers[1], 1e-9)
                    zc = (zhi + 1.0) + (tops[1][5] - zhi - 1.0) * f ** 1.3
                else:
                    x = (sc - c) / (L / 2)
                    low = zhi + SUSP_LOW_M
                    zc = low + (min(tops[0][5], tops[1][5]) - low) * x ** 2
                q = np.r_[e, zc]
                if prev is not None:
                    beams.append((prev, q, 0.9, 0.9))
                if zc - zhi > 1.5 and abs(sc - towers[0]) > 2 and abs(sc - towers[1]) > 2:
                    beams.append((q, np.r_[e, zhi - 0.6], 0.12, 0.12))
                prev = q
    else:
        for ti, sc_t in enumerate(towers):
            p, t, nrm, lo, hi, top = tops[ti]
            reach = L / 2
            for sc in np.arange(sc_t - reach + HANGER_M / 2, sc_t + reach, HANGER_M):
                if abs(sc - sc_t) < 6.0 or sc < 0 or sc > s_part[-1]:
                    continue
                s = _station(P, tans, k, float(sc), arcs)
                f = min(abs(sc - sc_t) / reach, 1.0)
                anchor_z = top - 0.3 * H * f
                for which, off in ((0, lo), (1, hi)):
                    e_t = p + nrm * off
                    e_d = s[0] + s[2] * (s[4] if which else s[3])
                    beams.append((np.r_[e_t, anchor_z], np.r_[e_d, s[6] - 0.2], 0.25, 0.25))
    return beams


def structure_members(parts, info, ground_fn, wet_fn=None, road_lines=()):
    """(beams, piers) of a chain's structure (chain_structure): a truss (truss_members), an arch, a suspension or a
    cable-stayed span (one main span: main_span); ([], []) for a plain deck or a movable span."""
    if info is None:
        return [], []
    if info["type"] in TRUSS_TYPES:
        return truss_members(parts, info, ground_fn, wet_fn, with_piers=True, road_lines=road_lines)
    if info["type"] not in ARCH_TYPES + (NBI_SUSPENSION, NBI_STAYED):
        return [], []
    P, arcs, tans = _prep(parts)
    ms = main_span(P, arcs, info, ground_fn, wet_fn)
    if ms is None:
        return [], []
    if info["type"] in ARCH_TYPES:
        return arch_members(P, arcs, tans, ms, info, ground_fn), []
    return suspension_members(P, arcs, tans, ms, info, ground_fn, stayed=info["type"] == NBI_STAYED), []
