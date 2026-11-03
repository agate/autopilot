from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from tests.helpers import ROOT, TempData, write_ap
from autopilot import cli, config, lock, ops, paths, state, workers

FAKE = str(ROOT / "tests" / "bin" / "fake-claude")


def src(id, status="todo"):
    return {"id": id, "title": {"en": id}, "depends_on": [], "status": status, "link": "", "needs": [],
            "critical": False, "phase": 1}


class RunbookOpsTest(unittest.TestCase):
    def setUp(self):
        self.td = TempData()
        self.data = self.td.__enter__()
        write_ap(self.data, "checkout")
        g = config.load_global()
        g["tools"]["claude"] = FAKE
        config.save_global(g)
        self.log = Path(tempfile.mkdtemp()) / "log"
        os.environ["FAKE_CLAUDE_LOG"] = str(self.log)
        ops.tasks_sync("checkout", [src("PROJ-123"), src("PROJ-124")])

    def tearDown(self):
        os.environ.pop("FAKE_CLAUDE_LOG", None)
        os.environ.pop("AUTOPILOT_ROLE", None)
        self.td.__exit__(None, None, None)

    def _out(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(argv)
        return rc, buf.getvalue()

    def test_end_keeps_session_for_resume(self):
        agents = Path(tempfile.mkdtemp()) / "agents.json"
        agents.write_text(json.dumps([{"id": "abcd1234", "name": "checkout · PROJ-123 · implement",
                                       "kind": "background", "sessionId": "sid-1", "startedAt": 1}]))
        os.environ["FAKE_AGENTS_JSON"] = str(agents)
        self.addCleanup(os.environ.pop, "FAKE_AGENTS_JSON", None)
        workers.start("checkout", "PROJ-123", "implement", str(self.data), "do it")
        workers.end("checkout", "PROJ-123", "implement")
        log = self.log.read_text()
        self.assertIn("stop abcd1234", log)
        self.assertNotIn("rm abcd1234", log)
        t = state.load("checkout")["tasks"][0]
        self.assertEqual(t["sessions"], {"implement": "sid-1"})

    def test_start_resume_continues_saved_session(self):
        st = state.load("checkout")
        st["tasks"][0]["sessions"] = {"implement": "sid-9"}
        state.save("checkout", st)
        workers.start("checkout", "PROJ-123", "implement", str(self.data), "fix these", resume=True)
        self.assertIn("--resume sid-9", self.log.read_text())

    def test_task_cleanup_removes_sessions(self):
        st = state.load("checkout")
        st["tasks"][0]["sessions"] = {"implement": "sid-9", "review": "sid-8"}
        state.save("checkout", st)
        self._out(["task", "cleanup", "checkout", "PROJ-123"])
        log = self.log.read_text()
        self.assertIn("rm sid-9", log)
        self.assertIn("rm sid-8", log)
        self.assertNotIn("sessions", state.load("checkout")["tasks"][0])

    def test_worker_list_shows_state(self):
        workers.start("checkout", "PROJ-123", "implement", str(self.data), "do it")
        run = paths.ap_dir("checkout") / "runs" / "PROJ-123"
        run.mkdir(parents=True)
        (run / "handback.json").write_text("{}")
        rc, out = self._out(["worker", "list", "checkout", "--json"])
        self.assertEqual(json.loads(out)[0]["state"], "done")

    def test_next_json_has_pass_numbers(self):
        rc, out = self._out(["next", "--json"])
        d = json.loads(out)
        self.assertEqual(d["free"], 5)
        self.assertEqual(d["ready_count"], 2)
        self.assertIn("allow", d)
        self.assertEqual(len(d["starts"]), 2)

    def test_report_due_command(self):
        rc, out = self._out(["report", "due", "checkout"])
        self.assertEqual(rc, 0)
        self.assertRegex(out.strip(), r"^(\d{4}-\d\d-\d\d-\d\d)?$")

    def test_talk_session_waits_for_lock(self):
        lock.acquire(paths.data_root() / "lock", "dispatcher")
        with self.assertRaises(TimeoutError):
            ops.task_set("checkout", "PROJ-123", {"status": "blocked"}, lock_wait_s=0)
        os.environ["AUTOPILOT_ROLE"] = "dispatcher"
        ops.task_set("checkout", "PROJ-123", {"status": "blocked"}, lock_wait_s=0)
        self.assertEqual(state.load("checkout")["tasks"][0]["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
