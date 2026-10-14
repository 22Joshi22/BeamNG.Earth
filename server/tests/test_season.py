"""season: leaf-off imagery to the summer reference's colour, and the fall grade by date and latitude."""
import datetime
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import season  # noqa: E402


class AmountsTest(unittest.TestCase):
    def test_fall_by_date_and_latitude(self):
        md = season.season_amounts(datetime.date(2026, 10, 14), 39.2)["fall"]
        self.assertGreater(md, 0.3)
        self.assertLess(md, 0.7, "mid October in Maryland: turning, not peak")
        self.assertEqual(season.season_amounts(datetime.date(2026, 7, 1), 39.2)["fall"], 0.0)
        self.assertGreater(season.season_amounts(datetime.date(2026, 10, 14), 46.0)["fall"], md, "further north: further")
        self.assertEqual(season.season_amounts(datetime.date(2026, 10, 14), 10.0)["fall"], 0.0, "tropics")
        self.assertEqual(season.season_amounts(datetime.date(2026, 10, 14), -39.2)["fall"], 0.0, "southern spring")
        self.assertEqual(season.amounts_for("off", 39.2)["fall"], 0.0)
        self.assertEqual(season.amounts_for("fall", 39.2)["fall"], 1.0)


class ColourTest(unittest.TestCase):
    def test_hsv_roundtrip(self):
        c = np.random.default_rng(1).integers(0, 256, (500, 3)).astype(np.uint8)
        self.assertEqual(int(np.abs(season._rgb(*season._hsv(c)).astype(int) - c).max()), 0)

    def test_leaf_on_greens_vegetation_only(self):
        rgb = np.zeros((64, 64, 3), np.uint8)
        rgb[:, :32] = (120, 105, 80)            # dormant grass (left), a grey roof (right)
        rgb[:, 32:] = (128, 128, 128)
        ref = np.zeros((8, 8, 3), np.uint8)
        ref[:, :4] = (70, 110, 60)              # summer: green left, the same roof right
        ref[:, 4:] = (128, 128, 128)
        out = season.leaf_on(rgb, ref).astype(int)
        self.assertGreater(out[32, 4, 1] - out[32, 4, 0], 10, "the grass turned green")
        self.assertLess(np.abs(out[32, 60] - 128).max(), 3, "the roof kept its colour")

    def test_fall_turns_dark_canopy_not_lawn(self):
        rng = np.random.default_rng(2)
        canopy = np.clip(np.array([50, 85, 45]) + rng.normal(0, 18, (64, 64, 1)), 0, 255)
        lawn = np.tile(np.array([110, 150, 90], float), (64, 64, 1))
        rgb = np.concatenate([canopy, lawn], 1).astype(np.uint8)
        ys, xs = np.mgrid[0:64, 0:128].astype(np.float32) * 1.0
        out = season.grade(rgb, xs, -ys, {"fall": 1.0})
        h0, _, _ = season._hsv(rgb)
        h1, _, _ = season._hsv(out)
        turned = (h0 - h1)[:, :64]
        self.assertGreater(float(np.mean(turned > 20)), 0.2, "a good share of the canopy turned yellow / red")
        self.assertLess(float(np.max((h0 - h1)[:, 70:])), 25, "the lawn only yellowed a little")
        self.assertTrue(np.array_equal(season.grade(rgb, xs, -ys, {"fall": 0.0}), rgb), "summer: unchanged")
        # world-anchored: the same place graded alone gives the same texels
        part = season.grade(rgb[:, :64], xs[:, :64], -ys[:, :64], {"fall": 1.0})
        self.assertLess(np.abs(part[:, 10:54].astype(int) - out[:, 10:54].astype(int)).max(), 2)


if __name__ == "__main__":
    unittest.main()
