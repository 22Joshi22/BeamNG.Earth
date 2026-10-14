"""buildings: footprints become closed walls and a roof, heights predicted where the data has none,
deterministic, every triangle facing out."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import buildings as bld  # noqa: E402


def rect(x, y, w, d):
    return np.array([(x, y), (x + w, y), (x + w, y + d), (x, y + d)], np.float64)


class PolygonTest(unittest.TestCase):
    def test_clean_ring_ccw_without_collinear(self):
        p = np.array([(0, 0), (0, 10), (5, 10.1), (10, 10), (10, 0), (0, 0)], np.float64)    # clockwise, closed
        r = bld.clean_ring(p)
        self.assertEqual(len(r), 4)
        self.assertGreater(bld._area2(r), 0)

    def test_triangulate_concave(self):
        L = np.array([(0, 0), (10, 0), (10, 4), (4, 4), (4, 10), (0, 10)], np.float64)
        t = bld.triangulate(L)
        self.assertEqual(len(t), 4)
        area = sum(abs(bld._area2(L[[a, b, c]])) / 2 for a, b, c in t)
        self.assertAlmostEqual(area, bld._area2(L) / 2)


class PlanTest(unittest.TestCase):
    def test_given_height_kept(self):
        s = bld.plan(rect(0, 0, 20, 20), {"class": "", "height": 18.0})
        self.assertAlmostEqual(s.height, 18.0)
        self.assertEqual(s.floors, 6)

    def test_row_house_three_storeys_or_two_flat(self):
        s = bld.plan(rect(0, 0, 5, 18), {"class": ""}, neighbours=2)
        self.assertIn(s.floors, (2, 3))
        self.assertEqual(s.roof, "flat")

    def test_small_house_pitched(self):
        roofs = {bld.plan(rect(x * 50, 0, 10, 8), {"class": "house"}).roof for x in range(20)}
        self.assertEqual(roofs, {"gabled", "hipped"})

    def test_big_box_blank_walls_one_storefront(self):
        s = bld.plan(rect(0, 0, 60, 40), {"class": "retail"}, 0, 38.57, -109.55, town=True)
        self.assertFalse(s.windows)
        self.assertTrue(s.wall.startswith(bld.PANEL))
        rows = bld.doors([(s, 0.0, 0.0)], [(np.array([(-50.0, -10.0), (200.0, -10.0)]), 6.0, "trunk", True)], 0.0, 0.0)
        self.assertEqual(rows, [])
        self.assertEqual(s.front, 0, "the south wall faces the road")
        P, UV, T, M, mats, C = bld.build([(s, 0.0, 0.0)])
        self.assertIn(bld.MATERIALS.index(bld.SHOP), set(M.tolist()))

    def test_named_kinds(self):
        big = rect(0, 0, 200, 60)
        t = bld.plan(big, {"class": "", "name": "Main Terminal"}, 0, 39.17, -76.67)
        self.assertEqual((t.floors, t.facade), (2, bld.OFFICE))
        g = bld.plan(big, {"class": "", "name": "Hourly Garage"}, 0, 39.17, -76.67)
        self.assertIn(g.floors, (4, 5))
        self.assertLess(g.height, 18.0)
        h = bld.plan(big, {"class": "", "name": "Hangar 4"}, 0, 39.17, -76.67)
        self.assertTrue(h.wall.startswith(bld.METALWALL))
        c = bld.plan(rect(0, 0, 30, 14), {"class": "church"}, 0, 37.36, -118.39)
        self.assertTrue(c.steeple)
        P = bld.build([(c, 0.0, 0.0)])[0]
        self.assertGreater(P[:, 2].max(), 25.0, "a spire over the nave")

    def test_dense_core_small_buildings_are_row_houses(self):
        s = bld.plan(rect(0, 0, 8, 12), {"class": "house"}, 0, 39.28, -76.59, dense=True)
        self.assertEqual(s.roof, "flat")
        self.assertIsNone(s.wall)

    def test_barn_gabled_metal(self):
        s = bld.plan(rect(0, 0, 30, 15), {"class": "barn"}, 0, 40.0, -76.1)
        self.assertEqual(s.roof, "gabled")
        self.assertTrue(s.roof_mat.startswith(bld.ROOF_METAL))
        self.assertGreater(s.height, 5.0)

    def test_deterministic(self):
        a = bld.plan(rect(3, 7, 12, 9), {"class": ""})
        b = bld.plan(rect(3, 7, 12, 9), {"class": ""})
        self.assertEqual((a.floors, a.roof, a.facade, a.band), (b.floors, b.roof, b.facade, b.band))


class MeshTest(unittest.TestCase):
    def check_outward(self, ring, attrs):
        spec = bld.plan(ring, attrs)
        P, UV, T, M, mats, C = bld.build([(spec, 0.0, 0.0)])
        c = np.append(ring.mean(0), spec.height / 2)
        n = np.cross(P[T[:, 1]] - P[T[:, 0]], P[T[:, 2]] - P[T[:, 0]])
        mid = P[T].mean(1)
        good = ((mid - c) * n).sum(1) > 0
        nz = n[:, 2] / np.maximum(np.linalg.norm(n, axis=1), 1e-12)
        good |= (nz < -0.99) & (mid[:, 2] > spec.height - 1.0)     # a cornice's underside faces down
        good |= mid[:, 2] > spec.height + bld.CORNICE_UP_M + 0.01   # roof boxes: their own outsides
        self.assertTrue(good.all(), f"{(~good).sum()} of {len(T)} triangles face in ({spec.roof})")
        self.assertLess(M.max(), len(mats))
        return spec, P

    def test_flat_box(self):
        spec, P = self.check_outward(rect(0, 0, 30, 20), {"class": "retail", "height": 9.0})
        self.assertAlmostEqual(P[:, 2][P[:, 2] < 9.5].max(), 9.0 + bld.CORNICE_UP_M)
        self.assertGreater(P[:, 2].max(), 10.0, "boxes on a 600 m2 flat roof")
        self.assertAlmostEqual(P[:, 2].min(), -bld.FOUNDATION_M)

    def test_gabled_and_hipped(self):
        for shape in ("gabled", "hipped"):
            spec, P = self.check_outward(rect(0, 0, 12, 8), {"class": "house", "roof_shape": shape})
            self.assertEqual(spec.roof, shape)
            self.assertGreater(P[:, 2].max(), spec.height + 1.5, "a ridge above the eaves")


class DoorTest(unittest.TestCase):
    def test_door_faces_the_road(self):
        spec = bld.plan(rect(0, 0, 10, 8), {"class": "house"})
        road = (np.array([(-50.0, -6.0), (50.0, -6.0)]), 4.0)        # south of the house
        rows = bld.doors([(spec, 0.0, 0.0)], [road], 0.0, 0.0)
        self.assertEqual(len(rows), 1)
        g, x, y, z, yaw, sc, h, r = rows[0]
        self.assertEqual(g, "door")
        self.assertAlmostEqual(y, -0.03, places=2)                     # on the south wall
        self.assertAlmostEqual(yaw, 0.0)                               # the wall runs +x

    def test_main_street_shopfronts(self):
        shops = 0
        for k in range(20):
            spec = bld.plan(rect(k * 30, 0, 10, 14), {"class": ""}, 2, 39.4, -77.4)
            street = (np.array([(-50.0, -7.0), (900.0, -7.0)]), 6.0, "tertiary", True)
            rows = bld.doors([(spec, 0.0, 0.0)], [street], 0.0, 0.0)
            if spec.shop:
                shops += 1
                self.assertEqual(rows, [], "a shopfront, no house door")
        self.assertGreater(shops, 8)

    def test_driveway_to_the_road(self):
        for k in range(40):                                  # a house with a siding wall (hashed)
            spec = bld.plan(rect(k * 40, 20, 12, 9), {"class": "house"}, 0, 39.2, -76.9)
            if spec.wall:
                break
        self.assertIsNotNone(spec.wall)
        drives = []
        bld.doors([(spec, 0.0, 0.0)], [(np.array([(-50.0, 6.0), (2000.0, 6.0)]), 4.0, "residential", True)], 0.0, 0.0,
                  drives)
        self.assertEqual(len(drives), 1)
        n = np.array(drives[0]["n"])
        self.assertAlmostEqual(n[0, 1], 19.8, delta=0.3, msg="from the front wall")
        self.assertAlmostEqual(n[-1, 1], 10.1, delta=0.3, msg="to the road's edge")

    def test_no_door_without_a_road(self):
        spec = bld.plan(rect(0, 0, 10, 8), {"class": "house"})
        self.assertEqual(bld.doors([(spec, 0.0, 0.0)], [(np.array([(500.0, 0), (600.0, 0)]), 4.0)], 0, 0), [])


class Feats:
    def __init__(self, ids, rings, **attrs):
        self.ids = np.array(ids, object)
        self.kind = np.full(len(ids), 3, np.uint8)
        self.rings = rings
        self.attrs = {k: np.array(v, object if isinstance(v[0], str) else np.float64) for k, v in attrs.items()}

    def parts(self, i):
        return [[np.asarray(self.rings[i], np.float64)]]


class PartsTest(unittest.TestCase):
    def test_tower_on_podium(self):
        import geodesy
        f = geodesy.EnuFrame(39.28, -76.59)

        def ll(x, y, w, d):
            e = np.array([x, x + w, x + w, x, x]); n = np.array([y, y, y + d, y + d, y])
            la, lo, _ = f.enu_to_geodetic(e, n, 0.0)
            return np.stack([lo, la], 1)
        b = Feats(["B"], [ll(10, 10, 40, 40)], **{"class": ["commercial"], "height": [90.0],
                                                   "has_parts": [1.0], "min_height": [np.nan]})
        parts = Feats(["p1", "p2", "p3"], [ll(10, 10, 40, 40), ll(20, 20, 20, 20), ll(10, 10, 40, 8)],
                      building_id=["B", "B", "B"], height=[15.0, np.nan, 30.0], min_height=[np.nan, np.nan, 20.0],
                      num_floors=[np.nan, np.nan, np.nan], min_floor=[np.nan, np.nan, np.nan])
        items = bld.tile_buildings(f, 0, 0, 256, 256, [b], lambda la, lo: np.zeros(np.shape(la)), part_layers=[parts])
        self.assertEqual(len(items), 3, "the parts, not the footprint")
        tops = sorted(round(s.height + s.min_h) for s, _, _ in items)
        self.assertEqual(tops, [15, 30, 90], "the podium, a floating part, the tower at the building's height")
        self.assertEqual(len({s.seed for s, _, _ in items}), 1, "one look")
        P = bld.build(items)[0]
        self.assertAlmostEqual(P[:, 2][P[:, 2] < 90.5].max(), 90.0, delta=0.01)


if __name__ == "__main__":
    unittest.main()
