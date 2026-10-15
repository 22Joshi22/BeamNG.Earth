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

    def test_west_town_one_storey_main_street(self):
        shops = [bld.plan(rect(x * 30, 0, 7, 25), {"class": ""}, 2, 37.36, -118.39, west_town=True)
                 for x in range(40)]
        self.assertTrue(all(s.floors in (1, 2) for s in shops), "attached shops, not 2-4 storey row houses")
        self.assertGreater(sum(s.floors == 1 for s in shops), 20)
        one = next(s for s in shops if s.floors == 1)
        self.assertGreater(one.height, bld.FLOOR_M + 0.6 + 1.0, "a false front's parapet")
        city = [bld.plan(rect(x * 30, 0, 5, 18), {"class": ""}, 2) for x in range(10)]
        self.assertTrue(all(s.floors >= 2 for s in city), "a city's row houses unchanged")
        given = bld.plan(rect(0, 0, 20, 20), {"class": "", "height": 18.0}, 0, west_town=True)
        self.assertEqual(given.floors, 6, "data heights win")

    def test_district_coverage(self):
        from test_roads import features
        f = features([[(0, 0), (10, 0)]], ["x"])
        lat, lon = f.lonlat[0, 1], f.lonlat[0, 0]
        # one 100 x 100 m square footprint at the centre, as a buildings layer
        sq = features([[(-50, -50), (50, -50), (50, 50), (-50, 50)]], ["x"])
        bld._cell_stats.clear()
        c = bld.district_coverage(lat, lon, [("k1", sq)])
        self.assertAlmostEqual(c, 10000.0 / (np.pi * bld.DISTRICT_M ** 2), delta=1e-4)
        self.assertAlmostEqual(bld.district_coverage(lat, lon, [("k1", None)]), c, msg="cached by cell")

    def test_parking_structure_not_a_barn(self):
        g = bld.plan(rect(0, 0, 60, 60), {"class": "parking"}, 0, 39.2, -76.86)
        self.assertGreaterEqual(g.floors, 4)
        self.assertFalse(g.facade.startswith(bld.SIDING), "garage panels, not barn siding")
        c = bld.plan(rect(0, 0, 8, 6), {"class": "parking"}, 0, 39.2, -76.86)
        self.assertEqual(c.floors, 1, "a carport")

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


class TowerShopTest(unittest.TestCase):
    def test_city_towers_have_a_shop_ground_floor(self):
        # Midtown, 2026-10-14: classless towers had their curtain wall down to the pavement
        t = bld.plan(rect(0, 0, 40, 30), {"class": "", "height": 120.0}, 2, 40.758, -73.985, dense=True)
        self.assertTrue(t.shop)
        low = bld.plan(rect(0, 0, 40, 30), {"class": "", "height": 9.0}, 2, 40.758, -73.985, dense=True)
        self.assertFalse(low.shop, "a low building keeps its own rules")
        suburb = bld.plan(rect(0, 0, 40, 30), {"class": "", "height": 120.0}, 0, 40.758, -73.985)
        self.assertFalse(suburb.shop, "outside a dense core")


class ParkingDeckTest(unittest.TestCase):
    def deck(self, w, d, gmax=0.0):
        spec = bld.plan(rect(0, 0, w, d), {"class": "parking", "num_floors": 3, "height": 9.0}, 0, 39.28, -76.59)
        return spec, bld.build([(spec, 0.0, gmax)])

    def tops(self, P, T, z, tol=0.05):
        """Triangles lying flat at height z."""
        tri = P[T]
        return tri[(np.abs(tri[:, :, 2] - z) < tol).all(1)]

    def covered(self, tris, x, y):
        for a, b, c in tris[:, :, :2]:
            d = [(b[0] - a[0]) * (y - a[1]) - (b[1] - a[1]) * (x - a[0]),
                 (c[0] - b[0]) * (y - b[1]) - (c[1] - b[1]) * (x - b[0]),
                 (a[0] - c[0]) * (y - c[1]) - (a[1] - c[1]) * (x - c[0])]
            if min(d) >= -1e-9 or max(d) <= 1e-9:
                return True
        return False

    def test_a_big_garage_is_open_decks_with_a_ramp_well(self):
        # the user, 2026-10-14: "garage buildings ... are solid"
        spec, (P, UV, T, M, mats, C) = self.deck(60, 40)
        self.assertTrue(spec.deck)
        zs = [0.03 + k * 3.0 for k in range(4)]
        for z in zs:
            self.assertGreaterEqual(len(self.tops(P, T, z)), 2, f"a deck at {z} m")
        # the longest edge is along x (y = 0): the well is cut from it on the upper decks, the ground deck is whole
        xs, ys = 30.0, bld.DECK_EDGE_M + 1.5 * bld.DECK_LANE_M        # the middle of the second lane
        self.assertTrue(self.covered(self.tops(P, T, zs[0]), xs, ys), "the ground deck under the ramps")
        self.assertFalse(self.covered(self.tops(P, T, zs[1]), xs, ys), "the well open on deck 1")
        self.assertTrue(self.covered(self.tops(P, T, zs[1]), 30.0, 30.0), "deck 1 beyond the well")
        self.assertEqual(bld.collider_boxes([(spec, 0.0, 0.0)], 0, 0), [], "driven into: no keep-out box")

    def test_ramps_are_drivable(self):
        _, (P, UV, T, M, mats, C) = self.deck(60, 40)
        tri = P[T]
        n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        up = n[:, 2] / np.maximum(np.linalg.norm(n, axis=1), 1e-9)
        sloped = (up > 0.9) & (up < 0.9999)
        self.assertGreaterEqual(int(sloped.sum()), 6, "three ramps facing up")
        self.assertTrue(np.all(np.sqrt(1 - up[sloped] ** 2) / up[sloped] <= bld.DECK_SLOPE + 1e-6), "not too steep")

    def test_too_short_or_sloped_keeps_the_box(self):
        spec, (P, UV, T, M, mats, C) = self.deck(30, 30)
        self.assertNotEqual(bld.collider_boxes([(spec, 0.0, 0.0)], 0, 0), [], "falls back")
        self.assertFalse(len(self.tops(P, T, 3.03)), "no decks: the closed box")
        spec, (P, UV, T, M, mats, C) = self.deck(60, 40, gmax=4.0)
        self.assertFalse(len(self.tops(P, T, 7.03)), "on a slope: the closed box")


class SplitTest(unittest.TestCase):
    def test_split_under_the_index_limit(self):
        items = [(bld.plan(rect(x * 30, 0, 20, 12), {"class": ""}), 0.0, 0.0) for x in range(30)]
        one = bld.build(items)
        parts = bld.build_parts(items, max_verts=len(one[0]) // 3)
        self.assertGreaterEqual(len(parts), 3)
        self.assertTrue(all(len(p[0]) <= len(one[0]) // 3 for p in parts))
        self.assertEqual(sum(len(p[2]) for p in parts), len(one[2]), "every triangle in exactly one part")
        self.assertEqual(len(bld.build_parts(items)), 1, "a small tile stays one mesh")


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
        lip = (mid[:, 2] > spec.height) & (mid[:, 2] < spec.height + bld.CORNICE_UP_M + 0.01) & (np.abs(nz) < 0.01)
        good |= lip                                                   # the cornice lip's inner face, above the roof
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
        shops = signed = 0
        for k in range(20):
            spec = bld.plan(rect(k * 30, 0, 10, 14), {"class": ""}, 2, 39.4, -77.4)
            street = (np.array([(-50.0, -7.0), (900.0, -7.0)]), 6.0, "tertiary", True)
            rows = bld.doors([(spec, 0.0, 0.0)], [street], 0.0, 0.0)
            if spec.shop:
                shops += 1
                self.assertEqual(rows, [], "a shopfront, no house door")
                if not spec.plain:
                    self.assertEqual(spec.sign, 0, "the sign band over the street (south) wall")
                    m = bld.Mesh()
                    bld._signs(m, spec, spec.ring, 4.2, bld._Looks(spec.seed))
                    if m.n:                     # a few shops have none (hashed)
                        signed += 1
                        ys = np.array(m.P)[:, 1]
                        self.assertTrue((ys < 0.01).all() and (ys > -bld.SIGN_OUT - 0.01).all(),
                                        "boxes just out from the south wall")
        self.assertGreater(shops, 8)
        self.assertGreater(signed, shops // 2)

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

    def test_porches_on_the_front_of_some_houses(self):
        with_porch = 0
        for k in range(40):
            spec = bld.plan(rect(k * 40, 0, 10, 8), {"class": "house"}, 0, 39.4, -77.4)
            bld.doors([(spec, 0.0, 0.0)], [(np.array([(-50.0, -6.0), (2000.0, -6.0)]), 4.0, "residential", True)], 0.0, 0.0)
            if not spec.porch or not spec.door:
                continue
            with_porch += 1
            m = bld.Mesh()
            bld._porch(m, spec, 1.0, bld._Looks(spec.seed))
            P = np.array(m.P)
            self.assertTrue(m.n > 0)
            self.assertAlmostEqual(P[:, 1].min(), -(bld.PORCH_DEPTH_M + 0.2), delta=0.01, msg="out in front of the south wall")
            self.assertGreaterEqual(P[:, 1].max(), -0.01)
            self.assertAlmostEqual(P[:, 2].max(), 1.0 + bld.PORCH_DECK_M + bld.PORCH_H_M + 0.18, places=3)
        self.assertTrue(10 < with_porch < 35, with_porch)

    def test_front_fence_with_a_gate(self):
        a, d = np.array([0.0, 20.0]), np.array([1.0, 0.0])         # a 12 m house front 20 m from a road at y = 0
        rows = bld._front_fence(type("S", (), {"seed": 0.3})(), a, d, 12.0, 0.5, np.array([-100.0, 0.0]),
                                np.array([300.0, 0.0]), 300.0 ** 2, 4.0, 1.5, False)
        self.assertTrue(rows)
        for g, x, y, z, yaw, sc, h, r in rows:
            self.assertEqual(g, bld.G_FENCE)
            self.assertAlmostEqual(y, 4.0 + bld.FENCE_BACK_M, places=2)
            self.assertEqual(yaw, 0.0)
            self.assertGreater(abs(x - 6.0), bld.FENCE_SEG_M / 2, "a gap in front of the door")
        self.assertEqual(bld._front_fence(type("S", (), {"seed": 0.3})(), np.array([0.0, 8.0]), d, 12.0, 0.5,
                                          np.array([-100.0, 0.0]), np.array([300.0, 0.0]), 300.0 ** 2, 4.0, 1.5,
                                          False), [], "no fence without a front yard")

    def test_company_sign_on_a_warehouse_front(self):
        seen = 0
        for k in range(30):
            spec = bld.plan(rect(k * 200, 0, 60, 40), {"class": "industrial"}, 0, 39.4, -77.4)
            if spec.windows is not False:
                continue
            rows = bld.doors([(spec, 0.0, 0.0)], [(np.array([(-50.0, -10.0), (9000.0, -10.0)]), 6.0, "primary", True)],
                             0.0, 0.0)
            for g, x, y, z, yaw, sc, h, r in rows:
                if g.startswith("wallsign"):
                    seen += 1
                    self.assertAlmostEqual(y, -0.35, places=2, msg="on the south (street) wall")
                    self.assertLess(z + h / 2, spec.height + 0.01)
        self.assertGreater(seen, 3)

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


class PhotoFillTest(unittest.TestCase):
    def test_big_footprint_fills_from_the_ground_around(self):
        rgb = np.zeros((512, 512, 3), np.uint8)
        rgb[:] = (40, 160, 60)
        mask = np.zeros((512, 512), bool)
        mask[10:500, 10:500] = True         # far wider than the first blur
        rgb[mask] = (200, 200, 200)
        out = bld.fill_photo(rgb, mask, 256.0, 512)
        self.assertTrue(np.allclose(out[mask].astype(int), (40, 160, 60), atol=4), "no black holes, no roof colour")


if __name__ == "__main__":
    unittest.main()
