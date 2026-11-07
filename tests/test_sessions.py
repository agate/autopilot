from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT
from autopilot import sessions

ALL = json.loads((ROOT / "tests" / "fixtures" / "agents.json").read_text())


def worker(task, role, started_at="2026-01-01T00:00:00Z"):
    return {"task": task, "role": role, "session_name": sessions.name_for("checkout", task, role),
            "session_id": "x", "short_id": "x", "ports": [7100, 7109], "needs": [], "started_at": started_at, "resumes": 0}


class SessionsTest(unittest.TestCase):
    def test_name_for(self):
        self.assertEqual(sessions.name_for("checkout", "PROJ-123", "implement"), "checkout · PROJ-123 · implement")

    def test_classify_done_by_file(self):
        run = Path(tempfile.mkdtemp())
        (run / "handback.json").write_text("{}")
        self.assertEqual(sessions.classify(worker("PROJ-123", "implement"), ALL, run), "done")

    def test_result_file_older_than_start_ignored(self):
        run = Path(tempfile.mkdtemp())
        f = run / "handback.json"
        f.write_text("{}")
        old = time.time() - 86400 * 400
        os.utime(f, (old, old))
        self.assertEqual(sessions.classify(worker("PROJ-123", "implement", "2026-01-01T00:00:00Z"), ALL, run), "running")

    def test_classify_blocked(self):
        self.assertEqual(sessions.classify(worker("PROJ-124", "review"), ALL, Path(tempfile.mkdtemp())), "blocked")

    def test_classify_dead_missing(self):
        self.assertEqual(sessions.classify(worker("PROJ-125", "implement"), ALL, Path(tempfile.mkdtemp())), "dead")

    def test_classify_uses_name_after_id_change(self):
        self.assertEqual(sessions.classify(worker("PROJ-123", "implement"), ALL, Path(tempfile.mkdtemp())), "running")

    def test_find_ignores_interactive(self):
        self.assertIsNone(sessions.find("some interactive session", ALL))

    def test_list_all_bad_json_returns_none(self):
        d = Path(tempfile.mkdtemp())
        fake = d / "claude"
        fake.write_text("#!/bin/bash\necho not-json\n")
        fake.chmod(0o755)
        self.assertIsNone(sessions.list_all(str(fake)))


class WaitTurnTest(unittest.TestCase):
    """The wait for a throwaway background session. The listing's state is the CLI's own label for how the turn
    ended: a turn that replied FAILED has been seen as done, blocked and failed. Any of them ends the wait."""

    def wait(self, entry, timeout_s=1.0):
        agents = Path(tempfile.mkdtemp()) / "agents.json"
        agents.write_text(json.dumps([dict({"id": "abcd1234", "kind": "background", "sessionId": "s1"}, **entry)]))
        fake = str(ROOT / "tests" / "bin" / "fake-claude")
        with mock.patch.dict(os.environ, {"FAKE_AGENTS_JSON": str(agents)}):
            t = time.monotonic()
            s, why = sessions.wait_turn(fake, "abcd1234", timeout_s, poll_s=0.05)
        return s, why, time.monotonic() - t

    def test_an_ended_turn_ends_the_wait_whatever_its_label(self):
        for state in ("done", "blocked", "failed"):
            s, why, took = self.wait({"state": state, "status": "idle", "pid": 1})
            self.assertEqual(why, "", state)
            self.assertEqual(s["sessionId"], "s1")
            self.assertLess(took, 0.9)

    def test_a_session_whose_process_exited_ends_the_wait(self):
        for state in ("done", "blocked", "failed", "stopped"):
            self.assertEqual(self.wait({"state": state})[1], "", state)

    def test_a_working_session_is_waited_for(self):
        for status in ("busy", "idle", None):   # "idle" while still working is seen for a moment after the start
            s, why, took = self.wait({"state": "working", "status": status, "pid": 1}, timeout_s=0.3)
            self.assertEqual(why, "did not finish in time", status)
            self.assertGreaterEqual(took, 0.3)

    def test_a_permission_prompt_ends_the_wait_with_its_reason(self):
        # Nobody answers a background session's prompt, so waiting longer cannot help.
        s, why, took = self.wait({"state": "blocked", "status": "waiting", "pid": 1})
        self.assertEqual(why, "stopped at a permission prompt")
        self.assertLess(took, 0.9)


if __name__ == "__main__":
    unittest.main()
