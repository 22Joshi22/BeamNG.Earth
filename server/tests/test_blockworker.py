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



class FarTileTest(unittest.TestCase):
    """blockworker.far_tile: the texture from the imagery on disk is the one the server renders, byte for byte; with
    a tile not on disk, none (the server renders it)."""

    def test_texture_from_disk(self):
        import io
        import zlib
        import farfield
        import imagery
        import rockface
        from PIL import Image

        class Fetcher:                  # every source tile a seeded noise JPEG
            def register_chain(self, *a):
                pass

            def miss_alive(self, miss, url):
                return False

            def get(self, url, kind="demand"):
                rng = np.random.default_rng(zlib.crc32(url.encode()))
                buf = io.BytesIO()
                Image.fromarray(rng.integers(20, 220, (256, 256, 3), dtype=np.uint8)).save(buf, "JPEG")
                return SimpleNamespace(body=buf.getvalue(), elapsed=0.01)
        layout = farfield.Layout(cells=64)
        tile = farfield.Tile(3, 1, -2)
        px = 128
        with tempfile.TemporaryDirectory() as cache, tempfile.TemporaryDirectory() as out:
            blockworker._s = SimpleNamespace(dem=FakeDem(), airports=None, sea=None, cache_dir=cache, imagery=None,
                                             anchor=lambda aid, lat, lon: (None, None))
            tex = {"path": os.path.join(out, "worker.dds"), "px": px, "chain": ["usgs"], "tone": 1.0,
                   "leaf_on": True, "season_mode": "off"}
            built, got = blockworker.far_tile(1, LAT, LON, tile, 12, None, 11, True, layout, tex)
            self.assertIsNone(got, "nothing on disk: the server renders it")
            im = imagery.Imagery(os.path.join(cache, "imagery"), ("usgs",), fetcher=Fetcher())
            x0, y0, _, _ = tile.bbox
            sq = imagery.gutter_square(x0, y0, tile.size, px)
            rgb, used = im.render(FRAME, *sq, px)
            g = np.asarray(built.grid)
            rgb = rockface.rock(rgb, FRAME, *sq, g, x0, y0, tile.size / (g.shape[1] - 1))
            n = imagery.write_dds(os.path.join(out, "server.dds"), rgb)
            built2, got = blockworker.far_tile(1, LAT, LON, tile, 12, None, 11, True, layout, tex)
            self.assertIsNotNone(got, "the tiles on disk now")
            self.assertEqual(got[0], n)
            self.assertEqual(set(got[1]), set(used))
            self.assertEqual(Path(out, "worker.dds").read_bytes(), Path(out, "server.dds").read_bytes())

if __name__ == "__main__":
    unittest.main()
