from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

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
        self.assertEqual(argv[argv.index("--settings") + 1], str(self.data / "checkout" / "settings.json"))
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
        self.assertIn("rm abcd1234", self.log.read_text())


if __name__ == "__main__":
    unittest.main()
