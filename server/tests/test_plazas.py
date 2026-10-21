"""Squares and pedestrian areas as a paved mesh (plazas.py; the user's report #45, 2026-10-17)."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import geodesy  # noqa: E402
import plazas  # noqa: E402


def _area(e):
    a, b = e[:, 1, :2] - e[:, 0, :2], e[:, 2, :2] - e[:, 0, :2]
    return 0.5 * np.abs(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]).sum()

FRAME = geodesy.EnuFrame(39.2817, -76.5929)


def ring(pts):
    la, lo, _ = FRAME.enu_to_geodetic(np.array([p[0] for p in pts], float), np.array([p[1] for p in pts], float),
                                      np.zeros(len(pts)))
    return np.c_[lo, la]


class PavingTest(unittest.TestCase):
    """GitHub #70: Paris's squares and the Louvre's courtyards all red brick (and their holes paved over)."""

    def test_by_the_mapped_surface(self):
        self.assertEqual(plazas.paving("asphalt", 48.86, 2.32)[0], plazas.ASPHALT[0])
        self.assertEqual(plazas.paving("sett", 48.86, 2.32)[0], plazas.STONE[0])
        self.assertIsNone(plazas.paving("compacted", 48.86, 2.32), "the Tuileries' sand: the terrain's photo")
        self.assertEqual(plazas.paving(None, 48.86, 2.32)[0], plazas.STONE[0], "not mapped, in Europe: stone")
        self.assertEqual(plazas.paving(None, 39.28, -76.59)[0], plazas.BRICK, "... in the US: brick")
        self.assertEqual(plazas.paving("paving_stones", 39.28, -76.59)[0], plazas.BRICK, "Fells Point's squares")

    def test_holes_left_out(self):
        outer = ring([(10, 10), (60, 10), (60, 40), (10, 40), (10, 10)])
        hole = ring([(20, 20), (30, 20), (30, 30), (20, 30), (20, 20)])
        flat = lambda la, lo: np.full(np.shape(la), 5.0)    # noqa: E731
        m = plazas.mesh(FRAME, (0.0, 0.0, 256.0, 256.0), [(outer, [hole], "asphalt")], flat, (0.0, 0.0, 0.0))
        P, T, UV, mats, M = m
        self.assertEqual(mats, [plazas.ASPHALT[0]])
        self.assertAlmostEqual(_area(P[T]), 50 * 30 - 10 * 10, delta=30.0)
        self.assertIsNone(plazas.mesh(FRAME, (0.0, 0.0, 256.0, 256.0), [(outer, [], "gravel")], flat, (0.0, 0.0, 0.0)))


class PlazaMeshTest(unittest.TestCase):
    def test_paved_and_draped(self):
        sq = ring([(10, 10), (60, 10), (60, 40), (10, 40), (10, 10)])
        elev = lambda la, lo: 5.0 + (np.asarray(lo) - (-76.5929)) * 10000.0    # noqa: E731 (a slope east)
        m = plazas.mesh(FRAME, (0.0, 0.0, 256.0, 256.0), [sq], elev, (0.0, 0.0, 0.0))
        self.assertIsNotNone(m)
        P, T, UV, mats, M = m
        self.assertEqual(mats, [plazas.BRICK])
        e = P[T]
        edges = np.hypot(*(e[:, [1, 2, 0], :2] - e[:, :, :2]).transpose(2, 0, 1))
        self.assertLessEqual(float(edges.max()), plazas.STEP_M + 1e-6, "split to follow the ground")
        area = _area(e)
        self.assertAlmostEqual(area, 50 * 30, delta=1.0, msg="the whole square, once")
        n = np.cross(e[:, 1] - e[:, 0], e[:, 2] - e[:, 0])
        self.assertTrue((n[:, 2] > 0).all(), "faces up")
        self.assertGreater(P[:, 2].max() - P[:, 2].min(), 1.0, "draped on the slope")

    def test_split_between_tiles(self):
        sq = ring([(-20, 10), (20, 10), (20, 30), (-20, 30)])
        flat = lambda la, lo: np.zeros(np.shape(la))    # noqa: E731
        a = plazas.mesh(FRAME, (-256.0, 0.0, 0.0, 256.0), [sq], flat, (0.0, 0.0, 0.0))
        b = plazas.mesh(FRAME, (0.0, 0.0, 256.0, 256.0), [sq], flat, (0.0, 0.0, 0.0))

        def area(m):
            return _area(m[0][m[1]])
        self.assertAlmostEqual(area(a) + area(b), 40 * 20, delta=1.0)

    def test_rust_and_python_split_alike(self):
        import fast
        if not hasattr(fast._ef, "subdivide_tris"):
            self.skipTest("no kernel")
        P0 = np.array([[0.0, 0.0], [17.0, 1.0], [5.0, 13.0]])
        T0 = np.array([[0, 1, 2]], np.int64)
        P1, T1 = fast._ef.subdivide_tris(P0, T0, 2.0)
        ef, avail = fast._ef, fast.AVAILABLE
        try:
            fast.AVAILABLE = False
            P2, T2 = plazas._subdivide(P0, T0, 2.0)
        finally:
            fast._ef, fast.AVAILABLE = ef, avail
        self.assertTrue(np.allclose(np.asarray(P1), P2))
        self.assertTrue(np.array_equal(np.asarray(T1), T2))


if __name__ == "__main__":
    unittest.main()
