from __future__ import annotations

import unittest

from tests.helpers import TempData
from autopilot import config, doctor


class DoctorTest(unittest.TestCase):
    def test_doctor_missing_tool(self):
        with TempData():
            g = config.load_global()
            g["tools"]["claude"] = "/nonexistent/claude"
            config.save_global(g)
            checks = {c["name"]: c for c in doctor.checks(config.load_global())}
            self.assertFalse(checks["tool claude"]["ok"])
            self.assertIn("ap install", checks["tool claude"]["fix"])


class DoctorSpecChecksTest(unittest.TestCase):
    def test_disk_mcp_and_page_checks_present(self):
        from unittest import mock
        from tests.helpers import write_ap
        with TempData() as d:
            write_ap(d, "checkout", repo=str(d))
            g = config.load_global()
            g["tools"]["claude"] = "/bin/echo"
            config.save_global(g)
            with mock.patch.object(doctor, "_run", return_value=(0, "x: ✓ Connected")), \
                    mock.patch.object(doctor, "_run_in", return_value=(0, "x: ✓ Connected")):
                names = {c["name"] for c in doctor.checks(config.load_global())}
        self.assertIn("disk space", names)
        self.assertIn("mcp servers checkout", names)
        self.assertIn("page published", names)


if __name__ == "__main__":
    unittest.main()
