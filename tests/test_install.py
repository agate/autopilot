from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT, TempData
from autopilot import install


class InstallTest(unittest.TestCase):
    def test_plist_fields(self):
        with TempData() as d:
            pl = plistlib.loads(install.render_plist(["/launcher"], 300, ["/opt/tools/bin"], d))
            self.assertEqual(pl["Label"], "com.autopilot.gate")
            self.assertEqual(pl["ProgramArguments"], ["/launcher"])
            self.assertEqual(pl["StartInterval"], 300)
            self.assertTrue(pl["RunAtLoad"])
            self.assertEqual(pl["StandardOutPath"], str(d / "gate.log"))
            self.assertEqual(pl["EnvironmentVariables"]["AUTOPILOT_DATA"], str(d))

    def test_plist_path_has_captured_dirs(self):
        with TempData() as d:
            pl = plistlib.loads(install.render_plist(["/launcher"], 300, ["/opt/tools/bin", "/usr/bin"], d))
            path = pl["EnvironmentVariables"]["PATH"].split(":")
            self.assertEqual(path[0], "/opt/tools/bin")
            self.assertIn("/bin", path)
            self.assertEqual(len(path), len(set(path)))

    def test_print_command_is_absolute(self):
        cmd = install.command_for_user()
        self.assertTrue(cmd.startswith('"/'))
        self.assertTrue(cmd.endswith('/bin/ap" install'))


class InstallCheckTest(unittest.TestCase):
    def _check(self, plist_exists, launchctl_rc):
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        with TempData() as d:
            plist = d / "LaunchAgents" / "com.autopilot.gate.plist"
            if plist_exists:
                plist.parent.mkdir(parents=True)
                plist.write_text("x")
            buf = io.StringIO()
            with mock.patch.object(install, "PLIST", plist), \
                    mock.patch.object(install, "_launchctl", return_value=(launchctl_rc, "")), redirect_stdout(buf):
                rc = install._install(["--check"])
            return rc, buf.getvalue()

    def test_check_missing_plist(self):
        rc, out = self._check(False, 0)
        self.assertEqual(rc, 1)
        self.assertIn("not installed", out)

    def test_check_plist_not_loaded(self):
        rc, out = self._check(True, 113)
        self.assertEqual(rc, 1)
        self.assertIn("not installed", out)

    def test_check_installed(self):
        rc, out = self._check(True, 0)
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "installed")


class LauncherTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("clang"), "needs clang")
    def test_launcher_runs_only_ap_gate_and_returns_its_code(self):
        with TempData() as d:
            script = d / "dir with \"quote\"" / "ap"
            script.parent.mkdir()
            marker = d / "args.txt"
            script.write_text(f'#!/bin/sh\necho "$@" > "{marker}"\nexit 3\n')
            script.chmod(0o755)
            dest = install.build_launcher(str(script), d / "out" / "autopilot-gate")
            rc = subprocess.run([str(dest), "--other", "x"]).returncode
            self.assertEqual(rc, 3)
            self.assertEqual(marker.read_text().strip(), "gate")

    def _install(self, launcher):
        with TempData() as d:
            plist = d / "LaunchAgents" / "com.autopilot.gate.plist"
            with mock.patch.object(install, "PLIST", plist), \
                    mock.patch.object(install, "build_launcher", return_value=launcher), \
                    mock.patch.object(install, "_launchctl", return_value=(0, "")):
                note = install.install()
            return plistlib.loads(plist.read_bytes())["ProgramArguments"], note

    def test_install_runs_gate_through_launcher(self):
        argv, _ = self._install(Path("/x/autopilot-gate"))
        self.assertEqual(argv, ["/x/autopilot-gate"])

    def test_install_without_compiler_runs_ap_directly_and_says_so(self):
        argv, note = self._install(None)
        self.assertEqual(argv[1:], ["gate"])
        self.assertTrue(argv[0].endswith("/bin/ap"))
        self.assertIn("cloud", note)

    def test_uninstall_removes_launcher(self):
        with TempData() as d:
            launcher = d / "autopilot-gate"
            launcher.write_text("x")
            with mock.patch.object(install, "PLIST", d / "none.plist"), \
                    mock.patch.object(install, "LAUNCHER", launcher), \
                    mock.patch.object(install, "_launchctl", return_value=(0, "")), \
                    mock.patch("sys.stdout"):
                install._uninstall([])
            self.assertFalse(launcher.exists())


if __name__ == "__main__":
    unittest.main()
