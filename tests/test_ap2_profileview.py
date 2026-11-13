"""What the pages show about access (design section 12, access on the pages): the profile badge's facts, the key and
profile errors, the cap card's state and the usage by mode, read without running a command or writing a file."""
from __future__ import annotations

import http.client
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.test_ap2_profiles import gateway
from tests.test_ap2_server import tree

from assistant import cap, datadir, keys, phone, profiles, profileview, server, spend
from assistant.config import Config
from assistant.store import Store

TZ = timezone(timedelta(hours=-5))
NOW = datetime(2026, 11, 11, 15, 0, tzinfo=TZ)  # 15:00 local, a Wednesday
CHOSEN = "2026-11-01T09:00:00-05:00"


def totals(today=0.0, week=None, month=None, unknown=False, unknown_today=False) -> dict:
    return {"today": today, "week": today if week is None else week, "month": today if month is None else month,
            "unknown": unknown, "unknown_today": unknown_today, "unknown_models": [], "unknown_launches": 0}


def usage_json(path: Path, now=NOW, percent=42, kind="five_hour", used=1234, limit=5000) -> Path:
    path.write_text(json.dumps({"cachedUsageUtilization": {
        "fetchedAtMs": int(now.timestamp() * 1000) - 60_000,
        "utilization": {"limits": [{"kind": kind, "percent": percent, "resets_at": "2026-11-11T18:00:00+00:00"},
                                   {"kind": "seven_day", "percent": 7, "resets_at": "2026-11-15T00:00:00+00:00"}],
                        "extra_usage": {"used_credits": used, "monthly_limit": limit, "decimal_places": 2}}}}))
    return path


class ViewCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-profileview-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = datadir.ensure(self.tmp / "data")
        self.usage = usage_json(self.tmp / "claude.json")
        self.counter = self.tmp / "key-runs"

    def counting_gateway(self, name="g", key_command=None, **over):
        """A gateway profile whose key command (by default) adds a line to a counter file each time it runs."""
        p = gateway(name, key_command=key_command or f"echo run >> {self.counter}", daily_cap_usd=500,
                    chosen_at=CHOSEN, label="Team Proj", model="claude-opus-5-5[1m]", effort="high")
        p.update(over)
        return profiles.save(self.data, p)

    def runs(self) -> int:
        return len(self.counter.read_text().splitlines()) if self.counter.exists() else 0

    def summary(self, now=NOW, usage_file=None):
        return profileview.summary(self.data, now, usage_file or self.usage)

    def state_over_http(self, cfg=None) -> tuple:
        srv = server.make_server(self.data, port=0, cfg=cfg or Config(), usage_file=self.usage,
                                 clock=lambda: NOW.astimezone(timezone.utc))
        srv.log_stream = io.StringIO()
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection(server.HOST, srv.server_address[1], timeout=10)
            conn.request("GET", "/api/state", headers={"Host": f"{server.HOST}:{srv.server_address[1]}"})
            r = conn.getresponse()
            raw = r.read()
            conn.close()
            return r.status, raw
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(10)


class SummaryTest(ViewCase):
    def test_subscription_summary(self):
        out = self.summary()
        self.assertEqual(set(out), {"access", "usage"})
        a = out["access"]
        self.assertEqual(a["profile"], {"name": "subscription", "label": "Subscription", "mode": "subscription",
                                        "model": None, "effort": "xhigh", "daily_cap_usd": None})
        self.assertIsNone(a["profile_error"])
        self.assertEqual(a["page_sessions"], "subscription")
        self.assertEqual(a["profiles"], [{"name": "subscription", "label": "Subscription", "mode": "subscription",
                                          "needs_choice": False}])
        self.assertEqual(a["broken_profiles"], [])
        self.assertIsNone(a["key_error"])
        self.assertEqual(a["cap"], {"day": "2026-11-11", "card": None, "card_id": None, "limit": None,
                                    "raise_pct": 0, "paused": False, "pause": None})
        u = out["usage"]
        self.assertEqual(u["mode"], "subscription")
        self.assertIsNone(u["gateway"])
        self.assertEqual(u["windows"][0], {"kind": "five_hour", "model": None, "percent": 42,
                                           "resets_at": "2026-11-11T18:00:00+00:00"})
        self.assertFalse(u["stale"])
        self.assertEqual((u["credits_used_usd"], u["credits_limit_usd"]), (12.34, 50.0))

    def test_gateway_summary_shows_label_model_effort_and_page_sessions(self):
        self.counting_gateway()
        profiles.save(self.data, gateway("h", label="Other", chosen_at=None))
        profiles.use(self.data, "g")
        a = self.summary()["access"]
        self.assertEqual(a["profile"], {"name": "g", "label": "Team Proj", "mode": "gateway",
                                        "model": "claude-opus-5-5[1m]", "effort": "high", "daily_cap_usd": 500})
        self.assertEqual(a["page_sessions"], "subscription")
        self.assertEqual(a["profiles"], [
            {"name": "g", "label": "Team Proj", "mode": "gateway", "needs_choice": False},
            {"name": "h", "label": "Other", "mode": "gateway", "needs_choice": True},
            {"name": "subscription", "label": "Subscription", "mode": "subscription", "needs_choice": False}])
        self.assertEqual(a["cap"]["limit"], 500.0)
        self.assertIsNone(a["key_error"])  # no key check has run yet: no record, no error
        self.assertEqual(self.summary()["usage"]["mode"], "gateway")
        self.assertEqual(self.runs(), 0)

    def test_broken_active_profile_is_profile_error_not_a_crash(self):
        (self.data / profiles.FOLDER).mkdir(exist_ok=True)
        (self.data / profiles.FOLDER / "bad.json").write_text("not json")
        (self.data / profiles.ACTIVE).write_text(json.dumps({"name": "bad"}))
        out = self.summary()
        a = out["access"]
        self.assertIsNone(a["profile"])
        self.assertIn(profiles.FIX, a["profile_error"])
        self.assertIn("bad.json", a["profile_error"])
        self.assertIsNone(a["key_error"])
        self.assertIsNone(a["cap"]["limit"])
        self.assertIsNone(out["usage"]["mode"])
        self.assertIsNone(out["usage"]["gateway"])
        self.assertEqual([b["name"] for b in a["broken_profiles"]], ["bad"])
        (self.data / profiles.ACTIVE).write_text("{oops")
        self.assertIn(profiles.FIX, self.summary()["access"]["profile_error"])
        status, raw = self.state_over_http()
        self.assertEqual(status, 200, raw)
        self.assertIn(profiles.FIX, json.loads(raw)["access"]["profile_error"])

    def test_broken_other_profile_is_listed(self):
        (self.data / profiles.FOLDER).mkdir(exist_ok=True)
        (self.data / profiles.FOLDER / "zz.json").write_text(json.dumps({"name": "zz", "mode": "nope"}))
        a = self.summary()["access"]
        self.assertEqual(a["profile"]["name"], "subscription")
        self.assertIsNone(a["profile_error"])
        self.assertEqual([p["name"] for p in a["profiles"]], ["subscription"])
        self.assertEqual(len(a["broken_profiles"]), 1)
        self.assertEqual(a["broken_profiles"][0]["name"], "zz")
        self.assertIn("zz.json", a["broken_profiles"][0]["error"])

    def test_a_cap_too_big_for_a_float_or_a_too_deep_file_is_an_error_field(self):
        (self.data / profiles.FOLDER).mkdir(exist_ok=True)
        big = json.dumps(gateway("big", daily_cap_usd=-1)).replace('"daily_cap_usd": -1', '"daily_cap_usd": 1'
                                                                    + "0" * 400)
        self.assertIn("0" * 400, big)
        (self.data / profiles.FOLDER / "big.json").write_text(big)
        (self.data / profiles.FOLDER / "deep.json").write_text(json.dumps(gateway("deep")))
        load = profiles.load
        deep = RecursionError("maximum recursion depth exceeded")  # json.loads on a deep nest, on some Pythons only

        def too_deep(data, name):
            if name == "deep":
                raise deep
            return load(data, name)

        with mock.patch.object(profiles, "load", side_effect=too_deep):
            a = self.summary()["access"]
        self.assertEqual(a["profile"]["name"], "subscription")
        self.assertIsNone(a["profile_error"])
        broken = {b["name"]: b["error"] for b in a["broken_profiles"]}
        self.assertEqual(set(broken), {"big", "deep"})
        self.assertTrue(broken["big"].startswith("big: OverflowError: "), broken["big"])
        self.assertTrue(broken["deep"].startswith("deep: RecursionError: "), broken["deep"])
        self.assertNotIn(str(self.tmp), json.dumps(a))
        status, raw = self.state_over_http()
        self.assertEqual(status, 200, raw)
        self.assertIn("big", [b["name"] for b in json.loads(raw)["access"]["broken_profiles"]])
        built = phone.build(self.data, cfg=Config(), now=NOW.astimezone(timezone.utc), usage_file=self.usage)
        self.assertIn("big", [b["name"] for b in built["access"]["broken_profiles"]])
        # The active one: the same text is the profile error, with the fix.
        (self.data / profiles.ACTIVE).write_text(json.dumps({"name": "big"}))
        a = self.summary()["access"]
        self.assertIsNone(a["profile"])
        self.assertTrue(a["profile_error"].startswith("big: OverflowError: "), a["profile_error"])
        self.assertIn(profiles.FIX, a["profile_error"])
        with mock.patch.object(profiles, "active", side_effect=deep), \
                mock.patch.object(profiles, "active_name", side_effect=deep):
            a = self.summary()["access"]
            status, raw = self.state_over_http()
        self.assertTrue(a["profile_error"].startswith("the active profile: RecursionError: "), a["profile_error"])
        self.assertIn(profiles.FIX, a["profile_error"])
        self.assertEqual(status, 200, raw)

    def test_an_unreadable_profiles_folder_is_an_error_field(self):
        (self.data / profiles.FOLDER).mkdir(exist_ok=True)
        os.rename(self.data / profiles.FOLDER, self.tmp / "real-profiles")
        (self.data / profiles.FOLDER).symlink_to(self.tmp / "real-profiles")
        a = self.summary()["access"]
        self.assertIn("symlink", a["error"])
        self.assertEqual(a["profiles"], [])
        self.assertIsNone(a["profile"])
        self.assertIn("symlink", a["profile_error"])

    def test_key_error_is_the_refusal_text(self):
        p = self.counting_gateway(key_command="exit 1")
        profiles.use(self.data, "g")
        err = keys.check(self.data, p, NOW)
        self.assertTrue(err)
        a = self.summary()["access"]
        self.assertEqual(a["key_error"], cap.refusal({"reason": "key", "error": keys.cached_error(self.data, p)}))
        self.assertIn(keys.KEY_FIX, a["key_error"])
        # A record for another profile is not this profile's error.
        self.counting_gateway("h")
        profiles.use(self.data, "h")
        self.assertIsNone(self.summary()["access"]["key_error"])

    def test_the_key_hold_line_names_the_profile(self):
        p = self.counting_gateway("team-gw")
        profiles.use(self.data, "team-gw")
        # The record a second rejection right after a refresh writes (keys.note_session_end): the hold.
        (self.data / keys.STATE).write_text(json.dumps({"profiles": {"team-gw": {
            "at": NOW.isoformat(), "ok": False, "auth": True, "profile": "team-gw", "error": keys.AUTH_AGAIN}}}))
        a = self.summary()["access"]
        self.assertIn("ap2 profile rotate team-gw", a["key_error"])
        self.assertNotIn("NAME", a["key_error"])
        refused = cap.allowance(self.data, self.tmp / "projects", NOW, p)
        self.assertEqual(refused["reason"], "key")
        self.assertEqual(a["key_error"], cap.refusal(refused))
        self.assertEqual(self.runs(), 0)  # the hold runs no key command

    def test_an_odd_key_record_or_pause_file_is_an_error_not_a_crash(self):
        self.counting_gateway()
        profiles.use(self.data, "g")
        deep = RecursionError("maximum recursion depth exceeded")  # json.loads on a deep nest, on some Pythons only
        with mock.patch.object(keys, "cached_error", side_effect=deep):
            a = self.summary()["access"]
            status, raw = self.state_over_http()
        self.assertEqual(status, 200, raw)
        self.assertIn(f"{keys.STATE}: RecursionError: ", a["key_error"])
        with mock.patch.object(cap, "paused", side_effect=deep):
            c = self.summary()["access"]["cap"]
            status, raw = self.state_over_http()
        self.assertEqual(status, 200, raw)
        self.assertTrue(c["paused"])  # never start work by accident
        self.assertEqual(c["pause"], {"reason": "unknown", "error": f"{cap.PAUSED}: RecursionError: {deep}"})

    def test_a_raise_too_big_for_a_float_is_a_cap_error_field(self):
        # The HTTP and phone legs read the cap day in this machine's zone: pin one where NOW's day is 2026-11-11.
        self.addCleanup(time.tzset)
        zone = mock.patch.dict(os.environ, {"TZ": "America/New_York"})
        zone.start()
        self.addCleanup(zone.stop)
        time.tzset()
        self.counting_gateway()
        profiles.use(self.data, "g")
        big = json.dumps({"day": "2026-11-11", "raise_pct": 7, "card": None}).replace('"raise_pct": 7',
                                                                                       '"raise_pct": 1' + "0" * 400)
        self.assertIn("0" * 400, big)
        (self.data / cap.STATE).write_text(big)
        c = self.summary()["access"]["cap"]
        self.assertTrue(c["error"].startswith(f"{cap.STATE}: OverflowError: "), c["error"])
        self.assertNotIn(str(self.tmp), c["error"])
        self.assertEqual(c["day"], "2026-11-11")
        status, raw = self.state_over_http()
        self.assertEqual(status, 200, raw)
        self.assertIn("OverflowError", json.loads(raw)["access"]["cap"]["error"])
        built = phone.build(self.data, cfg=Config(), now=NOW.astimezone(timezone.utc), usage_file=self.usage)
        self.assertIn("OverflowError", built["access"]["cap"]["error"])

    def test_cap_block_shows_todays_open_card_and_the_pause(self):
        self.counting_gateway()
        profiles.use(self.data, "g")
        store = Store(self.data, Config(), clock=lambda: NOW)
        cap.open_card(self.data, NOW, 500.0, "spent", totals(510.0), "g", store)
        c = self.summary()["access"]["cap"]
        self.assertEqual((c["card"], c["day"], c["limit"], c["paused"]), ("open", "2026-11-11", 500.0, False))
        self.assertEqual(c["card_id"], cap.state(self.data, NOW)["card_id"])
        cap.pause(self.data, NOW)
        c = self.summary()["access"]["cap"]
        self.assertTrue(c["paused"])
        self.assertEqual(c["pause"]["reason"], "cap")
        # Tomorrow the card is another day's: the state reads blank.
        c = self.summary(now=NOW + timedelta(days=1))["access"]["cap"]
        self.assertEqual((c["card"], c["card_id"], c["day"]), (None, None, "2026-11-12"))

    def test_a_symlinked_cap_state_is_an_error_field_not_a_crash(self):
        (self.data / cap.STATE).symlink_to(self.tmp / "nowhere.json")
        c = self.summary()["access"]["cap"]
        self.assertIn("symlink", c["error"])
        self.assertEqual(c["day"], "2026-11-11")

    def test_summary_runs_no_command_and_writes_nothing(self):
        self.counting_gateway()
        profiles.use(self.data, "g")
        (self.data / keys.STATE).write_text(json.dumps({"at": NOW.isoformat(), "ok": False, "error": "old",
                                                       "profile": "g"}))
        spend.remember(self.data, totals(3.0), NOW)
        before = tree(self.data)
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("a process")) as popen, \
                mock.patch.object(spend, "totals", side_effect=AssertionError("the ledger")) as tot:
            out = self.summary()
            for days in (0, 1, 40):
                self.summary(now=NOW + timedelta(days=days))
        popen.assert_not_called()
        tot.assert_not_called()
        self.assertEqual(self.runs(), 0)
        self.assertEqual(tree(self.data), before)
        self.assertIn("old", out["access"]["key_error"])
        missing = self.tmp / "nowhere"
        profileview.summary(missing, NOW, self.tmp / "no-usage.json")
        self.assertFalse(missing.exists())

    def test_no_key_value_anywhere(self):
        p = self.counting_gateway(key_command="echo SECRET123; exit 1")
        profiles.use(self.data, "g")
        self.assertTrue(keys.check(self.data, p, NOW))
        self.assertEqual(self.runs(), 0)  # the counter is not in this command; the key ran once, above
        status, raw = self.state_over_http()
        self.assertEqual(status, 200)
        self.assertNotIn(b"SECRET123", raw)
        self.assertIn("key command failed", json.loads(raw)["access"]["key_error"])
        built = phone.build(self.data, cfg=Config(), now=NOW.astimezone(timezone.utc), usage_file=self.usage)
        self.assertNotIn("SECRET123", json.dumps(built))
        self.assertIn("key command failed", built["access"]["key_error"])
        for f in self.data.rglob("*"):  # the profile file stores the command's text, by design; nothing else may
            if f.is_file() and f.parent.name != profiles.FOLDER:
                self.assertNotIn(b"SECRET123", f.read_bytes(), f)


class UsageTest(ViewCase):
    def setUp(self):
        super().setUp()
        self.counting_gateway()
        profiles.use(self.data, "g")

    def test_gateway_usage_from_the_last_count(self):
        self.assertEqual(self.summary()["usage"]["gateway"],
                         {"day": None, "counted_at": None, "today": None, "week": None, "month": None,
                          "unknown": None, "unknown_today": None, "limit": 500.0})
        spend.remember(self.data, totals(512.0, week=900.0, month=2300.0, unknown=True), NOW - timedelta(minutes=5))
        with mock.patch.object(spend, "totals", side_effect=OSError("never read")):
            u = self.summary()["usage"]
        self.assertEqual(u["mode"], "gateway")
        self.assertEqual(u["gateway"], {"day": "2026-11-11", "counted_at": (NOW - timedelta(minutes=5)).isoformat(),
                                        "today": 512.0, "week": 900.0, "month": 2300.0, "unknown": True,
                                        "unknown_today": False, "limit": 500.0})
        # The page sessions still use the subscription window, so it is still read.
        self.assertEqual(u["windows"][0]["percent"], 42)
        cap.answer(self.data, "raise50", NOW)
        self.assertEqual(self.summary()["usage"]["gateway"]["limit"], 750.0)

    def test_a_count_from_yesterday_is_not_today(self):
        spend.remember(self.data, totals(80.0, week=120.0, month=300.0, unknown_today=True, unknown=True),
                       NOW - timedelta(days=1))
        g = self.summary()["usage"]["gateway"]
        self.assertEqual(g["day"], "2026-11-10")
        self.assertIsNone(g["today"])
        self.assertIsNone(g["unknown_today"])
        self.assertEqual((g["week"], g["month"], g["unknown"]), (120.0, 300.0, True))

    def test_subscription_usage_reads_the_usage_file(self):
        profiles.use(self.data, "subscription")
        spend.remember(self.data, totals(9.0), NOW)
        other = usage_json(self.tmp / "other.json", percent=97, kind="seven_day_opus", used=0, limit=0)
        u = self.summary(usage_file=other)["usage"]
        self.assertEqual(u["mode"], "subscription")
        self.assertIsNone(u["gateway"])
        self.assertEqual([w["percent"] for w in u["windows"]], [97, 7])
        self.assertEqual(u["windows"][0]["kind"], "seven_day_opus")
        stale = usage_json(self.tmp / "stale.json", now=NOW - timedelta(hours=3))
        self.assertTrue(self.summary(usage_file=stale)["usage"]["stale"])

    def test_unreadable_usage_file_gives_null_windows(self):
        bad = self.tmp / "bad.json"
        bad.write_text("{nope")
        for f in (bad, self.tmp / "missing.json", self.tmp):
            with self.subTest(f):
                u = self.summary(usage_file=f)["usage"]
                self.assertIsNone(u["windows"])
                self.assertIsNone(u["stale"])
                self.assertIsNone(u["credits_used_usd"])
                self.assertIsNone(u["credits_limit_usd"])
                self.assertEqual(u["mode"], "gateway")
                self.assertNotIn("error", u)


if __name__ == "__main__":
    unittest.main()
