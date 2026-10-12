"""Roads near the player (docs/ROADMAP.md v1.6 "Roads"; first cut 2026-10-11): Overture road segments
from the map-data cache (mapdata.py "roads"), drawn by the mod as DecalRoads on the physics ground, and
painted into the surface masks (surface.py) so the tyres feel asphalt or dirt.

Tiles: TILE x TILE m squares of the anchor's ENU frame, keyed (anchor, tx, ty). The Tracker of a
connection keeps the tiles within SEND_M of the player sent (built in a worker, written to
`roads_<a>_<tx>_<ty>.json` in the session folder, announced with a `roads_tile` message) and drops
those beyond DROP_M (`roads_drop`). An anchor change re-sends everything in the new frame; the mod
drops the old anchor's roads then.

A tile's file is a list of chunks, each one DecalRoad:
    {"m": material, "w": width m, "tl": texture length m, "p": render priority, "n": [[x, y, z], ...],
     "h": curb height m (sidewalks only: the mod raises a slab this far above the road near the player)}
with x, y, z in the anchor frame (z on the DEM, the DecalRoad snaps its nodes to the ground anyway)
and at most MAX_NODES nodes. Roads are clipped to the tile (OVERLAP m past its edge), simplified
(RDP, SIMPLIFY_M) and split. Deterministic: a pure function of the cells' road data, the frame and
the DEM.

Not yet: grading the ground under roads, markings, junction shapes, bridges and tunnels (segments
flagged as bridge / tunnel or off ground level are left out), the navgraph.
"""
import json
import logging
import math
import os
import threading
from collections import namedtuple

import numpy as np

import junctions
import mapcells

log = logging.getLogger("earthd.roads")

VERSION = 1
TILE = 256.0            # m, tile side in the anchor frame
SEND_M = 640.0          # tiles whose square comes this close to the player (Chebyshev) are sent
DROP_M = 960.0          # ... and dropped beyond this
OVERLAP = 2.0           # m a road reaches past its tile's edge (no gap at tile seams)
SIMPLIFY_M = 0.25       # RDP tolerance (Road Spline's layerMgr)
MAX_NODES = 100         # nodes per DecalRoad (Road Spline's chunk size)
MAX_SPACING = 12.0      # m between nodes at most (official maps: median 11-12 m)
BUILD_PER_POSE = 4      # tiles started per pose at most

# class -> (width m, paved by default). Classes not here (footway, path, steps, cycleway, ...) are
# left out for now.
STYLE = {
    "motorway": (11.0, True), "trunk": (9.0, True), "primary": (8.0, True), "secondary": (7.5, True),
    "tertiary": (6.5, True), "residential": (6.0, True), "living_street": (5.0, True),
    "unclassified": (5.5, True), "service": (4.0, True), "track": (3.5, False), "unknown": (5.0, True),
}
PAVED = {"paved", "asphalt", "concrete", "metal", "wood"}
COBBLE = {"paving_stones", "sett", "cobblestone", "unhewn_cobblestone", "bricks"}   # Overture: "paving_stones"
GRAVEL = {"gravel", "fine_gravel", "compacted", "pebblestone"}
# kinds with a hard, built surface: carriageway extras (parking, strips, sidewalks) apply
BUILT = {"paved", "cobble"}
# surface kind -> DecalRoad look (stock materials, loaded by the mod from the official levels)
LOOK = {
    "paved": {"m": "road_asphalt_2lane", "tl": 11.0, "p": 20},
    # Italy's cobblestone (a mesh material with groundType COBBLESTONE): a DecalRoad stretches its
    # texture across the whole width, so cobbled roads are drawn as parallel strips of about
    # "strip" m with a square texture repeat, which keeps the stones their size
    "cobble": {"m": "italy_cobblestone", "tl": 2.5, "p": 20, "strip": 2.5},
    "gravel": {"m": "m_dirt_road", "tl": 88.0, "p": 21},
    "dirt": {"m": "m_dirt_road", "tl": 88.0, "p": 21},
}
Spec = namedtuple("Spec", "width kind urban cls oneway walk", defaults=(False, 2.5))   # road_specs' result per feature
# (walk: the sidewalk width, measured from the buildings where known: roadwidth.py)
# (width = carriageway). One-way driving widths (Overture's divided roads are two one-way segments, each
# drawn before as a full two-way street: 2026-10-11 orthophoto of Broadway, 14-15 m drawn for 8-9 m)
ONEWAY_STYLE = {"primary": 6.5, "secondary": 6.5, "tertiary": 4.0, "residential": 3.5, "living_street": 3.5,
                "unclassified": 3.5, "service": 3.5, "unknown": 3.5}

# Sidewalks and markings (user 2026-10-11, after the first street-view comparison of Fells Point:
# real streets have concrete sidewalks behind a curb and a double yellow centre line). Stock West
# Coast USA materials. renderPriority: lower draws on top (WCUSA: lines 2-6, the asphalt strip 20).
SIDEWALK = {"m": "sidewalk_concrete", "tl": 6.0, "p": 16, "w": 2.5}      # beyond the carriageway, city streets
# Storm drain inlets: a metal grate in the gutter (Driver Training's m_metal_grid_drain), DRAIN_IN m
# before the real end of each sidewalk run (one per corner, where real inlets sit) and every
# DRAIN_EVERY m along long runs (each is a DecalRoad: kept to ~40 per city tile)
DRAIN = {"m": "m_metal_grid_drain", "tl": 2.0, "p": 12, "w": 0.45, "len": 1.0}
DRAIN_IN, DRAIN_EVERY = 2.5, 90.0
GUTTER = {"m": "gutter1", "tl": 8.0, "p": 14, "w": 0.5}                  # at the curb line
CENTRE = {"tl": 12.0, "p": 6, "w": 0.12, "gap": 0.12}                    # a double line: two lines this far from the centre
EDGE_LINE = {"m": "line_white", "tl": 6.4, "p": 2, "w": 0.15, "inset": 0.3}
MEASURE_SKIP = {"service"}      # classes not measured between buildings (lots, alleys: far walls)
MEASURE_MAX = 3.0               # a measured carriageway at most this times the class default
SIDEWALK_CLASSES = {"residential", "tertiary", "secondary", "primary", "unclassified", "living_street"}
CENTRE_CLASSES = {"primary", "secondary", "tertiary", "trunk"}
EDGE_LINE_CLASSES = {"primary", "secondary", "tertiary", "trunk", "motorway"}
CUT_MARGIN = 0.5                # m: a sidewalk / line stops this far outside a crossing road's carriageway
MIN_PIECE_M = 3.0               # shorter sidewalk / line pieces are dropped
NEAR_M = 40.0                   # roads this far outside the tile still cut its sidewalks / lines
# Carriageway = driving width + parking lanes on city streets or paved edge strips on rural roads
# (docs/research/2026-10-11-official-road-study.md §2: rural paved surface = AI width + 2-2.5 m;
# West Coast USA's plain streets 12.5 m for 8 m of driving: ~2.3 m of parking each side). A width
# rule, where Overture has one, is the carriageway already.
PARKING_M = 2.3
PARKING_CLASSES = {"residential", "tertiary", "secondary", "unclassified", "living_street"}
EDGE_STRIP_M = 1.0
URBAN_CLASS = 50                # ESA WorldCover "built-up"
URBAN_SHARE = 0.5               # a road is urban when this share of its vertices is built-up


def _rule_value(table, i):
    """The value of the rule of feature i covering the largest share of it, or None."""
    if table is None or len(table.feature) == 0:
        return None
    sel = np.nonzero(table.feature == i)[0]
    if len(sel) == 0:
        return None
    span = table.end[sel] - table.start[sel]
    return table.value[sel[int(np.argmax(span))]]


def level_ranges(feats, i):
    """The off-ground ranges of feature i as sorted (start, end, "bridge" | "tunnel") fractions of its
    length, from its flags (is_bridge / is_tunnel) and level (> 0 bridge, < 0 tunnel) rules. Overture
    puts most bridges on a range of a longer segment (2026-10-11: a street with a few metres of bridge
    was taken whole as a bridge and dropped). Pure."""
    out = []
    for name in ("flags", "level"):
        t = feats.rules.get(name)
        if t is None or len(t.feature) == 0:
            continue
        for j in np.nonzero(t.feature == i)[0]:
            v = t.value[j]
            if name == "flags":
                kind = "tunnel" if "is_tunnel" in str(v) else "bridge" if "is_bridge" in str(v) else None
            else:
                try:
                    lv = float(v)
                except (TypeError, ValueError):
                    lv = 0.0
                kind = "bridge" if lv > 0 else "tunnel" if lv < 0 else None
            if kind is not None and t.end[j] > t.start[j]:
                out.append((float(t.start[j]), float(t.end[j]), kind))
    out.sort()
    merged = []                                 # overlapping ranges merged (a tunnel wins over a bridge)
    for a, b, k in out:
        if merged and a <= merged[-1][1] + 1e-9:
            pa, pb, pk = merged[-1]
            merged[-1] = (pa, max(pb, b), "tunnel" if "tunnel" in (pk, k) else "bridge")
        else:
            merged.append((a, b, k))
    return merged


def sub_polyline(ring, a, b):
    """The part of a lon/lat polyline (k, >=2) between fractions a < b of its length (metres along it,
    lon scaled by cos lat). Pure."""
    ring = np.asarray(ring, np.float64)
    c = math.cos(math.radians(float(np.mean(ring[:, 1]))))
    seg = np.hypot(np.diff(ring[:, 0]) * c, np.diff(ring[:, 1]))
    s = np.r_[0.0, np.cumsum(seg)]
    if s[-1] <= 0:
        return ring[:1]
    sa, sb = a * s[-1], b * s[-1]
    inner = ring[(s > sa) & (s < sb)]
    pa = np.array([np.interp(sa, s, ring[:, d]) for d in range(ring.shape[1])])
    pb = np.array([np.interp(sb, s, ring[:, d]) for d in range(ring.shape[1])])
    return np.vstack([pa, inner, pb])


MIN_PART = 1e-3     # parts shorter than this share of a segment are dropped


def _parts_by_level(feats, i, want):
    rngs = level_ranges(feats, i)
    if want == "ground":
        pieces, at = [], 0.0
        for a, b, _ in rngs:
            if a - at > MIN_PART:
                pieces.append((at, a))
            at = max(at, b)
        if 1.0 - at > MIN_PART:
            pieces.append((at, 1.0))
    else:
        pieces = [(a, b) for a, b, k in rngs if k == want and b - a > MIN_PART]
    out = []
    for part in feats.parts(i):
        for ring in part:
            if len(ring) < 2:
                continue
            for a, b in pieces:
                r = ring if (a, b) == (0.0, 1.0) else sub_polyline(ring, a, b)
                if len(r) >= 2:
                    out.append(r)
    return out


def bridge_levels(feats, i):
    """The level of each of bridge_parts(feats, i) (same order): the highest level rule overlapping its
    range, 1 for a bridge flagged without one. Overture's levels order stacked roads. Pure."""
    t = feats.rules.get("level")
    lv = []
    for a, b, k in level_ranges(feats, i):
        if k != "bridge" or b - a <= MIN_PART:
            continue
        best = 1.0
        if t is not None and len(t.feature):
            for j in np.nonzero(t.feature == i)[0]:
                if t.end[j] > a and t.start[j] < b:
                    try:
                        best = max(best, float(t.value[j]))
                    except (TypeError, ValueError):
                        pass
        lv.append(best)
    rings = sum(1 for part in feats.parts(i) for ring in part if len(ring) >= 2)
    return lv * rings


def ground_parts(feats, i):
    """The on-ground pieces of feature i (lon/lat polylines): its rings minus the bridge / tunnel ranges."""
    return _parts_by_level(feats, i, "ground")


def bridge_parts(feats, i):
    """The bridge pieces of feature i (lon/lat polylines)."""
    return _parts_by_level(feats, i, "bridge")


def surface_kind(cls, surface):
    """'paved', 'cobble', 'gravel' or 'dirt' from the class and the surface rule value (None if unknown)."""
    if surface is not None:
        s = str(surface).lower()
        if s in PAVED:
            return "paved"
        if s in COBBLE:
            return "cobble"
        if s in GRAVEL:
            return "gravel"
        return "dirt"
    return "paved" if STYLE.get(cls, STYLE["unknown"])[1] else "dirt"


def carriageway(cls, kind, urban, rule_width=None, oneway=False):
    """Paved / dirt width (m) of a road: the width rule if any, else the class's driving width plus
    parking lanes (urban paved streets; one side on a one-way street) or edge strips (rural paved
    roads). One-way: ONEWAY_STYLE's driving width (motorways and trunks are per carriageway already).
    Pure."""
    if rule_width is not None:
        return rule_width
    w = STYLE.get(cls, STYLE["unknown"])[0]
    if oneway and cls in ONEWAY_STYLE:
        w = ONEWAY_STYLE[cls]
    if kind not in BUILT:
        return w
    if urban and cls in PARKING_CLASSES:
        return w + (1 if oneway else 2) * PARKING_M
    if not urban and cls != "service":
        return w + 2 * EDGE_STRIP_M
    return w


def road_specs(feats, urban_fn=None, widths=None):
    """Per feature: None (left out) or (width m, surface kind). urban_fn(lat, lon) -> bool array (built-up
    land cover), None = rural everywhere; called once for every candidate's points (its cost is per
    call: 2026-10-11, one call per road made 4.4 of a physics block's 6 s). Pure given urban_fn."""
    out = []
    rules = feats.rules
    ask = []                                    # (index in out, cls, kind, rule_w, coordinate range)
    for i in range(len(feats)):
        cls = str(feats.attrs["class"][i]) if "class" in feats.attrs else "unknown"
        if str(feats.attrs.get("subtype", ["road"] * len(feats))[i]) != "road" or cls not in STYLE:
            out.append(None)
            continue
        rngs = level_ranges(feats, i)
        if rngs and sum(b - a for a, b, _ in rngs) > 1.0 - MIN_PART:     # entirely off the ground
            out.append(None)
            continue
        width = _rule_value(rules.get("width"), i)
        rule_w = float(width) if width is not None and 1.0 <= float(width) <= 40.0 else None
        kind = surface_kind(cls, _rule_value(rules.get("surface"), i))
        ow = _rule_value(rules.get("oneway"), i) is not None
        if urban_fn is not None and rule_w is None and kind in BUILT:
            a = feats.coord_off[feats.ring_off[feats.part_off[i]]]
            b = feats.coord_off[feats.ring_off[feats.part_off[i + 1]]]
            if b > a:
                ask.append((len(out), cls, kind, a, b, ow))
                out.append(None)
                continue
        out.append(Spec(carriageway(cls, kind, False, rule_w, ow), kind, False, cls, ow))
    if ask:
        ll = np.concatenate([feats.lonlat[a:b] for _, _, _, a, b, _ in ask])
        u = np.asarray(urban_fn(ll[:, 1], ll[:, 0]), bool)
        k = 0
        for j, cls, kind, a, b, ow in ask:
            urban = float(np.mean(u[k:k + (b - a)])) >= URBAN_SHARE
            k += b - a
            out[j] = Spec(carriageway(cls, kind, urban, None, ow), kind, urban, cls, ow)
    if widths is not None:                      # measured between the buildings (roadwidth.py)
        for j, sp in enumerate(out):
            if sp is None or sp.kind not in BUILT or sp.cls not in SIDEWALK_CLASSES or sp.cls in MEASURE_SKIP:
                continue
            if _rule_value(rules.get("width"), j) is not None:
                continue
            a = feats.coord_off[feats.ring_off[feats.part_off[j]]]
            b = feats.coord_off[feats.ring_off[feats.part_off[j + 1]]]
            m = widths(str(feats.ids[j]), feats.lonlat[a:b]) if b > a else None
            dflt = carriageway(sp.cls, sp.kind, True, None, sp.oneway)
            if m is not None and 0.6 * dflt <= m[0] <= MEASURE_MAX * dflt:
                out[j] = sp._replace(width=m[0], walk=m[1])
    return out


def bridge_specs(feats):
    """Per feature: the Spec of a road bridge (flag is_bridge, or level above ground; not a tunnel), else
    None. No parking widths on bridges (urban False). Pure."""
    out = []
    rules = feats.rules
    for i in range(len(feats)):
        cls = str(feats.attrs["class"][i]) if "class" in feats.attrs else "unknown"
        if str(feats.attrs.get("subtype", ["road"] * len(feats))[i]) != "road" or cls not in STYLE:
            out.append(None)
            continue
        if not any(k == "bridge" for _, _, k in level_ranges(feats, i)):
            out.append(None)
            continue
        width = _rule_value(rules.get("width"), i)
        rule_w = float(width) if width is not None and 1.0 <= float(width) <= 40.0 else None
        kind = surface_kind(cls, _rule_value(rules.get("surface"), i))
        ow = _rule_value(rules.get("oneway"), i) is not None
        out.append(Spec(carriageway(cls, kind, False, rule_w, ow), kind, False, cls, ow))
    return out


def _chain_graph(lines):
    """Nodes (end points within 0.5 m joined) and edges (start node, end node, arc lengths) of parts."""
    keys, nodes = {}, []

    def node(p):
        k = (round(float(p[0]) * 2), round(float(p[1]) * 2))
        if k not in keys:
            keys[k] = len(nodes)
            nodes.append(np.asarray(p[:2], np.float64))
        return keys[k]
    edges = []
    for ln in lines:
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(ln[:, :2], axis=0).T))]
        edges.append((node(ln[0]), node(ln[-1]), s))
    adj = [[] for _ in nodes]
    for a, b, s in edges:
        adj[a].append((b, s[-1]))
        adj[b].append((a, s[-1]))
    return nodes, edges, adj


def _dijkstra(adj, seeds):
    """Shortest distances along the chain from seeds {node: start distance}."""
    import heapq
    d = np.full(len(adj), np.inf)
    q = []
    for n, d0 in seeds.items():
        if d0 < d[n]:
            d[n] = d0
            q.append((d0, n))
    heapq.heapify(q)
    while q:
        dd, u = heapq.heappop(q)
        if dd > d[u]:
            continue
        for v, w in adj[u]:
            if dd + w < d[v]:
                d[v] = dd + w
                heapq.heappush(q, (dd + w, v))
    return d


def _merge_overlaps(lines, zs, widths, levels, rounds=3):
    """Heights of decks lying over each other (a ramp leaving the mainline, parallel carriageways of
    one level) pulled to their mean where their surfaces overlap, so a split is one surface until the
    decks part (2026-10-11 the user: exits and splits not clean). Parts are related when they share an
    end node or a level and run within ~30 degrees of each other. Abutment ends stay. Pure."""
    nodes, edges, _ = _chain_graph(lines)
    deg = np.zeros(len(nodes), int)
    for a, b, _ in edges:
        deg[a] += 1
        deg[b] += 1
    n = len(lines)
    pairs = []
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            share = {edges[i][0], edges[i][1]} & {edges[j][0], edges[j][1]}
            if not share and (levels is None or levels[i] != levels[j]):
                continue
            A, B = lines[i][:, :2], lines[j][:, :2]
            if (A.max(0) < B.min(0) - 30).any() or (B.max(0) < A.min(0) - 30).any():
                continue
            pairs.append((i, j))
    if not pairs:
        return zs
    zs = [np.array(z, np.float64) for z in zs]
    for _ in range(rounds):
        new = [z.copy() for z in zs]
        acc = [np.zeros(len(z)) for z in zs]
        cnt = [np.zeros(len(z)) for z in zs]
        for i, j in pairs:
            A, B = lines[i][:, :2], lines[j][:, :2]
            P, Q = B[:-1], B[1:]
            ab = Q - P
            L2 = np.maximum((ab ** 2).sum(1), 1e-12)
            t = np.clip(((A[:, None, :] - P[None]) * ab[None]).sum(2) / L2[None], 0, 1)
            near = P[None] + t[..., None] * ab[None]
            d = np.hypot(*(A[:, None, :] - near).transpose(2, 0, 1))
            m = np.argmin(d, axis=1)
            dmin = d[np.arange(len(A)), m]
            tj = t[np.arange(len(A)), m]
            zj = zs[j][m] + tj * (zs[j][np.minimum(m + 1, len(zs[j]) - 1)] - zs[j][m])
            ta = np.gradient(A, axis=0)
            tb = ab[m]
            cos = np.abs((ta * tb).sum(1)) / (np.hypot(*ta.T) * np.hypot(*tb.T) + 1e-12)
            # soft: full weight 2 m into the overlap and when parallel, none at its edge (a hard mask
            # made 4 m steps mid-viaduct where a neighbour's footprint started)
            depth = np.clip(((widths[i] + widths[j]) / 2 - dmin) / 2.0, 0.0, 1.0)
            w = depth * np.clip((cos - 0.85) / 0.1, 0.0, 1.0) * np.isfinite(zj)
            acc[i] += w * np.nan_to_num(zj)
            cnt[i] += w
        for i in range(n):
            m = cnt[i] > 0
            new[i][m] = (zs[i][m] + acc[i][m]) / (1 + cnt[i][m])
            a, b, _ = edges[i]
            if deg[a] == 1:
                new[i][0] = zs[i][0]
            if deg[b] == 1:
                new[i][-1] = zs[i][-1]
        zs = new
    out = []
    for i, z in enumerate(zs):                  # re-smoothed, ends kept
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(lines[i][:, :2], axis=0).T))]
        out.append(_smooth_pinned(z, s[-1] / max(len(s) - 1, 1), DECK_SIGMA) if len(z) > 3 else z)
    return out


def deck_profile(lines, ground_fn, crossings=(), clear=None, grade=None, lift=None, levels=None, widths=None):
    """deck_profile_once, then repeated with the deck-over-deck requirements: where two parts of
    different levels cross, the higher one clears the lower one's deck by `clear` (2026-10-11: crossing
    decks clipped into each other). Pure given ground_fn."""
    zs = deck_profile_once(lines, ground_fn, crossings, clear, grade, lift)
    if levels is None or len(lines) < 2:
        return zs
    over = _deck_crossings(lines)
    if not over:
        return _merge_overlaps(lines, zs, widths, levels) if widths is not None else zs
    arcs = [np.r_[0.0, np.cumsum(np.hypot(*np.diff(ln[:, :2], axis=0).T))] for ln in lines]
    for _ in range(4):
        extra = []
        for i, j, si, sj in over:
            if levels[i] == levels[j]:
                continue
            hi, lo, sh, sl = (i, j, si, sj) if levels[i] > levels[j] else (j, i, sj, si)
            extra.append((hi, sh, float(np.interp(sl, arcs[lo], zs[lo]))))
        if not extra:
            break
        new = deck_profile_once(lines, ground_fn, list(crossings) + extra, clear, grade, lift)
        if all(np.allclose(a, b, atol=0.02, equal_nan=True) for a, b in zip(new, zs)):
            zs = new
            break
        zs = new
    return _merge_overlaps(lines, zs, widths, levels) if widths is not None else zs


def deck_profile_once(lines, ground_fn, crossings=(), clear=None, grade=None, lift=None):
    """Deck heights (one array per part) of a bridge chain: deck_heights between the abutments, at
    least `lift` over the ground under it, and at least `clear` over each crossing road below
    ((part index, arc m, road height)), the requirement falling at `grade` along the chain; then
    smoothed per part with its end heights kept. Pure given ground_fn(xy (m, 2)) -> heights."""
    clear = DECK_ROAD_CLEAR if clear is None else clear
    grade = DECK_GRADE if grade is None else grade
    lift = DECK_CLEAR if lift is None else lift
    zs = deck_heights(lines, ground_fn)
    if not zs:
        return zs
    nodes, edges, adj = _chain_graph(lines)
    allxy = np.concatenate([ln[:, :2] for ln in lines])
    gz = np.asarray(ground_fn(allxy), np.float64)
    req = [np.full(len(ln), -np.inf) for ln in lines]
    for pi, sc, zc in crossings:
        a, b, s = edges[pi]
        d = _dijkstra(adj, {a: sc, b: s[-1] - sc})
        for k, (ea, eb, es) in enumerate(edges):
            dk = np.minimum(d[ea] + es, d[eb] + (es[-1] - es))
            if k == pi:
                dk = np.minimum(dk, np.abs(es - sc))
            req[k] = np.maximum(req[k], zc + clear - grade * dk)
    out, k0 = [], 0
    deg = np.zeros(len(nodes), int)
    for a, b, _ in edges:
        deg[a] += 1
        deg[b] += 1
    # no steeper than DECK_MAX_GRADE from an abutment: a short bridge over a road cannot reach full
    # headroom (2026-10-11: such decks became steep wedges, "walls" in the user's screenshot)
    cap = [np.full(len(ln), np.inf) for ln in lines]
    for k, (a, b, s) in enumerate(edges):
        for node, zend in ((a, zs[k][0]), (b, zs[k][-1])):
            if deg[node] != 1 or not np.isfinite(zend):
                continue
            d = _dijkstra(adj, {node: 0.0})
            for k2, (ea, eb, es) in enumerate(edges):
                dk = np.minimum(d[ea] + es, d[eb] + (es[-1] - es))
                cap[k2] = np.minimum(cap[k2], zend + DECK_MAX_GRADE * dk)
    for k, (z, (a, b, s)) in enumerate(zip(zs, edges)):
        g = gz[k0:k0 + len(z)] + lift
        k0 += len(z)
        if not np.isfinite(z).all():
            out.append(z)
            continue
        zz = np.maximum(np.minimum(np.maximum(z, req[k]), cap[k]), g)
        if deg[a] == 1:                         # an abutment keeps the ground's height
            zz[0] = z[0]
        if deg[b] == 1:
            zz[-1] = z[-1]
        if len(zz) > 3:
            step = s[-1] / (len(s) - 1)
            sm = _smooth_pinned(zz, step, DECK_SIGMA)
            zz = np.maximum(sm, g)
            zz[0], zz[-1] = sm[0], sm[-1]
        out.append(zz)
    return out


DECK_SIGMA = 12.0       # m, deck profile smoothing


def _dedupe(xy, tol=0.5):
    """A polyline without points closer than tol m to the previous one (ends kept). Pure."""
    if len(xy) < 3:
        return xy
    keep = [0]
    for k in range(1, len(xy) - 1):
        if np.hypot(*(xy[k] - xy[keep[-1]])) >= tol:
            keep.append(k)
    if np.hypot(*(xy[-1] - xy[keep[-1]])) < tol and len(keep) > 1:
        keep[-1] = len(xy) - 1
    else:
        keep.append(len(xy) - 1)
    return xy[keep]


def _smooth_pinned(z, step, sigma):
    """Gaussian smoothing that keeps both end values and the trend at the ends (the profile is
    point-reflected about each end: plain mirroring pulled a hump's height into its pinned ends and
    made 15 % steps there). Pure."""
    z = np.asarray(z, np.float64)
    r = int(math.ceil(3 * sigma / step))
    r = min(r, len(z) - 1)
    if r < 1:
        return z.copy()
    k = np.exp(-0.5 * (np.arange(-r, r + 1) * step / sigma) ** 2)
    k /= k.sum()
    pad = np.r_[2 * z[0] - z[r:0:-1], z, 2 * z[-1] - z[-2:-r - 2:-1]]
    out = np.convolve(pad, k, mode="valid")
    out[0], out[-1] = z[0], z[-1]
    return out


def deck_heights(lines, end_z):
    """Deck heights of chained bridge parts. lines: polylines (k, 2) in metres (one per bridge part);
    parts sharing an end point (within 0.5 m) are joined. end_z(xy (m, 2)) -> heights of the ground at
    the abutments (chain ends: points used by one part only). A point's height is the abutments'
    heights weighted by 1 / their distance along the chain (between two abutments: a straight line).
    Returns one height array per line. Pure given end_z."""
    if not lines:
        return []
    keys, nodes = {}, []

    def node(p):
        k = (round(float(p[0]) * 2), round(float(p[1]) * 2))      # 0.5 m
        if k not in keys:
            keys[k] = len(nodes)
            nodes.append(np.asarray(p[:2], np.float64))
        return keys[k]
    edges = []
    for ln in lines:
        seg = np.hypot(*np.diff(ln[:, :2], axis=0).T)
        s = np.r_[0.0, np.cumsum(seg)]
        edges.append((node(ln[0]), node(ln[-1]), s))
    deg = np.zeros(len(nodes), int)
    adj = [[] for _ in nodes]
    for a, b, s in edges:
        deg[a] += 1
        deg[b] += 1
        adj[a].append((b, s[-1]))
        adj[b].append((a, s[-1]))
    ends = [i for i in range(len(nodes)) if deg[i] == 1]
    ez = np.asarray(end_z(np.array([nodes[i] for i in ends]).reshape(-1, 2)), np.float64) if ends else np.zeros(0)
    import heapq
    dist = []                                   # per abutment: distance along the chain to every node
    for e in ends:
        d = np.full(len(nodes), np.inf)
        d[e] = 0.0
        q = [(0.0, e)]
        while q:
            dd, u = heapq.heappop(q)
            if dd > d[u]:
                continue
            for v, w in adj[u]:
                if dd + w < d[v]:
                    d[v] = dd + w
                    heapq.heappush(q, (dd + w, v))
        dist.append(d)
    out = []
    for a, b, s in edges:
        if not ends:                            # a closed loop: no abutment known
            out.append(np.full(len(s), np.nan))
            continue
        num = np.zeros(len(s))
        den = np.zeros(len(s))
        for k, d in enumerate(dist):
            dk = np.minimum(d[a] + s, d[b] + (s[-1] - s))
            ok = np.isfinite(dk)
            w = np.where(ok, 1.0 / np.maximum(dk, 1e-6), 0.0)
            num += w * np.where(ok, ez[k], 0.0)
            den += w
        out.append(np.where(den > 0, num / np.maximum(den, 1e-12), np.nan))
    return out


def centre_colour(lon):
    """Centre line colour: yellow in the Americas, white elsewhere (the country rule comes later)."""
    return "line_yellow" if -170.0 <= float(lon) <= -30.0 else "line_white"


def offset_polyline(pts, d):
    """The polyline moved d m to its left (xy; z kept), with mitred joints (miter at most 2x). Pure."""
    pts = np.asarray(pts, np.float64)
    if len(pts) < 2:
        return pts.copy()
    t = np.diff(pts[:, :2], axis=0)
    L = np.hypot(t[:, 0], t[:, 1])
    L[L < 1e-9] = 1e-9
    t /= L[:, None]
    nrm = np.stack([-t[:, 1], t[:, 0]], axis=1)          # left normals per segment
    vn = np.empty((len(pts), 2))
    vn[0], vn[-1] = nrm[0], nrm[-1]
    if len(pts) > 2:
        m = nrm[:-1] + nrm[1:]
        ml = np.hypot(m[:, 0], m[:, 1])
        ml[ml < 1e-9] = 1e-9
        m /= ml[:, None]
        cosh = np.clip(np.sum(m * nrm[1:], axis=1), 0.5, 1.0)  # miter scale 1/cos(half angle), at most 2
        vn[1:-1] = m / cosh[:, None]
    out = pts.copy()
    out[:, :2] += vn * d
    return out


def _seg_dist(p, a, b):
    """Distance from points p (N, 2) to segments a-b (M, 2): (N, M)."""
    ab = b - a
    L2 = np.maximum(np.sum(ab * ab, axis=1), 1e-12)
    ap = p[:, None, :] - a[None, :, :]
    t = np.clip(np.sum(ap * ab[None], axis=2) / L2[None], 0.0, 1.0)
    q = a[None] + t[..., None] * ab[None]
    return np.hypot(p[:, None, 0] - q[..., 0], p[:, None, 1] - q[..., 1])


def cut_outside(pts, blockers, spacing=2.0):
    """The pieces of a polyline (k, 3) outside every blocker (centreline xy (m, 2), half width): densified
    to `spacing`, points inside a blocker removed, runs of at least MIN_PIECE_M kept. Pure."""
    if not blockers:
        return [pts]
    d = densify(np.asarray(pts, np.float64), spacing)
    inside = np.zeros(len(d), bool)
    for line, hw in blockers:
        if len(line) < 2:
            continue
        lo, hi = line.min(axis=0) - hw, line.max(axis=0) + hw
        near = (d[:, 0] >= lo[0]) & (d[:, 0] <= hi[0]) & (d[:, 1] >= lo[1]) & (d[:, 1] <= hi[1]) & ~inside
        if not near.any():
            continue
        idx = np.nonzero(near)[0]
        dist = _seg_dist(d[idx, :2], line[:-1], line[1:]).min(axis=1)
        inside[idx[dist < hw]] = True
    out, start = [], None
    for k in range(len(d) + 1):
        if k < len(d) and not inside[k]:
            if start is None:
                start = k
        elif start is not None:
            run = d[start:k]
            if len(run) >= 2 and float(np.sum(np.hypot(*np.diff(run[:, :2], axis=0).T))) >= MIN_PIECE_M:
                out.append(run)
            start = None
    return out


def has_sidewalks(spec):
    return spec.kind in BUILT and spec.urban and spec.cls in SIDEWALK_CLASSES


def sidewalk_stop(spec, fid, other, ofid):
    """How far from the centreline of a crossing road (other) the sidewalks of road spec stop. The corner
    square is one sidewalk's: when both roads have sidewalks the wider road's (then the smaller id) runs on
    to the other's curb and the other's stops against its outer edge; with none on the other road, they
    run on to its curb. No gap and no overlap (two slabs at one height would z-fight). Pure."""
    if has_sidewalks(other) and (-other.width, ofid) < (-spec.width, fid):
        return other.width / 2 + other.walk
    return other.width / 2


def extras(spec, pts, lon):
    """The sidewalk / gutter / marking polylines of one road: [(look dict with m / tl / p, width, pts)].
    pts: its centreline (k, 3) in the anchor frame; lon: its longitude (centre line colour). Pure."""
    out = []
    w, kind, urban, cls = spec[:4]
    if kind not in BUILT:
        return out
    if has_sidewalks(spec):
        for side in (1, -1):
            out.append(({**SIDEWALK, "side": side}, spec.walk,
                        offset_polyline(pts, side * (w / 2 + spec.walk / 2))))
            out.append(({**GUTTER, "side": side}, GUTTER["w"], offset_polyline(pts, side * (w / 2 - GUTTER["w"] / 2))))
    if kind == "paved" and cls in CENTRE_CLASSES and w >= 6.0 and not spec.oneway:   # no paint on cobbles
        look = {"m": centre_colour(lon), "tl": CENTRE["tl"], "p": CENTRE["p"]}
        for side in (1, -1):
            out.append((look, CENTRE["w"], offset_polyline(pts, side * CENTRE["gap"])))
    if kind == "paved" and not urban and cls in EDGE_LINE_CLASSES:
        for side in (1, -1):
            out.append((EDGE_LINE, EDGE_LINE["w"], offset_polyline(pts, side * (w / 2 - EDGE_LINE["inset"]))))
    return out


def clip_polyline(pts, x0, y0, x1, y1):
    """The pieces of a polyline (k, D >= 2, xy first) inside the box, as a list of arrays (Liang-Barsky
    per segment, consecutive pieces joined). Pure."""
    pieces, cur = [], []
    for a, b in zip(pts[:-1], pts[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        t0, t1 = 0.0, 1.0
        ok = True
        for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
            if p == 0:
                if q < 0:
                    ok = False
                    break
            else:
                t = q / p
                if p < 0:
                    t0 = max(t0, t)
                else:
                    t1 = min(t1, t)
        if not ok or t0 > t1:
            if len(cur) >= 2:
                pieces.append(np.array(cur))
            cur = []
            continue
        pa = a + (b - a) * t0
        pb = a + (b - a) * t1
        if not cur or np.abs(np.asarray(cur[-1]) - pa).max() > 1e-6:
            if len(cur) >= 2:
                pieces.append(np.array(cur))
            cur = [pa]
        cur.append(pb)
        if t1 < 1.0:
            pieces.append(np.array(cur))
            cur = []
    if len(cur) >= 2:
        pieces.append(np.array(cur))
    return pieces


def simplify(pts, tol=SIMPLIFY_M):
    """Ramer-Douglas-Peucker on the xy of a polyline (k, D). Pure."""
    if len(pts) < 3:
        return pts
    keep = np.zeros(len(pts), bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        a, b = pts[i, :2], pts[j, :2]
        ab = b - a
        L = math.hypot(ab[0], ab[1])
        seg = pts[i + 1:j, :2]
        if len(seg) == 0:
            continue
        if L < 1e-9:
            d = np.hypot(seg[:, 0] - a[0], seg[:, 1] - a[1])
        else:
            d = np.abs(ab[0] * (seg[:, 1] - a[1]) - ab[1] * (seg[:, 0] - a[0])) / L
        k = int(np.argmax(d))
        if d[k] > tol:
            m = i + 1 + k
            keep[m] = True
            stack.append((i, m))
            stack.append((m, j))
    return pts[keep]


def densify(pts, spacing=MAX_SPACING):
    """Nodes added so no two are more than `spacing` m apart (xy). Pure."""
    out = [pts[0]]
    for a, b in zip(pts[:-1], pts[1:]):
        n = int(math.ceil(math.hypot(b[0] - a[0], b[1] - a[1]) / spacing))
        for k in range(1, n):
            out.append(a + (b - a) * (k / n))
        out.append(b)
    return np.array(out)


def split(pts, n=MAX_NODES):
    """Chunks of at most n nodes sharing their end nodes. Pure."""
    if len(pts) <= n:
        return [pts]
    out, i = [], 0
    while i < len(pts) - 1:
        out.append(pts[i:i + n])
        i += n - 1
    return out


def tile_bbox(tx, ty):
    return tx * TILE, ty * TILE, (tx + 1) * TILE, (ty + 1) * TILE


def tiles_near(x, y, radius):
    """Tile keys whose square comes within `radius` of (x, y) (Chebyshev)."""
    t0x, t1x = int(math.floor((x - radius) / TILE)), int(math.floor((x + radius) / TILE))
    t0y, t1y = int(math.floor((y - radius) / TILE)), int(math.floor((y + radius) / TILE))
    return [(tx, ty) for tx in range(t0x, t1x + 1) for ty in range(t0y, t1y + 1)]


def tile_distance(tx, ty, x, y):
    x0, y0, x1, y1 = tile_bbox(tx, ty)
    return max(x0 - x, x - x1, y0 - y, y - y1, 0.0)




def cells_of_tile(frame, tx, ty, margin=OVERLAP):
    x0, y0, x1, y1 = tile_bbox(tx, ty)
    lat, lon, _ = frame.enu_to_geodetic(np.array([x0 - margin, x1 + margin, x0 - margin, x1 + margin]),
                                        np.array([y0 - margin, y0 - margin, y1 + margin, y1 + margin]), 0.0)
    return mapcells.cells_for_bbox(float(np.min(lat)), float(np.min(lon)), float(np.max(lat)), float(np.max(lon)))


def _chunk(look, w, c):
    return {"m": look["m"], "w": round(float(w), 2), "tl": look["tl"], "p": look["p"],
            "n": [[round(float(v), 3) for v in p] for p in c]}


DECK = {"m": "road_asphalt_2lane", "tl": 11.0, "p": 20}      # bridge decks (roadmesh.deck)
DECK_STEP = 4.0         # m between deck profile points
DECK_CLEAR = 0.05       # m a deck stays above the ground under it (no z-fighting where it rests on it)
DECK_ROAD_CLEAR = 6.5   # m a deck's top stays over a road crossing under it (5 m headroom + the slab)
DECK_GRADE = 0.05       # that requirement falls 5 % along the deck from the crossing
DECK_MAX_GRADE = 0.06   # a deck climbs no steeper than this from an abutment
CHAIN_CELLS = 12        # map cells read at most to close one bridge chain


def _crossings(lines, ground_lines):
    """(part index, arc m, point xy) where a ground road (polylines in metres) crosses under a deck
    part. Pure."""
    out = []
    for pi, ln in enumerate(lines):
        lo, hi = ln.min(axis=0), ln.max(axis=0)
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(ln, axis=0).T))]
        P, Q = ln[:-1], ln[1:]
        for gl in ground_lines:
            if gl[:, 0].max() < lo[0] or gl[:, 0].min() > hi[0] or gl[:, 1].max() < lo[1] or gl[:, 1].min() > hi[1]:
                continue
            for a, b in zip(gl[:-1], gl[1:]):
                r, d = Q - P, b - a
                den = r[:, 0] * d[1] - r[:, 1] * d[0]
                ok = np.abs(den) > 1e-12
                ap = a - P
                t = np.where(ok, (ap[:, 0] * d[1] - ap[:, 1] * d[0]) / np.where(ok, den, 1), -1)
                u = np.where(ok, (ap[:, 0] * r[:, 1] - ap[:, 1] * r[:, 0]) / np.where(ok, den, 1), -1)
                for j in np.nonzero(ok & (t >= 0) & (t <= 1) & (u >= 0) & (u <= 1))[0]:
                    out.append((pi, s[j] + t[j] * (s[j + 1] - s[j]), P[j] + t[j] * r[j]))
    return out


def _deck_crossings(lines):
    """(i, j, arc on i, arc on j) where deck parts i < j cross (not at their end points). Pure."""
    out = []
    boxes = [(ln.min(axis=0), ln.max(axis=0)) for ln in lines]
    arcs = [np.r_[0.0, np.cumsum(np.hypot(*np.diff(ln, axis=0).T))] for ln in lines]
    for i in range(len(lines)):
        for j in range(i + 1, len(lines)):
            (alo, ahi), (blo, bhi) = boxes[i], boxes[j]
            if ahi[0] < blo[0] or bhi[0] < alo[0] or ahi[1] < blo[1] or bhi[1] < alo[1]:
                continue
            A, B = lines[i], lines[j]
            for q in range(len(B) - 1):
                a, d = B[q], B[q + 1] - B[q]
                P, r = A[:-1], A[1:] - A[:-1]
                den = r[:, 0] * d[1] - r[:, 1] * d[0]
                ok = np.abs(den) > 1e-12
                ap = a - P
                t = np.where(ok, (ap[:, 0] * d[1] - ap[:, 1] * d[0]) / np.where(ok, den, 1), -1)
                u = np.where(ok, (ap[:, 0] * r[:, 1] - ap[:, 1] * r[:, 0]) / np.where(ok, den, 1), -1)
                for k in np.nonzero(ok & (t > 1e-6) & (t < 1 - 1e-6) & (u > 1e-6) & (u < 1 - 1e-6))[0]:
                    si = arcs[i][k] + t[k] * (arcs[i][k + 1] - arcs[i][k])
                    sj = arcs[j][q] + u[k] * (arcs[j][q + 1] - arcs[j][q])
                    if min(si, arcs[i][-1] - si, sj, arcs[j][-1] - sj) > 1.0:
                        out.append((i, j, si, sj))
    return out


class BridgeProfiles:
    """Deck heights per bridge part for one anchor frame, each chain resolved once from all of it, so
    every tile gets the same deck (2026-10-11: computed per tile from the tile's cells, a chain's
    two halves disagreed at a tile seam). cell_fn(key) -> the roads Features of a map cell; elev:
    the physics elevation. Worker threads."""

    def __init__(self, frame, cell_fn, elev):
        self.frame, self.cell_fn, self.elev = frame, cell_fn, elev
        self.done = {}                          # (fid, j) -> (spec, xy (k, 2), z (k,), lon)
        self.lock = threading.Lock()

    def _ground(self, xy):
        la, lo, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        return self.frame.geodetic_to_enu(la, lo, np.asarray(self.elev(la, lo), np.float64))[2]

    def _load(self, key, pool, ground):
        feats = self.cell_fn(key)
        if feats is None or len(feats) == 0:
            return
        bs = bridge_specs(feats)
        rs = road_specs(feats)
        for i in range(len(feats)):
            fid = str(feats.ids[i])
            if bs[i] is not None:
                lvls = bridge_levels(feats, i)
                for j, ring in enumerate(bridge_parts(feats, i)):
                    if (fid, j) not in pool:
                        e, n, _ = self.frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
                        pool[(fid, j)] = (bs[i], densify(_dedupe(np.stack([e, n], axis=1)), DECK_STEP),
                                          float(np.mean(ring[:, 0])), lvls[j] if j < len(lvls) else 1.0)
            if rs[i] is not None and (fid, "g") not in ground:
                ground[(fid, "g")] = []
                for ring in ground_parts(feats, i):
                    e, n, _ = self.frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
                    ground[(fid, "g")].append(np.stack([e, n], axis=1))

    def _resolve(self, start, pool, ground, loaded):
        """The chain of part `start` (keys), loading the cell at each loose end until closed."""
        def nk(p):
            return (round(float(p[0]) * 2), round(float(p[1]) * 2))
        while True:
            by_node = {}
            for k, (_, xy, _, _) in pool.items():
                by_node.setdefault(nk(xy[0]), []).append(k)
                by_node.setdefault(nk(xy[-1]), []).append(k)
            keys = sorted(pool)
            over = _deck_crossings([pool[k][1] for k in keys])
            crossing = {}
            for a, b, _, _ in over:
                crossing.setdefault(keys[a], set()).add(keys[b])
                crossing.setdefault(keys[b], set()).add(keys[a])
            comp, stack = {start}, [start]
            while stack:                        # joined at nodes, or crossing (solved together)
                k = stack.pop()
                xy = pool[k][1]
                nxt = [k2 for p in (xy[0], xy[-1]) for k2 in by_node.get(nk(p), ())]
                for k2 in nxt + sorted(crossing.get(k, ())):
                    if k2 not in comp:
                        comp.add(k2)
                        stack.append(k2)
            deg = {}
            for k in comp:
                xy = pool[k][1]
                for p in (xy[0], xy[-1]):
                    deg[nk(p)] = deg.get(nk(p), 0) + 1
            grew = False
            for k in comp:
                xy = pool[k][1]
                for p in (xy[0], xy[-1]):
                    if deg[nk(p)] != 1:
                        continue
                    la, lo, _ = self.frame.enu_to_geodetic(np.array([p[0]]), np.array([p[1]]), 0.0)
                    key = mapcells.cell_of(float(la[0]), float(lo[0]))
                    if str(key) not in loaded and len(loaded) < CHAIN_CELLS:
                        loaded.add(str(key))
                        self._load(key, pool, ground)
                        grew = True
            if not grew:
                return sorted(comp)

    def parts(self, cells_feats, box):
        """[(fid, spec, xy, z, lon)] of the bridge parts in these cells that come near box."""
        x0, y0, x1, y1 = box
        out, todo = [], []
        for feats in cells_feats:
            if feats is None or len(feats) == 0:
                continue
            bs = bridge_specs(feats)
            for i in range(len(feats)):
                if bs[i] is None:
                    continue
                fid = str(feats.ids[i])
                for j, ring in enumerate(bridge_parts(feats, i)):
                    e, n, _ = self.frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
                    if e.max() < x0 - NEAR_M or e.min() > x1 + NEAR_M or n.max() < y0 - NEAR_M or n.min() > y1 + NEAR_M:
                        continue
                    todo.append(((fid, j), feats, i))
        for key, feats, i in todo:
            with self.lock:
                got = self.done.get(key)
            if got is None:
                pool, ground, loaded = {}, {}, set()
                ring = bridge_parts(feats, i)[key[1]]
                for p in (ring[0], ring[-1]):
                    ck = mapcells.cell_of(float(p[1]), float(p[0]))
                    if str(ck) not in loaded:
                        loaded.add(str(ck))
                        self._load(ck, pool, ground)
                if key not in pool:
                    continue
                comp = self._resolve(key, pool, ground, loaded)
                lines = [pool[k][1] for k in comp]
                gl = [g for v in ground.values() for g in v]
                cr = _crossings(lines, gl)
                road_z = self._ground(np.array([c[2] for c in cr])) if cr else []
                zs = deck_profile(lines, self._ground, [(c[0], c[1], z) for c, z in zip(cr, road_z)],
                                  levels=[pool[k][3] for k in comp], widths=[pool[k][0].width for k in comp])
                with self.lock:
                    for k, z in zip(comp, zs):
                        spec, xy, lon, _ = pool[k]
                        self.done.setdefault(k, (spec, xy, z, lon))
                    got = self.done.get(key)
            if got is not None:
                spec, xy, z, lon = got
                out.append((key[0], spec, xy, z, lon))
        return out


def _bridge_chunks(frame, bseen, elev, exact, bridges=None, cells=None):
    """The chunks of the bridges crossing the tile box `exact` (deck surfaces flagged "deck", their
    centre / edge lines; every bridge chunk "fixed": its z is the deck's, not the ground's). Deck
    heights: from `bridges` (BridgeProfiles: whole chains, shared by every tile), else from the
    chains found among the given cells (deck_heights; tests)."""
    if bridges is not None:
        return _deck_chunks([(fid, spec, np.c_[xy, z], lon) for fid, spec, xy, z, lon in bridges.parts(cells, exact)
                             if np.isfinite(z).all()], exact)
    if not bseen:
        return []
    parts = []                                  # (fid, spec, xy (k, 2), lon)
    for fid in sorted(bseen):
        feats, i, spec = bseen[fid]
        for ring in bridge_parts(feats, i):
            if len(ring) >= 2:
                e, n, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
                parts.append((fid, spec, densify(np.stack([e, n], axis=1), DECK_STEP),
                              float(np.mean(ring[:, 0]))))

    def ground(xy):
        la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        h = np.asarray(elev(la, lo), np.float64)
        return frame.geodetic_to_enu(la, lo, h)[2]
    zs = deck_heights([p[2] for p in parts], ground)
    allxy = np.concatenate([p[2] for p in parts])
    gz = ground(allxy)                          # one call: the ground under every deck point
    decks, k = [], 0
    for (fid, spec, xy, lon), z in zip(parts, zs):
        g = gz[k:k + len(xy)]
        k += len(xy)
        if not np.isfinite(z).all():
            continue
        # never below the ground under it (2026-10-11: abutments on low spots buried decks), then
        # smoothed along it like a road profile (the ends keep their heights: the abutments)
        g = g + DECK_CLEAR
        z = np.maximum(z, g)
        if len(z) > 3:
            import grading
            zz = grading.smooth_profile(z, DECK_STEP, grading.SIGMA_M)
            zz[0], zz[-1] = z[0], z[-1]
            z = np.maximum(zz, g)
        decks.append((fid, spec, np.c_[xy, z], lon))
    return _deck_chunks(decks, exact)


def _deck_chunks(decks, exact):
    """Deck + line chunks of bridge parts [(fid, spec, pts (k, 3), lon)] clipped to the box `exact`."""
    out = []
    for fid, spec, pts, lon in decks:
        lines = [({**DECK, "deck": True}, spec.width, pts)]
        for look, w, line in extras(spec, pts, lon):
            if look["m"] != SIDEWALK["m"] and look["m"] != GUTTER["m"]:
                lines.append((look, w, line))
        for look, w, line in lines:
            for piece in clip_polyline(line, *exact):
                for c in split(densify(simplify(piece))):
                    if len(c) >= 2:
                        ch = _chunk(look, w, c)
                        ch["fixed"] = True
                        if look.get("deck"):
                            ch["deck"] = True
                        out.append(ch)
    return out


def _on_box(p, box, eps=1e-6):
    x0, y0, x1, y1 = box
    return abs(p[0] - x0) < eps or abs(p[0] - x1) < eps or abs(p[1] - y0) < eps or abs(p[1] - y1) < eps


def drains(run, side, caps, walk_w=None):
    """The storm drain chunks along a sidewalk run (k, 3) on `side` of its road: DRAIN_IN m before its
    end if that is a real end (caps: (start, end) real) and every DRAIN_EVERY m along it. Pure."""
    import sidewalks
    curb, out = sidewalks.run_geometry(run, side, walk_w or SIDEWALK["w"])
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(curb[:, :2], axis=0).T))]
    L = s[-1]
    if L < 2 * DRAIN_IN:
        return []
    at = []
    if caps[1]:
        at.append(L - DRAIN_IN)
    if L > DRAIN_EVERY * 1.5:
        at += list(np.arange(DRAIN_EVERY, L - DRAIN_EVERY / 2, DRAIN_EVERY))
    res = []
    for a in sorted(at):
        pts = []
        for d in (a - DRAIN["len"] / 2, a + DRAIN["len"] / 2):
            x = np.interp(d, s, curb[:, 0])
            y = np.interp(d, s, curb[:, 1])
            z = np.interp(d, s, curb[:, 2])
            k = min(int(np.searchsorted(s, d)), len(s) - 1)
            o = out[k] / max(np.hypot(*out[k]), 1e-9)
            pts.append([x - o[0] * (GUTTER["w"] / 2), y - o[1] * (GUTTER["w"] / 2), z])
        res.append(_chunk(DRAIN, DRAIN["w"], np.array(pts)))
    return res


def build_tile(frame, tx, ty, cells, elev, urban_fn=None, walks=None, bridges=None, widths=None):
    """The chunks of one tile. cells: the mapgeom.Features of the "roads" layer of every cell touching
    the tile; elev(lat, lon) -> metres (the DEM); urban_fn: as road_specs. Features in several cells are
    taken once (by id). The carriageways, then each road's sidewalks / gutters / markings (extras), cut
    where another road's carriageway crosses them. walks: a list that gets the sidewalk runs (dicts
    pts / side / caps, clipped exactly to the tile, caps: whether each end is a real end) for the
    sidewalk mesh (sidewalks.py) and storm drains among the chunks; None: sidewalks as decals.
    bridges: a BridgeProfiles (deck heights of whole chains, shared by every tile); None: from the
    chains among these cells. Pure given its inputs (sorted by feature id)."""
    x0, y0, x1, y1 = tile_bbox(tx, ty)
    box = (x0 - OVERLAP, y0 - OVERLAP, x1 + OVERLAP, y1 + OVERLAP)
    near = (x0 - NEAR_M, y0 - NEAR_M, x1 + NEAR_M, y1 + NEAR_M)
    seen, bseen = {}, {}
    for feats in cells:
        if feats is None or len(feats) == 0:
            continue
        specs = road_specs(feats, urban_fn, widths)
        bspecs = bridge_specs(feats)
        for i in range(len(feats)):
            if bspecs[i] is not None and str(feats.ids[i]) not in bseen:
                bseen[str(feats.ids[i])] = (feats, i, bspecs[i])
            if specs[i] is None or str(feats.ids[i]) in seen:
                continue
            seen[str(feats.ids[i])] = (feats, i, specs[i])
    rings = []                                  # (fid, spec, lon/lat ring) near the tile (xy at h = 0)
    for fid in sorted(seen):
        feats, i, spec = seen[fid]
        for ring in ground_parts(feats, i):
            e, n, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
            if (e.max() < near[0] or e.min() > near[2] or n.max() < near[1] or n.min() > near[3]):
                continue
            rings.append((fid, spec, ring))
    roads = []                                  # (fid, spec, centreline (k, 3), lon) near the tile
    if rings:                                   # one elevation call for every ring (the grader's cost
        lon = np.concatenate([r[:, 0] for _, _, r in rings])     # is per call: 2026-10-11 profile,
        lat = np.concatenate([r[:, 1] for _, _, r in rings])     # 1,240 calls took 7 of a tile's 8 s)
        h = np.asarray(elev(lat, lon), np.float64)
        e, n, u = frame.geodetic_to_enu(lat, lon, h)
        k = 0
        for fid, spec, ring in rings:
            m = len(ring)
            roads.append((fid, spec, np.stack([e[k:k + m], n[k:k + m], u[k:k + m]], axis=1),
                          float(np.mean(ring[:, 0]))))
            k += m
    chunks = []
    for fid, spec, pts, _ in roads:
        look = LOOK[spec.kind]
        lanes = [(0.0, spec.width)]
        if look.get("strip"):                   # parallel strips across the width (see LOOK)
            n = max(1, int(round(spec.width / look["strip"])))
            sw = spec.width / n
            lanes = [((k + 0.5) * sw - spec.width / 2, sw) for k in range(n)]
        for off, lw in lanes:
            line = pts if off == 0.0 else offset_polyline(pts, off)
            for piece in clip_polyline(line, *box):
                for c in split(densify(simplify(piece))):
                    if len(c) >= 2:
                        chunks.append(_chunk(look, lw, c))
    chunks.extend(_bridge_chunks(frame, bseen, elev, (x0, y0, x1, y1), bridges, cells))
    exact = (x0, y0, x1, y1)
    corners, jcuts, crosswalks, jramps, jlines, jinternal = ({}, {}, [], {}, {}, set()) if walks is None else \
        junctions.find(roads, has_sidewalks, None, PARKING_M,
                       lambda sp: sp.urban and sp.cls in PARKING_CLASSES and sp.kind in BUILT and not sp.oneway
                       and sp.width >= STYLE.get(sp.cls, STYLE["unknown"])[0] + 2 * PARKING_M - 0.1)
    cornered = set()                            # road pairs whose sidewalks meet at a curb return
    if walks is not None:
        for (fid, side), cl in jcuts.items():
            for X, u, t in cl:
                for f2, s2, p2, _ in roads:
                    if f2 != fid and np.hypot(*(p2[:, :2] - X).T).min() < 1.0:
                        cornered.add((fid, f2))
        for c in corners:                       # each corner / crosswalk made by the tile it is in
            p = c["arc"][len(c["arc"]) // 2]
            if x0 <= p[0] < x1 and y0 <= p[1] < y1:
                walks.append({"corner": c})
                if c["fill"] is not None:
                    chunks.append({"m": LOOK["paved"]["m"], "w": 0.0, "tl": LOOK["paved"]["tl"], "p": LOOK["paved"]["p"],
                                   "poly": [[round(float(v), 3) for v in q] for q in c["fill"]], "n": []})
        for cw in crosswalks:                   # crosswalks and stop lines
            mid = 0.5 * (cw["a"] + cw["b"])
            if x0 <= mid[0] < x1 and y0 <= mid[1] < y1:
                look = junctions.STOP_LINE if cw.get("stop") else junctions.CROSSWALK
                chunks.append(_chunk(look, cw["w"], np.array([cw["a"], cw["b"]])))
    for fid, spec, pts, lon in roads:
        ex = extras(spec, pts, lon) if fid not in jinternal else []    # inside a junction: none
        if not ex:
            continue
        blockers = [(p2[:, :2], s2.width / 2 + CUT_MARGIN) for f2, s2, p2, _ in roads if f2 != fid]
        walk_blockers = [(p2[:, :2], s2.width / 2 if (fid, f2) in cornered else sidewalk_stop(spec, fid, s2, f2))
                         for f2, s2, p2, _ in roads if f2 != fid]
        for look, w, line in ex:
            walk = look["m"] == SIDEWALK["m"]
            side = look.get("side")
            if walk and walks is not None:
                for piece in clip_polyline(line, *exact):
                    for cpiece, (j0, j1) in junctions.apply_cuts(piece, jcuts.get((fid, side), [])):
                        for run in cut_outside(cpiece, walk_blockers, 0.5):
                            run = densify(simplify(run))
                            if len(run) < 2:
                                continue
                            at_j0 = j0 and np.hypot(*(run[0, :2] - cpiece[0, :2])) < 0.6
                            at_j1 = j1 and np.hypot(*(run[-1, :2] - cpiece[-1, :2])) < 0.6
                            caps = (not _on_box(run[0], exact) and not at_j0, not _on_box(run[-1], exact) and not at_j1)
                            ramps = junctions.ramp_ranges(run, jramps.get((fid, side), []))
                            walks.append({"pts": run, "side": side, "caps": caps, "ramps": ramps, "w": w})
                            chunks.extend(drains(run, side, (caps[0] or at_j0, caps[1] or at_j1), w))
                continue
            if walks is not None and look["m"] == GUTTER["m"]:     # gutters end where their curb turns
                gcuts = jcuts.get((fid, side), [])
                pieces = [cp for p in clip_polyline(line, *box) for cp, _ in junctions.apply_cuts(p, gcuts)]
            elif walks is not None and jlines.get(fid):             # markings stop at the crosswalks
                pieces = [cp for p in clip_polyline(line, *box) for cp, _ in junctions.apply_cuts(p, jlines[fid])]
            else:
                pieces = clip_polyline(line, *box)
            for piece in pieces:
                for run in cut_outside(piece, walk_blockers if walk else blockers, 0.5 if walk else 2.0):
                    for c in split(densify(simplify(run))):
                        if len(c) >= 2:
                            chunks.append(_chunk(look, w, c))
    return chunks


class Tracker:
    """Per connection: which road tiles the mod has, building and announcing the ones it needs."""

    def __init__(self):
        self.aid = None
        self.sent = set()       # (tx, ty) announced for self.aid
        self.building = set()

    def on_pose(self, conn, aid, x, y, loop, build):
        """Called with the player's position in the active anchor's frame. build(aid, tx, ty) runs in a
        worker and returns the tile's (path, n) or None; its result is announced with conn.send."""
        if aid != self.aid:
            self.aid, self.sent, self.building = aid, set(), set()
        want = [k for k in tiles_near(x, y, SEND_M) if k not in self.sent and k not in self.building]
        want.sort(key=lambda k: tile_distance(k[0], k[1], x, y))
        for tx, ty in want[:BUILD_PER_POSE]:
            self.building.add((tx, ty))
            fut = loop.run_in_executor(None, build, aid, tx, ty)
            fut.add_done_callback(lambda f, k=(tx, ty), a=aid: self._built(conn, a, k, f))
        drop = [k for k in self.sent if tile_distance(k[0], k[1], x, y) > DROP_M]
        for k in drop:
            self.sent.discard(k)
            conn.send({"type": "roads_drop", "anchorId": aid, "key": f"{k[0]}_{k[1]}"})

    def _built(self, conn, aid, key, fut):
        self.building.discard(key)
        if aid != self.aid:
            return
        try:
            res = fut.result()
        except Exception as e:      # network trouble: the next pose asks again
            log.debug("roads tile %s: %s: %s", key, type(e).__name__, e)
            return
        if res is None:
            return
        path, n = res
        self.sent.add(key)
        conn.send({"type": "roads_tile", "anchorId": aid, "key": f"{key[0]}_{key[1]}", "path": path, "n": n})


def write_tile(out_dir, vfs_dir, aid, tx, ty, chunks, walks=None, road_mesh=None, deck_mesh=None):
    """Write a tile's chunks (atomically), its sidewalk mesh (walks: build_tile's runs, if any:
    `walks_<a>_<tx>_<ty>.cdae`, announced in the tile as "walk": {shape, pos}) and its road mesh
    (road_mesh: roadmesh.build's result: `road_<a>_<tx>_<ty>.cdae`, "road": {shape, pos}; the mod then
    shows it instead of a DecalRoad per chunk) and its bridge decks (deck_mesh: roadmesh.deck's result:
    `deck_<a>_<tx>_<ty>.cdae`, "deck": {shape, pos}, colliding); returns (vfs path, number of chunks)."""
    import roadmesh
    import sidewalks
    name = f"roads_{aid}_{tx}_{ty}.json"
    path = os.path.join(out_dir, name)
    data = {"v": VERSION, "chunks": chunks}
    if road_mesh is not None:
        x0, y0, _, _ = tile_bbox(tx, ty)
        shape = f"road_{aid}_{tx}_{ty}.cdae"
        roadmesh.write(os.path.join(out_dir, shape), road_mesh)
        data["road"] = {"shape": f"{vfs_dir}/{shape}", "pos": [x0, y0, 0.0]}
    if deck_mesh is not None:
        x0, y0, _, _ = tile_bbox(tx, ty)
        shape = f"deck_{aid}_{tx}_{ty}.cdae"
        roadmesh.write(os.path.join(out_dir, shape), deck_mesh)
        data["deck"] = {"shape": f"{vfs_dir}/{shape}", "pos": [x0, y0, 0.0]}
    if walks:
        x0, y0, _, _ = tile_bbox(tx, ty)
        b = sidewalks.Builder((x0, y0, 0.0))
        for wk in walks:
            if "corner" in wk:
                c = wk["corner"]
                b.add_corner(c["arc"], c["inward"], c["outer"], ramps=c.get("ramps", ()))
                continue
            ww = wk.get("w", SIDEWALK["w"])
            curb, out = sidewalks.run_geometry(wk["pts"], wk["side"], ww)
            b.add_run(curb, out, ww, caps=wk["caps"], ramps=wk.get("ramps", ()))
        if not b.empty():
            shape = f"walks_{aid}_{tx}_{ty}.cdae"
            b.write(os.path.join(out_dir, shape))
            data["walk"] = {"shape": f"{vfs_dir}/{shape}", "pos": [x0, y0, 0.0]}
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, path)
    return f"{vfs_dir}/{name}", len(chunks)
