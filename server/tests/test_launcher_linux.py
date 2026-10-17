"""launcher.py on Linux: the user folder and start path with a Flatpak Steam, the game-closed grace period."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import install  # noqa: E402
import launcher  # noqa: E402

FP = (".var", "app", "com.valvesoftware.Steam")


def touch(path, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").close()
    if mtime is not None:
        os.utime(path, (mtime, mtime))


class FlatpakSteam(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.env = mock.patch.dict(os.environ, {"HOME": self.home})
        self.env.start()
        # the player's report (2026-10-17): Debian, Steam as a Flatpak; an earlier launcher made ~/.local/share's
        # folder (the mod in it), the game itself writes inside the Flatpak's home
        self.host = os.path.join(self.home, ".local", "share", "BeamNG", "BeamNG.drive", "current")
        os.makedirs(os.path.join(self.host, "mods"))
        self.flat = os.path.join(self.home, *FP, ".local", "share", "BeamNG", "BeamNG.drive", "current")
        self.exe = os.path.join(self.home, *FP, ".local", "share", "Steam", "steamapps", "common", "BeamNG.drive",
                                "BinLinux", "BeamNG.drive.x64")
        touch(self.exe)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_folder_the_game_wrote_in(self):
        touch(os.path.join(self.flat, "beamng.log"))
        with mock.patch.object(launcher, "steam_compat_tool", return_value=""):
            self.assertEqual(launcher.linux_build(), ("native", self.flat))

    def test_flatpak_folder_before_the_first_start(self):
        import shutil
        shutil.rmtree(os.path.join(self.home, ".local"))
        self.assertEqual(launcher.native_folder(), self.flat)

    def test_host_steam_keeps_local_share(self):
        os.makedirs(os.path.join(self.home, ".local", "share", "Steam"))
        self.assertEqual(launcher.native_folder(), self.host)

    def test_newest_log_wins(self):
        touch(os.path.join(self.flat, "beamng.log"), time.time() - 1000)
        touch(os.path.join(self.host, "beamng.log"))
        self.assertEqual(launcher.native_folder(), self.host)

    def test_flatpak_game_through_steam(self):
        self.assertEqual(install._steam_roots()[0], os.path.join(self.home, *FP, ".local", "share", "Steam"))
        self.assertEqual(launcher.linux_game_exe(), self.exe)
        self.assertTrue(launcher.sandboxed_game(self.exe))
        self.assertFalse(launcher.sandboxed_game("/usr/games/steam/BeamNG.drive.x64"))

    def test_flatpak_steam_pid(self):
        touch(os.path.join(self.home, *FP, ".steam", "steam.pid"))
        with open(os.path.join(self.home, *FP, ".steam", "steam.pid"), "w") as f:
            f.write(str(os.getpid()))
        self.assertTrue(launcher.steam_running())


class GameClosed(unittest.TestCase):
    def test_a_restart_during_the_start_is_not_a_close(self):
        seen = iter([[1], [], [], [2], [2], []] + [[]] * 100)
        clock = [0.0]

        def sleep(s):
            clock[0] += s
        with mock.patch.object(launcher, "game_pids", lambda: next(seen)), \
                mock.patch.object(launcher.time, "sleep", sleep), \
                mock.patch.object(launcher.time, "time", lambda: clock[0]):
            launcher.wait_for_game(None)
        # [1] found; gone twice (6 s), back ([2] x2, 6 s), then gone for good: closed after GAME_GONE_S of absence
        self.assertGreaterEqual(clock[0], 12 + launcher.GAME_GONE_S)


if __name__ == "__main__":
    unittest.main()
