"""Ground surfaces under the physics block (surface.py; earthd --surface on): the land-cover class ->
material table, the steep-slope rule, the masks (one per material, every sample in exactly one,
rows as the heightmap's), the cached land cover that never waits for the network, and the server
writing the masks next to the heightmap and naming them in the terrain message."""
import os
import sys
import tempfile
import unittest

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import earthd  # noqa: E402
import heightmap  # noqa: E402
import mapcells  # noqa: E402
import maprasters  # noqa: E402
import surface as sf  # noqa: E402

LAT, LON = 36.5785, -118.2923


class ClassifyTest(unittest.TestCase):
    def test_classes_map_to_materials(self):
        classes = np.array([[0, 10, 20, 30], [40, 50, 60, 70], [80, 90, 95, 100]], np.uint8)
        idx = sf.classify(classes, np.zeros(classes.shape), 2.0)
        self.assertEqual(idx.tolist(), [[sf.DIRT, sf.GRASS, sf.DUSTY, sf.GRASS], [sf.DIRT, sf.ASPHALT, sf.LOOSE, sf.SNOW],
                                        [sf.MUD, sf.MUD, sf.MUD, sf.GRASS]])
        self.assertEqual(sf.MATERIALS[sf.DIRT], ("beamngEarthDirt", "DIRT"))
        self.assertEqual(sf.DIRT, len(sf.MATERIALS) - 1, "the default material goes last (ties pick the later layer)")

    def test_unknown_class_is_dirt(self):
        self.assertEqual(sf.classify(np.array([[7, 255]], np.uint8), np.zeros((1, 2)), 2.0).tolist(), [[sf.DIRT, sf.DIRT]])

    def test_steep_ground_is_rock_but_snow_stays(self):
        n = 8
        u = np.tile(np.arange(n) * 2.0 * 1.2, (n, 1))          # slope 1.2 (50 deg) along x
        grass = np.full((n, n), 30, np.uint8)
        self.assertTrue((sf.classify(grass, u, 2.0) == sf.ROCK).all())
        self.assertTrue((sf.classify(np.full((n, n), 70, np.uint8), u, 2.0) == sf.SNOW).all())
        gentle = np.tile(np.arange(n) * 2.0 * 0.5, (n, 1))     # 27 deg: not rock
        self.assertTrue((sf.classify(grass, gentle, 2.0) == sf.GRASS).all())


class MaskTest(unittest.TestCase):
    def test_one_mask_per_material_every_sample_in_one(self):
        rng = np.random.default_rng(1)
        idx = rng.integers(0, len(sf.MATERIALS), (16, 16)).astype(np.uint8)
        with tempfile.TemporaryDirectory() as tmp:
            names = sf.write_masks(idx, tmp, "hm_1_0_0")
            self.assertEqual(names, sf.mask_names("hm_1_0_0"))
            masks = [np.array(Image.open(os.path.join(tmp, n))) for n in names]
            self.assertTrue(all(m.dtype == np.uint8 and set(np.unique(m)) <= {0, 255} for m in masks))
            self.assertTrue((sum((m == 255).astype(int) for m in masks) == 1).all())
            for i, m in enumerate(masks):
                self.assertTrue(((m == 255) == (idx == i)).all())
            self.assertFalse([f for f in os.listdir(tmp) if f.endswith(".tmp")])


class FakeMapData:
    """The parts of mapdata.MapData the Surface uses: path / _load / prefetch, in memory."""

    def __init__(self, fill):
        self.fill = fill            # key -> class value (or None: no coverage)
        self.cached = {}
        self.prefetched = []

    def path(self, key, layer):
        return (str(key), layer)

    def _load(self, path):
        return self.cached.get(path)

    def prefetch(self, key, layers):
        self.prefetched.append((key, layers))
        self.store(key)
        return True

    def store(self, key):
        v = self.fill(key)
        path = self.path(key, "landcover")
        if v is None:
            self.cached[path] = {"missing": np.array(1), "provenance": np.array("null")}
            return
        lon0, lat0, lon1, lat1 = key.bounds()
        data = np.full((4, 4), v, np.uint8)
        meta = {"crs": "EPSG:4326", "x0": lon0, "y0": lat1, "dx": (lon1 - lon0) / 4, "dy": (lat1 - lat0) / 4,
                "nodata": 0, "files": ["fake"]}
        import json
        self.cached[path] = {"data": data, "meta": np.array(json.dumps(meta)), "provenance": np.array("null")}


class SurfaceTest(unittest.TestCase):
    def test_not_cached_is_dirt_and_queued_then_cached(self):
        md = FakeMapData(lambda key: 30)
        s = sf.Surface(md)
        self.addCleanup(s.close)
        lat = np.full((3, 3), LAT)
        lon = np.full((3, 3), LON)
        key = mapcells.cell_of(LAT, LON)
        # the first block: nothing cached yet - no data (dirt), never waiting; a fetch is queued
        md._load = lambda path, real=md._load: None if not md.prefetched else real(path)
        classes, complete = s.classes(lat, lon)
        self.assertTrue((classes == 0).all())
        s.pool.shutdown(wait=True)                      # the queued fetch ran
        self.assertIn((key, ("landcover",)), md.prefetched)
        s.pool = sf.ThreadPoolExecutor(1)
        classes, complete = s.classes(lat, lon)
        self.assertTrue((classes == 30).all() and complete)

    def test_no_coverage_is_complete_dirt(self):
        md = FakeMapData(lambda key: None)
        s = sf.Surface(md)
        self.addCleanup(s.close)
        md.store(mapcells.cell_of(LAT, LON))
        classes, complete = s.classes(np.array([LAT]), np.array([LON]))
        self.assertEqual(classes.tolist(), [0])
        self.assertTrue(complete)

    def test_ready_waits_for_the_cells_around(self):
        md = FakeMapData(lambda key: 30)
        s = sf.Surface(md)
        self.addCleanup(s.close)
        self.assertTrue(s.ready(LAT, LON, radius_m=500, timeout_s=10))
        classes, complete = s.classes(np.array([LAT]), np.array([LON]))
        self.assertEqual(classes.tolist(), [30])
        self.assertTrue(complete)

    def test_ready_gives_up_after_the_timeout(self):
        md = FakeMapData(lambda key: 30)
        import threading
        gate = threading.Event()
        real = md.prefetch
        md.prefetch = lambda key, layers: (gate.wait(5), real(key, layers))[1]
        s = sf.Surface(md)
        self.addCleanup(s.close)
        import time
        t0 = time.monotonic()
        self.assertFalse(s.ready(LAT, LON, radius_m=500, timeout_s=0.3))
        self.assertLess(time.monotonic() - t0, 2.0)
        gate.set()

    def test_prefetch_queues_the_cells_around_once_per_move(self):
        md = FakeMapData(lambda key: 30)
        s = sf.Surface(md)
        s.prefetch(LAT, LON, radius_m=2000)
        s.pool.shutdown(wait=True)
        n = len(md.prefetched)
        self.assertGreaterEqual(n, 4)
        s.pool = sf.ThreadPoolExecutor(1)
        s.prefetch(LAT + 0.001, LON, radius_m=2000)    # ~110 m: not again
        s.pool.shutdown(wait=True)
        self.assertEqual(len(md.prefetched), n)


class ServerSurfaceTest(unittest.TestCase):
    """earthd with a Surface: the masks are written next to the heightmap (rows as the heightmap's,
    row 0 = north) and the terrain message names them; Server() without one sends no layers."""

    class NorthGrass:
        def classes(self, lat, lon):
            lat = np.asarray(lat)
            return np.where(lat > LAT, 30, 40).astype(np.uint8), True

    class Dem:
        def sample(self, lat, lon, zoom, progress=None):
            return np.full(np.shape(lat), 1500.0)

        def stats_snapshot(self):
            return {}

    def make(self, **kw):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        s = earthd.Server(tmp.name, os.path.join(tmp.name, "cache"), imagery=None, cells=64, block=512, **kw)
        self.addCleanup(s.hm_pool.shutdown)
        self.addCleanup(s.tile_pool.shutdown)
        s.dem = self.Dem()
        return s

    def test_masks_and_message(self):
        s = self.make(surface=self.NorthGrass())
        a = s.new_anchor(LAT, LON)
        info = s.make_heightmap(a, 0.0, 0.0, None)
        self.assertEqual(info["layers"], sf.mask_names(s.hm_name((a.id, 0.0, 0.0))[:-4]))
        self.assertTrue(info["surfaceComplete"])
        grass = np.array(Image.open(os.path.join(s.out_dir, info["layers"][sf.GRASS])))
        dirt = np.array(Image.open(os.path.join(s.out_dir, info["layers"][sf.DIRT])))
        self.assertEqual(grass.shape, (512, 512))
        self.assertTrue((grass[:200] == 255).all() and (grass[-200:] == 0).all(), "row 0 is north (grass)")
        self.assertTrue((dirt[-200:] == 255).all())
        conn = earthd.Connection.__new__(earthd.Connection)
        conn.s = s
        msg = conn.terrain_msg((a.id, 0.0, 0.0), info)
        self.assertEqual(msg["layers"], [f"{s.vfs_dir}/{n}" for n in info["layers"]])
        self.assertEqual(msg["materials"], [list(m) for m in sf.MATERIALS])

    def test_default_server_sends_no_layers(self):
        s = self.make()
        a = s.new_anchor(LAT, LON)
        info = s.make_heightmap(a, 0.0, 0.0, None)
        self.assertNotIn("layers", info)
        conn = earthd.Connection.__new__(earthd.Connection)
        conn.s = s
        self.assertNotIn("layers", conn.terrain_msg((a.id, 0.0, 0.0), info))

    def test_cli_flag(self):
        with open(earthd.__file__) as f:
            src = f.read()
        self.assertIn('ap.add_argument("--surface", choices=("on", "off"), default="off"', src)


if __name__ == "__main__":
    unittest.main()
