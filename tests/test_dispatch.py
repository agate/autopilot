from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT, TempData, write_ap
from autopilot import config, dispatch, lock, paths

FAKE = str(ROOT / "tests" / "bin" / "fake-claude")


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.td = TempData()
        self.data = self.td.__enter__()
        g = config.load_global()
        g["tools"]["claude"] = FAKE
        config.save_global(g)
        self.tmp = Path(tempfile.mkdtemp())
        self.log = self.tmp / "log"
        self.agents = self.tmp / "agents.json"
        os.environ["FAKE_CLAUDE_LOG"] = str(self.log)
        os.environ["FAKE_AGENTS_JSON"] = str(self.agents)

    def tearDown(self):
        for k in ("FAKE_CLAUDE_LOG", "FAKE_AGENTS_JSON"):
            os.environ.pop(k, None)
        self.td.__exit__(None, None, None)

    def _idle_dispatcher(self):
        self.agents.write_text(json.dumps([{"id": "abcd1234", "name": "autopilot · dispatch", "kind": "background",
                                            "status": "idle", "state": "done", "pid": 1, "startedAt": 1}]))

    def test_groups_by_repo(self):
        write_ap(self.data, "alpha", repo="/repo/one")
        write_ap(self.data, "beta", repo="/repo/two")
        write_ap(self.data, "gamma", repo="/repo/one")
        self.assertEqual(dispatch.groups(["alpha", "beta", "gamma"]),
                         {"/repo/one": ["alpha", "gamma"], "/repo/two": ["beta"]})

    def test_build_command_fields(self):
        argv, env = dispatch.build_command(config.load_global(), "/repo/one", ["alpha", "gamma"])
        self.assertEqual(argv[:4], [FAKE, "--bg", "-n", "autopilot · dispatch"])
        self.assertEqual(argv[argv.index("--settings") + 1], str(paths.package_root() / "settings" / "dispatcher.json"))
        self.assertIn("alpha, gamma", argv[-1])
        self.assertIn(str(paths.package_root() / "RUNBOOK.md"), argv[-1])
        self.assertEqual(env["AUTOPILOT_ROLE"], "dispatcher")

    def test_run_reads_last_pass_and_cleans_up(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        write_ap(self.data, "alpha", repo=str(repo))
        self._idle_dispatcher()
        hook = self.tmp / "hook"
        hook.write_text('#!/bin/bash\necho \'{"started": 2, "free": 3, "ready": 2, "allow": null}\' > "$AUTOPILOT_DATA/last_pass.json"\n')
        hook.chmod(0o755)
        os.environ["FAKE_BG_HOOK"] = str(hook)
        try:
            res = dispatch.run(config.load_global(), ["alpha"], poll_s=0)
        finally:
            os.environ.pop("FAKE_BG_HOOK", None)
        self.assertEqual(res[0]["started"], 2)
        log = self.log.read_text()
        self.assertIn("stop abcd1234", log)
        self.assertIn("rm abcd1234", log)
        self.assertIsNone(lock.held(paths.data_root() / "lock"))

    def test_timeout_stops_session(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        write_ap(self.data, "alpha", repo=str(repo))
        self.agents.write_text(json.dumps([{"id": "abcd1234", "name": "autopilot · dispatch", "kind": "background",
                                            "status": "busy", "state": "working", "pid": 1, "startedAt": 1}]))
        with mock.patch.object(dispatch, "_live_dispatcher", return_value=None):
            res = dispatch.run(config.load_global(), ["alpha"], poll_s=0, timeout_s=0)
        self.assertTrue(res[0]["timeout"])
        self.assertIn("stop abcd1234", self.log.read_text())

    def test_lock_released_on_error(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        write_ap(self.data, "alpha", repo=str(repo))
        with mock.patch.object(dispatch.workers, "_launch", side_effect=RuntimeError("boom")):
            res = dispatch.run(config.load_global(), ["alpha"], poll_s=0)
        self.assertIn("boom", res[0]["error"])
        self.assertIsNone(lock.held(paths.data_root() / "lock"))

    def test_pass_begun_before_start(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        write_ap(self.data, "alpha", repo=str(repo))
        self._idle_dispatcher()
        (self.data / "last_pass.json").write_text(json.dumps({"started": 9}))
        res = dispatch.run(config.load_global(), ["alpha"], poll_s=0)
        self.assertNotIn("started", res[0])


    def _agents(self, status, st):
        self.agents.write_text(json.dumps([{"id": "abcd1234", "name": "autopilot · dispatch", "kind": "background",
                                            "status": status, "state": st, "pid": os.getpid(), "startedAt": 1}]))

    def _ending_hook(self):
        hook = self.tmp / "hook"
        hook.write_text('#!/bin/bash\necho \'{"begun_at": "x", "ended_at": "y", "started": 0}\' > "$AUTOPILOT_DATA/last_pass.json"\n')
        hook.chmod(0o755)
        os.environ["FAKE_BG_HOOK"] = str(hook)
        self.addCleanup(os.environ.pop, "FAKE_BG_HOOK", None)

    def test_pass_that_ended_is_finished_even_when_labelled_blocked(self):
        # A finished pass whose last line asks the user for something reads state "blocked", status "idle".
        repo = self.tmp / "repo"
        repo.mkdir()
        write_ap(self.data, "alpha", repo=str(repo))
        self._agents("idle", "blocked")
        self._ending_hook()
        with mock.patch.object(dispatch, "_live_dispatcher", return_value=None):
            res = dispatch.run(config.load_global(), ["alpha"], poll_s=0, timeout_s=60)
        self.assertNotIn("blocked", res[0])
        self.assertEqual(res[0]["ended_at"], "y")
        self.assertIn("rm abcd1234", self.log.read_text())

    def test_end_marker_wins_over_a_waiting_session(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        write_ap(self.data, "alpha", repo=str(repo))
        self._agents("waiting", "blocked")
        self._ending_hook()
        with mock.patch.object(dispatch, "_live_dispatcher", return_value=None):
            res = dispatch.run(config.load_global(), ["alpha"], poll_s=0, timeout_s=60)
        self.assertNotIn("blocked", res[0])

    def test_idle_blocked_label_without_end_is_not_a_prompt(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        write_ap(self.data, "alpha", repo=str(repo))
        self._agents("idle", "blocked")
        with mock.patch.object(dispatch, "_live_dispatcher", return_value=None):
            res = dispatch.run(config.load_global(), ["alpha"], poll_s=0, timeout_s=60)
        self.assertNotIn("blocked", res[0])
        self.assertNotIn("timeout", res[0])
        self.assertIn("rm abcd1234", self.log.read_text())

    def test_waiting_session_is_blocked_and_kept(self):
        # A real permission prompt reads status "waiting" (waitingFor "permission prompt"), state "blocked".
        repo = self.tmp / "repo"
        repo.mkdir()
        write_ap(self.data, "alpha", repo=str(repo))
        self._agents("waiting", "blocked")
        with mock.patch.object(dispatch, "_live_dispatcher", return_value=None):
            res = dispatch.run(config.load_global(), ["alpha"], poll_s=0, timeout_s=60)
        self.assertTrue(res[0]["blocked"])
        self.assertNotIn("rm abcd1234", self.log.read_text())


if __name__ == "__main__":
    unittest.main()
