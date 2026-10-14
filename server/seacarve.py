"""Sea water: the ground carved under the sea, and land kept above the mod's sea plane (user 2026-10-13:
sea and bays first, roughly real depths, cars sink; docs/research/2026-10-13-water-probe.md).

    sc = SeaCarver(md, cache_dir)                 # md: mapdata.MapData
    z = sc.carve(lat, lon, z_dem)                 # the DEM with the sea floor carved in
    src = SeaDem(dem, sc)                         # a dem.Dem-like sampler (carver None: sink only)
    sc.sea_at(dem, lat, lon, size)                # which points of a water tile are sea (seatiles.py)

The sea is Overture's `ocean` polygons (OSM's coastline: open sea, bays, tidal rivers). Per map-data
cell (mapcells z14) it is rasterised on the z16 Web-Mercator pixel grid (~1.9 m at 39 N) with a margin
from the neighbouring cells, and the floor depth grows from the shoreline at DEPTH_SLOPE to DEPTH_CAP
(no bathymetry yet: GEBCO / NOAA CUDEM later). A point's depth is the bilinear sample of that raster,
so the floor is continuous across the shoreline and across cells; a point's carved height depends
only on its position and the map data (deterministic, the same in every process).

The mod draws the sea as WaterBlock tiles that follow the Earth's curve (seatiles.py). Beyond the
carved levels the far tiles' DEM sea (SEA_LO..SEA_HI) is sunk SINK_M so it never shows through them.
"""
import logging
import math
import os
import threading
from collections import OrderedDict

import numpy as np
from matplotlib.path import Path

import mapcells

log = logging.getLogger("earthd.seacarve")

VERSION = 3
SEA_E = 0.0                 # m: the sea surface (the DEMs' heights are above mean sea level)
DEPTH_SLOPE = 0.15          # floor depth per metre from the shoreline (1 : 6.7)
DEPTH_CAP = 6.0             # m: the floor's depth away from the shore (Chesapeake mean ~6.4 m)
PX_ZOOM = 16                # raster pixels: Web-Mercator z16 (256 px tiles): 1024 px per z14 cell
CELL_PX = 256 << (PX_ZOOM - mapcells.ZOOM)      # raster pixels per cell edge (1024)
PAD_PX = 32                 # margin rasterised from the neighbouring cells (> the cap's distance)
SEA_SUBTYPES = ("ocean",)
SEA_LO, SEA_HI = -0.5, 0.3  # m: DEM heights in this band are the sea where no polygons are at hand (far
                            # tiles, the large water tiles): the DEMs' sea surfaces are flat at ~0
SINK_M = 1.5                # m: such ground sunk below the surface (no flicker through the water tiles)
POLY_TILE_MAX = 1024.0      # water tiles up to this size are placed from the polygons, larger ones by
                            # the DEM band (map data is fetched near the player only)
CARVE_MAX_LEVEL = 2         # far tiles up to this level are carved (their map data is near the player:
                            # L2 reaches ~3 km, past the lakes' water tiles, seatiles.LAKE_RADIUS_M);
                            # coarser ones have the DEM's sea sunk (sink)
LAKE_SUBTYPES = ("lake", "reservoir", "pond", "water")
RIVER_SUBTYPES = ("river", "stream")  # polygons whose level follows the DEM, pixel by pixel (canals with
                                      # locks break into many tiny tiles: later, as River objects)
RIVER_SKIP_CLASSES = ("drain", "ditch")
RIVER_DEPTH_CAP = 3.0       # m
RIVER_DEM_STEP = 4          # river levels: the DEM every 4th pixel (~7.5 m), the lowest of 3 x 3 such
                            # samples (the flattened surface, not a bank)
LAKE_SKIP_CLASSES = ("fountain", "swimming_pool", "wastewater", "basin", "drain", "fish_pass")
LAKE_DEPTH_CAP = 4.0        # m: lakes and ponds (no bathymetry); the slope is DEPTH_SLOPE
LAKE_MIN_PX = 100           # a lake smaller than this in the 3 x 3 cells' raster (~350 m^2) is left out
LAKE_FLAT_M = 1.5           # a "lake" whose DEM inside spans more (p10..p90) is not flat (a river): out
LAKE_DEM_ZOOM = 15          # its level: the median DEM inside, at this zoom (US DEMs flatten lakes)
LEVEL_Q = 0.1               # m: lake levels quantised (the same in every cell for a flattened lake)
LEVEL_SAMPLES = 4000
LEVEL_PAD_PX = 2            # the level raster spread this far onto the shore (lookups beside the water)
MEM_CELLS = 24              # rasters kept per process
EARTH_R = 6371000.0


def _merc(lat, lon, z=PX_ZOOM):
    """Fractional Web-Mercator pixel coordinates (256 px tiles) at zoom z."""
    lat = np.clip(np.asarray(lat, np.float64), -mapcells.MAX_LAT, mapcells.MAX_LAT)
    n = 256.0 * (1 << z)
    x = (np.asarray(lon, np.float64) + 180.0) / 360.0 * n
    s = np.sin(np.radians(lat))
    y = (0.5 - np.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
    return x, y


def _px_m(lat):
    return 2 * math.pi * 6378137.0 / (256 << PX_ZOOM) * math.cos(math.radians(lat))


def shore_steps(water, kmax):
    """For each True pixel, the number of erosion steps (alternating 4- and 8-neighbour, an octagonal
    approximation of the euclidean distance) until it is reached from a False pixel, capped at kmax;
    0 on False pixels."""
    out = np.zeros(water.shape, np.int16)
    cur = water.copy()
    for k in range(1, kmax + 1):
        if not cur.any():
            break
        p = np.pad(cur, 1, constant_values=True)    # outside the raster: as its edge (padded margin)
        nxt = cur & p[:-2, 1:-1] & p[2:, 1:-1] & p[1:-1, :-2] & p[1:-1, 2:]
        if k % 2 == 0:
            nxt &= p[:-2, :-2] & p[:-2, 2:] & p[2:, :-2] & p[2:, 2:]
        out[cur & ~nxt] = k
        cur = nxt
    out[cur] = kmax
    return out


def _lonlat(mx, my, z=PX_ZOOM):
    """The inverse of _merc."""
    n = 256.0 * (1 << z)
    lon = np.asarray(mx, np.float64) / n * 360.0 - 180.0
    lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * np.asarray(my, np.float64) / n))))
    return lat, lon


class SeaCarver:
    """The water of the map-data cells near the player: the sea (level SEA_E) and lakes (each at its own
    level, measured from the DEM), with their floors carved from the shore."""

    def __init__(self, md, cache_dir=None, dem=None):
        self.md = md
        self.dem = dem                  # dem.Dem for the lakes' levels (None: no lakes)
        self.dir = os.path.join(cache_dir, "seacarve", f"v{VERSION}") if cache_dir else None
        self._mem = OrderedDict()       # (x, y) -> (depth, level) rasters or None
        self._lock = threading.Lock()
        self._cell_locks = {}

    # --- rasters --------------------------------------------------------------------------------
    def _water(self, key):
        try:
            return self.md.cell(key, layers=("water",)).layers.get("water")
        except Exception as e:          # offline / fetch refused: no water there for now
            log.warning("sea: no water data for %s (%s)", key, e)
            return None

    def raster(self, x, y):
        """(depth, level) of cell (x, y): (CELL_PX + 2)² float32 each, pixel [j, i] centred on z16 pixel
        (x * CELL_PX - 1 + i + 0.5, y * CELL_PX - 1 + j + 0.5); level NaN away from water (spread
        LEVEL_PAD_PX onto the shore, for the lookups next to it); None when the cell has no water."""
        k = (x, y)
        with self._lock:
            if k in self._mem:
                self._mem.move_to_end(k)
                return self._mem[k]
            lock = self._cell_locks.setdefault(k, threading.Lock())
        with lock:
            with self._lock:
                if k in self._mem:
                    return self._mem[k]
            r = self._load(x, y)
            with self._lock:
                self._mem[k] = r
                while len(self._mem) > MEM_CELLS:
                    self._mem.popitem(last=False)
            return r

    def _path(self, x, y):
        rel = getattr(self.md, "release", "x")
        lakes = "lakes" if self.dem is not None else "sea"
        return os.path.join(self.dir, str(rel), lakes, str(x), f"{y}.npz") if self.dir else None

    def _load(self, x, y):
        p = self._path(x, y)
        if p and os.path.exists(p):
            with np.load(p, allow_pickle=False) as f:
                if not f["any"]:
                    return None
                return f["depth"].astype(np.float32) / 20.0, f["level"].astype(np.float32)
        r = self._build(x, y)
        if r is not None:
            r = (np.round(r[0] * 20.0).astype(np.float32) / 20.0, r[1])     # the values the file gives back
        if p:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "wb") as fh:
                if r is None:
                    np.savez_compressed(fh, any=False)
                else:
                    np.savez_compressed(fh, any=True, depth=np.round(r[0] * 20.0).astype(np.uint8), level=r[1])
            os.replace(tmp, p)
        return r

    def _build(self, x, y):
        own = self._water((mapcells.ZOOM, x, y))
        if own is None or not (np.isin(own.attrs["subtype"], SEA_SUBTYPES).any()
                               or (self.dem is not None and (self._lake_rows(own).size
                                                             or self._river_rows(own).size))):
            return None                 # no water in the cell itself: nothing to carve here
        n = CELL_PX + 2 * PAD_PX
        ox, oy = x * CELL_PX - PAD_PX, y * CELL_PX - PAD_PX      # z16 pixel of the raster's corner
        sea = np.zeros((n, n), bool)
        river = np.zeros((n, n), bool)
        lake = np.full((n, n), -1, np.int32)
        lake_ids = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                f = own if (dx, dy) == (0, 0) else self._water((mapcells.ZOOM, x + dx, y + dy))
                if f is None:
                    continue
                for i in np.flatnonzero(np.isin(f.attrs["subtype"], SEA_SUBTYPES) & (f.kind == 3)):
                    self._rasterise(f, i, sea, ox, oy)
                if self.dem is not None:
                    for i in self._river_rows(f):
                        self._rasterise(f, i, river, ox, oy)
                    for i in self._lake_rows(f):
                        gid = str(f.ids[i])
                        if gid not in lake_ids:
                            lake_ids.append(gid)
                        m = np.zeros((n, n), bool)
                        self._rasterise(f, i, m, ox, oy)
                        lake[m] = lake_ids.index(gid)
        lat_c = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 0.5) / (1 << mapcells.ZOOM)))))
        px = _px_m(lat_c)
        level = np.full((n, n), np.nan, np.float32)
        depth = np.zeros((n, n), np.float32)
        lake[sea] = -1                  # the sea wins where they overlap
        river &= ~sea & (lake < 0)
        for k in range(len(lake_ids)):
            m = lake == k
            lv = self._lake_level(m, ox, oy, px)
            if lv is None:
                continue
            if lv <= SEA_HI:            # a lagoon, a tidal pond: part of the sea
                sea |= m
                continue
            kmax = int(math.ceil(LAKE_DEPTH_CAP / DEPTH_SLOPE / px)) + 2
            st = shore_steps(m, kmax).astype(np.float32)
            depth[m] = np.minimum(LAKE_DEPTH_CAP, DEPTH_SLOPE * (st[m] - 0.5) * px)
            level[m] = lv
        if river.any():
            rl = self._river_levels(river, ox, oy)
            if rl is not None:
                tidal = river & (rl <= SEA_HI)
                sea |= tidal            # a tidal river: the sea
                river &= ~tidal & np.isfinite(rl)
                kmax = int(math.ceil(RIVER_DEPTH_CAP / DEPTH_SLOPE / px)) + 2
                st = shore_steps(river, kmax).astype(np.float32)
                depth[river] = np.minimum(RIVER_DEPTH_CAP, DEPTH_SLOPE * (st[river] - 0.5) * px)
                level[river] = rl[river]
        if sea.any():
            kmax = int(math.ceil(DEPTH_CAP / DEPTH_SLOPE / px)) + 2
            st = shore_steps(sea, kmax).astype(np.float32)
            depth[sea] = np.minimum(DEPTH_CAP, DEPTH_SLOPE * (st[sea] - 0.5) * px)
            level[sea] = SEA_E
        if not np.isfinite(level[PAD_PX:-PAD_PX, PAD_PX:-PAD_PX]).any():
            return None
        for _ in range(LEVEL_PAD_PX):   # spread the levels onto the shore (bilinear lookups beside it)
            p = np.pad(level, 1, constant_values=np.nan)
            nb = np.stack([p[:-2, 1:-1], p[2:, 1:-1], p[1:-1, :-2], p[1:-1, 2:]])
            with np.errstate(all="ignore"):
                fill = np.nanmax(np.where(np.isfinite(nb), nb, -np.inf), axis=0)
            level = np.where(np.isnan(level) & np.isfinite(fill), fill, level).astype(np.float32)
        c = slice(PAD_PX - 1, PAD_PX + CELL_PX + 1)
        return depth[c, c].copy(), level[c, c].copy()

    @staticmethod
    def _river_rows(f):
        sub = f.attrs["subtype"]
        cls = f.attrs.get("class", np.full(len(sub), "", object))
        inter = f.attrs.get("is_intermittent", np.zeros(len(sub), bool))
        ok = np.isin(sub, RIVER_SUBTYPES) & ~np.isin(cls, RIVER_SKIP_CLASSES) & (f.kind == 3)
        return np.flatnonzero(ok & ~np.asarray(inter, bool))

    def _river_levels(self, m, ox, oy):
        """The water level of every river pixel (m): the DEM every RIVER_DEM_STEP pixels, the lowest of
        the 3 x 3 samples around (the flattened surface rather than a bank), quantised to LEVEL_Q;
        NaN elsewhere. None without a DEM."""
        jj, ii = np.nonzero(m)
        k = RIVER_DEM_STEP
        j0, j1 = jj.min() // k, jj.max() // k + 1
        i0, i1 = ii.min() // k, ii.max() // k + 1
        gj, gi = np.meshgrid(np.arange(j0, j1 + 1), np.arange(i0, i1 + 1), indexing="ij")
        lat, lon = _lonlat(ox + gi * k + k / 2, oy + gj * k + k / 2)
        try:
            e = np.asarray(self.dem.sample(lat.ravel(), lon.ravel(), LAKE_DEM_ZOOM), np.float64).reshape(gj.shape)
        except Exception as ex:
            log.debug("river levels: %s", ex)
            return None
        p = np.pad(e, 1, mode="edge")
        lo = np.min(np.stack([p[a:a + e.shape[0], b:b + e.shape[1]] for a in range(3) for b in range(3)]), axis=0)
        out = np.full(m.shape, np.nan, np.float32)
        out[jj, ii] = np.round(lo[jj // k - j0, ii // k - i0] / LEVEL_Q) * LEVEL_Q
        return out

    @staticmethod
    def _lake_rows(f):
        sub = f.attrs["subtype"]
        cls = f.attrs.get("class", np.full(len(sub), "", object))
        inter = f.attrs.get("is_intermittent", np.zeros(len(sub), bool))
        name = f.attrs.get("name", np.full(len(sub), "", object))
        fountain = np.array(["fountain" in str(v).lower() for v in name], bool)
        ok = np.isin(sub, LAKE_SUBTYPES) & ~np.isin(cls, LAKE_SKIP_CLASSES) & (f.kind == 3) & ~fountain
        return np.flatnonzero(ok & ~np.asarray(inter, bool))

    def _lake_level(self, m, ox, oy, px):
        """The water level of a lake (its pixels m): the median DEM height inside, away from the banks,
        quantised to LEVEL_Q (SEA_E at sea level: it joins the sea); None if it is too small or not flat
        (a river, bad data)."""
        cnt = int(m.sum())
        if cnt < LAKE_MIN_PX:
            return None
        inner = shore_steps(m, 3) >= 3
        jj, ii = np.nonzero(inner if inner.sum() >= 20 else m)
        if len(jj) > LEVEL_SAMPLES:
            sel = np.linspace(0, len(jj) - 1, LEVEL_SAMPLES).astype(np.int64)   # deterministic subsample
            jj, ii = jj[sel], ii[sel]
        lat, lon = _lonlat(ox + ii + 0.5, oy + jj + 0.5)
        try:
            e = np.asarray(self.dem.sample(lat, lon, LAKE_DEM_ZOOM), np.float64)
        except Exception as ex:         # no DEM there yet: no lake for now
            log.debug("lake level: %s", ex)
            return None
        p10, p50, p90 = np.percentile(e, [10, 50, 90])
        if p90 - p10 > LAKE_FLAT_M:
            return None
        return SEA_E if p50 <= SEA_HI else float(round(p50 / LEVEL_Q) * LEVEL_Q)

    @staticmethod
    def _rasterise(f, i, water, ox, oy):
        n = water.shape[0]
        for part in f.parts(i):
            rings = []
            for ring in part:
                mx, my = _merc(ring[:, 1], ring[:, 0])
                rings.append(np.stack([mx - ox, my - oy], -1))
            allp = np.concatenate(rings)
            i0, j0 = max(0, int(np.floor(allp[:, 0].min()))), max(0, int(np.floor(allp[:, 1].min())))
            i1, j1 = min(n, int(np.ceil(allp[:, 0].max())) + 1), min(n, int(np.ceil(allp[:, 1].max())) + 1)
            if i1 <= i0 or j1 <= j0:
                continue
            gi, gj = np.meshgrid(np.arange(i0, i1) + 0.5, np.arange(j0, j1) + 0.5)
            pts = np.stack([gi.ravel(), gj.ravel()], -1)
            inside = np.zeros(len(pts), bool)
            for r in rings:             # even-odd: holes (islands) cut out
                inside ^= Path(r).contains_points(pts)
            water[j0:j1, i0:i1] |= inside.reshape(j1 - j0, i1 - i0)

    # --- queries --------------------------------------------------------------------------------
    def water(self, lat, lon):
        """(floor depth below the surface, the surface's level) at the points: depth 0 and level NaN on
        land; the depth bilinear (continuous across the shore), the level of the nearest pixel."""
        lat = np.asarray(lat, np.float64)
        lon = np.asarray(lon, np.float64)
        d = np.zeros(lat.shape, np.float32)
        lv = np.full(lat.shape, np.nan, np.float32)
        if lat.size == 0:
            return d, lv
        mx, my = _merc(lat, lon)
        cx = np.floor(mx / CELL_PX).astype(np.int64)
        cy = np.floor(my / CELL_PX).astype(np.int64)
        keys = np.unique(np.stack([cx.ravel(), cy.ravel()], -1), axis=0)
        for kx, ky in keys:
            r = self.raster(int(kx), int(ky))
            if r is None:
                continue
            rd, rl = r
            sel = (cx == kx) & (cy == ky)
            fx = mx[sel] - kx * CELL_PX + 0.5      # raster coords: pixel centres at i + 0.5 - 1
            fy = my[sel] - ky * CELL_PX + 0.5
            i0 = np.clip(np.floor(fx).astype(np.int64), 0, rd.shape[1] - 2)
            j0 = np.clip(np.floor(fy).astype(np.int64), 0, rd.shape[0] - 2)
            tx, ty = np.clip(fx - i0, 0, 1), np.clip(fy - j0, 0, 1)
            d[sel] = ((rd[j0, i0] * (1 - tx) + rd[j0, i0 + 1] * tx) * (1 - ty)
                      + (rd[j0 + 1, i0] * (1 - tx) + rd[j0 + 1, i0 + 1] * tx) * ty)
            ni = np.clip(np.floor(fx + 0.5).astype(np.int64), 0, rl.shape[1] - 1)
            nj = np.clip(np.floor(fy + 0.5).astype(np.int64), 0, rl.shape[0] - 1)
            lv[sel] = rl[nj, ni]
        return d, lv

    def depth(self, lat, lon):
        """Floor depth below the water surface at the points (0 on land and away from water)."""
        return self.water(lat, lon)[0]

    def carve(self, lat, lon, z):
        """z (DEM heights at the points) with the sea's and the lakes' floors carved in."""
        d, lv = self.water(lat, lon)
        wet = (d > 0) & np.isfinite(lv)
        if not wet.any():
            return z
        z = np.asarray(z, np.float64)
        return np.where(wet, np.minimum(z, np.where(wet, lv, 0.0) - d), z)

    def lake_at(self, lat, lon):
        """The level of the inland water (lakes, rivers above sea level) at the points (NaN on land, at
        sea and away from water)."""
        d, lv = self.water(lat, lon)
        return np.where((d > 0) & (lv > SEA_HI), lv, np.nan)

    def near(self, lat, lon, radius_m, step_m=250.0):
        """Any sea within radius_m of (lat, lon) (sampled on a step_m grid)."""
        k = int(radius_m // step_m)
        o = np.arange(-k, k + 1) * step_m
        E, N = np.meshgrid(o, o)
        inside = np.hypot(E, N) <= radius_m
        la = lat + N[inside] / 111320.0
        lo = lon + E[inside] / (111320.0 * math.cos(math.radians(lat)))
        d, lv = self.water(la, lo)
        return bool(((d > 0) & (np.abs(lv - SEA_E) < 0.01)).any())

    def sea_at(self, dem, lat, lon, size):
        """Which points of a water tile of `size` m are sea (seatiles.plan): the polygons for the small
        tiles near the player, the DEM band for larger ones."""
        if size <= POLY_TILE_MAX:
            d, lv = self.water(lat, lon)
            return (d > 0) & (np.abs(lv - SEA_E) < 0.01)
        import dem as demmod
        lat = np.asarray(lat, np.float64)
        z = demmod.zoom_for(float(np.mean(lat)), size / 16.0)
        z = min(max(z, getattr(demmod, "MIN_ZOOM", 0)), demmod.MAX_ZOOM)
        e = np.asarray(dem.sample(lat, np.asarray(lon, np.float64), z), np.float64)
        return (e >= SEA_LO) & (e <= SEA_HI)


def sink(e):
    """Far-tile elevations with the DEM's sea (SEA_LO..SEA_HI m) sunk SINK_M below the surface, so the
    far tiles never show through the mod's water tiles (levels without the carved polygons)."""
    e = np.asarray(e, np.float64)
    return np.where((e >= SEA_LO) & (e <= SEA_HI), np.minimum(e, SEA_E - SINK_M), e)


class SeaDem:
    """A dem.Dem-like sampler with the sea floor carved in (carver) or, for the far levels without map
    data (carver None), the DEM's sea sunk (sink)."""

    def __init__(self, dem, carver=None):
        self.dem, self.carver = dem, carver

    def sample(self, lat, lon, zoom, progress=None):
        z = self.dem.sample(lat, lon, zoom, progress) if progress is not None else self.dem.sample(lat, lon, zoom)
        return self.carver.carve(lat, lon, z) if self.carver is not None else sink(z)

    def __getattr__(self, name):
        return getattr(self.dem, name)
