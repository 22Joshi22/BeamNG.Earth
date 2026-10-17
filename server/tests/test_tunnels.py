"""Tunnels (2026-10-17, the user: "tunnels through terrain need to be added"): the floor profile, the tube mesh and the
terrain block's holes at the portals."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import roadmesh  # noqa: E402
import roads  # noqa: E402

Spec = roads.Spec


def line(x0, x1, step=4.0, y=0.0):
    xs = np.arange(x0, x1 + 1e-9, step)
    return np.c_[xs, np.full(len(xs), y)]


def hill(xy):
    """A mountain over a 1 km tunnel: steep portal faces (1:1) up to 80 m over the line, the portals at 100 m and
    110 m."""
    x = np.asarray(xy, np.float64)[:, 0]
    return 100.0 + 0.01 * x + np.minimum(80.0, np.minimum(x, 1000.0 - x))


class Profile(unittest.TestCase):
    def test_mountain_tunnel_is_straight_between_the_portals(self):
        ln = line(0, 1000)
        z = roads.tunnel_profile([ln], hill)[0]
        self.assertAlmostEqual(z[0], 100.0, places=3)
        self.assertAlmostEqual(z[-1], 110.0, places=3)
        mid = len(z) // 2
        self.assertLess(abs(z[mid] - 105.0), 0.5)               # the straight line, not the ridge
        self.assertTrue((np.diff(z) > -1e-6).all())             # rising steadily

    def test_split_tunnel_is_one_chain(self):
        a, b = line(0, 500), line(500, 1000)
        za, zb = roads.tunnel_profile([a, b], hill)
        self.assertAlmostEqual(za[-1], zb[0], places=6)        # the joint is not a portal at the ridge's height
        self.assertLess(abs(za[-1] - 105.0), 0.5)

    def test_under_a_hollow_it_dips_no_steeper_than_the_grade(self):
        def bay(xy):            # flat land at 5 m, a 1.5 km bay at -10 m in the middle
            x = np.asarray(xy, np.float64)[:, 0]
            return np.where((x > 300) & (x < 1700), -10.0, 5.0)
        ln = line(0, 2000)
        z = roads.tunnel_profile([ln], bay)[0]
        self.assertAlmostEqual(z[0], 5.0, places=3)
        self.assertLess(z[len(z) // 2], -10.0 - roads.TUNNEL_COVER + 1.0)     # under the bed with cover
        s = np.r_[0, np.cumsum(np.hypot(*np.diff(ln, axis=0).T))]
        self.assertTrue((z >= 5.0 - roads.TUNNEL_GRADE * np.minimum(s, s[-1] - s) - 0.5).all())


class Holes(unittest.TestCase):
    def test_portal_opening_only(self):
        n, sq = 256, 2.0
        cx = cy = 0.0
        x0 = -n * sq / 2
        xs = x0 + np.arange(n) * sq
        X, Y = np.meshgrid(xs, xs)
        u = np.where(X < 0, 10.0, 10.0 + (X / 2.0))             # flat, then a hillside rising 1 m per 2 m
        part = ("t", Spec(7.0, "paved", False, "primary", False), line(0, 200), np.full(51, 10.0), 0.0)
        m = roads.tunnel_hole_mask([part], u, cx, cy, sq)
        hit = np.argwhere(m)
        self.assertTrue(len(hit) > 0)
        hx = xs[hit[:, 1]]
        hy = xs[hit[:, 0]]
        self.assertTrue((hx >= -2.0).all() and (hx <= 2 * (roadmesh.TUNNEL_H + 1.0)).all())   # the mouth only
        self.assertTrue((np.abs(hy) <= 7.0 / 2 + roadmesh.TUNNEL_SHOULDER + 1.0).all())        # the tube's width
        self.assertFalse(m[:, xs > 40].any())                    # rock over the vault stays


class Mesh(unittest.TestCase):
    def test_tube_and_portal(self):
        pts = np.c_[line(0, 40), np.full(11, 5.0)]
        ch = roads._chunk({**roads.TUNNEL, "deck": True, "tunnel": True}, 7.0, pts)
        ch.update(fixed=True, deck=True, tunnel=True, portal=[True, False])
        P, T, UV, mats, M = roadmesh.tunnel([ch], (0.0, 0.0, 0.0))
        self.assertEqual(mats, [roadmesh.TUNNEL_SIDE, roadmesh.TUNNEL_LIGHT])
        lamp = T[M == 1]
        self.assertGreater(len(lamp), 0)                          # the lamps, under the vault, faced down
        self.assertTrue((P[lamp][:, :, 2] < 5.0 + roadmesh.TUNNEL_H).all())
        ln = np.cross(P[lamp[:, 1]] - P[lamp[:, 0]], P[lamp[:, 2]] - P[lamp[:, 0]])
        self.assertTrue((ln[:, 2] < 0).all())
        self.assertAlmostEqual(P[:, 2].max(), 5.0 + roadmesh.TUNNEL_H + roadmesh.PORTAL_FRAME, places=3)
        tube = P[P[:, 0] > 1.0]                                   # past the portal face
        self.assertAlmostEqual(tube[:, 2].max(), 5.0 + roadmesh.TUNNEL_H, places=3)
        self.assertAlmostEqual(np.abs(tube[:, 1]).max(), 3.5 + roadmesh.TUNNEL_SHOULDER, places=3)
        # tube faces look inward: towards the axis
        tri = T[(P[T[:, 0], 0] > 1.0) & (P[T[:, 1], 0] > 1.0) & (P[T[:, 2], 0] > 1.0)]
        nrm = np.cross(P[tri[:, 1]] - P[tri[:, 0]], P[tri[:, 2]] - P[tri[:, 0]])
        cen = P[tri].mean(axis=1)
        to_axis = np.c_[np.zeros(len(cen)), -cen[:, 1], 5.0 + roadmesh.TUNNEL_H / 2 - cen[:, 2]]
        self.assertTrue(((nrm * to_axis).sum(1) > -1e-9).all())

    def test_no_tunnel_no_mesh(self):
        ch = roads._chunk(roads.DECK, 7.0, np.c_[line(0, 40), np.full(11, 5.0)])
        self.assertIsNone(roadmesh.tunnel([ch], (0.0, 0.0, 0.0)))


if __name__ == "__main__":
    unittest.main()


class OpenUnderpass(unittest.TestCase):
    def chunks(self, elev):
        from test_roads import features, FRAME, M_LAT
        f = features([[(-100, 0), (100, 0)]], ["residential"], flags=[(0, "is_tunnel")])
        tp = roads.TunnelProfiles(FRAME, lambda key: f, elev)
        return roads._tunnel_chunks(tp, [f], (-128.0, -128.0, 128.0, 128.0))

    def test_flat_ground_is_a_road_in_the_open(self):
        # a 'tunnel' under a park bridge in a trench: the ground over it never rises TUNNEL_OPEN_COVER over the road
        ch = self.chunks(lambda la, lo: np.full(np.shape(la), 10.0))
        self.assertTrue(ch)
        self.assertFalse(any(c.get("tunnel") for c in ch), "no tube or portals")
        self.assertTrue(any(c["m"] == roads.LOOK["paved"]["m"] for c in ch), "the road on the ground")

    def test_under_a_hill_keeps_its_tube(self):
        from test_roads import LON, M_LON

        def hill(la, lo):
            e = (np.asarray(lo) - LON) * M_LON
            return 10.0 + np.maximum(0.0, 40.0 - np.abs(e) * 0.4)
        ch = self.chunks(hill)
        self.assertTrue(any(c.get("tunnel") for c in ch))


class LowArch(unittest.TestCase):
    def test_a_park_bridge_is_a_low_stone_arch(self):
        # 5 m of ground over a short 'tunnel' (Central Park's West Drive over the 86th Street Transverse): a tube that
        # fits under it (ground - TUNNEL_SOIL_M), of stone, straight between its portals; no lamps
        from test_roads import features, FRAME, LON, M_LON
        f = features([[(-20, 0), (20, 0)]], ["residential"], flags=[(0, "is_tunnel")])

        def arch(la, lo):
            e = (np.asarray(lo) - LON) * M_LON
            return 10.0 + np.where(np.abs(e) < 15.0, 5.0, 0.0)
        tp = roads.TunnelProfiles(FRAME, lambda key: f, arch)
        ch = roads._tunnel_chunks(tp, [f], (-128.0, -128.0, 128.0, 128.0))
        tube = [c for c in ch if c.get("tunnel")]
        self.assertTrue(tube)
        h = tube[0]["tunnel_h"]
        self.assertAlmostEqual(h, 5.0 - roads.TUNNEL_SOIL_M, delta=0.3)
        zs = np.concatenate([np.asarray(c["n"])[:, 2] for c in tube])
        self.assertLess(np.ptp(zs), 0.1, "straight, no dip")
        for c in tube:
            c["portal"] = [True, True]
        P, T, UV, mats, M = roadmesh.tunnel(tube, (0.0, 0.0, 0.0))
        self.assertIn(roadmesh.TUNNEL_STONE, mats)
        self.assertNotIn(1, set(M.tolist()), "no lamps under an arch")
        self.assertLess(P[:, 2].max(), zs.max() + h + roadmesh.LOW_PORTAL_UP + 0.01)


class ArchRaise(unittest.TestCase):
    def test_ground_over_a_low_arch_stays_over_its_vault(self):
        xy = np.array([[0.0, 0.0], [30.0, 0.0]])
        part = ("a", Spec(7.0, "paved", False, "residential"), xy, np.array([10.0, 10.0]), 0.0)
        px = np.array([1.0, 15.0, 15.0, 15.0, 40.0])
        py = np.array([0.0, 0.0, 4.0, 20.0, 0.0])
        z = np.array([10.0, 12.0, 12.0, 12.0, 12.0])
        out = roads.arch_raise([part], {"a": 4.0}, px, py, z)
        self.assertAlmostEqual(out[0], 9.98, msg="the mouth: flat at the floor (cut away)")
        self.assertAlmostEqual(out[1], 10.0 + 4.0 + roads.ARCH_SOIL_MIN)
        self.assertAlmostEqual(out[2], 10.0 + 4.0 + roads.ARCH_SOIL_MIN, msg="beside the vault too")
        self.assertEqual(out[3], 12.0, "away from it as it was")
        self.assertEqual(out[4], 12.0, "past its portal as it was")
        self.assertTrue(np.array_equal(roads.arch_raise([part], {}, px, py, z), z), "a full tunnel: nothing")
