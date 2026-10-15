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

    def test_points_and_distances(self):
        sq = np.array([[0, 0], [10, 0], [10, 10], [0, 10]], float)
        pts = np.array([[5, 5], [15, 5], [-1, -1], [9.9, 0.1]], float)
        self.assertEqual(list(fast._ef.points_inside(sq, pts)), [True, False, False, True])
        d = fast._ef.polyline_dist(np.array([[0, 0], [10, 0]], float), np.array([[5, 3], [12, 0], [-3, 4]], float))
        self.assertTrue(np.allclose(d, [3, 2, 5]))


if __name__ == "__main__":
    unittest.main()
