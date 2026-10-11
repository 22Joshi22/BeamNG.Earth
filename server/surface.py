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
from PIL import Image, ImageDraw

import mapcells
import mapdata as mapdatamod
import roads as roadsmod

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


# road surface kind (roads.py) -> material; drawn in this order (paved last: it wins at junctions)
ROAD_MATERIAL = {"dirt": DIRT, "gravel": LOOSE, "paved": ASPHALT}
ROAD_ORDER = ("dirt", "gravel", "paved")


def grid_affine(lat, lon):
    """Least-squares affine map (lon, lat) -> (col, row) of a sample grid given by its lat / lon arrays
    (a 1 km block is affine to well under a centimetre). Returns a function (lon, lat) -> (col, row)."""
    lat = np.asarray(lat, np.float64)
    lon = np.asarray(lon, np.float64)
    h, w = lat.shape
    step = max(1, min(h, w) // 16)
    rr, cc = np.mgrid[0:h:step, 0:w:step]
    la, lo = lat[rr, cc].ravel(), lon[rr, cc].ravel()
    lo0, la0 = float(lo.mean()), float(la.mean())
    A = np.stack([lo - lo0, la - la0, np.ones_like(lo)], axis=1)
    coef, *_ = np.linalg.lstsq(A, np.stack([cc.ravel(), rr.ravel()], axis=1).astype(np.float64), rcond=None)

    def f(lon_, lat_):
        B = np.stack([np.asarray(lon_, np.float64) - lo0, np.asarray(lat_, np.float64) - la0,
                      np.ones(np.shape(lon_))], axis=-1)
        cr = B @ coef
        return cr[..., 0], cr[..., 1]
    return f


def paint_roads(idx, lat, lon, feats_list, square, urban_fn=None):
    """idx (material per sample, [row, col], as classify gives it) with the roads of feats_list (the
    "roads" Features of the cells around) drawn in at their width: ROAD_MATERIAL by surface kind.
    lat / lon: the samples' positions; square: m between samples; urban_fn: as roads.road_specs (the
    same widths as the drawn roads). Pure (a new array)."""
    h, w = idx.shape
    to_px = grid_affine(lat, lon)
    layers = {k: Image.new("L", (w, h), 0) for k in ROAD_ORDER}
    draws = {k: ImageDraw.Draw(im) for k, im in layers.items()}
    seen = set()
    for feats in feats_list:
        if feats is None or len(feats) == 0:
            continue
        specs = roadsmod.road_specs(feats, urban_fn)
        for i in range(len(feats)):
            fid = str(feats.ids[i])
            if specs[i] is None or fid in seen:
                continue
            seen.add(fid)
            width, kind = specs[i].width, specs[i].kind
            px = max(1, int(round(width / square)))
            for part in feats.parts(i):
                for ring in part:
                    if len(ring) < 2:
                        continue
                    c, r = to_px(ring[:, 0], ring[:, 1])
                    if c.max() < -px or c.min() > w + px or r.max() < -px or r.min() > h + px:
                        continue
                    draws[kind].line(list(zip(c.tolist(), r.tolist())), fill=255, width=px, joint="curve")
    out = idx.copy()
    for k in ROAD_ORDER:
        m = np.asarray(layers[k]) > 0
        out[m] = ROAD_MATERIAL[k]
    return out


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
        self._pending_layers = set()    # (key, layers) queued
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

    def _queue(self, key, layers=("landcover",)):
        with self._lock:
            if (key, layers) in self._pending_layers:
                return
            self._pending_layers.add((key, layers))
            self._pending.add(key)

        def run():
            try:
                self.md.prefetch(key, layers)
            except Exception as e:      # network trouble: asked again by the next block / prefetch
                log.debug("surface: land cover of %s: %s: %s", key, type(e).__name__, e)
            finally:
                with self._lock:
                    self._pending_layers.discard((key, layers))
                    if not any(k == key for k, _ in self._pending_layers):
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
            if self.md._load(self.md.path(key, "roads")) is None:
                self._queue(key, ("roads",))

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

    def roads(self, lat, lon):
        """(the "roads" Features of the cells around the points that are cached, complete). Never waits:
        missing cells are fetched in the background."""
        lat = np.asarray(lat, np.float64)
        lon = np.asarray(lon, np.float64)
        out, complete = [], True
        for key in mapcells.cells_for_bbox(float(lat.min()), float(lon.min()), float(lat.max()), float(lon.max())):
            if self.md._load(self.md.path(key, "roads")) is None:
                self._queue(key, ("roads",))
                complete = False
                continue
            try:
                out.append(self.md.cell(key, layers=("roads",), kind="background").layers.get("roads"))
            except Exception as e:      # an unreadable cache file: like a missing one
                log.debug("surface: roads of %s: %s: %s", key, type(e).__name__, e)
                complete = False
        return out, complete

    def urban(self, lat, lon):
        """Built-up land cover (WorldCover 50) at the points: roads.road_specs's urban_fn. Uncached cells
        read as not built-up."""
        return self.classes(lat, lon)[0] == roadsmod.URBAN_CLASS

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
