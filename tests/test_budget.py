from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tests.helpers import TempData
from autopilot import budget, config

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)


def _write(percent=10, age_min=5, used=0, limit=20000, reached=False) -> Path:
    cache = {"cachedUsageUtilization": {
        "fetchedAtMs": NOW_MS - age_min * 60000,
        "utilization": {
            "limits": [{"kind": "session", "percent": percent, "resets_at": "2026-01-01T14:00:00+00:00"}],
            "extra_usage": {"used_credits": used, "monthly_limit": limit, "decimal_places": 2,
                            "spend_limit_reached": reached},
        }}}
    p = Path(tempfile.mkdtemp()) / "claude.json"
    p.write_text(json.dumps(cache))
    return p


class BudgetTest(unittest.TestCase):
    def setUp(self):
        self.td = TempData()
        self.td.__enter__()
        self.cfg = config.load_global()

    def tearDown(self):
        self.td.__exit__(None, None, None)

    def test_window_at_threshold_pauses(self):
        b = budget.read(_write(percent=85), now_ms=NOW_MS)
        a = budget.allowance(b, self.cfg, NOW)
        self.assertEqual(a["allow"], 0)
        self.assertEqual(a["paused_until"], "2026-01-01T14:05:00+00:00")

    def test_below_threshold_unlimited(self):
        b = budget.read(_write(percent=84), now_ms=NOW_MS)
        self.assertIsNone(budget.allowance(b, self.cfg, NOW)["allow"])

    def test_stale_allows_one(self):
        b = budget.read(_write(percent=10, age_min=61), now_ms=NOW_MS)
        self.assertTrue(b["stale"])
        self.assertEqual(budget.allowance(b, self.cfg, NOW)["allow"], 1)

    def test_credit_pct_pauses(self):
        b = budget.read(_write(used=18000, limit=20000), now_ms=NOW_MS)
        self.assertEqual(b["credits_used_usd"], 180.0)
        a = budget.allowance(b, self.cfg, NOW)
        self.assertEqual(a["allow"], 0)
        self.assertEqual(a["reason"], "credits")

    def test_missing_file_allows_one(self):
        b = budget.read(Path("/nonexistent/claude.json"), now_ms=NOW_MS)
        self.assertFalse(b["ok"])
        self.assertEqual(budget.allowance(b, self.cfg, NOW)["allow"], 1)


if __name__ == "__main__":
    unittest.main()
