from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.helpers import TempData, write_ap
from autopilot import budget, config, profile, publish, state, usage, workers

NOW = datetime(2026, 3, 10, 15, 0, tzinfo=timezone.utc)
URL = "https://llm-gateway.example.com/anthropic"


def gw(key="echo k", **extra):
    return {"name": "gw", "mode": "gateway", "model": "m", "small_model": "s", "daily_cap_usd": 500,
            "gateway": {"base_url": URL, "key_command": key, **extra}}


class Base(unittest.TestCase):
    def setUp(self):
        self.td = TempData()
        self.data = self.td.__enter__()

    def tearDown(self):
        self.td.__exit__(None, None, None)

    def use(self, **kw):
        profile.save(gw(**kw) if "key" not in kw else gw(kw.pop("key"), **kw))
        profile.use("gw")


class KeyTest(Base):
    def test_key_failure_blocks_launch_without_fallback(self):
        self.use(key="exit 1")
        write_ap(self.data, "a")
        cur = budget.current(config.load_global(), NOW)
        self.assertEqual((cur["workers"]["allow"], cur["workers"]["reason"]), (0, "key"))
        self.assertIsNone(cur["passes"]["allow"])   # page sessions run on the subscription and go on
        with mock.patch.object(workers, "_launch") as launch:
            with self.assertRaises(RuntimeError) as cm:
                workers.start("a", "T-1", "implement", str(self.data), "go")
        launch.assert_not_called()
        self.assertIn("key", str(cm.exception))
        self.assertEqual(profile.active()["name"], "gw")
        self.assertEqual(state.load("a")["workers"], [])

    def test_the_cap_blocks_start_resume_and_one_off(self):
        from autopilot import perms, spend
        self.use(key="echo k")
        write_ap(self.data, "a")
        w = {"task": "T-1", "role": "implement", "session_id": "s1", "short_id": "x", "cwd": str(self.data)}
        with state.mutate("a") as st:
            st["decisions"] = [{"id": "D1", "status": "answered", "answer": "yes", "kind": "ask",
                                "action": {"tool": "mcp__x__send_message", "prompt": "p", "option": "yes"},
                                "title": {"en": "t"}, "body": {"en": "b"}, "options": [], "links": [],
                                "created_at": "2026-01-01T00:00:00Z"}]
        spent = {"today": 900.0, "week": 900.0, "month": 900.0, "unknown": False, "unknown_today": False}
        self.assertIsNone(profile.check_key(profile.active(), datetime.now(timezone.utc)))   # cached: the key works
        with mock.patch.object(spend, "totals", return_value=spent), mock.patch.object(workers, "_launch") as launch, \
                mock.patch("subprocess.run") as run:
            for call in (lambda: workers.start("a", "T-1", "implement", str(self.data), "go"),
                         lambda: workers.resume("a", w), lambda: perms.run_one_off("a", "D1")):
                with self.assertRaises(RuntimeError) as cm:
                    call()
                self.assertIn("daily cap", str(cm.exception))
        launch.assert_not_called()
        run.assert_not_called()

    def test_a_broken_active_profile_blocks_start(self):
        write_ap(self.data, "a")
        config.set_setting("active_profile", "ghost")
        with mock.patch.object(workers, "_launch") as launch:
            with self.assertRaises(RuntimeError) as cm:
                workers.start("a", "T-1", "implement", str(self.data), "go")
        launch.assert_not_called()
        self.assertIn("ghost", str(cm.exception))

    def test_key_ok_does_not_block(self):
        self.use(key="echo k")
        self.assertIsNone(profile.check_key(profile.active(), NOW))
        self.assertNotEqual(budget.current(config.load_global(), NOW)["workers"]["reason"], "key")

    def test_subscription_has_no_key_error(self):
        self.assertIsNone(profile.check_key(profile.load("subscription"), NOW))

    def test_key_output_never_logged(self):
        self.use(key="echo SECRET$((100+23))")
        profile.check_key(profile.active(), NOW)
        self.use(key="echo SECRET$((100+23)) >&2; exit 1")
        profile.check_key(profile.active(), NOW + timedelta(hours=1))
        budget.current(config.load_global(), NOW + timedelta(hours=1))
        publish.collect([])
        for f in self.data.rglob("*"):
            if f.is_file():
                self.assertNotIn("SECRET123", f.read_text(errors="ignore"), str(f))

    def test_failed_key_rechecked_after_a_minute(self):
        flag = self.data / "fixed"
        self.use(key=f"test -f {flag}")
        p = profile.active()
        self.assertIn("failed", profile.check_key(p, NOW))
        flag.write_text("")
        self.assertIn("failed", profile.check_key(p, NOW + timedelta(seconds=30)))   # still cached
        self.assertIsNone(profile.check_key(p, NOW + timedelta(seconds=61)))

    def test_result_is_cached_ten_minutes(self):
        marker = self.data / "ran"
        self.use(key=f"echo x >> {marker}")
        p = profile.active()
        profile.check_key(p, NOW)
        profile.check_key(p, NOW + timedelta(minutes=5))
        self.assertEqual(len(marker.read_text().split()), 1)
        profile.check_key(p, NOW + timedelta(minutes=11))
        self.assertEqual(len(marker.read_text().split()), 2)

    def test_page_shows_key_error_and_fix(self):
        self.use(key="exit 1")
        acc = publish.collect([])["meta"]["access"]
        self.assertIn("exit 1", acc["key_error"]["error"])
        self.assertIn("terminal", acc["key_error"]["fix"])

    def test_page_has_no_key_error_when_fine(self):
        self.use(key="echo k")
        self.assertIsNone(publish.collect([])["meta"]["access"].get("key_error"))


class RotateTest(Base):
    def test_rotate_runs_command_and_reapplies(self):
        marker = self.data / "rotated"
        self.use(key="echo k", rotate_command=f"echo NEWKEY; touch {marker}")
        with mock.patch.object(profile, "apply") as ap:
            profile.rotate("gw")
        self.assertTrue(marker.exists())
        ap.assert_called_once()
        self.assertFalse((self.data / "profile-key.json").exists())

    def test_rotate_clears_refresh_record(self):
        self.use(rotate_command="true")
        (self.data / "profile-refresh.json").write_text(json.dumps({"at": NOW.isoformat(), "profile": "gw"}))
        with mock.patch.object(profile, "apply"):
            profile.rotate("gw")
        self.assertFalse((self.data / "profile-refresh.json").exists())

    def test_rotate_without_command_raises(self):
        self.use()
        with self.assertRaises(ValueError):
            profile.rotate("gw")

    def test_rotate_failure_raises(self):
        self.use(rotate_command="exit 3")
        with self.assertRaises(RuntimeError):
            profile.rotate("gw")


def transcript(dir_: Path, sid: str, lines: list) -> None:
    (dir_ / f"{sid}.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")


def api_error(text):
    return {"type": "assistant", "isApiErrorMessage": True,
            "message": {"model": "<synthetic>", "content": [{"type": "text", "text": text}]}}


class AuthFailureTest(Base):
    def setUp(self):
        super().setUp()
        self.home = Path(tempfile.mkdtemp())
        proj = self.home / ".claude" / "projects" / "p"
        proj.mkdir(parents=True)
        self.proj = proj
        patch = mock.patch.object(Path, "home", return_value=self.home)
        patch.start()
        self.addCleanup(patch.stop)
        self.marker = self.data / "refreshed"
        self.use(key="echo k", refresh_command=f"echo TOKEN$((4+5)); echo x >> {self.marker}")

    def count(self):
        return len(self.marker.read_text().split()) if self.marker.exists() else 0

    def test_auth_failure_refreshes_once(self):
        transcript(self.proj, "s1", [{"type": "user"}, api_error("API Error: 401 Invalid API key")])
        with mock.patch.object(profile, "apply") as ap:
            self.assertTrue(profile.note_session_end("s1", "gw", NOW))
            self.assertEqual(self.count(), 1)
            ap.assert_called_once()
            self.assertIsNone(profile.check_key(profile.active(), NOW))
            # a second failure within ten minutes sets the key error and does not refresh again
            transcript(self.proj, "s2", [api_error("API Error: 403 forbidden")])
            profile.note_session_end("s2", "gw", NOW + timedelta(minutes=3))
            self.assertEqual(self.count(), 1)
            err = profile.check_key(profile.active(), NOW + timedelta(minutes=4))
            self.assertIn("rejected", err)
            # after ten minutes the key command is tried again
            self.assertIsNone(profile.check_key(profile.active(), NOW + timedelta(minutes=14)))

    def test_stale_error_after_refresh_is_not_a_second_failure(self):
        old = (NOW - timedelta(minutes=1)).isoformat()
        with mock.patch.object(profile, "apply"):
            transcript(self.proj, "s1", [dict(api_error("API Error: 401"), timestamp=old)])
            profile.note_session_end("s1", "gw", NOW - timedelta(minutes=2))   # refresh at NOW-2m
            # an error stamped before that refresh, still at the end of a later transcript
            transcript(self.proj, "s2", [dict(api_error("API Error: 401"), timestamp=(NOW - timedelta(minutes=3)).isoformat())])
            self.assertFalse(profile.note_session_end("s2", "gw", NOW))
        self.assertIsNone(profile.check_key(profile.active(), NOW))

    def test_recovered_session_is_not_a_failure(self):
        transcript(self.proj, "s1", [api_error("API Error: 401"),
                                     {"type": "assistant", "message": {"model": "m", "content": [{"type": "text", "text": "back"}]}}])
        self.assertFalse(profile.session_auth_failed("s1"))

    def test_rate_limit_line_is_not_an_auth_failure(self):
        line = {"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit",
                "message": {"model": "<synthetic>", "content": [{"type": "text", "text":
                            "You've hit your individual spend limit \u00b7 run /usage-credits to ask for more"}]}}
        transcript(self.proj, "s1", [line])
        with mock.patch.object(profile, "apply"):
            self.assertFalse(profile.note_session_end("s1", "gw", NOW))
            self.assertFalse(profile.note_session_end("s1", "gw", NOW + timedelta(minutes=1)))
        self.assertEqual(self.count(), 0)
        self.assertFalse((self.data / "profile-key.json").exists())
        self.assertFalse((self.data / "profile-refresh.json").exists())

    def test_other_errors_do_nothing(self):
        transcript(self.proj, "s1", [api_error("API Error: 500 overloaded"),
                                     {"type": "assistant", "message": {"content": [{"type": "text", "text": "got 401 in a log"}]}}])
        self.assertFalse(profile.note_session_end("s1", "gw", NOW))
        self.assertEqual(self.count(), 0)

    def test_subscription_worker_ignored(self):
        transcript(self.proj, "s1", [api_error("API Error: 401 x")])
        self.assertFalse(profile.note_session_end("s1", "subscription", NOW))

    def test_detects_error_field_variants(self):
        transcript(self.proj, "s1", [{"type": "assistant", "error": "authentication_failed",
                                      "message": {"content": [{"type": "text", "text": "Invalid API key"}]}}])
        self.assertTrue(profile.session_auth_failed("s1"))

    def test_refresh_output_never_stored(self):
        transcript(self.proj, "s1", [api_error("API Error: 401")])
        with mock.patch.object(profile, "apply"):
            profile.note_session_end("s1", "gw", NOW)
        for f in self.data.rglob("*"):
            if f.is_file():
                self.assertNotIn("TOKEN9", f.read_text(errors="ignore"))


if __name__ == "__main__":
    unittest.main()
