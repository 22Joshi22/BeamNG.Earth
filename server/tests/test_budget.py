"""budget.py: the small-machine profile (a Steam Deck: 8.2 GB of server caches next to the game, the game killed by
the system's out-of-memory guard, 2026-10-20)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import budget  # noqa: E402
import dem  # noqa: E402
import imagery  # noqa: E402
import mapdata  # noqa: E402


class BudgetTest(unittest.TestCase):
    def setUp(self):
        self.saved = (dem.MEM_TILES, imagery.MEM_TILES, mapdata.MEM_MB)

    def tearDown(self):
        dem.MEM_TILES, imagery.MEM_TILES, mapdata.MEM_MB = self.saved

    def test_profile_by_ram(self):
        with mock.patch.dict(os.environ, {"BNGE_BUDGET": ""}):
            self.assertTrue(budget.small(14.5), "a Steam Deck")
            self.assertFalse(budget.small(64.0), "a desktop")
            self.assertFalse(budget.small(None) and budget.total_gb() is None, "unknown RAM: desktop")
        with mock.patch.dict(os.environ, {"BNGE_BUDGET": "small"}):
            self.assertTrue(budget.small(64.0))
        with mock.patch.dict(os.environ, {"BNGE_BUDGET": "desktop"}):
            self.assertFalse(budget.small(8.0))

    def test_small_caches(self):
        self.assertFalse(budget.apply(False))
        self.assertEqual(dem.MEM_TILES, self.saved[0], "the desktop sizes stay")
        self.assertTrue(budget.apply(True))
        self.assertEqual((dem.MEM_TILES, imagery.MEM_TILES, mapdata.MEM_MB),
                         (budget.SMALL_DEM_TILES, budget.SMALL_IMAGERY_TILES, budget.SMALL_MAPDATA_MB))
        d = dem.Dem(os.path.join(os.path.dirname(__file__), "_no_cache"), fetcher=mock.Mock())
        self.assertEqual(d.mem_tiles, budget.SMALL_DEM_TILES, "read when the cache is made")


if __name__ == "__main__":
    unittest.main()
