"""Street widths between the buildings (roadwidth.py) and their use by the roads (roads.road_specs, the
sidewalks of build_tile)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import roads  # noqa: E402
import roadwidth  # noqa: E402
from test_roads import features, flat, FRAME  # noqa: E402


def row_of_houses(y_face, depth_sign, x0=-150.0, x1=150.0, w=8.0):
    """Wall segments of houses side by side along y = y_face (fronts), going depth_sign deep."""
    segs = []
    for x in np.arange(x0, x1, w):
        a, b = (x, y_face), (x + w - 0.5, y_face)
        c, d = (x + w - 0.5, y_face + depth_sign * 10), (x, y_face + depth_sign * 10)
        for p, q in ((a, b), (b, c), (c, d), (d, a)):
            segs.append((*p, *q))
    return segs


class MeasureTest(unittest.TestCase):
    def test_walled_street(self):
        walls = roadwidth.Walls(row_of_houses(9.0, 1) + row_of_houses(-9.0, -1))
        self.assertAlmostEqual(walls.ray(np.array([0.0, 0.0]), np.array([0.0, 1.0])), 9.0, places=6)
        self.assertIsNone(walls.ray(np.array([0.0, 0.0]), np.array([1.0, 0.0]), reach=5.0))
        line = np.array([(-100.0, 0.5), (100.0, -0.5)])
        self.assertAlmostEqual(roadwidth.measure_row(line, walls), 18.0, delta=0.1)
        cw, walk = roadwidth.split(18.0)
        self.assertAlmostEqual(cw + 2 * walk, 18.0)
        self.assertTrue(roadwidth.WALK_MIN <= walk <= roadwidth.WALK_MAX)

    def test_open_or_one_sided_street(self):
        one = roadwidth.Walls(row_of_houses(9.0, 1))
        self.assertIsNone(roadwidth.measure_row(np.array([(-100.0, 0.0), (100.0, 0.0)]), one), "a quay: one side")
        self.assertIsNone(roadwidth.measure_row(np.array([(-100.0, 0.0), (100.0, 0.0)]), roadwidth.Walls([])))
        self.assertIsNone(roadwidth.split(8.0), "no plausible carriageway")


class UseTest(unittest.TestCase):
    def test_measured_width_and_sidewalks(self):
        f = features([[(-300, 50), (300, 50)]], ["residential"])
        widths = lambda fid, ll: (14.0, 3.6)
        sp = roads.road_specs(f, lambda la, lo: np.ones(np.shape(la), bool), widths)[0]
        self.assertEqual((sp.width, sp.walk), (14.0, 3.6))
        walks = []
        roads.build_tile(FRAME, 0, 0, [f], flat, lambda la, lo: np.ones(np.shape(la), bool), walks=walks,
                         widths=widths)
        runs = [w for w in walks if "pts" in w]
        self.assertTrue(runs and all(w["w"] == 3.6 for w in runs))
        for w in runs:                          # the sidewalk centre: half the street + half the sidewalk out
            self.assertAlmostEqual(abs(float(np.mean(w["pts"][:, 1])) - 50.0), 7.0 + 1.8, delta=0.3)
        too_wide = lambda fid, ll: (60.0, 4.0)
        self.assertNotEqual(roads.road_specs(f, None, too_wide)[0].width, 60.0, "implausible: the default stays")
        svc = features([[(-300, 50), (300, 50)]], ["service"])
        self.assertEqual(roads.road_specs(svc, None, widths)[0].walk, roads.SIDEWALK["w"], "service: not measured")


if __name__ == "__main__":
    unittest.main()
