from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT, TempData, write_ap
from autopilot import budget, cap, cli, config, gate, park, paths, profile, snapshot, spend, state, workers

DAY = datetime(2026, 3, 10, 15, 0)          # naive = the computer's local clock
URL = "https://llm-gateway.example.com/anthropic"


def usage_file(pct: int) -> Path:
    p = Path(tempfile.mkdtemp()) / "claude.json"
    p.write_text(json.dumps({"cachedUsageUtilization": {
        "fetchedAtMs": int(time.time() * 1000),
        "utilization": {"limits": [{"kind": "session", "percent": pct, "resets_at": "2099-01-01T00:00:00+00:00"}],
                        "extra_usage": {"used_credits": 0, "monthly_limit": 20000, "decimal_places": 2}}}}))
    return p


def totals(today=0.0, unknown=False, unknown_today=None):
    """`unknown` is the month-wide gap; the cap trips on `unknown_today`, which defaults to the same value."""
    return {"today": today, "week": today, "month": today, "unknown": unknown,
            "unknown_today": unknown if unknown_today is None else unknown_today}


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
        self.assertEqual(cur["workers"], {"allow": 0, "paused_until": None, "reason": "cap", "cause": "spent"})
        self.assertEqual(cur["passes"]["reason"], "ok")
        self.assertEqual(cap.state(DAY)["card"], "open")

    def test_card_opens_once_a_day(self):
        self.current(totals(500))
        cap.answer("raise50", DAY)
        self.current(totals(800))
        self.assertEqual(cap.state(DAY)["card"], "answered")

    def test_unknown_cost_counts_as_over(self):
        w = self.current(totals(1, unknown=True))["workers"]
        self.assertEqual((w["reason"], w["cause"]), ("cap", "unknown"))

    def test_spent_cause(self):
        self.assertEqual(self.current(totals(500))["workers"]["cause"], "spent")

    def test_unknown_from_an_earlier_day_does_not_block_today(self):
        w = self.current(totals(1, unknown=True, unknown_today=False))["workers"]
        self.assertEqual(w["reason"], "ok")

    def test_card_closes_when_the_condition_clears_and_can_reopen(self):
        self.current(totals(500))
        self.assertEqual(cap.state(DAY)["card"], "open")
        self.current(totals(10))
        self.assertIsNone(cap.state(DAY)["card"])
        self.current(totals(500))
        self.assertEqual(cap.state(DAY)["card"], "open")

    def test_an_answered_card_is_not_closed(self):
        self.current(totals(500))
        cap.answer("raise50", DAY)
        self.current(totals(10))
        self.assertEqual(cap.state(DAY)["card"], "answered")

    def test_budget_command_and_page_data_show_the_workers_allowance(self):
        import contextlib
        import io
        from autopilot import cli, publish
        buf = io.StringIO()
        with mock.patch.object(spend, "totals", return_value=totals(500)), contextlib.redirect_stdout(buf):
            cli.main(["budget"])
        out = json.loads(buf.getvalue())
        self.assertEqual(out["allowance"]["reason"], "cap")
        self.assertEqual(out["passes"]["reason"], "ok")
        with mock.patch.object(spend, "totals", return_value=totals(500)):
            meta = publish.collect([])["meta"]["budget"]
        self.assertEqual((meta["allowance"]["reason"], meta["passes"]["reason"]), ("cap", "ok"))

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


FAKE = str(ROOT / "tests" / "bin" / "fake-claude")


def add_worker(ap, task="T-1", short="abcd1234", sid="sid-1", **kw):
    w = {"task": task, "role": "implement", "session_name": f"{ap} · {task} · implement", "session_id": sid,
         "short_id": short, "ports": [7110, 7119], "needs": [], "started_at": "2026-03-10T00:00:00Z",
         "resumes": 0, "cwd": "/tmp"}
    w.update(kw)
    with state.mutate(ap) as st:
        st["workers"].append(w)


class PauseTest(Base):
    def setUp(self):
        super().setUp()
        g = config.load_global()
        g["tools"]["claude"] = FAKE
        config.save_global(g)
        self.gcfg = config.load_global()
        self.log = Path(tempfile.mkdtemp()) / "log"
        os.environ["FAKE_CLAUDE_LOG"] = str(self.log)

    def tearDown(self):
        os.environ.pop("FAKE_CLAUDE_LOG", None)
        super().tearDown()

    def test_pause_pauses_all_stops_and_parks(self):
        for n, short in (("alpha", "aaaa1111"), ("beta", "bbbb2222")):
            write_ap(self.data, n)
            add_worker(n, short=short)
        cap.answer("pause", DAY)
        log = self.log.read_text()
        for n, short in (("alpha", "aaaa1111"), ("beta", "bbbb2222")):
            self.assertIn(f"stop {short}", log)
            self.assertTrue((paths.ap_dir(n) / "paused").exists())
            w = state.load(n)["workers"][0]
            self.assertEqual(w["parked"]["note"], "paused at the daily cap")
            self.assertEqual(w["parked"]["kind"], "cap")
            self.assertTrue((paths.ap_dir(n) / "runs" / "T-1" / "parked.json").exists())
            with mock.patch.object(spend, "totals", return_value=totals(0)):
                self.assertFalse(park.resume_due(w, (DAY + timedelta(days=1)).astimezone(), self.gcfg,
                                                 budget.read(), n))

    def test_an_autopilot_the_user_already_paused_is_stopped_too(self):
        write_ap(self.data, "alpha")
        write_ap(self.data, "beta")
        add_worker("beta", short="bbbb2222")
        (paths.ap_dir("beta") / "paused").write_text("")
        cap.answer("pause", DAY)
        self.assertIn("stop bbbb2222", self.log.read_text())
        self.assertEqual(state.load("beta")["workers"][0]["parked"]["kind"], "cap")

    def test_a_stop_that_times_out_does_not_abort_the_pause(self):
        import subprocess
        for n, short in (("alpha", "aaaa1111"), ("beta", "bbbb2222")):
            write_ap(self.data, n)
            add_worker(n, short=short)
        real = subprocess.run

        def run(argv, *a, **kw):
            if argv[1:] == ["stop", "aaaa1111"]:
                raise subprocess.TimeoutExpired(argv, 60)
            return real(argv, *a, **kw)

        with mock.patch("autopilot.cap.subprocess.run", side_effect=run):
            cap.answer("pause", DAY)
        for n in ("alpha", "beta"):
            self.assertTrue((paths.ap_dir(n) / "paused").exists())
        self.assertEqual(state.load("beta")["workers"][0]["parked"]["kind"], "cap")
        self.assertIn("stop bbbb2222", self.log.read_text())
        ev = (paths.ap_dir("alpha") / "events.jsonl").read_text()
        self.assertIn("cap_stop_failed", ev)
        self.assertIn("timed out", ev)
        self.assertNotIn("worker_parked", ev)
        self.assertFalse((paths.ap_dir("alpha") / "runs" / "T-1" / "parked.json").exists())

    def test_a_failed_stop_leaves_the_worker_running_and_the_next_gate_tick_retries(self):
        import subprocess
        from autopilot import sessions
        write_ap(self.data, "alpha")
        add_worker("alpha", short="aaaa1111")
        real = subprocess.run
        with mock.patch("autopilot.cap.subprocess.run", side_effect=subprocess.TimeoutExpired("x", 60)):
            cap.answer("pause", DAY)
        w = state.load("alpha")["workers"][0]
        self.assertNotIn("parked", w)
        live = [{"id": "aaaa1111", "name": w["session_name"], "state": "working", "pid": os.getpid()}]
        self.assertEqual(sessions.classify(w, live, paths.ap_dir("alpha") / "runs" / "T-1"), "running")
        self.assertFalse(self.log.exists() and "stop aaaa1111" in self.log.read_text())
        with mock.patch.object(gate, "_sessions", lambda g: []), mock.patch.object(gate, "notify", lambda m: None), \
                mock.patch.object(spend, "totals", return_value=totals(0)):
            gate.run(dispatch_fn=lambda g, aps: [])
        self.assertIn("stop aaaa1111", self.log.read_text())
        self.assertEqual(state.load("alpha")["workers"][0]["parked"]["kind"], "cap")
        self.assertIn("worker_parked", (paths.ap_dir("alpha") / "events.jsonl").read_text())

    def test_a_hand_pause_is_not_retried(self):
        write_ap(self.data, "alpha")
        add_worker("alpha", short="aaaa1111")
        (paths.ap_dir("alpha") / "paused").write_text("")
        cap.retry_stops(DAY)
        self.assertNotIn("parked", state.load("alpha")["workers"][0])

    def test_a_stopped_autopilot_is_left_alone(self):
        write_ap(self.data, "alpha", stopped=True)
        add_worker("alpha")
        cap.answer("pause", DAY)
        self.assertFalse(self.log.exists() and "stop" in self.log.read_text())

    def test_after_the_user_resumes_the_worker_continues_its_own_session(self):
        write_ap(self.data, "alpha")
        add_worker("alpha", short="aaaa1111", sid="sid-7")
        cap.answer("pause", DAY)
        (paths.ap_dir("alpha") / "paused").unlink()
        with mock.patch.object(spend, "totals", return_value=totals(0)):
            w = state.load("alpha")["workers"][0]
            self.assertTrue(park.resume_due(w, (DAY + timedelta(days=1)).astimezone(), self.gcfg, budget.read(), "alpha"))
        workers.start("alpha", "T-1", "implement", str(self.data), "Continue.", resume=True)
        self.assertIn("--resume sid-7", self.log.read_text())
        self.assertNotIn("parked", state.load("alpha")["workers"][0])

    def test_cap_parked_text_does_not_read_as_a_quota_park(self):
        write_ap(self.data, "alpha")
        add_worker("alpha")
        cap.answer("pause", DAY)
        w = state.load("alpha")["workers"][0]
        self.assertIn("paused at the daily cap; resumes when you resume the autopilot", snapshot._parked(w))
        self.assertNotIn("quota", snapshot._parked(w))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["worker", "list", "alpha"])
        self.assertIn("paused at the daily cap; resumes when you resume the autopilot", buf.getvalue())
        self.assertNotIn("None", buf.getvalue())


class WorkerProfileTest(Base):
    def test_a_gateway_worker_is_not_parked_by_a_full_subscription_window_after_a_switch(self):
        budget.DEFAULT_PATH = usage_file(97)
        profile.use("subscription")
        b = budget.read()
        payload = {"tool_name": "Read", "tool_input": {}}
        self.assertIsNotNone(park.decide(payload, {"AUTOPILOT_ROLE": "worker"}, b, self.gcfg))
        self.assertIsNone(park.decide(payload, {"AUTOPILOT_ROLE": "worker", "AUTOPILOT_PROFILE": "gw"}, b, self.gcfg))

    def test_workers_park_command_uses_the_workers_own_profile(self):
        budget.DEFAULT_PATH = usage_file(97)
        write_ap(self.data, "alpha")
        (paths.ap_dir("alpha") / "settings.json").unlink()   # no permission floor to re-merge on a switch
        add_worker("alpha")
        profile.use("subscription")
        with mock.patch.dict(os.environ, {"AUTOPILOT_PROFILE": "gw"}):
            with self.assertRaises(ValueError):
                workers.park("alpha", "T-1", "implement", "n", DAY.astimezone())


class ResumeProfileTest(Base):
    LATER = (DAY + timedelta(days=1)).astimezone()

    def parked(self, name):
        return {"profile": name, "task": "T-1", "role": "implement",
                "parked": {"kind": "cap", "resume_at": None, "reason": "cap"}}

    def test_a_gateway_worker_ignores_a_full_subscription_window_after_a_switch(self):
        budget.DEFAULT_PATH = usage_file(97)
        profile.use("subscription")
        with mock.patch.object(spend, "totals", return_value=totals(0)):
            self.assertTrue(park.resume_due(self.parked("gw"), self.LATER, self.gcfg, budget.read()))

    def test_a_subscription_worker_waits_for_the_window_after_a_switch_to_gateway(self):
        budget.DEFAULT_PATH = usage_file(97)
        with mock.patch.object(spend, "totals", return_value=totals(0)):
            self.assertFalse(park.resume_due(self.parked("subscription"), self.LATER, self.gcfg, budget.read()))
            self.assertTrue(park.resume_due(self.parked("gw"), self.LATER, self.gcfg, budget.read()))

    def test_a_worker_without_a_profile_uses_the_active_one(self):
        w = self.parked("gw")
        del w["profile"]
        budget.DEFAULT_PATH = usage_file(97)
        with mock.patch.object(spend, "totals", return_value=totals(0)):
            self.assertTrue(park.resume_due(w, self.LATER, self.gcfg, budget.read()))   # active = gateway
            profile.use("subscription")
            self.assertFalse(park.resume_due(w, self.LATER, self.gcfg, budget.read()))


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

    def test_no_new_workers_start_at_the_cap(self):
        write_ap(self.data, "alpha")
        with state.mutate("alpha") as st:
            st["tasks"].append({"id": "T-1", "title": {"en": "t"}, "depends_on": [], "status": "todo", "link": "",
                                "needs": [], "critical": False, "phase": 1, "origin": "source"})
        buf = io.StringIO()
        with mock.patch.object(spend, "totals", return_value=totals(500)), contextlib.redirect_stdout(buf):
            cli.main(["next", "alpha", "--json"])
        out = json.loads(buf.getvalue())
        self.assertEqual(out["ready_count"], 1)
        self.assertEqual((out["allow"], out["starts"]), (0, []))
        with mock.patch.object(spend, "totals", return_value=totals(10)), contextlib.redirect_stdout(io.StringIO()) as b2:
            cli.main(["next", "alpha", "--json"])
        self.assertEqual(json.loads(b2.getvalue())["starts"], [["alpha", "T-1"]])


if __name__ == "__main__":
    unittest.main()
