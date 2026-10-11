"""Road surfaces and markings as a mesh (roadmesh.py): ribbons laid on the ground, facing up, lifted by
layer, one material per chunk material, written as a cdae."""
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import cdae  # noqa: E402
import roadmesh  # noqa: E402
from test_roads import FRAME  # noqa: E402


def chunk(m, w, pts, tl=10.0):
    return {"m": m, "w": w, "tl": tl, "p": 20, "n": [list(p) for p in pts]}


def slope(la, lo):
    """A 5 % slope east (via the frame): heights from the points' ENU x."""
    e, _, _ = FRAME.geodetic_to_enu(np.asarray(la), np.asarray(lo), 0.0)
    return 10.0 + 0.05 * e


class RoadMeshTest(unittest.TestCase):
    def test_ribbons_drape_face_up_and_lift(self):
        chunks = [chunk("road_asphalt_2lane", 6.0, [(0, 0, 0), (40, 0, 0), (80, 20, 0)]),
                  chunk("line_yellow", 0.12, [(0, 0.12, 0), (40, 0.12, 0)])]
        P, T, UV, mats, M = roadmesh.build(FRAME, chunks, slope, (0.0, 0.0, 0.0))
        self.assertEqual(mats, ["road_asphalt_2lane", "line_yellow"])
        n = np.cross(P[T[:, 1]] - P[T[:, 0]], P[T[:, 2]] - P[T[:, 0]])
        self.assertTrue((n[:, 2] > 0).all(), "counter-clockwise seen from above")
        base = P[np.unique(T[M == 0])]
        want = 10.0 + 0.05 * base[:, 0] + roadmesh.LIFT["base"]
        self.assertLess(np.abs(base[:, 2] - want).max(), 0.01, "on the ground, lifted (frame curvature ~mm)")
        mark = P[np.unique(T[M == 1])]
        self.assertGreater((mark[:, 2] - (10.0 + 0.05 * mark[:, 0])).min(), roadmesh.LIFT["base"] + 0.005,
                           "markings above the asphalt")
        self.assertLessEqual(np.diff(np.unique(np.round(base[:, 0], 3))).max(), roadmesh.DRAPE_STEP + 1e-6)
        self.assertTrue(set(np.unique(UV[:, 0])) <= {0.0, 1.0}, "u across the width")

    def test_writes_one_primitive_per_material(self):
        chunks = [chunk("road_asphalt_2lane", 6.0, [(0, 0, 0), (30, 0, 0)]),
                  chunk("gutter1", 0.5, [(0, 3, 0), (30, 3, 0)]),
                  chunk("road_asphalt_2lane", 6.0, [(10, -20, 0), (10, 20, 0)])]
        mesh = roadmesh.build(FRAME, chunks, lambda la, lo: np.zeros(np.shape(la)), (0.0, 0.0, 0.0))
        with tempfile.TemporaryDirectory() as d:
            roadmesh.write(os.path.join(d, "r.cdae"), mesh)
            shape = cdae.read(os.path.join(d, "r.cdae"))
        self.assertEqual([m["name"] for m in shape["materials"]], ["road_asphalt_2lane", "gutter1"])
        self.assertEqual(roadmesh.layer_of({"m": "line_white"}), "mark")
        self.assertIsNone(roadmesh.build(FRAME, [], lambda la, lo: la, (0, 0, 0)))


if __name__ == "__main__":
    unittest.main()


class BridgeTest(unittest.TestCase):
    def test_deck_heights_straight_between_abutments(self):
        import roads
        parts = [np.array([[0.0, 0], [60, 0], [100, 0]]), np.array([[100.0, 0], [200, 0]])]
        z = roads.deck_heights(parts, lambda xy: 10.0 + 0.1 * xy[:, 0])     # abutments at x 0 and 200
        self.assertTrue(np.allclose(z[0], [10.0, 16.0, 20.0]), z[0])
        self.assertTrue(np.allclose(z[1], [20.0, 30.0]), z[1])
        loop = [np.array([[0.0, 0], [10, 0]]), np.array([[10.0, 0], [0, 0]])]
        self.assertTrue(np.isnan(roads.deck_heights(loop, lambda xy: xy[:, 0])[0]).all(), "no abutment known")

    def test_deck_slab_collides_and_closes(self):
        deck = {"m": "road_asphalt_2lane", "w": 8.0, "tl": 11.0, "p": 20, "deck": True, "fixed": True,
                "n": [[0, 0, 20.0], [50, 0, 22.0]]}
        P, T, UV, mats, M = roadmesh.deck([deck], (0.0, 0.0, 0.0))
        self.assertEqual(mats, [roadmesh.DECK_SIDE], "an opaque slab")
        n = np.cross(P[T[:, 1]] - P[T[:, 0]], P[T[:, 2]] - P[T[:, 0]])
        n /= np.linalg.norm(n, axis=1)[:, None]
        flat = np.abs(n[:, 2]) > 0.5
        c = P[T].mean(1)
        high = c[:, 2] > 20.0 + 0.04 * c[:, 0] - roadmesh.DECK_DEPTH / 2   # the top rises 20 -> 22 m
        self.assertTrue((n[flat & high, 2] > 0).all() and (flat & high).any(), "the top faces up")
        self.assertTrue((n[flat & ~high, 2] < 0).all() and (flat & ~high).any(), "the underside faces down")
        self.assertAlmostEqual(P[:, 2].max(), 22.0)
        self.assertAlmostEqual(P[:, 2].min(), 20.0 - roadmesh.DECK_DEPTH)
        Pr, Tr, _, mr, _ = roadmesh.build(FRAME, [deck], lambda la, lo: la, (0, 0, 0))
        self.assertEqual(mr, ["road_asphalt_2lane"], "the asphalt is in the road mesh ...")
        self.assertAlmostEqual(Pr[:, 2].max(), 22.0 + roadmesh.DECK_SKIN, msg="... just over the slab")


class BridgeProfilesTest(unittest.TestCase):
    def test_chain_across_cells_and_clearance(self):
        import mapcells
        import roads
        from test_roads import features
        # a bridge in two parts (two features) over a street at x = 0; the ground is flat at 10 m
        f = features([[(-300, 0), (0, 0)], [(0, 0), (300, 0)], [(0, -200), (0, 200)]],
                     ["primary", "primary", "residential"],
                     levels=[(0, 1.0, 0.5, 1.0), (1, 1.0, 0.0, 0.5)], ids=["a", "b", "street"])
        calls = []

        def cell_fn(key):
            calls.append(str(key))
            return f
        flat = lambda la, lo: np.full(np.shape(la), 10.0)
        bp = roads.BridgeProfiles(FRAME, cell_fn, flat)
        got1 = {fid: (xy, z) for fid, spec, xy, z, lon in bp.parts([f], (-256, -128, 0, 128))}
        self.assertEqual(set(got1), {"a", "b"}, "both halves of the chain")
        za = got1["a"][1]
        self.assertAlmostEqual(za[0], 10.0, delta=0.2, msg="an abutment on the ground")
        k = int(np.argmin(np.abs(got1["a"][0][:, 0] - 0.0)))
        self.assertGreaterEqual(za[k], 10.0 + roads.DECK_ROAD_CLEAR - 0.6, "headroom over the street")
        bp2 = roads.BridgeProfiles(FRAME, cell_fn, flat)        # another tile asking first: same deck
        got2 = {fid: (xy, z) for fid, spec, xy, z, lon in bp2.parts([f], (0, -128, 256, 128))}
        self.assertTrue(np.allclose(got2["a"][1], got1["a"][1]) and np.allclose(got2["b"][1], got1["b"][1]))
        ea, eb = got1["a"], got1["b"]
        self.assertAlmostEqual(float(ea[1][-1]), float(eb[1][0]), places=6, msg="the halves meet")

    def test_crossing_decks_stack_by_level(self):
        import roads
        from test_roads import features
        # two bridges crossing at (0, 0): level 1 east-west, level 2 north-south; ground flat at 10 m
        f = features([[(-300, 0), (300, 0)], [(0, -300), (0, 300)]], ["primary", "motorway"],
                     levels=[(0, 1.0, 0.2, 0.8), (1, 2.0, 0.2, 0.8)], ids=["low", "high"])
        flat = lambda la, lo: np.full(np.shape(la), 10.0)
        bp = roads.BridgeProfiles(FRAME, lambda key: f, flat)
        got = {fid: (xy, z) for fid, spec, xy, z, lon in bp.parts([f], (-128, -128, 128, 128))}
        lo_xy, lo_z = got["low"]
        hi_xy, hi_z = got["high"]
        zl = lo_z[int(np.argmin(np.abs(lo_xy[:, 0])))]
        zh = hi_z[int(np.argmin(np.abs(hi_xy[:, 1])))]
        self.assertGreaterEqual(zh - zl, roads.DECK_ROAD_CLEAR - 0.6, (zl, zh))
