"""Gateway key health (design section 12, key health): the cached key check, one refresh after the gateway rejects
the key, the hold after a second rejection, `ap2 profile rotate`, and what is not a key failure (the gateway's
safety guard, a limit). Ported from v1's `tests/test_profile_key.py` (v1 commit 519346b). Every folder is a temp
folder; workers run the fake claude (`tests/bin/fake-claude-v2`), and `spend.totals` is a stub where a worker
starts. A key command that must print a secret prints `SECRET$((100+23))`, so the command text itself (kept in the
profile file) never holds the text the tests look for."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests import test_ap2_cap as capt
from tests.helpers import ROOT
from tests.test_ap2_cli import temp_inside_the_accounts_claude
from tests.test_ap2_profiles import gateway
from assistant import apistop, cap, keys, profiles, resources, transcripts
from assistant import workers as workers_mod
from assistant.config import Config
from assistant.store import Store

NOW = datetime(2026, 11, 12, 3, 0, tzinfo=timezone.utc)
AP2 = str(ROOT / "bin" / "ap2")
SECRET = "SECRET123"
PRINTS_SECRET = "echo SECRET$((100+23)); echo SECRET$((100+23)) >&2"  # prints SECRET123; the text does not hold it
SAFETY = "API Error: 400 Request blocked by safety guard"
LIMIT = ("You've hit your individual spend limit · your 5-hour limit resets 8:40pm (America/Los_Angeles)")


def at(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def reply(t: datetime, text="Done.") -> dict:
    return {"type": "assistant", "timestamp": at(t),
            "message": {"model": "claude-opus-5-5", "content": [{"type": "text", "text": text}]}}


def api_error(t: datetime, text: str, status=None, error="unknown", **over) -> dict:
    """An API error line as Claude Code writes it: an assistant line flagged isApiErrorMessage, model <synthetic>."""
    e = {"type": "assistant", "timestamp": at(t), "isApiErrorMessage": True, "error": error,
         "message": {"model": "<synthetic>", "content": [{"type": "text", "text": text}]}}
    if status is not None:
        e["apiErrorStatus"] = status
    e.update(over)
    return e


def auth_401(t: datetime) -> dict:
    return api_error(t, 'API Error: 401 {"type":"error","error":{"type":"authentication_error","message":"invalid '
                        'x-api-key"}}', status=401, error="authentication_failed")


def counter(path: Path, code: int = 0) -> str:
    """A command that adds a line to `path` each time it runs, then exits `code`."""
    return f"echo x >> {shlex.quote(str(path))}; exit {code}"


def runs(path: Path) -> int:
    return len(path.read_text().split()) if path.exists() else 0


def snapshot(folder: Path) -> dict:
    return {str(p.relative_to(folder)): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


class KeyCase(unittest.TestCase):
    """A temp data folder and a temp folder of transcripts; profile `g` is a gateway profile."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-keys-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.projects = self.tmp / "projects"
        self.projects.mkdir()
        self.n = 0

    def save(self, name="g", key="true", **gw) -> dict:
        p = gateway(name, key_command=key, daily_cap_usd=500)
        p["gateway"].update(gw)
        profiles.save(self.data, p)
        return profiles.load(self.data, name)

    def transcript(self, *lines, raw: str = "") -> str:
        """Write a session's jsonl under the projects folder; return its session id."""
        self.n += 1
        sid = f"00000000-0000-4000-8000-{self.n:012d}"
        folder = self.projects / "-tmp-worktree"
        folder.mkdir(exist_ok=True)
        text = raw + "".join(json.dumps(x) + "\n" for x in lines)
        (folder / f"{sid}.jsonl").write_text(text, encoding="utf-8")
        return sid

    def state(self):
        f = self.data / keys.STATE
        return json.loads(f.read_text()) if f.exists() else None

    def refresh_record(self):
        f = self.data / keys.REFRESH
        return json.loads(f.read_text()) if f.exists() else None

    def end(self, sid, name="g", now=NOW) -> bool:
        return keys.note_session_end(self.data, self.projects, sid, name, now)

    def errors(self, sid) -> list:
        """The session's API errors as `keys.note_session_end` reads them (`apistop.tail`)."""
        return (apistop.tail(self.projects, sid) or {}).get("errors") or []


# -- 1. a failing key blocks every start, with no fallback ---------------------------------------------------------------

class AllowanceKeyTest(capt.CapWorkerCase):
    def use_key(self, key, name="g", **gw):
        p = gateway(name, key_command=key, daily_cap_usd=500)
        p["gateway"].update(gw)
        profiles.save(self.data, p)
        profiles.use(self.data, name)
        return profiles.active(self.data)

    def state_ok(self) -> bool:
        return json.loads((self.data / keys.STATE).read_text())["ok"] is True

    def allow(self, p, now=None):
        return cap.allowance(self.data, self.fake_home / "projects", now or self.clock(), p,
                             store=Store(self.data, self.cfg, self.clock))

    def test_key_failure_blocks_launch_without_fallback(self):
        p = self.use_key("exit 1")
        a = self.allow(p)
        self.assertEqual((a["allow"], a["reason"]), (0, "key"))
        self.assertIn("exit 1", a["error"])
        self.assertIn("key", cap.REASONS)
        self.assertEqual(self.totals_calls, 0)  # refused before any spend is read
        cid = self.add_card()
        with self.assertRaises(cap.CapError) as e:
            self.workers().start(cid, "implement", cwd=self.wt, prompt="Do the work.")
        msg = str(e.exception)
        self.assertEqual(keys.KEY_FIX, "Run the profile's key command in a terminal (sign in again if it asks); the "
                                       "next start or check tries it again.")  # the spec's fixed text
        self.assertIn(keys.KEY_FIX, msg)
        self.assertIn("the gateway key does not work", msg)
        self.assertIn("exit 1", msg)
        self.assertEqual(self.bg_calls(), [])  # nothing launched
        self.assertEqual(self.discovery_calls(), [])  # nothing discovered
        self.assertEqual(self.records(), [])
        self.assertEqual(profiles.active_name(self.data), "g")  # no fallback to the subscription
        self.assertFalse((self.data / "sessions" / cid / "settings-implement.json").exists())

    def test_a_key_check_that_raises_never_allows(self):
        # A key check that cannot answer (here a symlinked key record) refuses: it never fails open to a start.
        p = self.use_key("exit 1")
        elsewhere = self.tmp / "elsewhere.json"
        elsewhere.write_text(json.dumps({"at": self.clock().isoformat(), "ok": True, "profile": "g"}))
        (self.data / keys.STATE).symlink_to(elsewhere)
        with self.assertRaisesRegex(profiles.ProfileError, "symlink"):
            self.allow(p)
        cid = self.add_card()
        with self.assertRaisesRegex(profiles.ProfileError, "symlink"):
            self.workers().start(cid, "implement", cwd=self.wt, prompt="Do the work.")
        self.assertEqual(self.bg_calls(), [])  # nothing launched
        self.assertEqual(self.records(), [])
        self.assertEqual(self.totals_calls, 0)

    def test_subscription_runs_no_key_command(self):
        ran = self.tmp / "ran"
        self.use_key(counter(ran, 1))
        self.use_subscription()
        sub = profiles.active(self.data)
        self.assertIsNone(keys.check(self.data, sub, self.clock()))
        self.assertEqual(self.allow(sub)["reason"], "ok")
        self.start()
        self.assertEqual(len(self.bg_calls()), 1)
        self.assertEqual(runs(ran), 0)
        self.assertFalse((self.data / keys.STATE).exists())

    def test_key_error_opens_no_cap_card(self):
        p = self.use_key("exit 1")
        self.spent = capt.totals(900.0)  # over the cap too: the key wins, and no card opens
        a = self.allow(p)
        self.assertEqual(a["reason"], "key")
        self.assertEqual([c for c in self.workers().store.list() if c.get("feed") == "cap"], [])
        self.assertFalse((self.data / cap.STATE).exists())
        self.assertEqual(self.totals_calls, 0)

    def test_paused_wins_over_the_key(self):
        p = self.use_key("exit 1")
        cap.pause(self.data, self.clock())
        self.assertEqual(self.allow(p)["reason"], "paused")

    def test_parked_worker_stays_parked_on_a_key_error(self):
        w = self.workers()
        a = self.start(w)
        self.park_quota(w, a)
        self.clock.advance(4 * 60)  # past the reset
        self.use_key("exit 1")
        with mock.patch.object(workers_mod.Workers, "_parked_due", return_value=True):
            out = self.workers().check(usage_file=self.quiet())[0]
        self.assertEqual((out["state"], out["did"]), ("parked", "nothing"))
        self.assertIn("gateway key", out["note"])
        self.assertTrue(self.records()[0]["parked"])
        self.assertIsNone(self.get(a["card"]).get("ask"))
        self.assertEqual(len(self.bg_calls()), 1)

    def test_dead_worker_keeps_its_resume_on_a_key_error(self):
        self.use_key("true")
        a = self.start()
        self.assertTrue(self.state_ok())
        flag = self.tmp / "fixed"
        self.use_key(f"test -f {shlex.quote(str(flag))}", name="g2")  # the cached ok is g's, not g2's
        self.drop_agent(a["short_id"])
        out = self.workers().check()[0]
        self.assertEqual((out["state"], out["did"]), ("dead", "nothing"))
        self.assertIn("gateway key", out["note"])
        self.assertEqual(self.records()[0]["resumes"], 0)
        flag.write_text("")
        self.clock.advance(2)  # past the 60 s recheck of a failing key
        self.assertEqual(self.workers().check()[0]["did"], "resumed")

    def test_a_resume_runs_no_key_command_under_the_lock(self):
        # `check` runs the key check before it locks the worker list: the resume's allowance under the lock reads
        # that cached answer, so a key command that hangs never holds the lock.
        ran = self.tmp / "keyed"
        self.use_key(counter(ran))
        a = self.start()
        self.assertEqual(runs(ran), 1)
        self.clock.advance(11)  # past the 10 minutes a working key is kept
        self.drop_agent(a["short_id"])
        held, lock = [], {"in": False}
        real_locked, real_run = resources.Registry.locked, keys._run_silent

        @contextlib.contextmanager
        def locked(reg):
            with real_locked(reg) as recs:
                lock["in"] = True
                try:
                    yield recs
                finally:
                    lock["in"] = False

        def run(cmd, timeout):
            held.append(lock["in"])
            return real_run(cmd, timeout)
        with mock.patch.object(resources.Registry, "locked", locked), \
                mock.patch.object(keys, "_run_silent", side_effect=run):
            out = self.workers().check()[0]
        self.assertEqual((out["state"], out["did"]), ("dead", "resumed"))
        self.assertEqual(held, [False])
        self.assertEqual(runs(ran), 2)

    def test_a_paused_or_shadow_check_runs_no_key_command(self):
        # The key check before the lock runs only when a resume could ask for the allowance: while paused the
        # allowance answers `paused` before it checks the key, and in shadow mode a dead worker's resume checks the
        # mode before it asks for the allowance.
        ran = self.tmp / "keyed"
        self.use_key(counter(ran))
        a = self.start()
        self.assertEqual(runs(ran), 1)
        self.clock.advance(11)  # past the 10 minutes a working key is kept
        self.drop_agent(a["short_id"])
        cap.pause(self.data, self.clock())
        self.workers().check()
        self.assertEqual(runs(ran), 1)
        cap.resume(self.data)
        self.workers(cfg=Config()).check()  # shadow, the default
        self.assertEqual(runs(ran), 1)
        self.assertTrue(self.records())  # a worker is still listed: only the mode kept the key command from running

    def test_a_shadow_check_of_a_parked_worker_runs_the_key_command_before_the_lock(self):
        # A parked worker's due check asks for the allowance before any live-mode check, so even in shadow mode the
        # key check runs before the lock, and the due check under the lock reads that cached answer.
        ran = self.tmp / "keyed"
        w = self.workers()
        a = self.start(w)
        self.park_quota(w, a)  # parked on the subscription (a gateway worker is never parked by its windows)
        self.assertTrue(self.records()[0]["parked"])
        self.use_key(counter(ran))  # then the gateway, its key never checked
        self.assertEqual(runs(ran), 0)
        # On the gateway a quota park's wait (a subscription window's) is skipped: the worker is due at once, so
        # its due check asks for the allowance.
        held, lock = [], {"in": False}
        real_locked, real_run = resources.Registry.locked, keys._run_silent

        @contextlib.contextmanager
        def locked(reg):
            with real_locked(reg) as recs:
                lock["in"] = True
                try:
                    yield recs
                finally:
                    lock["in"] = False

        def run(cmd, timeout):
            held.append(lock["in"])
            return real_run(cmd, timeout)
        with mock.patch.object(resources.Registry, "locked", locked), \
                mock.patch.object(keys, "_run_silent", side_effect=run):
            self.workers(cfg=Config()).check(usage_file=self.quiet())  # shadow, the default
        self.assertEqual(held, [False])
        self.assertEqual(runs(ran), 1)

    def test_key_state_is_edit_denied(self):
        self.use_key("true")
        w = self.workers()
        for role in ("implement", "review", "pr"):
            cid = self.add_card()
            w.start(cid, role, cwd=self.wt)
            deny = self.deny(cid, role)
            self.assertIn(f"Edit(/{self.data}/profile-key.json)", deny, role)
            self.assertIn(f"Edit(/{self.data}/profile-refresh.json)", deny, role)
        self.assertIn(keys.STATE, workers_mod.PROTECTED)
        self.assertIn(keys.REFRESH, workers_mod.PROTECTED)


# -- 2. the check is cached and never sees the key -----------------------------------------------------------------------

class CheckTest(KeyCase):
    def test_ok_is_cached_ten_minutes(self):
        ran = self.tmp / "ran"
        p = self.save(key=counter(ran))
        self.assertIsNone(keys.check(self.data, p, NOW))
        self.assertIsNone(keys.check(self.data, p, NOW + timedelta(minutes=9)))
        self.assertEqual(runs(ran), 1)
        self.assertIsNone(keys.check(self.data, p, NOW + timedelta(minutes=11)))
        self.assertEqual(runs(ran), 2)
        rec = self.state()
        self.assertEqual((rec["ok"], rec["error"], rec["profile"]), (True, None, "g"))

    def test_failure_is_rechecked_after_sixty_seconds(self):
        flag = self.tmp / "fixed"
        p = self.save(key=f"test -f {shlex.quote(str(flag))}")
        self.assertEqual(keys.check(self.data, p, NOW), "the key command failed (exit 1)")
        flag.write_text("")
        self.assertIn("exit 1", keys.check(self.data, p, NOW + timedelta(seconds=30)))  # still cached
        self.assertIsNone(keys.check(self.data, p, NOW + timedelta(seconds=61)))

    def test_timeout_and_os_errors_name_only_the_class(self):
        p = self.save()
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("x", 60)):
            self.assertEqual(keys.check(self.data, p, NOW), "the key command failed (TimeoutExpired)")
        with mock.patch("subprocess.run", side_effect=PermissionError(13, "denied")):
            self.assertEqual(keys.check(self.data, p, NOW + timedelta(hours=1)), "the key command failed (PermissionError)")

    def test_the_command_runs_with_every_stream_closed(self):
        p = self.save(key="true")
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess("true", 0)) as run:
            keys.check(self.data, p, NOW)
        kw = run.call_args.kwargs
        self.assertEqual((kw["stdin"], kw["stdout"], kw["stderr"]), (subprocess.DEVNULL,) * 3)
        self.assertEqual((kw["shell"], kw["timeout"]), (True, 60))

    def test_auth_failure_holds_ten_minutes(self):
        ran = self.tmp / "ran"
        p = self.save(key=counter(ran))
        (self.data / keys.STATE).write_text(json.dumps({"at": NOW.isoformat(), "ok": False, "auth": True,
                                                         "profile": "g", "error": keys.AUTH_AGAIN}))
        self.assertEqual(keys.check(self.data, p, NOW + timedelta(minutes=5)), keys.AUTH_AGAIN)
        self.assertEqual(runs(ran), 0)  # the key command works: it is not asked again for 10 minutes
        self.assertIsNone(keys.check(self.data, p, NOW + timedelta(minutes=11)))
        self.assertEqual(runs(ran), 1)

    def test_record_for_another_profile_is_not_used(self):
        ran = self.tmp / "ran"
        p = self.save(key=counter(ran, 1))
        (self.data / keys.STATE).write_text(json.dumps({"at": NOW.isoformat(), "ok": True, "error": None,
                                                         "profile": "h"}))
        self.assertIn("exit 1", keys.check(self.data, p, NOW))
        self.assertEqual(runs(ran), 1)
        self.assertEqual(self.state()["profile"], "g")

    def test_malformed_record_runs_the_command_again(self):
        ran = self.tmp / "ran"
        p = self.save(key=counter(ran))
        f = self.data / keys.STATE
        bad = ("not json", "[]", "\xff\xfe", json.dumps({"ok": True, "profile": "g"}),
               json.dumps({"at": "yesterday", "ok": True, "profile": "g"}),
               json.dumps({"at": (NOW + timedelta(hours=1)).isoformat(), "ok": True, "profile": "g"}))
        for i, text in enumerate(bad, 1):
            f.write_bytes(text.encode("utf-8", "surrogateescape") if text != "\xff\xfe" else b"\xff\xfe")
            self.assertIsNone(keys.check(self.data, p, NOW), text)
            self.assertEqual(runs(ran), i, text)
        f.write_text(json.dumps({"at": NOW.isoformat(), "ok": "yes", "profile": "g"}))  # ok must be true itself
        self.assertIsNone(keys.check(self.data, p, NOW))
        self.assertEqual(runs(ran), len(bad) + 1)
        # A fresh failing record with no error text is still a failure: it never reads as a working key.
        f.write_text(json.dumps({"at": NOW.isoformat(), "ok": False, "profile": "g"}))
        self.assertEqual(keys.check(self.data, p, NOW), "the key command failed")
        self.assertEqual(keys.cached_error(self.data, p), "the key command failed")
        self.assertEqual(runs(ran), len(bad) + 1)  # the cached failure is used: the command does not run
        self.assertIn("the key command failed", cap.refusal({"reason": "key"}))

    def test_symlinked_state_is_refused(self):
        p = self.save()
        elsewhere = self.tmp / "elsewhere.json"
        elsewhere.write_text(json.dumps({"at": NOW.isoformat(), "ok": True, "profile": "g"}))
        for name in (keys.STATE, keys.REFRESH):
            link = self.data / name
            link.symlink_to(elsewhere)
            with self.assertRaisesRegex(profiles.ProfileError, "symlink"):
                if name == keys.STATE:
                    keys.check(self.data, p, NOW)
                else:
                    self.end(self.transcript(auth_401(NOW)))
            self.assertTrue(link.is_symlink())
            link.unlink()
        dangling = self.data / keys.STATE
        dangling.symlink_to(self.tmp / "missing.json")
        with self.assertRaisesRegex(profiles.ProfileError, "symlink"):
            keys.check(self.data, p, NOW)
        self.assertIn("symlink", keys.cached_error(self.data, p))  # `list` shows it and reads nothing through it
        self.assertFalse((self.tmp / "missing.json").exists())
        self.assertEqual(json.loads(elsewhere.read_text())["ok"], True)

    def test_a_symlinked_state_is_never_written_or_removed(self):
        # The hold writes the key record and the refresh removes it: a symlink there is refused on both paths, the
        # link is kept and the file it points to is untouched.
        self.save(refresh_command="true")
        elsewhere = self.tmp / "elsewhere"
        elsewhere.write_text("keep me")
        link = self.data / keys.STATE
        link.symlink_to(elsewhere)
        (self.data / keys.REFRESH).write_text(json.dumps({"at": NOW.isoformat(), "profile": "g"}))
        with self.assertRaisesRegex(profiles.ProfileError, "symlink"):  # a second rejection: the hold
            self.end(self.transcript(auth_401(NOW + timedelta(minutes=2))), now=NOW + timedelta(minutes=3))
        self.assertTrue(link.is_symlink())
        self.assertEqual(elsewhere.read_text(), "keep me")
        (self.data / keys.REFRESH).unlink()
        with self.assertRaisesRegex(profiles.ProfileError, "symlink"):  # a first rejection: the refresh
            self.end(self.transcript(auth_401(NOW + timedelta(minutes=2))), now=NOW + timedelta(minutes=3))
        self.assertTrue(link.is_symlink())
        self.assertEqual(elsewhere.read_text(), "keep me")

    def test_subscription_has_no_key_error(self):
        self.assertIsNone(keys.check(self.data, profiles.load(self.data, profiles.SUBSCRIPTION), NOW))
        self.assertIsNone(keys.cached_error(self.data, profiles.load(self.data, profiles.SUBSCRIPTION)))
        self.assertFalse((self.data / keys.STATE).exists())

    def test_key_output_never_logged(self):
        if temp_inside_the_accounts_claude():
            self.skipTest("the temp folder is inside the account's ~/.claude, so ap2 refuses every temp data folder")
        p = self.save(key=PRINTS_SECRET + "; exit 1", refresh_command=PRINTS_SECRET,
                      rotate_command=PRINTS_SECRET + "; exit 2")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertIn("exit 1", keys.check(self.data, p, NOW))
            self.assertTrue(self.end(self.transcript(auth_401(NOW))))  # the refresh command ran
            with self.assertRaises(profiles.ProfileError) as e:
                keys.rotate(self.data, "g", NOW)
            self.assertNotIn(SECRET, str(e.exception))
            self.assertIn("exit 1", keys.check(self.data, p, NOW + timedelta(hours=1)))
        self.assertNotIn(SECRET, out.getvalue() + err.getvalue())
        home = self.tmp / "home"
        home.mkdir()
        env = {k: v for k, v in os.environ.items() if k != "AP2_DATA" and not k.startswith("AUTOPILOT_")}
        env.update(HOME=str(home), AUTOPILOT_DATA=str(self.tmp / "v1"))
        r = subprocess.run([sys.executable, AP2, "profile", "list", "--data", str(self.data)],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(SECRET, r.stdout + r.stderr)
        self.assertIn("exit 1", r.stdout)
        for f in self.data.rglob("*"):
            if f.is_file():
                self.assertNotIn(SECRET, f.read_text(errors="ignore"), str(f))


# -- 3. one refresh on a rejected key, then hold -------------------------------------------------------------------------

class RefreshTest(KeyCase):
    def test_auth_failure_refreshes_once(self):
        ref, key = self.tmp / "refreshed", self.tmp / "keyed"
        self.save(key=counter(key), refresh_command=counter(ref))
        (self.data / keys.STATE).write_text(json.dumps({"at": NOW.isoformat(), "ok": True, "profile": "g"}))
        sid = self.transcript({"type": "user", "prompt": "go"}, reply(NOW - timedelta(minutes=3)),
                              auth_401(NOW - timedelta(minutes=1)))
        self.assertTrue(self.end(sid))
        self.assertEqual((runs(ref), runs(key)), (1, 0))
        rec = self.refresh_record()
        self.assertEqual(rec["profile"], "g")
        self.assertEqual(datetime.fromisoformat(rec["at"]), NOW)
        self.assertFalse((self.data / keys.STATE).exists())  # the next check runs the key command

    def test_key_command_used_when_no_refresh_command(self):
        key = self.tmp / "keyed"
        self.save(key=counter(key))
        self.assertTrue(self.end(self.transcript(auth_401(NOW))))
        self.assertEqual(runs(key), 1)

    def test_refresh_command_runs_with_every_stream_closed(self):
        self.save(refresh_command="true")
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess("true", 0)) as run:
            self.assertTrue(self.end(self.transcript(auth_401(NOW))))
        kw = run.call_args.kwargs
        self.assertEqual((kw["stdin"], kw["stdout"], kw["stderr"]), (subprocess.DEVNULL,) * 3)
        self.assertEqual((run.call_args.args[0], kw["shell"], kw["timeout"]), ("true", True, 60))

    def test_second_failure_within_ten_minutes_sets_the_key_error(self):
        ref, key = self.tmp / "refreshed", self.tmp / "keyed"
        p = self.save(key=counter(key), refresh_command=counter(ref))
        self.assertTrue(self.end(self.transcript(auth_401(NOW - timedelta(seconds=30)))))
        later = NOW + timedelta(minutes=3)
        self.assertTrue(self.end(self.transcript(auth_401(NOW + timedelta(minutes=2))), now=later))
        self.assertEqual(runs(ref), 1)  # no second refresh
        rec = self.state()
        self.assertEqual((rec["ok"], rec["auth"], rec["profile"], rec["error"]), (False, True, "g", keys.AUTH_AGAIN))
        self.assertEqual(rec["error"], "the gateway rejected the key again right after a refresh")  # the spec's text
        self.assertEqual(keys.check(self.data, p, later + timedelta(minutes=5)), keys.AUTH_AGAIN)
        self.assertEqual(runs(key), 0)
        a = cap.allowance(self.data, self.projects, later + timedelta(minutes=5), p)
        self.assertEqual((a["allow"], a["reason"], a["error"]), (0, "key", keys.AUTH_AGAIN))
        # More than 10 minutes after the refresh, a new rejection refreshes again.
        self.assertTrue(self.end(self.transcript(auth_401(NOW + timedelta(minutes=14))), now=NOW + timedelta(minutes=15)))
        self.assertEqual(runs(ref), 2)

    def test_a_second_failure_closed_late_still_holds(self):
        # A session the gateway rejected ends its turn and is closed as idle up to an hour later: the failure's own
        # time, not the close's, says whether it came right after the refresh.
        ref, key = self.tmp / "refreshed", self.tmp / "keyed"
        self.save(key=counter(key), refresh_command=counter(ref))
        self.assertTrue(self.end(self.transcript(auth_401(NOW))))  # the refresh, at NOW
        self.assertEqual(runs(ref), 1)
        sid = self.transcript(auth_401(NOW + timedelta(minutes=2)))
        self.assertTrue(self.end(sid, now=NOW + timedelta(minutes=62)))
        self.assertEqual((runs(ref), runs(key)), (1, 0))  # no second refresh
        rec = self.state()
        self.assertEqual((rec["ok"], rec["auth"], rec["error"]), (False, True, keys.AUTH_AGAIN))
        self.assertEqual(datetime.fromisoformat(rec["at"]), NOW + timedelta(minutes=62))  # held from the close

    def test_a_third_failure_keeps_the_hold(self):
        # Every gateway worker is rejected at once: the first close refreshes, the second holds, and the third must
        # keep that hold, not lift it and refresh again.
        ref = self.tmp / "refreshed"
        p = self.save(refresh_command=counter(ref))
        self.assertTrue(self.end(self.transcript(auth_401(NOW))))  # the refresh, at NOW
        self.assertTrue(self.end(self.transcript(auth_401(NOW + timedelta(minutes=1))), now=NOW + timedelta(minutes=2)))
        self.assertTrue(self.end(self.transcript(auth_401(NOW + timedelta(minutes=2))), now=NOW + timedelta(minutes=3)))
        self.assertEqual(runs(ref), 1)  # refreshed once
        rec = self.state()
        self.assertEqual((rec["auth"], rec["error"]), (True, keys.AUTH_AGAIN))
        self.assertEqual(keys.check(self.data, p, NOW + timedelta(minutes=4)), keys.AUTH_AGAIN)

    def test_a_second_failure_long_after_the_refresh_refreshes_again(self):
        ref = self.tmp / "refreshed"
        self.save(refresh_command=counter(ref))
        self.assertTrue(self.end(self.transcript(auth_401(NOW))))  # the refresh, at NOW
        sid = self.transcript(auth_401(NOW + timedelta(minutes=11)))
        self.assertTrue(self.end(sid, now=NOW + timedelta(minutes=12)))
        self.assertEqual(runs(ref), 2)
        self.assertIsNone(self.state())  # no hold: the next check runs the key command
        self.assertEqual(datetime.fromisoformat(self.refresh_record()["at"]), NOW + timedelta(minutes=12))

    def test_an_error_with_no_time_is_judged_by_the_close(self):
        # An error line with no timestamp counts even after a refresh; whether it came right after that refresh is
        # then judged by the close time.
        ref = self.tmp / "refreshed"
        self.save(refresh_command=counter(ref))
        line = auth_401(NOW)
        del line["timestamp"]
        sid = self.transcript(line)
        (self.data / keys.REFRESH).write_text(json.dumps({"at": NOW.isoformat(), "profile": "g"}))
        self.assertTrue(self.end(sid, now=NOW + timedelta(minutes=3)))  # closed within 10 minutes: held
        rec = self.state()
        self.assertEqual((rec["auth"], rec["error"]), (True, keys.AUTH_AGAIN))
        self.assertEqual(runs(ref), 0)
        (self.data / keys.STATE).unlink()
        (self.data / keys.REFRESH).write_text(json.dumps({"at": NOW.isoformat(), "profile": "g"}))
        self.assertTrue(self.end(sid, now=NOW + timedelta(minutes=11)))  # closed later: refreshed again
        self.assertEqual(runs(ref), 1)
        self.assertIsNone(self.state())

    def test_old_error_before_the_refresh_is_ignored(self):
        ref = self.tmp / "refreshed"
        self.save(refresh_command=counter(ref))
        (self.data / keys.REFRESH).write_text(json.dumps({"at": NOW.isoformat(), "profile": "g"}))
        before = snapshot(self.data)
        self.assertFalse(self.end(self.transcript(auth_401(NOW - timedelta(minutes=1))), now=NOW + timedelta(minutes=2)))
        self.assertEqual(runs(ref), 0)
        self.assertEqual(snapshot(self.data), before)

    def test_refresh_of_another_profile_does_not_count(self):
        ref = self.tmp / "refreshed"
        self.save(refresh_command=counter(ref))
        (self.data / keys.REFRESH).write_text(json.dumps({"at": NOW.isoformat(), "profile": "h"}))
        self.assertTrue(self.end(self.transcript(auth_401(NOW + timedelta(minutes=1))), now=NOW + timedelta(minutes=2)))
        self.assertEqual(runs(ref), 1)
        self.assertNotIn("auth", self.state() or {})

    def test_recovered_session_is_not_a_failure(self):
        ref = self.tmp / "refreshed"
        self.save(refresh_command=counter(ref))
        sid = self.transcript(auth_401(NOW - timedelta(minutes=2)), reply(NOW - timedelta(minutes=1)))
        self.assertFalse(self.end(sid))
        self.assertEqual(runs(ref), 0)
        self.assertIsNone(self.refresh_record())

    def test_subscription_session_refreshes_nothing(self):
        ref = self.tmp / "refreshed"
        self.save(refresh_command=counter(ref))
        profiles.use(self.data, "g")  # the active profile is the gateway; the worker's own was the subscription
        sid = self.transcript(auth_401(NOW))
        before = snapshot(self.data)
        for name in (None, "", profiles.SUBSCRIPTION, "ghost"):
            self.assertFalse(self.end(sid, name=name), name)
        self.assertEqual(runs(ref), 0)
        self.assertEqual(snapshot(self.data), before)

    def test_no_transcript_refreshes_nothing(self):
        ref = self.tmp / "refreshed"
        self.save(refresh_command=counter(ref))
        for sid in ("", "00000000-0000-4000-8000-999999999999"):
            self.assertFalse(self.end(sid))
        self.assertEqual(runs(ref), 0)


class CloseTest(capt.CapWorkerCase):
    def test_close_runs_the_check_and_never_raises(self):
        ref = self.tmp / "refreshed"
        p = gateway("g", key_command="true", daily_cap_usd=500)
        p["gateway"]["refresh_command"] = counter(ref)
        profiles.save(self.data, p)
        profiles.use(self.data, "g")
        a = self.start()
        f = transcripts.find(self.fake_home / "projects", a["session_id"])
        with f.open("a", encoding="utf-8") as h:
            h.write(json.dumps(auth_401(self.clock())) + "\n")
        self.write_result(a["card"])
        out = self.workers().check()[0]
        self.assertEqual((out["state"], out["did"]), ("done", "ended"))
        self.assertEqual(runs(ref), 1)
        self.assertEqual(json.loads((self.data / keys.REFRESH).read_text())["profile"], "g")
        # A close whose key bookkeeping raises still finishes, with one stderr line.
        b = self.start()
        self.write_result(b["card"])
        err = io.StringIO()
        with mock.patch.object(keys, "note_session_end", side_effect=RuntimeError("boom\n" * 200)) as note, \
                contextlib.redirect_stderr(err):
            out = self.workers().check()[0]
        note.assert_called_once()
        self.assertEqual((out["state"], out["did"]), ("done", "ended"))
        self.assertEqual(self.records(), [])
        self.assertEqual(self.get(b["card"])["sessions"]["implement"]["state"], "ended")
        lines = err.getvalue().splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith("ap2: key health: "), lines[0])
        self.assertLessEqual(len(lines[0]), workers_mod.SPEND_LINE)

    def test_a_failed_stop_checks_no_key(self):
        # The key bookkeeping runs only once the stop returned: a stop that raised may not have landed, so the
        # session may still be running and its transcript is not final.
        ref = self.tmp / "refreshed"
        p = gateway("g", key_command="true", daily_cap_usd=500)
        p["gateway"]["refresh_command"] = counter(ref)
        profiles.save(self.data, p)
        profiles.use(self.data, "g")
        a = self.start()
        f = transcripts.find(self.fake_home / "projects", a["session_id"])
        with f.open("a", encoding="utf-8") as h:
            h.write(json.dumps(auth_401(self.clock())) + "\n")
        self.write_result(a["card"])
        stop = subprocess.TimeoutExpired("claude stop", 60)
        with mock.patch.object(workers_mod.Workers, "_kill", side_effect=stop), \
                mock.patch.object(keys, "note_session_end", wraps=keys.note_session_end) as note:
            out = self.workers().check()[0]
        note.assert_not_called()
        self.assertEqual(out["error"], "TimeoutExpired")
        self.assertEqual(runs(ref), 0)
        self.assertFalse((self.data / keys.REFRESH).exists())
        self.assertEqual([(r["short_id"], r["closing"]["path"]) for r in self.records()], [(a["short_id"], "done")])
        # The next check's stop works: that close finishes and the key is refreshed once.
        self.assertEqual(self.workers().check()[0]["did"], "ended")
        self.assertEqual(runs(ref), 1)
        self.assertEqual(self.records(), [])

    def test_a_hold_written_by_a_later_close_refuses_a_planned_resume(self):
        # Worker A (dead) is planned first, on the pass's allowance that said ok; worker B, listed after it, then
        # closes on a second rejection right after a refresh and writes the hold. A's resume must see that hold.
        ref = self.tmp / "refreshed"
        p = gateway("g", key_command="true", daily_cap_usd=500)
        p["gateway"]["refresh_command"] = counter(ref)
        profiles.save(self.data, p)
        profiles.use(self.data, "g")
        a, b = self.start(), self.start()
        self.assertEqual([r["card"] for r in self.records()], [a["card"], b["card"]])  # A is listed first
        (self.data / keys.REFRESH).write_text(json.dumps({"at": (self.clock() - timedelta(minutes=5)).isoformat(),
                                                          "profile": "g"}))
        self.drop_agent(a["short_id"])
        f = transcripts.find(self.fake_home / "projects", b["session_id"])
        with f.open("a", encoding="utf-8") as h:
            h.write(json.dumps(auth_401(self.clock())) + "\n")
        self.write_result(b["card"])
        launched = len(self.bg_calls())
        out = {e["card"]: e for e in self.workers().check()}
        self.assertEqual((out[a["card"]]["state"], out[a["card"]]["did"]), ("dead", "nothing"))
        self.assertIn("not resumed", out[a["card"]]["note"])
        self.assertIn(f"the gateway key does not work ({keys.AUTH_AGAIN})", out[a["card"]]["note"])
        self.assertNotIn("record changed", out[a["card"]]["note"])
        self.assertEqual((out[b["card"]]["state"], out[b["card"]]["did"]), ("done", "ended"))
        rec = json.loads((self.data / keys.STATE).read_text())
        self.assertEqual((rec["ok"], rec["auth"], rec["error"]), (False, True, keys.AUTH_AGAIN))
        self.assertEqual(runs(ref), 0)  # held instead of refreshed
        self.assertEqual(len(self.bg_calls()), launched)  # nothing launched for A
        self.assertEqual([(r["card"], r["resumes"]) for r in self.records()], [(a["card"], 0)])  # its resume kept

    def test_close_uses_the_workers_own_profile(self):
        ref = self.tmp / "refreshed"
        a = self.start()  # on the subscription
        p = gateway("g", key_command="true", daily_cap_usd=500)
        p["gateway"]["refresh_command"] = counter(ref)
        profiles.save(self.data, p)
        profiles.use(self.data, "g")  # switched after the start: the worker keeps the subscription
        f = transcripts.find(self.fake_home / "projects", a["session_id"])
        with f.open("a", encoding="utf-8") as h:
            h.write(json.dumps(auth_401(self.clock())) + "\n")
        self.write_result(a["card"])
        self.assertEqual(self.workers().check()[0]["did"], "ended")
        self.assertEqual(runs(ref), 0)
        self.assertFalse((self.data / keys.REFRESH).exists())


# -- 4. a safety-guard refusal is not a key failure (lessons 4.3) --------------------------------------------------------

class AuthTextTest(KeyCase):
    def setUp(self):
        super().setUp()
        self.ref, self.key, self.rot = self.tmp / "refreshed", self.tmp / "keyed", self.tmp / "rotated"
        self.save(key=counter(self.key), refresh_command=counter(self.ref), rotate_command=counter(self.rot))

    def assert_not_auth(self, *lines):
        sid = self.transcript(*lines)
        errors = self.errors(sid)
        self.assertTrue(errors)
        self.assertFalse(keys.auth_failed(errors, None))
        before = snapshot(self.data)
        self.assertFalse(self.end(sid))
        self.assertEqual((runs(self.ref), runs(self.key), runs(self.rot)), (0, 0, 0))
        self.assertEqual(snapshot(self.data), before)

    def test_400_safety_guard_never_refreshes(self):
        self.assert_not_auth(api_error(NOW, SAFETY, status=400, error="invalid_request"))
        # The status decides: a 400 is never an auth failure, whatever its text says.
        self.assert_not_auth(api_error(NOW, "API Error: 400 authentication_error invalid api key 401", status=400))

    def test_safety_guard_text_without_status(self):
        self.assert_not_auth(api_error(NOW, "API Error: 403 Request blocked by Safety Guard"))
        self.assert_not_auth(api_error(NOW, "Request blocked", error="401 SAFETY GUARD"))

    def test_safety_guard_text_wins_over_an_auth_status(self):
        # A gateway that sends its safety-guard refusal as a 401 or 403 still refreshes nothing: the text is
        # judged before the status.
        self.assert_not_auth(api_error(NOW, "API Error: 403 Request blocked by safety guard", status=403))
        self.assert_not_auth(api_error(NOW, "API Error: 401 Request blocked by safety guard", status=401))

    def test_429_limit_is_not_auth(self):
        self.assert_not_auth(api_error(NOW, LIMIT, status=429, error="rate_limit"))
        self.assert_not_auth(api_error(NOW, "API Error: 500 authentication service down 401", status=500))

    def test_403_status_is_auth(self):
        sid = self.transcript(api_error(NOW, "Forbidden", status=403, error="unknown"))
        self.assertTrue(keys.auth_failed(self.errors(sid), None))
        self.assertTrue(self.end(sid))
        self.assertEqual(runs(self.ref), 1)
        self.assertEqual(runs(self.rot), 0)  # nothing rotates on its own

    def test_auth_text_without_status_is_auth(self):
        for text, error in (("API Error: 401 Unauthorized", "unknown"), ("Invalid API key · Please run /login", ""),
                            ("API Error", "authentication_failed"), ("Authentication error", "")):
            e = api_error(NOW, text, error=error)
            self.assertTrue(keys.auth_failed(self.errors(self.transcript(e)), None), text)
        self.assertFalse(keys.auth_failed(self.errors(self.transcript(
            api_error(NOW, "API Error: overloaded", error="overloaded"))), None))

    def test_after_skips_older_errors(self):
        errors = self.errors(self.transcript(auth_401(NOW)))
        self.assertFalse(keys.auth_failed(errors, NOW))
        self.assertFalse(keys.auth_failed(errors, NOW + timedelta(seconds=1)))
        self.assertTrue(keys.auth_failed(errors, NOW - timedelta(seconds=1)))
        self.assertFalse(keys.auth_failed([], None))


# -- 5. the transcript tail's API errors (`apistop.tail`, the one reader) -------------------------------------------------

class TranscriptTailTest(KeyCase):
    """What the reader itself does (its lines, marks, order and cut) is `tests/test_ap2_apistop.py`'s: these pin only
    what the key health reads through it."""

    def test_a_system_line_after_the_error_still_refreshes(self):
        # Claude Code writes a `system` line (turn_duration) right after an API error: only an assistant reply
        # clears the errors, so the session still ends on the rejected key and refreshes it.
        ref = self.tmp / "refreshed"
        self.save(refresh_command=counter(ref))
        turn = {"type": "system", "subtype": "turn_duration", "timestamp": at(NOW + timedelta(seconds=1)),
                "durationMs": 1000}
        sid = self.transcript(reply(NOW - timedelta(minutes=1)), auth_401(NOW), turn)
        self.assertTrue(self.end(sid, now=NOW + timedelta(seconds=2)))
        self.assertEqual(runs(ref), 1)

    def test_only_the_tail_is_read(self):
        # A 401 at the end of a transcript longer than the tail refreshes; one only before the tail does not.
        ref = self.tmp / "refreshed"
        self.save(refresh_command=counter(ref))
        filler = "".join(json.dumps({"type": "user", "prompt": "p" * 1000}) + "\n" for _ in range(100))
        self.assertGreater(len(filler), apistop.TAIL_BYTES)
        sid = self.transcript(raw=json.dumps(auth_401(NOW)) + "\n" + filler)
        self.assertFalse(self.end(sid, now=NOW + timedelta(seconds=1)))  # the early error is past the tail
        self.assertEqual(runs(ref), 0)
        sid = self.transcript(raw=filler + json.dumps(auth_401(NOW)) + "\n")
        self.assertTrue(self.end(sid, now=NOW + timedelta(seconds=1)))
        self.assertEqual(runs(ref), 1)


# -- 6, 7. ap2 profile rotate and list -----------------------------------------------------------------------------------

class CliCase(KeyCase):
    def setUp(self):
        if temp_inside_the_accounts_claude():
            self.skipTest("the temp folder is inside the account's ~/.claude, so ap2 refuses every temp data folder")
        super().setUp()
        home = self.tmp / "home"
        home.mkdir()
        self.env = {k: v for k, v in os.environ.items() if k != "AP2_DATA" and not k.startswith("AUTOPILOT_")}
        self.env.update(HOME=str(home), AUTOPILOT_DATA=str(self.tmp / "v1-data"), PYTHONIOENCODING="utf-8")

    def ap2(self, *args):
        return subprocess.run([sys.executable, AP2, "profile", *args, "--data", str(self.data)],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, env=self.env, timeout=60)

    def fails(self, *args, text=""):
        r = self.ap2(*args)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(r.stdout, "")
        self.assertEqual(len(r.stderr.splitlines()), 1, r.stderr)
        self.assertTrue(r.stderr.startswith("ap2: "), r.stderr)
        self.assertIn(text, r.stderr)
        return r.stderr

    def caches(self, profile="g"):
        (self.data / keys.STATE).write_text(json.dumps({"at": NOW.isoformat(), "ok": False, "auth": True,
                                                         "profile": profile, "error": keys.AUTH_AGAIN}))
        (self.data / keys.REFRESH).write_text(json.dumps({"at": NOW.isoformat(), "profile": profile}))


class RotateCliTest(CliCase):
    def test_rotate_runs_command_and_clears_the_cache(self):
        rot = self.tmp / "rotated"
        self.save(rotate_command=counter(rot))
        self.caches()
        r = self.ap2("rotate", "g")
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertEqual(json.loads(r.stdout), {"rotated": "g"})
        self.assertEqual(runs(rot), 1)
        self.assertFalse((self.data / keys.STATE).exists())
        self.assertFalse((self.data / keys.REFRESH).exists())

    def test_rotate_keeps_another_profiles_refresh(self):
        self.save(rotate_command="true")
        self.caches(profile="h")
        self.assertEqual(self.ap2("rotate", "g").returncode, 0)
        self.assertFalse((self.data / keys.STATE).exists())  # the next check runs the key command again
        self.assertEqual(self.refresh_record()["profile"], "h")

    def test_rotate_runs_with_every_stream_closed(self):
        self.save(rotate_command="true")
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess("true", 0)) as run:
            self.assertEqual(keys.rotate(self.data, "g", NOW), {"rotated": "g"})
        kw = run.call_args.kwargs
        self.assertEqual((kw["stdin"], kw["stdout"], kw["stderr"]), (subprocess.DEVNULL,) * 3)
        self.assertEqual((kw["shell"], kw["timeout"]), (True, 120))

    def test_rotate_refuses_the_subscription(self):
        self.fails("rotate", "subscription", text="subscription")
        self.fails("rotate", "ghost", text="ghost")

    def test_rotate_refuses_a_profile_without_rotate_command(self):
        self.save()
        self.caches()
        self.fails("rotate", "g", text="rotate_command")
        self.assertTrue((self.data / keys.STATE).exists())

    def test_rotate_failure_hides_output(self):
        self.save(rotate_command=PRINTS_SECRET + "; exit 3")
        self.caches()
        err = self.fails("rotate", "g", text="rotate_command failed (exit 3)")
        self.assertNotIn(SECRET, err)
        self.assertTrue((self.data / keys.STATE).exists())  # nothing is cleared when the rotation failed

    def test_rotate_works_in_shadow_mode(self):
        rot = self.tmp / "rotated"
        self.save(rotate_command=counter(rot))
        (self.data / "config.yaml").write_text("mode: shadow\n")
        r = self.ap2("rotate", "g")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(runs(rot), 1)

    def test_help_names_rotate(self):
        r = subprocess.run([sys.executable, AP2, "profile", "--help"], stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, env=self.env, timeout=60)
        self.assertEqual(r.returncode, 0)
        self.assertIn("rotate", r.stdout)


class ListCliTest(CliCase):
    def test_list_shows_cached_key_error_and_runs_nothing(self):
        ran = self.tmp / "ran"
        p = self.save(key=counter(ran, 1))
        self.save("h", key=counter(ran, 1))
        self.save("ok", key="true")
        r = self.ap2("list")
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = {row["name"]: row for row in json.loads(r.stdout)["profiles"]}
        self.assertEqual((rows["g"]["key_error"], rows["h"]["key_error"]), (None, None))  # never checked
        self.assertNotIn("key_error", rows["subscription"])
        keys.check(self.data, p, NOW)
        self.assertEqual(runs(ran), 1)
        r = self.ap2("list")
        rows = {row["name"]: row for row in json.loads(r.stdout)["profiles"]}
        self.assertEqual(rows["g"]["key_error"], "the key command failed (exit 1)")
        self.assertIsNone(rows["h"]["key_error"])  # the record is g's
        self.assertEqual(runs(ran), 1)  # list runs no command
        keys.check(self.data, profiles.load(self.data, "ok"), NOW)
        rows = {row["name"]: row for row in json.loads(self.ap2("list").stdout)["profiles"]}
        self.assertEqual((rows["g"]["key_error"], rows["ok"]["key_error"]), (None, None))


if __name__ == "__main__":
    unittest.main()
