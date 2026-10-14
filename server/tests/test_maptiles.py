"""The world map's tile endpoint (server/maptiles.py) with a fake imagery object: source order,
coverage fall-through, overzoom, errors, and the HTTP server on 127.0.0.1. Also runs the map's
tile math test (test_mapmath.mjs) when node is installed."""
import asyncio
import io
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import imagery  # noqa: E402
import maptiles  # noqa: E402

S = imagery.SOURCES


class FakeImagery:
    """Imagery's .chain / .tiles / .attribution: each source's tiles are one flat colour (red channel
    = the source) with a gradient so they are not uniform; `missing` / `blank` / `fail` per source."""

    def __init__(self, chain=imagery.DEFAULT_CHAIN, missing=(), blank=(), fail=()):
        self.chain = tuple(S[n] for n in chain)
        self.missing, self.blank, self.fail = set(missing), set(blank), set(fail)
        self.calls = []

    def tiles(self, keys, kind="demand"):
        out = {}
        for k in keys:
            src, z, x, y = k
            self.calls.append((src.name, z, x, y, kind))
            if src.name in self.fail:
                raise imagery.ImageryError("down")
            if src.name in self.missing:
                out[k] = None
                continue
            a = np.zeros((256, 256, 3), np.uint8)
            a[..., 0] = {"usgs": 60, "eox2023": 140, "eox2016": 200}[src.name]
            if src.name not in self.blank:
                a[..., 1] = np.arange(256, dtype=np.uint8)[:, None]      # rows 0..255: a gradient north to south
            out[k] = imagery.as_tile(a)
        return out

    def attribution(self):
        return "Imagery: fake"


def decode(jpeg):
    return np.asarray(Image.open(io.BytesIO(jpeg)).convert("RGB")).astype(int)


class MapTilesTest(unittest.TestCase):
    def test_paths(self):
        self.assertEqual(maptiles.parse_path("/tiles/3/2/1.jpg"), (3, 2, 1))
        self.assertEqual(maptiles.parse_path("/tiles/3/2/1?t=5"), (3, 2, 1))
        for bad in ("/tiles/3/8/1.jpg", "/tiles/-1/0/0.jpg", "/tiles/19/0/0.jpg", "/tiles/a/b/c.jpg",
                    "/x/1/0/0.jpg", "/tiles/1/0"):
            with self.assertRaises(maptiles.BadTile, msg=bad):
                maptiles.parse_path(bad)

    def test_eox_first_at_low_zoom_usgs_above(self):
        img = FakeImagery()
        t = maptiles.MapTiles(img)
        _, src = t.tile(11, 584, 783)
        self.assertEqual(src, "eox2023")
        _, src = t.tile(14, 4683, 6266)
        self.assertEqual(src, "usgs")
        self.assertTrue(all(c[4] == maptiles.FETCH_KIND for c in img.calls))

    def test_usgs_not_below_its_min_zoom(self):
        img = FakeImagery(chain=("usgs",))
        self.assertIsNone(maptiles.MapTiles(img).tile(5, 9, 12))
        self.assertEqual(img.calls, [])

    def test_fall_through_missing_and_blank(self):
        self.assertEqual(maptiles.MapTiles(FakeImagery(missing=("usgs",))).tile(15, 9412, 12490)[1], "eox2023")
        self.assertEqual(maptiles.MapTiles(FakeImagery(blank=("usgs", "eox2023"))).tile(15, 9412, 12490)[1],
                         "eox2016")
        self.assertIsNone(maptiles.MapTiles(FakeImagery(missing=("usgs", "eox2023", "eox2016"))).tile(15, 1, 1))

    def test_failure_raises_only_when_nothing_else_has_it(self):
        self.assertEqual(maptiles.MapTiles(FakeImagery(fail=("usgs",))).tile(15, 9412, 12490)[1], "eox2023")
        with self.assertRaises(imagery.ImageryError):
            maptiles.MapTiles(FakeImagery(fail=("usgs", "eox2023", "eox2016"))).tile(15, 9412, 12490)

    def test_overzoom_crops_the_ancestor(self):
        img = FakeImagery(chain=("eox2023",))
        t = maptiles.MapTiles(img)
        # z16 from the z14 ancestor: 4 x 4 children; the bottom row of children holds the
        # ancestor's rows 192..255 of the gradient
        x, y = (100 << 2) + 1, (200 << 2) + 3
        jpeg, src = t.tile(16, x, y)
        self.assertEqual(img.calls[-1][:4], ("eox2023", 14, 100, 200))
        a = decode(jpeg)
        self.assertEqual(a.shape, (256, 256, 3))
        self.assertTrue(185 <= a[:, :, 1].mean() <= 230, a[:, :, 1].mean())
        top = decode(t.tile(16, 100 << 2, 200 << 2)[0])
        self.assertLess(top[:, :, 1].mean(), 40)
        self.assertEqual(maptiles.crop_up(np.zeros((256, 256, 3), np.uint8), 0, 0, 0).shape, (256, 256, 3))

    def test_respond(self):
        t = maptiles.MapTiles(FakeImagery())
        st, ctype, body = t.respond("GET", "/tiles/12/1170/1566.jpg")
        self.assertEqual((st, ctype, body[:2]), (200, "image/jpeg", b"\xff\xd8"))
        self.assertEqual(t.respond("GET", "/tiles/40/0/0.jpg")[0], 404)
        self.assertEqual(t.respond("POST", "/tiles/1/0/0.jpg")[0], 405)
        self.assertIn(b"fake", t.respond("GET", "/attribution")[2])
        self.assertEqual(t.respond("GET", "/health")[2], b"ok")
        dead = maptiles.MapTiles(FakeImagery(fail=("usgs", "eox2023", "eox2016")))
        self.assertEqual(dead.respond("GET", "/tiles/12/1170/1566.jpg")[0], 503)
        self.assertEqual(maptiles.MapTiles(FakeImagery(missing=("usgs", "eox2023", "eox2016"))).respond(
            "GET", "/tiles/12/1170/1566.jpg")[0], 404)

    def test_http_on_localhost(self):
        async def run():
            t = maptiles.MapTiles(FakeImagery())
            srv, port = await t.start(0)
            self.assertEqual(srv.sockets[0].getsockname()[0], "127.0.0.1")

            async def get(path):
                r, w = await asyncio.open_connection("127.0.0.1", port)
                w.write(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
                await w.drain()
                data = await r.read()
                w.close()
                return data
            try:
                ok = await get("/tiles/3/2/3.jpg")
                bad = await get("/tiles/3/9/3.jpg")
                junk = await get("")
            finally:
                srv.close()
                await srv.wait_closed()
            return ok, bad, junk
        ok, bad, junk = asyncio.run(run())
        head, body = ok.split(b"\r\n\r\n", 1)
        self.assertTrue(head.startswith(b"HTTP/1.1 200"))
        self.assertIn(b"Access-Control-Allow-Origin: *", head)
        self.assertIn(f"Content-Length: {len(body)}".encode(), head)
        self.assertEqual(body[:2], b"\xff\xd8")
        self.assertTrue(bad.startswith(b"HTTP/1.1 404"))
        self.assertTrue(junk.startswith(b"HTTP/1.1 400"))


@unittest.skipUnless(shutil.which("node"), "node not installed")
class MapMathTest(unittest.TestCase):
    def test_mapmath(self):
        r = subprocess.run(["node", str(Path(__file__).with_name("test_mapmath.mjs"))], capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
