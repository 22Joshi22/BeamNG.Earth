import math
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dem as demmod  # noqa: E402
import geodesy as g  # noqa: E402
import heightmap  # noqa: E402

LAT0, LON0 = 36.5785, -118.2923


def rough(lat, lon):
    """Synthetic rough DEM: metre-scale bumps at ~10 m wavelength on a 600 m relief."""
    lat, lon = np.asarray(lat), np.asarray(lon)
    return (1500 + 300 * np.sin(lat * 90) * np.cos(lon * 70)
            + 3.0 * np.sin(lat * 61000) * np.cos(lon * 47000) + 1.3 * np.sin(lon * 151000))


class GeometryTest(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(heightmap.SIZE, 1024)
        self.assertEqual(heightmap.SQUARE, 2.0)
        self.assertEqual(heightmap.VALID, 2046.0)           # (N-1)*s, probe P1
        self.assertEqual(heightmap.GRID / heightmap.SQUARE, 128)

    def test_sample_positions_on_lattice(self):
        for cx, cy in ((0.0, 0.0), (256.0, -768.0), (10240.0, 5376.0), (-24832.0, 24576.0)):
            xs, ys = heightmap.sample_coords(cx, cy)
            self.assertEqual(len(xs), 1024)
            # Block position (SW corner) = (cx - 1024, cy - 1024); sample gx at x0 + 2 gx, exactly.
            self.assertEqual(xs[0], cx - 1024)
            self.assertEqual(ys[0], cy - 1024)
            self.assertTrue((xs == cx - 1024 + 2.0 * np.arange(1024)).all())
            self.assertTrue((xs % 2 == 0).all())
            self.assertEqual(xs[1023], cx + 1022)
        # Adjacent blocks share their samples bit for bit.
        a, _ = heightmap.sample_coords(0.0, 0.0)
        b, _ = heightmap.sample_coords(256.0, 0.0)
        self.assertTrue((a[128:] == b[:896]).all())
        with self.assertRaises(ValueError):
            heightmap.sample_coords(1.0, 0.0)

    def test_snap(self):
        self.assertEqual(heightmap.snap(127.9), 0.0)
        self.assertEqual(heightmap.snap(128.0), 256.0)
        self.assertEqual(heightmap.snap(-128.1), -256.0)
        self.assertEqual(heightmap.snap(-127.0), 0.0)
        self.assertEqual(str(heightmap.snap(-1.0)), "0.0")    # no -0.0 in file names / JSON
        self.assertEqual(heightmap.snap(5000.0), 5120.0)

    def test_choose_q_and_block_params(self):
        self.assertEqual(heightmap.choose_q(50), 1 / 256)
        self.assertEqual(heightmap.choose_q(600), 1 / 64)       # 1024 m range
        self.assertEqual(heightmap.choose_q(3000), 1 / 16)
        self.assertEqual(heightmap.choose_q(1e6), 1 / 16)
        q, zb, mh = heightmap.block_params(1234.5, 1500.0, 1 / 64)
        self.assertEqual((q, zb, mh), (1 / 64, 1216.0, 1024.0))
        self.assertEqual(zb % 64, 0)
        # Too much relief for the anchor's quantum: the next coarser one, for this block only.
        q, zb, mh = heightmap.block_params(0.0, 1500.0, 1 / 64)
        self.assertEqual((q, mh), (1 / 32, 2048.0))
        self.assertEqual(heightmap.block_params(0.0, 1e5, 1 / 64)[0], 1 / 16)


class HeightmapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "hm.png")
        self.frame = g.EnuFrame(LAT0, LON0)

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, path=None):
        with Image.open(path or self.path) as im:
            self.assertEqual(im.size, (1024, 1024))
            self.assertIn(im.mode, ("I;16", "I"))
            return np.asarray(im).astype(np.int64)[::-1]       # back to [gy, gx], gy = north

    def test_orientation_and_sample_positions(self):
        # Ground rising to the north-east: north edge high, east edge high.
        elev = lambda lat, lon: 1000 + (lat - LAT0) * 20000 + (lon - LON0) * 5000
        cx, cy = 768.0, -256.0
        info = heightmap.generate(self.frame, elev, self.path, cx, cy)
        with Image.open(self.path) as im:
            png = np.asarray(im).astype(np.float64)
        self.assertGreater(png[0].mean(), png[-1].mean())        # row 0 = north
        self.assertGreater(png[:, -1].mean(), png[:, 0].mean())  # column 0 = west
        self.assertEqual((info["cx"], info["cy"]), (cx, cy))
        self.assertEqual(info["maxHeight"], info["q"] * 65536)
        self.assertEqual(info["zBase"] % 64, 0)
        self.assertLessEqual(info["zBase"], info["minU"] - 8)
        q = self.read()
        h = heightmap.heights(q, info["zBase"], info["maxHeight"])
        # Sample (gx, gy) at (cx-1024+2gx, cy-1024+2gy); the last row/column is real data.
        for gy, gx in ((100, 900), (0, 0), (1023, 1023), (1023, 0), (0, 1023), (511, 511)):
            x, y = cx - 1024 + 2 * gx, cy - 1024 + 2 * gy
            _, _, _, _, _, u = self.frame.surface_enu(np.float64(x), np.float64(y), elev)
            self.assertAlmostEqual(h[gy, gx], float(u), delta=info["q"] / 2 + 1e-9)
        self.assertFalse((q[:, 1023] == q[:, 1022]).all())
        self.assertFalse((q[1023] == q[1022]).all())
        # (0, 0) is inside this block: groundZ is the terrain height there.
        _, _, _, _, _, u0 = self.frame.surface_enu(np.float64(0), np.float64(0), elev)
        self.assertAlmostEqual(info["groundZ"], float(u0), delta=0.01)

    def test_overlapping_blocks_identical(self):
        """§1.8: adjacent blocks' overlapping samples are bit-identical (same stored height and,
        with the same zBase, the same uint16), on rough ground."""
        base = heightmap.generate(self.frame, rough, self.path, 0.0, 0.0)
        q0 = self.read()
        h0 = heightmap.heights(q0, base["zBase"], base["maxHeight"])
        for dx, dy in ((256.0, 0.0), (0.0, 256.0), (256.0, 256.0), (-512.0, 768.0)):
            with self.subTest(dx=dx, dy=dy):
                p = os.path.join(self.tmp.name, f"hm_{dx}_{dy}.png")
                info = heightmap.generate(self.frame, rough, p, dx, dy)
                q1 = self.read(p)
                h1 = heightmap.heights(q1, info["zBase"], info["maxHeight"])
                sx, sy = int(dx // 2), int(dy // 2)
                a = (slice(max(0, sy), 1024 + min(0, sy)), slice(max(0, sx), 1024 + min(0, sx)))
                b = (slice(max(0, -sy), 1024 - max(0, sy)), slice(max(0, -sx), 1024 - max(0, sx)))
                self.assertEqual(info["q"], base["q"])
                self.assertTrue((h0[a] == h1[b]).all())
                if info["zBase"] == base["zBase"]:
                    self.assertTrue((q0[a] == q1[b]).all())
                else:
                    self.assertTrue((q0[a] - q1[b] == (info["zBase"] - base["zBase"]) / info["q"]).all())
        # Stored heights are Q * round(u / Q) for any zBase.
        u = heightmap.grid_u(self.frame, rough, 0.0, 0.0)
        self.assertTrue((h0 == heightmap.quantise(u, base["q"]) * base["q"]).all())

    def test_overlap_identical_through_dem_mosaic(self):
        """The same, through dem.Dem.sample (tile mosaic + bilinear) on synthetic z15 tiles."""
        class SynthDem(demmod.Dem):
            def _load_tile(self, key):
                rng = np.random.default_rng(abs(hash(key)) % 2 ** 32)
                return (2000 + rng.normal(0, 3, (demmod.TILE, demmod.TILE))).astype(np.float32)
        with tempfile.TemporaryDirectory() as cache:
            dem = SynthDem(cache, fetch_workers=2)
            fn = lambda la, lo: dem.sample(la, lo, 15)      # noqa: E731
            a = heightmap.generate(self.frame, fn, self.path, 0.0, 0.0)
            h0 = heightmap.heights(self.read(), a["zBase"], a["maxHeight"])
            p = os.path.join(self.tmp.name, "b.png")
            b = heightmap.generate(self.frame, fn, p, 256.0, -768.0)
            h1 = heightmap.heights(self.read(p), b["zBase"], b["maxHeight"])
            dem.pool.shutdown()
        self.assertTrue((h0[:640, 128:] == h1[384:, :896]).all())

    def test_ground_outside_block(self):
        info = heightmap.generate(self.frame, lambda lat, lon: np.zeros_like(lat), self.path, 2048.0, 0.0)
        self.assertIsNone(info["groundZ"])

    def test_valid_area(self):
        info = heightmap.generate(self.frame, lambda lat, lon: np.zeros_like(lat), self.path, 0.0, 0.0)
        q = self.read()
        args = (q, info["zBase"], info["maxHeight"], 0.0, 0.0)
        self.assertIsNotNone(heightmap.height_at(*args, -1024.0, -1024.0))
        self.assertIsNotNone(heightmap.height_at(*args, 1022.0, 1022.0))      # sample 1023: still collidable
        self.assertIsNone(heightmap.height_at(*args, 1023.0, 0.0))            # the 2 m N/E strip
        self.assertIsNone(heightmap.height_at(*args, 0.0, -1024.5))

    def test_quantisation_and_curvature(self):
        info = heightmap.generate(self.frame, lambda lat, lon: np.zeros_like(lat), self.path)
        q = self.read()
        # Flat ellipsoid: centre is highest; corners (1.45 km out) drop ~0.16 m.
        self.assertEqual(info["q"], heightmap.Q_DEFAULT)
        self.assertEqual(info["zBase"], math.floor((info["minU"] - 8) / 64) * 64)
        self.assertAlmostEqual(info["groundZ"], 0.0, delta=0.01)
        corner = heightmap.heights(q, info["zBase"], info["maxHeight"])[0, 0]
        self.assertAlmostEqual(corner, -(1024 ** 2 * 2) / (2 * 6371000), delta=0.02)


if __name__ == "__main__":
    unittest.main()
