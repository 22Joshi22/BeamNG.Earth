import sys
import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dem as demmod  # noqa: E402


class FlakyDem(demmod.Dem):
    """No network: the first load of every tile fails, later loads succeed."""

    def __init__(self, cache_dir):
        super().__init__(cache_dir, fetch_workers=4)
        self.calls = {}
        self.calls_lock = threading.Lock()

    def _load_tile(self, key):
        with self.calls_lock:
            n = self.calls[key] = self.calls.get(key, 0) + 1
        if n == 1:
            raise IOError(f"simulated fetch failure {key}")
        return np.full((demmod.TILE, demmod.TILE), key[1], np.float32)


class DemCacheTest(unittest.TestCase):
    def test_failed_fetch_is_retried(self):
        with tempfile.TemporaryDirectory() as d:
            dem = FlakyDem(d)
            keys = [(5, x, 3) for x in range(6)]
            with self.assertRaises(IOError):
                dem.tiles(keys)
            dem.pool.shutdown(wait=True)        # let the other failing loads finish
            dem.pool = demmod.ThreadPoolExecutor(4)
            # A failure must not be cached: the retry loads every tile again.
            got = dem.tiles(keys)
            self.assertEqual(sorted(got), keys)
            self.assertEqual(dem._inflight, {})
            self.assertTrue(all(dem.calls[k] == 2 for k in keys), dem.calls)
            self.assertTrue(all(float(got[k][0, 0]) == k[1] for k in keys))
            # Third call is served from memory.
            dem.tiles(keys)
            self.assertTrue(all(dem.calls[k] == 2 for k in keys))


if __name__ == "__main__":
    unittest.main()
