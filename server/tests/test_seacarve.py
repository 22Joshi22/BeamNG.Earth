"""seacarve: the sea floor carved under ocean polygons, deterministic and continuous across the shoreline
and across cells; land lifted above the anchor's sea plane; near()."""
import math
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import geodesy  # noqa: E402
import mapcells  # noqa: E402
import seacarve  # noqa: E402

LAT0, LON0 = 39.28, -76.59
SEA_LAT = 39.27         # the "sea": every point south of this latitude in the test's area


class Water:
    """mapgeom.Features-like: one ocean polygon (the area south of SEA_LAT) and one lake beside it."""

    def __init__(self, polys):
        self.polys = polys
        self.attrs = {"subtype": np.array([s for s, _ in polys])}
        self.kind = np.full(len(polys), 3, np.uint8)

    def parts(self, i):
        return [[np.asarray(self.polys[i][1], np.float64)]]


class Cell:
    def __init__(self, water):
        self.layers = {"water": water}


class FakeMapData:
    release = "test"

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def cell(self, key, layers=None, kind="demand"):
        self.calls += 1
        if self.fail:
            raise OSError("offline")
        ocean = [(-77.5, 38.5), (-75.5, 38.5), (-75.5, SEA_LAT), (-77.5, SEA_LAT), (-77.5, 38.5)]
        lake = [(-76.62, 39.30), (-76.61, 39.30), (-76.61, 39.31), (-76.62, 39.31), (-76.62, 39.30)]
        return Cell(Water([("ocean", ocean), ("lake", lake)]))


def south(m):
    """The point m metres south of (SEA_LAT, LON0) (negative: north, on land)."""
    return SEA_LAT - m / 111320.0


class SeaCarveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sc = seacarve.SeaCarver(FakeMapData(), self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_depth_from_the_shore(self):
        d = [float(self.sc.depth(np.array([south(m)]), np.array([LON0]))[0]) for m in (-20, -2, 2, 10, 20, 100)]
        self.assertEqual(d[0], 0.0, "land")
        self.assertLess(d[1], 0.3, "within a pixel of the shore: hardly any depth")
        self.assertTrue(0 < d[2] < d[3] < d[4], f"deeper away from the shore: {d}")
        self.assertAlmostEqual(d[3], seacarve.DEPTH_SLOPE * 10, delta=0.6)
        self.assertEqual(d[5], seacarve.DEPTH_CAP)

    def test_lakes_are_not_sea(self):
        self.assertEqual(float(self.sc.depth(np.array([39.305]), np.array([-76.615]))[0]), 0.0)

    def test_continuous_across_cells(self):
        """Along a line through a cell edge the depth changes no more than its slope allows."""
        x, _ = seacarve._merc(np.array([SEA_LAT]), np.array([LON0]))
        edge_px = (math.floor(x[0] / seacarve.CELL_PX) + 1) * seacarve.CELL_PX
        edge_lon = edge_px / (256 << seacarve.PX_ZOOM) * 360.0 - 180.0
        lons = edge_lon + np.linspace(-0.0003, 0.0003, 61)
        d = self.sc.depth(np.full(61, south(12)), lons)
        self.assertLess(float(np.abs(np.diff(d)).max()), 0.05, d)

    def test_carve(self):
        lat = np.array([south(-50), south(50)])
        lon = np.full(2, LON0)
        z = np.array([3.21, 0.4])
        out = self.sc.carve(lat, lon, z)
        self.assertEqual(out[0], 3.21, "land: the DEM bit for bit")
        self.assertAlmostEqual(out[1], -seacarve.DEPTH_CAP, places=5)

    def test_cached_on_disk_and_deterministic(self):
        lat = np.array([south(7.3), south(31)])
        lon = np.array([LON0, LON0 + 0.001])
        a = self.sc.depth(lat, lon)
        md = FakeMapData()
        b = seacarve.SeaCarver(md, self.tmp.name).depth(lat, lon)
        self.assertEqual(md.calls, 0, "read back from disk")
        np.testing.assert_array_equal(a, b)
        with tempfile.TemporaryDirectory() as other:
            np.testing.assert_array_equal(a, seacarve.SeaCarver(FakeMapData(), other).depth(lat, lon))

    def test_no_map_data_no_sea(self):
        sc = seacarve.SeaCarver(FakeMapData(fail=True), None)
        self.assertFalse(sc.depth(np.array([south(100)]), np.array([LON0])).any())

    def test_near(self):
        self.assertTrue(self.sc.near(south(-1000), LON0, 2000))
        self.assertFalse(self.sc.near(south(-5000), LON0, 2000))


class LiftTest(unittest.TestCase):
    def test_land_kept_above_the_plane(self):
        f = geodesy.EnuFrame(LAT0, LON0)
        lat = LAT0 + np.array([0.0, 0.0, 0.0, 0.0, 0.0])
        lon = LON0 + np.array([0.0, 0.1, 0.1, 0.1, 0.6]) / math.cos(math.radians(LAT0))   # 0, ~11 km, ~67 km
        e = np.array([0.5, 0.5, 0.0, 40.0, 0.5])
        out = seacarve.lift(f, lat, lon, e)
        z = f.geodetic_to_enu(lat, lon, out)[2]
        self.assertEqual(out[0], 0.5, "at the anchor: unchanged")
        self.assertAlmostEqual(z[1], seacarve.LIFT_Z, places=3)
        self.assertEqual(out[2], 0.0, "the sea itself stays under the plane")
        self.assertEqual(out[3], 40.0, "land high enough: unchanged")
        self.assertEqual(out[4], 0.5, "beyond LIFT_RADIUS_M: unchanged")


if __name__ == "__main__":
    unittest.main()
