"""fast: the Rust kernels give what the Python code gives."""
import math
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fast  # noqa: E402
import islands  # noqa: E402


@unittest.skipUnless(fast.AVAILABLE, "no earthfast binary for this platform (tools/build_native.py)")
class FastTest(unittest.TestCase):
    def python_ring(self, poly, roads_hw, z=0.0):
        saved = fast._ef
        fast._ef = None
        try:
            return islands._island_ring(poly, roads_hw, z)
        finally:
            fast._ef = saved

    def test_island_ring_matches_python(self):
        rng = np.random.default_rng(7)
        for trial in range(20):
            r = 15 + 20 * rng.random()
            k = 24
            t = np.linspace(0, 2 * math.pi, k, endpoint=False)
            wob = 1 + 0.15 * np.sin(3 * t + trial)
            poly = np.stack([r * wob * np.cos(t), r * wob * np.sin(t)], 1)
            roads = [(np.vstack([poly[i:i + 7], poly[(i + 7) % k:(i + 8) % k or None][:1]]), 3.0 + rng.random())
                     for i in range(0, k, 6)]
            a = islands._island_ring(poly, roads, 1.0)
            b = self.python_ring(poly, roads, 1.0)
            self.assertEqual(a is None, b is None)
            if a is not None:
                self.assertTrue(np.allclose(a, b, atol=1e-9), f"trial {trial}")

    def test_clear_matches_python(self):
        import furniture
        rng = np.random.default_rng(3)
        lines = [(f"r{i}", 2 + 3 * rng.random(), np.cumsum(rng.normal(0, 8, (6, 3)), 0)) for i in range(8)]
        o = fast.Others(lines)
        for _ in range(300):
            p = rng.normal(0, 20, 3)
            own = f"r{rng.integers(0, 8)}"
            self.assertEqual(furniture._clear(p, o, own), furniture._clear(p, lines, own))

    def test_dual_side_matches_python(self):
        import roads
        rng = np.random.default_rng(11)
        for trial in range(200):
            n = rng.integers(2, 12)
            a = np.cumsum(rng.normal(0, 25, (n, 2)), 0)
            off = rng.normal(0, 1, 2) * rng.uniform(2, 50)
            b = a[::-1] + off + rng.normal(0, 1, (n, 2)) if trial % 2 else np.cumsum(rng.normal(0, 25, (n, 2)), 0)
            got = roads._dual_side(a, b)
            saved = fast._ef
            fast._ef, fast.AVAILABLE = None, False
            try:
                want = roads._dual_side(a, b)
            finally:
                fast._ef, fast.AVAILABLE = saved, True
            # the share may differ by one ~10 m sample: where the nearest point on b is a vertex both segments share,
            # which one's direction counts is a floating-point tie (numpy and Rust round it differently)
            samples = max(2, int(np.hypot(*np.diff(a, axis=0).T).sum() // 10) + 1)
            self.assertLessEqual(abs(got[0] - want[0]), 1.0 / samples + 1e-9, (trial, got, want))
            if abs(got[1]) > 0.5 and abs(want[1]) > 0.5:     # (the caller uses only a clear side, > 0.5)
                self.assertEqual(np.sign(got[1]), np.sign(want[1]), (trial, got, want))

    def test_deck_crossings_matches_python(self):
        import roads
        rng = np.random.default_rng(5)
        for trial in range(50):
            lines = [np.cumsum(rng.normal(0, 30, (rng.integers(1, 9), 2)), 0) + rng.normal(0, 60, 2)
                     for _ in range(rng.integers(0, 7))]
            got = roads._deck_crossings(lines)
            want = roads._deck_crossings_py(lines)
            self.assertEqual([g[:2] for g in got], [w[:2] for w in want], trial)
            for g, w in zip(got, want):
                self.assertAlmostEqual(g[2], w[2], places=9)
                self.assertAlmostEqual(g[3], w[3], places=9)

    def test_point_inside_matches_python(self):
        rng = np.random.default_rng(9)
        for _ in range(100):
            k = rng.integers(3, 12)
            ring = rng.normal(0, 20, (k, 2))
            ring[rng.integers(0, k)][1] = ring[0][1]           # a level edge (the 1e-12 divisor)
            for p in rng.normal(0, 25, (20, 2)):
                saved = fast._ef
                fast._ef = None
                try:
                    want = islands._inside(ring, p)
                finally:
                    fast._ef = saved
                self.assertEqual(islands._inside(ring, p), want)

    def test_geodetic_to_enu_matches_numpy(self):
        import geodesy
        rng = np.random.default_rng(2)
        for lat0, lon0 in ((39.28, -76.6), (36.17, -115.145), (-33.9, 151.2), (64.1, -21.9)):
            f = geodesy.EnuFrame(lat0, lon0)
            lat = lat0 + rng.normal(0, 0.05, 50)
            lon = lon0 + rng.normal(0, 0.05, 50)
            h = rng.uniform(-50, 3000, 50)
            for hh in (h, 0.0, 12.5):
                got = f.geodetic_to_enu(lat, lon, hh)
                want = f.ecef_to_enu(*geodesy.geodetic_to_ecef(lat, lon, hh))
                for g, w in zip(got, want):
                    self.assertTrue(np.allclose(g, w, rtol=0, atol=1e-6))

    def test_points_and_distances(self):
        sq = np.array([[0, 0], [10, 0], [10, 10], [0, 10]], float)
        pts = np.array([[5, 5], [15, 5], [-1, -1], [9.9, 0.1]], float)
        self.assertEqual(list(fast._ef.points_inside(sq, pts)), [True, False, False, True])
        d = fast._ef.polyline_dist(np.array([[0, 0], [10, 0]], float), np.array([[5, 3], [12, 0], [-3, 4]], float))
        self.assertTrue(np.allclose(d, [3, 2, 5]))


if __name__ == "__main__":
    unittest.main()
