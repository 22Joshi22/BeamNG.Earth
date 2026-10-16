"""fast: the Rust kernels give what the Python code gives."""
import math
import sys
import types
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fast  # noqa: E402
import islands  # noqa: E402


@unittest.skipUnless(fast.AVAILABLE, "no earthfast binary for this platform (tools/build_native.py)")
class FastTest(unittest.TestCase):
    def python_ring(self, poly, roads_hw, z=0.0):
        saved = fast._ef
        fast._ef = None
        try:
            return islands._island_ring(poly, roads_hw, z)
        finally:
            fast._ef = saved

    def test_island_ring_matches_python(self):
        rng = np.random.default_rng(7)
        for trial in range(20):
            r = 15 + 20 * rng.random()
            k = 24
            t = np.linspace(0, 2 * math.pi, k, endpoint=False)
            wob = 1 + 0.15 * np.sin(3 * t + trial)
            poly = np.stack([r * wob * np.cos(t), r * wob * np.sin(t)], 1)
            roads = [(np.vstack([poly[i:i + 7], poly[(i + 7) % k:(i + 8) % k or None][:1]]), 3.0 + rng.random())
                     for i in range(0, k, 6)]
            a = islands._island_ring(poly, roads, 1.0)
            b = self.python_ring(poly, roads, 1.0)
            self.assertEqual(a is None, b is None)
            if a is not None:
                self.assertTrue(np.allclose(a, b, atol=1e-9), f"trial {trial}")

    def test_clear_matches_python(self):
        import furniture
        rng = np.random.default_rng(3)
        lines = [(f"r{i}", 2 + 3 * rng.random(), np.cumsum(rng.normal(0, 8, (6, 3)), 0)) for i in range(8)]
        o = fast.Others(lines)
        for _ in range(300):
            p = rng.normal(0, 20, 3)
            own = f"r{rng.integers(0, 8)}"
            self.assertEqual(furniture._clear(p, o, own), furniture._clear(p, lines, own))

    def test_dual_side_matches_python(self):
        import roads
        rng = np.random.default_rng(11)
        for trial in range(200):
            n = rng.integers(2, 12)
            a = np.cumsum(rng.normal(0, 25, (n, 2)), 0)
            off = rng.normal(0, 1, 2) * rng.uniform(2, 50)
            b = a[::-1] + off + rng.normal(0, 1, (n, 2)) if trial % 2 else np.cumsum(rng.normal(0, 25, (n, 2)), 0)
            got = roads._dual_side(a, b)
            saved = fast._ef
            fast._ef, fast.AVAILABLE = None, False
            try:
                want = roads._dual_side(a, b)
            finally:
                fast._ef, fast.AVAILABLE = saved, True
            # the share may differ by one ~10 m sample: where the nearest point on b is a vertex both segments share,
            # which one's direction counts is a floating-point tie (numpy and Rust round it differently)
            samples = max(2, int(np.hypot(*np.diff(a, axis=0).T).sum() // 10) + 1)
            self.assertLessEqual(abs(got[0] - want[0]), 1.0 / samples + 1e-9, (trial, got, want))
            if abs(got[1]) > 0.5 and abs(want[1]) > 0.5:     # (the caller uses only a clear side, > 0.5)
                self.assertEqual(np.sign(got[1]), np.sign(want[1]), (trial, got, want))

    def test_deck_crossings_matches_python(self):
        import roads
        rng = np.random.default_rng(5)
        for trial in range(50):
            lines = [np.cumsum(rng.normal(0, 30, (rng.integers(1, 9), 2)), 0) + rng.normal(0, 60, 2)
                     for _ in range(rng.integers(0, 7))]
            got = roads._deck_crossings(lines)
            want = roads._deck_crossings_py(lines)
            self.assertEqual([g[:2] for g in got], [w[:2] for w in want], trial)
            for g, w in zip(got, want):
                self.assertAlmostEqual(g[2], w[2], places=9)
                self.assertAlmostEqual(g[3], w[3], places=9)

    def test_point_inside_matches_python(self):
        rng = np.random.default_rng(9)
        for _ in range(100):
            k = rng.integers(3, 12)
            ring = rng.normal(0, 20, (k, 2))
            ring[rng.integers(0, k)][1] = ring[0][1]           # a level edge (the 1e-12 divisor)
            for p in rng.normal(0, 25, (20, 2)):
                saved = fast._ef
                fast._ef = None
                try:
                    want = islands._inside(ring, p)
                finally:
                    fast._ef = saved
                self.assertEqual(islands._inside(ring, p), want)

    def test_geodetic_to_enu_matches_numpy(self):
        import geodesy
        rng = np.random.default_rng(2)
        for lat0, lon0 in ((39.28, -76.6), (36.17, -115.145), (-33.9, 151.2), (64.1, -21.9)):
            f = geodesy.EnuFrame(lat0, lon0)
            lat = lat0 + rng.normal(0, 0.05, 50)
            lon = lon0 + rng.normal(0, 0.05, 50)
            h = rng.uniform(-50, 3000, 50)
            for hh in (h, 0.0, 12.5):
                got = f.geodetic_to_enu(lat, lon, hh)
                want = f.ecef_to_enu(*geodesy.geodetic_to_ecef(lat, lon, hh))
                for g, w in zip(got, want):
                    self.assertTrue(np.allclose(g, w, rtol=0, atol=1e-6))

    def test_bc7_matches_python(self):
        import bc7
        rng = np.random.default_rng(4)
        lum = np.cumsum(np.cumsum(rng.normal(0, 1.0, (64, 64)), 0), 1)
        lum = (lum - lum.min()) / (np.ptp(lum) + 1e-9) * 200 + 30
        photo = np.clip(lum[..., None] * np.array([1.0, 0.9, 0.75]) + rng.normal(0, 6, (64, 64, 3)), 0, 255)
        for img in (photo.astype(np.uint8), photo.astype(np.uint8)[:61, :37],
                    (rng.random((32, 48, 4)) * 255).astype(np.uint8), np.full((8, 8, 3), 255, np.uint8)):
            self.assertEqual(bc7.encode(img), bc7._encode_py(img), img.shape)

    def test_cut_outside_matches_python(self):
        import roads
        rng = np.random.default_rng(8)
        for trial in range(150):
            k = int(rng.integers(2, 8))
            pts = np.c_[np.cumsum(rng.normal(0, 15, (k, 2)), 0), np.full(k, rng.normal())]
            blk = [(np.cumsum(rng.normal(0, 15, (int(rng.integers(1, 6)), 2)), 0) + rng.normal(0, 20, 2),
                    rng.uniform(1, 8)) for _ in range(int(rng.integers(0, 5)))]
            got = roads.cut_outside(pts, blk, 2.0)
            fast.AVAILABLE = False
            try:
                want = roads.cut_outside(pts, blk, 2.0)
            finally:
                fast.AVAILABLE = True
            self.assertEqual(len(got), len(want), trial)
            for a, b in zip(got, want):
                self.assertTrue(np.array_equal(a, b), trial)

    def test_densify_matches_python(self):
        import roads
        rng = np.random.default_rng(2)
        for trial in range(200):
            k, c = int(rng.integers(1, 9)), int(rng.integers(2, 4))
            pts = np.cumsum(rng.normal(0, 12, (k, c)), 0)
            if trial % 7 == 0 and k > 1:
                pts[1] = pts[0]                     # a zero-length segment
            sp = float(rng.choice([0.5, 2.0, 4.0, 10.0]))
            got = roads.densify(pts, sp)
            fast.AVAILABLE = False
            try:
                want = roads.densify(pts, sp)
            finally:
                fast.AVAILABLE = True
            self.assertTrue(np.array_equal(got, want), trial)

    def test_measure_row_matches_python(self):
        import roadwidth
        rng = np.random.default_rng(6)
        for trial in range(60):
            # a street between two rows of building walls (gaps, jitter), sometimes open on one side
            half = rng.uniform(5, 15)
            xs = np.arange(-80, 80, 9.0)
            segs = []
            for side in (1, -1):
                if trial % 5 == 0 and side < 0:
                    continue
                for x in xs:
                    if rng.random() < 0.8:
                        y = side * (half + rng.normal(0, 0.6))
                        segs.append([x, y, x + rng.uniform(4, 9), y + rng.normal(0, 0.4)])
            walls = roadwidth.Walls(np.array(segs) if segs else np.zeros((0, 4)))
            k = int(rng.integers(2, 9))
            line = np.c_[np.linspace(-60, 60, k), rng.normal(0, 0.5, k)]
            got = roadwidth.measure_row(line, walls)
            fast.AVAILABLE = False
            try:
                want = roadwidth.measure_row(line, walls)
            finally:
                fast.AVAILABLE = True
            self.assertEqual(got is None, want is None, trial)
            if got is not None:
                self.assertAlmostEqual(got, want, places=9)

    def test_grade_points_matches_python(self):
        import grading
        rng = np.random.default_rng(12)
        for trial in range(20):
            px, py = rng.uniform(-200, 200, 3000), rng.uniform(-200, 200, 3000)
            base = rng.normal(100, 5, 3000)
            plans = []
            for _ in range(int(rng.integers(0, 6))):
                k = int(rng.integers(2, 12))
                xy = np.cumsum(rng.normal(0, 30, (k, 2)), 0) + rng.uniform(-150, 150, 2)
                plans.append(types.SimpleNamespace(xy=xy, z=rng.normal(100, 3, k), half=float(rng.uniform(2, 8)),
                                                   bbox=(*xy.min(0), *xy.max(0))))
            got = grading.grade_points(px, py, base, plans, 0.1)
            fast.AVAILABLE = False
            try:
                want = grading.grade_points(px, py, base, plans, 0.1)
            finally:
                fast.AVAILABLE = True
            self.assertTrue(np.allclose(got, want, rtol=0, atol=1e-9), trial)

    def test_resample_and_smooth_match_python(self):
        import grading
        rng = np.random.default_rng(4)
        for trial in range(150):
            k = int(rng.integers(1, 15))
            xy = np.cumsum(rng.normal(0, 20, (k, 2)), 0)
            if trial % 9 == 0 and k > 1:
                xy[1] = xy[0]
            z = rng.normal(100, 5, int(rng.integers(1, 40)))
            got = (grading.resample(xy, 4.0), [grading.smooth_profile(z, 4.0, s) for s in (0.0, 6.0, 20.0, 60.0)])
            fast.AVAILABLE = False
            try:
                want = (grading.resample(xy, 4.0), [grading.smooth_profile(z, 4.0, s) for s in (0.0, 6.0, 20.0, 60.0)])
            finally:
                fast.AVAILABLE = True
            self.assertTrue(np.array_equal(got[0][0], want[0][0]) and np.array_equal(got[0][1], want[0][1]), trial)
            for a, b in zip(got[1], want[1]):
                self.assertTrue(np.allclose(a, b, rtol=0, atol=1e-9), trial)

    def test_contains_points_and_seg_dist_match_python(self):
        import polygon
        import roads
        rng = np.random.default_rng(3)
        for trial in range(100):
            k = int(rng.integers(3, 40))
            ang = np.sort(rng.uniform(0, 2 * np.pi, k))
            rad = rng.uniform(5, 30, k)
            ring = np.c_[rad * np.cos(ang), rad * np.sin(ang)]
            if trial % 5 == 0:
                ring = np.round(ring)               # shared ys, horizontal edges, points on edges
            pts = np.round(rng.uniform(-35, 35, (300, 2)), 1 if trial % 2 else 3)
            got = polygon.contains_points(ring, pts)
            saved = polygon._ef
            polygon._ef = None
            try:
                want = polygon.contains_points(ring, pts)
            finally:
                polygon._ef = saved
            self.assertTrue(np.array_equal(got, want), trial)
            line = np.cumsum(rng.normal(0, 10, (int(rng.integers(2, 9)), 2)), 0)
            p = rng.normal(0, 20, 2)
            a = roads._seg_dist_min(line, p)
            fast.AVAILABLE = False
            try:
                b = roads._seg_dist_min(line, p)
            finally:
                fast.AVAILABLE = True
            self.assertEqual(a, b)

    def test_rock_matches_python(self):
        import geodesy
        import rockface
        frame = geodesy.EnuFrame(46.6, 7.9)
        rng = np.random.default_rng(5)
        for trial in range(25):
            n, usp = int(rng.integers(3, 20)), float(rng.uniform(4, 40))
            amp = usp * rng.uniform(0.2, 1.6)
            U = np.cumsum(rng.normal(0, amp / 3, (n, n)), 0) + rng.normal(0, amp / 3, (n, n))
            px = int(rng.choice([32, 64]))
            rgb = rng.integers(0, 256, (px, px, 3)).astype(np.uint8)
            size = usp * (n - 1)
            got = rockface.rock(rgb, frame, 100.0, -50.0, size, U, 100.0, -50.0, usp)
            saved = rockface._ef
            rockface._ef = None
            try:
                want = rockface.rock(rgb, frame, 100.0, -50.0, size, U, 100.0, -50.0, usp)
            finally:
                rockface._ef = saved
            self.assertTrue(np.array_equal(got, want), trial)

    def test_mip_levels_match_python(self):
        import imagery
        # the Rust sRGB step reads bytes from thresholds: exact only if the formula never decreases (every float32)
        self.assertTrue(fast._ef.srgb_monotonic())
        rng = np.random.default_rng(0)
        for n in (1, 2, 4, 8, 64, 256):
            rgb = (rng.random((n, n, 3)) * 255).astype(np.uint8)
            got = imagery.mip_arrays(rgb)
            saved = imagery._ef
            imagery._ef = None
            try:
                want = imagery.mip_arrays(rgb)
            finally:
                imagery._ef = saved
            self.assertEqual(len(got), len(want), n)
            for a, b in zip(got, want):
                self.assertTrue(np.array_equal(a, b), n)

    def test_loop_crossed_and_any_within_match_python(self):
        import junctions
        rng = np.random.default_rng(14)
        for trial in range(80):
            k = int(rng.integers(3, 14))
            ang = np.sort(rng.uniform(0, 2 * np.pi, k))
            poly = np.c_[25 * np.cos(ang), 25 * np.sin(ang)] * rng.uniform(0.5, 1.5)
            road = np.cumsum(rng.normal(0, 12, (int(rng.integers(2, 7)), 2)), 0) + rng.normal(0, 20, 2)
            got = islands._crosses(poly, road)
            saved = fast._ef
            fast._ef = None
            try:
                want = islands._crosses(poly, road)
            finally:
                fast._ef = saved
            self.assertEqual(got, want, trial)
            pts = rng.normal(0, 30, (int(rng.integers(1, 20)), 2))
            h = float(rng.uniform(1, 10))
            self.assertEqual(fast._ef.any_within(pts, road, h), bool((junctions._seg_dist(pts, road) < h).any()))

    def test_shared_walls_matches_python(self):
        import buildings
        rng = np.random.default_rng(17)
        for trial in range(30):
            rings = []
            x = 0.0
            for _ in range(int(rng.integers(2, 40))):          # a terrace: shared walls, a few gaps and others
                w = float(rng.uniform(4, 9))
                d = float(rng.uniform(8, 14))
                y0 = float(rng.normal(0, 0.3))
                rings.append(np.array([[x, y0], [x + w, y0], [x + w, y0 + d], [x, y0 + d]]) + rng.normal(0, 0.2, (4, 2)))
                x += w + float(rng.choice([0.0, 0.0, 0.3, 2.0]))
            for _ in range(int(rng.integers(0, 10))):
                c = rng.uniform(-20, x + 20, 2)
                k = int(rng.integers(3, 9))
                ang = np.sort(rng.uniform(0, 2 * np.pi, k))
                rings.append(c + np.c_[np.cos(ang), np.sin(ang)] * rng.uniform(3, 15))
            got = buildings._shared_walls(rings)
            saved = fast._ef
            fast._ef = None
            try:
                want = buildings._shared_walls(rings)
            finally:
                fast._ef = saved
            self.assertEqual(got.tolist(), want.tolist(), trial)
            self.assertGreater(want.sum(), 0)

    def test_season_grade_matches_python(self):
        import season
        rng = np.random.default_rng(23)
        for trial in range(24):
            px = int(rng.choice([64, 128, 256]))
            rgb = rng.integers(0, 256, (px, px, 3), dtype=np.uint8)
            green = rng.random((px, px)) < 0.7
            rgb[green, 1] = np.maximum(rgb[green, 1], np.maximum(rgb[green, 0], rgb[green, 2]))
            rgb[:4, :4] = rgb[0, 0]                       # ties: r == g, g == b
            rgb[5, :8, 0] = rgb[5, :8, 1]
            span = float(rng.choice([100.0, 3000.0]))     # fine texels (canopy texture) and far ones
            xs = np.linspace(0, span, px, dtype=np.float32) + np.float32(rng.uniform(-1e6, 1e6))
            ys = np.linspace(span, 0, px, dtype=np.float32) + np.float32(rng.uniform(-1e6, 1e6))
            xm, ym = np.meshgrid(xs, ys)
            amounts = [{"fall": 1.0}, {"fall": 0.4, "winter": 0.6}, {"spring": 0.7}, {"fall": 0.2, "spring": 0.3},
                       {"winter": 1.0}][trial % 5]
            got = season.grade_full(rgb, xm, ym, amounts)
            saved = fast._ef
            fast._ef = None
            try:
                want = season.grade_full(rgb, xm, ym, amounts)
            finally:
                fast._ef = saved
            np.testing.assert_array_equal(got, want, err_msg=str(trial))

    def test_tangents_match_python(self):
        import math
        import cdae
        import dae
        rng = np.random.default_rng(29)
        for trial in range(30):
            pos, uv, tri = dae.grid(int(rng.integers(2, 30)), float(rng.uniform(0.5, 50)),
                                    zfun=lambda x, y: math.sin(x * 0.1) * math.cos(y * 0.07) * 20)
            V = np.asarray(pos, float).reshape(-1, 3)
            UV = np.asarray(uv, float).reshape(-1, 2) + (rng.normal(0, 0.01, (len(V), 2)) if trial % 3 == 0 else 0)
            if trial % 5 == 0:
                UV[:len(UV) // 3] = 0.25                   # constant UVs: the fallback axes
            T = np.asarray(tri, np.int64).reshape(-1, 3)
            I = np.ascontiguousarray(T[:, ::-1])
            N = cdae.face_normal_sum(V, T)
            N = N / np.maximum(np.linalg.norm(N, axis=1), 1e-12)[:, None]
            got = cdae.tangents(V, UV, N, I)
            saved = fast._ef
            fast._ef = None
            try:
                want = cdae.tangents(V, UV, N, I)
            finally:
                fast._ef = saved
            # equal on Linux; Windows' numpy sums its 3-term dots an ulp differently (2e-16), so not exactly there
            np.testing.assert_allclose(got, want, rtol=0, atol=1e-12, err_msg=str(trial))
            np.testing.assert_array_equal(got[:, 3], want[:, 3])

    def test_season_noise_matches_python(self):
        import season
        rng = np.random.default_rng(31)
        for t in range(20):
            x = (rng.uniform(-5e5, 5e5, (48, 40)) + rng.normal(0, 30, (48, 40))).astype(np.float32) / np.float32(18.0)
            y = rng.uniform(-5e5, 5e5, (48, 40)).astype(np.float32) / np.float32(25.0)
            got = season._noise(x, y, t % 4)
            saved = fast._ef
            fast._ef = None
            try:
                want = season._noise(x, y, t % 4)
            finally:
                fast._ef = saved
            self.assertEqual(got.dtype, want.dtype)
            np.testing.assert_array_equal(got, want)

    def test_terrarium_heights_match_python(self):
        import io
        import dem
        from PIL import Image
        rng = np.random.default_rng(37)
        for mode in ("RGB", "RGBA"):
            a = rng.integers(0, 256, (512, 512, 4 if mode == "RGBA" else 3), dtype=np.uint8)
            buf = io.BytesIO()
            Image.fromarray(a, mode).save(buf, "PNG")
            got = dem.decode_terrarium(buf.getvalue(), 512)
            saved = fast._ef
            fast._ef = None
            try:
                want = dem.decode_terrarium(buf.getvalue(), 512)
            finally:
                fast._ef = saved
            self.assertEqual(got.dtype, want.dtype)
            np.testing.assert_array_equal(got, want)

    def test_seam_pass_matches_python(self):
        import colormatch
        rng = np.random.default_rng(43)
        h, wd = 90, 110
        yy, xx = np.mgrid[0:h, 0:wd]
        L = rng.normal(0, 0.05, (h, wd, 6)).astype(np.float32)
        L[..., :3] += (xx > 0.6 * wd + 0.2 * yy)[..., None] * np.float32(0.3)      # a USGS-only seam
        w = rng.random((h, wd)).astype(np.float32)
        w[20:40, 30:70] = 0
        Lw = L * w[..., None]
        for seg, depth in ((16, 3), (6, 2)):
            offs = [int(round((k - (colormatch.NSEG - 1) / 2) * seg)) for k in range(colormatch.NSEG)]
            for deg in (0.0, 35.0, 120.0):
                arrs = [colormatch._rot(w, deg)] + [colormatch._rot(Lw[..., c], deg) for c in range(6)]
                got, gD = colormatch._seam_pass(arrs, seg // 2, depth, offs, colormatch.NSEG, colormatch.GATE)
                saved = fast._ef
                fast._ef = None
                try:
                    want, wD = colormatch._seam_pass(arrs, seg // 2, depth, offs, colormatch.NSEG, colormatch.GATE)
                finally:
                    fast._ef = saved
                self.assertGreater(int((want > 0).sum()), 0)
                np.testing.assert_array_equal(got, want, err_msg=f"score {seg} {deg}")
                for c in range(3):
                    np.testing.assert_array_equal(gD[c], wD[c], err_msg=f"Dm {c} {seg} {deg}")

    def test_view_distances_match_python(self):
        import farfield as ff
        rng = np.random.default_rng(47)
        tiles = [ff.Tile(int(rng.integers(-2, 7)), int(rng.integers(-40, 40)), int(rng.integers(-40, 40)))
                 for _ in range(400)]
        for cam in ({"x": 100, "y": -50, "fx": 0.6, "fy": 0.8, "fovDeg": 65}, {"x": 0, "y": 0, "fx": 0, "fy": 0}):
            cone = ff.view_cone(cam)
            got = ff.view_distances(tiles, cone, 30.0, -20.0)
            want = [ff.view_distance(t, cone, 30.0, -20.0) for t in tiles]
            np.testing.assert_allclose(got, want, rtol=1e-12, atol=1e-9)

    def test_vmedian_matches_python(self):
        import colormatch
        rng = np.random.default_rng(41)
        for trial in range(6):
            h, w = int(rng.integers(5, 60)), int(rng.integers(5, 60))
            for K in (5, 3, 7):
                V = rng.normal(0, 1, (K, h, w)).astype(np.float32)
                ok = rng.random((K, h, w)) < 0.7
                nv = ok.sum(0)
                gm = colormatch._vmedian(V, ok, nv)
                saved = fast._ef
                fast._ef = None
                try:
                    wm = colormatch._vmedian(V, ok, nv)
                finally:
                    fast._ef = saved
                np.testing.assert_array_equal(gm, wm, err_msg=f"vmedian {trial} {K}")

    def test_dt_blur_matches_python(self):
        import colormatch
        rng = np.random.default_rng(43)
        for trial in range(8):
            h, w = int(rng.integers(5, 70)), int(rng.integers(5, 70))
            X = rng.random((h, w, int(rng.choice([3, 6, 7])))).astype(np.float32)
            cut = (rng.random((h, w)) < 0.1) if trial % 2 else None
            sigma = float(rng.uniform(1, 20))
            got = colormatch.dt_blur(X, sigma, cut)
            saved = fast._ef
            fast._ef = None
            try:
                want = colormatch.dt_blur(X, sigma, cut)
            finally:
                fast._ef = saved
            np.testing.assert_array_equal(got, want, err_msg=str(trial))

    def test_mesh_warp_matches_pillow(self):
        from PIL import Image
        rng = np.random.default_rng(47)
        for t in range(12):
            H, W = int(rng.integers(256, 700)), int(rng.integers(256, 700))
            img = rng.integers(0, 256, (H, W, 4), dtype=np.uint8)
            if t % 2:
                img[..., 3] = rng.choice(np.array([0, 254, 255], np.uint8), (H, W))
            px = int(rng.choice([64, 128, 256])); cells = min(16, px); cw = px // cells
            k = np.arange(cells + 1) / cells
            X, Y = np.meshgrid(k, k)
            qx = X * W * rng.uniform(0.3, 1.2) + rng.uniform(-50, 200) + 20 * np.sin(Y * 3)
            qy = Y * H * rng.uniform(0.3, 1.2) + rng.uniform(-50, 200) + 15 * np.cos(X * 2)
            mesh = [((c * cw, r * cw, (c + 1) * cw, (r + 1) * cw),
                     (qx[r, c], qy[r, c], qx[r + 1, c], qy[r + 1, c], qx[r + 1, c + 1], qy[r + 1, c + 1],
                      qx[r, c + 1], qy[r, c + 1])) for r in range(cells) for c in range(cells)]
            want = np.asarray(Image.fromarray(img, "RGBA").transform((px, px), Image.MESH, mesh,
                                                                     resample=Image.BILINEAR))
            got = fast._ef.mesh_warp(img, np.ascontiguousarray(qx), np.ascontiguousarray(qy), cells, cw, px)
            np.testing.assert_array_equal(got, want, err_msg=str(t))

    def test_points_and_distances(self):
        sq = np.array([[0, 0], [10, 0], [10, 10], [0, 10]], float)
        pts = np.array([[5, 5], [15, 5], [-1, -1], [9.9, 0.1]], float)
        self.assertEqual(list(fast._ef.points_inside(sq, pts)), [True, False, False, True])
        d = fast._ef.polyline_dist(np.array([[0, 0], [10, 0]], float), np.array([[5, 3], [12, 0], [-3, 4]], float))
        self.assertTrue(np.allclose(d, [3, 2, 5]))


if __name__ == "__main__":
    unittest.main()
