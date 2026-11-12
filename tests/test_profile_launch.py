from __future__ import annotations

import ast
import json
import subprocess
import unittest
from unittest import mock

from tests.helpers import ROOT, TempData, write_ap
from tests.test_profile_settings import URL, gateway, read, write_files
from autopilot import config, dispatch, paths, perms, profile, replies, refresh, spend, state, workers


def opt(argv, flag):
    return argv[argv.index(flag) + 1]


class EveryLaunchTest(unittest.TestCase):
    def test_every_launch_passes_settings(self):
        """A list literal that starts a session (--bg or -p) must also name --settings, so no new launch
        point can skip the profile."""
        seen = 0
        for f in sorted((ROOT / "lib" / "autopilot").glob("*.py")):
            for node in ast.walk(ast.parse(f.read_text())):
                if not isinstance(node, ast.List):
                    continue
                words = {e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}
                if words & {"--bg", "-p"}:
                    seen += 1
                    self.assertIn("--settings", words, f"{f.name}:{node.lineno} starts a session without --settings")
        self.assertGreaterEqual(seen, 6)


class LaunchBase(unittest.TestCase):
    def setUp(self):
        self.td = TempData()
        self.data = self.td.__enter__()
        write_ap(self.data, "a")
        self.addCleanup(self.td.__exit__, None, None, None)
        g = gateway()
        g.update(effort="high")
        g["model"] = "m1"
        profile.save(g)
        write_files("a")
        profile.use("g")


class LaunchTest(LaunchBase):
    def test_worker_argv_uses_profile_model_and_effort(self):
        argv, _ = workers.build_start(config.load_global(), config.load_ap("a"), "a", "T-1", "implement", "go")
        self.assertEqual(opt(argv, "--model"), "m1")
        self.assertEqual(opt(argv, "--effort"), "high")
        self.assertEqual(opt(argv, "--settings"), str(self.data / "projects" / "a" / "settings.json"))

    def test_reply_check_uses_small_model(self):
        sub = profile.load("subscription")
        sub["small_model"] = "haiku"
        profile.save(sub)
        seen = {}

        def launch(argv, cwd, env):
            seen["argv"] = argv
            raise RuntimeError("stop here")

        with mock.patch.object(workers, "_launch", launch):
            replies.check(config.load_global(), "https://x.example.com/a")
        self.assertEqual(opt(seen["argv"], "--model"), "haiku")
        self.assertEqual(opt(seen["argv"], "--settings"), str(self.data / "settings-light.json"))
        self.assertEqual(opt(seen["argv"], "--setting-sources"), "project")
        self.assertEqual(read(self.data / "settings-light.json")["env"]["ANTHROPIC_BASE_URL"], "")

    def test_refresh_uses_light_settings(self):
        seen = {}

        def launch(argv, cwd, env):
            seen["argv"] = argv
            raise RuntimeError("stop here")

        with mock.patch.object(workers, "_launch", launch):
            refresh.run_session(config.load_global(), "https://x.example.com/a", "p", self.data)
        self.assertEqual(opt(seen["argv"], "--model"), "haiku")
        self.assertEqual(opt(seen["argv"], "--settings"), str(self.data / "settings-light.json"))

    def test_dispatcher_stays_on_subscription(self):
        argv, _ = dispatch.build_command(config.load_global(), str(self.data), ["a"])
        self.assertEqual(opt(argv, "--settings"), str(self.data / "settings-dispatcher.json"))
        self.assertEqual(read(self.data / "settings-dispatcher.json")["env"]["ANTHROPIC_BASE_URL"], "")
        rec = []
        with mock.patch.object(workers, "_launch", return_value="abcd1234"), \
                mock.patch.object(dispatch, "build_command", return_value=(["claude"], {})), \
                mock.patch.object(dispatch, "_live_dispatcher", return_value=None), \
                mock.patch.object(dispatch.sessions, "list_all", return_value=[]), \
                mock.patch.object(dispatch.ops, "pass_begin"), mock.patch.object(dispatch.access, "run_due"), \
                mock.patch.object(dispatch, "_last_pass", return_value={"ended_at": "x"}), \
                mock.patch.object(dispatch, "_cleanup"), \
                mock.patch.object(spend, "record_launch", lambda *a: rec.append(a)):
            g = config.load_global()
            aps = ["a"]
            with mock.patch.object(dispatch, "groups", return_value={str(self.data): aps}):
                dispatch.run(g, aps, poll_s=0)
        self.assertEqual([(r[0], r[1], r[3]["name"]) for r in rec], [("abcd1234", "dispatcher", "subscription")])

    def test_blanks_api_key_in_dispatcher_settings(self):
        self.assertEqual(read(self.data / "settings-dispatcher.json")["env"]["ANTHROPIC_API_KEY"], "")

    def test_worker_launch_records_spend(self):
        rec = []
        listing = [{"id": "abcd1234", "sessionId": "S" * 8}]
        with mock.patch.object(workers, "_launch", return_value="abcd1234"), \
                mock.patch.object(workers.sessions, "list_all", return_value=listing), \
                mock.patch.object(workers.spend, "record_launch", lambda *a: rec.append(a)):
            r = workers.start("a", "T-1", "implement", str(self.data), "go")
        self.assertEqual(r["profile"], "g")
        self.assertEqual([(x[0], x[1], x[2], x[3]["name"]) for x in rec], [("abcd1234", "worker", "a", "g")])

    def test_resumed_worker_uses_new_profile(self):
        g2 = gateway()
        g2.update(name="g2", label="G2")
        g2["gateway"]["base_url"] = "https://other.example.com/anthropic"
        profile.save(g2)
        worker = {"task": "T-1", "role": "implement", "session_name": "a · T-1 · implement", "session_id": "sess-1",
                  "short_id": "old1", "ports": [7100, 7109], "needs": [], "started_at": "2026-01-01T00:00:00Z",
                  "resumes": 0, "cwd": str(self.data), "profile": "g"}
        with state.mutate("a") as st:
            st["workers"] = [worker]
        profile.use("g2")
        seen, rec = {}, []

        def launch(argv, cwd, env):
            seen["argv"] = argv
            return "new2"

        with mock.patch.object(workers, "_launch", launch), \
                mock.patch.object(workers.spend, "record_launch", lambda *a: rec.append(a)):
            got = workers.resume("a", worker)
        f = opt(seen["argv"], "--settings")
        self.assertEqual(read(paths.Path(f))["env"]["ANTHROPIC_BASE_URL"], "https://other.example.com/anthropic")
        self.assertEqual(got["profile"], "g2")
        self.assertEqual(rec[0][3]["name"], "g2")
        self.assertEqual(state.load("a")["workers"][0]["profile"], "g2")

    def test_discover_passes_profile_settings(self):
        seen = {}

        def run(argv, **kw):
            seen["argv"] = argv
            init = json.dumps({"type": "system", "subtype": "init", "tools": ["Bash"], "mcp_servers": []})
            return subprocess.CompletedProcess(argv, 0, init, "")

        with mock.patch.object(subprocess, "run", run):
            perms.discover("claude", str(self.data), self.data / "projects" / "a" / "settings-discover.json")
        self.assertEqual(opt(seen["argv"], "--settings"), str(self.data / "projects" / "a" / "settings-discover.json"))

    def test_write_for_discovers_with_the_active_profile(self):
        seen = {}

        def disc(claude, cwd, settings):
            seen["settings"] = settings
            seen["body"] = read(settings)
            return ["Bash"], []

        with mock.patch.object(perms, "discover", disc), mock.patch("autopilot.access.record_servers"):
            perms.write_for("a")
        self.assertEqual(seen["body"]["env"]["ANTHROPIC_BASE_URL"], URL)
        self.assertNotIn("permissions", seen["body"])

    def test_one_off_passes_settings_and_runs_on_active(self):
        argv_seen = {}

        def run(argv, **kw):
            argv_seen["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, "done", "")

        act = {"tool": "mcp__slack__slack_send_message", "prompt": "p", "option": "yes"}
        with state.mutate("a") as st:
            st["decisions"] = [{"id": "D1", "status": "answered", "answer": "yes", "action": act, "kind": "ask",
                                "title": {"en": "t"}, "body": {"en": "b"}, "options": [], "links": [],
                                "created_at": "2026-01-01T00:00:00Z"}]
        with mock.patch.object(subprocess, "run", run):
            perms.run_one_off("a", "D1")
        f = opt(argv_seen["argv"], "--settings")
        self.assertEqual(read(paths.Path(f))["env"]["ANTHROPIC_BASE_URL"], URL)


class CleanupTest(LaunchBase):
    def test_cleanup_settles_before_rm(self):
        calls = []
        with state.mutate("a") as st:
            st["tasks"] = [{"id": "T-1", "title": {"en": "t"}, "depends_on": [], "status": "done", "link": "", "needs": [],
                            "critical": False, "phase": "p", "origin": "source",
                            "sessions": {"implement": "sess-1"}}]
        listing = [{"id": "abcd1234", "sessionId": "sess-1"}]
        with mock.patch.object(workers.spend, "settle", lambda s: calls.append(("settle", s))), \
                mock.patch.object(workers.sessions, "list_all", return_value=listing), \
                mock.patch.object(subprocess, "run", lambda argv, **kw: calls.append((argv[1], argv[2]))):
            workers.cleanup("a", "T-1")
        self.assertEqual(calls, [("settle", "abcd1234"), ("rm", "sess-1")])

    def test_replies_cleanup_settles_before_rm(self):
        calls = []
        with mock.patch.object(replies.spend, "settle", lambda s: calls.append(("settle", s))), \
                mock.patch.object(subprocess, "run", lambda argv, **kw: calls.append((argv[1], argv[2]))):
            replies._cleanup("claude", "abcd1234")
        self.assertEqual(calls, [("stop", "abcd1234"), ("settle", "abcd1234"), ("rm", "abcd1234")])

    def test_dispatch_cleanup_settles_before_rm(self):
        calls = []
        with mock.patch.object(dispatch.spend, "settle", lambda s: calls.append(("settle", s))), \
                mock.patch.object(subprocess, "run", lambda argv, **kw: calls.append((argv[1], argv[2]))):
            dispatch._cleanup("claude", "abcd1234")
        self.assertEqual(calls, [("stop", "abcd1234"), ("settle", "abcd1234"), ("rm", "abcd1234")])


if __name__ == "__main__":
    unittest.main()
