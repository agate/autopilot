from __future__ import annotations

import plistlib
import unittest
from pathlib import Path

from tests.helpers import ROOT, TempData
from autopilot import install


class InstallTest(unittest.TestCase):
    def test_plist_fields(self):
        with TempData() as d:
            pl = plistlib.loads(install.render_plist("/pkg/bin/ap", 300, ["/opt/tools/bin"], d))
            self.assertEqual(pl["Label"], "com.autopilot.gate")
            self.assertEqual(pl["ProgramArguments"], ["/pkg/bin/ap", "gate"])
            self.assertEqual(pl["StartInterval"], 300)
            self.assertTrue(pl["RunAtLoad"])
            self.assertEqual(pl["StandardOutPath"], str(d / "gate.log"))
            self.assertEqual(pl["EnvironmentVariables"]["AUTOPILOT_DATA"], str(d))

    def test_plist_path_has_captured_dirs(self):
        with TempData() as d:
            pl = plistlib.loads(install.render_plist("/pkg/bin/ap", 300, ["/opt/tools/bin", "/usr/bin"], d))
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


if __name__ == "__main__":
    unittest.main()
