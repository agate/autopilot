from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
