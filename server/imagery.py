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
sent), 404s remembered as <y>.404 markers (as dem.py does; they expire after the host's miss_ttl,
90 days), decoded tiles in an in-memory LRU. Downloads run on a small pool (FETCH_WORKERS <= 6)
through a netfetch.Fetcher: the host's rate limit, concurrency cap, retries and circuit breaker
(keyed by host, so eox2023 and eox2016 share one), one persistent HTTPS connection per thread and
host. A source whose host cannot be asked fails at once (ImageryError), and render() takes the
next source. prefetch() asks with kind="prefetch" (dropped when the host's bucket is low; render
then fetches the tile on demand).

Output: DDS DXT1 with a full mip chain (write_dds). Pillow 12 writes DXT1 but no mipmaps (its
header always says 0 mips), so every level is box-downsampled and BC1-encoded by Pillow separately
and the levels are concatenated under our own header (DDSD_MIPMAPCOUNT, DDSCAPS_MIPMAP|COMPLEX).
"""
import io
import logging
import math
import os
import struct
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np
from PIL import Image

import dem as demmod
import netfetch

log = logging.getLogger("earthd.imagery")

TILE = 256
MAX_LAT = demmod.MAX_LAT
EARTH_CIRC = 40075016.686          # m, equatorial circumference (Web-Mercator scale)
FETCH_WORKERS = 6
MEM_TILES = 512                    # decoded 256² RGB tiles kept in memory (~100 MB)
# Attempts, retry delays, timeouts and the "source unreachable" backoff are the host's netfetch
# policy now: once its circuit is open, its tiles fail at once instead of each paying the attempts
# and timeouts (offline, every texture took ~10 s before the backoff existed), and fall back to
# the next source or, with none left, to the palette material.
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
                      14, "EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH (Contains modified "
                          "Copernicus Sentinel data 2023), CC BY-NC-SA 4.0"),
    "eox2016": Source("eox2016", "https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless_3857/default/g/{z}/{y}/{x}.jpg",
                      14, "EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH (Contains modified "
                          "Copernicus Sentinel data 2016 & 2017), CC BY 4.0"),
}
DEFAULT_CHAIN = ("usgs", "eox2023", "eox2016")
# The line the mod shows (V1_3_SPEC §C), for the default chain: every source's own credit, EOX's in
# its June 2026 wording (docs/research/2026-10-10-rate-limits.md: "EOxCloudless https://cloudless.eox.at
# by EOX IT Services GmbH (Contains modified Copernicus Sentinel data 2023)", 2016: "... 2016 & 2017").
ATTRIBUTION = "Imagery: " + "; ".join(SOURCES[n].attribution for n in DEFAULT_CHAIN)


class ImageryError(IOError):
    """Imagery could not be fetched (network or server errors, not a missing tile). deferred: a
    prefetch / background request the limiter dropped (a demand fetch may still get it)."""

    def __init__(self, msg, deferred=False):
        super().__init__(msg)
        self.deferred = deferred


# netfetch errors of a non-demand request that a demand request would not get
_DEFERRED = (netfetch.Deferred, netfetch.PolicyDenied, netfetch.BudgetExceeded)


def parse_chain(text):
    """--imagery value -> tuple of source names, or None for 'off'."""
    if text.strip().lower() == "off":
        return None
    chain = tuple(s.strip().lower() for s in text.split(",") if s.strip())
    bad = [s for s in chain if s not in SOURCES]
    if bad or not chain:
        raise ValueError(f"unknown imagery source(s) {bad or text!r}; choose from {', '.join(SOURCES)} or off")
    return chain


def tone_lut(strength=1.0, lift=1.0):
    """Mild global tone curve as a 256-entry LUT (applied per channel, sRGB values).

    Satellite images have the sun's shading and shadows baked in, and the engine lights the
    ground again, so the curve lifts the deepest shadows a little (+0.06 at black) and pulls
    the brightest highlights down (-0.10 at white, so bare rock and sand do not glow under the
    sun) while leaving mid-tones almost unchanged (-0.005 at 0.5). strength 0 = identity; the
    curve stays monotonic for strength <= 3. lift scales the shadow lift alone (de-shadowed
    texels, deshadow.FULL_TONE_LIFT)."""
    x = np.arange(256) / 255.0
    y = x + strength * (lift * 0.06 * (1 - x) ** 3 - 0.10 * x ** 3)
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


_SAME = object()                    # colour-match LRU marker: the tile is unchanged


class Imagery:
    def __init__(self, cache_dir, chain=DEFAULT_CHAIN, fetch_workers=FETCH_WORKERS, mem_tiles=MEM_TILES,
                 tone=1.0, deshadow=None, colormatch=None, fetcher=None):
        """deshadow: an optional deshadow.Deshadow (--deshadow lambert|full): _mosaic relights its
        source's tiles; None = the imagery as served (byte-identical textures).
        colormatch: an optional colormatch.ColorMatch (--colormatch s2): _mosaic removes the colour
        seams of its source's tiles, after the relight and before the tone curve; None = off.
        fetcher: the server's netfetch.Fetcher (shared limits); None = one of its own."""
        self.cache_dir = cache_dir
        self.chain = tuple(SOURCES[s] if isinstance(s, str) else s for s in chain)
        self.fetcher = fetcher if fetcher is not None else netfetch.Fetcher()
        self.fetcher.register_chain("imagery", [s.url for s in self.chain])
        self.pool = ThreadPoolExecutor(min(fetch_workers, FETCH_WORKERS), thread_name_prefix="img-fetch")
        self.mem_tiles = mem_tiles
        self.lut = tone_lut(tone)
        self.lut_table = self.lut.tolist() * 3          # Image.point: one LUT per band
        self.deshadow = deshadow
        if deshadow is not None:
            deshadow.bind(self)
            self.lut_ds = tone_lut(tone, deshadow.tone_lift)    # texels of relit source tiles
        self.colormatch = colormatch
        if colormatch is not None:
            colormatch.bind(self)
        self._cm_lru = OrderedDict()        # (source, z, x, y, "cm1", relit) -> corrected RGB, or the input
        self._lru = OrderedDict()           # (source, z, x, y) -> Tile, or None (404)
        self._inflight = {}
        self._lock = threading.RLock()     # RLock: a done future's callback runs at once
        self._local = threading.local()     # per fetch thread: the kind of the request being loaded
        self.stats = {}                     # counters ("usgs:net", "eox2023:404", ...), and fetch times
        self.fetch_ms = []                  # network fetch latencies (ms), for the log / measurements

    def attribution(self):
        """Attribution line for the sources in use (with --colormatch, its reference layers too:
        their data shape the USGS colours even where they are not in the chain)."""
        names = [s.name for s in self.chain]
        extra = [SOURCES[n] for n in getattr(self.colormatch, "refs", ()) if n not in names]
        if names == list(DEFAULT_CHAIN) and not extra:
            return ATTRIBUTION
        return "Imagery: " + "; ".join(s.attribution for s in list(self.chain) + extra)

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
        extra = [o.summary() for o in (self.deshadow, self.colormatch) if o is not None]
        return "; ".join([lat, str(st)] + extra)

    def _paths(self, src, z, x, y):
        d = os.path.join(self.cache_dir, src.name, str(z), str(x))
        return os.path.join(d, f"{y}.jpg"), os.path.join(d, f"{y}.404")

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
        url = src.url.format(z=z, x=x, y=y)
        if self.fetcher.miss_alive(miss, url):
            return None
        kind = getattr(self._local, "kind", "demand")
        try:
            r = self.fetcher.get(url, kind=kind)
        except netfetch.NotFound:               # outside the source's coverage
            os.makedirs(os.path.dirname(miss), exist_ok=True)
            open(miss, "wb").close()            # (re)written: its mtime starts the marker's TTL
            self._count(src.name + ":404")
            return None
        except netfetch.FetchError as e:
            deferred = kind != "demand" and isinstance(e, _DEFERRED)
            self._count(src.name + (":deferred" if deferred else ":skipped" if isinstance(e, netfetch.HostUnavailable)
                                    else ":error"))
            raise ImageryError(f"imagery fetch failed: {url}: {e}", deferred) from None
        data = r.body
        with self._lock:
            self.fetch_ms.append(r.elapsed * 1000)
            del self.fetch_ms[:-2000]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{threading.get_ident()}.tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        if os.path.exists(miss):                # an expired marker: the source has data there now
            os.remove(miss)
        self._count(src.name + ":net")
        return data

    def _load_kind(self, key, kind):
        """_load(key) in a pool thread, its requests made as `kind` (subclasses override _load(key))."""
        self._local.kind = kind
        try:
            return self._load(key)
        finally:
            self._local.kind = "demand"

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
                self._future(p, "prefetch")
                return "unknown"
            if not self._covered(self._lru[p]):
                return "outside"
        return "covered"

    def _future(self, key, kind="demand", stale=None):
        """The fetch of key (started if needed, or when the one in flight is `stale`, a finished
        fetch whose done callback has not run yet). Caller holds the lock."""
        fut = self._inflight.get(key)
        if fut is None or fut is stale:
            fut = self._inflight[key] = self.pool.submit(self._load_kind, key, kind)
            fut.add_done_callback(lambda f: self._finish(key, f))
        return fut

    def _result(self, key, fut, kind):
        """fut's tile; a demand caller that met a dropped prefetch fetches the tile itself (one fetch
        shared by every caller that met the same dropped one)."""
        while True:                         # (a demand fetch is never dropped: this ends)
            try:
                return fut.result()
            except ImageryError as e:
                if not e.deferred or kind != "demand":
                    raise
            with self._lock:
                fut = self._future(key, stale=fut)

    def tiles(self, keys, kind="demand"):
        """{(source, z, x, y): Tile or None (404, or outside the coverage probe)}, fetched
        concurrently and deduplicated across callers. Raises ImageryError if a fetch failed.
        kind: the netfetch kind of the requests ("background": de-shadow region fits)."""
        keys = list(dict.fromkeys(keys))
        probes = {k: self._probe_key(k) for k in keys}
        known = self.tiles({p for p in probes.values() if p is not None}, kind) if any(probes.values()) else {}
        out, futs = {}, {}
        with self._lock:
            for k in keys:
                if probes[k] is not None and not self._covered(known[probes[k]]):
                    out[k] = None           # the source has nothing in this area
                elif k in self._lru:
                    self._lru.move_to_end(k)
                    out[k] = self._lru[k]
                else:
                    futs[k] = self._future(k, kind)
        err = None
        for k, fut in futs.items():
            try:
                out[k] = self._result(k, fut, kind)
            except ImageryError as e:
                err = e
        if err is not None:
            raise err
        return out

    def prefetch(self, frame, x0, y0, size, px):
        """Start fetching the source tiles render(frame, x0, y0, size, px) will need, without
        waiting: the first source of the chain, or the next one where every tile of the
        previous source is already known to be missing (an unknown coverage probe is fetched
        first). Errors are left for render to meet. Skips a source whose host's circuit is open
        (render fails over past it) and stops at one whose host takes no prefetch now (policy,
        budget, bucket below half): render fetches those on demand."""
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
            host = netfetch.host_of(src.url)
            if not self.fetcher.available(host):
                continue                    # circuit open: render fails over to the next source, warm that
            # --colormatch: their regions' seams, in the background (not under the lock: it may read a
            # field from disk); before the prefetch check, so a host that takes no prefetch right now
            # still gets the regions render will need queued (they fetch as kind "background")
            if self.colormatch is not None:
                for ty in range(ty0, ty1 + 1):
                    for tx in range(tx0, tx1 + 1):
                        self.colormatch.request((src, z, tx % n, ty))
            if not self.fetcher.allow(host, "prefetch"):
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
                        self._future(key, "prefetch")
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
    def _mosaic(self, src, z, fx, fy, last, pending=None, corrected=None):
        """RGBA mosaic of src's tiles at zoom z covering global pixel coordinates fx, fy (A = 255
        where the source has data, 254 where a de-shadowed tile replaced it); returns (image,
        origin x, origin y, any data) or None if it would take more than MAX_MOSAIC tiles.
        pending: a set that collects the colour-match regions not ready yet (tiles left raw);
        corrected: a set that collects the keys of the tiles the colour match changed."""
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
            relit = None if self.deshadow is None else self.deshadow.relight(k, a, self)
            rgb = a.rgb if relit is None else relit
            if self.colormatch is not None and not a.uniform and self.colormatch.handles(k):
                cm = self._colormatched(k, rgb, relit is not None, pending)
                if cm is not rgb and corrected is not None:
                    corrected.add(k)
                rgb = cm
            mosaic[r:r + TILE, c:c + TILE, :3] = rgb
            mosaic[r:r + TILE, c:c + TILE, 3] = 255 if relit is None else 254
            any_data = True
        return Image.fromarray(mosaic, "RGBA"), tx0 * TILE, ty0 * TILE, any_data

    def _colormatched(self, key, rgb, relit, pending):
        """The colour-seam corrected RGB of a source tile (raw or relit: raw -> de-shadow -> colour
        gain -> tone curve), from a second LRU; the raw Tile in _lru stays as it is."""
        ck = key + ("cm1", relit)
        with self._lock:
            v = self._cm_lru.get(ck)
            if v is not None:
                self._cm_lru.move_to_end(ck)
                return rgb if v is _SAME else v
        out, info = self.colormatch.apply(key, rgb, self)
        if out is self.colormatch.PENDING:     # its region is not ready: raw this time
            if pending is not None:
                pending.add(info)
            return rgb
        if info:                            # cacheable (not a neighbour-fetch fallback)
            with self._lock:
                self._cm_lru[ck] = _SAME if out is None else out
                while len(self._cm_lru) > self.mem_tiles:
                    self._cm_lru.popitem(last=False)
        return rgb if out is None else out

    def render(self, frame, x0, y0, size, px, pending=None):
        """Texture of the ENU square [x0, x0+size] x [y0, y0+size] (anchor `frame`), px x px,
        row 0 = north edge, column 0 = west edge (texel centres at (i + 0.5) * size / px).
        Returns (uint8 [px, px, 3] after the tone curve, {source: texels}).

        The ENU -> lat/lon -> Web-Mercator mapping is evaluated on a MESH_CELLS² grid of cell
        corners and mapped bilinearly in between (Pillow's MESH transform, BILINEAR sampling):
        the mapping is smooth enough that this is exact to well under a texel (second-order
        error ~ R tan(lat) dphi² / 8 for a cell of dphi rad: ~4 m on a 131 km tile at 36 deg,
        0.01 of its 512 m texel; nothing measurable on near tiles). Each texel takes the first
        source of the chain whose four bilinear taps all have data. pending: a set that collects the
        colour-match regions (--colormatch) that were not ready, i.e. whose tiles stayed raw here.
        The counts also carry "none" (fallback colour), "deshadow" (relit texels) and, with
        --colormatch, "colormatch": the texels of the sources whose mosaic had tiles the colour match
        changed (their colours then come partly from its S2 references: the data credits name them)."""
        cells = min(MESH_CELLS, px)
        cw = px // cells
        k = np.arange(cells + 1) * (size / cells)
        E, N = np.meshgrid(x0 + k, y0 + size - k)                 # [row (north first), col]
        lat, lon, _ = frame.enu_to_geodetic(E, N, 0.0)
        lat = np.clip(lat, -89.9, 89.9)
        zw = zoom_for(float(lat[cells // 2, cells // 2]), size / px)
        out = np.zeros((px, px, 3), np.uint8)
        filled = np.zeros((px, px), bool)
        relit = None if self.deshadow is None else np.zeros((px, px), bool)
        used, error = {}, None
        for i, src in enumerate(self.chain):
            cm_keys = set() if self.colormatch is not None else None
            z = min(zw, src.max_zoom)
            if z < src.min_zoom:
                continue
            while True:
                fx, fy = mercator_px(lat, lon, z)
                w = TILE * 2 ** z
                fx = fx - w * np.round((fx - fx[0, 0]) / w)        # unwrap across the antimeridian
                try:
                    m = self._mosaic(src, z, fx, fy, i == len(self.chain) - 1, pending, cm_keys)
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
            alpha = np.asarray(res.getchannel("A"))
            hit = alpha == 255 if relit is None else alpha >= 254
            if not used and hit.all():      # the usual case: one source covers the texture
                out[:] = np.asarray(res.convert("RGB"))
                filled[:] = True
                if relit is not None:
                    relit[:] = alpha == 254
            else:
                hit &= ~filled
                np.copyto(out, np.asarray(res)[..., :3], where=hit[..., None])
                filled |= hit
                if relit is not None:
                    relit |= hit & (alpha == 254)
            if hit.any():
                used[src.name] = int(hit.sum())
                if cm_keys:
                    used["colormatch"] = used.get("colormatch", 0) + int(hit.sum())
            if filled.all():
                break
        if not filled.all():
            if error is not None:           # not known to be uncovered: do not bake a fallback
                raise error
            out[~filled] = FALLBACK_RGB
            used["none"] = int((~filled).sum())
        if relit is not None and relit.any():
            used["deshadow"] = int(relit.sum())
            res = self.lut[out]
            res[relit] = self.lut_ds[out[relit]]
            return res, used
        return np.asarray(Image.fromarray(out, "RGB").point(self.lut_table)), used

    def close(self):
        if self.deshadow is not None:
            self.deshadow.close()
        if self.colormatch is not None:
            self.colormatch.close()
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
