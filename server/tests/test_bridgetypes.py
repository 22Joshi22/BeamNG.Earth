"""bridgetypes: a bridge's structure from a tagged outline, the NBI or the size rule; the truss beside its decks."""
import json
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridgetypes as bt  # noqa: E402
import geodesy  # noqa: E402
import roadmesh  # noqa: E402

LAT, LON = 45.0, 10.0
FRAME = geodesy.EnuFrame(LAT, LON)


def deck(x0=-150.0, x1=150.0, z=30.0, y=0.0, w=10.0):
    xs = np.arange(x0, x1 + 1e-6, 4.0)
    return (np.c_[xs, np.full(len(xs), y)], np.full(len(xs), z), w)


def db_with(*rows):
    """An inventory of rows (east m, north m, type, span) in FRAME."""
    e = np.array([r[0] for r in rows], np.float64)
    n = np.array([r[1] for r in rows], np.float64)
    la, lo, _ = FRAME.enu_to_geodetic(e, n, np.zeros(len(e)))
    o = np.argsort(la)
    return {"lat": np.asarray(la, np.float32)[o], "lon": np.asarray(lo, np.float32)[o],
            "type": np.array([r[2] for r in rows], np.uint8)[o], "span": np.array([r[3] for r in rows], np.float32)[o],
            "length": np.full(len(rows), 300.0, np.float32), "width": np.full(len(rows), 20.0, np.float32),
            "nav": np.array([r[4] if len(r) > 4 else 0.0 for r in rows], np.float32)[o]}


class Outline:
    """Infrastructure Features: one bridge outline (lon/lat ring) with tags."""
    def __init__(self, box, tags):
        x0, y0, x1, y1 = box
        e = np.array([x0, x1, x1, x0, x0], np.float64)
        n = np.array([y0, y0, y1, y1, y0], np.float64)
        la, lo, _ = FRAME.enu_to_geodetic(e, n, np.zeros(5))
        self.ring = np.c_[lo, la]
        self.kind = np.array([3], np.uint8)
        self.attrs = {"tags": np.array([json.dumps(tags)], object), "subtype": np.array(["bridge"], object)}

    def __len__(self):
        return 1

    def parts(self, i):
        return [[self.ring]]


class StructureTest(unittest.TestCase):
    def tearDown(self):
        bt._db = None

    def test_nbi_record_on_the_bridge(self):
        bt._db = db_with((20.0, 5.0, bt.NBI_THROUGH_TRUSS, 180.0))
        info = bt.chain_structure(FRAME, [deck()])
        self.assertEqual((info["type"], info["src"]), (bt.NBI_THROUGH_TRUSS, "nbi"))

    def test_inventory_without_a_record_is_plain(self):
        bt._db = db_with((5000.0, 0.0, bt.NBI_THROUGH_TRUSS, 180.0))      # 5 km off: covered, not this bridge
        self.assertIsNone(bt.chain_structure(FRAME, [deck()], lambda xy: np.ones(len(xy), bool)))

    def test_size_rule_outside_the_inventory(self):
        bt._db = db_with((90000.0, 0.0, bt.NBI_THROUGH_TRUSS, 180.0))
        wet = lambda xy: np.abs(xy[:, 0]) < 100                          # 200 m of water
        self.assertEqual(bt.chain_structure(FRAME, [deck()], wet)["src"], "size")
        self.assertIsNone(bt.chain_structure(FRAME, [deck()], lambda xy: np.abs(xy[:, 0]) < 40))

    def test_osm_tag_first(self):
        bt._db = db_with((20.0, 5.0, bt.NBI_THROUGH_TRUSS, 180.0))
        beam = Outline((-160, -10, 160, 10), {"man_made": "bridge", "bridge:structure": "beam"})
        self.assertIsNone(bt.chain_structure(FRAME, [deck()], None, [beam]))
        bt._db = db_with((90000.0, 0.0, 1, 0.0))
        truss = Outline((-160, -10, 160, 10), {"man_made": "bridge", "bridge:structure": "truss"})
        self.assertEqual(bt.chain_structure(FRAME, [deck()], None, [truss])["type"], bt.NBI_THROUGH_TRUSS)


class NavTest(unittest.TestCase):
    def tearDown(self):
        bt._db = None

    def test_nav_clearance_of_the_bridge(self):
        bt._db = db_with((20.0, 5.0, 2, 60.0, 41.1), (3000.0, 0.0, 2, 60.0, 9.0))
        self.assertAlmostEqual(bt.nav_clearance(FRAME, [deck()[0]]), 41.1, places=3)
        self.assertEqual(bt.nav_clearance(FRAME, [deck(y=500.0)[0]]), 0.0)

    def test_girder_record_is_not_a_truss(self):
        bt._db = db_with((20.0, 5.0, 2, 60.0, 41.1))
        info = bt.chain_structure(FRAME, [deck()])
        self.assertEqual(bt.truss_members([deck()], info, lambda xy: np.zeros(len(xy))), [])


class TrussTest(unittest.TestCase):
    def test_through_truss_beside_the_decks(self):
        info = {"type": bt.NBI_THROUGH_TRUSS, "span": 180.0}
        parts = [deck(y=0.0, w=10.0), deck(y=12.0, w=8.0)]            # two carriageways side by side
        beams = bt.truss_members(parts, info, lambda xy: np.zeros(len(xy)))
        self.assertGreater(len(beams), 40)
        ends = np.array([p for b in beams for p in b[:2]])
        upright = [b for b in beams if abs(b[0][0] - b[1][0]) < 1e-6 and abs(b[0][1] - b[1][1]) < 1e-6]
        ys = np.array([b[0][1] for b in upright])
        self.assertTrue(((ys < -5.0 - roadmesh.PARAPET_W) | (ys > 16.0 + roadmesh.PARAPET_W)).all(),
                        "posts outside both decks, none between them")
        self.assertGreaterEqual(ends[:, 2].max(), 30.0 + bt.HEADROOM_M - 1e-6)

    def test_plain_or_low_deck_no_truss(self):
        self.assertEqual(bt.truss_members([deck()], None, lambda xy: np.zeros(len(xy))), [])
        info = {"type": bt.NBI_THROUGH_TRUSS, "span": 180.0}
        self.assertEqual(bt.truss_members([deck(z=3.0)], info, lambda xy: np.zeros(len(xy))), [])

    def test_big_truss_over_the_water_only(self):
        info = {"type": bt.NBI_THROUGH_TRUSS, "span": 100.0}
        long = deck(-600.0, 600.0)
        beams = bt.truss_members([long], info, lambda xy: np.zeros(len(xy)), lambda xy: np.abs(xy[:, 0]) < 100)
        xs = np.array([p[0] for b in beams for p in b[:2]])
        self.assertLessEqual(np.abs(xs).max(), 100.0 + 100.0 + bt.PANEL_M)

    def test_anchor_piers_off_the_roads(self):
        info = {"type": bt.NBI_THROUGH_TRUSS, "span": 100.0}
        long = deck(-600.0, 600.0)
        wet = lambda xy: np.abs(xy[:, 0]) < 100
        beams, piers = bt.truss_members([long], info, lambda xy: np.zeros(len(xy)), wet, with_piers=True)
        self.assertEqual(len(piers), 2)
        x_end = piers[0][0]
        road = [np.array([[x_end, -50.0], [x_end, 50.0]])]          # a street under that end
        _, moved = bt.truss_members([long], info, lambda xy: np.zeros(len(xy)), wet, with_piers=True,
                                    road_lines=road)
        self.assertTrue(all(abs(p[0] - x_end) > bt.ROAD_GAP_M for p in moved))

    def test_mesh(self):
        info = {"type": bt.NBI_THROUGH_TRUSS, "span": 180.0}
        beams = bt.truss_members([deck()], info, lambda xy: np.zeros(len(xy)))
        P, T, UV, mats, M = roadmesh.deck([], (0.0, 0.0, 0.0), beams=beams)
        self.assertIn(roadmesh.TRUSS_STEEL, mats)
        self.assertEqual(len(T), 8 * len(beams))
        self.assertEqual(len(P), 8 * len(beams))


if __name__ == "__main__":
    unittest.main()
