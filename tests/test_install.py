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


if __name__ == "__main__":
    unittest.main()
