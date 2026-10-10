"""Mesh near-visual mode (docs/V1_3_SPEC.md §B) and the textured tile shapes (§A):
  - selection down to L-2 with the min-level radii and no block forcing: no gaps, no overlaps;
  - hysteresis band >= 25 % of every switch distance;
  - L-2 vertices are exactly the terrain PNG's heights (same lattice, same quantum), also as
    the engine computes them in float32; L-1 vertices are every 2nd sample;
  - the triangle diagonals are the Torque TerrainBlock's, checked against an independent port of
    TerrainBlock::getHeight + TerrainFile::_buildGridMap's level-0 split rule;
  - textured shapes: UV0 = footprint, UV1 = world-locked detail coordinates (continuous across
    tiles), in the engine's convention (verified against a stock two-UV-set .cdae if the game is
    installed).
"""
import math
import os
import re
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cdae  # noqa: E402
import farfield as ff  # noqa: E402
import geodesy as g  # noqa: E402
import heightmap  # noqa: E402

LAT0, LON0 = 36.5785, -118.2923
GAME = Path.home() / ".local/share/Steam/steamapps/common/BeamNG.drive/content/art_shapes.zip"


class RoughDem:
    """Rough synthetic terrain with detail below 2 m; zoom independent."""

    def sample(self, lat, lon, zoom, progress=None):
        lat, lon = np.asarray(lat), np.asarray(lon)
        return (1500 + 300 * np.sin(lat * 90) * np.cos(lon * 70)
                + 4.0 * np.sin(lat * 61000) * np.cos(lon * 47000) + 2.0 * np.sin(lon * 151000))


def torque_height(h, x, y):
    """Torque3D TerrainBlock::getHeight (Engine/source/terrain/terrData.cpp) on a block-local
    height grid h[gy, gx] at square coordinates (x, y), with the level-0 split rule of
    TerrainFile::_buildGridMap (terrFile.cpp): Split45 iff ((squareX ^ squareY) & 1) == 0.
    Written from the C++ independently of farfield.surface_at."""
    xi, yi = int(math.floor(x)), int(math.floor(y))
    xp, yp = x - xi, y - yi
    zbl, zbr = h[yi, xi], h[yi, xi + 1]
    ztl, ztr = h[yi + 1, xi], h[yi + 1, xi + 1]
    if ((xi ^ yi) & 1) == 0:
        if xp > yp:     # bottom half
            return zbl + xp * (zbr - zbl) + yp * (ztr - zbr)
        return zbl + xp * (ztr - ztl) + yp * (ztl - zbl)
    if 1.0 - xp > yp:   # bottom half
        return zbr + (1.0 - xp) * (zbl - zbr) + yp * (ztl - zbl)
    return zbr + (1.0 - xp) * (ztl - ztr) + yp * (ztr - zbr)


def coverage(tiles, px, py, radius, cell=64.0):
    """Count how often each `cell` square of the radius square is covered."""
    gx0, gy0 = math.floor((px - radius) / cell), math.floor((py - radius) / cell)
    nx, ny = math.ceil((px + radius) / cell) - gx0, math.ceil((py + radius) / cell) - gy0
    cnt = np.zeros((ny, nx), np.int16)
    for t in tiles.values():
        x0, y0, x1, y1 = t.bbox
        i0, i1 = max(0, int(x0 // cell) - gx0), min(nx, int(x1 // cell) - gx0)
        j0, j1 = max(0, int(y0 // cell) - gy0), min(ny, int(y1 // cell) - gy0)
        cnt[j0:j1, i0:i1] += 1
    return cnt


def mesh_select(px, py, prev=frozenset(), **kw):
    return ff.select(px, py, [], prev, min_level=ff.MESH_MIN_LEVEL, **kw)


class MeshSelectionTest(unittest.TestCase):
    def test_tiling_no_gaps_or_overlaps(self):
        for px, py, r in ((0.0, 0.0, ff.RADIUS), (1000.5, -333.3, 20000.0), (-12345.0, 6789.0, 20000.0),
                          (31.9, 32.1, 5000.0)):
            with self.subTest(px=px, py=py):
                tiles, refined = mesh_select(px, py, radius=r)
                cnt = coverage(tiles, px, py, r)
                self.assertTrue((cnt == 1).all(), f"{int((cnt == 0).sum())} holes, {int((cnt > 1).sum())} overlaps")
                for t in tiles.values():
                    self.assertTrue(ff.MESH_MIN_LEVEL <= t.level <= ff.MAX_LEVEL)
                    self.assertEqual(t.key, f"{t.level}_{t.i}_{t.j}")
                    self.assertEqual(ff.tile_of(t.key), t)
                    self.assertEqual(t.bbox[0] % t.size, 0)
                    d = t.distance(px, py)
                    # min-level radii (V1_3_SPEC §B): L-2 within 150 m, at most L-1 within 400 m
                    if d < ff.NEAR_RADII[-2]:
                        self.assertEqual(t.level, -2, t)
                    if d < ff.NEAR_RADII[-1]:
                        self.assertLessEqual(t.level, -1, t)
                    # a leaf is never closer than its own switch distance (no hysteresis here)
                    self.assertGreaterEqual(d, ff.switch_distance(t.level, min_level=ff.MESH_MIN_LEVEL), t)
                    if t.level < ff.MAX_LEVEL:
                        self.assertIn((t.level + 1, t.i // 2, t.j // 2), refined)

    def test_counts(self):
        tiles, _ = mesh_select(0.0, 0.0)
        by = {}
        for t in tiles.values():
            by[t.level] = by.get(t.level, 0) + 1
        print(f"\n  mesh initial set at (0, 0): {len(tiles)} tiles, by level {dict(sorted(by.items()))}", file=sys.stderr)
        self.assertTrue(30 <= by[-2] <= 50, by)
        self.assertTrue(30 <= by[-1] <= 60, by)
        self.assertLess(len(tiles), 900)
        # the terrain mode's L0 floor is unchanged
        t0, _ = ff.select(0.0, 0.0, [ff.Block(0.0, 0.0).bbox])
        self.assertEqual(min(t.level for t in t0.values()), 0)

    def test_terrain_mode_ignores_radii(self):
        self.assertEqual(ff.switch_distance(0), 0.0)
        self.assertEqual(ff.switch_distance(1), 3 * 512)
        self.assertEqual(ff.switch_distance(0, min_level=-2), 400.0)
        self.assertEqual(ff.switch_distance(-1, min_level=-2), 150.0)
        self.assertEqual(ff.switch_distance(-2, min_level=-2), 0.0)
        self.assertEqual(ff.switch_distance(1, min_level=-2), 3 * 512)
        self.assertEqual(ff.switch_distance(0, radii={-2: 100.0, -1: 300.0}, min_level=-2), 300.0)

    def test_hysteresis_near_levels(self):
        """An oscillation of +-20 m around the 150 m (L-1 -> L-2) and the 400 m (L0 -> L-1)
        thresholds changes nothing after the first refinement; past (1 + h) x the distance the
        node coarsens."""
        for node, thr in (((-1, 3, 0), 150.0), ((0, 2, 0), 400.0)):
            with self.subTest(node=node):
                x0 = ff.level_size(node[0]) * node[1]       # west edge of the node; player west of it
                _, prev = mesh_select(x0 - thr - 20.0, 30.0)
                self.assertNotIn(node, prev)
                sets = []
                for n, dx in enumerate([20.0, -20.0] * 5):
                    tiles, prev = mesh_select(x0 - thr + dx, 30.0, prev)
                    sets.append(set(tiles))
                    self.assertIn(node, prev)
                self.assertTrue(all(s == sets[0] for s in sets[1:]))
                _, prev = mesh_select(x0 - thr * (1 + ff.HYSTERESIS) + 1.0, 30.0, prev)
                self.assertIn(node, prev)
                _, prev = mesh_select(x0 - thr * (1 + ff.HYSTERESIS) - 1.0, 30.0, prev)
                self.assertNotIn(node, prev)

    def test_swap_groups_with_near_levels(self):
        a, ra = mesh_select(0.0, 0.0)
        b, _ = mesh_select(100.0, 30.0, ra)
        groups = ff.swap_groups(set(a), b)
        self.assertTrue(groups)
        cur = set(a)
        for adds, removes in groups:
            self.assertTrue(adds and removes)
            ca = coverage({k: ff.tile_of(k) for k in adds}, 0, 0, 2000)
            cr = coverage({k: ff.tile_of(k) for k in removes}, 0, 0, 2000)
            np.testing.assert_array_equal(ca, cr)
            cur = (cur - set(removes)) | set(adds)
        self.assertEqual(cur, set(b))


class LatticeTest(unittest.TestCase):
    """L-2 tiles are the TerrainBlock's surface (§B)."""

    @classmethod
    def setUpClass(cls):
        cls.frame = g.EnuFrame(LAT0, LON0)
        cls.dem = RoughDem()
        cls.q = 1 / 64
        cls.tmp = tempfile.TemporaryDirectory()
        cls.cx, cls.cy = 256.0, -512.0
        path = os.path.join(cls.tmp.name, "hm.png")
        info = heightmap.generate(cls.frame, lambda la, lo: cls.dem.sample(la, lo, 15), path, cls.cx, cls.cy, q=cls.q)
        with Image.open(path) as im:
            cls.q16 = np.asarray(im).astype(np.int64)[::-1]           # [gy, gx], gy = north
        cls.info = info
        cls.h = heightmap.heights(cls.q16, info["zBase"], info["maxHeight"])
        cls.x0, cls.y0 = cls.cx - heightmap.HALF, cls.cy - heightmap.HALF

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def build(self, tile):
        return ff.build_tile(self.frame, self.dem, tile, 15, self.q, with_sink=False)

    def test_block_lattice_parity(self):
        """Every block starts at an even lattice index, so block-local square parity is the
        world lattice parity (torque_split45's premise)."""
        for c in (0.0, 256.0, -256.0, 768.0, -9984.0, 24832.0):
            self.assertEqual(heightmap.lattice_index(c) % 2, 0, c)
        for lvl in (-2, -1, 0):
            self.assertEqual(ff.level_size(lvl) / heightmap.SQUARE % 2, 0)

    def test_l2_vertices_equal_terrain_png(self):
        for tile in (ff.Tile(-2, 3, -5), ff.Tile(-2, -10, -23), ff.Tile(-2, 18, 3)):
            with self.subTest(tile=tile.key):
                b = self.build(tile)
                gx0 = int((tile.bbox[0] - self.x0) / heightmap.SQUARE)
                gy0 = int((tile.bbox[1] - self.y0) / heightmap.SQUARE)
                want = self.h[gy0:gy0 + 33, gx0:gx0 + 33]
                rel = b.positions[:33 * 33, 2].reshape(33, 33)
                got = rel.astype(np.float32).astype(np.float64) + b.pos[2]      # as written to the .cdae
                self.assertTrue((got == want).all(), np.abs(got - want).max())
                self.assertEqual(b.pos[2], round(b.pos[2]))                     # integral origin
                # as the engine computes both in float32: zBase + q16 * (maxHeight / 65536) for the
                # terrain, object position + vertex for the TSStatic
                f32 = np.float32
                eng = f32(self.info["zBase"]) + self.q16[gy0:gy0 + 33, gx0:gx0 + 33].astype(f32) * f32(
                    self.info["maxHeight"] / 65536)
                mesh = f32(b.pos[2]) + rel.astype(f32)
                self.assertTrue((eng == mesh).all())
                # and the x, y of the vertices are the lattice points
                xy = b.positions[:33 * 33, :2] + b.pos[:2]
                self.assertTrue((xy[:, 0] % 2 == 0).all() and (xy[:, 1] % 2 == 0).all())

    def test_l1_vertices_are_every_second_sample(self):
        tile = ff.Tile(-1, 1, -3)
        b = self.build(tile)
        gx0 = int((tile.bbox[0] - self.x0) / 2)
        gy0 = int((tile.bbox[1] - self.y0) / 2)
        want = self.h[gy0:gy0 + 65:2, gx0:gx0 + 65:2]
        got = b.positions[:33 * 33, 2].reshape(33, 33).astype(np.float32).astype(np.float64) + b.pos[2]
        self.assertTrue((got == want).all())

    def test_diagonals_match_torque(self):
        """The L-2 surface equals Torque's TerrainBlock::getHeight everywhere (not only at the
        vertices), and every cell of the built mesh uses the TerrainBlock's diagonal."""
        tile = ff.Tile(-2, 3, -5)
        b = self.build(tile)
        grid = b.grid.astype(np.float64)
        rng = np.random.default_rng(3)
        pts = rng.random((4000, 2)) * 64.0
        pts[:20] = [[2 * k + 0.5, 2 * k + 1.5] for k in range(20)]        # upper triangles near the diagonal
        ours = ff.surface_at(grid, 2.0, pts[:, 0], pts[:, 1])
        bx = (tile.bbox[0] - self.x0 + pts[:, 0]) / heightmap.SQUARE
        by = (tile.bbox[1] - self.y0 + pts[:, 1]) / heightmap.SQUARE
        theirs = np.array([torque_height(self.h, x, y) for x, y in zip(bx, by)])
        np.testing.assert_allclose(ours, theirs, atol=1e-9)
        # v1.2's uniform SE-NW split would differ on this rough ground (the test has teeth).
        i, j = np.minimum((pts // 2.0).astype(int), 31).T
        a, c = pts[:, 0] / 2.0 - i, pts[:, 1] / 2.0 - j
        z00, z10, z01, z11 = grid[j, i], grid[j, i + 1], grid[j + 1, i], grid[j + 1, i + 1]
        uniform = np.where(a + c <= 1, z00 + a * (z10 - z00) + c * (z01 - z00),
                           z11 + (1 - a) * (z01 - z11) + (1 - c) * (z10 - z11))
        self.assertGreater(np.abs(uniform - theirs).max(), 0.05)
        # Mesh triangles: cell (i, j) is triangles 2k, 2k+1 (k = j * 32 + i); the shared edge is
        # its diagonal.
        tris = b.tris[:2 * 32 * 32].reshape(32 * 32, 2, 3)
        gx0 = int((tile.bbox[0] - self.x0) / 2)
        gy0 = int((tile.bbox[1] - self.y0) / 2)
        for k, (t1, t2) in enumerate(tris):
            j, i = divmod(k, 32)
            shared = sorted(set(t1.tolist()) & set(t2.tolist()))
            a = j * 33 + i
            sw_ne = shared == sorted([a, a + 34])
            se_nw = shared == sorted([a + 1, a + 33])
            self.assertTrue(sw_ne or se_nw)
            self.assertEqual(sw_ne, bool(ff.torque_split45(gx0 + i, gy0 + j)), (i, j))
        # winding: every grid triangle faces up
        P = b.positions
        main = b.tris[:2048]
        n = np.cross(P[main[:, 1]] - P[main[:, 0]], P[main[:, 2]] - P[main[:, 0]])
        self.assertTrue((n[:, 2] > 0).all())

    def test_err_and_no_sink(self):
        b = self.build(ff.Tile(-2, 3, -5))
        self.assertIsNone(b.sink)
        self.assertGreater(b.err, 0.0)          # 1 m reference: the DEM detail between lattice points
        self.assertLess(b.err, 8.0)
        self.assertEqual(b.skirt, ff.skirt_depth(64.0, b.err))
        b0 = ff.build_tile(self.frame, self.dem, ff.Tile(0, 0, -2), 15, self.q, with_sink=True)
        self.assertIsNotNone(b0.sink)


class TexturedShapeTest(unittest.TestCase):
    def test_uv0_footprint_uv1_detail(self):
        frame = g.EnuFrame(LAT0, LON0)
        with tempfile.TemporaryDirectory() as d:
            uv1 = {}
            for tile in (ff.Tile(-2, 0, 0), ff.Tile(-2, 1, 0), ff.Tile(3, -1, 2)):
                b = ff.build_tile(frame, RoughDem(), tile, 15 if tile.level <= 0 else 10,
                                  1 / 64 if tile.level <= 0 else None, with_sink=False)
                path = os.path.join(d, f"{tile.key}.cdae")
                ff.write_built(path, b, "cdae", material="bngEarthSat_1_m2_0_0", textured=True)
                shape = cdae.read(path)
                self.assertEqual(shape["materials"][0]["name"], "bngEarthSat_1_m2_0_0")
                m = cdae.mesh_arrays(shape["meshes"][0])
                V = m["verts"].astype(np.float64)
                local = V[:, :2]                # verts are relative to the tile origin (SW corner)
                S = tile.size
                # UV0 = footprint (engine stores v flipped), UV1 = metres / DETAIL_M (flipped too)
                np.testing.assert_allclose(m["tverts"][:, 0], local[:, 0] / S, atol=1e-6)
                np.testing.assert_allclose(m["tverts"][:, 1], 1 - local[:, 1] / S, atol=1e-6)
                np.testing.assert_allclose(m["tverts2"][:, 0], local[:, 0] / ff.DETAIL_M, atol=1e-4)
                np.testing.assert_allclose(m["tverts2"][:, 1], 1 - local[:, 1] / ff.DETAIL_M, atol=1e-4)
                self.assertTrue((m["tverts"] >= -1e-6).all() and (m["tverts"] <= 1 + 1e-6).all())
                uv1[tile.key] = (V[:, :2] + b.pos[:2], m["tverts2"].astype(np.float64))
            # world-locked: on the shared edge x = 64 the two L-2 tiles' UV1 differ by whole repeats
            (pa, ua), (pb, ub) = uv1["-2_0_0"], uv1["-2_1_0"]
            ea = {round(p[1], 3): u for p, u in zip(pa, ua) if abs(p[0] - 64.0) < 1e-6}
            eb = {round(p[1], 3): u for p, u in zip(pb, ub) if abs(p[0] - 64.0) < 1e-6}
            self.assertTrue(ea and ea.keys() == eb.keys())
            for y in ea:
                diff = ea[y] - eb[y]
                np.testing.assert_allclose(diff, np.round(diff), atol=1e-4)

    def test_palette_tiles_have_no_uv1(self):
        frame = g.EnuFrame(LAT0, LON0)
        b = ff.build_tile(frame, RoughDem(), ff.Tile(2, 0, 0), 12)
        with tempfile.TemporaryDirectory() as d:
            ff.write_built(os.path.join(d, "t.cdae"), b)
            m = cdae.read(os.path.join(d, "t.cdae"))["meshes"][0]
            self.assertEqual(m["tverts2"][0], 0)

    @unittest.skipUnless(GAME.is_file(), "BeamNG.drive not installed")
    def test_uv2_convention_matches_engine(self):
        """A stock shape compiled by the engine from a Collada file with two UV sets stores
        tverts2 = (u1, 1 - v1), exactly as tverts = (u0, 1 - v0), which is what cdae.encode
        writes for uvs2 (read-only check against the game's art_shapes.zip)."""
        base = "art/shapes/common/building_architect_modules/string_course/G_C_SC_01_300x20"
        with zipfile.ZipFile(GAME) as z:
            txt = z.read(base + ".dae").decode()
            shape = cdae.decode(z.read(base + ".cdae"))

        def arr(key):
            return np.array(re.search(rf'id="[^"]*{key}-array" count="\d+">([^<]*)<', txt).group(1).split(), float)
        pos, uv0, uv1 = arr("mesh-positions").reshape(-1, 3), arr("mesh-map-0").reshape(-1, 2), arr("mesh-map-1").reshape(-1, 2)
        p = np.array(re.search(r"<p>([^<]*)</p>", txt).group(1).split(), int).reshape(-1, 4)
        m = cdae.mesh_arrays(shape["meshes"][0])
        for row in p:
            k = np.where((np.abs(m["verts"] - pos[row[0]]).max(1) < 1e-4)
                         & (np.abs(m["tverts"] - [uv0[row[2], 0], 1 - uv0[row[2], 1]]).max(1) < 1e-4))[0]
            self.assertTrue(len(k))
            self.assertLess(np.abs(m["tverts2"][k] - [uv1[row[2], 0], 1 - uv1[row[2], 1]]).max(1).min(), 1e-6)
        self.assertEqual(shape["meshes"][0]["flags"], 0)
        # ours, for comparison
        ours = cdae.mesh_arrays(cdae.decode(cdae.encode(pos[:3], [[0, 1, 2]], uvs=uv0[:3], uvs2=uv1[:3]))["meshes"][0])
        for i in range(3):
            j = np.where(np.abs(ours["verts"] - pos[i]).max(1) < 1e-6)[0][0]
            np.testing.assert_allclose(ours["tverts2"][j], [uv1[i, 0], 1 - uv1[i, 1]], atol=1e-6)


if __name__ == "__main__":
    unittest.main()
