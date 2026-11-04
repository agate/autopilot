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


class DoctorLoginTest(unittest.TestCase):
    def _login_check(self, status_out, rc=0):
        from unittest import mock
        with TempData():
            g = config.load_global()
            g["tools"]["claude"] = "/bin/echo"
            config.save_global(g)
            def run(argv, timeout=30):
                return (rc, status_out) if argv[1:3] == ["auth", "status"] else (0, "")
            with mock.patch.object(doctor, "_run", side_effect=run), \
                    mock.patch.object(doctor, "_run_in", return_value=(0, "")):
                return {c["name"]: c for c in doctor.checks(config.load_global())}["claude login"]

    def _status(self, hours):
        from datetime import datetime, timedelta, timezone
        end = (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return '{"loggedIn": true, "authMethod": "claude.ai", "loginExpiresAt": "%s"}' % end

    def test_login_ending_soon_fails_with_date(self):
        c = self._login_check(self._status(30))
        self.assertFalse(c["ok"])
        self.assertIn("ends", c["detail"])
        self.assertIn("claude auth login", c["fix"])

    def test_login_far_off_passes_and_shows_date(self):
        c = self._login_check(self._status(24 * 6))
        self.assertTrue(c["ok"])
        self.assertIn("ends", c["detail"])

    def test_logged_out_fails(self):
        c = self._login_check('{"loggedIn": false}')
        self.assertFalse(c["ok"])
        self.assertIn("claude auth login", c["fix"])

    def test_text_output_falls_back_to_exit_code(self):
        self.assertTrue(self._login_check("Logged in")["ok"])


if __name__ == "__main__":
    unittest.main()
