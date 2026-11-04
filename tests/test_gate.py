from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta
from unittest import mock

from tests.helpers import TempData, write_ap
from autopilot import gate, lock, ops, paths, state


def src(id):
    return {"id": id, "title": {"en": id}, "depends_on": [], "status": "todo", "link": "", "needs": [],
            "critical": False, "phase": 1}


class ReportDueTest(unittest.TestCase):
    T = ["09:00", "17:00", "21:00"]

    def test_report_due_at_slot(self):
        self.assertEqual(gate.report_due(self.T, None, datetime(2026, 1, 2, 9, 1)), "2026-01-02-09")

    def test_report_not_due_before(self):
        self.assertIsNone(gate.report_due(self.T, "2026-01-01-21", datetime(2026, 1, 2, 8, 59)))

    def test_report_written_not_due(self):
        self.assertIsNone(gate.report_due(self.T, "2026-01-02-09", datetime(2026, 1, 2, 9, 30)))

    def test_report_due_after_offline_night(self):
        self.assertEqual(gate.report_due(self.T, "2026-01-01-17", datetime(2026, 1, 2, 8, 0)), "2026-01-01-21")


class DecideTest(unittest.TestCase):
    NOW = datetime(2026, 1, 2, 12, 0)

    def test_should_dispatch_same_recent(self):
        fp = {"a": 1}
        ok, _ = gate.should_dispatch(fp, dict(fp), self.NOW - timedelta(minutes=10), self.NOW, 30)
        self.assertFalse(ok)

    def test_should_dispatch_forced(self):
        fp = {"a": 1}
        ok, why = gate.should_dispatch(fp, dict(fp), self.NOW - timedelta(minutes=31), self.NOW, 30)
        self.assertTrue(ok)
        self.assertEqual(why, "forced")

    def test_should_dispatch_changed(self):
        ok, why = gate.should_dispatch({"files": 1, "ready": 1}, {"files": 2, "ready": 1},
                                       self.NOW - timedelta(minutes=1), self.NOW, 30)
        self.assertTrue(ok)
        self.assertIn("files", why)

    def test_idle_failed(self):
        self.assertTrue(gate.idle_failed({"started": 0, "free": 2, "ready": 1, "allow": None}))
        self.assertFalse(gate.idle_failed({"started": 1, "free": 2, "ready": 1, "allow": None}))
        self.assertFalse(gate.idle_failed({"started": 0, "free": 2, "ready": 1, "allow": 0}))
        self.assertFalse(gate.idle_failed({"started": 0, "free": 0, "ready": 1, "allow": None}))
        self.assertFalse(gate.idle_failed({"timeout": True}))


class RunTest(unittest.TestCase):
    def setUp(self):
        self.td = TempData()
        self.data = self.td.__enter__()
        self.p1 = mock.patch.object(gate, "_sessions", lambda g: [])
        self.p2 = mock.patch.object(gate, "notify", lambda msg: None)
        self.p3 = mock.patch.object(gate, "_allowance", lambda g, now: {"allow": None, "paused_until": None, "reason": "ok"})
        self.p1.start()
        self.p2.start()
        self.p3.start()

    def tearDown(self):
        self.p1.stop()
        self.p2.stop()
        self.p3.stop()
        self.td.__exit__(None, None, None)

    def test_broken_state_isolated(self):
        write_ap(self.data, "alpha")
        write_ap(self.data, "beta")
        (self.data / "beta" / "state.json").write_text("{broken")
        calls = []

        def fake(g, aps):
            calls.append(list(aps))
            return [{"started": 0, "free": 0, "ready": 0, "allow": None, "ended_at": "x"}]

        self.assertEqual(gate.run(dispatch_fn=fake), 0)
        self.assertEqual(calls, [["alpha"]])
        self.assertIn("state_broken", (self.data / "beta" / "events.jsonl").read_text())
        self.assertIn("beta", json.loads((self.data / "gate.json").read_text())["broken"])

    def test_lock_held_skips(self):
        write_ap(self.data, "alpha")
        lock.acquire(paths.data_root() / "lock", "someone")
        called = []
        self.assertEqual(gate.run(dispatch_fn=lambda g, a: called.append(a) or []), 0)
        self.assertEqual(called, [])

    def test_no_change_no_dispatch(self):
        write_ap(self.data, "alpha")
        calls = []
        fake = lambda g, aps: calls.append(aps) or [{"started": 0, "free": 0, "ready": 0, "allow": None, "ended_at": "x"}]
        gate.run(dispatch_fn=fake)
        gate.run(dispatch_fn=fake)
        self.assertEqual(len(calls), 1)

    def test_stuck_twice_adds_decision(self):
        write_ap(self.data, "alpha")
        ops.tasks_sync("alpha", [src("PROJ-123")])
        calls = []

        def fake(g, aps):
            calls.append(aps)
            return [{"started": 0, "free": 5, "ready": 1, "allow": None}]

        gate.run(dispatch_fn=fake)
        self.assertEqual(len(calls), 2)
        decs = state.load("alpha")["decisions"]
        self.assertEqual(len(decs), 1)
        self.assertEqual(decs[0]["title"], {"key": "dispatcher_stuck"})


if __name__ == "__main__":
    unittest.main()
