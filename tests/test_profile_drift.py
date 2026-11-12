"""The active profile and the settings files never drift apart silently, and a broken active profile never
crashes the gate."""
from __future__ import annotations

import contextlib
import io
import json
import unittest
from datetime import datetime, timezone
from unittest import mock

from tests.helpers import ROOT, TempData, write_ap
from tests.test_profile_settings import gateway, read, write_files
from autopilot import (budget, cli, config, dispatch, gate, paths, perms, profile, publish, replies, spend, state,
                       workers)

FAKE = str(ROOT / "tests" / "bin" / "fake-claude")
NOW = datetime(2026, 3, 10, 15, 0, tzinfo=timezone.utc)


class Base(unittest.TestCase):
    def setUp(self):
        self.td = TempData()
        self.data = self.td.__enter__()
        self.addCleanup(self.td.__exit__, None, None, None)
        write_ap(self.data, "a")
        gc = config.load_global()
        gc["tools"]["claude"] = FAKE
        config.save_global(gc)
        profile.save(gateway(key_command="true"))
        write_files("a")

    def events(self):
        f = self.data / "events-global.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue() + err.getvalue()


class ConfigRefusesActiveProfileTest(Base):
    def test_set_and_reset_are_refused(self):
        for argv in (("config", "set", "active_profile=g"), ("config", "reset", "active_profile")):
            code, out = self.run_cli(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("ap profile use", out)
        self.assertEqual(config.load_global()["active_profile"], "subscription")


class ConfigRefusesMovedKeysTest(Base):
    def test_moved_model_keys_point_at_ap_profile(self):
        for key in ("models.dispatcher", "models.worker", "worker_effort", "reply_check.model"):
            for argv in (("config", "set", f"{key}=x"), ("config", "reset", key)):
                code, out = self.run_cli(*argv)
                self.assertEqual(code, 2, argv)
                self.assertIn("ap profile", out)
        raw = (self.data / "config.json").read_text()
        self.assertNotIn("worker_effort", raw)


class UseAppliesFirstTest(Base):
    def test_a_failed_apply_keeps_the_active_name_and_raises(self):
        with mock.patch.object(perms, "_write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                profile.use("g")
        self.assertEqual(config.load_global()["active_profile"], "subscription")
        self.assertNotIn("profile_used", [e.get("type") for e in self.events()])

    def test_a_good_apply_then_saves(self):
        profile.use("g")
        self.assertEqual(config.load_global()["active_profile"], "g")
        self.assertEqual(read(paths.ap_dir("a") / "settings.json")["env"]["AUTOPILOT_PROFILE"], "g")


class LaunchReadsTheSettingsFileTest(Base):
    def drift(self):
        """The settings files say subscription; the config says g (written behind `ap profile use`'s back)."""
        config.set_setting("active_profile", "g")

    def test_worker_start_refuses_settings_for_another_profile(self):
        self.drift()
        with mock.patch.object(workers, "_launch") as launch:
            with self.assertRaises(RuntimeError) as cm:
                workers.start("a", "T-1", "implement", str(self.data), "go")
        launch.assert_not_called()
        self.assertIn("ap profile use g", str(cm.exception))
        self.assertIn("settings_out_of_date", [e.get("type") for e in self.events()])

    def test_worker_resume_refuses_settings_for_another_profile(self):
        self.drift()
        w = {"task": "T-1", "role": "implement", "session_id": "s1", "short_id": "x", "cwd": str(self.data)}
        with mock.patch.object(workers, "_launch") as launch:
            with self.assertRaises(RuntimeError):
                workers.resume("a", w)
        launch.assert_not_called()

    def test_one_off_refuses_a_stale_file_and_records_the_files_profile(self):
        with state.mutate("a") as st:
            st["decisions"] = [{"id": "D1", "status": "answered", "answer": "yes", "kind": "ask",
                                "action": {"tool": "mcp__x__send_message", "prompt": "p", "option": "yes"},
                                "title": {"en": "t"}, "body": {"en": "b"}, "options": [], "links": [],
                                "created_at": "2026-01-01T00:00:00Z"}]
        rec = []
        real = perms.one_off_settings

        def stale(ap, tool):   # a one-off file left from another profile
            f = real(ap, tool)
            body = read(f)
            body["env"]["AUTOPILOT_PROFILE"] = "g"
            f.write_text(json.dumps(body))
            return f

        with mock.patch.object(perms, "one_off_settings", stale), mock.patch("subprocess.run") as run:
            with self.assertRaises(RuntimeError):
                perms.run_one_off("a", "D1")
        run.assert_not_called()
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout='{"result": "ok"}', stderr="")), \
                mock.patch.object(perms.spend, "record_session", lambda *a: rec.append(a)):
            perms.run_one_off("a", "D1")
        self.assertEqual(rec[0][3]["name"], "subscription")

    def test_worker_records_the_profile_its_settings_file_names(self):
        profile.use("g")
        rec = []
        with mock.patch.object(workers, "_launch", return_value="abcd1234"), \
                mock.patch.object(workers.sessions, "list_all", return_value=[]), \
                mock.patch.object(workers.spend, "record_launch", lambda *a: rec.append(a)), \
                mock.patch.object(profile, "active", wraps=profile.active):
            r = workers.start("a", "T-1", "implement", str(self.data), "go")
        self.assertEqual((r["profile"], rec[0][3]["name"]), ("g", "g"))

    def test_dispatcher_refuses_a_dispatcher_file_for_another_profile(self):
        body = read(self.data / "settings-dispatcher.json")
        body["env"]["AUTOPILOT_PROFILE"] = "g"
        (self.data / "settings-dispatcher.json").write_text(json.dumps(body))
        with mock.patch.object(workers, "_launch") as launch, \
                mock.patch.object(dispatch, "_live_dispatcher", return_value=None), \
                mock.patch.object(dispatch.ops, "pass_begin"), mock.patch.object(dispatch.access, "run_due"):
            res = dispatch.run(config.load_global(), ["a"], poll_s=0, timeout_s=0)
        launch.assert_not_called()
        self.assertIn("ap profile use subscription", res[0]["error"])

    def test_page_light_file_heals_itself(self):
        f = profile.ensure_light()
        body = read(f)
        body["env"]["AUTOPILOT_PROFILE"] = "g"
        f.write_text(json.dumps(body))
        self.assertEqual(read(profile.ensure_light())["env"]["AUTOPILOT_PROFILE"], "subscription")


class BrokenActiveProfileTest(Base):
    def setUp(self):
        super().setUp()
        config.set_setting("active_profile", "ghost")   # its file was removed by hand

    def test_budget_reports_it_instead_of_crashing(self):
        w = budget.current(config.load_global(), NOW)["workers"]
        self.assertEqual((w["allow"], w["reason"]), (0, "profile"))
        self.assertIn("ghost", w["error"])

    def test_page_shows_it_like_a_key_error(self):
        acc = publish.collect([])["meta"]["access"]
        self.assertIn("ghost", acc["profile_error"]["error"])
        self.assertTrue(acc["profile_error"]["fix"])

    def test_gate_and_reply_check_keep_running(self):
        publish._save_page({"url": "https://claude.ai/artifact/X"})
        with mock.patch.object(replies, "check", return_value={"ok": True, "replies": []}):
            replies.step(config.load_global(), {}, datetime(2026, 1, 2, 12, 0).astimezone())
            with mock.patch.object(gate, "_sessions", lambda g: []), mock.patch.object(gate, "notify", lambda m: None), \
                    mock.patch.object(gate.refresh, "step"):
                self.assertEqual(gate.run(dispatch_fn=lambda g, aps: []), 0)

    def test_no_worker_starts(self):
        with mock.patch.object(workers, "_launch") as launch:
            with self.assertRaises(RuntimeError):
                workers.start("a", "T-1", "implement", str(self.data), "go")
        launch.assert_not_called()


class SnapshotUsesCachedStateTest(Base):
    def test_hook_prompt_budget_line_runs_nothing_and_opens_no_card(self):
        from autopilot import cap, snapshot
        profile.use("g")
        with mock.patch.object(spend, "totals", return_value={"today": 7.25, "week": 7.25, "month": 7.25,
                                                             "unknown": False, "unknown_today": False}):
            budget.current(config.load_global(), datetime.now(timezone.utc))   # the gate's minute: caches both
        boom = AssertionError("the hook ran something")
        with mock.patch.object(profile, "_run_silent", side_effect=boom), \
                mock.patch.object(spend, "totals", side_effect=boom), mock.patch.object(spend, "_resolve", side_effect=boom), \
                mock.patch.object(cap, "open_card", side_effect=boom), mock.patch.object(cap, "close_card", side_effect=boom), \
                mock.patch.object(cap, "_save", side_effect=boom):
            line = snapshot._budget_line()
        self.assertIn("$7.25", line)
        self.assertIn("gateway profile g", line)

    def test_without_cached_spend_it_says_unknown(self):
        from autopilot import snapshot
        profile.use("g")
        with mock.patch.object(spend, "totals", side_effect=AssertionError("ran")), \
                mock.patch.object(profile, "_run_silent", side_effect=AssertionError("ran")):
            line = snapshot._budget_line()
        self.assertIn("not counted yet", line)

    def test_a_broken_active_profile_is_one_line(self):
        from autopilot import snapshot
        config.set_setting("active_profile", "ghost")
        self.assertIn("ghost", snapshot._budget_line())


if __name__ == "__main__":
    unittest.main()
