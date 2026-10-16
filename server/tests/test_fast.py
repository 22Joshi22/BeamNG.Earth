"""fast: the Rust kernels give what the Python code gives."""
import math
import sys
import types
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

    def test_bc7_matches_python(self):
        import bc7
        rng = np.random.default_rng(4)
        lum = np.cumsum(np.cumsum(rng.normal(0, 1.0, (64, 64)), 0), 1)
        lum = (lum - lum.min()) / (np.ptp(lum) + 1e-9) * 200 + 30
        photo = np.clip(lum[..., None] * np.array([1.0, 0.9, 0.75]) + rng.normal(0, 6, (64, 64, 3)), 0, 255)
        for img in (photo.astype(np.uint8), photo.astype(np.uint8)[:61, :37],
                    (rng.random((32, 48, 4)) * 255).astype(np.uint8), np.full((8, 8, 3), 255, np.uint8)):
            self.assertEqual(bc7.encode(img), bc7._encode_py(img), img.shape)

    def test_cut_outside_matches_python(self):
        import roads
        rng = np.random.default_rng(8)
        for trial in range(150):
            k = int(rng.integers(2, 8))
            pts = np.c_[np.cumsum(rng.normal(0, 15, (k, 2)), 0), np.full(k, rng.normal())]
            blk = [(np.cumsum(rng.normal(0, 15, (int(rng.integers(1, 6)), 2)), 0) + rng.normal(0, 20, 2),
                    rng.uniform(1, 8)) for _ in range(int(rng.integers(0, 5)))]
            got = roads.cut_outside(pts, blk, 2.0)
            fast.AVAILABLE = False
            try:
                want = roads.cut_outside(pts, blk, 2.0)
            finally:
                fast.AVAILABLE = True
            self.assertEqual(len(got), len(want), trial)
            for a, b in zip(got, want):
                self.assertTrue(np.array_equal(a, b), trial)

    def test_densify_matches_python(self):
        import roads
        rng = np.random.default_rng(2)
        for trial in range(200):
            k, c = int(rng.integers(1, 9)), int(rng.integers(2, 4))
            pts = np.cumsum(rng.normal(0, 12, (k, c)), 0)
            if trial % 7 == 0 and k > 1:
                pts[1] = pts[0]                     # a zero-length segment
            sp = float(rng.choice([0.5, 2.0, 4.0, 10.0]))
            got = roads.densify(pts, sp)
            fast.AVAILABLE = False
            try:
                want = roads.densify(pts, sp)
            finally:
                fast.AVAILABLE = True
            self.assertTrue(np.array_equal(got, want), trial)

    def test_measure_row_matches_python(self):
        import roadwidth
        rng = np.random.default_rng(6)
        for trial in range(60):
            # a street between two rows of building walls (gaps, jitter), sometimes open on one side
            half = rng.uniform(5, 15)
            xs = np.arange(-80, 80, 9.0)
            segs = []
            for side in (1, -1):
                if trial % 5 == 0 and side < 0:
                    continue
                for x in xs:
                    if rng.random() < 0.8:
                        y = side * (half + rng.normal(0, 0.6))
                        segs.append([x, y, x + rng.uniform(4, 9), y + rng.normal(0, 0.4)])
            walls = roadwidth.Walls(np.array(segs) if segs else np.zeros((0, 4)))
            k = int(rng.integers(2, 9))
            line = np.c_[np.linspace(-60, 60, k), rng.normal(0, 0.5, k)]
            got = roadwidth.measure_row(line, walls)
            fast.AVAILABLE = False
            try:
                want = roadwidth.measure_row(line, walls)
            finally:
                fast.AVAILABLE = True
            self.assertEqual(got is None, want is None, trial)
            if got is not None:
                self.assertAlmostEqual(got, want, places=9)

    def test_grade_points_matches_python(self):
        import grading
        rng = np.random.default_rng(12)
        for trial in range(20):
            px, py = rng.uniform(-200, 200, 3000), rng.uniform(-200, 200, 3000)
            base = rng.normal(100, 5, 3000)
            plans = []
            for _ in range(int(rng.integers(0, 6))):
                k = int(rng.integers(2, 12))
                xy = np.cumsum(rng.normal(0, 30, (k, 2)), 0) + rng.uniform(-150, 150, 2)
                plans.append(types.SimpleNamespace(xy=xy, z=rng.normal(100, 3, k), half=float(rng.uniform(2, 8)),
                                                   bbox=(*xy.min(0), *xy.max(0))))
            got = grading.grade_points(px, py, base, plans, 0.1)
            fast.AVAILABLE = False
            try:
                want = grading.grade_points(px, py, base, plans, 0.1)
            finally:
                fast.AVAILABLE = True
            self.assertTrue(np.allclose(got, want, rtol=0, atol=1e-9), trial)

    def test_resample_and_smooth_match_python(self):
        import grading
        rng = np.random.default_rng(4)
        for trial in range(150):
            k = int(rng.integers(1, 15))
            xy = np.cumsum(rng.normal(0, 20, (k, 2)), 0)
            if trial % 9 == 0 and k > 1:
                xy[1] = xy[0]
            z = rng.normal(100, 5, int(rng.integers(1, 40)))
            got = (grading.resample(xy, 4.0), [grading.smooth_profile(z, 4.0, s) for s in (0.0, 6.0, 20.0, 60.0)])
            fast.AVAILABLE = False
            try:
                want = (grading.resample(xy, 4.0), [grading.smooth_profile(z, 4.0, s) for s in (0.0, 6.0, 20.0, 60.0)])
            finally:
                fast.AVAILABLE = True
            self.assertTrue(np.array_equal(got[0][0], want[0][0]) and np.array_equal(got[0][1], want[0][1]), trial)
            for a, b in zip(got[1], want[1]):
                self.assertTrue(np.allclose(a, b, rtol=0, atol=1e-9), trial)

    def test_points_and_distances(self):
        sq = np.array([[0, 0], [10, 0], [10, 10], [0, 10]], float)
        pts = np.array([[5, 5], [15, 5], [-1, -1], [9.9, 0.1]], float)
        self.assertEqual(list(fast._ef.points_inside(sq, pts)), [True, False, False, True])
        d = fast._ef.polyline_dist(np.array([[0, 0], [10, 0]], float), np.array([[5, 3], [12, 0], [-3, 4]], float))
        self.assertTrue(np.allclose(d, [3, 2, 5]))


if __name__ == "__main__":
    unittest.main()
