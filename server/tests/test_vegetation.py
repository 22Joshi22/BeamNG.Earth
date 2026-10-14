"""vegetation: tree tops from a canopy height model (one per crown, the tallest), never on buildings or roads,
deterministic; tiles of items in the anchor frame."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import geodesy  # noqa: E402
import mapcells  # noqa: E402
import vegetation as veg  # noqa: E402

LAT, LON = 39.29, -76.58
KEY = mapcells.cell_of(LAT, LON)


class Raster:
    def __init__(self, data, x0, y0, dx, nodata=None):
        self.data, self.x0, self.y0, self.dx, self.dy, self.nodata = data, x0, y0, dx, dx, nodata

    def sample(self, lon, lat):
        return np.full(np.shape(lon), 30, np.uint8)        # grass everywhere


class Feats:
    def __init__(self, kind, rows, cls=None):
        self.kind = np.full(len(rows), kind, np.uint8)
        self.rows = rows
        self.attrs = {"class": np.array(cls or [""] * len(rows), object)}

    def parts(self, i):
        return [[np.asarray(self.rows[i], np.float64)]]


def cell_data():
    """A cell whose canopy has three crowns: one free, one under a building, one on a road."""
    lon0, lat0, lon1, lat1 = KEY.bounds()
    x0, y1 = veg._to3857(lon0, lat1)
    x1, y0 = veg._to3857(lon1, lat0)
    dx = (x1 - x0) / 512
    h = np.zeros((512, 512), np.uint8)
    yy, xx = np.mgrid[0:512, 0:512]
    crowns = [(100, 100), (300, 300), (100, 400)]          # (row, col)
    for r, c in crowns:
        h = np.maximum(h, np.clip(15 - np.hypot(yy - r, xx - c), 0, 15).astype(np.uint8))
    chm = Raster(h, x0, y1, dx, nodata=255)

    def lonlat(r, c):
        return veg._to4326(x0 + (c + 0.5) * dx, y1 - (r + 0.5) * dx)

    def square(r, c, half):
        pts = [lonlat(r - half, c - half), lonlat(r - half, c + half), lonlat(r + half, c + half),
               lonlat(r + half, c - half), lonlat(r - half, c - half)]
        return [(float(a), float(b)) for a, b in pts]
    building = Feats(3, [square(300, 300, 6)])
    a, b = lonlat(100, 380), lonlat(100, 420)
    road = Feats(2, [[(float(a[0]), float(a[1])), (float(b[0]), float(b[1]))]], ["residential"])
    layers = {"canopy": chm, "landcover": Raster(None, 0, 0, 1), "buildings": building, "roads": road,
              "water": Feats(3, [])}
    return SimpleNamespace(bounds=KEY.bounds(), layers=layers), crowns, lonlat


class FakeMapData:
    release = "t"

    def __init__(self):
        self.cd, self.crowns, self.lonlat = cell_data()

    def cell(self, key, layers=None, kind="demand"):
        if tuple(key)[1:] == (KEY.x, KEY.y):
            return self.cd
        empty = SimpleNamespace(bounds=None, layers={"buildings": None, "roads": None, "water": None})
        return empty


class VegetationTest(unittest.TestCase):
    def test_one_tree_per_free_crown(self):
        md = FakeMapData()
        it = veg.Vegetation(md, None).cell(KEY)
        trees = it.take(it.kind == veg.KIND_TREE)
        self.assertEqual(len(trees), 1, "the building's and the road's crowns are left out")
        lon, lat = md.lonlat(100, 100)
        self.assertLess(abs(trees.lon[0] - lon) * 85000 + abs(trees.lat[0] - lat) * 111000, 4.0)
        self.assertGreaterEqual(trees.height[0], 12.0)

    def test_deterministic(self):
        a = veg.Vegetation(FakeMapData(), None).cell(KEY)
        b = veg.Vegetation(FakeMapData(), None).cell(KEY)
        for f in ("lon", "lat", "height", "kind", "group", "rank", "yaw", "scale"):
            np.testing.assert_array_equal(getattr(a, f), getattr(b, f))

    def test_thin_keeps_the_tallest(self):
        jj = np.array([10, 10, 11, 40])          # crown radius of the 14 m top: 0.8 (0.12 h + 1) / 1.2 = 1.8 px
        ii = np.array([11, 12, 11, 40])
        h = np.array([10.0, 14.0, 9.0, 8.0])
        kj, ki = veg._thin(jj, ii, h, 1.2)
        self.assertEqual(sorted(zip(kj.tolist(), ki.tolist())), [(10, 12), (40, 40)])

    def test_tile_items_in_the_anchor_frame(self):
        md = FakeMapData()
        v = veg.Vegetation(md, None)
        f = geodesy.EnuFrame(LAT, LON)
        items = v.cell(KEY)
        e, n, _ = f.geodetic_to_enu(items.lat, items.lon, np.zeros(len(items)))
        tx, ty = int(np.floor(e[0] / veg.TILE)), int(np.floor(n[0] / veg.TILE))
        rows = veg.tile_items(f, tx, ty, v, lambda k: True, lambda la, lo: np.full(np.shape(la), 12.0))
        self.assertTrue(rows)
        g, x, y, z, yaw, sc, hgt, rank = rows[0]
        self.assertTrue(tx * veg.TILE <= x < (tx + 1) * veg.TILE and ty * veg.TILE <= y < (ty + 1) * veg.TILE)
        self.assertAlmostEqual(z, float(f.geodetic_to_enu(items.lat[:1], items.lon[:1], np.array([12.0]))[2][0]),
                               delta=0.5)


if __name__ == "__main__":
    unittest.main()
