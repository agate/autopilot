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


if __name__ == "__main__":
    unittest.main()
