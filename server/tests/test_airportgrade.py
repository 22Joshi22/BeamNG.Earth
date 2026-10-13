"""airportgrade: runways graded into a DEM sampler. Away from airports the DEM bit for bit; on a runway the
graded field (runwaygeom), the same whichever box asks first (plans per group, cached); the wrapped DEM
(far tiles) gives the same heights as apply."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import airportfixtures as fx  # noqa: E402
import airportgrade  # noqa: E402
import runwaygeom  # noqa: E402


class PlaneDem:
    """dem.Dem-like: a bumpy plane (zoom ignored); counts calls."""

    def __init__(self):
        self.fn = fx.plane(100.0, 0.01, -0.005, bumps=3.0)
        self.calls = 0

    def sample(self, lat, lon, zoom, progress=None):
        self.calls += 1
        return self.fn(lat, lon)


class AirportGradeTest(unittest.TestCase):
    def setUp(self):
        self.db = fx.db()
        self.dem = PlaneDem()
        self.ag = airportgrade.AirportGrader(self.db, self.dem)

    def grid(self, lat0, lon0, half=0.01, n=81):
        la, lo = np.meshgrid(np.linspace(lat0 - half, lat0 + half, n), np.linspace(lon0 - half, lon0 + half, n), indexing="ij")
        return la, lo

    def test_far_from_airports_untouched(self):
        la, lo = self.grid(46.0, 8.0)
        z = self.dem.fn(la, lo)
        out = self.ag.apply(la, lo, z)
        self.assertIs(out, z)
        self.assertEqual(self.ag.stats["plans"], 0)

    def test_runway_graded_and_cached(self):
        la, lo = self.grid(45.0, 7.0)
        z = self.dem.fn(la, lo)
        out = self.ag.apply(la, lo, z)
        self.assertGreater(np.abs(out - z).max(), 0.1, "the bumps are graded away on the runways")
        plans = self.ag.stats["plans"]
        self.assertGreater(plans, 0)
        # the same as runwaygeom with the group's plans
        groups = self.ag._groups(la, lo)
        want = runwaygeom.apply([p for g in groups for p in self.ag.plans_for(g)], la, lo, z)[0]
        np.testing.assert_array_equal(out, want)
        # a smaller box over the same runway: same heights there, no new plans
        sub = self.ag.apply(la[30:50, 30:50], lo[30:50, 30:50], z[30:50, 30:50])
        np.testing.assert_array_equal(sub, out[30:50, 30:50])
        self.assertEqual(self.ag.stats["plans"], plans)

    def test_wrapped_dem(self):
        la, lo = self.grid(45.0, 7.0, half=0.004, n=41)
        src = self.ag.wrap(self.dem)
        np.testing.assert_array_equal(src.sample(la, lo, 15), self.ag.apply(la, lo, self.dem.fn(la, lo)))


if __name__ == "__main__":
    unittest.main()
