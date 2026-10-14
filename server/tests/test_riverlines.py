"""riverlines: river / stream centrelines chained in flow order, resampled, their surface from the channel's
DEM never rising downstream, cut into pieces on a lattice with stable keys, left out where tiles are water."""
import math
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import geodesy  # noqa: E402
import riverlines  # noqa: E402

LAT, LON = 40.0, -77.0
M_LON = 1.0 / (111320.0 * math.cos(math.radians(LAT)))


class Lines:
    """mapgeom.Features-like: water lines (kind 2)."""

    def __init__(self, rows):          # (id, subtype, class, [(lon, lat), ...])
        self.ids = np.array([r[0] for r in rows])
        self.kind = np.full(len(rows), 2, np.uint8)
        self.attrs = {"subtype": np.array([r[1] for r in rows]), "class": np.array([r[2] for r in rows]),
                      "is_intermittent": np.zeros(len(rows), bool)}
        self.rows = rows

    def parts(self, i):
        return [[np.asarray(self.rows[i][3], np.float64)]]


def east(m):
    return (LON + m * M_LON, LAT)


class ChainTest(unittest.TestCase):
    def test_continuing_ways_chained_in_order(self):
        a = ("a", "stream", np.array([east(0), east(100)]))
        b = ("b", "stream", np.array([east(100), east(250)]))
        c = ("c", "stream", np.array([east(250), east(400)]))
        out = riverlines.chains([c, a, b])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][0], ("a", "b", "c"))
        self.assertEqual(len(out[0][2]), 4)

    def test_confluence_not_chained_through(self):
        a = ("a", "stream", np.array([east(0), east(100)]))
        b = ("b", "stream", np.array([(LON, LAT + 0.001), east(100)]))
        c = ("c", "stream", np.array([east(100), east(200)]))
        self.assertEqual(sorted(ids for ids, _, _ in riverlines.chains([a, b, c])), [("a",), ("b",), ("c",)])


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.f = geodesy.EnuFrame(LAT, LON)
        self.lines = Lines([("s1", "stream", "stream", [east(-1500), east(-200)]),
                            ("s2", "stream", "stream", [east(-200), east(1500)]),
                            ("d1", "canal", "ditch", [(LON, LAT + 0.002), east(300)])])

    def plan(self, wet=lambda la, lo: np.zeros(np.shape(la))):
        # the channel falls 1 m per 100 m eastwards, with a 2 m bump the surface must not follow up
        def dem(la, lo):
            x = (np.asarray(lo) - LON) / M_LON
            return 50.0 - x / 100.0 + np.where(np.abs(x - 500) < 15, 2.0, 0.0)
        return riverlines.plan(self.f, 0.0, 0.0, lambda k: self.lines, wet, dem)

    def test_pieces_downstream_never_rising(self):
        rv = self.plan()
        self.assertTrue(rv)
        self.assertTrue(all(r.kind == "stream" and r.flow == riverlines.FLOW_MS["stream"] for r in rv), "no ditch")
        nodes = sorted((n for r in rv for n in r.nodes), key=lambda n: n[0])
        z = np.array([n[2] for n in nodes])
        self.assertTrue(np.all(np.diff(z) <= 1e-6), "never rises downstream (the bump is not followed)")
        for r in rv:
            xs = [n[0] for n in r.nodes]
            self.assertTrue(all(b > a for a, b in zip(xs, xs[1:])), "flow order")
            self.assertEqual(r.nodes[0][3], riverlines.WIDTH_M["stream"])
        self.assertEqual(sorted(r.key for r in rv), sorted(r.key for r in self.plan()), "stable keys")

    def test_left_out_where_tiles_are_water(self):
        wet = lambda la, lo: ((np.asarray(lo) - LON) / M_LON > 0).astype(float)    # noqa: E731
        rv = self.plan(wet)
        self.assertTrue(rv)
        for r in rv:
            self.assertTrue(all(n[0] <= riverlines.SPACING_M["stream"] + 1e-6 for n in r.nodes))


if __name__ == "__main__":
    unittest.main()
