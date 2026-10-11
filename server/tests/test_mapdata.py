"""The map-data pipeline (mapdata.py and its helpers mapcells, rangeread, cog, maprasters, mapgeom,
overture): global cell keys, STAC filtering, range reads and their block cache, the COG reader,
GeoParquet parsing through DuckDB, clipping, determinism (content digests independent of fetch
order, file order and row order), cache hits, netfetch request kinds, licences in the provenance.

No network: synthetic sources from mapfixtures.py behind a fake netfetch transport. The Overture
tests need duckdb and fsspec (server/requirements.txt) and are skipped without them."""
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cog  # noqa: E402
import mapcells  # noqa: E402
import mapdata  # noqa: E402
import mapfixtures as mf  # noqa: E402
import mapgeom as mg  # noqa: E402
import maprasters  # noqa: E402
import netfetch  # noqa: E402
import overture  # noqa: E402
import rangeread  # noqa: E402

try:
    import duckdb  # noqa: F401 (only whether it is installed)
    HAVE_DUCKDB = rangeread.NetfetchFS is not None
except ImportError:
    HAVE_DUCKDB = False
needs_duckdb = unittest.skipUnless(HAVE_DUCKDB, "duckdb / fsspec not installed (server/requirements.txt)")
REPO = Path(__file__).resolve().parents[2]


class SmallBlocks:
    """Run with 4 KiB blocks and an 8 KiB footer guess (the fixtures are a few hundred KB)."""

    def setUp(self):
        self._saved = (rangeread.BLOCK, overture.FOOTER_GUESS, overture.MERGE_GAP)
        rangeread.BLOCK, overture.FOOTER_GUESS, overture.MERGE_GAP = 4096, 8192, 1024
        self.tmp = tempfile.mkdtemp(prefix="mapdata-test-")

    def tearDown(self):
        rangeread.BLOCK, overture.FOOTER_GUESS, overture.MERGE_GAP = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)


# --- cells ---------------------------------------------------------------------------------------
class CellKeyTest(unittest.TestCase):
    def test_benchmark_cells(self):
        self.assertEqual(mapcells.cell_of(39.2834, -76.5934), mapcells.CellKey(14, 4706, 6244))
        self.assertEqual(mapcells.cell_of(36.7818, -110.1393), mapcells.CellKey(14, 3179, 6389))
        self.assertEqual(mapcells.cell_of(36.7874, -110.2382), mapcells.CellKey(14, 3174, 6389))
        # the canopy height file of a cell is its quadkey-10 parent
        self.assertEqual(mapcells.CellKey(14, 4706, 6244).parent(10).quadkey(), "0320100330")
        self.assertEqual(mapcells.CellKey(14, 4706, 6244).quadkey(), "03201003300210")

    def test_bounds_center_roundtrip(self):
        for lat, lon in [(0.0, 0.0), (39.2834, -76.5934), (-33.86, 151.21), (64.1, -21.9), (85.0, 179.99),
                         (-85.0, -179.99)]:
            k = mapcells.cell_of(lat, lon)
            lon0, lat0, lon1, lat1 = k.bounds()
            self.assertTrue(lon0 <= lon < lon1 and lat0 < lat <= lat1 + 1e-12, (lat, lon, k.bounds()))
            clat, clon = k.center()
            self.assertEqual(mapcells.cell_of(clat, clon), k)
            self.assertEqual(mapcells.parse(str(k)), k)
            self.assertEqual(mapcells.parse(str(k).replace("/", "_")), k)

    def test_edges_and_limits(self):
        k = mapcells.CellKey(14, 4706, 6244)
        lon0, lat0, lon1, lat1 = k.bounds()
        self.assertEqual(mapcells.cell_of(lat1, lon0), k)              # the NW corner belongs to it
        self.assertEqual(mapcells.cell_of(lat0, lon0).y, k.y + 1)      # the south edge to the cell below
        self.assertEqual(mapcells.cell_of(10.0, 180.0).x, 0)           # 180 E wraps to x = 0
        with self.assertRaises(ValueError):
            mapcells.cell_of(86.0, 0.0)
        for bad in ("14/16384/0", "14/1/2/3", "a/b/c", "14/-1/0"):
            with self.assertRaises(ValueError):
                mapcells.parse(bad)
        self.assertGreater(k.size_m()[0], 1800)
        self.assertLess(k.size_m()[0], 2000)

    def test_cells_for_bbox(self):
        k = mapcells.CellKey(14, 4706, 6244)
        lon0, lat0, lon1, lat1 = k.bounds()
        self.assertEqual(mapcells.cells_for_bbox(lat0 + 1e-6, lon0 + 1e-6, lat1 - 1e-6, lon1 - 1e-6), [k])
        got = mapcells.cells_for_bbox(lat0 - 1e-6, lon0 - 1e-6, lat1 + 1e-6, lon1 + 1e-6)
        self.assertEqual(len(got), 9)
        self.assertEqual(got[4], k)                                    # row-major, north first
        self.assertEqual([c.y for c in got[:3]], [k.y - 1] * 3)
        self.assertEqual(sorted(got[:4] + got[5:]), sorted(k.neighbours()))
        # across the antimeridian
        wrap = mapcells.cells_for_bbox(10.0, 179.99, 10.01, -179.99)
        self.assertEqual({c.x for c in wrap}, {0, (1 << 14) - 1})
        with self.assertRaises(ValueError):
            mapcells.cells_for_bbox(0, 0, 10, 10)

    def test_owner_is_unique(self):
        """Every point has exactly one owner, the cell that contains it."""
        rng = np.random.default_rng(1)
        lat, lon = rng.uniform(-80, 80, 500), rng.uniform(-180, 180, 500)
        ox, oy = mapcells.owner(lon, lat)
        for i in range(0, 500, 50):
            self.assertEqual((ox[i], oy[i]), mapcells.cell_of(lat[i], lon[i])[1:])


# --- geometry -------------------------------------------------------------------------------------
def wkb_polygon(rings, bo="<"):
    import struct
    b = struct.pack(bo + "bII", 1 if bo == "<" else 0, 3, len(rings))
    for r in rings:
        b += struct.pack(bo + "I", len(r)) + b"".join(struct.pack(bo + "dd", *p) for p in r)
    return b


class GeomTest(unittest.TestCase):
    def test_wkb(self):
        import struct
        sq = [(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)]
        k, parts = mg.parse_wkb(wkb_polygon([sq]))
        self.assertEqual(k, mg.POLYGON)
        np.testing.assert_array_equal(parts[0][0], np.array(sq, float))
        k, parts = mg.parse_wkb(wkb_polygon([sq], ">"))               # big endian
        np.testing.assert_array_equal(parts[0][0], np.array(sq, float))
        pt = struct.pack("<bIddd", 1, 1001, 1.0, 2.0, 3.0)             # ISO POINT Z
        self.assertEqual(mg.parse_wkb(pt)[1][0][0].tolist(), [[1.0, 2.0]])
        line = struct.pack("<bII", 1, 2, 2) + struct.pack("<dddd", 0, 0, 1, 1)
        multi = struct.pack("<bII", 1, 6, 2) + wkb_polygon([sq]) + wkb_polygon([sq])
        self.assertEqual(mg.parse_wkb(line)[0], mg.LINE)
        k, parts = mg.parse_wkb(multi)
        self.assertEqual((k, len(parts)), (mg.POLYGON, 2))
        with self.assertRaises(mg.WkbError):
            mg.parse_wkb(line[:-4])

    def area(self, ring):
        x, y = ring[:, 0], ring[:, 1]
        return 0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))

    def test_clip(self):
        box = (0.0, 0.0, 1.0, 1.0)
        inside = np.array([(0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.2)])
        np.testing.assert_array_equal(mg.clip_ring(inside, box), inside)
        half = np.array([(0.5, -1.0), (2.0, -1.0), (2.0, 2.0), (0.5, 2.0), (0.5, -1.0)])
        c = mg.clip_ring(half, box)
        self.assertAlmostEqual(self.area(c), 0.5)
        self.assertTrue(np.array_equal(c[0], c[-1]))
        around = np.array([(-5, -5), (5, -5), (5, 5), (-5, 5), (-5, -5)], float)
        self.assertAlmostEqual(self.area(mg.clip_ring(around, box)), 1.0)       # surrounds the box: the box
        outside = np.array([(2, 2), (3, 2), (3, 3), (2, 2)], float)
        self.assertIsNone(mg.clip_ring(outside, box))
        tri = np.array([(-1, 0.5), (0.5, -1), (2, 0.5), (-1, 0.5)], float)     # a concave cut
        self.assertGreater(self.area(mg.clip_ring(tri, box)), 0.3)
        # exact edge coordinates where the ring was cut
        self.assertTrue(set(np.round(c[:, 0], 12)) <= {0.5, 1.0})
        parts, changed = mg.clip_polygon_parts([[around, np.array([(0.4, 0.4), (0.6, 0.4), (0.6, 0.6),
                                                                    (0.4, 0.4)])]], box)
        self.assertTrue(changed)
        self.assertEqual(len(parts[0]), 2)                                      # the hole survives

    def test_build_and_arrays(self):
        sq = np.array([(0, 0), (1, 0), (1, 1), (0, 0)], float)
        f = mg.build("x", mg.text_column(["a", "b"]), [(mg.POLYGON, [[sq, sq * 0.5]]), (mg.LINE, [[sq[:2]]])])
        self.assertEqual(f.part_off.tolist(), [0, 1, 2])
        self.assertEqual(f.ring_off.tolist(), [0, 2, 3])
        self.assertEqual(f.coord_off.tolist(), [0, 4, 8, 10])
        self.assertEqual(f.index("b"), 1)
        self.assertEqual(f.index("c"), -1)
        f.attrs["h"] = mg.float_column([1.5, None])
        f.rules["r"] = mg.rule_table([(1, "paved", None), (1, "dirt", [0.5, 1.0])])
        g = mg.Features.from_arrays("x", f.to_arrays("p/"), "p/")
        self.assertEqual(g.parts(0)[0][1].tolist(), (sq * 0.5).tolist())
        self.assertTrue(np.isnan(g.attrs["h"][1]))
        self.assertEqual(g.rules["r"].for_feature(1), [("paved", 0.0, 1.0), ("dirt", 0.5, 1.0)])


# --- COG -----------------------------------------------------------------------------------------
class CogTest(unittest.TestCase):
    def image(self, w, h):
        rr, cc = np.mgrid[0:h, 0:w]
        return ((cc * 7 + rr * 3) % 251).astype(np.uint8)

    def check(self, **kw):
        w, h, t = 100, 70, 32
        img = self.image(w, h)

        def tile(tx, ty):
            a = np.zeros((t, t), np.uint8)
            part = img[ty * t:(ty + 1) * t, tx * t:(tx + 1) * t]
            a[:part.shape[0], :part.shape[1]] = part
            return None if (tx, ty) == (2, 1) else a                     # one sparse tile
        data = mf.tiff_bytes(w, h, t, tile, pixel_scale=(0.5, 0.25), tiepoint=(10.0, 20.0), epsg=4326,
                             nodata=255, **kw)
        src = rangeread.MemoryRange(data)
        c = cog.Cog(src)
        self.assertEqual((c.width, c.height, c.tile_w, c.epsg, c.nodata), (w, h, t, 4326, 255.0))
        self.assertEqual((c.x0, c.y0, c.sx, c.sy), (10.0, 20.0, 0.5, 0.25))
        want = img.copy()
        want[32:64, 64:96] = 255                                         # the sparse tile is nodata
        np.testing.assert_array_equal(c.read_window(0, 0, w, h), want)
        np.testing.assert_array_equal(c.read_window(5, 7, 40, 30), want[7:37, 5:45])
        out = c.read_window(90, 60, 20, 20)                              # partly outside: nodata
        np.testing.assert_array_equal(out[:10, :10], want[60:70, 90:100])
        self.assertTrue((out[10:, :] == 255).all() and (out[:, 10:] == 255).all())
        return c, src

    def test_deflate(self):
        c, src = self.check()
        n = len(src.reads)
        c.read_window(0, 0, 64, 32)                                      # two neighbouring tiles: one read
        self.assertEqual(len(src.reads), n + 1)

    def test_predictor_raw_bigendian(self):
        self.check(predictor=2)
        self.check(compress=False)
        self.check(big_endian=True)

    def test_not_tiff(self):
        with self.assertRaises(cog.CogError):
            cog.Cog(rangeread.MemoryRange(b"GIF89a" + b"\0" * 100))


# --- range reads ---------------------------------------------------------------------------------
class RangeTest(SmallBlocks, unittest.TestCase):
    URL = "https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com/f.parquet"
    MIRROR = "https://overturemapswestus2.blob.core.windows.net/f.parquet"

    def setUp(self):
        super().setUp()
        self.data = bytes(np.random.default_rng(3).integers(0, 256, 50000, dtype=np.uint8))
        self.t = mf.Transport({self.URL: self.data, self.MIRROR: self.data})
        self.f = mf.fetcher(self.t)

    def remote(self, size="known"):
        return rangeread.RemoteFile([self.URL, self.MIRROR], len(self.data) if size == "known" else None,
                                    os.path.join(self.tmp, "f"), self.f)

    def test_reads_and_cache(self):
        r = self.remote()
        self.assertEqual(r.read(100, 9000), self.data[100:9000])
        self.assertEqual(self.t.count(), 1)                              # blocks 0-2 in one request
        self.assertEqual(r.read(5000, 6000), self.data[5000:6000])
        self.assertEqual(self.t.count(), 1)                              # memory
        r2 = self.remote()                                               # a new instance: from disk
        self.assertEqual(r2.read(0, 12288), self.data[:12288])
        self.assertEqual(self.t.count(), 1)
        self.assertEqual(r2.read(49000, 60000), self.data[49000:])       # cut at the end
        self.assertEqual(set(os.listdir(os.path.join(self.tmp, "f"))),
                         {"0.blk", "1.blk", "2.blk", "11.blk", "12.blk"})

    def test_size_from_content_range(self):
        r = self.remote(size=None)
        self.assertEqual(r.read(0, 10), self.data[:10])
        self.assertEqual(r.size, len(self.data))

    def test_mirror_failover(self):
        self.t.fail[netfetch.host_of(self.URL)] = 503
        r = self.remote()
        self.assertEqual(r.read(0, 100), self.data[:100])
        self.assertTrue(any(u == self.MIRROR for u, _ in self.t.log))

    def test_missing_marker(self):
        r = rangeread.RemoteFile("https://esa-worldcover.s3.eu-central-1.amazonaws.com/none.tif", None,
                                 os.path.join(self.tmp, "none"), self.f)
        with self.assertRaises(rangeread.Missing):
            r.read(0, 10)
        n = self.t.count()
        with self.assertRaises(rangeread.Missing):                       # remembered, no request
            r.read(0, 10)
        self.assertEqual(self.t.count(), n)

    def test_one_fetch_per_block_across_threads(self):
        gate = threading.Event()
        inner = self.t.request

        def slow(url, headers, timeout):
            gate.wait(2)
            return inner(url, headers, timeout)
        self.t.request = slow
        r = self.remote()
        out = []
        ths = [threading.Thread(target=lambda: out.append(r.read(0, 4096))) for _ in range(4)]
        for th in ths:
            th.start()
        gate.set()
        for th in ths:
            th.join()
        self.assertEqual(out, [self.data[:4096]] * 4)
        self.assertEqual(self.t.count(), 1)

    def test_request_kind(self):
        """The kind reaches netfetch: a host without prefetch refuses a prefetch range."""
        pol = netfetch.with_policy(mf.FAST, netfetch.host_of(self.URL), allow_prefetch=False)
        pol = netfetch.with_policy(pol, netfetch.host_of(self.MIRROR), allow_prefetch=False)
        r = rangeread.RemoteFile([self.URL, self.MIRROR], len(self.data), os.path.join(self.tmp, "k"),
                                 mf.fetcher(self.t, pol))
        with self.assertRaises(netfetch.PolicyDenied):
            r.read(0, 10, "prefetch")
        self.assertEqual(r.read(0, 10, "demand"), self.data[:10])


# --- the pipeline over synthetic sources ---------------------------------------------------------
class Sources:
    """Builds the synthetic release + rasters once per test class."""

    @classmethod
    def setUpClass(cls):
        cls.fix_dir = tempfile.mkdtemp(prefix="mapdata-fix-")
        files, cls.names = mf.release(cls.fix_dir)
        files.update(mf.rasters())
        cls.files = files

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.fix_dir, ignore_errors=True)

    def mapdata(self, files=None, cache=None, policies=None, **kw):
        t = mf.Transport(files or self.files)
        md = mapdata.MapData(cache or os.path.join(self.tmp, "cache"), mf.fetcher(t, policies, **kw), mf.RELEASE)
        self.addCleanup(md.close)
        return md, t


@needs_duckdb
class StacTest(Sources, SmallBlocks, unittest.TestCase):
    def test_files_for_cell(self):
        md, t = self.mapdata()
        idx = md.overture.index
        hits = idx.files("buildings", "building", mf.CELL.bounds())
        self.assertEqual([h[0] for h in hits], [self.names[("buildings", "building")][0]])
        self.assertEqual(len(hits[0][1]), 2)                              # S3 + Azure mirror
        self.assertEqual(t.count("/00001/"), 0)                           # the far file's item: not asked
        n = t.count()
        idx2 = overture.StacIndex(mf.RELEASE, md.raw_dir, md.fetcher)     # cached on disk
        self.assertEqual(idx2.files("buildings", "building", mf.CELL.bounds()), hits)
        self.assertEqual(t.count(), n)
        far = mapcells.cell_of(10.0, 10.0)
        self.assertEqual(idx.files("buildings", "building", far.bounds()), [])

    def test_release_gone(self):
        md, t = self.mapdata()
        idx = overture.StacIndex("1999-01-01.0", md.raw_dir, md.fetcher)
        with self.assertRaises(overture.ReleaseGone):
            idx.files("buildings", "building", mf.CELL.bounds())

    def test_intersects_inclusive(self):
        self.assertTrue(overture.intersects([0, 0, 1, 1], (1, 1, 2, 2)))
        self.assertFalse(overture.intersects([0, 0, 1, 1], (1.0001, 0, 2, 2)))


@needs_duckdb
class OvertureTest(Sources, SmallBlocks, unittest.TestCase):
    def test_roads(self):
        md, t = self.mapdata()
        cd = md.cell(mf.CELL, ("roads",))
        r = cd.layers["roads"]
        self.assertEqual(list(r.ids), ["seg-00", "seg-01", "seg-02", "seg-03", "seg-04", "seg-edge"])
        self.assertEqual(list(r.attrs["class"]), ["residential", "primary", "footway", "service", "track",
                                                  "secondary"])               # seg-03 v1, not the old v0
        self.assertEqual(r.attrs["subclass"][2], "sidewalk")
        self.assertEqual(r.attrs["name"][0], "Street 0")
        self.assertEqual(r.own.tolist(), [True] * 5 + [False])               # seg-edge is the east cell's
        self.assertEqual(r.rules["surface"].for_feature(1), [("paved", 0.0, 0.5), ("gravel", 0.5, 1.0)])
        self.assertEqual(r.rules["flags"].for_feature(0), [("is_bridge", 0.25, 0.5), ("is_link", 0.25, 0.5)])
        self.assertEqual(r.rules["width"].for_feature(0), [(9.5, 0.0, 1.0)])
        self.assertEqual(r.rules["level"].for_feature(0), [(1.0, 0.25, 0.5)])
        speed = r.rules["max_speed_kmh"]
        self.assertAlmostEqual(speed.for_feature(0)[0][0], 25 * 1.609344, places=4)   # mph, unconditional only
        self.assertEqual(len(speed.for_feature(0)), 1)
        self.assertAlmostEqual(speed.for_feature(1)[0][0], 50.0)
        self.assertEqual([v for v, a, b in r.rules["connectors"].for_feature(0)], ["con-a", "con-b"])
        # geometry: lon/lat and local metres (north-south line ~0.6 of the cell long)
        line = r.parts(0, r.en)[0][0]
        self.assertAlmostEqual(abs(line[1, 1] - line[0, 1]), 0.6 * mf.CELL.size_m()[1], delta=15)
        self.assertLess(abs(line[1, 0] - line[0, 0]), 0.5)          # a meridian in the tangent plane
        lon, lat = r.parts(0)[0][0][0]
        self.assertEqual((lon, lat), mf._cell_xy(0.1, 0.2))
        # only the cell's file was read; its row groups without a match were not
        self.assertEqual(t.count("part-00001"), 0)
        size = len(self.files[f"{mf.S3}/release/{mf.RELEASE}/theme=transportation/type=segment/"
                              f"{self.names[('transportation', 'segment')][0]}"])
        self.assertLess(t.bytes("type=segment"), 0.6 * size)

    def test_buildings_water_land_use(self):
        md, t = self.mapdata()
        cd = md.cell(mf.CELL, ("buildings", "water", "land_use", "infrastructure", "connectors"))
        b = cd.layers["buildings"]
        self.assertEqual(len(b), 7)
        i = b.index("bld-01")
        self.assertEqual((b.attrs["height"][i], b.attrs["num_floors"][i], b.attrs["roof_shape"][i]),
                         (np.float32(7.5), np.float32(2), "gabled"))
        self.assertEqual(b.attrs["height_src"][i], "Microsoft ML Buildings")
        self.assertTrue(np.isnan(b.attrs["height"][b.index("bld-00")]))
        self.assertEqual(len(b.parts(b.index("bld-yard"))[0]), 2)               # exterior + courtyard
        w = cd.layers["water"]
        h = w.index("wat-harbour")
        self.assertTrue(w.clipped[h] and w.attrs["is_salt"][h])
        ring = w.parts(h)[0][0]
        lon0, lat0, lon1, lat1 = mf.CELL.bounds()
        self.assertTrue(ring[:, 0].max() <= lon1 and ring[:, 1].min() >= lat0)  # cut at the cell edges
        self.assertEqual(w.kind[w.index("wat-canal")], mg.LINE)
        lu = cd.layers["land_use"]
        ring = lu.parts(0)[0][0]
        self.assertEqual((ring[:, 0].min(), ring[:, 0].max(), ring[:, 1].min(), ring[:, 1].max()),
                         (lon0, lon1, lat0, lat1))                             # the park covers the cell
        self.assertEqual(json.loads(lu.attrs["tags"][0]), {"leisure": "park"})
        self.assertEqual(lu.attrs["name"][0], "Patterson Park")
        inf = cd.layers["infrastructure"]
        self.assertEqual(list(inf.attrs["class"]), ["power_line", "power_pole"])
        self.assertEqual(inf.kind.tolist(), [mg.LINE, mg.POINT])
        self.assertEqual(list(cd.layers["connectors"].ids), ["con-a", "con-b"])

    def test_empty_layer_keeps_schema(self):
        md, t = self.mapdata()
        far = mapcells.cell_of(10.0, 10.0)
        cd = md.cell(far, ("roads", "water"))
        self.assertEqual(len(cd.layers["roads"]), 0)
        self.assertIn("class", cd.layers["roads"].attrs)
        self.assertIn("surface", cd.layers["roads"].rules)
        self.assertEqual(cd.provenance, [])

    def test_fetch_errors_surface(self):
        """A host that cannot deliver: the netfetch error, not DuckDB's generic one."""
        md, t = self.mapdata()
        for h in ("overturemaps-us-west-2.s3.us-west-2.amazonaws.com", "overturemapswestus2.blob.core.windows.net"):
            t.fail[h] = 500
        with self.assertRaises(netfetch.FetchError):
            md.cell(mf.CELL, ("buildings",))
        self.assertFalse(os.path.exists(md.path(mf.CELL, "buildings")))      # nothing cached


@needs_duckdb
class RasterLayerTest(Sources, SmallBlocks, unittest.TestCase):
    def test_landcover_and_canopy(self):
        md, t = self.mapdata()
        cd = md.cell(mf.CELL, ("landcover", "canopy"))
        lc = cd.layers["landcover"]
        c0, r0, c1, r1 = maprasters.worldcover_window(mf.CELL.bounds())
        cc, rr = np.meshgrid(np.arange(c0, c1), np.arange(r0, r1))
        np.testing.assert_array_equal(lc.data, mf.worldcover_class(cc, rr))
        lon, lat = mf._cell_xy(0.3, 0.3)
        col, row = int((lon + 180) * 12000), int((90 - lat) * 12000)
        self.assertEqual(lc.sample(lon, lat), mf.worldcover_class(col, row))
        ch = cd.layers["canopy"]
        self.assertEqual(ch.data.shape, (2048, 2048))
        x0, y0 = (mf.CELL.x - 294 * 16) * 2048, (mf.CELL.y - 390 * 16) * 2048
        cc, rr = np.meshgrid(np.arange(x0, x0 + 2048), np.arange(y0, y0 + 2048))
        np.testing.assert_array_equal(ch.data, mf.canopy_value(cc, rr))
        self.assertLessEqual(t.count("dataforgood"), 1 + 4)   # header + at most one read per row of 4 tiles

    def test_missing_canopy_file(self):
        files = {k: v for k, v in self.files.items() if "dataforgood" not in k}
        md, t = self.mapdata(files)
        cd = md.cell(mf.CELL, ("canopy", "landcover"))
        self.assertIsNone(cd.layers["canopy"])
        self.assertEqual([p["id"] for p in cd.provenance], ["esa-worldcover"])
        md2, t2 = self.mapdata(files)                         # cached as missing: no request
        self.assertIsNone(md2.cell(mf.CELL, ("canopy",)).layers["canopy"])
        self.assertEqual(t2.count(), 0)


@needs_duckdb
class DeterminismTest(Sources, SmallBlocks, unittest.TestCase):
    def test_digest_independent_of_order(self):
        """Same content -> same digest: cold vs warm, layers asked in another order or one by one,
        blocks fetched in another order, rows shuffled inside the parquet files."""
        md, t = self.mapdata(cache=os.path.join(self.tmp, "a"))
        a = md.cell(mf.CELL).digest()
        self.assertEqual(md.cell(mf.CELL).digest(), a)                         # warm (from the cache)
        md2, t2 = self.mapdata(cache=os.path.join(self.tmp, "b"))
        for layer in reversed(mapdata.LAYERS):                                  # one layer at a time
            md2.cell(mf.CELL, (layer,))
        self.assertEqual(md2.cell(mf.CELL, tuple(reversed(mapdata.LAYERS))).digest(), a)
        # the raw blocks fetched backwards first (another fetch order), cells built afterwards
        md3, t3 = self.mapdata(cache=os.path.join(self.tmp, "c"))
        for url, data in sorted(self.files.items(), reverse=True):
            if url.startswith(mf.S3) and "type=" in url:
                theme, typ = url.split("theme=")[1].split("/")[0], url.split("type=")[1].split("/")[0]
                r = rangeread.RemoteFile(url, len(data), os.path.join(md3.raw_dir, "overture", mf.RELEASE, theme,
                                                                       typ, url.rsplit("/", 1)[1]), md3.fetcher)
                for i in reversed(range(-(-len(data) // rangeread.BLOCK))):
                    r.blocks(i, i + 1)
        n = t3.count(mf.S3)
        self.assertEqual(md3.cell(mf.CELL).digest(), a)
        self.assertEqual(t3.count(mf.S3), n)                                   # all from those blocks
        # rows in another order inside the files
        fix2 = tempfile.mkdtemp(dir=self.tmp)
        files2, _ = mf.release(fix2, shuffle=np.random.default_rng(7))
        files2.update(mf.rasters())
        self.assertNotEqual(files2[[u for u in files2 if "type=building/part-00000" in u][0]],
                            self.files[[u for u in self.files if "type=building/part-00000" in u][0]])
        md4, t4 = self.mapdata(files2, cache=os.path.join(self.tmp, "d"))
        self.assertEqual(md4.cell(mf.CELL).digest(), a)

    def test_digest_changes_with_content_and_versions(self):
        md, t = self.mapdata(cache=os.path.join(self.tmp, "a"))
        cd = md.cell(mf.CELL, ("buildings",))
        a = cd.digest()
        cd.layers["buildings"].attrs["height"] = cd.layers["buildings"].attrs["height"].copy()
        cd.layers["buildings"].attrs["height"][0] = 3.0
        self.assertNotEqual(cd.digest(), a)
        cd2 = md.cell(mf.CELL, ("buildings",))
        cd2.versions = dict(cd2.versions, overture="other")
        self.assertNotEqual(cd2.digest(), a)

    def test_concurrent_cells(self):
        md, t = self.mapdata(cache=os.path.join(self.tmp, "x"))
        out = []
        ths = [threading.Thread(target=lambda: out.append(md.cell(mf.CELL).digest())) for _ in range(3)]
        for th in ths:
            th.start()
        for th in ths:
            th.join()
        self.assertEqual(len(set(out)), 1)
        md2, _ = self.mapdata(cache=os.path.join(self.tmp, "y"))
        self.assertEqual(md2.cell(mf.CELL).digest(), out[0])


@needs_duckdb
class CacheTest(Sources, SmallBlocks, unittest.TestCase):
    def test_layout_and_hits(self):
        cache = os.path.join(self.tmp, "cache")
        md, t = self.mapdata(cache=cache)
        cd = md.cell(mf.CELL)
        self.assertTrue(all(not s["hit"] for s in cd.stats.values()))
        n = t.count()
        self.assertGreater(n, 0)
        rel = os.path.relpath(md.path(mf.CELL, "roads"), cache)
        self.assertEqual(rel, f"mapdata/v{mapdata.VERSION}/overture/{mf.RELEASE}/roads/14/4706/6244.npz")
        self.assertTrue(os.path.relpath(md.path(mf.CELL, "landcover"), cache).startswith(
            f"mapdata/v{mapdata.VERSION}/worldcover/{maprasters.WORLDCOVER_RELEASE}/landcover/14/"))
        self.assertTrue(os.path.isdir(os.path.join(cache, "mapdata", "raw", "overture", mf.RELEASE, "stac")))
        cd2 = md.cell(mf.CELL)
        self.assertTrue(all(s["hit"] for s in cd2.stats.values()))
        self.assertEqual(t.count(), n)
        md2, t2 = self.mapdata(cache=cache)                                      # a new process: disk
        md2.cell(mf.CELL)
        self.assertEqual(t2.count(), 0)
        # the layer files hold no pickles
        with np.load(md.path(mf.CELL, "roads"), allow_pickle=False) as z:
            self.assertIn("ids", z.files)
        # offline: the cache serves; an uncached cell raises netfetch.Offline
        md3, t3 = self.mapdata(cache=cache, offline=True)
        self.assertEqual(md3.cell(mf.CELL).digest(), cd.digest())
        with self.assertRaises(netfetch.Offline):
            md3.cell(mapcells.cell_of(10.0, 10.0), ("landcover",))

    def test_neighbour_reuses_blocks(self):
        md, t = self.mapdata()
        md.cell(mf.CELL, ("roads",))
        n = t.count("type=segment")
        md.cell(mapcells.CellKey(14, 4707, 6244), ("roads",))                  # east: seg-edge lives there
        self.assertLessEqual(t.count("type=segment") - n, 2)


@needs_duckdb
class KindsTest(Sources, SmallBlocks, unittest.TestCase):
    def test_prefetch_refused_then_demand(self):
        hosts = ["overturemaps-us-west-2.s3.us-west-2.amazonaws.com", "overturemapswestus2.blob.core.windows.net",
                 "stac.overturemaps.org"]
        pol = dict(mf.FAST)
        for h in hosts:
            pol[h] = replace(pol[h], allow_prefetch=False)
        md, t = self.mapdata(policies=pol)
        self.assertFalse(md.prefetch(mf.CELL, ("roads",)))
        self.assertFalse(os.path.exists(md.path(mf.CELL, "roads")))
        self.assertEqual(len(md.cell(mf.CELL, ("roads",)).layers["roads"]), 6)  # demand gets it

    def test_prefetch_and_background(self):
        md, t = self.mapdata()
        self.assertTrue(md.prefetch(mf.CELL, ("roads", "landcover")))
        self.assertTrue(os.path.exists(md.path(mf.CELL, "roads")))
        md2, _ = self.mapdata(cache=os.path.join(self.tmp, "bg"))
        cd = md2.cell(mf.CELL, ("buildings",), kind="background")
        self.assertEqual(len(cd.layers["buildings"]), 7)

    def test_policies(self):
        for h in ("overturemaps-us-west-2.s3.us-west-2.amazonaws.com", "overturemapswestus2.blob.core.windows.net",
                  "esa-worldcover.s3.eu-central-1.amazonaws.com", "dataforgood-fb-data.s3.amazonaws.com"):
            p = netfetch.POLICIES[h]
            self.assertEqual((p.rate, p.burst, p.max_conc, p.allow_prefetch, p.miss_ttl), (20.0, 40.0, 4, True, None))
        self.assertTrue(netfetch.POLICIES["stac.overturemaps.org"].allow_prefetch)


@needs_duckdb
class ProvenanceTest(Sources, SmallBlocks, unittest.TestCase):
    def test_licences(self):
        md, t = self.mapdata()
        cd = md.cell(mf.CELL)
        ids = [p["id"] for p in cd.provenance]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(set(ids), {"overture:transportation", "overture:buildings", "overture:base",
                                    "esa-worldcover", "meta-wri-chm"})
        tr = next(p for p in cd.provenance if p["id"] == "overture:transportation")
        self.assertEqual(tr["attribution"], "© OpenStreetMap contributors, Overture Maps Foundation")
        self.assertEqual(tr["release"], mf.RELEASE)
        self.assertIn("Open Database License", tr["licence"])
        self.assertEqual(tr["datasets"], [{"dataset": "OpenStreetMap", "license": "ODbL-1.0", "features": 8}])
        bl = next(p for p in cd.provenance if p["id"] == "overture:buildings")
        self.assertIn({"dataset": "Microsoft ML Buildings", "license": "ODbL-1.0", "features": 1}, bl["datasets"])
        wc = next(p for p in cd.provenance if p["id"] == "esa-worldcover")
        self.assertIn("CC BY 4.0", wc["licence"])
        self.assertEqual(wc["files"], ["N39W078"])
        self.assertEqual(cd.credits(), ["esa-worldcover", "meta-wri-chm", "overture"])
        self.assertIn("© OpenStreetMap contributors, Overture Maps Foundation", cd.credit_line())

    def test_credits_md_has_the_wordings(self):
        text = (REPO / "CREDITS.md").read_text()
        for line in list(mapdata.CREDIT_LINES.values()) + [mapdata.WORLDCOVER["attribution"],
                                                            mapdata.CANOPY["attribution"]]:
            self.assertIn(line, text)


@needs_duckdb
class CliTest(Sources, SmallBlocks, unittest.TestCase):
    def test_dump_and_png_offline(self):
        cache = os.path.join(self.tmp, "cache")
        md, t = self.mapdata(cache=cache)
        digest = md.cell(mf.CELL).digest()
        png = os.path.join(self.tmp, "out", "cell.png")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = mapdata.main(["--key", str(mf.CELL), "--dump", "--png", png, "--cache-dir", cache,
                               "--release", mf.RELEASE, "--offline"])
        self.assertEqual(rc, 0, err.getvalue())
        d = json.loads(out.getvalue())
        c = d["cells"][0]
        self.assertEqual(c["key"], "14/4706/6244")
        self.assertEqual(c["layers"]["roads"]["features"], 6)
        self.assertIn("residential 1", c["layers"]["roads"]["class"])
        self.assertEqual(c["layers"]["buildings"]["height_pct"], round(100 / 7, 1))
        self.assertEqual(c["digest"], digest)
        from PIL import Image
        with Image.open(png) as im:
            self.assertEqual(im.size, (2 * 1024 + 8, 1024))

    def test_offline_uncached_fails_cleanly(self):
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = mapdata.main(["--key", "14/1/1", "--layers", "roads", "--cache-dir", os.path.join(self.tmp, "e"),
                               "--offline", "--dump"])
        self.assertEqual(rc, 1)
        self.assertIn("mapdata:", err.getvalue())


if __name__ == "__main__":
    unittest.main()
