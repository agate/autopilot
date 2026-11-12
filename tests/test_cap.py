from __future__ import annotations

import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from tests.helpers import TempData, write_ap
from autopilot import budget, cap, config, gate, park, paths, profile, spend, state

DAY = datetime(2026, 3, 10, 15, 0)          # naive = the computer's local clock
URL = "https://llm-gateway.example.com/anthropic"


def usage_file(pct: int) -> Path:
    p = Path(tempfile.mkdtemp()) / "claude.json"
    p.write_text(json.dumps({"cachedUsageUtilization": {
        "fetchedAtMs": int(time.time() * 1000),
        "utilization": {"limits": [{"kind": "session", "percent": pct, "resets_at": "2099-01-01T00:00:00+00:00"}],
                        "extra_usage": {"used_credits": 0, "monthly_limit": 20000, "decimal_places": 2}}}}))
    return p


def totals(today=0.0, unknown=False):
    return {"today": today, "week": today, "month": today, "unknown": unknown}


class Base(unittest.TestCase):
    def setUp(self):
        self.td = TempData()
        self.data = self.td.__enter__()
        profile.save({"name": "gw", "mode": "gateway", "model": "m", "small_model": "s", "daily_cap_usd": 500,
                      "gateway": {"base_url": URL, "key_command": "echo k"}})
        profile.use("gw")
        self.gcfg = config.load_global()

    def tearDown(self):
        self.td.__exit__(None, None, None)

    def current(self, spent, now=DAY):
        with mock.patch.object(spend, "totals", return_value=spent):
            return budget.current(self.gcfg, now)


class CapTest(Base):
    def test_gateway_ignores_subscription_window(self):
        budget.DEFAULT_PATH = usage_file(97)
        cur = self.current(totals(10))
        self.assertEqual(cur["workers"]["reason"], "ok")
        self.assertIsNone(cur["workers"]["allow"])
        self.assertEqual(cur["passes"]["reason"], "window")
        self.assertIsNone(budget.parking(cur["budget"], self.gcfg, cur["profile"]))

    def test_cap_reached_blocks_new_work(self):
        cur = self.current(totals(500))
        self.assertEqual(cur["workers"], {"allow": 0, "paused_until": None, "reason": "cap"})
        self.assertEqual(cur["passes"]["reason"], "ok")
        self.assertEqual(cap.state(DAY)["card"], "open")

    def test_card_opens_once_a_day(self):
        self.current(totals(500))
        cap.answer("raise50", DAY)
        self.current(totals(800))
        self.assertEqual(cap.state(DAY)["card"], "answered")

    def test_unknown_cost_counts_as_over(self):
        self.assertEqual(self.current(totals(1, unknown=True))["workers"]["reason"], "cap")

    def test_subscription_mode_never_parses_spend(self):
        profile.use("subscription")
        with mock.patch.object(spend, "totals", side_effect=AssertionError("spend parsed")):
            cur = budget.current(config.load_global(), DAY)
        self.assertIsNone(cur["spent"])
        self.assertEqual(cur["workers"], cur["passes"])

    def test_raise_expires_at_local_midnight(self):
        cap.answer("raise50", DAY)
        self.assertEqual(cap.today_limit(profile.active(), DAY), 750)
        self.assertEqual(self.current(totals(600))["workers"]["reason"], "ok")
        later = datetime(2026, 3, 11, 0, 1)
        self.assertEqual(cap.today_limit(profile.active(), later), 500)
        self.assertEqual(cap.state(later), {"day": "2026-03-11", "raise_pct": 0, "card": None})

    def test_raise100(self):
        cap.answer("raise100", DAY)
        self.assertEqual(cap.today_limit(profile.active(), DAY), 1000)

    def test_unknown_option_refused(self):
        with self.assertRaises(ValueError):
            cap.answer("nope", DAY)


class PauseTest(Base):
    def test_pause_pauses_all_and_parks(self):
        for n in ("alpha", "beta"):
            write_ap(self.data, n)
            with state.mutate(n) as st:
                st["workers"].append({"task": "T-1", "role": "implement", "session_name": f"{n} · T-1 · implement",
                                      "session_id": "s", "short_id": "abcd1234", "ports": [], "needs": [],
                                      "started_at": "2026-03-10T00:00:00Z", "resumes": 0, "cwd": "/tmp"})
        cap.answer("pause", DAY)
        for n in ("alpha", "beta"):
            self.assertTrue((paths.ap_dir(n) / "paused").exists())
            w = state.load(n)["workers"][0]
            self.assertEqual(w["parked"]["note"], "paused at the daily cap")
            self.assertTrue((paths.ap_dir(n) / "runs" / "T-1" / "parked.json").exists())
            tomorrow = DAY + timedelta(days=1)
            with mock.patch.object(spend, "totals", return_value=totals(0)):
                self.assertFalse(park.resume_due(w, tomorrow.astimezone(), self.gcfg, budget.read(), n))
        (paths.ap_dir("alpha") / "paused").unlink()
        w = state.load("alpha")["workers"][0]
        with mock.patch.object(spend, "totals", return_value=totals(0)):
            self.assertTrue(park.resume_due(w, (DAY + timedelta(days=1)).astimezone(), self.gcfg, budget.read(), "alpha"))


class GateTest(Base):
    def test_reports_still_run_at_cap(self):
        write_ap(self.data, "alpha", report_times=["00:00"])
        calls = []
        with mock.patch.object(gate, "_sessions", lambda g: []), mock.patch.object(gate, "notify", lambda m: None), \
                mock.patch.object(spend, "totals", return_value=totals(500)):
            gate.run(dispatch_fn=lambda g, aps: calls.append(list(aps)) or
                     [{"started": 0, "free": 0, "ready": 0, "allow": 0, "ended_at": "x"}])
        self.assertEqual(calls, [["alpha"]])
        self.assertEqual(cap.state()["card"], "open")


if __name__ == "__main__":
    unittest.main()
