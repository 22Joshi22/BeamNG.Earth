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

VERSION = 1
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
CARVE_MAX_LEVEL = 1         # far tiles up to this level are carved (their map data is near the player);
                            # coarser ones have the DEM's sea sunk (sink)
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


class SeaCarver:
    def __init__(self, md, cache_dir=None):
        self.md = md
        self.dir = os.path.join(cache_dir, "seacarve", f"v{VERSION}") if cache_dir else None
        self._mem = OrderedDict()       # (x, y) -> depth raster (float32, CELL_PX + 2 square) or None
        self._lock = threading.Lock()
        self._cell_locks = {}

    # --- rasters --------------------------------------------------------------------------------
    def _water(self, key):
        try:
            return self.md.cell(key, layers=("water",)).layers.get("water")
        except Exception as e:          # offline / fetch refused: no sea there for now
            log.warning("sea: no water data for %s (%s)", key, e)
            return None

    def raster(self, x, y):
        """Depth raster of cell (x, y): (CELL_PX + 2)² float32, pixel [j, i] centred on z16 pixel
        (x * CELL_PX - 1 + i + 0.5, y * CELL_PX - 1 + j + 0.5); None when the cell has no sea."""
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
        return os.path.join(self.dir, str(rel), str(x), f"{y}.npz") if self.dir else None

    def _load(self, x, y):
        p = self._path(x, y)
        if p and os.path.exists(p):
            with np.load(p, allow_pickle=False) as f:
                return f["depth"].astype(np.float32) / 20.0 if f["any"] else None
        r = self._build(x, y)
        if p:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "wb") as fh:
                if r is None:
                    np.savez_compressed(fh, any=False, depth=np.zeros((1, 1), np.uint8))
                else:
                    np.savez_compressed(fh, any=True, depth=np.round(r * 20.0).astype(np.uint8))
            os.replace(tmp, p)
            if r is not None:
                r = np.round(r * 20.0).astype(np.float32) / 20.0     # the value the file gives back
        return r

    def _build(self, x, y):
        own = self._water((mapcells.ZOOM, x, y))
        if own is None or not np.isin(own.attrs["subtype"], SEA_SUBTYPES).any():
            return None                 # no sea in the cell itself: nothing to carve here
        n = CELL_PX + 2 * PAD_PX
        ox, oy = x * CELL_PX - PAD_PX, y * CELL_PX - PAD_PX      # z16 pixel of the raster's corner
        water = np.zeros((n, n), bool)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                f = own if (dx, dy) == (0, 0) else self._water((mapcells.ZOOM, x + dx, y + dy))
                if f is not None:
                    self._rasterise(f, water, ox, oy)
        lat_c = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 0.5) / (1 << mapcells.ZOOM)))))
        px = _px_m(lat_c)
        kmax = int(math.ceil(DEPTH_CAP / DEPTH_SLOPE / px)) + 2
        steps = shore_steps(water, kmax).astype(np.float32)
        depth = np.where(steps > 0, np.minimum(DEPTH_CAP, DEPTH_SLOPE * (steps - 0.5) * px), 0.0)
        return depth[PAD_PX - 1:PAD_PX + CELL_PX + 1, PAD_PX - 1:PAD_PX + CELL_PX + 1].astype(np.float32)

    @staticmethod
    def _rasterise(f, water, ox, oy):
        n = water.shape[0]
        for i in np.flatnonzero(np.isin(f.attrs["subtype"], SEA_SUBTYPES) & (f.kind == 3)):
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
                for r in rings:         # even-odd: holes (islands) cut out
                    inside ^= Path(r).contains_points(pts)
                water[j0:j1, i0:i1] |= inside.reshape(j1 - j0, i1 - i0)

    # --- queries --------------------------------------------------------------------------------
    def depth(self, lat, lon):
        """Floor depth below the sea surface at the points (0 on land and away from the sea)."""
        lat = np.asarray(lat, np.float64)
        lon = np.asarray(lon, np.float64)
        out = np.zeros(lat.shape, np.float32)
        if lat.size == 0:
            return out
        mx, my = _merc(lat, lon)
        cx = np.floor(mx / CELL_PX).astype(np.int64)
        cy = np.floor(my / CELL_PX).astype(np.int64)
        keys = np.unique(np.stack([cx.ravel(), cy.ravel()], -1), axis=0)
        for kx, ky in keys:
            r = self.raster(int(kx), int(ky))
            if r is None:
                continue
            sel = (cx == kx) & (cy == ky)
            fx = mx[sel] - kx * CELL_PX + 0.5      # raster coords: pixel centres at i + 0.5 - 1
            fy = my[sel] - ky * CELL_PX + 0.5
            i0 = np.clip(np.floor(fx).astype(np.int64), 0, r.shape[1] - 2)
            j0 = np.clip(np.floor(fy).astype(np.int64), 0, r.shape[0] - 2)
            tx, ty = np.clip(fx - i0, 0, 1), np.clip(fy - j0, 0, 1)
            out[sel] = ((r[j0, i0] * (1 - tx) + r[j0, i0 + 1] * tx) * (1 - ty)
                        + (r[j0 + 1, i0] * (1 - tx) + r[j0 + 1, i0 + 1] * tx) * ty)
        return out

    def carve(self, lat, lon, z):
        """z (DEM heights at the points) with the sea floor carved in."""
        d = self.depth(lat, lon)
        if not d.any():
            return z
        z = np.asarray(z, np.float64)
        return np.where(d > 0, np.minimum(z, SEA_E - d), z)

    def near(self, lat, lon, radius_m, step_m=250.0):
        """Any sea within radius_m of (lat, lon) (sampled on a step_m grid)."""
        k = int(radius_m // step_m)
        o = np.arange(-k, k + 1) * step_m
        E, N = np.meshgrid(o, o)
        inside = np.hypot(E, N) <= radius_m
        la = lat + N[inside] / 111320.0
        lo = lon + E[inside] / (111320.0 * math.cos(math.radians(lat)))
        return bool((self.depth(la, lo) > 0).any())

    def sea_at(self, dem, lat, lon, size):
        """Which points of a water tile of `size` m are sea (seatiles.plan): the polygons for the small
        tiles near the player, the DEM band for larger ones."""
        if size <= POLY_TILE_MAX:
            return self.depth(lat, lon) > 0
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
