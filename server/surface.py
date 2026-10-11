"""Ground surfaces under the physics block (docs/ROADMAP.md v1.5 "Per-cell surface physics"; earthd
--surface, on by default since 2026-10-11).

The TerrainBlock is physics-only in mesh mode, so its layer map decides nothing but the tyres'
ground model (and GroundCover, which binds to a layer). Every 2 m sample of the block gets one of
MATERIALS from the ESA WorldCover class under it (docs/research/2026-10-10-world-data-sources.md
§5.4, without the OSM overrides yet) and the slope (ROCK where steeper than ROCK_SLOPE_DEG); the
server writes one 0/255 mask per material next to the heightmap and the mod imports them with the
block (8 greyscale masks: +4-5 ms per 512² import, docs/research/2026-10-11-engine-probes.md).

Land cover comes from the map-data cache (mapdata.py "landcover" layer, per z14 cell). Building a
block never waits for the network: a cell that is not cached yet reads as "no data" (DIRT, the
material the block had before this module) and is fetched in the background (prefetch around the
player, kind "prefetch"); the blocks built after it arrives have the right surfaces. The masks are a
pure function of the cached land cover and the heights, so a block rebuilt from a complete cache is
the same every time.
"""
import logging
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image

import mapcells
import mapdata as mapdatamod

log = logging.getLogger("earthd.surface")

VERSION = 1
# (TerrainMaterial name, BeamNG ground model). The last one is the level's own dirt material: the
# default, and the one every block had before (an all-zero sample falls to the later layer on ties,
# docs/research/2026-10-10-world-data-sources.md §4.4, so DIRT goes last).
# Names carry their own prefix: "bngEarthSnow" is the weather's Precipitation object (weather.lua), and
# a TerrainMaterial of that name was never made - its layer fell back to asphalt (in-game 2026-10-11).
MATERIALS = (
    ("bngEarthGroundAsphalt", "ASPHALT_OLD"),
    ("bngEarthGroundGrass", "GRASS"),
    ("bngEarthGroundDusty", "DIRT_DUSTY"),
    ("bngEarthGroundLoose", "DIRT_DUSTY_LOOSE"),
    ("bngEarthGroundRock", "ROCK"),
    ("bngEarthGroundSnow", "SNOW"),
    ("bngEarthGroundMud", "MUD"),
    ("beamngEarthDirt", "DIRT"),
)
ASPHALT, GRASS, DUSTY, LOOSE, ROCK, SNOW, MUD, DIRT = range(len(MATERIALS))
# WorldCover class -> material (§5.4 of the world-data research; built-up is asphalt until the OSM
# land-use overrides tell lawns from streets)
CLASS_MATERIAL = {0: DIRT, 10: GRASS, 20: DUSTY, 30: GRASS, 40: DIRT, 50: ASPHALT, 60: LOOSE, 70: SNOW,
                  80: MUD, 90: MUD, 95: MUD, 100: GRASS}
ROCK_SLOPE_DEG = 35.0       # steeper ground is rock (cliffs, scree), except snow
# Organic borders (user 2026-10-11: rectangular grass in Monument Valley). WorldCover's 10 m square
# pixels give stair-stepped, ruler-straight class borders, which the grass makes visible. Every
# sample reads the land cover a few metres off, by smooth value noise on a fixed world grid (no
# state, the same for every block): (feature size m, amplitude m) per octave.
WARP_OCTAVES = ((40.0, 8.0), (12.0, 3.0))
M_PER_DEG = 111320.0
PREFETCH_M = 3000.0         # land cover is fetched this far around the player
PREFETCH_MOVE_M = 500.0     # ... again after the player moved this far

_LUT = np.full(256, DIRT, np.uint8)
for _c, _m in CLASS_MATERIAL.items():
    _LUT[_c] = _m


def classify(classes, u, square):
    """Material index per sample: the land-cover class's material, ROCK where the slope of the
    height grid u (metres, [row, col], `square` m apart) is steeper than ROCK_SLOPE_DEG (snow stays
    snow). Pure."""
    idx = _LUT[np.asarray(classes, np.uint8)]
    u = np.asarray(u, np.float64)
    if min(u.shape) < 2:            # no slope from a single row / column
        return idx.astype(np.uint8)
    gy, gx = np.gradient(u, square)
    steep = (gx * gx + gy * gy) > math.tan(math.radians(ROCK_SLOPE_DEG)) ** 2
    return np.where(steep & (idx != SNOW), np.uint8(ROCK), idx).astype(np.uint8)


def _hash01(ix, iy, seed):
    """Deterministic pseudo-random in [-1, 1] per integer lattice point (uint64 arithmetic)."""
    h = (ix.astype(np.int64).astype(np.uint64) * np.uint64(0x9E3779B185EBCA87)
         ^ iy.astype(np.int64).astype(np.uint64) * np.uint64(0xC2B2AE3D27D4EB4F)
         ^ np.uint64(seed) * np.uint64(0x165667B19E3779F9))
    h ^= h >> np.uint64(29)
    h *= np.uint64(0xBF58476D1CE4E5B9)
    h ^= h >> np.uint64(32)
    return (h >> np.uint64(11)).astype(np.float64) / float(1 << 53) * 2.0 - 1.0


def _value_noise(x, y, seed):
    """Smooth value noise in [-1, 1] at points (x, y) given in lattice units."""
    x0 = np.floor(x)
    y0 = np.floor(y)
    fx = x - x0
    fy = y - y0
    fx = fx * fx * (3.0 - 2.0 * fx)
    fy = fy * fy * (3.0 - 2.0 * fy)
    ix = x0.astype(np.int64)
    iy = y0.astype(np.int64)
    a = _hash01(ix, iy, seed)
    b = _hash01(ix + 1, iy, seed)
    c = _hash01(ix, iy + 1, seed)
    d = _hash01(ix + 1, iy + 1, seed)
    return (a + (b - a) * fx) + ((c + (d - c) * fx) - (a + (b - a) * fx)) * fy


def warp(lat, lon):
    """(lat, lon) moved by a few metres of smooth noise (WARP_OCTAVES): where each sample reads the
    land cover, so class borders follow organic curves instead of the 10 m pixel grid. A pure
    function of the world position (blocks agree along their shared samples). Pure."""
    lat = np.asarray(lat, np.float64)
    lon = np.asarray(lon, np.float64)
    m_lon = M_PER_DEG * np.cos(np.radians(lat))
    xm = lon * m_lon                     # metres east of the lon 0 meridian (at this latitude)
    ym = lat * M_PER_DEG                 # metres north of the equator
    dx = np.zeros_like(lat)
    dy = np.zeros_like(lat)
    for k, (size, amp) in enumerate(WARP_OCTAVES):
        dx += amp * _value_noise(xm / size, ym / size, 2 * k + 1)
        dy += amp * _value_noise(xm / size, ym / size, 2 * k + 2)
    return lat + dy / M_PER_DEG, lon + dx / np.maximum(m_lon, 1e-6)


def mask_names(stem):
    return [f"{stem}_m{i}.png" for i in range(len(MATERIALS))]


def write_masks(idx, out_dir, stem):
    """One 0/255 greyscale PNG per material (rows as the heightmap's: row 0 = north), written
    atomically. Returns the file names (in out_dir), in MATERIALS order."""
    names = mask_names(stem)
    for i, name in enumerate(names):
        path = os.path.join(out_dir, name)
        tmp = path + ".tmp"
        Image.fromarray(np.where(idx == i, 255, 0).astype(np.uint8), "L").save(tmp, format="PNG", compress_level=1)
        os.replace(tmp, path)
    return names


class Surface:
    """Land cover for the blocks, from the map-data cache, never waiting for the network."""

    def __init__(self, mapdata):
        self.md = mapdata
        self.pool = ThreadPoolExecutor(1, thread_name_prefix="surface-prefetch")
        self._lock = threading.Lock()
        self._pending = set()
        self._last = None           # (lat, lon) of the last prefetch

    def close(self):
        self.pool.shutdown(wait=False)

    def _cached(self, key):
        """The cell's land-cover Raster from the cache, None if absent (a fetch is queued) or no
        coverage (open ocean: the layer is stored as missing)."""
        arrays = self.md._load(self.md.path(key, "landcover"))
        if arrays is None:
            self._queue(key)
            return None
        arrays = dict(arrays)
        arrays.pop("provenance", None)
        if "missing" in arrays:
            return None
        return mapdatamod._raster_from(arrays)

    def _queue(self, key):
        with self._lock:
            if key in self._pending:
                return
            self._pending.add(key)

        def run():
            try:
                self.md.prefetch(key, ("landcover",))
            except Exception as e:      # network trouble: asked again by the next block / prefetch
                log.debug("surface: land cover of %s: %s: %s", key, type(e).__name__, e)
            finally:
                with self._lock:
                    self._pending.discard(key)
        self.pool.submit(run)

    def prefetch(self, lat, lon, radius_m=PREFETCH_M):
        """Queue the land cover of the cells within radius_m (at most every PREFETCH_MOVE_M)."""
        if self._last is not None:
            dlat = (lat - self._last[0]) * 111320.0
            dlon = (lon - self._last[1]) * 111320.0 * math.cos(math.radians(lat))
            if dlat * dlat + dlon * dlon < PREFETCH_MOVE_M * PREFETCH_MOVE_M:
                return
        self._last = (lat, lon)
        dlat = radius_m / 111320.0
        dlon = radius_m / (111320.0 * max(0.05, math.cos(math.radians(lat))))
        for key in mapcells.cells_for_bbox(lat - dlat, lon - dlon, lat + dlat, lon + dlon):
            if self.md._load(self.md.path(key, "landcover")) is None:
                self._queue(key)

    def ready(self, lat, lon, radius_m=1000.0, timeout_s=2.0):
        """Fetch the land cover within radius_m now and wait for it, at most timeout_s (a teleport's
        first block: built once and kept while the player stands still). Returns True if it all
        arrived. Worker threads only (it blocks)."""
        dlat = radius_m / 111320.0
        dlon = radius_m / (111320.0 * max(0.05, math.cos(math.radians(lat))))
        keys = [k for k in mapcells.cells_for_bbox(lat - dlat, lon - dlon, lat + dlat, lon + dlon)
                if self.md._load(self.md.path(k, "landcover")) is None]
        for k in keys:
            self._queue(k)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not any(self._is_queued(k) for k in keys):
                return True
            time.sleep(0.05)
        return False

    def classes(self, lat, lon):
        """(WorldCover class per point, complete): 0 (no data) where the cell is not cached yet. The
        land cover is read at the warped points (warp: organic borders)."""
        lat, lon = warp(lat, lon)
        out = np.zeros(lat.shape, np.uint8)
        complete = True
        for key in mapcells.cells_for_bbox(float(lat.min()), float(lon.min()), float(lat.max()), float(lon.max())):
            r = self._cached(key)
            lon0, lat0, lon1, lat1 = key.bounds()
            inside = (lat >= lat0) & (lat < lat1) & (lon >= lon0) & (lon < lon1)
            if not inside.any():
                continue
            if r is None:
                complete = complete and not self._is_queued(key)
                continue
            out[inside] = r.sample(lon[inside], lat[inside])
        return out, complete

    def _is_queued(self, key):
        with self._lock:
            return key in self._pending
