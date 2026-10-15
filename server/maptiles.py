"""Map tiles for the in-game world map (mod ui/ui-vue/mods/beamngEarth/EarthMap.vue).

The game's UI (CEF) may not reach the internet, so the companion serves 256 px Web Mercator tiles
z/x/y over a small HTTP endpoint bound to 127.0.0.1 only (its port goes in hello.mapPort). The
tiles come from the server's own imagery chain (imagery.py: the same sources, the same disk cache
and network limits, and the same attribution the mod already shows):

  - z <= EOX_FIRST_MAX_Z: Sentinel-2 cloudless (EOX) first: one consistent global picture, and no
    USGS coverage probes for the world view;
  - above: the chain's order (USGS first in the US, z <= 16; EOX elsewhere, native to z14);
  - above a source's max zoom the ancestor tile at that zoom is cropped and scaled up (overzoom),
    so the map zooms to MAX_Z everywhere.

A source with no data there (404, outside its coverage probe, a blank tile) passes to the next one.

HTTP (GET only, one request per connection):
  /tiles/{z}/{x}/{y}.jpg   -> image/jpeg; 404 no imagery anywhere; 503 fetch failed (retry later)
  /attribution             -> application/json {"attribution": "..."}
  /health                  -> "ok"
No Google or Cesium tiles; every source is credited by imagery.attribution().
"""
import asyncio
import io
import json
import logging

import numpy as np
from PIL import Image

import imagery as imagerymod

log = logging.getLogger("earthd.maptiles")

MAX_Z = 18                  # the deepest zoom served (overzoomed above the sources' own)
EOX_FIRST_MAX_Z = 12        # up to here EOX goes before the rest of the chain
JPEG_QUALITY = 85
# netfetch kind: "demand", what the player is looking at (2026-10-15: as "prefetch" the map's tiles were dropped
# while the terrain streamed after a teleport, "bucket low", and the map stayed blank squares for a minute; the
# UI asks for at most 48 at a time, so the terrain still gets most of the budget)
FETCH_KIND = "demand"
MAX_REQUEST = 8192          # bytes of request head accepted
READ_TIMEOUT_S = 10


class BadTile(ValueError):
    pass


def check_tile(z, x, y):
    """Validated ints (z, x, y) or BadTile."""
    try:
        z, x, y = int(z), int(x), int(y)
    except (TypeError, ValueError):
        raise BadTile("not a tile") from None
    if not 0 <= z <= MAX_Z:
        raise BadTile(f"zoom must be 0..{MAX_Z}")
    n = 1 << z
    if not (0 <= x < n and 0 <= y < n):
        raise BadTile("tile outside the world")
    return z, x, y


def parse_path(path):
    """'/tiles/z/x/y.jpg' (or .png / no extension, a query string ignored) -> (z, x, y);
    BadTile for anything else."""
    path = path.split("?", 1)[0]
    parts = path.strip("/").split("/")
    if len(parts) != 4 or parts[0] != "tiles":
        raise BadTile("not a tile path")
    y = parts[3].rsplit(".", 1)[0] if "." in parts[3] else parts[3]
    return check_tile(parts[1], parts[2], y)


def crop_up(rgb, dz, x, y):
    """The part of an ancestor tile (dz levels up) covering tile x, y, scaled back to 256 px."""
    if dz <= 0:
        return rgb
    n = 1 << dz
    span = rgb.shape[0] // n
    if span < 1:                # deeper than one source pixel: take the pixel
        span = 1
    ox, oy = (x % n) * rgb.shape[1] // n, (y % n) * rgb.shape[0] // n
    part = rgb[oy:oy + span, ox:ox + span]
    im = Image.fromarray(np.ascontiguousarray(part)).resize((imagerymod.TILE, imagerymod.TILE), Image.BILINEAR)
    return np.asarray(im)


def encode_jpeg(rgb):
    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(rgb, dtype=np.uint8)).save(buf, "JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


class MapTiles:
    """Map tiles from an imagery.Imagery (or anything with .chain, .tiles(keys, kind) and
    .attribution())."""

    def __init__(self, imagery, kind=FETCH_KIND):
        self.imagery = imagery
        self.kind = kind
        self.stats = {"served": 0, "missing": 0, "failed": 0, "bad": 0}

    def order(self, z):
        chain = list(self.imagery.chain)
        if z <= EOX_FIRST_MAX_Z:
            chain.sort(key=lambda s: 0 if s.name.startswith("eox") else 1)     # stable: chain order kept
        return chain

    def tile(self, z, x, y):
        """(jpeg bytes, source name), or None where no source has imagery. Raises
        imagery.ImageryError when a fetch failed (and no later source had the tile)."""
        z, x, y = check_tile(z, x, y)
        err = None
        for src in self.order(z):
            if z < src.min_zoom:
                continue
            sz = min(z, src.max_zoom)
            dz = z - sz
            key = (src, sz, x >> dz, y >> dz)
            if not imagerymod.in_bounds(*key):
                continue                    # a regional source elsewhere
            try:
                t = self.imagery.tiles([key], self.kind).get(key)
            except imagerymod.ImageryError as e:
                err = e
                continue
            if t is None or t.uniform:
                continue
            return encode_jpeg(crop_up(t.rgb, dz, x, y)), src.name
        if err is not None:
            raise err
        return None

    def attribution(self):
        return self.imagery.attribution()

    # --- HTTP ------------------------------------------------------------------------------
    def respond(self, method, path):
        """(status, content type, body) for one request; runs in a worker thread (fetches block)."""
        if method not in ("GET", "HEAD"):
            return 405, "text/plain", b"GET only"
        p = path.split("?", 1)[0]
        if p == "/health":
            return 200, "text/plain", b"ok"
        if p == "/attribution":
            return 200, "application/json", json.dumps({"attribution": self.attribution()}).encode()
        try:
            z, x, y = parse_path(p)
        except BadTile as e:
            self.stats["bad"] += 1
            return 404, "text/plain", str(e).encode()
        try:
            got = self.tile(z, x, y)
        except imagerymod.ImageryError as e:
            self.stats["failed"] += 1
            return 503, "text/plain", str(e).encode()
        except Exception as e:                  # never take the server down for a map tile
            log.exception("map tile %s/%s/%s failed", z, x, y)
            self.stats["failed"] += 1
            return 500, "text/plain", str(e).encode()
        if got is None:
            self.stats["missing"] += 1
            return 404, "text/plain", b"no imagery here"
        self.stats["served"] += 1
        return 200, "image/jpeg", got[0]

    async def handle(self, reader, writer):
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), READ_TIMEOUT_S)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            writer.close()
            return
        try:
            line = head.split(b"\r\n", 1)[0].decode("latin-1")
            method, path, _ = line.split(" ", 2)
        except ValueError:
            method, path = "", ""
        if method and path.startswith("/"):
            status, ctype, body = await asyncio.get_running_loop().run_in_executor(
                None, self.respond, method, path)
        else:
            status, ctype, body = 400, "text/plain", b"bad request"
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed",
                  500: "Internal Server Error", 503: "Service Unavailable"}.get(status, "")
        hdr = (f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
               "Access-Control-Allow-Origin: *\r\n"
               f"Cache-Control: {'max-age=86400' if status == 200 else 'no-store'}\r\n"
               "Connection: close\r\n\r\n")
        try:
            writer.write(hdr.encode("latin-1") + (b"" if method == "HEAD" else body))
            await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()

    async def start(self, port=0, host="127.0.0.1"):
        """The HTTP server on host:port (0: any free port); returns (asyncio server, port)."""
        srv = await asyncio.start_server(self.handle, host, port, limit=MAX_REQUEST)
        return srv, srv.sockets[0].getsockname()[1]
