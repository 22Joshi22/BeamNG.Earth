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
    """RuleTable from [(feature, value[, start, end]), ...] (default: covering whole features)."""
    if not entries:
        return mapgeom.RuleTable(np.zeros(0, np.int32), np.zeros(0, "<U8"), np.zeros(0, np.float32), np.zeros(0, np.float32))
    f = np.array([e[0] for e in entries], np.int32)
    v = np.array([e[1] for e in entries], np.float32 if kind == "float" else "<U16")
    a = np.array([e[2] if len(e) > 2 else 0.0 for e in entries], np.float32)
    b = np.array([e[3] if len(e) > 3 else 1.0 for e in entries], np.float32)
    return mapgeom.RuleTable(f, v, a, b)


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
        self.assertEqual(s[0][:2], (roads.STYLE["residential"][0] + 2 * roads.EDGE_STRIP_M, "paved"), "rural: edge strips")
        self.assertIsNone(s[1], "footways left out")
        self.assertEqual(s[2][:2], (14.0, "paved"), "width rule")
        self.assertIsNone(s[3], "off ground level (bridge / tunnel) left out")
        self.assertEqual(s[4][:2], (roads.STYLE["track"][0], "gravel"))
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
        chunks = [c for c in roads.build_tile(FRAME, 0, 0, [f], flat) if c["m"] in {l["m"] for l in roads.LOOK.values()}]
        self.assertEqual(len(chunks), 2, "one surface chunk per road (lines and sidewalks aside)")
        prim = next(c for c in chunks if c["w"] == roads.STYLE["primary"][0] + 2 * roads.EDGE_STRIP_M)
        xs = [n[0] for n in prim["n"]]
        self.assertAlmostEqual(min(xs), -roads.OVERLAP, delta=0.05)
        self.assertAlmostEqual(max(xs), roads.TILE + roads.OVERLAP, delta=0.05)
        self.assertTrue(all(abs(n[1] - 50) < 0.3 for n in prim["n"]), "on its line (the test helper is spherical: ~0.13 m)")
        self.assertEqual(prim["m"], roads.LOOK["paved"]["m"])
        track = next(c for c in chunks if c is not prim)
        self.assertEqual(track["m"], roads.LOOK["dirt"]["m"])
        self.assertTrue(all(abs(n[2] - 10.0) < 0.05 for n in prim["n"]), "z on the DEM (flat, near the anchor)")

    def test_ramps_one_lane_wide(self):
        f = features([[(0, 0), (100, 0)]] * 3, ["motorway", "motorway", "primary"])
        f.rules["subclass"] = rules([(0, "link"), (2, "link")])
        f.rules["oneway"] = rules([(0, 1.0), (1, 1.0)], "float")
        s = roads.road_specs(f)
        self.assertEqual(s[0].width, roads.LINK_M[1], "a one-way motorway ramp")
        self.assertGreater(s[1].width, 10.0, "the motorway itself")
        self.assertEqual(s[2].width, roads.LINK_M[2], "a two-way slip road")
        self.assertEqual(roads.bridge_specs(features([[(0, 0), (100, 0)]], ["motorway"])), [None])

    def test_divided_trunk_sidewalk_outer_side_only_when_slow(self):
        pts = np.array([(0.0, 0.0, 0.0), (100.0, 0.0, 0.0)])

        def walks(spec, left_hand=False):
            return [line[0][1] for look, w, line in roads.extras(spec, pts, -75.0, left_hand)
                    if look["m"] == roads.SIDEWALK["m"]]
        slow = roads.Spec(7.0, "paved", True, "trunk", True, 2.5, 1, 56.0)
        self.assertEqual(len(walks(slow)), 1)
        self.assertLess(walks(slow)[0], 0, "driving east on the right: the curb is south, the median north")
        self.assertGreater(walks(slow._replace(dir=-1))[0], 0)
        self.assertGreater(walks(slow, left_hand=True)[0], 0)
        self.assertEqual(walks(slow._replace(kmh=100.0)), [], "an expressway carriageway: none")
        self.assertEqual(walks(slow._replace(kmh=None)), [], "unknown limit and junctions: none")
        self.assertEqual(len(walks(slow._replace(kmh=None, gap=50.0))), 1, "no limit, a junction every block")
        self.assertEqual(walks(slow._replace(kmh=None, gap=900.0)), [], "no limit, interchanges far apart")
        f = features([[(0, 0), (300, 0)]], ["trunk"])
        f.rules["connectors"] = rules([(0, "A", 0.0, 0.0), (0, "B", 0.5, 0.5), (0, "C", 1.0, 1.0)])
        self.assertAlmostEqual(roads.road_specs(f)[0].gap, 150.0, delta=1.0)
        self.assertEqual(len(walks(roads.Spec(9.0, "paved", True, "trunk", False))), 2, "a two-way main street")

    def test_city_street_sidewalks_and_lines_stop_at_crossings(self):
        f = features([[(-300, 100), (300, 100)], [(120, -100), (120, 300)]], ["secondary", "residential"])
        chunks = roads.build_tile(FRAME, 0, 0, [f], flat, lambda la, lo: np.ones(np.shape(la), bool))
        by = {}
        for c in chunks:
            by.setdefault(c["m"], []).append(c)
        self.assertIn(roads.SIDEWALK["m"], by, "city streets get sidewalks")
        self.assertIn(roads.GUTTER["m"], by)
        self.assertIn("line_yellow", by, "double yellow centre line (Americas)")
        self.assertNotIn(roads.EDGE_LINE["m"], by, "no rural edge lines in the city")
        sec_w = roads.carriageway("secondary", "paved", True)
        res_w = roads.carriageway("residential", "paved", True)
        for c in by["line_yellow"]:              # the secondary's centre lines stop at the residential street
            for x, y, _ in c["n"]:
                self.assertFalse(abs(x - 120) < res_w / 2, (x, y))
        for c in by[roads.SIDEWALK["m"]]:        # no sidewalk crosses another street's carriageway
            for x, y, _ in c["n"]:
                on_sec = abs(y - 100) < sec_w / 2
                on_res = abs(x - 120) < res_w / 2
                self.assertFalse(on_sec and on_res, (x, y))
        sw = roads.SIDEWALK["w"]               # the corner: the secondary's sidewalk runs on to the
        sec_walk = [c for c in by[roads.SIDEWALK["m"]] if abs(c["n"][0][1] - 100) > 1 and   # residential's curb
                    all(abs(n[1] - c["n"][0][1]) < 0.01 for n in c["n"])]
        res_walk = [c for c in by[roads.SIDEWALK["m"]] if c not in sec_walk]
        xs = sorted(x for c in sec_walk for x, _, _ in (c["n"][0], c["n"][-1]))
        self.assertTrue(any(abs(x - (120 - res_w / 2)) < 0.6 for x in xs), ("to the curb", xs))
        for c in res_walk:                       # ... and the residential's stops at its outer edge
            for x, y, _ in c["n"]:
                self.assertGreater(abs(y - 100), sec_w / 2 + sw - 0.6, (x, y))
        yc = next(c["n"][0][1] for c in by[roads.LOOK["paved"]["m"]] if abs(c["n"][0][1] - 100) < 1)
        ys = sorted({round(c["n"][0][1], 3) for c in by["line_yellow"]})
        self.assertTrue(any(abs(y - (yc + roads.CENTRE["gap"])) < 0.01 for y in ys) and
                        any(abs(y - (yc - roads.CENTRE["gap"])) < 0.01 for y in ys), (yc, ys))

    def test_sidewalk_mesh_runs_and_drains(self):
        import tempfile
        import cdae
        import sidewalks
        f = features([[(-300, 100), (300, 100)], [(120, -100), (120, 300)]], ["secondary", "residential"])
        walks = []
        chunks = roads.build_tile(FRAME, 0, 0, [f], flat, lambda la, lo: np.ones(np.shape(la), bool), walks=walks)
        self.assertFalse(any(c["m"] == roads.SIDEWALK["m"] for c in chunks), "sidewalks are the mesh, not decals")
        self.assertTrue(walks and {w["side"] for w in walks} == {1, -1})
        x0, y0, x1, y1 = roads.tile_bbox(0, 0)
        for w in walks:                              # clipped exactly to the tile; caps only at real ends
            self.assertTrue((w["pts"][:, 0] >= x0 - 1e-6).all() and (w["pts"][:, 0] <= x1 + 1e-6).all())
            for end, cap in ((w["pts"][0], w["caps"][0]), (w["pts"][-1], w["caps"][1])):
                self.assertEqual(cap, not roads._on_box(end, (x0, y0, x1, y1)))
        dr = [c for c in chunks if c["m"] == roads.DRAIN["m"]]
        self.assertTrue(dr, "storm drains at the corners")
        sec_w = roads.carriageway("secondary", "paved", True)
        curbs = [sidewalks.run_geometry(w["pts"], w["side"], roads.SIDEWALK["w"]) for w in walks]
        for c in dr:              # in the gutter: GUTTER w / 2 from a curb, on the road's side of it
            p = np.mean(np.array(c["n"])[:, :2], axis=0)
            best = min(((roads._seg_dist(p[None], cu[:-1, :2], cu[1:, :2]).min(), cu, o) for cu, o in curbs),
                       key=lambda t: t[0])
            d, cu, o = best
            k = int(np.argmin(np.hypot(*(cu[:, :2] - p).T)))
            self.assertAlmostEqual(d, roads.GUTTER["w"] / 2, delta=0.05)
            self.assertLess(float((p - cu[k, :2]) @ o[k]), 0, "on the road side of the curb")
        with tempfile.TemporaryDirectory() as d:
            path, n = roads.write_tile(d, "/vfs", 1, 0, 0, chunks, walks)
            import json
            with open(os.path.join(d, os.path.basename(path))) as fh:
                data = json.load(fh)
            self.assertEqual(data["walk"]["pos"], [x0, y0, 0.0])
            shape = cdae.read(os.path.join(d, os.path.basename(data["walk"]["shape"])))
            self.assertEqual([m["name"] for m in shape["materials"]], list(sidewalks.MATERIALS))

    def test_sidewalk_profile_faces_out(self):
        import sidewalks
        run = np.array([[0.0, 5.0, 10.0], [20.0, 5.0, 10.0], [40.0, 8.0, 10.0]])
        for side in (1, -1):
            curb, out = sidewalks.run_geometry(run, side, 2.5)
            b = sidewalks.Builder((0.0, 0.0, 0.0))
            b.add_run(curb, out, 2.5)
            P, T, UV, M = b.arrays()
            n = np.cross(P[T[:, 1]] - P[T[:, 0]], P[T[:, 2]] - P[T[:, 0]])
            n /= np.linalg.norm(n, axis=1)[:, None]
            top = n[:, 2] > 0.9
            self.assertTrue(top.any())
            self.assertAlmostEqual(P[:, 2].max(), 10.0 + sidewalks.CURB_H + 2.5 * sidewalks.CROSS_SLOPE, places=6)
            # the curb face looks at the road (away from the sidewalk)
            face = (np.abs(n[:, 2]) < 0.3) & (M == 0)
            c = P[T[face]].mean(1)
            near_curb = np.abs(np.hypot(*(c[:, :2] - curb[1, :2]).T)) < 15
            o3 = np.r_[out[1], 0.0]
            looks = n[face][near_curb] @ o3
            self.assertTrue((looks < 0).any() and (looks > 0).any(), "a face toward the road and a back face")

    def test_junction_corners_crosswalks_ramps(self):
        import tempfile
        import junctions
        import sidewalks
        # four residential streets meeting at (120, 120) (Overture: split at the junction)
        X = (120.0, 120.0)
        arms = [[X, (300, 120)], [X, (120, 300)], [X, (-60, 120)], [X, (120, -60)]]
        f = features(arms, ["residential"] * 4)
        walks = []
        chunks = roads.build_tile(FRAME, 0, 0, [f], flat, lambda la, lo: np.ones(np.shape(la), bool), walks=walks)
        corners = [w["corner"] for w in walks if "corner" in w]
        runs = [w for w in walks if "corner" not in w]
        self.assertEqual(len(corners), 4, "a curb return in each quadrant")
        cw = [c for c in chunks if c["m"] == junctions.CROSSWALK["m"]]
        self.assertEqual(len(cw), 4, "a crosswalk on each arm")
        self.assertTrue(sum(len(w["ramps"]) for w in runs) + sum(len(c["ramps"]) for c in corners) >= 8,
                        "two curb ramps per crosswalk")
        # residential streets with parking: curb extensions (bulb-outs) at the corners
        wd = roads.carriageway("residential", "paved", True) / 2 - roads.PARKING_M
        cls_ = [np.array(c["n"])[:, :2] for c in chunks if c["m"] == roads.LOOK["paved"]["m"] and c["n"]]
        for c in corners:
            d = np.min([junctions._seg_dist(c["arc"][:, :2], ln) for ln in cls_], axis=0)
            self.assertGreater(float(d.min()), wd - 0.1, "the curb never on the driving lanes")
            self.assertLess(float(d.min()), wd + 0.3, "the bulb reaches the driving lanes")
            self.assertEqual(len(c["ramps"]), 2, "the crosswalks land on the bulbs")
        for c in chunks:
            if c["m"] == junctions.CROSSWALK["m"]:
                self.assertAlmostEqual(np.hypot(*np.subtract(c["n"][0][:2], c["n"][-1][:2])), 2 * wd, delta=0.3)
        w = roads.carriageway("residential", "paved", True) / 2
        for c in chunks:                        # no markings through the junction or its crosswalks
            if c["m"].startswith("line_") and c["w"] != junctions.STOP_LINE["w"]:
                for x, y, _ in c["n"]:
                    self.assertGreater(max(abs(x - X[0]), abs(y - X[1])), w + junctions.RADII["residential"], (x, y))
        stops = [c for c in chunks if c["m"] == junctions.STOP_LINE["m"] and c["w"] == junctions.STOP_LINE["w"]]
        self.assertEqual(len(stops), 4, "a stop line on each approach")
        for c in stops:                         # on the approach half, beyond the crosswalk
            a, b = np.array(c["n"][0][:2]), np.array(c["n"][-1][:2])
            self.assertLess(np.hypot(*(b - a)), w)
        fills = [c for c in chunks if c.get("poly")]
        self.assertEqual(len(fills), 4, "the corner asphalt")
        for c in corners:                       # the arc keeps RADIUS from its centre, between the curbs
            arc = c["arc"]
            d = np.hypot(arc[:, 0] - X[0], arc[:, 1] - X[1])
            self.assertTrue((d > w - 0.5).all(), d.min())
        near = [r for r in runs if np.hypot(*(r["pts"][0, :2] - X)) < 30 or np.hypot(*(r["pts"][-1, :2] - X)) < 30]
        for r in near:                          # sidewalk ends at a corner: no end cap there
            for end, cap in ((r["pts"][0], r["caps"][0]), (r["pts"][-1], r["caps"][1])):
                if np.hypot(*(end[:2] - X)) < 30:
                    self.assertFalse(cap)
        with tempfile.TemporaryDirectory() as d:
            mesh = __import__("roadmesh").build(FRAME, chunks, flat, (0.0, 0.0, 0.0))
            path, n = roads.write_tile(d, "/vfs", 1, 0, 0, chunks, walks, mesh)
            import cdae
            shape = cdae.read(os.path.join(d, "walks_1_0_0.cdae"))
            self.assertEqual([m["name"] for m in shape["materials"]], list(sidewalks.MATERIALS))

    def test_junction_short_connector_no_corner_on_roads(self):
        import junctions
        # two parallel streets joined by an 8 m connector (two junctions 8 m apart): no curb return
        # may put slab on a carriageway; crosswalks never cross another road
        arms = [[(-200, 100), (0, 100)], [(0, 100), (200, 100)], [(-200, 108), (0, 108)], [(0, 108), (200, 108)],
                [(0, 100), (0, 108)]]
        f = features(arms, ["residential"] * 5)
        walks = []
        chunks = roads.build_tile(FRAME, -1, 0, [f], flat, lambda la, lo: np.ones(np.shape(la), bool), walks=walks)
        w = roads.carriageway("residential", "paved", True) / 2
        lines = [np.array([(-200, 100), (200, 100)]), np.array([(-200, 108), (200, 108)]), np.array([(0, 100), (0, 108)])]
        for c in [wk["corner"] for wk in walks if "corner" in wk]:
            probe = c["arc"][:, :2] + c["inward"] * 1.0
            for ln in lines:
                self.assertGreater(junctions._seg_dist(probe, ln).min(), w - 0.5)
        for c in chunks:
            if c["m"] == junctions.CROSSWALK["m"]:
                a, b = np.array(c["n"][0][:2]), np.array(c["n"][-1][:2])
                for ln in lines:
                    if junctions._seg_dist(np.array([(a + b) / 2]), ln)[0] > 1.0:
                        self.assertFalse(junctions._crosses(a, b, ln))

    def test_cobblestone_strips_no_paint(self):
        self.assertEqual(roads.surface_kind("residential", "paving_stones"), "cobble")
        f = features([[(-300, 50), (300, 50)]], ["tertiary"], surfaces=[(0, "paving_stones")])
        chunks = roads.build_tile(FRAME, 0, 0, [f], flat, lambda la, lo: np.ones(np.shape(la), bool))
        cob = [c for c in chunks if c["m"] == roads.LOOK["cobble"]["m"]]
        w = roads.carriageway("tertiary", "cobble", True)
        n = max(1, int(round(w / roads.LOOK["cobble"]["strip"])))
        self.assertEqual(len(cob), n, "parallel strips across the width")
        self.assertTrue(all(abs(c["w"] - w / n) < 0.01 for c in cob))
        self.assertTrue(any(c["m"] == roads.SIDEWALK["m"] for c in chunks), "cobbled city streets keep sidewalks")
        self.assertFalse(any(c["m"].startswith("line_") for c in chunks), "no lines painted on cobbles")

    def test_partial_bridge_kept_and_decked(self):
        # 400 m street; level 1 on 40-60 % of it (a bridge in the middle): ground parts drawn, the
        # bridge part a deck between the two abutments' heights (the ground slopes 5 % east)
        f = features([[(-200, 50), (200, 50)]], ["secondary"], levels=[(0, 1.0, 0.4, 0.6)])
        (a, b, k), = roads.level_ranges(f, 0)
        self.assertAlmostEqual(a, 0.4, places=5)
        self.assertAlmostEqual(b, 0.6, places=5)
        self.assertEqual(k, "bridge")
        self.assertEqual(len(roads.ground_parts(f, 0)), 2)
        self.assertEqual(len(roads.bridge_parts(f, 0)), 1)
        self.assertIsNotNone(roads.road_specs(f)[0], "a street with a bridge in it stays")
        self.assertIsNotNone(roads.bridge_specs(f)[0])

        def slope(la, lo):
            e, _, _ = FRAME.geodetic_to_enu(np.asarray(la), np.asarray(lo), 0.0)
            return 10.0 + 0.05 * e
        chunks = roads.build_tile(FRAME, -1, 0, [f], slope)
        decks = [c for c in chunks if c.get("deck")]
        self.assertTrue(decks)
        for c in decks:
            for x, y, z in c["n"]:
                self.assertTrue(-41 <= x <= 1, x)              # the bridge range, clipped to the tile
                self.assertAlmostEqual(z, 10.0 + 0.05 * x, delta=0.3)   # straight between abutments
        asphalt = [c for c in chunks if c["m"] == roads.LOOK["paved"]["m"] and not c.get("deck")]
        for c in asphalt:
            for x, _, _ in c["n"]:
                self.assertFalse(-39 < x < -1, x)               # no ground road over the bridge range
        tunnel = features([[(-200, 50), (200, 50)]], ["secondary"], flags=[(0, "is_tunnel", 0.0, 1.0)])
        self.assertIsNone(roads.road_specs(tunnel)[0])
        self.assertIsNone(roads.bridge_specs(tunnel)[0])

    def test_offset_and_colour(self):
        pts = np.array([[0.0, 0, 5], [10, 0, 5], [10, 10, 5]])
        o = roads.offset_polyline(pts, 1.0)
        self.assertTrue(np.allclose(o[0], [0, 1, 5]) and np.allclose(o[-1], [9, 10, 5]))
        self.assertTrue(np.allclose(o[1], [9, 1, 5]), "mitred corner")
        self.assertEqual(roads.centre_colour(-76.6), "line_yellow")
        self.assertEqual(roads.centre_colour(12.5), "line_white")

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


    def test_collider_boxes_deck(self):
        deck = {"deck": True, "w": 8, "n": [[0, 0, 10], [50, 0, 12]]}
        boxes = roads.collider_boxes(None, [deck, {"w": 6, "n": [[0, 0, 0], [5, 0, 0]]}])
        self.assertEqual(list(boxes), ["deck"])
        b = np.array(boxes["deck"])                    # every piece with the point before it: the span covered
        self.assertTrue(b[:, 0].min() < 0 and b[:, 3].max() > 50 and b[:, 1].min() <= -4.5 and b[:, 4].max() >= 4.5)
        self.assertTrue(b[:, 2].min() < 9 and b[:, 5].max() >= 14)
        self.assertEqual(roads.collider_boxes(None, None), {})

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

    def test_cobbles_under_the_tyres(self):
        lat, lon = self.grid()
        f = features([[(-200, 0), (200, 0)]], ["residential"], surfaces=[(0, "paving_stones")])
        out = sf.paint_roads(np.full(lat.shape, sf.GRASS, np.uint8), lat, lon, [f], 2.0)
        self.assertEqual(int(out[64, 10]), sf.COBBLE)
        self.assertEqual(sf.MATERIALS[sf.COBBLE][1], "COBBLESTONE")
        self.assertEqual(sf.DIRT, len(sf.MATERIALS) - 1, "dirt still last")


if __name__ == "__main__":
    unittest.main()
