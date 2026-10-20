"""blockworker.roads_tile: a road tile built in a road process is the tile the server's own thread
builds (roads.build_tile + roadmesh + write_tile with the same inputs), files byte for byte."""
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import blockworker  # noqa: E402
import roadmesh  # noqa: E402
import roads  # noqa: E402
from test_roads import FRAME, LAT, LON, features  # noqa: E402


class FakeMapData:
    """The roads layer of the tile's first cell only (every cell the same object would double them)."""

    def __init__(self, feats, first):
        self.feats, self.first = feats, first

    def cell(self, key, layers=(), kind="demand"):
        return SimpleNamespace(layers={"roads": self.feats if key == self.first else None})


class FakeDem:
    def sample(self, lat, lon, zoom, progress=None):
        return np.full(np.shape(lat), 10.0)


class RoadsTileTest(unittest.TestCase):
    def test_same_as_in_process(self):
        f = features([[(-300, 100), (300, 100)], [(120, -100), (120, 300)]], ["secondary", "residential"])
        keys = roads.cells_of_tile(FRAME, 0, 0)
        md = FakeMapData(f, keys[0])
        blockworker._s = SimpleNamespace(md=md, dem=FakeDem(), widths=None, surface=None, airports=None, sea=None, lock=threading.Lock(),
                                         bridges={}, anchor=lambda aid, lat, lon: (None, None))
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            got = blockworker.roads_tile(1, LAT, LON, 15, 0, 0, a, "/temp/x", True, 0.05)
            cells = [md.cell(k).layers.get("roads") for k in keys]
            elev = lambda la, lo: FakeDem().sample(la, lo, 15)      # noqa: E731
            walks = []
            bridges = roads.BridgeProfiles(FRAME, lambda k: md.cell(k).layers.get("roads"), elev)
            chunks = roads.build_tile(FRAME, 0, 0, cells, elev, None, walks=walks, bridges=bridges)
            x0, y0, _, _ = roads.tile_bbox(0, 0)
            want = roads.write_tile(b, "/temp/x", 1, 0, 0, chunks, walks,
                                    roadmesh.build(FRAME, chunks, elev, (x0, y0, 0.0), 0.05, clip=roads.tile_bbox(0, 0),
                                                   follow=True),
                                    roadmesh.deck(chunks, (x0, y0, 0.0)))
            self.assertEqual(got, want)
            self.assertGreater(got[1], 0)
            self.assertEqual(sorted(os.listdir(a)), sorted(os.listdir(b)))
            for name in os.listdir(a):
                self.assertEqual(Path(a, name).read_bytes(), Path(b, name).read_bytes(), name)


if __name__ == "__main__":
    unittest.main()
