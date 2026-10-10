"""Satellite imagery for the visual tiles (docs/V1_3_SPEC.md §A).

Sources (all Web-Mercator XYZ, 256 px tiles, no key; verified 2026-10-10):
  usgs     USGS The National Map ImageryOnly. Public domain. US only, z <= 16 (~1.9 m/px at 36 N).
           Outside its coverage it answers 404, or (at low zooms) a uniform white tile.
  eox2023  Sentinel-2 cloudless 2023 by EOX. Global, ~10 m (native around z14). CC BY-NC-SA 4.0.
           Open ocean is a uniform deep blue tile (a real colour, not "no data").
  eox2016  Sentinel-2 cloudless 2016 by EOX. Global, ~10 m. CC BY 4.0. Last fallback.

A texture is rendered for a tile's ENU footprint by sampling the imagery at every texel centre:
ENU plane point (anchor frame) -> lat/lon (the same mapping the tile geometry uses, so the
image sits exactly on the terrain) -> Web-Mercator pixel at a zoom whose ground resolution is
about the texel size (never finer than the source's max zoom) -> bilinear. Each sample takes the
first source of the chain that has data there: a source tile that is 404, or uniform (blank)
while a later source remains, counts as missing, so the chain falls back per source tile.
Samples no source covers (beyond +-85.05 deg, or 404 everywhere) get FALLBACK_RGB.

Caching: raw tiles on disk under <cache>/<source>/<z>/<x>/<y>.jpg (whatever format the server
sent), 404s remembered as <y>.404 markers (as dem.py does), decoded tiles in an in-memory LRU.
Downloads run on a small pool (FETCH_WORKERS <= 6 concurrent requests, polite User-Agent), each
thread keeping one persistent HTTPS connection per host (a fresh TLS handshake per tile roughly
doubled the latency).

Output: DDS DXT1 with a full mip chain (write_dds). Pillow 12 writes DXT1 but no mipmaps (its
header always says 0 mips), so every level is box-downsampled and BC1-encoded by Pillow separately
and the levels are concatenated under our own header (DDSD_MIPMAPCOUNT, DDSCAPS_MIPMAP|COMPLEX).
"""
import http.client
import io
import logging
import math
import os
import struct
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urlsplit

import numpy as np
from PIL import Image

import dem as demmod

log = logging.getLogger("earthd.imagery")

TILE = 256
MAX_LAT = demmod.MAX_LAT
EARTH_CIRC = 40075016.686          # m, equatorial circumference (Web-Mercator scale)
FETCH_WORKERS = 6
MEM_TILES = 512                    # decoded 256² RGB tiles kept in memory (~100 MB)
ATTEMPTS = 3
RETRY_DELAY = 0.5                  # s, doubled per attempt
TIMEOUT = 20.0
# After a tile fetch failed every attempt (network down, server errors), the source counts as
# unreachable for this long: its tiles fail at once instead of each paying ATTEMPTS x TIMEOUT
# plus the retry delays (offline, every texture took ~10 s, so the 3 tile workers - which also
# build the geometry - fell minutes behind the player). Tiles fall back to the next source or,
# with none left, to the palette material.
SOURCE_BACKOFF = 30.0              # s
MAX_MOSAIC = 36                    # source tiles per texture and source (zoom is lowered beyond)
MESH_CELLS = 8                     # render(): exact reprojection on an 8 x 8 grid of texture cells
FALLBACK_RGB = (100, 89, 69)       # no imagery anywhere: the terrain material's dirt colour
BLANK_RANGE = 2                    # a tile whose pixels all lie within this many levels is uniform


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    max_zoom: int
    attribution: str
    # Coverage probes: a tile is only fetched if its ancestors at these zooms have data (not 404,
    # not blank). USGS answers outside the US with 404s or white tiles, ~280 ms each; without the
    # probes a start in the Alps asked it 1132 times before using EOX.
    probe_zooms: tuple = ()
    # Below this zoom the source is not used. USGS serves a global low-resolution basemap at
    # z <= 9 (verified 2026-10-10: imagery for the Alps / Pyrenees / open Pacific at z6 and z8,
    # 404 at z10), which would put a different source on the far rings outside the US.
    min_zoom: int = 0


SOURCES = {
    "usgs": Source("usgs", "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}",
                   16, "USGS The National Map (public domain)", probe_zooms=(10, 12), min_zoom=10),
    "eox2023": Source("eox2023", "https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2023_3857/default/g/{z}/{y}/{x}.jpg",
                      14, "Sentinel-2 cloudless 2023 by EOX IT Services GmbH (Contains modified Copernicus "
                          "Sentinel data 2023), CC BY-NC-SA 4.0"),
    "eox2016": Source("eox2016", "https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless_3857/default/g/{z}/{y}/{x}.jpg",
                      14, "Sentinel-2 cloudless 2016 by EOX IT Services GmbH (Contains modified Copernicus "
                          "Sentinel data 2016), CC BY 4.0"),
}
DEFAULT_CHAIN = ("usgs", "eox2023", "eox2016")
# The line the mod shows (V1_3_SPEC §C), for the default chain.
ATTRIBUTION = ("Imagery: USGS The National Map (public domain); Sentinel-2 cloudless by EOX "
               "(CC BY-NC-SA 4.0 / CC BY 4.0)")


class ImageryError(IOError):
    """Imagery could not be fetched (network or server errors, not a missing tile)."""


def parse_chain(text):
    """--imagery value -> tuple of source names, or None for 'off'."""
    if text.strip().lower() == "off":
        return None
    chain = tuple(s.strip().lower() for s in text.split(",") if s.strip())
    bad = [s for s in chain if s not in SOURCES]
    if bad or not chain:
        raise ValueError(f"unknown imagery source(s) {bad or text!r}; choose from {', '.join(SOURCES)} or off")
    return chain


def tone_lut(strength=1.0):
    """Mild global tone curve as a 256-entry LUT (applied per channel, sRGB values).

    Satellite images have the sun's shading and shadows baked in, and the engine lights the
    ground again, so the curve lifts the deepest shadows a little (+0.06 at black) and pulls
    the brightest highlights down (-0.10 at white, so bare rock and sand do not glow under the
    sun) while leaving mid-tones almost unchanged (-0.005 at 0.5). strength 0 = identity; the
    curve stays monotonic for strength <= 3."""
    x = np.arange(256) / 255.0
    y = x + strength * (0.06 * (1 - x) ** 3 - 0.10 * x ** 3)
    return np.clip(np.rint(y * 255), 0, 255).astype(np.uint8)


def ground_res(lat, z):
    """Web-Mercator ground resolution (m per 256 px tile pixel) at latitude lat and zoom z."""
    return EARTH_CIRC * math.cos(math.radians(min(abs(lat), MAX_LAT))) / (TILE * 2 ** z)


def zoom_for(lat, texel_m, max_zoom=19):
    """Zoom whose ground resolution is closest (in log2) to the texel size."""
    c = EARTH_CIRC * max(math.cos(math.radians(min(abs(lat), MAX_LAT))), 1e-6)
    return int(min(max_zoom, max(0, round(math.log2(c / (TILE * texel_m))))))


def mercator_px(lat, lon, z):
    """Global pixel coordinates (x right, y down; pixel centres at +0.5) of lat/lon at zoom z."""
    w = TILE * 2 ** z
    x = (np.asarray(lon, np.float64) / 360.0 + 0.5) * w
    y = (0.5 - np.arcsinh(np.tan(np.radians(lat))) / (2 * np.pi)) * w
    return x, y


def is_uniform(a):
    """True for a one-colour (blank) tile: every channel varies by at most BLANK_RANGE."""
    return int((a.max(axis=(0, 1)).astype(int) - a.min(axis=(0, 1))).max()) <= BLANK_RANGE


@dataclass(frozen=True)
class Tile:
    """A decoded source tile: RGB uint8 [256, 256, 3], and whether it is one colour (blank)."""
    rgb: np.ndarray
    uniform: bool


def as_tile(rgb):
    return Tile(rgb, is_uniform(rgb))


class Imagery:
    def __init__(self, cache_dir, chain=DEFAULT_CHAIN, fetch_workers=FETCH_WORKERS, mem_tiles=MEM_TILES,
                 tone=1.0):
        self.cache_dir = cache_dir
        self.chain = tuple(SOURCES[s] if isinstance(s, str) else s for s in chain)
        self.pool = ThreadPoolExecutor(min(fetch_workers, FETCH_WORKERS), thread_name_prefix="img-fetch")
        self.mem_tiles = mem_tiles
        self.lut = tone_lut(tone)
        self.lut_table = self.lut.tolist() * 3          # Image.point: one LUT per band
        self._lru = OrderedDict()           # (source, z, x, y) -> Tile, or None (404)
        self._inflight = {}
        self._lock = threading.RLock()     # RLock: a done future's callback runs at once
        self._local = threading.local()     # per fetch thread: {host: HTTPSConnection}
        self.stats = {}                     # counters ("usgs:net", "eox2023:404", ...), and fetch times
        self.fetch_ms = []                  # network fetch latencies (ms), for the log / measurements
        self._down = {}                     # source name -> monotonic time until it counts as unreachable

    def attribution(self):
        """Attribution line for the sources in use."""
        if [s.name for s in self.chain] == list(DEFAULT_CHAIN):
            return ATTRIBUTION
        return "Imagery: " + "; ".join(s.attribution for s in self.chain)

    def _count(self, what):
        with self._lock:
            self.stats[what] = self.stats.get(what, 0) + 1

    def stats_snapshot(self):
        with self._lock:
            return dict(self.stats)

    # --- raw tiles -------------------------------------------------------------------------
    def summary(self):
        """Counters and network fetch latency (median / p95 ms), for the log."""
        with self._lock:
            ms = sorted(self.fetch_ms)
            st = dict(sorted(self.stats.items()))
        lat = f"fetch {ms[len(ms) // 2]:.0f}/{ms[int(len(ms) * 0.95)]:.0f} ms (median/p95, n={len(ms)})" if ms else "no fetches"
        return f"{lat}; {st}"

    def _paths(self, src, z, x, y):
        d = os.path.join(self.cache_dir, src.name, str(z), str(x))
        return os.path.join(d, f"{y}.jpg"), os.path.join(d, f"{y}.404")

    def _http_get(self, url):
        """(status, body) over this thread's persistent connection to the host."""
        u = urlsplit(url)
        conns = self._local.__dict__.setdefault("conns", {})
        path = u.path + (f"?{u.query}" if u.query else "")
        for retry_closed in (0, 1):
            conn = conns.get(u.netloc)
            if conn is None:
                conn = conns[u.netloc] = http.client.HTTPSConnection(u.netloc, timeout=TIMEOUT)
            try:
                conn.request("GET", path, headers={"User-Agent": demmod.USER_AGENT, "Connection": "keep-alive"})
                r = conn.getresponse()
                return r.status, r.read()
            except (http.client.HTTPException, OSError):
                conn.close()
                del conns[u.netloc]
                if retry_closed:        # a fresh connection failed too
                    raise
                # else: the server closed the idle keep-alive connection; reconnect once

    def _discard(self, src, z, x, y):
        try:
            os.remove(self._paths(src, z, x, y)[0])
        except OSError:
            pass

    def _download(self, src, z, x, y):
        """Raw tile bytes from the disk cache or the network; None for a (remembered) 404."""
        path, miss = self._paths(src, z, x, y)
        if os.path.exists(path):
            self._count(src.name + ":disk")
            with open(path, "rb") as f:
                return f.read()
        if os.path.exists(miss):
            return None
        url = src.url.format(z=z, x=x, y=y)
        if time.monotonic() < self._down.get(src.name, 0.0):
            self._count(src.name + ":skipped")
            raise ImageryError(f"{src.name} unreachable (backing off): {url}")
        err = None
        for attempt in range(ATTEMPTS):
            if attempt:
                time.sleep(RETRY_DELAY * 2 ** (attempt - 1))
            t0 = time.perf_counter()
            try:
                status, data = self._http_get(url)
            except (http.client.HTTPException, OSError) as e:
                err = e
            else:
                if status == 200:
                    break
                if status in (400, 404):            # outside the source's coverage
                    os.makedirs(os.path.dirname(miss), exist_ok=True)
                    open(miss, "wb").close()
                    self._count(src.name + ":404")
                    return None
                err = f"HTTP {status}"
        else:
            self._down[src.name] = time.monotonic() + SOURCE_BACKOFF
            raise ImageryError(f"imagery fetch failed: {url}: {err}")
        with self._lock:
            self.fetch_ms.append((time.perf_counter() - t0) * 1000)
            del self.fetch_ms[:-2000]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{threading.get_ident()}.tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        self._count(src.name + ":net")
        return data

    def _load(self, key):
        """Decoded tile (Tile), or None if the source has none there."""
        src, z, x, y = key
        data = self._download(src, z, x, y)
        if data is None:
            return None
        try:
            im = Image.open(io.BytesIO(data))
            if im.mode in ("P", "LA", "RGBA"):     # EOX's ocean PNG: paletted with transparency
                im = im.convert("RGBA")
            im = im.convert("RGB")
        except (OSError, ValueError) as e:     # not an image (an error page served as 200?)
            self._discard(src, z, x, y)
            raise ImageryError(f"undecodable {src.name} tile {z}/{x}/{y}: {e}") from None
        if im.size != (TILE, TILE):
            im = im.resize((TILE, TILE), Image.BILINEAR)
        return as_tile(np.asarray(im))

    @staticmethod
    def _probe_key(key):
        """The finest coverage-probe ancestor of a tile, or None (probes chain: the probe's own
        probe is checked when the probe is fetched through tiles())."""
        src, z, x, y = key
        pz = max((p for p in src.probe_zooms if p < z), default=None)
        if pz is None:
            return None
        return (src, pz, x >> (z - pz), y >> (z - pz))

    @staticmethod
    def _covered(probe):
        return probe is not None and not probe.uniform

    def _probe_status(self, key):
        """'covered' / 'outside' / 'unknown' from the key's coverage probes, coarsest first;
        starts the fetch of the first unknown one. Caller holds the lock."""
        src, z, x, y = key
        for pz in sorted(p for p in src.probe_zooms if p < z):
            p = (src, pz, x >> (z - pz), y >> (z - pz))
            if p not in self._lru:
                self._future(p)
                return "unknown"
            if not self._covered(self._lru[p]):
                return "outside"
        return "covered"

    def _future(self, key):
        """The fetch of key (started if needed). Caller holds the lock."""
        fut = self._inflight.get(key)
        if fut is None:
            fut = self._inflight[key] = self.pool.submit(self._load, key)
            fut.add_done_callback(lambda f: self._finish(key, f))
        return fut

    def tiles(self, keys):
        """{(source, z, x, y): Tile or None (404, or outside the coverage probe)}, fetched
        concurrently and deduplicated across callers. Raises ImageryError if a fetch failed."""
        keys = list(dict.fromkeys(keys))
        probes = {k: self._probe_key(k) for k in keys}
        known = self.tiles({p for p in probes.values() if p is not None}) if any(probes.values()) else {}
        out, futs = {}, {}
        with self._lock:
            for k in keys:
                if probes[k] is not None and not self._covered(known[probes[k]]):
                    out[k] = None           # the source has nothing in this area
                elif k in self._lru:
                    self._lru.move_to_end(k)
                    out[k] = self._lru[k]
                else:
                    futs[k] = self._future(k)
        err = None
        for k, fut in futs.items():
            try:
                out[k] = fut.result()
            except ImageryError as e:
                err = e
        if err is not None:
            raise err
        return out

    def prefetch(self, frame, x0, y0, size, px):
        """Start fetching the source tiles render(frame, x0, y0, size, px) will need, without
        waiting: the first source of the chain, or the next one where every tile of the
        previous source is already known to be missing (an unknown coverage probe is fetched
        first). Errors are left for render to meet."""
        t = size / px
        c = np.array([0.0, 0.5, 1.0]) * size
        E, N = np.meshgrid(x0 + c, y0 + c)
        lat, lon, _ = frame.enu_to_geodetic(E, N, 0.0)
        if np.abs(lat).max() > MAX_LAT:
            return
        zw = zoom_for(float(lat[1, 1]), t)
        for k, src in enumerate(self.chain):
            z = min(zw, src.max_zoom)
            if z < src.min_zoom:
                continue
            n = 2 ** z
            fx, fy = mercator_px(lat.ravel(), lon.ravel(), z)
            fx = fx - n * TILE * np.round((fx - fx[0]) / (n * TILE))
            tx0, tx1 = int(math.floor(fx.min() - 1)) // TILE, int(math.floor(fx.max() + 1)) // TILE
            ty0, ty1 = max(0, int(math.floor(fy.min() - 1)) // TILE), min(n - 1, int(math.floor(fy.max() + 1)) // TILE)
            if (tx1 - tx0 + 1) * (ty1 - ty0 + 1) > MAX_MOSAIC:
                return
            missing = True                  # every tile of this source known to be missing
            with self._lock:
                for ty in range(ty0, ty1 + 1):
                    for tx in range(tx0, tx1 + 1):
                        key = (src, z, tx % n, ty)
                        cover = self._probe_status(key)
                        if cover == "outside":
                            continue
                        if cover == "unknown":          # a probe is being fetched first
                            missing = False
                            continue
                        if key in self._lru:
                            a = self._lru[key]
                            missing &= a is None or (k < len(self.chain) - 1 and a.uniform)
                            continue
                        missing = False
                        self._future(key)
            if not missing:
                return

    def _finish(self, key, fut):
        with self._lock:
            if self._inflight.get(key) is fut:
                del self._inflight[key]
            if not fut.cancelled() and fut.exception() is None:
                self._lru[key] = fut.result()
                while len(self._lru) > self.mem_tiles:
                    self._lru.popitem(last=False)

    # --- sampling ----------------------------------------------------------------------------
    def _mosaic(self, src, z, fx, fy, last):
        """RGBA mosaic of src's tiles at zoom z covering global pixel coordinates fx, fy (A = 255
        where the source has data); returns (image, origin x, origin y, any data) or None if
        it would take more than MAX_MOSAIC tiles."""
        n = 2 ** z
        tx0, tx1 = int(math.floor(fx.min() - 1)) // TILE, int(math.floor(fx.max() + 1)) // TILE
        ty0 = max(0, int(math.floor(fy.min() - 1)) // TILE)
        ty1 = min(n - 1, int(math.floor(fy.max() + 1)) // TILE)
        nx, ny = tx1 - tx0 + 1, ty1 - ty0 + 1
        if nx * ny > MAX_MOSAIC or ny <= 0:
            return None
        keys = {(tx, ty): (src, z, tx % n, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)}
        got = self.tiles(keys.values())
        mosaic = np.zeros((ny * TILE, nx * TILE, 4), np.uint8)
        any_data = False
        for (tx, ty), k in keys.items():
            a = got[k]
            if a is None or (not last and a.uniform):
                continue                    # missing here: the next source fills it
            c, r = (tx - tx0) * TILE, (ty - ty0) * TILE
            mosaic[r:r + TILE, c:c + TILE, :3] = a.rgb
            mosaic[r:r + TILE, c:c + TILE, 3] = 255
            any_data = True
        return Image.fromarray(mosaic, "RGBA"), tx0 * TILE, ty0 * TILE, any_data

    def render(self, frame, x0, y0, size, px):
        """Texture of the ENU square [x0, x0+size] x [y0, y0+size] (anchor `frame`), px x px,
        row 0 = north edge, column 0 = west edge (texel centres at (i + 0.5) * size / px).
        Returns (uint8 [px, px, 3] after the tone curve, {source: texels}).

        The ENU -> lat/lon -> Web-Mercator mapping is evaluated on a MESH_CELLS² grid of cell
        corners and mapped bilinearly in between (Pillow's MESH transform, BILINEAR sampling):
        the mapping is smooth enough that this is exact to well under a texel (second-order
        error ~ R tan(lat) dphi² / 8 for a cell of dphi rad: ~4 m on a 131 km tile at 36 deg,
        0.01 of its 512 m texel; nothing measurable on near tiles). Each texel takes the first
        source of the chain whose four bilinear taps all have data."""
        cells = min(MESH_CELLS, px)
        cw = px // cells
        k = np.arange(cells + 1) * (size / cells)
        E, N = np.meshgrid(x0 + k, y0 + size - k)                 # [row (north first), col]
        lat, lon, _ = frame.enu_to_geodetic(E, N, 0.0)
        lat = np.clip(lat, -89.9, 89.9)
        zw = zoom_for(float(lat[cells // 2, cells // 2]), size / px)
        out = np.zeros((px, px, 3), np.uint8)
        filled = np.zeros((px, px), bool)
        used, error = {}, None
        for i, src in enumerate(self.chain):
            z = min(zw, src.max_zoom)
            if z < src.min_zoom:
                continue
            while True:
                fx, fy = mercator_px(lat, lon, z)
                w = TILE * 2 ** z
                fx = fx - w * np.round((fx - fx[0, 0]) / w)        # unwrap across the antimeridian
                try:
                    m = self._mosaic(src, z, fx, fy, i == len(self.chain) - 1)
                except ImageryError as e:   # this source is unreachable: the next one may do
                    error, m = e, False
                    break
                if m is not None or z <= src.min_zoom:
                    break
                z -= 1                      # footprint too large for the zoom: coarser
            if not m:
                continue
            im, ox, oy, any_data = m
            if not any_data:
                continue
            qx, qy = fx - ox, fy - oy
            mesh = [((c * cw, r * cw, (c + 1) * cw, (r + 1) * cw),
                     (qx[r, c], qy[r, c], qx[r + 1, c], qy[r + 1, c],
                      qx[r + 1, c + 1], qy[r + 1, c + 1], qx[r, c + 1], qy[r, c + 1]))
                    for r in range(cells) for c in range(cells)]
            res = im.transform((px, px), Image.MESH, mesh, resample=Image.BILINEAR)
            hit = np.asarray(res.getchannel("A")) == 255
            if not used and hit.all():      # the usual case: one source covers the texture
                out[:] = np.asarray(res.convert("RGB"))
                filled[:] = True
            else:
                hit &= ~filled
                np.copyto(out, np.asarray(res)[..., :3], where=hit[..., None])
                filled |= hit
            if hit.any():
                used[src.name] = int(hit.sum())
            if filled.all():
                break
        if not filled.all():
            if error is not None:           # not known to be uncovered: do not bake a fallback
                raise error
            out[~filled] = FALLBACK_RGB
            used["none"] = int((~filled).sum())
        return np.asarray(Image.fromarray(out, "RGB").point(self.lut_table)), used

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)


# --- texture gutter ----------------------------------------------------------------------
def gutter(px):
    """Border (texels per side) rendered around a tile's footprint. The engine samples material
    textures with a fixed WRAP sampler (shaders/common/bng.hlsl defaultSampler2D), so without a
    border every tile edge blends in the opposite edge's colour: a thin seam line along each
    tile boundary (seen in-game, v1.3). With g real-imagery texels around the footprint, mip
    levels 0 .. log2(g) are seam-free; deeper mips are only used where a texel is far below a
    pixel. 8 texels (3 % per side at 256 px) keeps levels 0-3 clean."""
    return max(1, min(8, px // 32))


def gutter_square(x0, y0, size, px):
    """(x0', y0', size') of the rendered square: the footprint plus gutter(px) texels per side,
    the footprint covering texels g .. px - g exactly."""
    g = gutter(px)
    t = size / (px - 2 * g)
    return x0 - g * t, y0 - g * t, px * t


def gutter_uv(planar, px):
    """UV0 of footprint coordinates in [0, 1] for a texture with a gutter (see gutter())."""
    g = gutter(px)
    return (g + np.asarray(planar, np.float64) * (px - 2 * g)) / px


# --- DDS ---------------------------------------------------------------------------------
DDS_MAGIC = b"DDS "
DDSD_CAPS, DDSD_HEIGHT, DDSD_WIDTH, DDSD_PIXELFORMAT = 0x1, 0x2, 0x4, 0x1000
DDSD_MIPMAPCOUNT, DDSD_LINEARSIZE = 0x20000, 0x80000
DDPF_FOURCC = 0x4
DDSCAPS_COMPLEX, DDSCAPS_TEXTURE, DDSCAPS_MIPMAP = 0x8, 0x1000, 0x400000
DDS_HEADER = 128                    # magic + 124-byte header


def dxt1_size(w, h):
    return max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * 8


def mip_chain(img):
    """[level 0, 1, ...] down to 1 x 1, each a 2 x 2 box average of the previous one."""
    levels = [img]
    while max(levels[-1].size) > 1:
        w, h = levels[-1].size
        levels.append(levels[-1].reduce((2 if w > 1 else 1, 2 if h > 1 else 1)))
    return levels


def encode_dds(rgb):
    """DDS bytes of an RGB image (uint8 [h, w, 3], power-of-two sides): DXT1 (BC1, no alpha)
    with the full mip chain."""
    img = Image.fromarray(np.ascontiguousarray(rgb), "RGB")
    w, h = img.size
    blobs = []
    for lv in mip_chain(img):
        b = io.BytesIO()
        lv.save(b, "DDS", pixel_format="DXT1")      # one level, Pillow's BC1 encoder
        data = b.getvalue()[DDS_HEADER:]
        assert len(data) == dxt1_size(*lv.size), (lv.size, len(data))
        blobs.append(data)
    flags = DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PIXELFORMAT | DDSD_MIPMAPCOUNT | DDSD_LINEARSIZE
    head = (DDS_MAGIC
            + struct.pack("<7I", 124, flags, h, w, dxt1_size(w, h), 0, len(blobs))
            + struct.pack("<11I", *((0,) * 11))
            + struct.pack("<2I4s5I", 32, DDPF_FOURCC, b"DXT1", 0, 0, 0, 0, 0)
            + struct.pack("<5I", DDSCAPS_TEXTURE | DDSCAPS_MIPMAP | DDSCAPS_COMPLEX, 0, 0, 0, 0))
    assert len(head) == DDS_HEADER
    return head + b"".join(blobs)


def write_dds(path, rgb):
    """encode_dds() to path atomically; returns the file size."""
    data = encode_dds(rgb)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return len(data)


def read_dds_info(data):
    """(width, height, mip count, fourcc, [level byte sizes]) of a DXT1 DDS (for tests/checks)."""
    if data[:4] != DDS_MAGIC:
        raise ValueError("not a DDS file")
    size, flags, h, w, linear, depth, mips = struct.unpack_from("<7I", data, 4)
    fourcc = data[84:88]
    mips = mips if flags & DDSD_MIPMAPCOUNT else 1
    sizes = []
    for k in range(max(1, mips)):
        sizes.append(dxt1_size(max(1, w >> k), max(1, h >> k)))
    return w, h, mips, fourcc, sizes
