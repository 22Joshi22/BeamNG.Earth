"""Roads near the player (docs/ROADMAP.md v1.6 "Roads"; first cut 2026-10-11): Overture road segments
from the map-data cache (mapdata.py "roads"), drawn by the mod as DecalRoads on the physics ground, and
painted into the surface masks (surface.py) so the tyres feel asphalt or dirt.

Tiles: TILE x TILE m squares of the anchor's ENU frame, keyed (anchor, tx, ty). The Tracker of a
connection keeps the tiles within SEND_M of the player sent (built in a worker, written to
`roads_<a>_<tx>_<ty>.json` in the session folder, announced with a `roads_tile` message) and drops
those beyond DROP_M (`roads_drop`). An anchor change re-sends everything in the new frame; the mod
drops the old anchor's roads then.

A tile's file is a list of chunks, each one DecalRoad:
    {"m": material, "w": width m, "tl": texture length m, "p": render priority, "n": [[x, y, z], ...]}
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

import numpy as np

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
PAVED = {"paved", "asphalt", "concrete", "paving_stones", "sett", "cobblestone", "metal", "wood"}
GRAVEL = {"gravel", "fine_gravel", "compacted", "pebblestone"}
# surface kind -> DecalRoad look (stock materials, loaded by the mod from the official levels)
LOOK = {
    "paved": {"m": "road_asphalt_2lane", "tl": 11.0, "p": 20},
    "gravel": {"m": "m_dirt_road", "tl": 88.0, "p": 21},
    "dirt": {"m": "m_dirt_road", "tl": 88.0, "p": 21},
}
SKIP_FLAGS = {"is_bridge", "is_tunnel"}


def _rule_value(table, i):
    """The value of the rule of feature i covering the largest share of it, or None."""
    if table is None or len(table.feature) == 0:
        return None
    sel = np.nonzero(table.feature == i)[0]
    if len(sel) == 0:
        return None
    span = table.end[sel] - table.start[sel]
    return table.value[sel[int(np.argmax(span))]]


def surface_kind(cls, surface):
    """'paved', 'gravel' or 'dirt' from the class and the surface rule value (None if unknown)."""
    if surface is not None:
        s = str(surface).lower()
        if s in PAVED:
            return "paved"
        if s in GRAVEL:
            return "gravel"
        return "dirt"
    return "paved" if STYLE.get(cls, STYLE["unknown"])[1] else "dirt"


def road_specs(feats):
    """Per feature: None (left out) or (width m, surface kind). Pure."""
    out = []
    rules = feats.rules
    for i in range(len(feats)):
        cls = str(feats.attrs["class"][i]) if "class" in feats.attrs else "unknown"
        if str(feats.attrs.get("subtype", ["road"] * len(feats))[i]) != "road" or cls not in STYLE:
            out.append(None)
            continue
        flags = _rule_value(rules.get("flags"), i)
        level = _rule_value(rules.get("level"), i)
        if (flags is not None and str(flags) in SKIP_FLAGS) or (level is not None and float(level) != 0.0):
            out.append(None)
            continue
        width = _rule_value(rules.get("width"), i)
        w = float(width) if width is not None and 1.0 <= float(width) <= 40.0 else STYLE[cls][0]
        out.append((w, surface_kind(cls, _rule_value(rules.get("surface"), i))))
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


def cells_of_tile(frame, tx, ty):
    x0, y0, x1, y1 = tile_bbox(tx, ty)
    lat, lon, _ = frame.enu_to_geodetic(np.array([x0 - OVERLAP, x1 + OVERLAP, x0 - OVERLAP, x1 + OVERLAP]),
                                        np.array([y0 - OVERLAP, y0 - OVERLAP, y1 + OVERLAP, y1 + OVERLAP]), 0.0)
    return mapcells.cells_for_bbox(float(np.min(lat)), float(np.min(lon)), float(np.max(lat)), float(np.max(lon)))


def build_tile(frame, tx, ty, cells, elev):
    """The chunks of one tile. cells: the mapgeom.Features of the "roads" layer of every cell touching
    the tile; elev(lat, lon) -> metres (the DEM). Features in several cells are taken once (by id).
    Pure given its inputs (sorted by feature id, so the output does not depend on cell order)."""
    x0, y0, x1, y1 = tile_bbox(tx, ty)
    box = (x0 - OVERLAP, y0 - OVERLAP, x1 + OVERLAP, y1 + OVERLAP)
    seen = {}
    for feats in cells:
        if feats is None or len(feats) == 0:
            continue
        specs = road_specs(feats)
        for i in range(len(feats)):
            if specs[i] is None or str(feats.ids[i]) in seen:
                continue
            seen[str(feats.ids[i])] = (feats, i, specs[i])
    chunks = []
    for fid in sorted(seen):
        feats, i, (w, kind) = seen[fid]
        for part in feats.parts(i):
            for ring in part:
                if len(ring) < 2:
                    continue
                lon, lat = ring[:, 0], ring[:, 1]
                h = np.asarray(elev(lat, lon), np.float64)
                e, n, u = frame.geodetic_to_enu(lat, lon, h)
                pts = np.stack([e, n, u], axis=1)
                if (pts[:, 0].max() < box[0] or pts[:, 0].min() > box[2] or pts[:, 1].max() < box[1]
                        or pts[:, 1].min() > box[3]):
                    continue
                for piece in clip_polyline(pts, *box):
                    piece = densify(simplify(piece))
                    for c in split(piece):
                        if len(c) >= 2:
                            look = LOOK[kind]
                            chunks.append({"m": look["m"], "w": round(w, 2), "tl": look["tl"], "p": look["p"],
                                           "n": [[round(float(v), 3) for v in p] for p in c]})
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


def write_tile(out_dir, vfs_dir, aid, tx, ty, chunks):
    """Write a tile's chunks (atomically); returns (vfs path, number of chunks)."""
    name = f"roads_{aid}_{tx}_{ty}.json"
    path = os.path.join(out_dir, name)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"v": VERSION, "chunks": chunks}, f, separators=(",", ":"))
    os.replace(tmp, path)
    return f"{vfs_dir}/{name}", len(chunks)
