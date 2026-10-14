"""navgraph: the drivable roads as the stock map's graph: connectors shared at junctions, one-way direction,
lanes, speed limits, clipped to the window."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import navgraph  # noqa: E402
from test_roads import FRAME, features, flat, rules  # noqa: E402


def graph(lines, classes, connectors=(), oneway=(), speeds=(), subclass=(), centre=(0.0, 0.0), window=1100.0):
    f = features(lines, classes)
    f.rules["connectors"] = rules(list(connectors))
    f.rules["oneway"] = rules(list(oneway), "float")
    f.rules["max_speed_kmh"] = rules(list(speeds), "float")
    f.rules["subclass"] = rules(list(subclass))
    return navgraph.build(FRAME, [f], flat, centre, window=window)


class GraphTest(unittest.TestCase):
    def test_junction_shares_the_connector(self):
        g = graph([[(0, 0), (100, 0)], [(100, 0), (100, 100)]], ["residential", "residential"],
                  connectors=[(0, "A", 0.0, 0.0), (0, "J", 1.0, 1.0), (1, "J", 0.0, 0.0), (1, "B", 1.0, 1.0)])
        self.assertIn("cJ", g["nodes"])
        links = [e for e in g["edges"] if "cJ" in e[:2]]
        self.assertEqual(len(links), 2, "both roads end at the junction node")
        x, y, z, r = g["nodes"]["cJ"]
        self.assertAlmostEqual(x, 100.0, delta=0.5)
        self.assertAlmostEqual(y, 0.0, delta=0.5)
        self.assertEqual(z, 10.0)
        for e in g["edges"]:
            self.assertEqual((e[3], e[4]), (1, 1), "a two-way residential street: one lane each way")
            self.assertAlmostEqual(e[5], 25 * navgraph.MPH, places=2)

    def test_long_edges_split_and_oneway_against_the_geometry(self):
        g = graph([[(0, 0), (300, 0)]], ["secondary"], connectors=[(0, "A", 0.0, 0.0), (0, "B", 1.0, 1.0)],
                  oneway=[(0, -1.0)], speeds=[(0, 72.0)])
        self.assertGreaterEqual(len(g["edges"]), 4, "300 m in edges of at most MAX_EDGE_M")
        for e in g["edges"]:
            self.assertEqual(e[2], 1)
            self.assertLess(g["nodes"][e[1]][0], g["nodes"][e[0]][0], "one-way -1: drives against the geometry")
            self.assertEqual((e[3], e[4]), (e[3], 0))
            self.assertAlmostEqual(e[5], 20.0, places=2)
        self.assertTrue(any(e[1] == "cA" for e in g["edges"]))

    def test_not_drivable_and_window(self):
        g = graph([[(0, 0), (50, 0)], [(0, 10), (50, 10)], [(0, 20), (50, 20)], [(5000, 0), (5100, 0)]],
                  ["footway", "service", "residential", "residential"], subclass=[(1, "parking_aisle")])
        ys = {round(n[1]) for n in g["nodes"].values()}
        self.assertEqual(ys, {20}, "only the residential street in the window")


if __name__ == "__main__":
    unittest.main()
