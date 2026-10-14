"""The AI road graph ("navgraph") around the player, for BeamNG's own traffic and AI (docs/research/
2026-10-10-navgraph-traffic-signals.md): the mod hands it to `map.reset(customNodes)`.

Nodes are Overture connectors (named "c<connector id>": the same name in every window and anchor, and shared
by every segment meeting at a junction) and the simplified shape points between them ("s<segment id>_<k>").
Each edge carries what the custom map path does not compute (research §2.4): its half width (radius), lanes
(per direction), one-way direction, speed limit (m/s) and drivability. Positions are in the anchor frame, z the
ground (elev); the mod clips the graph to the terrain block before using it (the only collidable ground).

Pure given the map cells and elev; deterministic.
"""
import math

import numpy as np

import roads

# classes the AI drives on: drivability (path cost divisor) and a default speed limit (mph, US practice)
DRIVE = {"motorway": (1.0, 65), "trunk": (1.0, 55), "primary": (1.0, 45), "secondary": (1.0, 40),
         "tertiary": (0.9, 35), "residential": (0.8, 25), "unclassified": (0.8, 30), "living_street": (0.5, 15),
         "service": (0.4, 15), "unknown": (0.6, 25)}
SKIP_SUBCLASSES = {"driveway", "parking_aisle", "sidewalk", "crosswalk", "cycle_crossing"}
URBAN_MPH_CAP = {"primary": 35, "secondary": 35, "tertiary": 30}   # in town (spec.urban) unless mapped
MPH = 0.44704
LANE_W_M = 3.3
SIMPLIFY_M = 0.6            # a shape point is kept where the chord would be off by more than this
MAX_EDGE_M = 80.0           # long straight edges are split (the AI and the spawner like shorter ones)
WINDOW_M = 1100.0           # half size of the square around the player the graph covers


def _rdp(p, tol):
    """Indices kept by Ramer-Douglas-Peucker on polyline p (k, 2)."""
    keep = np.zeros(len(p), bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(p) - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        d = p[b] - p[a]
        L = math.hypot(*d)
        seg = p[a + 1:b] - p[a]
        dist = np.abs(seg[:, 0] * d[1] - seg[:, 1] * d[0]) / L if L > 1e-9 else np.hypot(*seg.T)
        k = int(dist.argmax())
        if dist[k] > tol:
            keep[a + 1 + k] = True
            stack += [(a, a + 1 + k), (a + 1 + k, b)]
    return np.flatnonzero(keep)


def _lanes(width, oneway):
    n = max(1, int(round(width / LANE_W_M)))
    if oneway:
        return n, 0
    n = max(2, n)
    return (n + 1) // 2, n // 2


def _deck_z(decks, fid, x, y):
    """The deck height at (x, y) of feature fid's bridge parts (decks: fid -> [(xy (k, 2), z (k,))]), or None
    when the point is not on one (more than 1.5 m off its line)."""
    best = None
    for xy, z in decks.get(fid, ()):
        a, b = xy[:-1], xy[1:]
        d = b - a
        L2 = np.maximum((d * d).sum(1), 1e-12)
        t = np.clip(((np.array([x, y]) - a) * d).sum(1) / L2, 0, 1)
        q = a + t[:, None] * d
        dist = np.hypot(q[:, 0] - x, q[:, 1] - y)
        k = int(dist.argmin())
        if dist[k] < 1.5 and (best is None or dist[k] < best[0]):
            best = (float(dist[k]), float(z[k] + t[k] * (z[k + 1] - z[k])))
    return None if best is None else best[1]


def build(frame, feats_list, elev, centre, urban_fn=None, window=WINDOW_M, decks=None, widths=None, left_hand=False):
    """{"nodes": {name: [x, y, z, radius]}, "edges": [[in, out, oneway, lanes_out, lanes_in, speed m/s,
    drivability, radius]]} of the drivable roads in the square of half size `window` around centre (x, y) of
    the anchor frame. feats_list: the "roads" mapgeom.Features of the cells around (duplicates across cells
    are taken once). elev(lat, lon) -> metres. decks: fid -> [(xy, z)] of the bridge parts (roads.BridgeProfiles):
    nodes on a bridge take its deck height (else traffic would spawn on the ground under it), and roads that are
    bridge all along are in the graph (road_specs leaves them to the bridge builder). widths: the road tiles'
    measured street widths (roadwidth.py), so the AI's lanes are on the carriageway the road mesh draws."""
    cx, cy = centre
    nodes, edges, seen = {}, [], set()
    arms = {}               # connector name -> [(fid, spec, unit vector away, point, neighbour, incoming)]
    pending = {}            # name -> (x, y) of nodes whose z is still to sample
    node_fid = {}           # name -> the feature it was made on (its deck, if any)
    for feats in feats_list:
        if feats is None or len(feats) == 0:
            continue
        specs = roads.road_specs(feats, urban_fn, widths)
        bspecs = roads.bridge_specs(feats)
        specs = [sp if sp is not None else bs for sp, bs in zip(specs, bspecs)]
        rules = feats.rules
        sub = rules.get("subclass")
        con = rules.get("connectors")
        speed = rules.get("max_speed_kmh")
        ow = rules.get("oneway")
        for i, spec in enumerate(specs):
            fid = str(feats.ids[i])
            if spec is None or fid in seen or spec.cls not in DRIVE:
                continue
            seen.add(fid)
            if roads._rule_value(sub, i) in SKIP_SUBCLASSES:
                continue
            parts = feats.parts(i)
            if not parts or not parts[0]:
                continue
            ll = np.asarray(parts[0][0], np.float64)
            if len(ll) < 2:
                continue
            e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.0)
            p = np.stack([e, n], 1)
            if (np.abs(p[:, 0] - cx).min() > window + 200) or (np.abs(p[:, 1] - cy).min() > window + 200):
                continue
            seg = np.hypot(*np.diff(p, axis=0).T)
            s = np.concatenate([[0.0], np.cumsum(seg)])
            L = float(s[-1])
            if L < 1.0:
                continue
            # the nodes along it: connectors at their arc length, shape points that bend it, long gaps split
            marks = {}
            if con is not None:
                for r in con.rows(i):
                    marks[round(float(con.start[r]) * L, 2)] = "c" + str(con.value[r])
            for k in _rdp(p, SIMPLIFY_M):
                sk = round(float(s[k]), 2)
                if not any(abs(sk - m) < 1.0 for m in marks):
                    marks[sk] = f"s{fid}_{k}"
            if 0.0 not in marks and not any(m < 1.0 for m in marks):
                marks[0.0] = f"s{fid}_0"
            if not any(m > L - 1.0 for m in marks):
                marks[round(L, 2)] = f"s{fid}_{len(p) - 1}"
            order = sorted(marks)
            full = []
            for a, b in zip(order[:-1], order[1:]):
                full.append(a)
                gap = b - a
                if gap > MAX_EDGE_M:
                    m = int(math.ceil(gap / MAX_EDGE_M))
                    for j in range(1, m):
                        sj = round(a + gap * j / m, 2)
                        marks[sj] = f"s{fid}_{int(round(sj * 10))}d"
                        full.append(sj)
            full.append(order[-1])
            xy = np.stack([np.interp(full, s, p[:, 0]), np.interp(full, s, p[:, 1])], 1)
            names = [marks[v] for v in full]
            # this road's edge attributes
            w = float(spec.width)
            mid = 0.5
            kmh = None
            if speed is not None:
                rows_ = speed.rows(i)
                for r in rows_:
                    if speed.start[r] <= mid <= speed.end[r]:
                        kmh = float(speed.value[r])
            driv, mph = DRIVE[spec.cls]
            if kmh is None and spec.urban and spec.cls in URBAN_MPH_CAP:
                mph = min(mph, URBAN_MPH_CAP[spec.cls])
            v = kmh / 3.6 if kmh else mph * MPH
            direction = 0
            if ow is not None:
                for r in ow.rows(i):
                    if ow.start[r] <= mid <= ow.end[r]:
                        direction = int(ow.value[r])
            lo, li = _lanes(w, direction != 0)
            # the junction arms at this road's two ends (furniture.junction_controls decides the signals)
            for end in (0, -1):
                j, nb = (0, 1) if end == 0 else (len(names) - 1, len(names) - 2)
                if not names[j].startswith("c") or names[j] == names[nb]:
                    continue
                d = xy[nb] - xy[j]
                dl = float(np.hypot(*d))
                if dl < 1e-6:
                    continue
                # traffic comes into the junction on this arm unless it is one-way out of it
                incoming = direction == 0 or (direction > 0) == (end == -1)
                arms.setdefault(names[j], []).append(
                    (fid, spec, d / dl, np.array([xy[j][0], xy[j][1], 0.0]), names[nb], incoming))
            for j in range(len(names) - 1):
                a, b = names[j], names[j + 1]
                if a == b:
                    continue
                if direction < 0:
                    a, b = b, a
                pa, pb = xy[j] if direction >= 0 else xy[j + 1], xy[j + 1] if direction >= 0 else xy[j]
                for nm, q in ((a, pa), (b, pb)):
                    if nm not in nodes and nm not in pending:
                        pending[nm] = (float(q[0]), float(q[1]))
                        node_fid[nm] = fid
                edges.append([a, b, 1 if direction else 0, lo, li, round(v, 2), driv, round(w / 2, 2)])
    # keep the edges with both ends in the window; z of their nodes from the ground
    inside = {nm for nm, (x, y) in pending.items() if abs(x - cx) <= window and abs(y - cy) <= window}
    edges = [e for e in edges if e[0] in inside and e[1] in inside]
    used = sorted({e[0] for e in edges} | {e[1] for e in edges})
    if used:
        X = np.array([pending[nm][0] for nm in used])
        Y = np.array([pending[nm][1] for nm in used])
        la, lo, _ = frame.enu_to_geodetic(X, Y, 0.0)
        Z = np.asarray(elev(la, lo), np.float64).copy()
        if decks:
            for k, nm in enumerate(used):
                dz = _deck_z(decks, node_fid.get(nm), X[k], Y[k])
                if dz is not None:
                    Z[k] = dz
        radius = {}
        for e in edges:
            for nm in e[:2]:
                radius[nm] = max(radius.get(nm, 0.0), e[7])
        for nm, x, y, z in zip(used, X, Y, Z):
            nodes[nm] = [round(float(x), 2), round(float(y), 2), round(float(z), 2), radius[nm]]
    signals = _signals(arms, nodes, left_hand)
    # one edge per node pair (two segments joining the same connectors: keep the first)
    uniq, pairs = [], set()
    for e in edges:
        k = (min(e[0], e[1]), max(e[0], e[1]))
        if k not in pairs:
            pairs.add(k)
            uniq.append(e)
    return {"nodes": nodes, "edges": uniq, "signals": signals}


STOP_BACK_M = 2.0           # the stop point: this far before the junction's widest road edge


def _signals(arms, nodes, left_hand=False):
    """[{"j": junction node, "kind": "signal" | "stop", "app": [[approach node, x, y, z, dir x, dir y, phase]]}] of
    the urban junctions furniture.junction_controls controls: per incoming approach, its stop point on the right
    half of the approach (where the AI's closest-edge search finds that approach) and the travel direction; the
    signals' phase (0 / 1) by the approach's axis."""
    import furniture
    out = []
    for name, a in arms.items():
        if name not in nodes or len(a) < 3 or not all(x[1].urban for x in a):
            continue
        p = a[0][3]
        k = (round(float(p[0]) / furniture.JUNCTION_SNAP_M), round(float(p[1]) / furniture.JUNCTION_SNAP_M))
        put = furniture.junction_controls([x[:4] for x in a], k)
        if not put:
            continue
        kind = "signal" if put[0][1] == furniture.G_SIGNAL else "stop"
        axis = put[0][0][2]
        wide = max(x[1].width for x in a) / 2
        jx, jy, jz = nodes[name][:3]
        app = []
        for arm, _ in put:
            full = next(x for x in a if x[:4] is arm or (x[0] == arm[0] and x[2] is arm[2]))
            fid, spec, u, _, nb, incoming = full
            if not incoming or nb not in nodes:
                continue
            inc = -u
            right = np.array([inc[1], -inc[0]]) * (-1 if left_hand else 1)     # the side traffic keeps to
            q = np.array([jx, jy]) + u * (wide + STOP_BACK_M) + right * (spec.width / 4)
            phase = 0 if abs(float(u @ axis)) > 0.7 else 1
            # the head's prop where furniture.junction_props stands it (the switchable stock boom replaces it)
            pp = np.array([jx, jy]) + u * (wide + 2.5) + right * (spec.width / 2 + 1.0)
            app.append([nb, round(float(q[0]), 2), round(float(q[1]), 2), round(jz, 2),
                        round(float(inc[0]), 4), round(float(inc[1]), 4), phase,
                        round(float(pp[0]), 2), round(float(pp[1]), 2), round(jz + 0.15, 2),
                        round(furniture._yaw(right[0], right[1]), 1)])
        if app:
            out.append({"j": name, "kind": kind, "app": app})
    return out


# Road rules by country (ISO 3166-1 alpha-2; the nearest airport's, AirportDB: a stand-in until a border map)
LEFT_HAND = {"GB", "IE", "IM", "JE", "GG", "MT", "CY", "IN", "PK", "BD", "LK", "NP", "BT", "MV", "JP", "ID", "MY",
             "SG", "BN", "TH", "TL", "HK", "MO", "AU", "NZ", "PG", "FJ", "WS", "TO", "KI", "SB", "TV", "NR", "NU",
             "CK", "ZA", "NA", "BW", "ZW", "ZM", "MZ", "MW", "KE", "TZ", "UG", "LS", "SZ", "MU", "SC", "SH", "FK",
             "JM", "BS", "BB", "TT", "GY", "SR", "AG", "DM", "GD", "KN", "LC", "VC", "VG", "VI", "AI", "KY", "TC",
             "BM", "MS", "PN"}
TURN_ON_RED = {"US", "CA"}
RED_YELLOW = {"GB", "IE", "DE", "AT", "CH", "LI", "NL", "BE", "LU", "DK", "NO", "SE", "FI", "IS", "PL", "CZ", "SK",
              "HU", "SI", "HR", "BA", "RS", "ME", "MK", "AL", "RO", "BG", "LT", "LV", "EE", "UA", "BY", "RU", "IL"}


def road_rules(country):
    """{"rightHandDrive" (the game's name: true = driving on the LEFT), "turnOnRed", "redYellow"} of a country
    (US rules for an unknown one)."""
    c = (country or "US").upper()
    return {"rightHandDrive": c in LEFT_HAND, "turnOnRed": c in TURN_ON_RED, "redYellow": c in RED_YELLOW,
            "country": c}


def country_near(db, lat, lon):
    """The country of the airport nearest (lat, lon) in airports.AirportDB db, or None."""
    if db is None or not getattr(db, "runways", None):
        return None
    import airports
    d = airports._hav(lat, lon, db._mid[:, 0], db._mid[:, 1])
    rw = db.runways[int(np.argmin(d))]
    ap = db.airports.get(rw.airport_ident)
    return getattr(ap, "country", None) or getattr(rw, "country", None) or None


REBUILD_M = 400.0           # a new graph once the player is this far from the last one's centre


def write(out_dir, vfs_dir, aid, serial, graph):
    """The graph as navgraph_<aid>_<serial>.json in the session folder; returns its vfs path."""
    import json
    import os
    name = f"navgraph_{aid}_{serial}.json"
    path = os.path.join(out_dir, name)
    with open(path + ".tmp", "w") as f:
        json.dump(graph, f, separators=(",", ":"))
    os.replace(path + ".tmp", path)
    return f"{vfs_dir}/{name}"


class Tracker:
    """Per connection: builds a graph around the player when the anchor changes or the player moved
    REBUILD_M from the last one's centre, one at a time, and announces it ("navgraph")."""

    def __init__(self, rebuild_m=REBUILD_M):
        self.rebuild_m = rebuild_m
        self.aid = None
        self.centre = None
        self.building = False
        self.serial = 0

    def on_pose(self, conn, aid, x, y, loop, build):
        """build(aid, x, y, serial) runs in a worker and returns (vfs path, nodes, edges) or None."""
        if self.building:
            return
        if aid == self.aid and self.centre is not None and \
                max(abs(x - self.centre[0]), abs(y - self.centre[1])) < self.rebuild_m:
            return
        self.building = True
        self.serial += 1
        serial = self.serial
        fut = loop.run_in_executor(None, build, aid, x, y, serial)
        fut.add_done_callback(lambda f: self._built(conn, aid, (x, y), serial, f))

    def _built(self, conn, aid, centre, serial, fut):
        self.building = False
        try:
            res = fut.result()
        except Exception as e:      # map data not at hand: the next pose asks again
            import logging
            logging.getLogger("earthd.navgraph").debug("navgraph %s: %s: %s", centre, type(e).__name__, e)
            return
        if res is None:
            return
        self.aid, self.centre = aid, centre
        path, n, m = res
        conn.send({"type": "navgraph", "anchorId": aid, "path": path, "cx": round(centre[0], 1),
                   "cy": round(centre[1], 1), "nodes": n, "edges": m, "serial": serial})
