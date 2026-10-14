"""furniture: street lights along sidewalk runs at fixed stations of their road, utility poles and their
wires along country roads; the same stations whichever tile draws them."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import furniture  # noqa: E402
import roads  # noqa: E402


def road(fid, cls, urban, x0=-300.0, x1=600.0, y=50.0, width=8.0):
    pts = np.array([(x0, y, 0.0), (x1, y, 0.0)])
    return (fid, roads.Spec(width, "paved", urban, cls, False), pts, 0.0)


class LampTest(unittest.TestCase):
    def walks(self, r, side):
        fid, spec, pts, _ = r
        line = roads.offset_polyline(pts, side * (spec.width / 2 + 1.25))
        return [{"fid": fid, "pts": line, "side": side, "w": 2.5, "caps": (False, False)}]

    def test_residential_one_side_spaced(self):
        r = road("a", "residential", True)
        rows = []
        for side in (1, -1):
            rows += furniture.place([r], self.walks(r, side), (0, 0, 256, 256))
        lamps = [x for x in rows if x[0] == furniture.G_LAMP]
        self.assertTrue(lamps)
        ys = {round(x[2]) for x in lamps}
        self.assertEqual(len(ys), 1, "one side of a residential street")
        xs = sorted(x[1] for x in lamps)
        self.assertTrue(np.allclose(np.diff(xs), furniture.LAMP_SPACING["residential"][0], atol=0.5))
        # the arm (local +x) points at the road
        lamp = lamps[0]
        d = np.array([np.cos(np.radians(lamp[4])), np.sin(np.radians(lamp[4]))])
        self.assertGreater(float(d @ np.array([0.0, 50.0 - lamp[2]])), 0)

    def test_same_stations_in_neighbouring_tiles(self):
        r = road("b", "residential", True)
        side = 1 if furniture._h01("b", "side") < 0.5 else -1
        a = furniture.place([r], self.walks(r, side), (0, 0, 256, 256))
        b = furniture.place([r], self.walks(r, side), (256, 0, 512, 256))
        xs = sorted(x[1] for x in a + b)
        self.assertTrue(np.allclose(np.diff(xs), furniture.LAMP_SPACING["residential"][0], atol=0.5))


class PoleTest(unittest.TestCase):
    def test_poles_and_wires_on_country_roads(self):
        wires = []
        rows = furniture.place([road("c", "tertiary", False)], [], (0, 0, 256, 256), wires=wires)
        poles = [x for x in rows if x[0] == furniture.G_POLE]
        self.assertGreaterEqual(len(poles), 5)
        self.assertTrue(all(abs(abs(x[2] - 50.0) - (4.0 + furniture.POLE_OFF_M)) < 0.01 for x in poles))
        self.assertEqual(len(wires), 3 * len(poles), "three wires per span starting in the tile")
        m = furniture.wire_mesh(wires, (0, 0, 0))
        self.assertEqual(len(m[1]) % 4, 0)

    def test_no_poles_in_town(self):
        self.assertEqual(furniture.place([road("d", "tertiary", True)], [], (0, 0, 256, 256)), [])


class JunctionTest(unittest.TestCase):
    def arm(self, fid, cls, dx, dy, c=(100.0, 100.0)):
        pts = np.array([(c[0], c[1], 0.0), (c[0] + dx * 80, c[1] + dy * 80, 0.0)])
        return (fid, roads.Spec(10.0, "paved", True, cls, False), pts, 0.0)

    def test_signals_where_major_roads_cross(self):
        rs = [self.arm(f"a{i}", "tertiary", dx, dy) for i, (dx, dy) in enumerate([(1, 0), (-1, 0), (0, 1), (0, -1)])]
        rows = furniture.junction_props(rs, (0, 0, 256, 256))
        self.assertEqual(sorted(r[0] for r in rows), [furniture.G_SIGNAL] * 4)
        for r in rows:              # on the approach's right, outside the carriageways
            self.assertGreater(max(abs(r[1] - 100), abs(r[2] - 100)), 5.0)

    def test_stop_on_the_stem_of_a_minor_t(self):
        rs = [self.arm("e", "residential", 1, 0), self.arm("w", "residential", -1, 0), self.arm("s", "residential", 0, -1)]
        rows = furniture.junction_props(rs, (0, 0, 256, 256))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], furniture.G_STOP)
        self.assertLess(rows[0][2], 100.0, "on the southern stem")
        self.assertGreater(rows[0][1], 100.0, "on its right for northbound drivers (east side)")


if __name__ == "__main__":
    unittest.main()
