from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from tests.helpers import ROOT, TempData, write_ap
from autopilot import config, dispatch, gate, ops, paths, state, workers

FAKE_CLAUDE = str(ROOT / "tests" / "bin" / "fake-claude")
FAKE_GH = str(ROOT / "tests" / "bin" / "fake-gh")


def src(id):
    return {"id": id, "title": {"en": id}, "depends_on": [], "status": "todo", "link": "", "needs": [],
            "critical": False, "phase": 1}


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.td = TempData()
        self.data = self.td.__enter__()
        self.tmp = Path(tempfile.mkdtemp())
        repo = self.tmp / "repo"
        repo.mkdir()
        write_ap(self.data, "checkout", repo=str(repo))
        g = config.load_global()
        g["tools"].update(claude=FAKE_CLAUDE, gh=FAKE_GH)
        config.save_global(g)
        ops.tasks_sync("checkout", [src("PROJ-123"), src("PROJ-124"), src("PROJ-125")])
        self.agents = self.tmp / "agents.json"
        self._agents([])
        self.hook = self.tmp / "hook"
        self.env = {"FAKE_CLAUDE_LOG": str(self.tmp / "log"), "FAKE_AGENTS_JSON": str(self.agents),
                    "FAKE_BG_HOOK": str(self.hook)}
        os.environ.update(self.env)
        self.notes = []
        self._orig_notify = gate.notify
        gate.notify = self.notes.append

    def tearDown(self):
        gate.notify = self._orig_notify
        for k in self.env:
            os.environ.pop(k, None)
        self.td.__exit__(None, None, None)

    def _agents(self, rows):
        self.agents.write_text(json.dumps(rows))

    def _dispatcher_writes(self, started, free, ready):
        ap = str(ROOT / "bin" / "ap")
        starts = "".join(f"\"{ap}\" event checkout worker_started '{{}}'; " for _ in range(started))
        self.hook.write_text(f"#!/bin/bash\ncase \"$*\" in *'autopilot · dispatch'*) {starts}"
                             f"\"{ap}\" pass end --started {started} --free {free} --ready {ready} --allow none ;; esac\n")
        self.hook.chmod(0o755)
        self._agents([{"id": "abcd1234", "name": "autopilot · dispatch", "kind": "background", "status": "idle",
                       "state": "done", "pid": 1, "startedAt": 1}])

    def _run(self, now=None):
        calls = []

        def fn(g, aps):
            calls.append(aps)
            return dispatch.run(g, aps, poll_s=0)

        gate.run(now=now, dispatch_fn=fn)
        return calls

    def test_gate_dispatches_then_idle_check_passes(self):
        self._dispatcher_writes(started=2, free=5, ready=3)
        self.assertEqual(self._run(), [["checkout"]])
        self.assertEqual(state.load("checkout")["decisions"], [])
        self.assertIn("last_pass_at", json.loads((self.data / "gate.json").read_text()))
        self.assertIn("rm abcd1234", (self.tmp / "log").read_text())

    def test_gate_stuck_twice(self):
        self._dispatcher_writes(started=0, free=5, ready=3)
        self.assertEqual(len(self._run()), 2)
        decs = state.load("checkout")["decisions"]
        self.assertEqual([d["title"] for d in decs], [{"key": "dispatcher_stuck"}])
        self.assertTrue(self.notes)

    def test_worker_done_triggers_dispatch(self):
        self._dispatcher_writes(started=1, free=5, ready=3)
        now = datetime.now().astimezone()
        self._run(now)
        workers.start("checkout", "PROJ-123", "implement", str(self.tmp), "do it")
        self._run(now + timedelta(minutes=1))
        self.assertEqual(self._run(now + timedelta(minutes=2)), [])
        run = paths.ap_dir("checkout") / "runs" / "PROJ-123"
        run.mkdir(parents=True)
        (run / "handback.json").write_text("{}")
        self.assertEqual(self._run(now + timedelta(minutes=3)), [["checkout"]])

    def test_dead_worker_seen(self):
        workers.start("checkout", "PROJ-123", "implement", str(self.tmp), "do it")
        fp = gate.fingerprint("checkout", config.load_ap("checkout"), state.load("checkout"), [],
                              datetime.now().astimezone(), FAKE_GH, 4, config.load_global())
        self.assertEqual(fp["workers"], ["checkout · PROJ-123 · implement=dead"])


if __name__ == "__main__":
    unittest.main()
