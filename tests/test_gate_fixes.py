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
from autopilot import cli, config, dispatch, events, gate, lock, ops, paths, state

FAKE = str(ROOT / "tests" / "bin" / "fake-claude")
OK_BUDGET = {"allow": None, "paused_until": None, "reason": "ok"}


def src(id, needs=()):
    return {"id": id, "title": {"en": id}, "depends_on": [], "status": "todo", "link": "", "needs": list(needs),
            "critical": False, "phase": 1}


def rollup(started, completed, conclusion="SUCCESS", status="COMPLETED"):
    return {"headRefOid": "abc", "reviewDecision": "REVIEW_REQUIRED",
            "statusCheckRollup": [{"name": "build", "status": status, "conclusion": conclusion,
                                   "startedAt": started, "completedAt": completed}],
            "latestReviews": [{"author": {"login": "teammate"}, "state": "COMMENTED", "commit": {"oid": "abc"},
                               "submittedAt": started}]}


class Base(unittest.TestCase):
    def setUp(self):
        self.td = TempData()
        self.data = self.td.__enter__()
        self.tmp = Path(tempfile.mkdtemp())
        self.notes = []
        self.patches = [mock.patch.object(gate, "_sessions", lambda g: []),
                        mock.patch.object(gate, "notify", self.notes.append),
                        mock.patch.object(gate, "_allowance", lambda g, now: dict(OK_BUDGET))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.td.__exit__(None, None, None)

    def ap(self, name, **cfg):
        repo = self.tmp / f"repo-{name}"
        repo.mkdir(exist_ok=True)
        cfg.setdefault("repo", str(repo))
        return write_ap(self.data, name, **cfg)


def fake_pass(started=0, end=True, during=None):
    """A dispatch_fn standing in for a pass: optional side effect, worker_started events, last_pass numbers."""
    calls = []

    def fn(g, aps):
        calls.append(list(aps))
        ops.pass_begin()
        time.sleep(0.01)
        if during:
            during()
        for a in aps[:1]:
            for _ in range(started):
                events.append(a, "worker_started", {})
        if end:
            ops.pass_end(started=started, free=5, ready=1, allow=None)
        return [json.loads((paths.data_root() / "last_pass.json").read_text())]
    fn.calls = calls
    return fn


class TickTest(Base):
    def test_tick_queues_instead_of_running(self):
        self.ap("alpha")
        with mock.patch("autopilot.install.kick") as kick, \
                mock.patch.object(dispatch, "run", side_effect=AssertionError("tick must not run a pass")):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(cli.main(["tick", "alpha"]), 0)
        self.assertEqual(json.loads((self.data / "tick.json").read_text())["aps"], ["alpha"])
        kick.assert_called_once()

    def test_gate_runs_queued_tick(self):
        self.ap("alpha")
        fn = fake_pass()
        gate.run(dispatch_fn=fn)
        (self.data / "tick.json").write_text(json.dumps({"aps": ["alpha"]}))
        gate.run(dispatch_fn=fn)
        self.assertEqual(len(fn.calls), 2)
        self.assertFalse((self.data / "tick.json").exists())

    def test_a_tick_queued_during_a_pass_survives_it(self):
        # A pass queues the next pass for an answer that arrived late; the gate must not wipe that note.
        self.ap("alpha")
        gate.run(dispatch_fn=fake_pass())

        def queue_next():
            (self.data / "tick.json").write_text(json.dumps({"aps": ["alpha"], "at": "2999-01-01T00:00:00Z"}))
        fn = fake_pass(during=queue_next)
        (self.data / "tick.json").write_text(json.dumps({"aps": ["alpha"], "at": "2000-01-01T00:00:00Z"}))
        gate.run(dispatch_fn=fn)
        self.assertTrue((self.data / "tick.json").exists())
        (self.data / "tick.json").write_text(json.dumps({"aps": ["alpha"], "at": "2000-01-01T00:00:00Z"}))
        nxt = fake_pass()
        gate.run(dispatch_fn=nxt)
        self.assertEqual(nxt.calls, [["alpha"]])
        self.assertFalse((self.data / "tick.json").exists())


class DispatchGuardTest(Base):
    def setUp(self):
        super().setUp()
        g = config.load_global()
        g["tools"]["claude"] = FAKE
        g["tools"]["gh"] = str(ROOT / "tests" / "bin" / "fake-gh")  # the pass's access check
        config.save_global(g)
        self.log = self.tmp / "log"
        self.agents = self.tmp / "agents.json"
        os.environ.update(FAKE_CLAUDE_LOG=str(self.log), FAKE_AGENTS_JSON=str(self.agents))
        self.addCleanup(os.environ.pop, "FAKE_CLAUDE_LOG", None)
        self.addCleanup(os.environ.pop, "FAKE_AGENTS_JSON", None)

    def test_refuses_second_dispatcher(self):
        self.ap("alpha")
        self.agents.write_text(json.dumps([{"id": "old11111", "name": "autopilot · dispatch", "kind": "background",
                                            "status": "busy", "state": "working", "pid": os.getpid(), "startedAt": 1}]))
        res = dispatch.run(config.load_global(), ["alpha"], poll_s=0)
        self.assertEqual(res[0]["skipped"], "dispatcher_running")
        self.assertNotIn("--bg", self.log.read_text() if self.log.exists() else "")

    def test_cleanup_runs_when_polling_fails(self):
        self.ap("alpha")
        self.agents.write_text("[]")
        real = dispatch.sessions.list_all
        calls = {"n": 0}

        def flaky(b):
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("listing broke")
            return real(b)
        with mock.patch.object(dispatch.sessions, "list_all", flaky):
            res = dispatch.run(config.load_global(), ["alpha"], poll_s=0)
        self.assertIn("error", res[0])
        self.assertIn("stop abcd1234", self.log.read_text())

    def test_blocked_dispatcher_stops_early(self):
        self.ap("alpha")
        self.agents.write_text(json.dumps([{"id": "abcd1234", "name": "autopilot · dispatch", "kind": "background",
                                            "status": "waiting", "state": "blocked", "pid": os.getpid(), "startedAt": 5}]))
        with mock.patch.object(dispatch, "_live_dispatcher", return_value=None):
            res = dispatch.run(config.load_global(), ["alpha"], poll_s=0, timeout_s=60)
        self.assertTrue(res[0]["blocked"])

    def test_one_group_failing_does_not_stop_the_next(self):
        self.ap("alpha")
        self.ap("beta")
        launched = []

        def launch(argv, cwd, env):
            launched.append(cwd)
            if "repo-alpha" in cwd:
                raise OSError("boom")
            return "abcd1234"
        self.agents.write_text(json.dumps([{"id": "abcd1234", "name": "autopilot · dispatch", "kind": "background",
                                            "status": "idle", "state": "done", "pid": 1, "startedAt": 9}]))
        with mock.patch.object(dispatch.workers, "_launch", launch), \
                mock.patch.object(dispatch, "_live_dispatcher", return_value=None):
            res = dispatch.run(config.load_global(), ["alpha", "beta"], poll_s=0)
        self.assertEqual(len(launched), 2)
        self.assertIn("error", res[0])


class PrFingerprintTest(unittest.TestCase):
    def test_same_when_only_timestamps_move(self):
        a = gate.pr_summary(rollup("2026-01-01T10:00:00Z", None, conclusion="", status="IN_PROGRESS"))
        b = gate.pr_summary(rollup("2026-01-01T10:00:00Z", None, conclusion="", status="QUEUED"))
        c = gate.pr_summary(rollup("2026-01-01T10:05:00Z", None, conclusion="", status="IN_PROGRESS"))
        self.assertEqual(a, b)
        self.assertEqual(a, c)

    def test_changes_when_ci_turns_green(self):
        pending = gate.pr_summary(rollup("t", None, conclusion="", status="IN_PROGRESS"))
        green = gate.pr_summary(rollup("t", "t2"))
        self.assertNotEqual(pending, green)

    def test_task_with_live_worker_not_polled(self):
        with TempData() as d:
            write_ap(d, "alpha")
            ops.tasks_sync("alpha", [src("PROJ-1")])
            ops.task_set("alpha", "PROJ-1", {"status": "in_review", "pr": 5})
            with state.mutate("alpha") as st:
                st["workers"].append({"task": "PROJ-1", "role": "pr", "session_name": "alpha · PROJ-1 · pr",
                                      "session_id": "s", "short_id": "x", "ports": [7100, 7109], "needs": [],
                                      "started_at": "2026-01-01T00:00:00Z", "resumes": 0})
            with mock.patch.object(gate, "pr_fingerprint", side_effect=AssertionError("polled")):
                fp = gate.fingerprint("alpha", config.load_ap("alpha"), state.load("alpha"), [],
                                      datetime.now().astimezone(), "gh", 4, config.load_global())
            self.assertEqual(fp["prs"], {})


class RobustnessTest(Base):
    def _broken_then_check(self, breaker):
        self.ap("alpha")
        b = self.ap("beta")
        breaker(b)
        fn = fake_pass()
        self.assertEqual(gate.run(dispatch_fn=fn), 0)
        self.assertEqual(fn.calls, [["alpha"]])
        self.assertIn("beta", json.loads((self.data / "gate.json").read_text())["broken"])

    def test_tasks_not_objects(self):
        self._broken_then_check(lambda d: (d / "state.json").write_text(json.dumps(dict(state.empty(), tasks=[1]))))

    def test_option_not_object(self):
        def brk(d):
            st = state.empty()
            st["decisions"] = [{"id": "D1", "kind": "ask", "title": {"en": "t"}, "body": {"en": "b"}, "options": ["x"],
                                "links": [], "status": "open", "created_at": "x"}]
            (d / "state.json").write_text(json.dumps(st))
        self._broken_then_check(brk)

    def test_corrupt_config(self):
        self._broken_then_check(lambda d: (d / "config.json").write_text("{nope"))

    def test_bad_report_time(self):
        self._broken_then_check(lambda d: (d / "config.json").write_text(json.dumps(
            {"name": "beta", "repo": str(d), "source": {"preset": "idea"}, "report_times": ["9am"]})))

    def test_watch_without_cmd(self):
        self._broken_then_check(lambda d: (d / "config.json").write_text(json.dumps(
            {"name": "beta", "repo": str(d), "source": {"preset": "idea"}, "watches": [{"name": "x"}]})))

    def test_missing_repo(self):
        self._broken_then_check(lambda d: (d / "config.json").write_text(json.dumps(
            {"name": "beta", "source": {"preset": "idea"}})))


class PassResultsTest(Base):
    def test_result_written_during_pass_is_seen_next_run(self):
        self.ap("alpha", report_times=[])
        ops.tasks_sync("alpha", [src("PROJ-1")])
        now = datetime.now().astimezone()
        gate.run(now=now, dispatch_fn=fake_pass(started=1))

        def write_handback():
            run = paths.ap_dir("alpha") / "runs" / "PROJ-1"
            run.mkdir(parents=True, exist_ok=True)
            (run / "handback.json").write_text("{}")
        (self.data / "tick.json").write_text(json.dumps({"aps": ["alpha"]}))
        gate.run(now=now + timedelta(minutes=1), dispatch_fn=fake_pass(started=1, during=write_handback))
        nxt = fake_pass(started=1)
        gate.run(now=now + timedelta(minutes=2), dispatch_fn=nxt)
        self.assertEqual(nxt.calls, [["alpha"]])

    def test_forced_pass_timer_per_autopilot(self):
        self.ap("alpha", report_times=[])
        self.ap("beta", report_times=[])
        now = datetime.now().astimezone()
        fn = fake_pass()
        gate.run(now=now, dispatch_fn=fn)
        (self.data / "tick.json").write_text(json.dumps({"aps": ["alpha"]}))
        gate.run(now=now + timedelta(minutes=20), dispatch_fn=fn)
        later = fake_pass()
        gate.run(now=now + timedelta(minutes=31), dispatch_fn=later)
        self.assertEqual(later.calls, [["beta"]])


class IdleCheckTest(Base):
    def test_resource_blocked_work_is_not_stuck(self):
        self.ap("alpha", resources=["emulator"])
        ops.tasks_sync("alpha", [src("PROJ-1", needs=["emulator"]), src("PROJ-2", needs=["emulator"])])
        with state.mutate("alpha") as st:
            st["workers"].append({"task": "PROJ-1", "role": "implement", "session_name": "alpha · PROJ-1 · implement",
                                  "session_id": "s", "short_id": "x", "ports": [7100, 7109], "needs": ["emulator"],
                                  "started_at": "2026-01-01T00:00:00Z", "resumes": 0})
        fn = fake_pass(started=0)
        gate.run(dispatch_fn=fn)
        self.assertEqual(len(fn.calls), 1)
        self.assertEqual(state.load("alpha")["decisions"], [])

    def test_pass_without_end_counts_as_failed(self):
        self.ap("alpha")
        ops.tasks_sync("alpha", [src("PROJ-1")])
        fn = fake_pass(started=1, end=False)
        gate.run(dispatch_fn=fn)
        self.assertEqual(len(fn.calls), 2)
        self.assertEqual(len(state.load("alpha")["decisions"]), 1)

    def test_blocked_dispatcher_notifies(self):
        self.ap("alpha")
        gate.run(dispatch_fn=lambda g, aps: [{"blocked": True, "aps": aps}])
        self.assertTrue(any("permission" in n for n in self.notes))
        self.assertIn("dispatcher_blocked", (self.data / "alpha" / "events.jsonl").read_text())

    def test_blocked_message_says_the_session_is_waiting(self):
        self.ap("alpha")
        gate.run(dispatch_fn=lambda g, aps: [{"blocked": True, "aps": aps}])
        self.assertTrue(any("is waiting at a permission prompt" in n and "claude attach" in n for n in self.notes))


class BudgetGateTest(Base):
    def test_credit_pause_only_dispatches_due_reports(self):
        self.ap("alpha", report_times=["00:00"])
        self.ap("beta", report_times=["00:00"])
        with state.mutate("beta") as st:
            st["last_report"] = "2999-01-01-00"
        with mock.patch.object(gate, "_allowance", lambda g, now: {"allow": 0, "paused_until": None, "reason": "credits"}):
            fn = fake_pass()
            gate.run(dispatch_fn=fn)
        self.assertEqual(fn.calls, [["alpha"]])

    def test_window_pause_skips_forced_passes(self):
        self.ap("alpha", report_times=[])
        now = datetime.now().astimezone()
        gate.run(now=now, dispatch_fn=fake_pass())
        until = (now + timedelta(hours=2)).isoformat()
        with mock.patch.object(gate, "_allowance", lambda g, n: {"allow": 0, "paused_until": until, "reason": "window"}):
            fn = fake_pass()
            gate.run(now=now + timedelta(minutes=40), dispatch_fn=fn)
        self.assertEqual(fn.calls, [])
        self.assertEqual(json.loads((self.data / "gate.json").read_text())["paused_until"], until)


if __name__ == "__main__":
    unittest.main()
