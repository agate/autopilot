from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT, TempData, write_ap
from autopilot import config, state, workers

FAKE = str(ROOT / "tests" / "bin" / "fake-claude")


class WorkersTest(unittest.TestCase):
    def setUp(self):
        self.td = TempData()
        self.data = self.td.__enter__()
        write_ap(self.data, "checkout")
        g = config.load_global()
        g["tools"]["claude"] = FAKE
        config.save_global(g)
        self.log = Path(tempfile.mkdtemp()) / "log"
        os.environ["FAKE_CLAUDE_LOG"] = str(self.log)

    def tearDown(self):
        os.environ.pop("FAKE_CLAUDE_LOG", None)
        os.environ.pop("FAKE_AGENTS_JSON", None)
        self.td.__exit__(None, None, None)

    def test_ports_first_slot(self):
        self.assertEqual(workers.ports(7100, 0), [7100, 7109])

    def test_next_slot_fills_gap(self):
        ws = [{"ports": [7100, 7109]}, {"ports": [7120, 7129]}]
        self.assertEqual(workers.next_slot(ws, 7100), 1)

    def test_build_start_implement(self):
        argv, env = workers.build_start(config.load_global(), config.load_ap("checkout"), "checkout",
                                        "PROJ-123", "implement", "do it")
        self.assertEqual(argv[0], FAKE)
        self.assertIn("--bg", argv)
        self.assertEqual(argv[argv.index("-n") + 1], "checkout · PROJ-123 · implement")
        self.assertEqual(argv[argv.index("--agent") + 1], "autopilot:implementer")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "auto")
        self.assertEqual(argv[argv.index("--settings") + 1], str(self.data / "projects" / "checkout" / "settings.json"))
        self.assertEqual(argv[-1], "do it")
        self.assertEqual(env["AUTOPILOT_ROLE"], "worker")
        self.assertEqual(env["AUTOPILOT_DATA"], str(self.data))
        self.assertNotIn("--model", argv)

    def test_build_start_pr_uses_command(self):
        argv, _ = workers.build_start(config.load_global(), config.load_ap("checkout"), "checkout",
                                      "PROJ-123", "pr", "https://example.com/pull/1")
        self.assertNotIn("--agent", argv)
        self.assertTrue(argv[-1].startswith("/autopilot:pr-loop "))

    def test_start_records_worker(self):
        agents = Path(tempfile.mkdtemp()) / "agents.json"
        agents.write_text(json.dumps([{"id": "abcd1234", "name": "checkout · PROJ-123 · implement",
                                       "kind": "background", "sessionId": "sid-1", "startedAt": 1}]))
        os.environ["FAKE_AGENTS_JSON"] = str(agents)
        rec = workers.start("checkout", "PROJ-123", "implement", str(self.data), "do it", needs=["emulator"])
        st = state.load("checkout")
        self.assertEqual(st["workers"][0]["short_id"], "abcd1234")
        self.assertEqual(st["workers"][0]["session_id"], "sid-1")
        self.assertEqual(st["workers"][0]["needs"], ["emulator"])
        self.assertEqual(rec["ports"], [7100, 7109])

    def test_end_removes_worker(self):
        workers.start("checkout", "PROJ-123", "implement", str(self.data), "do it")
        workers.end("checkout", "PROJ-123", "implement")
        self.assertEqual(state.load("checkout")["workers"], [])
        self.assertIn("stop abcd1234", self.log.read_text())

    def test_launch_reads_id_when_output_has_no_name(self):
        # A resumed copy prints the id with no name after it (seen 2026-11-04).
        out = ("backgrounded · 5aca4bb2\n  claude agents             list sessions\n"
               "  claude attach 5aca4bb2    open in this terminal\n")
        done = subprocess.CompletedProcess([], 0, stdout=out, stderr="")
        with mock.patch.object(workers.subprocess, "run", return_value=done):
            self.assertEqual(workers._launch(["claude"], str(self.data), {}), "5aca4bb2")

    def test_launch_reads_id_after_copy_note(self):
        out = ("note: background session f0e6bc79 keeps its own saved options, so the flags you passed "
               "started a copy as 079f331b.\nbackgrounded · 079f331b · checkout · PROJ-1 · implement\n")
        done = subprocess.CompletedProcess([], 0, stdout=out, stderr="")
        with mock.patch.object(workers.subprocess, "run", return_value=done):
            self.assertEqual(workers._launch(["claude"], str(self.data), {}), "079f331b")

    def test_start_resume_keeps_the_worker_name(self):
        # Resuming with flags starts a copy; without -n the copy gets an auto title.
        with state.mutate("checkout") as st:
            st["tasks"] = [{"id": "PROJ-1", "title": {"en": "t"}, "depends_on": [], "status": "in_progress", "link": "",
                            "needs": [], "critical": False, "phase": "", "origin": "source",
                            "sessions": {"implement": "sid-old"}}]
        with mock.patch.object(workers, "_launch", return_value="new11111") as launch:
            workers.start("checkout", "PROJ-1", "implement", str(self.data), "fix it", resume=True)
        argv = launch.call_args[0][0]
        self.assertEqual(argv[argv.index("--resume") + 1], "sid-old")
        self.assertEqual(argv[argv.index("-n") + 1], "checkout · PROJ-1 · implement")

    def test_resume_passes_only_name_and_settings(self):
        # --resume carries only -n (the worker's name) and --settings (the session must bill the current profile):
        # no model, effort or permission flags.
        rec = workers.start("checkout", "PROJ-1", "implement", str(self.data), "do it")
        with mock.patch.object(workers, "_launch", return_value="abcd1234") as launch:
            workers.resume("checkout", rec)
        argv = launch.call_args[0][0]
        self.assertEqual(argv[1:3], ["--bg", "--resume"])
        self.assertEqual(argv[6:8], ["--settings", str(self.data / "projects" / "checkout" / "settings.json")])
        self.assertEqual(len(argv), 9)


if __name__ == "__main__":
    unittest.main()
