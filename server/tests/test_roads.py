"""Roads near the player (roads.py) and the road surfaces (surface.paint_roads): the class / surface rules,
the polyline helpers, the tile build (clipped, simplified, chunked, deterministic), the Tracker's
send / drop / anchor-change logic, and roads painted into the material grid."""
import asyncio
import json
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import geodesy  # noqa: E402
import mapgeom  # noqa: E402
import roads  # noqa: E402
import surface as sf  # noqa: E402

LAT, LON = 39.2834, -76.5934
FRAME = geodesy.EnuFrame(LAT, LON, 0.0)
M_LAT = 111320.0
M_LON = 111320.0 * np.cos(np.radians(LAT))


def rules(entries, kind="text"):
    """RuleTable from [(feature, value), ...] covering whole features."""
    if not entries:
        return mapgeom.RuleTable(np.zeros(0, np.int32), np.zeros(0, "<U8"), np.zeros(0, np.float32), np.zeros(0, np.float32))
    f = np.array([e[0] for e in entries], np.int32)
    v = np.array([e[1] for e in entries], np.float32 if kind == "float" else "<U16")
    return mapgeom.RuleTable(f, v, np.zeros(len(f), np.float32), np.ones(len(f), np.float32))


def features(lines, classes, surfaces=(), widths=(), levels=(), flags=(), ids=None, subtypes=None):
    """Features of LINE roads; lines: list of [(east m, north m), ...] around (LAT, LON)."""
    coords, part_off, ring_off, coord_off = [], [0], [0], [0]
    for ln in lines:
        for e, n in ln:
            coords.append((LON + e / M_LON, LAT + n / M_LAT))
        coord_off.append(len(coords))
        ring_off.append(len(coord_off) - 1)
        part_off.append(len(ring_off) - 1)
    n = len(lines)
    return mapgeom.Features(
        layer="roads", ids=np.array(ids or [f"id{i:03d}" for i in range(n)]), kind=np.full(n, mapgeom.LINE, np.uint8),
        part_off=np.array(part_off, np.int64), ring_off=np.array(ring_off, np.int64),
        coord_off=np.array(coord_off, np.int64), lonlat=np.array(coords, np.float64),
        attrs={"class": np.array(classes), "subtype": np.array(subtypes or ["road"] * n)},
        rules={"surface": rules(list(surfaces)), "width": rules(list(widths), "float"),
               "level": rules(list(levels), "float"), "flags": rules(list(flags))})


def flat(lat, lon):
    return np.full(np.shape(lat), 10.0)


class RulesTest(unittest.TestCase):
    def test_surface_kind(self):
        self.assertEqual(roads.surface_kind("residential", None), "paved")
        self.assertEqual(roads.surface_kind("track", None), "dirt")
        self.assertEqual(roads.surface_kind("track", "asphalt"), "paved")
        self.assertEqual(roads.surface_kind("primary", "gravel"), "gravel")
        self.assertEqual(roads.surface_kind("primary", "unpaved"), "dirt")

    def test_specs_width_and_left_out(self):
        f = features([[(0, 0), (10, 0)]] * 6, ["residential", "footway", "primary", "tertiary", "track", "secondary"],
                     surfaces=[(4, "gravel")], widths=[(2, 14.0)], levels=[(3, 1.0)], flags=[(5, "is_bridge")])
        s = roads.road_specs(f)
        self.assertEqual(s[0], (roads.STYLE["residential"][0] + 2 * roads.EDGE_STRIP_M, "paved"), "rural: edge strips")
        self.assertIsNone(s[1], "footways left out")
        self.assertEqual(s[2], (14.0, "paved"), "width rule")
        self.assertIsNone(s[3], "off ground level (bridge / tunnel) left out")
        self.assertEqual(s[4], (roads.STYLE["track"][0], "gravel"))
        self.assertIsNone(s[5], "bridge flag left out")

    def test_carriageway_parking_and_strips(self):
        res = roads.STYLE["residential"][0]
        self.assertEqual(roads.carriageway("residential", "paved", True), res + 2 * roads.PARKING_M, "city street: parking")
        self.assertEqual(roads.carriageway("residential", "paved", False), res + 2 * roads.EDGE_STRIP_M, "rural: strips")
        self.assertEqual(roads.carriageway("motorway", "paved", True), roads.STYLE["motorway"][0], "no parking on motorways")
        self.assertEqual(roads.carriageway("service", "paved", False), roads.STYLE["service"][0], "service: as is")
        self.assertEqual(roads.carriageway("track", "dirt", True), roads.STYLE["track"][0], "dirt: as is")
        self.assertEqual(roads.carriageway("residential", "paved", True, 7.5), 7.5, "a width rule is the carriageway")

    def test_urban_from_land_cover(self):
        f = features([[(0, 0), (10, 0)], [(0, 100), (10, 100)]], ["residential", "residential"], widths=[(1, 9.0)])
        s = roads.road_specs(f, lambda lat, lon: np.ones(np.shape(lat), bool))
        self.assertEqual(s[0][0], roads.STYLE["residential"][0] + 2 * roads.PARKING_M)
        self.assertEqual(s[1][0], 9.0, "width rule wins over the parking guess")


class GeometryTest(unittest.TestCase):
    def test_clip(self):
        pts = np.array([[-10.0, 5.0], [20.0, 5.0], [20.0, 30.0], [5.0, 30.0], [5.0, 5.0]])
        pieces = roads.clip_polyline(pts, 0, 0, 10, 10)
        self.assertEqual(len(pieces), 2)
        self.assertTrue(np.allclose(pieces[0], [[0, 5], [10, 5]]))
        self.assertTrue(np.allclose(pieces[1], [[5, 10], [5, 5]]))
        self.assertEqual(roads.clip_polyline(np.array([[20.0, 20], [30, 30]]), 0, 0, 10, 10), [])

    def test_simplify_densify_split(self):
        pts = np.array([[0.0, 0, 0], [10, 0.1, 0], [20, 0, 0], [30, 10, 0]])
        s = roads.simplify(pts)
        self.assertEqual(len(s), 3, "the 0.1 m bump goes, the corner stays")
        d = roads.densify(np.array([[0.0, 0, 0], [100, 0, 0]]))
        self.assertTrue((np.diff(d[:, 0]) <= roads.MAX_SPACING + 1e-9).all() and d[-1, 0] == 100)
        chunks = roads.split(np.arange(250 * 3, dtype=float).reshape(250, 3))
        self.assertTrue(all(len(c) <= roads.MAX_NODES for c in chunks))
        self.assertTrue(all(np.array_equal(a[-1], b[0]) for a, b in zip(chunks[:-1], chunks[1:])), "chunks share their ends")
        self.assertEqual(sum(len(c) for c in chunks) - (len(chunks) - 1), 250)

    def test_tiles_near(self):
        ks = roads.tiles_near(0, 0, 300)
        self.assertIn((0, 0), ks)
        self.assertIn((-2, -2), ks)
        self.assertNotIn((2, 0), ks)
        self.assertEqual(roads.tile_distance(0, 0, 10, 10), 0.0)
        self.assertEqual(roads.tile_distance(1, 0, 10, 10), roads.TILE - 10)


class BuildTileTest(unittest.TestCase):
    def test_build_clips_and_styles(self):
        f = features([[(-300, 50), (300, 50)], [(100, 10), (100, 200)]], ["primary", "track"])
        chunks = roads.build_tile(FRAME, 0, 0, [f], flat)
        self.assertEqual(len(chunks), 2)
        prim = next(c for c in chunks if c["w"] == roads.STYLE["primary"][0] + 2 * roads.EDGE_STRIP_M)
        xs = [n[0] for n in prim["n"]]
        self.assertAlmostEqual(min(xs), -roads.OVERLAP, delta=0.05)
        self.assertAlmostEqual(max(xs), roads.TILE + roads.OVERLAP, delta=0.05)
        self.assertTrue(all(abs(n[1] - 50) < 0.3 for n in prim["n"]), "on its line (the test helper is spherical: ~0.13 m)")
        self.assertEqual(prim["m"], roads.LOOK["paved"]["m"])
        track = next(c for c in chunks if c is not prim)
        self.assertEqual(track["m"], roads.LOOK["dirt"]["m"])
        self.assertTrue(all(abs(n[2] - 10.0) < 0.05 for n in prim["n"]), "z on the DEM (flat, near the anchor)")

    def test_feature_in_two_cells_once_and_deterministic(self):
        f = features([[(-300, 50), (300, 50)]], ["primary"])
        a = roads.build_tile(FRAME, 0, 0, [f, f], flat)
        b = roads.build_tile(FRAME, 0, 0, [f], flat)
        self.assertEqual(a, b)

    def test_write_tile(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, n = roads.write_tile(tmp, "/temp/beamngEarth/s", 3, -1, 2, [{"m": "x", "w": 1, "tl": 1, "p": 1, "n": [[0, 0, 0], [1, 0, 0]]}])
            self.assertEqual((path, n), ("/temp/beamngEarth/s/roads_3_-1_2.json", 1))
            with open(os.path.join(tmp, "roads_3_-1_2.json")) as fh:
                self.assertEqual(json.load(fh)["chunks"][0]["m"], "x")


class FakeConn:
    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)


class TrackerTest(unittest.TestCase):
    def run_poses(self, poses):
        conn = FakeConn()
        tr = roads.Tracker()
        built = []

        def build(aid, tx, ty):
            built.append((aid, tx, ty))
            return (f"/p/{aid}_{tx}_{ty}", 1)

        async def go():
            loop = asyncio.get_running_loop()
            for aid, x, y in poses:
                for _ in range(40):             # several poses at one place: every tile gets built
                    tr.on_pose(conn, aid, x, y, loop, build)
                    await asyncio.sleep(0.01)
        asyncio.run(go())
        return conn, tr, built

    def test_sends_near_tiles_once(self):
        conn, tr, built = self.run_poses([(1, 0.0, 0.0)])
        keys = {(m["anchorId"], m["key"]) for m in conn.sent if m["type"] == "roads_tile"}
        want = {(1, f"{tx}_{ty}") for tx, ty in roads.tiles_near(0, 0, roads.SEND_M)}
        self.assertEqual(keys, want)
        self.assertEqual(len(built), len(want), "each tile built once")

    def test_drops_far_and_resends_on_anchor_change(self):
        conn, tr, built = self.run_poses([(1, 0.0, 0.0), (1, 3000.0, 0.0), (2, 3000.0, 0.0)])
        drops = [m for m in conn.sent if m["type"] == "roads_drop"]
        self.assertTrue(drops and all(m["anchorId"] == 1 for m in drops))
        self.assertTrue(all(roads.tile_distance(*map(int, m["key"].split("_")), 3000.0, 0.0) > roads.DROP_M for m in drops))
        a2 = {m["key"] for m in conn.sent if m["type"] == "roads_tile" and m["anchorId"] == 2}
        self.assertEqual(a2, {f"{tx}_{ty}" for tx, ty in roads.tiles_near(3000.0, 0.0, roads.SEND_M)})


class PaintRoadsTest(unittest.TestCase):
    def grid(self, n=128):
        rows = np.arange(n)[:, None]
        cols = np.arange(n)[None, :]
        lat = LAT + (n / 2 - rows) * 2 / M_LAT + np.zeros((1, n))
        lon = LON + (cols - n / 2) * 2 / M_LON + np.zeros((n, 1))
        return lat, lon

    def test_affine(self):
        lat, lon = self.grid()
        f = sf.grid_affine(lat, lon)
        c, r = f(lon[10, 20], lat[10, 20])
        self.assertAlmostEqual(float(c), 20, places=3)
        self.assertAlmostEqual(float(r), 10, places=3)

    def test_paved_and_dirt_painted_at_width(self):
        lat, lon = self.grid()
        f = features([[(-200, 0), (200, 0)], [(20, -200), (20, 200)]], ["residential", "track"])
        idx = np.full(lat.shape, sf.GRASS, np.uint8)
        out = sf.paint_roads(idx, lat, lon, [f], 2.0)
        self.assertEqual(int(out[64, 10]), sf.ASPHALT, "on the east-west street")
        self.assertEqual(int(out[64 - 6, 10]), sf.GRASS, "12 m north of it: off the 8 m street (6 m + strips)")
        self.assertEqual(int(out[30, 74]), sf.DIRT, "on the track (20 m east)")
        self.assertEqual(int(out[64, 74]), sf.ASPHALT, "paved wins at the crossing")
        self.assertEqual(int(idx[64, 10]), sf.GRASS, "input untouched")


if __name__ == "__main__":
    unittest.main()
