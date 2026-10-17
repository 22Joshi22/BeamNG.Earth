"""staging: atomic writes into the game's user folder stage their temporary file outside it."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import staging  # noqa: E402


class StagingTest(unittest.TestCase):
    def test_outside_the_user_folder_same_disk(self):
        with tempfile.TemporaryDirectory() as root:
            user = os.path.join(root, "BeamNG.drive", "current")
            target = os.path.join(user, "temp", "beamngEarth", "s1", "far_1_2.cdae")
            os.makedirs(os.path.dirname(target))
            tmp = staging.tmp_for(target)
            self.assertEqual(os.path.dirname(tmp), os.path.join(root, "BeamNG.drive", staging.STAGING))
            self.assertFalse(tmp.startswith(user))
            with open(tmp, "wb") as f:
                f.write(b"x")
            os.replace(tmp, target)
            self.assertTrue(os.path.exists(target))
            self.assertNotEqual(staging.tmp_for(target), staging.tmp_for(target))   # one per write

    def test_elsewhere_beside_the_file(self):
        self.assertEqual(staging.tmp_for("/home/x/.cache/beamng-earth/dem/1/2.webp"),
                         "/home/x/.cache/beamng-earth/dem/1/2.webp.tmp")

    def test_old_leftovers_swept(self):
        with tempfile.TemporaryDirectory() as root:
            d = os.path.join(root, "BeamNG.drive", staging.STAGING)
            os.makedirs(d)
            old, new = os.path.join(d, "a.tmp"), os.path.join(d, "b.tmp")
            for p in (old, new):
                open(p, "w").close()
            t = time.time() - staging.SWEEP_AGE_S - 10
            os.utime(old, (t, t))
            staging.tmp_for(os.path.join(root, "BeamNG.drive", "current", "temp", "x", "y.json"))
            self.assertFalse(os.path.exists(old))
            self.assertTrue(os.path.exists(new))


if __name__ == "__main__":
    unittest.main()
