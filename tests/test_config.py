from __future__ import annotations

import json
import unittest

from tests.helpers import TempData, write_ap
from autopilot import config, paths


class ConfigTest(unittest.TestCase):
    def test_defaults_when_no_file(self):
        with TempData():
            g = config.load_global()
            self.assertEqual(g["max_workers"], 5)
            self.assertEqual(g["port_base"], 7100)
            self.assertEqual(g["budget"]["window_pct"], 85)
            self.assertEqual(g["report_times"], ["09:00", "17:00", "21:00"])

    def test_file_overrides_deep_merge(self):
        with TempData() as d:
            (d / "config.json").write_text(json.dumps({"budget": {"window_pct": 70}}))
            g = config.load_global()
            self.assertEqual(g["budget"]["window_pct"], 70)
            self.assertEqual(g["budget"]["stale_min"], 60)

    def test_valid_name(self):
        for ok in ["checkout", "a-b"]:
            self.assertTrue(paths.valid_name(ok), ok)
        for bad in ["Checkout", "1x", "a_b", ""]:
            self.assertFalse(paths.valid_name(bad), bad)

    def test_active_excludes_paused_and_stopped(self):
        with TempData() as d:
            write_ap(d, "alpha")
            write_ap(d, "beta")
            write_ap(d, "gamma", stopped=True)
            (d / "beta" / "paused").write_text("")
            self.assertEqual(config.list_aps(), ["alpha", "beta", "gamma"])
            self.assertEqual(config.status_of("beta"), "paused")
            self.assertEqual(config.status_of("gamma"), "stopped")
            self.assertEqual(config.active_aps(), ["alpha"])


if __name__ == "__main__":
    unittest.main()
