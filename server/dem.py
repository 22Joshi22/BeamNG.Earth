"""Elevation tiles (Terrarium encoding, Web Mercator) with a disk cache.

Primary: Mapterhorn, 512 px WebP tiles. Measured 2026-10-09: z<=12 everywhere
on land, deeper only where national lidar exists (z16 around Lone Pine CA,
z17 in the Swiss Alps); everything else returns 404. Open ocean is 404 even at
z11, and in coastal tiles the sea reads exactly 0.
Fallback: AWS Terrain Tiles, 256 px PNG, z<=15, global, includes bathymetry.

All sampling happens on the Mapterhorn grid (512 px tiles). A fallback tile is
assembled from AWS tiles of the same footprint and resampled to 512 px.

Requests go through a netfetch.Fetcher (rate limit, retries, circuit breaker per host). When
Mapterhorn cannot be asked (circuit open, rate limited, failing) the same tile comes from AWS
(failover); if AWS fails too, DemUnavailable. 404s are remembered as <y>.404 markers that expire
after the host's miss_ttl (Mapterhorn 90 days: it adds lidar areas; AWS never).
"""
import io
import logging
import math
import os
import threading
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image

import netfetch

log = logging.getLogger("earthd.dem")

TILE = 512                  # sampling grid tile size (Mapterhorn)
MAX_ZOOM = 15               # AWS stops at z15; nothing in v1 needs more
GLOBAL_ZOOM = 12            # Mapterhorn is complete on land up to here
MAX_LAT = 85.0511287798     # Web Mercator limit
MAX_TILES_PER_SAMPLE = 1024

MAPTERHORN = dict(name="mapterhorn", ext="webp", size=512,
                  url="https://tiles.mapterhorn.com/{z}/{x}/{y}.webp")
AWS = dict(name="aws-terrarium", ext="png", size=256,
           url="https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png")


class DemUnavailable(IOError):
    """Neither Mapterhorn nor AWS could deliver a tile (network, offline)."""


def zoom_for(lat, pixel_m, max_zoom=MAX_ZOOM):
    """Smallest zoom whose 512 px tile pixels are <= pixel_m at this latitude."""
    c = 40075016.686 * max(math.cos(math.radians(min(abs(lat), MAX_LAT))), 1e-6)
    return int(min(max_zoom, max(0, math.ceil(math.log2(c / (TILE * pixel_m))))))


def decode_terrarium(data, size):
    im = Image.open(io.BytesIO(data)).convert("RGB")
    if im.size != (size, size):
        log.warning("unexpected DEM tile size %s (expected %d), resampling", im.size, size)
        im = im.resize((size, size), Image.BILINEAR)
    a = np.asarray(im).astype(np.float32)
    return a[..., 0] * 256.0 + a[..., 1] + a[..., 2] / 256.0 - 32768.0


def _resize(a, size):
    if a.shape[0] == size:
        return a
    return np.asarray(Image.fromarray(a, mode="F").resize((size, size), Image.BILINEAR))


class Dem:
    # mem_tiles: decoded float32 512² tiles kept in memory (1 MB each; 512 per docs/V1_2_DESIGN.md §4.3).
    # fetcher: the server's netfetch.Fetcher (shared rate limits); None = a Fetcher of its own.
    def __init__(self, cache_dir, fetch_workers=8, mem_tiles=512, fetcher=None):
        self.cache_dir = cache_dir
        self.fetcher = fetcher if fetcher is not None else netfetch.Fetcher()
        self.fetcher.register_chain("dem", (MAPTERHORN["url"], AWS["url"]))
        self.pool = ThreadPoolExecutor(fetch_workers, thread_name_prefix="dem-fetch")
        self.mem_tiles = mem_tiles
        self._lru = OrderedDict()       # (z, x, y) -> float32 [TILE, TILE]
        self._inflight = {}             # (z, x, y) -> Future
        self._lock = threading.Lock()
        self.stats = Counter()          # updated from fetch threads; read via stats_snapshot()

    def _count(self, what):
        with self._lock:
            self.stats[what] += 1

    def stats_snapshot(self):
        with self._lock:
            return dict(self.stats)

    # --- raw tiles -------------------------------------------------------
    def _download(self, src, z, x, y):
        """Raw tile bytes from disk cache or network; None for a (cached) 404. Raises
        netfetch.FetchError when the host cannot deliver."""
        path = os.path.join(self.cache_dir, src["name"], str(z), str(x), f"{y}.{src['ext']}")
        miss = os.path.join(self.cache_dir, src["name"], str(z), str(x), f"{y}.404")
        if os.path.exists(path):
            self._count(src["name"] + ":disk")
            with open(path, "rb") as f:
                return f.read()
        url = src["url"].format(z=z, x=x, y=y)
        if self.fetcher.miss_alive(miss, url):
            return None
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            data = self.fetcher.get(url).body
        except netfetch.NotFound:
            open(miss, "wb").close()        # (re)written: its mtime starts the marker's TTL
            self._count(src["name"] + ":404")
            return None
        except netfetch.FetchError:
            self._count(src["name"] + ":error")
            raise
        tmp = f"{path}.{threading.get_ident()}.tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        if os.path.exists(miss):            # an expired marker: the host has data there now
            os.remove(miss)
        self._count(src["name"] + ":net")
        return data

    def _aws_tile(self, z, x, y):
        """Fallback for Mapterhorn tile (z, x, y), built from AWS z+1 (max 15)."""
        za = min(MAX_ZOOM, z + 1)
        n = 2 ** (za - z)
        s = AWS["size"]
        mosaic = np.zeros((n * s, n * s), np.float32)
        found = False
        for j in range(n):
            for i in range(n):
                data = self._download(AWS, za, x * n + i, y * n + j)
                if data is not None:
                    mosaic[j * s:(j + 1) * s, i * s:(i + 1) * s] = decode_terrarium(data, s)
                    found = True
        return _resize(mosaic, TILE) if found else None

    def _load_tile(self, key):
        z, x, y = key
        try:
            data = self._download(MAPTERHORN, z, x, y)
        except netfetch.FetchError as e:
            # Mapterhorn cannot be asked (circuit open, rate limited, failing): the same tile from AWS,
            # as it is (with AWS bathymetry: a failure says nothing about where the sea is).
            self._count("mapterhorn:failover")
            try:
                a = self._aws_tile(z, x, y)
            except netfetch.FetchError as e2:
                raise DemUnavailable(f"DEM tile {z}/{x}/{y}: Mapterhorn: {e}; AWS: {e2}") from None
            return np.zeros((TILE, TILE), np.float32) if a is None else a
        if data is not None:
            return decode_terrarium(data, MAPTERHORN["size"])
        try:
            a = self._aws_tile(z, x, y)
        except netfetch.FetchError as e:
            raise DemUnavailable(f"DEM tile {z}/{x}/{y}: AWS: {e}") from None
        if a is None:
            return np.zeros((TILE, TILE), np.float32)
        if z <= GLOBAL_ZOOM:
            # Mapterhorn is complete on land at these zooms, so a 404 here is
            # open ocean. Drop AWS bathymetry: the sea surface is what we draw.
            a = np.maximum(a, 0.0)
        return a

    def tiles(self, keys, progress=None):
        """Fetch many tiles concurrently (deduplicated across callers)."""
        keys = list(dict.fromkeys(keys))
        out, futs, new = {}, {}, []
        with self._lock:
            for k in keys:
                if k in self._lru:
                    self._lru.move_to_end(k)
                    out[k] = self._lru[k]
                elif k in self._inflight:
                    futs[k] = self._inflight[k]
                else:
                    futs[k] = self._inflight[k] = self.pool.submit(self._load_tile, k)
                    new.append(k)
        # Outside the lock: the callback runs at once if the future is already done.
        for k in new:
            futs[k].add_done_callback(lambda f, k=k: self._finish(k, f))
        done = len(out)
        if progress:
            progress(done, len(keys))
        for k, fut in futs.items():
            out[k] = fut.result()
            done += 1
            if progress:
                progress(done, len(keys))
        return out

    def _finish(self, key, fut):
        """Move a finished load from _inflight into the LRU (failures are dropped,
        so the next request retries instead of reusing a stale exception)."""
        with self._lock:
            if self._inflight.get(key) is fut:
                del self._inflight[key]
            if not fut.cancelled() and fut.exception() is None:
                self._lru[key] = fut.result()
                while len(self._lru) > self.mem_tiles:
                    self._lru.popitem(last=False)

    def block_max(self, z, x, y, block):
        """Highest elevation in every block x block pixel square of tile (z, x, y): a (TILE / block)²
        array, row 0 north (the horizon mode's coarse pass, farfield.horizon_grid)."""
        a = self.tiles([(z, x, y)])[(z, x, y)]
        n = TILE // block
        return a.reshape(n, block, n, block).max(axis=(1, 3))

    # --- sampling --------------------------------------------------------
    def sample(self, lat, lon, zoom, progress=None):
        """Bilinear elevation (m) at arbitrary lat/lon arrays from zoom-level tiles.
        Points beyond the Web Mercator limit (|lat| > 85.05) return 0."""
        if zoom > MAX_ZOOM:
            raise ValueError(f"zoom {zoom} > {MAX_ZOOM}")
        lat = np.asarray(lat, dtype=np.float64)
        lon = np.asarray(lon, dtype=np.float64)
        shape = np.broadcast(lat, lon).shape
        lat, lon = np.broadcast_to(lat, shape).ravel(), np.broadcast_to(lon, shape).ravel()
        out = np.zeros(lat.shape, np.float64)
        valid = np.abs(lat) <= MAX_LAT
        if not valid.any():
            return out.reshape(shape)
        lat, lon = lat[valid], lon[valid]
        n = 2 ** zoom
        w = n * TILE
        # Global pixel coordinates; pixel centres sit at +0.5.
        fx = (lon / 360.0 + 0.5) * w
        fx -= w * np.round((fx - fx[0]) / w)    # unwrap across the antimeridian
        fx -= 0.5
        fy = (0.5 - np.arcsinh(np.tan(np.radians(lat))) / (2 * np.pi)) * w - 0.5
        tx0, tx1 = int(math.floor(fx.min())) // TILE, int(math.floor(fx.max()) + 1) // TILE
        ty0 = max(0, int(math.floor(fy.min())) // TILE)
        ty1 = min(n - 1, int(math.floor(fy.max()) + 1) // TILE)
        nx, ny = tx1 - tx0 + 1, ty1 - ty0 + 1
        if nx * ny > MAX_TILES_PER_SAMPLE:
            raise ValueError(f"sample area too large: {nx}x{ny} tiles at z{zoom}")
        keys = {(tx, ty): (zoom, tx % n, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)}
        got = self.tiles(keys.values(), progress)
        mosaic = np.empty((ny * TILE, nx * TILE), np.float32)
        for (tx, ty), k in keys.items():
            c, r = (tx - tx0) * TILE, (ty - ty0) * TILE
            mosaic[r:r + TILE, c:c + TILE] = got[k]
        fx -= tx0 * TILE
        fy -= ty0 * TILE
        ix = np.clip(np.floor(fx).astype(np.int64), 0, nx * TILE - 2)
        iy = np.clip(np.floor(fy).astype(np.int64), 0, ny * TILE - 2)
        wx = np.clip(fx - ix, 0.0, 1.0)
        wy = np.clip(fy - iy, 0.0, 1.0)
        v00, v10 = mosaic[iy, ix], mosaic[iy, ix + 1]
        v01, v11 = mosaic[iy + 1, ix], mosaic[iy + 1, ix + 1]
        out[valid] = (v00 * (1 - wx) + v10 * wx) * (1 - wy) + (v01 * (1 - wx) + v11 * wx) * wy
        return out.reshape(shape)
