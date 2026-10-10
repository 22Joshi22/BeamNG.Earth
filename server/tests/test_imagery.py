"""server/imagery.py (docs/V1_3_SPEC.md §A): reprojection (a known lat/lon lands on the right
texel), the source chain with coverage detection (404 / blank tiles), the disk cache with
remembered 404s, unreachable sources, the tone curve and the DXT1 DDS with its mip chain.
Synthetic tile servers, no network, except NetworkImageryTest (a few real tiles, cached in
<tmp>/beamng-earth-test-cache/imagery; skipped when offline)."""
import io
import math
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import geodesy as g  # noqa: E402
import imagery  # noqa: E402

LAT0, LON0 = 36.5785, -118.2923
CACHE = os.path.join(tempfile.gettempdir(), "beamng-earth-test-cache", "imagery")
URL = re.compile(r"/(\d+)/(\d+)/(\d+)(?:\.jpg)?$")


def png(a):
    b = io.BytesIO()
    Image.fromarray(np.asarray(a, np.uint8), "RGB").save(b, "PNG")
    return b.getvalue()


def background(z, x, y):
    """A non-uniform dark pattern (so a tile is never 'blank')."""
    i = np.arange(256)
    return np.stack(np.broadcast_arrays(np.full((256, 256), 20), (i[None, :] + x) % 40 + 10,
                                        (i[:, None] + y) % 40 + 10), -1).astype(np.uint8)


class FakeNet(imagery.Imagery):
    """Imagery whose HTTP layer is a function host_name -> (z, x, y) -> (status, body)."""

    def __init__(self, cache_dir, serve, chain=imagery.DEFAULT_CHAIN, **kw):
        super().__init__(cache_dir, chain, **kw)
        self.serve = serve
        self.requests = []

    def _http_get(self, url):
        name = next(n for n, s in imagery.SOURCES.items() if url.startswith(s.url.split("{z}")[0]))
        z, a, b = (int(v) for v in URL.search(url).groups())
        x, y = (b, a) if name == "usgs" else (b, a)        # both are .../{z}/{y}/{x}
        self.requests.append((name, z, x, y))
        return self.serve(name, z, x, y)


class ReprojectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def landmark_server(self, lat, lon):
        """Tiles with a 3 x 3 white mark centred on the pixel that contains (lat, lon)."""
        def serve(name, z, x, y):
            if name != "usgs":
                return 404, b""
            a = background(z, x, y)
            fx, fy = imagery.mercator_px(lat, lon, z)
            px, py = int(math.floor(float(fx))) - 256 * x, int(math.floor(float(fy))) - 256 * y
            a[max(0, py - 1):max(0, py + 2), max(0, px - 1):max(0, px + 2)] = 255
            return 200, png(a)
        return serve

    def check(self, frame, e, n, x0, y0, size, px):
        lat, lon, h = frame.enu_to_geodetic(e, n, 0.0)
        im = FakeNet(tempfile.mkdtemp(dir=self.tmp.name), self.landmark_server(float(lat), float(lon)),
                     chain=("usgs",), tone=0.0)
        rgb, used = im.render(frame, x0, y0, size, px)
        self.assertEqual(set(used), {"usgs"})
        t = size / px
        # The mark is centred on the source pixel containing the landmark: map that pixel's
        # centre back (inverse Mercator, then into the frame) to get where it must appear.
        z = max(r[1] for r in im.requests)
        W = 256 * 2 ** z
        fx, fy = imagery.mercator_px(float(lat), float(lon), z)
        cx_px, cy_px = math.floor(float(fx)) + 0.5, math.floor(float(fy)) + 0.5
        clon = cx_px / W * 360.0 - 180.0
        clat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * cy_px / W))))
        ce, cn, _ = frame.geodetic_to_enu(clat, clon, float(h))
        col, row = (ce - x0) / t - 0.5, (y0 + size - cn) / t - 0.5      # texel centres at +0.5
        lum = rgb[..., 0].astype(float) - 20
        w = np.clip(lum, 0, None)
        rr, cc = np.mgrid[0:px, 0:px]
        cy, cx = (w * rr).sum() / w.sum(), (w * cc).sum() / w.sum()
        # The 3 x 3 mark is symmetric around that centre, so its centroid sits on it (a
        # half-pixel convention error would move it by ~0.5 source pixel = ~1 m here).
        self.assertLess(math.hypot((cx - col) * t, (cy - row) * t), 0.2, (cx, cy, col, row))
        self.assertTrue(abs(cx * t - (e - x0)) < 2.5 and abs(cy * t - (y0 + size - n)) < 2.5)
        return im

    def test_landmark_on_its_texel(self):
        f = g.EnuFrame(LAT0, LON0)
        im = self.check(f, 123.4, -56.7, 64.0, -128.0, 128.0, 128)
        self.assertEqual({r[1] for r in im.requests}, {10, 12, 16})    # 1 m texels: max zoom (+ probes)
        # a far-off footprint in another frame (curvature / meridian convergence matter there)
        f2 = g.EnuFrame(LAT0 + 0.15, LON0 - 0.2)
        self.check(f2, 17890.0, -16720.0, 17856.0, -16768.0, 64.0, 64)

    def test_orientation(self):
        """Row 0 is north, column 0 is west: a mark north-east of the centre shows up top right."""
        f = g.EnuFrame(LAT0, LON0)
        self.check(f, 50.0, 40.0, 0.0, 0.0, 64.0, 64)

    def test_zoom_choice(self):
        self.assertEqual(imagery.zoom_for(LAT0, imagery.ground_res(LAT0, 15)), 15)
        self.assertEqual(imagery.zoom_for(LAT0, imagery.ground_res(LAT0, 9) * 1.3), 9)
        self.assertEqual(imagery.zoom_for(0.0, 0.01), 19)


class ChainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        imagery.RETRY_DELAY, self._delay = 0.0, imagery.RETRY_DELAY

    def tearDown(self):
        imagery.RETRY_DELAY = self._delay
        self.tmp.cleanup()

    @staticmethod
    def colour(name):
        return {"usgs": (200, 30, 30), "eox2023": (30, 200, 30), "eox2016": (30, 30, 200)}[name]

    def tile(self, name, z, x, y, uniform=False):
        a = np.zeros((256, 256, 3), np.uint8) + np.array(self.colour(name), np.uint8)
        if not uniform:
            a[::2, ::2] += 6
        return 200, png(a)

    def render(self, im, x0=0.0):
        rgb, used = im.render(g.EnuFrame(LAT0, LON0), x0, 0.0, 256.0, 64)
        return rgb, used

    def test_fallback_on_404_and_blank(self):
        def serve(name, z, x, y):
            if name == "usgs":
                return (404, b"") if x % 2 else self.tile(name, z, x, y, uniform=True)   # white-tile style
            return self.tile(name, z, x, y)
        im = FakeNet(self.tmp.name, serve, tone=0.0)
        rgb, used = self.render(im)
        self.assertEqual(set(used), {"eox2023"})
        self.assertLess(np.abs(rgb.reshape(-1, 3).mean(0) - (31.5, 201.5, 31.5)).max(), 2)
        # usgs: only its z10 coverage probe (blank / 404 here); eox at its max z14
        self.assertEqual({(r[0], r[1]) for r in im.requests}, {("usgs", 10), ("eox2023", 14)})

    def test_coverage_probe(self):
        """usgs tiles are only asked for where their z10 and z12 ancestors have data, and never
        below z10 (its global low-zoom basemap would differ from EOX on the far rings)."""
        def serve(name, z, x, y):
            if name == "usgs" and z == 12:
                return (404, b"") if self.outside else self.tile(name, z, x, y)
            return self.tile(name, z, x, y)
        for outside in (True, False):
            with self.subTest(outside=outside):
                self.outside = outside
                im = FakeNet(tempfile.mkdtemp(dir=self.tmp.name), serve, tone=0.0)
                _, used = self.render(im)
                zs = {(r[0], r[1]) for r in im.requests}
                if outside:     # the probes, no 404 storm at z15
                    self.assertEqual(zs, {("usgs", 10), ("usgs", 12), ("eox2023", 14)})
                    self.assertEqual(set(used), {"eox2023"})
                else:           # 4 m texels: usgs at z15 (3.9 m pixels here)
                    self.assertEqual(zs, {("usgs", 10), ("usgs", 12), ("usgs", 15)})
                    self.assertEqual(set(used), {"usgs"})
        # a far tile with 600 m texels (z8) never asks usgs
        im = FakeNet(tempfile.mkdtemp(dir=self.tmp.name), serve, tone=0.0)
        _, used = im.render(g.EnuFrame(LAT0, LON0), 0.0, 0.0, 153600.0, 256)
        self.assertEqual(set(used), {"eox2023"})
        self.assertFalse([r for r in im.requests if r[0] == "usgs"])

    def test_blank_last_source_is_used(self):
        """EOX draws the open ocean as one uniform colour: accepted from the last source."""
        def serve(name, z, x, y):
            return (404, b"") if name == "usgs" else self.tile(name, z, x, y, uniform=True)
        rgb, used = self.render(FakeNet(self.tmp.name, serve, tone=0.0))
        self.assertEqual(set(used), {"eox2016"})
        self.assertTrue((rgb == self.colour("eox2016")).all())

    def test_nothing_anywhere(self):
        rgb, used = self.render(FakeNet(self.tmp.name, lambda *a: (404, b""), tone=0.0))
        self.assertEqual(set(used), {"none"})
        self.assertTrue((rgb == imagery.FALLBACK_RGB).all())

    def test_disk_cache_and_remembered_404(self):
        def serve(name, z, x, y):
            return (404, b"") if name == "usgs" else self.tile(name, z, x, y)
        im = FakeNet(self.tmp.name, serve, tone=0.0)
        a, _ = self.render(im)
        n = len(im.requests)
        self.assertTrue(n)
        b, _ = self.render(im)                     # memory LRU: no requests
        self.assertEqual(len(im.requests), n)

        def offline(*a):
            raise AssertionError("network used")
        im2 = FakeNet(self.tmp.name, offline, tone=0.0)
        c, used = self.render(im2)                 # disk: tiles and 404 markers
        self.assertEqual(im2.requests, [])
        self.assertTrue((a == b).all() and (a == c).all())
        self.assertEqual(set(used), {"eox2023"})
        stats = im2.stats_snapshot()
        self.assertGreater(stats.get("eox2023:disk", 0), 0)
        markers = [f for _, _, fs in os.walk(os.path.join(self.tmp.name, "usgs")) for f in fs if f.endswith(".404")]
        self.assertTrue(markers)

    def test_unreachable_source_falls_back(self):
        def serve(name, z, x, y):
            if name == "usgs":
                return 503, b"busy"
            return self.tile(name, z, x, y)
        rgb, used = self.render(FakeNet(self.tmp.name, serve, tone=0.0))
        self.assertEqual(set(used), {"eox2023"})

    def test_all_unreachable_raises(self):
        def serve(*a):
            raise OSError("network down")
        with self.assertRaises(imagery.ImageryError):
            self.render(FakeNet(self.tmp.name, serve, tone=0.0))
        # nothing was remembered as missing
        self.assertFalse([f for _, _, fs in os.walk(self.tmp.name) for f in fs])

    def test_unreachable_source_backs_off(self):
        """After a fetch failed every attempt, the source is skipped (no requests, no retry
        delays) for SOURCE_BACKOFF seconds; then it is asked again."""
        up = {"usgs": False}

        def serve(name, z, x, y):
            if name == "usgs" and not up["usgs"]:
                raise OSError("network down")
            return self.tile(name, z, x, y)
        im = FakeNet(self.tmp.name, serve, tone=0.0)
        _, used = self.render(im)
        self.assertEqual(set(used), {"eox2023"})
        n = sum(1 for r in im.requests if r[0] == "usgs")
        self.assertEqual(n, imagery.ATTEMPTS)          # the first fetch failed; the rest were skipped
        _, used = self.render(im, x0=5000.0)            # other source tiles: still skipped
        self.assertEqual(set(used), {"eox2023"})
        self.assertEqual(sum(1 for r in im.requests if r[0] == "usgs"), n)
        self.assertGreater(im.stats_snapshot().get("usgs:skipped", 0), 0)
        up["usgs"] = True
        im._down["usgs"] = time.monotonic() - 1.0       # the backoff has run out
        _, used = self.render(im, x0=10000.0)
        self.assertEqual(set(used), {"usgs"})

    def test_undecodable_tile_is_an_error(self):
        def serve(name, z, x, y):
            return 200, b"<html>maintenance</html>"
        with self.assertRaises(imagery.ImageryError):
            self.render(FakeNet(self.tmp.name, serve, chain=("eox2023",), tone=0.0))
        self.assertFalse([f for _, _, fs in os.walk(self.tmp.name) for f in fs if f.endswith(".jpg")])

    def test_prefetch(self):
        def serve(name, z, x, y):
            return (404, b"") if name == "usgs" else self.tile(name, z, x, y)
        im = FakeNet(self.tmp.name, serve, tone=0.0)
        f = g.EnuFrame(LAT0, LON0)
        im.prefetch(f, 0.0, 0.0, 256.0, 64)
        time.sleep(0.3)
        self.assertTrue(im.requests and all(r[0] == "usgs" for r in im.requests))
        im.prefetch(f, 0.0, 0.0, 256.0, 64)      # usgs known missing there: eox2023 next
        time.sleep(0.3)
        self.assertIn("eox2023", {r[0] for r in im.requests})
        n = len(im.requests)
        self.render(im)
        self.assertEqual(len(im.requests), n)     # render found everything prefetched

    def test_parse_chain_and_attribution(self):
        self.assertIsNone(imagery.parse_chain("off"))
        self.assertEqual(imagery.parse_chain("usgs,eox2016"), ("usgs", "eox2016"))
        with self.assertRaises(ValueError):
            imagery.parse_chain("google")
        self.assertEqual(imagery.Imagery(self.tmp.name).attribution(), imagery.ATTRIBUTION)
        self.assertIn("CC BY 4.0", imagery.Imagery(self.tmp.name, ("eox2016",)).attribution())
        # EOX's required wording (June 2026, docs/research/2026-10-10-rate-limits.md)
        eox = "EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH (Contains modified Copernicus Sentinel data "
        self.assertEqual(imagery.SOURCES["eox2023"].attribution, eox + "2023), CC BY-NC-SA 4.0")
        self.assertEqual(imagery.SOURCES["eox2016"].attribution, eox + "2016 & 2017), CC BY 4.0")
        self.assertEqual(imagery.ATTRIBUTION, "Imagery: USGS The National Map (public domain); " + eox
                         + "2023), CC BY-NC-SA 4.0; " + eox + "2016 & 2017), CC BY 4.0")
        self.assertNotIn("Sentinel-2 cloudless", imagery.ATTRIBUTION)


class GutterTest(unittest.TestCase):
    """Tile textures carry a border of real imagery (the engine's sampler wraps)."""

    def test_footprint_maps_to_inner_texels(self):
        for px in (64, 256, 512):
            g = imagery.gutter(px)
            self.assertTrue(1 <= g <= 8)
            x0, y0, size = imagery.gutter_square(1000.0, -64.0, 64.0, px)
            t = size / px
            # the footprint [1000, 1064] covers texels g .. px - g exactly
            self.assertAlmostEqual(1000.0 - x0, g * t, places=9)
            self.assertAlmostEqual(x0 + size - 1064.0, g * t, places=9)
            self.assertAlmostEqual(-64.0 - y0, g * t, places=9)
            # UV0 of a footprint point = its position in the rendered square
            for f in (0.0, 0.3, 1.0):
                u = float(imagery.gutter_uv(f, px))
                self.assertAlmostEqual(u * size + x0, 1000.0 + f * 64.0, places=9)
            self.assertGreater(float(imagery.gutter_uv(0.0, px)), 0.5 / px)        # off the wrap seam
            self.assertLess(float(imagery.gutter_uv(1.0, px)), 1 - 0.5 / px)

    def test_shape_uv_inset(self):
        import farfield as ff
        import cdae
        frame = g.EnuFrame(36.0, -118.0)

        class Flat:
            def sample(self, lat, lon, zoom, progress=None):
                return np.zeros(np.shape(lat))
        tile = ff.Tile(-2, 0, 0)
        b = ff.build_tile(frame, Flat(), tile, 15, 1 / 64, with_sink=False)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.cdae")
            ff.write_built(path, b, "cdae", material="m", textured=True, tex_px=256)
            m = cdae.mesh_arrays(cdae.read(path)["meshes"][0])
        local = m["verts"].astype(np.float64)[:, :2] / tile.size
        np.testing.assert_allclose(m["tverts"][:, 0], imagery.gutter_uv(local[:, 0], 256), atol=1e-6)
        np.testing.assert_allclose(m["tverts"][:, 1], 1 - imagery.gutter_uv(local[:, 1], 256), atol=1e-6)


class ToneTest(unittest.TestCase):
    def test_lut(self):
        self.assertTrue((imagery.tone_lut(0.0) == np.arange(256)).all())
        lut = imagery.tone_lut(1.0).astype(int)
        self.assertTrue((np.diff(lut) >= 0).all())
        self.assertEqual((lut[0], lut[255]), (15, 230))
        self.assertLessEqual(abs(lut[128] - 128), 2)
        self.assertTrue((np.diff(imagery.tone_lut(3.0).astype(int)) >= 0).all())


def bc1_decode(data, w, h):
    """Minimal BC1 (DXT1) decoder for the tests: RGB [h, w, 3] float."""
    bw, bh = max(1, (w + 3) // 4), max(1, (h + 3) // 4)
    blocks = np.frombuffer(data, np.uint8).reshape(bh * bw, 8)
    c0 = blocks[:, 0].astype(np.uint32) | (blocks[:, 1].astype(np.uint32) << 8)
    c1 = blocks[:, 2].astype(np.uint32) | (blocks[:, 3].astype(np.uint32) << 8)
    idx = (blocks[:, 4].astype(np.uint32) | (blocks[:, 5].astype(np.uint32) << 8)
           | (blocks[:, 6].astype(np.uint32) << 16) | (blocks[:, 7].astype(np.uint32) << 24))

    def rgb565(c):
        return np.stack([(c >> 11 & 31) * 255 / 31, (c >> 5 & 63) * 255 / 63, (c & 31) * 255 / 31], -1)
    p0, p1 = rgb565(c0), rgb565(c1)
    four = (c0 > c1)[:, None]
    pal = np.stack([p0, p1, np.where(four, (2 * p0 + p1) / 3, (p0 + p1) / 2),
                    np.where(four, (p0 + 2 * p1) / 3, 0.0)], 1)              # (n, 4, 3)
    sel = (idx[:, None] >> (2 * np.arange(16, dtype=np.uint32))) & 3          # (n, 16)
    px = np.take_along_axis(pal, sel[:, :, None].astype(np.int64), 1)        # (n, 16, 3)
    img = px.reshape(bh, bw, 4, 4, 3).transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, 3)
    return img[:h, :w]


class DdsTest(unittest.TestCase):
    def test_dxt1_with_full_mip_chain(self):
        n = 128
        # smooth, imagery-like content (BC1 is lossy; this tests the chain, not the encoder)
        low = (np.random.default_rng(5).random((8, 8, 3)) * 100 + 60).astype(np.uint8)
        rgb = np.asarray(Image.fromarray(low, "RGB").resize((n, n), Image.BICUBIC))
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.dds")
            size = imagery.write_dds(path, rgb)
            data = Path(path).read_bytes()
            self.assertEqual(size, len(data))
            w, h, mips, fourcc, sizes = imagery.read_dds_info(data)
            self.assertEqual((w, h, mips, fourcc), (n, n, 8, b"DXT1"))     # 128 .. 1
            self.assertEqual(len(data), imagery.DDS_HEADER + sum(sizes))
            self.assertEqual(sizes[0], n * n // 2)
            caps = int.from_bytes(data[108:112], "little")
            self.assertTrue(caps & imagery.DDSCAPS_MIPMAP and caps & imagery.DDSCAPS_COMPLEX)
            with Image.open(path) as im:                                    # a standard reader accepts it
                self.assertEqual(im.size, (n, n))
            # level k is the BC1 encoding of the 2 x 2 box reduction of level k - 1 ...
            off, lv = imagery.DDS_HEADER, Image.fromarray(rgb, "RGB")
            for k, sz in enumerate(sizes):
                b = io.BytesIO()
                lv.save(b, "DDS", pixel_format="DXT1")
                self.assertEqual(data[off:off + sz], b.getvalue()[imagery.DDS_HEADER:], k)
                off += sz
                lv = lv.reduce(2) if lv.size[0] > 1 else lv
            # ... and decodes to the image (row 0 first, as the source image)
            got = bc1_decode(data[imagery.DDS_HEADER:imagery.DDS_HEADER + sizes[0]], n, n)
            self.assertLess(np.abs(got - rgb).mean(), 4.0)
            last = bc1_decode(data[-8:], 1, 1)[0, 0]
            np.testing.assert_allclose(last, rgb.reshape(-1, 3).mean(0), atol=6.0)      # one RGB565 step

    def test_sizes(self):
        for px, want in ((512, 128 + sum(max(1, (512 >> k) // 4) ** 2 * 8 for k in range(10))),
                         (256, 128 + sum(max(1, (256 >> k) // 4) ** 2 * 8 for k in range(9)))):
            self.assertEqual(len(imagery.encode_dds(np.zeros((px, px, 3), np.uint8))), want)


class NetworkImageryTest(unittest.TestCase):
    """Real sources (a handful of tiles, cached across runs)."""

    def render(self, lat, lon, size=256.0, px=64):
        im = imagery.Imagery(CACHE)
        try:
            return im.render(g.EnuFrame(lat, lon), -size / 2, -size / 2, size, px)
        except imagery.ImageryError as e:
            self.skipTest(f"imagery sources unreachable: {e}")
        finally:
            im.close()

    def test_us_uses_usgs(self):
        rgb, used = self.render(LAT0, LON0)
        self.assertEqual(set(used), {"usgs"})
        self.assertGreater(rgb.std(), 5)

    def test_outside_us_falls_back_to_eox(self):
        rgb, used = self.render(46.5, 8.0)              # Swiss Alps
        self.assertEqual(set(used), {"eox2023"})
        self.assertGreater(rgb.std(), 5)

    def test_open_ocean(self):
        rgb, used = self.render(30.0, -140.0, size=4096.0)
        self.assertEqual(set(used), {"eox2016"})       # uniform deep blue from both EOX layers
        self.assertLess(rgb.reshape(-1, 3).std(0).max(), 2)
        self.assertGreater(int(rgb[..., 2].mean()), int(rgb[..., 0].mean()))


if __name__ == "__main__":
    unittest.main()
