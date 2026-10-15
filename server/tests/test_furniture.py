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
        xs = sorted(x[1] for x in a + b if x[0] == furniture.G_LAMP)
        self.assertTrue(np.allclose(np.diff(xs), furniture.LAMP_SPACING["residential"][0], atol=0.5))

    def test_town_streets_get_wired_poles(self):
        r = road("t", "residential", True)
        rows, wires = [], []
        for side in (1, -1):
            rows += furniture.place([r], self.walks(r, side), (0, 0, 256, 256), wires=wires, town=True)
        groups = [x[0] for x in rows]
        self.assertNotIn(furniture.G_LAMP, groups)
        self.assertGreaterEqual(groups.count(furniture.G_POLE), 5)
        self.assertGreaterEqual(groups.count(furniture.G_POLELAMP), 2)
        self.assertTrue(wires)

    def test_hydrants_on_town_streets(self):
        r = road("h", "residential", True, x0=-300, x1=900)
        rows = []
        for side in (1, -1):
            rows += furniture.place([r], self.walks(r, side), (0, 0, 600, 256))
        hy = sorted(x[1] for x in rows if x[0] == furniture.G_HYDRANT)
        self.assertGreaterEqual(len(hy), 8)
        self.assertTrue(np.allclose(np.diff(hy), furniture.HYDRANT_SPACING / 2, atol=1.0), "alternating sides")


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

    def test_divided_road_crossing_one_signal_per_way_in(self):
        # Boston St: a divided road (two one-way carriageways 20 m apart) crossing a street is two junctions; the
        # user saw ten heads there (2026-10-14). One per way in: none on the arms leaving, none across the median.
        def seg(fid, cls, a, b, oneway=False):
            sp = roads.Spec(10.0, "paved", True, cls, oneway, 2.5, 1 if oneway else 0)
            return (fid, sp, np.array([(a[0], a[1], 0.0), (b[0], b[1], 0.0)]), 0.0)
        rs = [seg("eb_w", "primary", (20, 100), (100, 100), True), seg("eb_e", "primary", (100, 100), (180, 100), True),
              seg("wb_e", "primary", (180, 120), (100, 120), True), seg("wb_w", "primary", (100, 120), (20, 120), True),
              seg("s", "tertiary", (100, 100), (100, 20)), seg("link", "tertiary", (100, 100), (100, 120)),
              seg("n", "tertiary", (100, 120), (100, 200))]
        rows = furniture.junction_props(rs, (0, 0, 256, 256))
        self.assertEqual([r[0] for r in rows], [furniture.G_SIGNAL] * 4, "the two carriageways' and the street's ways in")

    def test_left_hand_traffic_stands_them_on_the_left(self):
        rs = [self.arm("e", "residential", 1, 0), self.arm("w", "residential", -1, 0), self.arm("s", "residential", 0, -1)]
        rows = furniture.junction_props(rs, (0, 0, 256, 256), left_hand=True)
        self.assertEqual(len(rows), 1)
        self.assertLess(rows[0][1], 100.0, "on the left of northbound drivers (west side)")

    def test_stop_on_the_stem_of_a_minor_t(self):
        rs = [self.arm("e", "residential", 1, 0), self.arm("w", "residential", -1, 0), self.arm("s", "residential", 0, -1)]
        rows = furniture.junction_props(rs, (0, 0, 256, 256))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], furniture.G_STOP)
        self.assertLess(rows[0][2], 100.0, "on the southern stem")
        self.assertGreater(rows[0][1], 100.0, "on its right for northbound drivers (east side)")



class SpeedSignTest(unittest.TestCase):
    def test_a_sign_after_each_end_facing_its_traffic(self):
        r = road("t", "tertiary", True, x0=0.0, x1=400.0)
        rows = furniture.speed_signs([r], {"t": 30.0 / 0.999}, (-50, -50, 450, 150))
        self.assertEqual(sorted(x[0] for x in rows), ["speed30", "speed30"])
        east = min(rows, key=lambda x: x[1])
        self.assertAlmostEqual(east[1], furniture.SIGN_IN_M, places=1)
        self.assertLess(east[2], 50.0, "eastbound traffic: its kerb is the south side")
        west = max(rows, key=lambda x: x[1])
        self.assertGreater(west[2], 50.0)

    def test_no_sign_without_a_model_or_on_short_roads(self):
        self.assertEqual(furniture.speed_signs([road("t", "tertiary", True)], {"t": 35.0}, (-500, -50, 700, 150)), [])
        self.assertEqual(furniture.speed_signs([road("s", "tertiary", True, x0=0.0, x1=100.0)], {"s": 30.0},
                                               (-500, -50, 700, 150)), [])


    def test_km_h_signs_where_limits_are_in_km_h(self):
        """Switzerland: an 80 km/h road gets the round 80 sign, a 50 km/h one none (no model), never mph signs."""
        r = road("t", "tertiary", True, x0=0.0, x1=400.0)
        rows = furniture.speed_signs([r], {"t": 80 / 1.609344}, (-50, -50, 450, 150), mph_signs=False)
        self.assertEqual(sorted(x[0] for x in rows), ["speedk80", "speedk80"])
        self.assertEqual(furniture.speed_signs([r], {"t": 50 / 1.609344}, (-50, -50, 450, 150), mph_signs=False), [])


if __name__ == "__main__":
    unittest.main()
