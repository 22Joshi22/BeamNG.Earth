"""Installer dependency failures: no DuckDB source build on Windows, and a usable recovery path."""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import install  # noqa: E402


class SetupVenv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.venv = os.path.join(self.tmp.name, ".venv")
        self.stamp = os.path.join(self.venv, ".requirements.sha256")
        for name, value in (("VENV", self.venv), ("REQ_STAMP", self.stamp)):
            patch = mock.patch.object(install, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def existing_venv(self, windows):
        py = os.path.join(self.venv, "Scripts", "python.exe") if windows else os.path.join(self.venv, "bin", "python")
        os.makedirs(os.path.dirname(py), exist_ok=True)
        Path(py).touch()
        return py

    def test_success_uses_the_existing_interpreter_and_stamps_requirements(self):
        # The existing environment can differ from the Python running install.py (or start.py).
        for windows in (True, False):
            with self.subTest(windows=windows), mock.patch.object(install, "IS_WIN", windows):
                py = self.existing_venv(windows)
                with mock.patch.object(install.subprocess, "run", side_effect=[
                        mock.Mock(returncode=0), mock.Mock(returncode=0, stdout="numpy / Pillow\n")]) as run:
                    install.setup_venv(dry_run=False)
                cmd = run.call_args_list[0].args[0]
                self.assertEqual(cmd[:4], [py, "-m", "pip", "install"])
                self.assertEqual("--only-binary=duckdb" in cmd, windows)
                self.assertEqual(cmd[-2:], ["-r", install.REQUIREMENTS])
                self.assertEqual(Path(self.stamp).read_text(), install.requirements_hash())

    def test_missing_windows_wheel_explains_how_to_replace_the_existing_venv(self):
        with mock.patch.object(install, "IS_WIN", True):
            py = self.existing_venv(True)
            Path(self.stamp).write_text("old requirements")
            with mock.patch.object(install.subprocess, "run", return_value=mock.Mock(returncode=1)) as run:
                with self.assertRaises(SystemExit) as error:
                    install.setup_venv(dry_run=False)
            self.assertEqual(error.exception.code, 1)
            self.assertEqual(run.call_count, 1)
            self.assertIn("--only-binary=duckdb", run.call_args.args[0])
            self.assertTrue(os.path.isfile(py))
            self.assertEqual(Path(self.stamp).read_text(), "old requirements")
        message = self.output.getvalue()
        for text in ("No matching distribution found", "64-bit Python 3.12", "rename", "unused backup name",
                     "py -3.12 install.py", "does not replace an existing .venv", "standalone Windows .exe"):
            self.assertIn(text, message)

    def test_failed_linux_install_keeps_the_network_hint_without_windows_commands(self):
        with mock.patch.object(install, "IS_WIN", False):
            self.existing_venv(False)
            with mock.patch.object(install.subprocess, "run", return_value=mock.Mock(returncode=1)):
                with self.assertRaises(SystemExit):
                    install.setup_venv(dry_run=False)
        self.assertIn("Check your internet connection", self.output.getvalue())
        self.assertNotIn("py -3.12", self.output.getvalue())
        self.assertFalse(os.path.exists(self.stamp))

    def test_failed_import_does_not_mark_requirements_as_ready(self):
        with mock.patch.object(install, "IS_WIN", True):
            self.existing_venv(True)
            with mock.patch.object(install.subprocess, "run", side_effect=[
                    mock.Mock(returncode=0), mock.Mock(returncode=1, stderr="broken import")]):
                with self.assertRaises(SystemExit):
                    install.setup_venv(dry_run=False)
        self.assertIn("broken import", self.output.getvalue())
        self.assertFalse(os.path.exists(self.stamp))

    def test_dry_run_does_not_create_an_environment_or_run_pip(self):
        with mock.patch.object(install.subprocess, "run") as run:
            install.setup_venv(dry_run=True)
        run.assert_not_called()
        self.assertFalse(os.path.exists(self.venv))


if __name__ == "__main__":
    unittest.main()
