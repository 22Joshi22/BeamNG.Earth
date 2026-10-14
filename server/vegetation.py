"""Trees, bushes and rocks per map-data cell (docs/ROADMAP.md v1.9 "Cities & nature"; the user, 2026-10-13:
"trees, rocks and other decoration").

    veg = Vegetation(md, cache_dir)
    items = veg.cell(key)          # Items: lon, lat, height, radius, kind, group, rank, yaw, scale

Deterministic (ROADMAP "Principle: deterministic generation"): every item is a pure function of the
cell's map data and VERSION; its random choices (species, yaw, size, thinning rank) hash its position.

- Trees: the tops of Meta/WRI's 1.2 m canopy height model (CHMv2), local maxima of the lightly
  smoothed heights, in a window that grows with the height (a crown about 0.12 h + 1 m across), taller
  than TREE_MIN_M. Not on buildings (the CHM counts roofs, solar panels: Kayenta), roads (a trunk in
  the lane, under canopy that overhangs it) or water.
- Bushes: canopy maxima from BUSH_MIN_M to TREE_MIN_M, and a hashed scatter over shrubland.
- Rocks: a hashed scatter over bare / sparse land (WorldCover 60), thinner over shrubs and grass.
- Groups: deciduous / conifer by a regional mix and height, shrub, rock (the mod maps groups to its
  stock shapes).
"""
import hashlib
import logging
import math
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np
from matplotlib.path import Path

import mapcells

log = logging.getLogger("earthd.vegetation")

VERSION = 5
TREE_MIN_M = 3.0
CROWN_SPACING = 0.5          # tops kept this many crown radii apart (0.8 looked sparse: the user, 2026-10-13)
BUSH_MIN_M = 1.0
KIND_TREE, KIND_BUSH, KIND_ROCK = 0, 1, 2
GROUPS = ("deciduous", "conifer", "shrub", "rock", "pine_west", "palm", "juniper", "desert_shrub", "desert_rock",
          "oak_west", "tropical")
(G_DECIDUOUS, G_CONIFER, G_SHRUB, G_ROCK, G_PINE_WEST, G_PALM, G_JUNIPER, G_DESERT_SHRUB, G_DESERT_ROCK,
 G_OAK_WEST, G_TROPICAL) = range(len(GROUPS))
ROAD_CLEAR_M = 1.5          # trees kept this far off a road's edge (its width by class below)
ROAD_HALF_WIDTH_M = {"motorway": 7.0, "trunk": 6.0, "primary": 5.5, "secondary": 5.0, "tertiary": 4.5,
                     "residential": 4.0, "service": 3.0, "unclassified": 3.5, "living_street": 3.0}
ROAD_HALF_DEFAULT_M = 2.5
SCATTER_CELL_M = 6.0        # the hashed scatter's grid (one candidate per cell)
SCATTER_P = {20: (0.10, 0.04), 30: (0.01, 0.003), 60: (0.0, 0.08), 100: (0.02, 0.02)}   # class: (bush, rock)
MEM_CELLS = 16
WARM_M = 1200.0              # cells within this of the player are built ahead (warm_around)
WC = {"tree": 10, "shrub": 20, "grass": 30, "crop": 40, "built": 50, "bare": 60, "snow": 70,
      "water": 80, "wetland": 90, "mangrove": 95, "moss": 100}


@dataclass
class Items:
    lon: np.ndarray
    lat: np.ndarray
    height: np.ndarray          # m (trees: the CHM top; bushes and rocks: their size)
    radius: np.ndarray          # m (crown / footprint)
    kind: np.ndarray            # uint8 KIND_*
    group: np.ndarray           # uint8 GROUPS index
    rank: np.ndarray            # float32 in [0, 1): thinning (a level shows those below its threshold)
    yaw: np.ndarray             # degrees
    scale: np.ndarray           # size variation (around 1)

    def __len__(self):
        return len(self.lon)

    def take(self, sel):
        return Items(*(getattr(self, f)[sel] for f in ("lon", "lat", "height", "radius", "kind", "group",
                                                       "rank", "yaw", "scale")))


def _hash01(a, b, salt):
    """Deterministic uniform [0, 1) from two integer arrays and a salt."""
    a = np.asarray(a, np.uint64)
    b = np.asarray(b, np.uint64)
    x = a * np.uint64(0x9E3779B97F4A7C15) ^ (b + np.uint64(salt)) * np.uint64(0xC2B2AE3D27D4EB4F)
    x ^= x >> np.uint64(31)
    x *= np.uint64(0xBF58476D1CE4E5B9)
    x ^= x >> np.uint64(29)
    return (x >> np.uint64(11)).astype(np.float64) / float(1 << 53)


def _shift_max(a, r):
    """The maximum over the (2r+1)^2 window around each pixel (edges padded with -inf)."""
    p = np.pad(a, r, constant_values=-np.inf)
    out = np.full(a.shape, -np.inf, a.dtype)
    n, m = a.shape
    for dj in range(2 * r + 1):
        for di in range(2 * r + 1):
            if (dj - r) ** 2 + (di - r) ** 2 <= r * r + r:      # a disc, not a square
                np.maximum(out, p[dj:dj + n, di:di + m], out=out)
    return out


def regional(group, height, lat, lon, lcls, u):
    """The groups of a region (the stock maps' species: West Coast USA's pines, oaks and palms, Utah's junipers
    and desert bushes, Johnson Valley's rocks, Jungle Rock Island's palms and tropical trees) from the generic
    ones, by a coarse climate from the position and the land cover class at the item (until a species map).
    u: a hashed [0, 1) per item. Pure."""
    g = np.asarray(group).copy()
    lat, lon, h, lcls = np.asarray(lat), np.asarray(lon), np.asarray(height), np.asarray(lcls)
    west_coast = (lon < -114.0) & (lat > 32.0) & (lat < 49.5)
    dry_west = (lon < -102.0) & (lat > 25.0) & (lat < 46.0) & ~((lon < -121.0) & (lat > 37.0))
    open_dry = dry_west & np.isin(lcls, (WC["shrub"], WC["bare"], WC["grass"]))
    tropics = np.abs(lat) < 23.5
    warm_coast = ((lat < 30.5) & (lon > -100.0) & (lon < -79.0)) | ((lat < 34.6) & (lon < -114.0) & (lat > 20.0))
    built = lcls == WC["built"]
    # towns of the dry West are irrigated broadleaves (cottonwoods, elms) far more than conifers (Moab's photos)
    g = np.where(dry_west & built & (g == G_CONIFER) & (u < 0.8), G_DECIDUOUS, g)
    tree = (g == G_DECIDUOUS) | (g == G_CONIFER)
    # the west: pines for the conifers, oaks for the small broadleaves
    g = np.where(west_coast & (g == G_CONIFER), G_PINE_WEST, g)
    g = np.where(west_coast & (g == G_DECIDUOUS) & (h < 11.0), G_OAK_WEST, g)
    # dry open country: junipers, desert bushes and rocks
    g = np.where(open_dry & tree & (h < 9.0), G_JUNIPER, g)
    g = np.where(open_dry & (g == G_CONIFER), G_PINE_WEST, g)
    g = np.where(dry_west & (g == G_SHRUB), G_DESERT_SHRUB, g)
    g = np.where(dry_west & (g == G_ROCK), G_DESERT_ROCK, g)
    # palms: most of a warm coast's town trees, some of its others; tall broadleaves in the tropics
    palm_p = np.where(built, 0.6, 0.2)
    g = np.where((warm_coast | tropics) & tree & (u < palm_p), G_PALM, g)
    g = np.where(tropics & (g == G_DECIDUOUS) & (h >= 12.0), G_TROPICAL, g)
    return g.astype(np.uint8)


def _conifer_share(lat, lon):
    """The regional share of conifers (a coarse climate stand-in until a species map)."""
    west = lon < -100.0
    share = np.where(west, 0.7, 0.2)
    share = np.where(np.abs(lat) > 45.0, np.maximum(share, 0.6), share)
    return share


class Vegetation:
    def __init__(self, md, cache_dir=None):
        self.md = md
        self.dir = os.path.join(cache_dir, "vegetation", f"v{VERSION}") if cache_dir else None
        self._mem = OrderedDict()
        self._lock = threading.Lock()
        self._cell_locks = {}
        self._warm, self._warm_thread = [], None
        self._warm_cv = threading.Condition()

    def cell(self, key):
        """The Items of map-data cell key ((z, x, y) or CellKey)."""
        k = (int(key[1]), int(key[2]))
        with self._lock:
            if k in self._mem:
                self._mem.move_to_end(k)
                return self._mem[k]
            lock = self._cell_locks.setdefault(k, threading.Lock())
        with lock:
            with self._lock:
                if k in self._mem:
                    return self._mem[k]
            items = self._load(*k)
            with self._lock:
                self._mem[k] = items
                while len(self._mem) > MEM_CELLS:
                    self._mem.popitem(last=False)
            return items

    def warm_around(self, lat, lon, radius_m=WARM_M):
        """Build the cells within radius_m of (lat, lon) in the background (nearest first): the tiles
        near a teleport's destination then find them ready instead of waiting ~3.5 s each."""
        d = radius_m / 111320.0
        dl = d / max(math.cos(math.radians(lat)), 0.01)
        try:
            keys = mapcells.cells_for_bbox(lat - d, lon - dl, lat + d, lon + dl, max_cells=64)
        except ValueError:
            return
        keys = sorted(keys, key=lambda k: (k.center()[0] - lat) ** 2 + ((k.center()[1] - lon) * math.cos(math.radians(lat))) ** 2)
        with self._warm_cv:
            for k in keys:
                kk = (int(k.x), int(k.y))
                if kk not in self._warm and not self.ready(k):
                    self._warm.append(kk)
            if self._warm_thread is None:
                self._warm_thread = threading.Thread(target=self._warm_loop, daemon=True, name="veg-warm")
                self._warm_thread.start()
            self._warm_cv.notify()

    def _warm_loop(self):
        while True:
            with self._warm_cv:
                while not self._warm:
                    self._warm_cv.wait()
                x, y = self._warm.pop(0)
            try:
                self.cell((mapcells.ZOOM, x, y))
            except Exception:
                log.exception("vegetation: building cell %s", (x, y))

    def ready(self, key):
        k = (int(key[1]), int(key[2]))
        with self._lock:
            if k in self._mem:
                return True
        p = self._path(*k)
        return bool(p and os.path.exists(p))

    def _path(self, x, y):
        rel = getattr(self.md, "release", "x")
        return os.path.join(self.dir, str(rel), str(x), f"{y}.npz") if self.dir else None

    def _load(self, x, y):
        p = self._path(x, y)
        fields = ("lon", "lat", "height", "radius", "kind", "group", "rank", "yaw", "scale")
        if p and os.path.exists(p):
            with np.load(p, allow_pickle=False) as f:
                return Items(*(f[n] for n in fields))
        items = self._build(x, y)
        if p:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "wb") as fh:
                np.savez_compressed(fh, **{n: getattr(items, n) for n in fields})
            os.replace(tmp, p)
        return items

    # --- the build ------------------------------------------------------------------------------
    def _build(self, x, y):
        key = (mapcells.ZOOM, x, y)
        cd = self.md.cell(key, layers=("canopy", "landcover", "buildings", "roads", "water"))
        chm = cd.layers.get("canopy")
        lc = cd.layers.get("landcover")
        if chm is None:
            return _empty()
        h = np.where(chm.data == (chm.nodata if chm.nodata is not None else 255), 0, chm.data).astype(np.float32)
        # pixel centres (EPSG:3857) of the CHM window, cut to the cell (the window may be larger)
        n_r, n_c = h.shape
        mx = chm.x0 + (np.arange(n_c) + 0.5) * chm.dx
        my = chm.y0 - (np.arange(n_r) + 0.5) * chm.dy
        lon0, lat0, lon1, lat1 = cd.bounds
        ex0, ey0 = _to3857(lon0, lat0)
        ex1, ey1 = _to3857(lon1, lat1)
        ci = (mx >= ex0) & (mx < ex1)
        rj = (my > ey0) & (my <= ey1)
        if not ci.any() or not rj.any():
            return _empty()
        # the blocked pixels: buildings, roads, water (on the CHM grid, with the neighbours' features)
        blocked = np.zeros(h.shape, bool)
        road = np.zeros(h.shape, bool)
        push = np.zeros((3,) + h.shape, np.float32)
        push[2] = np.inf
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                kk = (mapcells.ZOOM, x + dx, y + dy)
                try:
                    c2 = cd if (dx, dy) == (0, 0) else self.md.cell(kk, layers=("buildings", "roads", "water"))
                except Exception as e:          # a neighbour not at hand: its features unmasked
                    log.debug("vegetation: neighbour %s: %s", kk, e)
                    continue
                _mask_polys(c2.layers.get("buildings"), blocked, chm, 1.0)
                _mask_polys(c2.layers.get("water"), blocked, chm, 0.0, kind=3)
                _mask_roads(c2.layers.get("roads"), road, chm, push)
        # smoothed heights (3 x 3 mean), then the local maxima by height band
        p = np.pad(h, 1, mode="edge")
        hs = sum(p[a:a + n_r, b:b + n_c] for a in range(3) for b in range(3)) / 9.0
        tops = np.zeros(h.shape, bool)
        for lo_m, hi_m, r in ((BUSH_MIN_M, TREE_MIN_M, 1), (TREE_MIN_M, 8.0, 2), (8.0, 15.0, 3), (15.0, 1e9, 4)):
            band = (hs >= lo_m) & (hs < hi_m)
            if band.any():
                tops |= band & (hs >= _shift_max(hs, r))
        tops &= ~blocked
        tops[~rj, :] = False
        tops[:, ~ci] = False
        # street trees: a top over a road corridor (its crown overhangs the street) moves just out of it,
        # onto the verge / sidewalk, unless that lands on a building, water or another road
        on_road = tops & road
        tops &= ~road
        rj_, ri_ = np.nonzero(on_road)
        if len(rj_):
            nc = np.rint(ri_ + push[0, rj_, ri_]).astype(np.int64)
            nr = np.rint(rj_ + push[1, rj_, ri_]).astype(np.int64)
            ok = (nc >= 0) & (nc < n_c) & (nr >= 0) & (nr < n_r)
            ok[ok] &= ~blocked[nr[ok], nc[ok]] & ~road[nr[ok], nc[ok]]
            moved = np.zeros(h.shape, bool)
            moved[nr[ok], nc[ok]] = True
            moved[~rj, :] = False
            moved[:, ~ci] = False
            hs = hs.copy()
            hs[nr[ok], nc[ok]] = np.maximum(hs[nr[ok], nc[ok]], hs[rj_[ok], ri_[ok]])     # keep its height
            tops |= moved
        blocked |= road
        jj, ii = np.nonzero(tops)
        jj, ii = _thin(jj, ii, hs[jj, ii], chm.dx)     # one top per crown (flat crowns had several)
        th = hs[jj, ii]
        tlon, tlat = _to4326(mx[ii], my[jj])
        is_tree = th >= TREE_MIN_M
        # the landcover class at each top (rocks / bushes scatter below)
        lcls = lc.sample(tlon, tlat) if lc is not None else np.zeros(len(tlon), np.uint8)
        is_tree &= lcls != WC["water"]
        # hashed choices keyed on the global pixel (z17 Web-Mercator index: the CHM grid)
        gx = np.floor((mx[ii] + 20037508.342789244) / chm.dx).astype(np.int64)
        gy = np.floor((20037508.342789244 - my[jj]) / chm.dy).astype(np.int64)
        u1, u2, u3, u4 = (_hash01(gx, gy, s) for s in (1, 2, 3, 4))
        conifer = u1 < _conifer_share(tlat, tlon) * np.clip(th / 12.0, 0.5, 1.2)
        group = np.where(is_tree, np.where(conifer, G_CONIFER, G_DECIDUOUS), G_SHRUB).astype(np.uint8)
        group = regional(group, th, tlat, tlon, lcls, _hash01(gx, gy, 7))
        kind = np.where(is_tree, KIND_TREE, KIND_BUSH).astype(np.uint8)
        jitter = chm.dx * 0.5
        tlon2, tlat2 = _to4326(mx[ii] + (u3 - 0.5) * jitter, my[jj] + (u4 - 0.5) * jitter)
        tree = Items(tlon2, tlat2, th.astype(np.float32), (0.12 * th + 1.0).astype(np.float32), kind, group,
                     _hash01(gx, gy, 5).astype(np.float32), (u2 * 360.0).astype(np.float32),
                     (0.85 + 0.3 * _hash01(gx, gy, 6)).astype(np.float32))
        scatter = self._scatter(cd, lc, blocked, chm, hs)
        return _concat(tree, scatter)

    def _scatter(self, cd, lc, blocked, chm, hs):
        """Bushes and rocks on a hashed SCATTER_CELL_M grid over shrubland, grass and bare ground."""
        if lc is None:
            return _empty()
        lon0, lat0, lon1, lat1 = cd.bounds
        ex0, ey0 = _to3857(lon0, lat0)
        ex1, ey1 = _to3857(lon1, lat1)
        s = SCATTER_CELL_M
        gi = np.arange(math.floor(ex0 / s), math.ceil(ex1 / s))
        gj = np.arange(math.floor(ey0 / s), math.ceil(ey1 / s))
        GI, GJ = np.meshgrid(gi, gj)
        GI, GJ = GI.ravel(), GJ.ravel()
        px = (GI + _hash01(GI, GJ, 11)) * s
        py = (GJ + _hash01(GI, GJ, 12)) * s
        keep = (px >= ex0) & (px < ex1) & (py >= ey0) & (py < ey1)
        px, py, GI, GJ = px[keep], py[keep], GI[keep], GJ[keep]
        lon, lat = _to4326(px, py)
        cls = lc.sample(lon, lat)
        u = _hash01(GI, GJ, 13)
        pb = np.zeros(len(px))
        pr = np.zeros(len(px))
        for c, (b, r) in SCATTER_P.items():
            pb = np.where(cls == c, b, pb)
            pr = np.where(cls == c, r, pr)
        # on the CHM grid: not blocked, no canopy there already (the trees are the CHM's)
        c_ = np.floor((px - chm.x0) / chm.dx).astype(np.int64)
        r_ = np.floor((chm.y0 - py) / chm.dy).astype(np.int64)
        ok = (c_ >= 0) & (c_ < hs.shape[1]) & (r_ >= 0) & (r_ < hs.shape[0])
        free = np.zeros(len(px), bool)
        free[ok] = ~blocked[r_[ok], c_[ok]] & (hs[r_[ok], c_[ok]] < BUSH_MIN_M)
        rock = free & (u < pr)
        bush = free & ~rock & (u < pr + pb)
        sel = rock | bush
        n = int(sel.sum())
        size = 0.4 + 1.6 * _hash01(GI[sel], GJ[sel], 14) ** 2
        return Items(lon[sel], lat[sel], size.astype(np.float32), (size * 0.6).astype(np.float32),
                     np.where(rock[sel], KIND_ROCK, KIND_BUSH).astype(np.uint8),
                     regional(np.where(rock[sel], G_ROCK, G_SHRUB), size, lat[sel], lon[sel], cls[sel],
                              _hash01(GI[sel], GJ[sel], 18)),
                     _hash01(GI[sel], GJ[sel], 15).astype(np.float32),
                     (_hash01(GI[sel], GJ[sel], 16) * 360.0).astype(np.float32),
                     (0.8 + 0.4 * _hash01(GI[sel], GJ[sel], 17)).astype(np.float32)) if n else _empty()


def _thin(jj, ii, h, px):
    """Non-maximum suppression: the tallest tops first, each removing the others within its crown
    radius (0.12 h + 1 m, times CROWN_SPACING). Deterministic (ties broken by position)."""
    if len(jj) == 0:
        return jj, ii
    order = np.lexsort((ii, jj, -h))
    rad = (CROWN_SPACING * (0.12 * h + 1.0) / px)
    cell = max(1, int(np.ceil(rad.max())))
    grid = {}
    keep = np.zeros(len(jj), bool)
    for k in order:
        j, i, r = jj[k], ii[k], rad[k]
        gj, gi = j // cell, i // cell
        ok = True
        for a in (gj - 1, gj, gj + 1):
            for b in (gi - 1, gi, gi + 1):
                for m in grid.get((a, b), ()):
                    if (jj[m] - j) ** 2 + (ii[m] - i) ** 2 < max(r, rad[m]) ** 2:
                        ok = False
                        break
                if not ok:
                    break
            if not ok:
                break
        if ok:
            keep[k] = True
            grid.setdefault((gj, gi), []).append(k)
    return jj[keep], ii[keep]


def _empty():
    z = np.zeros(0)
    return Items(z, z, z.astype(np.float32), z.astype(np.float32), z.astype(np.uint8), z.astype(np.uint8),
                 z.astype(np.float32), z.astype(np.float32), z.astype(np.float32))


def _concat(a, b):
    return Items(*(np.concatenate([getattr(a, f), getattr(b, f)]) for f in
                   ("lon", "lat", "height", "radius", "kind", "group", "rank", "yaw", "scale")))


R = 6378137.0


def _to3857(lon, lat):
    lon = np.asarray(lon, np.float64)
    lat = np.clip(np.asarray(lat, np.float64), -85.0511287798066, 85.0511287798066)
    return np.radians(lon) * R, np.log(np.tan(np.pi / 4 + np.radians(lat) / 2)) * R


def _to4326(x, y):
    x = np.asarray(x, np.float64)
    y = np.asarray(y, np.float64)
    return np.degrees(x / R), np.degrees(2 * np.arctan(np.exp(y / R)) - np.pi / 2)


def _grid(chm, mx, my):
    """Fractional (col, row) of EPSG:3857 points on the CHM grid."""
    return (np.asarray(mx) - chm.x0) / chm.dx, (chm.y0 - np.asarray(my)) / chm.dy


def _mask_polys(f, mask, chm, grow_m, kind=3):
    """Mark the polygons of Features f (buildings, water) on the CHM-grid mask, grown by grow_m."""
    if f is None:
        return
    n_r, n_c = mask.shape
    g = grow_m / chm.dx
    for i in np.flatnonzero(f.kind == kind):
        for part in f.parts(i):
            rings = []
            for ring in part:
                x, y = _to3857(ring[:, 0], ring[:, 1])
                rings.append(np.stack(_grid(chm, x, y), -1))
            allp = np.concatenate(rings)
            c0, r0 = int(np.floor(allp[:, 0].min() - g)), int(np.floor(allp[:, 1].min() - g))
            c1, r1 = int(np.ceil(allp[:, 0].max() + g)) + 1, int(np.ceil(allp[:, 1].max() + g)) + 1
            c0, r0, c1, r1 = max(c0, 0), max(r0, 0), min(c1, n_c), min(r1, n_r)
            if c1 <= c0 or r1 <= r0:
                continue
            gc, gr = np.meshgrid(np.arange(c0, c1) + 0.5, np.arange(r0, r1) + 0.5)
            pts = np.stack([gc.ravel(), gr.ravel()], -1)
            inside = np.zeros(len(pts), bool)
            for r in rings:
                inside ^= Path(r).contains_points(pts)
            mask[r0:r1, c0:c1] |= inside.reshape(r1 - r0, c1 - c0)


def _mask_roads(f, mask, chm, push=None):
    """Mark road corridors (half width by class + ROAD_CLEAR_M) on the CHM-grid mask. push (optional, (3, rows,
    cols) float32: dx, dy in px and the distance to the nearest road, inf where none) gets, per corridor
    pixel, the move that takes it just out of its nearest road's corridor (street trees: vegetation._build)."""
    if f is None:
        return
    n_r, n_c = mask.shape
    cls = f.attrs.get("class", np.full(len(f.kind), "", object))
    for i in np.flatnonzero(f.kind == 2):
        hw_m = ROAD_HALF_WIDTH_M.get(str(cls[i]), ROAD_HALF_DEFAULT_M) + ROAD_CLEAR_M
        for part in f.parts(i):
            for ring in part:
                # ground metres to Web-Mercator pixels (a 3857 metre is 1 / cos(lat) of a ground metre)
                hw = hw_m / (chm.dx * max(math.cos(math.radians(float(np.mean(ring[:, 1])))), 0.05))
                x, y = _to3857(ring[:, 0], ring[:, 1])
                c, r = _grid(chm, x, y)
                q = np.stack([c, r], -1)
                if (q[:, 0].max() < -hw or q[:, 1].max() < -hw or q[:, 0].min() > n_c + hw
                        or q[:, 1].min() > n_r + hw):
                    continue
                for a, b in zip(q[:-1], q[1:]):
                    c0, r0 = max(int(min(a[0], b[0]) - hw), 0), max(int(min(a[1], b[1]) - hw), 0)
                    c1, r1 = min(int(max(a[0], b[0]) + hw) + 2, n_c), min(int(max(a[1], b[1]) + hw) + 2, n_r)
                    if c1 <= c0 or r1 <= r0:
                        continue
                    gc, gr = np.meshgrid(np.arange(c0, c1) + 0.5, np.arange(r0, r1) + 0.5)
                    ab = b - a
                    L2 = float(ab @ ab) or 1e-9
                    t = np.clip(((gc - a[0]) * ab[0] + (gr - a[1]) * ab[1]) / L2, 0, 1)
                    ox, oy = gc - (a[0] + t * ab[0]), gr - (a[1] + t * ab[1])
                    d = np.hypot(ox, oy)
                    mask[r0:r1, c0:c1] |= d <= hw
                    if push is not None:
                        win = push[:, r0:r1, c0:c1]
                        closer = (d <= hw) & (d < win[2])
                        if closer.any():
                            # away from the centreline (sideways if on it: the segment's normal)
                            nx, ny = -ab[1], ab[0]
                            nl = math.hypot(nx, ny) or 1.0
                            ux = np.where(d > 1e-3, ox / np.maximum(d, 1e-3), nx / nl)
                            uy = np.where(d > 1e-3, oy / np.maximum(d, 1e-3), ny / nl)
                            go = hw + 0.6 - d
                            win[0][closer] = (ux * go)[closer]
                            win[1][closer] = (uy * go)[closer]
                            win[2][closer] = d[closer]


# --- tiles for the mod --------------------------------------------------------------------------------
TILE = 256.0                # m, as the road tiles (roads.tile_bbox)
SEND_M = 1280.0             # tiles this close to the player (Chebyshev) are sent ...
DROP_M = 1536.0             # ... and dropped beyond this
BUILD_PER_POSE = 8


def tile_items(frame, tx, ty, veg, cell_ready, elev, sea=None):
    """The items of anchor tile (tx, ty), each [group, x, y, z, yaw, scale, height, rank] in the anchor
    frame, the ground z from elev(lat, lon) (the DEM shaped as the block's ground: runways, carved
    water). None when a cell under it is not at hand yet (veg cells are built in the background)."""
    import roads as roadsmod
    x0, y0, x1, y1 = roadsmod.tile_bbox(tx, ty)
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x1, x0, x1]), np.array([y0, y0, y1, y1]), np.zeros(4))
    keys = mapcells.cells_for_bbox(float(la.min()), float(lo.min()), float(la.max()), float(lo.max()))
    parts = []
    for k in keys:
        if not cell_ready(k):
            return None
        parts.append(veg.cell(k))
    items = parts[0] if len(parts) == 1 else _concat_all(parts)
    if len(items) == 0:
        return []
    e, n, _ = frame.geodetic_to_enu(items.lat, items.lon, np.zeros(len(items)))
    e, n = np.asarray(e), np.asarray(n)
    sel = (e >= x0) & (e < x1) & (n >= y0) & (n < y1)
    if not sel.any():
        return []
    it = items.take(sel)
    e, n = e[sel], n[sel]
    if sea is not None:                 # nothing in the water (lakes, rivers, sea: their carved beds)
        dry = sea.water(it.lat, it.lon)[0] <= 0
        it, e, n = it.take(dry), e[dry], n[dry]
    if len(it) == 0:
        return []
    zg = np.asarray(elev(it.lat, it.lon), np.float64)
    _, _, z = frame.geodetic_to_enu(it.lat, it.lon, zg)
    return [[int(g), round(float(a), 2), round(float(b), 2), round(float(c), 2), round(float(yw), 1),
             round(float(sc), 3), round(float(h), 1), round(float(r), 4)]
            for g, a, b, c, yw, sc, h, r in zip(it.group, e, n, np.asarray(z), it.yaw, it.scale, it.height, it.rank)]


def _concat_all(parts):
    out = parts[0]
    for p in parts[1:]:
        out = _concat(out, p)
    return out


def write_tile(out_dir, vfs_dir, aid, tx, ty, rows):
    import json
    name = f"veg_{aid}_{tx}_{ty}.json"
    path = os.path.join(out_dir, name)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"groups": list(GROUPS), "items": rows}, f, separators=(",", ":"))
    os.replace(tmp, path)
    return f"{vfs_dir}/{name}", len(rows)


class Tracker:
    """Per connection: which tiles of a kind the mod has (as roads.Tracker): vegetation tiles by default
    (messages veg_tile / veg_drop); the far buildings use one too (buildings.FAR_*)."""

    def __init__(self, send_m=SEND_M, drop_m=DROP_M, per_pose=BUILD_PER_POSE, kind="veg"):
        self.aid = None
        self.sent = set()
        self.building = set()
        self.send_m, self.drop_m, self.per_pose, self.kind = send_m, drop_m, per_pose, kind

    def on_pose(self, conn, aid, x, y, loop, build):
        import roads as roadsmod
        if aid != self.aid:
            self.aid, self.sent, self.building = aid, set(), set()
        want = [k for k in roadsmod.tiles_near(x, y, self.send_m) if k not in self.sent and k not in self.building]
        want.sort(key=lambda k: roadsmod.tile_distance(k[0], k[1], x, y))
        for tx, ty in want[:self.per_pose]:
            self.building.add((tx, ty))
            fut = loop.run_in_executor(None, build, aid, tx, ty)
            fut.add_done_callback(lambda f, k=(tx, ty), a=aid: self._built(conn, a, k, f))
        for k in [k for k in self.sent if roadsmod.tile_distance(k[0], k[1], x, y) > self.drop_m]:
            self.sent.discard(k)
            conn.send({"type": f"{self.kind}_drop", "anchorId": aid, "key": f"{k[0]}_{k[1]}"})

    def _built(self, conn, aid, key, fut):
        self.building.discard(key)
        if aid != self.aid:
            return
        try:
            res = fut.result()
        except Exception as e:
            log.debug("%s tile %s: %s: %s", self.kind, key, type(e).__name__, e)
            return
        if res is None:                 # its cells not at hand yet: asked again on a later pose
            return
        path, n = res
        self.sent.add(key)
        conn.send({"type": f"{self.kind}_tile", "anchorId": aid, "key": f"{key[0]}_{key[1]}", "path": path, "n": n})
