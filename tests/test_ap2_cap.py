"""The daily cap of a gateway profile (design section 12, daily cap): at today's limit (or at cost that cannot be
priced) no worker starts or resumes, one card per day asks the user to raise the limit, accept the unknown cost or
pause, the pause stops and parks every running worker, and parking is split by mode. Ported from v1's
`tests/test_cap.py` (v1 commit 519346b). Every folder is a temp folder; workers run the fake claude
(`tests/bin/fake-claude-v2`), and `spend.totals` is a stub where a test only needs a number."""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT
from tests.test_ap2_profiles import gateway
from tests.test_ap2_store import card
from tests.test_ap2_workers import FAKE, WorkerCase, usage_file
from assistant import cap, cards, config, parkhook, profiles, resources, spend
from assistant.cards import CardError
from assistant import workers as workers_mod
from assistant.config import Config, ConfigError
from assistant.resources import WorkerError
from assistant.store import Store

TZ = timezone(timedelta(hours=-5))
NOW = datetime(2026, 11, 11, 15, 0, tzinfo=TZ)  # 15:00 local, a Wednesday
AP2 = str(ROOT / "bin" / "ap2")
HAIKU_1M = json.loads((ROOT / "lib" / "assistant" / "prices.json").read_text())["models"]["claude-haiku-4-5"]["input"]


def totals(today=0.0, unknown_today=False, models=(), launches=0) -> dict:
    """What `spend.totals` returns."""
    return {"today": today, "week": today, "month": today, "unknown": unknown_today, "unknown_today": unknown_today,
            "unknown_models": list(models), "unknown_launches": launches}


class CapCase(unittest.TestCase):
    cfg = Config(primary_language="en", secondary_language="zh")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-cap-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.projects = self.tmp / "projects"
        self.projects.mkdir()
        profiles.save(self.data, gateway("g", daily_cap_usd=500))
        profiles.use(self.data, "g")
        self.p = profiles.active(self.data)
        self.now = NOW
        self.store = Store(self.data, self.cfg, clock=lambda: self.now)
        self.spent = totals()
        self.totals_calls = 0

    def fake_totals(self, data, projects, now=None, mode="gateway", listing=None):
        self.totals_calls += 1
        self.assertEqual(mode, "gateway")
        return dict(self.spent)

    def allow(self, now=None, p=None):
        with mock.patch.object(spend, "totals", side_effect=self.fake_totals):
            return cap.allowance(self.data, self.projects, now or self.now, p or self.p, store=self.store)

    def answer(self, option, now=None, **kw):
        return cap.answer(self.data, option, now or self.now, store=self.store, **kw)

    def cap_cards(self) -> list:
        return [c for c in self.store.list() if c.get("feed") == "cap"]

    def text(self, card) -> str:
        return " ".join(card["plain"][line][lang] for line in cards.PLAIN_LINES for lang in ("en", "zh"))


# -- 1. the allowance -------------------------------------------------------------------------------------------------

class AllowanceTest(CapCase):
    def test_cap_reached_blocks_new_work(self):
        self.spent = totals(500.0)
        a = self.allow()
        self.assertEqual((a["allow"], a["reason"], a["cause"], a["limit"]), (0, "cap", "spent", 500.0))
        self.assertEqual(a["spent"]["today"], 500.0)
        self.assertEqual(cap.state(self.data, self.now)["card"], "open")
        self.allow()  # still at the cap: the card is not opened again
        self.assertEqual(len(self.cap_cards()), 1)
        self.assertFalse(cap.open_card(self.data, self.now, 500.0, "spent", self.spent, "g", self.store))
        self.assertEqual(self.cap_cards()[0]["ask"]["type"], "approve")

    def test_unknown_cost_counts_as_over(self):
        self.spent = totals(10.0, unknown_today=True, models=["mystery-1"])
        a = self.allow()
        self.assertEqual((a["allow"], a["reason"], a["cause"]), (0, "cap", "unknown"))

    def test_under_the_cap_is_ok(self):
        self.spent = totals(499.99)
        a = self.allow()
        self.assertEqual((a["allow"], a["reason"]), (None, "ok"))
        self.assertEqual(self.cap_cards(), [])
        self.assertEqual(cap.state(self.data, self.now)["card"], None)

    def test_subscription_ignores_the_cap(self):
        self.spent = totals(10_000.0, unknown_today=True)
        a = self.allow(p=profiles.builtin())
        self.assertEqual((a["allow"], a["reason"]), (None, "ok"))
        self.assertEqual(self.totals_calls, 0)  # no spend is parsed for a subscription profile
        self.assertEqual(self.cap_cards(), [])

    def test_switching_to_the_subscription_closes_the_cap_card(self):
        self.spent = totals(500.0)
        self.allow()
        cid = cap.state(self.data, self.now)["card_id"]
        profiles.use(self.data, profiles.SUBSCRIPTION)
        a = self.allow(p=profiles.active(self.data))
        self.assertEqual((a["allow"], a["reason"]), (None, "ok"))
        self.assertNotIn("card_error", a)
        self.assertEqual(self.totals_calls, 1)  # the gateway call only: closing the card reads no spend
        c = self.store.get(cid)
        self.assertIsNone(c["ask"])
        self.assertEqual(c["lane"], "done")
        self.assertIsNone(cap.state(self.data, self.now)["card"])

    def test_card_write_failure_still_refuses(self):
        self.spent = totals(500.0)
        with mock.patch.object(cap, "open_card", side_effect=CardError("the store is locked")):
            a = self.allow()
        self.assertEqual((a["allow"], a["reason"]), (0, "cap"))
        self.assertIn("the store is locked", a["card_error"])

    def test_a_card_that_could_not_be_written_is_made_next_time(self):
        self.spent = totals(500.0)
        with mock.patch.object(Store, "add", side_effect=CardError("the store is locked")):
            a = self.allow()
        self.assertEqual((a["allow"], a["reason"]), (0, "cap"))
        self.assertIn("the store is locked", a["card_error"])
        self.assertEqual(self.cap_cards(), [])
        a = self.allow()  # the state was not saved as open: the card is tried again
        self.assertNotIn("card_error", a)
        [c] = self.cap_cards()
        s = cap.state(self.data, self.now)
        self.assertEqual((s["card"], s["card_id"]), ("open", c["id"]))

    def test_switching_gateway_profiles_sums_for_the_cap(self):
        profiles.save(self.data, gateway("g2", daily_cap_usd=1.5))
        profiles.use(self.data, "g2")
        p2 = profiles.active(self.data)
        # One $1 request today on each gateway profile, and one on the subscription that never counts.
        for n, (name, mode) in enumerate((("g", "gateway"), ("g2", "gateway"), ("subscription", "subscription"))):
            sid = f"0000000{n}-0000-4000-8000-000000000000"
            spend.record_launch(self.data, f"s{n}", "worker", None, {"name": name, "mode": mode}, session_id=sid,
                                at=NOW - timedelta(hours=6))
            line = {"type": "assistant", "requestId": f"req_{n}", "timestamp": (NOW - timedelta(hours=1)).isoformat(),
                    "message": {"model": "claude-haiku-4-5", "usage": {"input_tokens": 1_000_000, "output_tokens": 0}}}
            f = self.projects / "-tmp-x" / f"{sid}.jsonl"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(line) + "\n")
        a = cap.allowance(self.data, self.projects, NOW, p2, store=self.store)
        self.assertEqual((a["reason"], a["cause"]), ("cap", "spent"))
        self.assertAlmostEqual(a["spent"]["today"], 2 * HAIKU_1M)

    def test_cleared_condition_closes_an_unanswered_card(self):
        self.spent = totals(500.0)
        self.allow()
        cid = cap.state(self.data, self.now)["card_id"]
        self.spent = totals(100.0)  # e.g. a price was added and the spend went down
        self.assertEqual(self.allow()["reason"], "ok")
        self.assertIsNone(cap.state(self.data, self.now)["card"])
        c = self.store.get(cid)
        self.assertIsNone(c["ask"])
        self.assertEqual(c["lane"], "done")
        self.spent = totals(600.0)  # later the same day: the card opens again, still one card for the day
        self.assertEqual(self.allow()["reason"], "cap")
        self.assertEqual([c["id"] for c in self.cap_cards()], [cid])
        c = self.store.get(cid)
        self.assertEqual((c["lane"], c["ask"]["type"]), ("needs", "approve"))

    def test_reasons_are_a_small_closed_set(self):
        self.assertEqual(cap.REASONS, ("ok", "cap", "paused"))


# -- 2. raises, accepting unknown cost, one card per day ---------------------------------------------------------------

class RaiseTest(CapCase):
    def test_raise_expires_at_local_midnight(self):
        self.spent = totals(500.0)
        self.allow()
        self.answer("raise50")
        self.assertEqual(cap.today_limit(self.data, self.p, self.now), 750.0)
        self.assertEqual(self.allow()["reason"], "ok")
        tomorrow = datetime(2026, 11, 12, 0, 1, tzinfo=TZ)
        self.assertEqual(cap.today_limit(self.data, self.p, tomorrow), 500.0)
        s = cap.state(self.data, tomorrow)
        self.assertEqual((s["day"], s["raise_pct"], s["card"]), ("2026-11-12", 0, None))

    def test_second_raise_adds_to_the_first(self):
        self.answer("raise50")
        self.answer("raise100")
        self.assertEqual(cap.state(self.data, self.now)["raise_pct"], 150)
        self.assertEqual(cap.today_limit(self.data, self.p, self.now), 1250.0)

    def test_card_reopens_when_spend_crosses_the_raised_limit(self):
        self.spent = totals(500.0)
        self.allow()
        self.answer("raise50")
        cid = cap.state(self.data, self.now)["card_id"]
        self.assertIsNone(self.store.get(cid)["ask"])
        self.spent = totals(760.0)
        a = self.allow()
        self.assertEqual((a["reason"], a["limit"]), ("cap", 750.0))
        s = cap.state(self.data, self.now)
        self.assertEqual((s["card"], s["opened"]), ("open", {"limit": 750.0, "cause": "spent"}))
        c = self.store.get(cid)
        self.assertEqual((c["lane"], c["ask"]["type"]), ("needs", "approve"))
        self.assertIn("$750.00", self.text(c))

    def test_answered_card_does_not_reopen_for_the_same_limit(self):
        self.spent = totals(500.0)
        self.allow()
        self.answer("pause")
        cap.resume(self.data)
        self.assertEqual(self.allow()["reason"], "cap")  # still at the cap, for the same limit and cause
        s = cap.state(self.data, self.now)
        self.assertEqual(s["card"], "answered")
        self.assertIsNone(self.store.get(s["card_id"])["ask"])

    def test_accept_unknown_counts_unknown_as_zero_until_midnight(self):
        self.spent = totals(10.0, unknown_today=True)
        self.assertEqual(self.allow()["cause"], "unknown")
        self.answer(cap.ACCEPT_UNKNOWN)
        self.assertTrue(cap.unknown_accepted(self.data, self.now))
        self.assertEqual(self.allow()["reason"], "ok")
        self.spent = totals(500.0, unknown_today=True)  # the priced part still counts
        self.assertEqual(self.allow()["cause"], "spent")
        s = cap.state(self.data, self.now)  # the same limit, another cause: the answered card opens again
        self.assertEqual(s["card"], "open")
        self.assertEqual(self.store.get(s["card_id"])["ask"]["cause"], "spent")
        tomorrow = datetime(2026, 11, 12, 0, 1, tzinfo=TZ)
        self.assertFalse(cap.unknown_accepted(self.data, tomorrow))
        self.spent = totals(10.0, unknown_today=True)
        self.assertEqual(self.allow(now=tomorrow)["cause"], "unknown")

    def test_accept_unknown_is_offered_only_for_unknown(self):
        self.spent = totals(500.0)
        self.allow()
        cid = cap.state(self.data, self.now)["card_id"]
        self.assertEqual(self.store.get(cid)["ask"]["options"], ["raise50", "raise100", "pause"])
        self.store.update(cid, {"ask": None}, by="test")
        tomorrow = datetime(2026, 11, 12, 9, 0, tzinfo=TZ)
        self.spent = totals(1.0, unknown_today=True)
        self.allow(now=tomorrow)
        cid2 = cap.state(self.data, tomorrow)["card_id"]
        self.assertNotEqual(cid2, cid)
        self.assertEqual(self.store.get(cid2)["ask"]["options"], ["raise50", "raise100", "accept_unknown", "pause"])

    def test_unknown_option_is_refused(self):
        with self.assertRaises(cap.CapError) as e:
            self.answer("raise200")
        self.assertIsInstance(e.exception, ConfigError)
        self.assertIn("raise50", str(e.exception))
        self.assertFalse((self.data / cap.STATE).exists())

    def test_answer_closes_the_cards_ask(self):
        self.spent = totals(500.0)
        self.allow()
        self.answer("raise100")
        s = cap.state(self.data, self.now)
        self.assertEqual(s["card"], "answered")
        c = self.store.get(s["card_id"])
        self.assertIsNone(c["ask"])
        self.assertEqual(c["lane"], "done")


# -- 3. the card --------------------------------------------------------------------------------------------------------

class CardTest(CapCase):
    def test_cap_card_is_one_card_per_day(self):
        self.spent = totals(512.5)
        self.allow()
        [c] = self.cap_cards()
        cards.validate(c, ("en", "zh"))
        self.assertEqual((c["key"], c["feed"], c["lane"], c["pri"], c["origin"]),
                         ("cap:2026-11-11", "cap", "needs", "P0", "found"))
        self.assertEqual(cap.state(self.data, self.now)["card_id"], c["id"])
        for lang in ("en", "zh"):
            words = " ".join(c["plain"][line][lang] for line in cards.PLAIN_LINES)
            self.assertIn("$512.50", words, lang)
            self.assertIn("$500.00", words, lang)
        self.assertIn("ap2 cap answer raise50", c["plain"]["do"]["en"])
        self.assertNotIn("accept_unknown", c["plain"]["do"]["en"])
        self.answer("raise50")
        self.spent = totals(800.0)
        self.allow()  # reopened: the same card
        self.assertEqual(len(self.cap_cards()), 1)
        tomorrow = datetime(2026, 11, 12, 9, 0, tzinfo=TZ)
        self.allow(now=tomorrow)
        self.assertEqual(sorted(c["key"] for c in self.cap_cards()), ["cap:2026-11-11", "cap:2026-11-12"])

    def test_unknown_card_names_models_and_launches(self):
        self.spent = totals(3.0, unknown_today=True, models=["mystery-1", "mystery-2"], launches=2)
        self.allow()
        [c] = self.cap_cards()
        cards.validate(c, ("en", "zh"))
        for lang in ("en", "zh"):
            words = " ".join(c["plain"][line][lang] for line in cards.PLAIN_LINES)
            for want in ("mystery-1", "mystery-2", "2", "prices.json", "accept_unknown"):
                self.assertIn(want, words, (lang, want))
        self.assertEqual(c["ask"]["cause"], "unknown")

    def test_card_in_the_primary_language_only(self):
        store = Store(self.data, Config(), clock=lambda: self.now)
        self.spent = totals(500.0)
        with mock.patch.object(spend, "totals", side_effect=self.fake_totals):
            cap.allowance(self.data, self.projects, self.now, self.p, store=store)
        [c] = [c for c in store.list() if c.get("feed") == "cap"]
        cards.validate(c, ("en",))
        self.assertEqual(set(c["plain"]["what"]), {"en"})

    def test_yesterdays_open_card_is_closed_on_a_new_day(self):
        self.spent = totals(500.0)
        self.allow()
        cid = cap.state(self.data, self.now)["card_id"]
        self.spent = totals(10.0)
        tomorrow = datetime(2026, 11, 12, 9, 0, tzinfo=TZ)
        self.assertEqual(self.allow(now=tomorrow)["reason"], "ok")
        c = self.store.get(cid)
        self.assertIsNone(c["ask"])
        self.assertEqual(c["lane"], "done")

    def test_opening_todays_card_closes_yesterdays(self):
        self.spent = totals(500.0)
        self.allow()
        cid = cap.state(self.data, self.now)["card_id"]
        self.spent = totals(1.0, unknown_today=True)
        tomorrow = datetime(2026, 11, 12, 9, 0, tzinfo=TZ)
        self.assertEqual(self.allow(now=tomorrow)["reason"], "cap")
        c = self.store.get(cid)
        self.assertIsNone(c["ask"])
        self.assertEqual(c["lane"], "done")
        new = cap.state(self.data, tomorrow)["card_id"]
        self.assertNotEqual(new, cid)
        self.assertEqual(self.store.get(new)["lane"], "needs")

    def test_answer_after_midnight_closes_yesterdays_card(self):
        self.spent = totals(500.0)
        self.allow()
        cid = cap.state(self.data, self.now)["card_id"]
        self.answer("raise50", now=datetime(2026, 11, 12, 0, 10, tzinfo=TZ))
        c = self.store.get(cid)
        self.assertIsNone(c["ask"])
        self.assertEqual(c["lane"], "done")


# -- 4. the pause file and the state file --------------------------------------------------------------------------------

class FilesTest(CapCase):
    def test_pause_lasts_past_midnight(self):
        self.answer("pause")
        self.assertEqual(cap.paused(self.data)["reason"], "cap")
        later = NOW + timedelta(days=3)
        self.spent = totals(0.0)
        self.assertEqual(self.allow(now=later)["reason"], "paused")
        a = self.allow(now=later, p=profiles.builtin())  # paused in any mode
        self.assertEqual((a["allow"], a["reason"]), (0, "paused"))

    def test_resume_says_whether_it_was_paused(self):
        self.assertEqual(cap.resume(self.data), {"paused": False, "was": False})
        self.answer("pause")
        self.assertEqual(cap.resume(self.data), {"paused": False, "was": True})
        self.assertIsNone(cap.paused(self.data))

    def test_an_unreadable_pause_file_counts_as_paused(self):
        (self.data / cap.PAUSED).write_text("{not json")
        self.assertIsNotNone(cap.paused(self.data))
        self.assertEqual(self.allow()["reason"], "paused")

    def test_a_malformed_state_reads_as_blank_for_today(self):
        (self.data / cap.STATE).write_text('{"day": "2026-11-11", "raise_pct": "lots"}')
        self.assertEqual(cap.state(self.data, self.now), {"day": "2026-11-11", "raise_pct": 0, "card": None})

    def test_a_symlink_is_refused(self):
        other = self.tmp / "elsewhere.json"
        other.write_text("{}")
        (self.data / cap.STATE).symlink_to(other)
        with self.assertRaisesRegex(cap.CapError, "symlink"):
            self.answer("raise50")
        self.assertEqual(other.read_text(), "{}")
        (self.data / cap.STATE).unlink()
        (self.data / cap.PAUSED).symlink_to(other)
        self.assertIsNotNone(cap.paused(self.data))  # never followed, and counts as paused
        with self.assertRaisesRegex(cap.CapError, "symlink"):
            self.answer("pause")
        self.assertEqual(other.read_text(), "{}")

    def test_the_day_is_the_local_day_of_now(self):
        late = datetime(2026, 11, 11, 23, 30, tzinfo=TZ)  # 04:30 UTC on the 12th
        self.assertEqual(cap.state(self.data, late)["day"], "2026-11-11")
        self.assertEqual(cap.state(self.data, datetime(2026, 11, 11, 23, 30))["day"], "2026-11-11")  # naive: as is


# -- 5. workers: every start and resume is refused at the cap ---------------------------------------------------------------

class CapWorkerCase(WorkerCase):
    def setUp(self):
        super().setUp()
        self.spent = totals()
        self.totals_calls = 0
        patch = mock.patch.object(spend, "totals", side_effect=self.fake_totals)
        patch.start()
        self.addCleanup(patch.stop)

    def fake_totals(self, data, projects, now=None, mode="gateway", listing=None):
        self.totals_calls += 1
        return dict(self.spent)

    def use_gateway(self, name="g", cap_usd=500):
        if name not in profiles.names(self.data):
            profiles.save(self.data, gateway(name, daily_cap_usd=cap_usd))
        profiles.use(self.data, name)

    def use_subscription(self):
        profiles.use(self.data, profiles.SUBSCRIPTION)

    def records(self) -> list:
        return resources.Registry(self.data).read()

    def pause(self, w=None):
        return cap.answer(self.data, "pause", self.clock(), workers=w or self.workers(), store=self.workers().store)

    def quiet(self, name="q.json"):
        return usage_file(self.tmp / name, 3, resets_at="2026-01-06T17:00:00+00:00", fetched=self.clock())

    def park_quota(self, w, a):
        """Park `a` on the subscription at a 95% window that resets at 12:00 (resume at 12:05)."""
        return w.park(a["card"], "implement", "x", usage_file=usage_file(self.tmp / "u.json", 95))


class WorkerCapTest(CapWorkerCase):
    def test_start_refused_at_the_cap_runs_nothing(self):
        self.use_gateway()
        self.spent = totals(500.0)
        cid = self.add_card()
        with self.assertRaises(cap.CapError) as e:
            self.workers().start(cid, "implement", cwd=self.wt, prompt="Do the work.")
        msg = str(e.exception)
        for want in ("daily cap", "$500.00", "ap2 cap answer"):
            self.assertIn(want, msg)
        self.assertEqual(self.calls(), [])  # no discovery, no launch
        self.assertEqual(self.records(), [])
        self.assertFalse((self.data / "spend" / "launches.jsonl").exists())
        self.assertEqual(len([c for c in Store(self.data, self.cfg).list() if c.get("feed") == "cap"]), 1)

    def test_resume_refused_while_paused(self):
        a = self.start()
        w = self.workers()
        w.end(a["card"], "implement")
        cap.pause(self.data, self.clock())
        with self.assertRaisesRegex(cap.CapError, "paused"):
            w.start(a["card"], "implement", prompt="Continue.", resume=True)
        self.assertEqual(len(self.bg_calls()), 1)
        self.assertEqual(self.records(), [])

    def pause_while_tools_are_listed(self):
        """Patch `Workers._tools` so a pause lands between the plan and the lock."""
        real = workers_mod.Workers._tools

        def tools(w, *args, **kw):
            cap.pause(self.data, self.clock())
            return real(w, *args, **kw)
        return mock.patch.object(workers_mod.Workers, "_tools", autospec=True, side_effect=tools)

    def test_pause_while_tools_are_listed_refuses_the_start(self):
        cid = self.add_card()
        with self.pause_while_tools_are_listed():
            with self.assertRaisesRegex(cap.CapError, "paused"):
                self.workers().start(cid, "implement", cwd=self.wt, prompt="Do the work.")
        self.assertEqual(self.bg_calls(), [])
        self.assertEqual(self.records(), [])

    def test_pause_while_tools_are_listed_refuses_a_dead_workers_resume(self):
        a = self.start()
        self.drop_agent(a["short_id"])
        with self.pause_while_tools_are_listed():
            out = self.workers().check()[0]
        self.assertEqual((out["state"], out["did"]), ("dead", "nothing"))
        self.assertEqual(len(self.bg_calls()), 1)
        self.assertEqual(self.records()[0]["resumes"], 0)

    def test_answer_refused_while_paused(self):
        a = self.start()
        self.write_result(a["card"], "question.json", {"text": "Which one?"})
        w = self.workers()
        self.assertEqual(w.check()[0]["did"], "asked")
        cap.pause(self.data, self.clock())
        with self.assertRaisesRegex(cap.CapError, "ap2 cap resume"):
            w.answer(a["card"], "The first one.")
        self.assertEqual(len(self.bg_calls()), 1)

    def test_dead_worker_keeps_its_resume_at_the_cap(self):
        self.use_gateway()
        a = self.start()
        self.spent = totals(500.0)
        self.drop_agent(a["short_id"])
        out = self.workers().check()[0]
        self.assertEqual((out["state"], out["did"]), ("dead", "nothing"))
        self.assertIn("daily cap", out["note"])
        self.assertNotIn("error", out)
        self.assertEqual(self.records()[0]["resumes"], 0)
        self.assertEqual(len(self.bg_calls()), 1)
        self.spent = totals(10.0)  # under the cap again: it gets its one resume
        self.assertEqual(self.workers().check()[0]["did"], "resumed")

    def test_parked_worker_stays_parked_at_the_cap(self):
        w = self.workers()
        a = self.start(w)
        self.park_quota(w, a)
        self.clock.advance(4 * 60)  # past the reset
        self.use_gateway()
        self.spent = totals(500.0)
        out = self.workers().check(usage_file=self.quiet())[0]
        self.assertEqual((out["state"], out["did"]), ("parked", "nothing"))
        # Even when the due check is passed, the refusal in the plan keeps it parked: not stopped as parked_failed.
        with mock.patch.object(workers_mod.Workers, "_parked_due", return_value=True):
            out = self.workers().check(usage_file=self.quiet())[0]
        self.assertEqual((out["state"], out["did"]), ("parked", "nothing"))
        self.assertIn("daily cap", out["note"])
        self.assertTrue(self.records()[0]["parked"])
        self.assertIsNone(self.get(a["card"]).get("ask"))
        self.assertEqual(len(self.bg_calls()), 1)

    def test_running_workers_are_not_stopped_by_the_cap(self):
        self.use_gateway()
        a, b = self.start(), self.start()
        self.spent = totals(900.0)
        out = self.workers().check()
        self.assertEqual([(e["state"], e["did"]) for e in out], [("running", "nothing")] * 2)
        self.assertEqual([c for c in self.calls() if c["argv"][:1] == ["stop"]], [])
        self.assertEqual({x["id"] for x in self.agents()}, {a["short_id"], b["short_id"]})

    def test_one_totals_call_per_check(self):
        self.use_gateway()
        started = [self.start() for _ in range(3)]
        for a in started:
            self.drop_agent(a["short_id"])
        self.totals_calls = 0
        out = self.workers().check()
        self.assertEqual([e["did"] for e in out], ["resumed"] * 3)
        self.assertEqual(self.totals_calls, 1)
        self.totals_calls = 0
        self.workers().check()  # nothing to resume: no totals at all
        self.assertEqual(self.totals_calls, 0)

    def test_workers_count_the_local_day_not_the_utc_day(self):
        if not hasattr(time, "tzset"):
            self.skipTest("time.tzset is not available")
        patch = mock.patch.dict(os.environ, {"TZ": "America/Chicago"})
        patch.start()
        self.addCleanup(time.tzset)  # runs last: after the zone is restored
        self.addCleanup(patch.stop)
        time.tzset()
        self.use_gateway()
        self.clock.at = datetime(2026, 11, 12, 3, 0, tzinfo=timezone.utc)  # 21:00 CST on the 11th
        cap.answer(self.data, "raise50", datetime(2026, 11, 11, 15, 0))  # today's raise: $750, naive local time
        self.spent = totals(600.0)  # over the base $500, under today's $750
        a = self.workers().start(self.add_card(), "implement", cwd=self.wt, prompt="Do the work.")
        self.assertEqual([r["short_id"] for r in self.records()], [a["short_id"]])
        self.assertEqual(len(self.bg_calls()), 1)

    def test_cap_state_is_edit_denied(self):
        self.use_gateway()
        w = self.workers()
        for role in ("implement", "review", "pr"):
            cid = self.add_card()
            w.start(cid, role, cwd=self.wt)
            deny = self.deny(cid, role)
            self.assertIn(f"Edit(/{self.data}/cap-state.json)", deny, role)
            self.assertIn(f"Edit(/{self.data}/paused.json)", deny, role)
        self.assertIn(cap.STATE, workers_mod.PROTECTED)
        self.assertIn(cap.PAUSED, workers_mod.PROTECTED)


# -- 6. the pause stops and parks; a failed stop is retried ----------------------------------------------------------------

class PauseTest(CapWorkerCase):
    def test_pause_parks_running_workers(self):
        self.use_gateway()
        a, b = self.start(), self.start()
        out = self.pause()
        self.assertTrue(out["paused"])
        stops = [c["argv"][1] for c in self.calls() if c["argv"][:1] == ["stop"]]
        self.assertEqual(sorted(stops), sorted([a["short_id"], b["short_id"]]))
        for rec in self.records():
            pk = rec["parked"]
            self.assertEqual((pk["kind"], pk["reason"], pk["resume_at"], pk["note"]),
                             ("cap", "cap", None, cap.PAUSE_NOTE))
            self.assertEqual({k: pk[k] for k in ("model", "percent", "resets_at")},
                             {"model": None, "percent": None, "resets_at": None})
            self.assertTrue(pk["parked_at"])
            self.assertEqual(self.get(rec["card"])["sessions"]["implement"]["state"], "parked")
        self.assertEqual([e["state"] for e in self.workers().list()], ["parked", "parked"])

    def test_pause_writes_the_flag_before_any_stop(self):
        self.start(), self.start()
        real, seen = subprocess.run, []

        def run(argv, *args, **kw):
            if argv[1:2] == ["stop"]:
                seen.append((self.data / cap.PAUSED).exists())
            return real(argv, *args, **kw)
        with mock.patch("assistant.workers.subprocess.run", side_effect=run):
            self.pause()
        self.assertEqual(seen, [True, True])

    def test_failed_stop_stays_unparked_and_is_retried_by_check(self):
        a, b = self.start(), self.start()
        real = subprocess.run

        def run(argv, *args, **kw):
            if argv[1:3] == ["stop", a["short_id"]]:
                raise subprocess.TimeoutExpired(argv, 60)
            return real(argv, *args, **kw)
        with mock.patch("assistant.workers.subprocess.run", side_effect=run):
            out = self.pause()
        recs = {r["card"]: r for r in self.records()}
        self.assertIsNone(recs[a["card"]]["parked"])  # still running, as far as anyone can tell
        self.assertEqual(recs[b["card"]]["parked"]["kind"], "cap")
        self.assertIn(a["card"], json.dumps(out["failed"]))
        entries = {e["card"]: e for e in self.workers().check()}
        self.assertEqual(self.records()[0]["parked"]["kind"], "cap")
        self.assertEqual({r["parked"]["kind"] for r in self.records()}, {"cap"})
        self.assertEqual(entries[a["card"]]["state"], "parked")
        self.assertNotIn(a["short_id"], [x["id"] for x in self.agents()])

    def test_pause_lasts_past_midnight(self):
        self.use_gateway()
        a = self.start()
        self.pause()
        self.clock.advance(3 * 24 * 60)
        out = self.workers().check()[0]
        self.assertEqual((out["state"], out["did"]), ("parked", "nothing"))
        with self.assertRaisesRegex(cap.CapError, "paused"):
            self.start()
        self.assertEqual(len(self.bg_calls()), 1)
        self.assertTrue(self.records()[0]["parked"])
        self.assertEqual(self.records()[0]["short_id"], a["short_id"])

    def test_cap_resume_lifts_the_pause_and_cap_parked_workers_resume_on_check(self):
        self.use_gateway()
        a = self.start()
        self.pause()
        self.assertEqual(self.workers().check()[0]["did"], "nothing")
        self.assertEqual(cap.resume(self.data), {"paused": False, "was": True})
        out = self.workers().check()[0]  # gateway active, under the cap: no usage file needed
        self.assertEqual((out["state"], out["did"]), ("parked", "resumed"))
        rec = self.records()[0]
        self.assertEqual((rec["parked"], rec["resumes"], rec["ports"]), (None, 0, a["ports"]))
        self.assertIn(workers_mod.CAP_RESUME, self.bg_calls()[-1]["argv"][-1])

    def test_pause_in_shadow_mode_stops_nothing(self):
        a = self.start()
        out = cap.answer(self.data, "pause", self.clock(), workers=None, store=self.workers().store)
        self.assertTrue(out["paused"])
        self.assertIn("no worker was stopped", out["note"])
        self.assertEqual([c for c in self.calls() if c["argv"][:1] == ["stop"]], [])
        self.assertIsNone(self.records()[0]["parked"])
        self.assertEqual(self.records()[0]["short_id"], a["short_id"])

    def stops(self) -> list:
        return [c["argv"][1] for c in self.calls() if c["argv"][:1] == ["stop"]]

    def test_shadow_check_while_paused_stops_nothing(self):
        self.start()
        cap.pause(self.data, self.clock())
        self.workers(cfg=Config()).check()
        self.assertEqual(self.stops(), [])
        self.assertIsNone(self.records()[0]["parked"])

    def test_pause_leaves_a_worker_that_died_twice_to_check(self):
        a = self.start()
        with resources.Registry(self.data).locked() as records:
            records[0]["resumes"] = 1
        self.drop_agent(a["short_id"])
        out = self.pause()
        self.assertEqual(out["stopped"], [])
        self.assertEqual(self.stops(), [])
        self.assertIsNone(self.records()[0]["parked"])
        entry = self.workers().check()[0]
        self.assertEqual((entry["state"], entry["did"]), ("dead", "stopped"))
        self.assertEqual(self.records(), [])
        ask = self.get(a["card"])["ask"]
        self.assertEqual((ask["type"], ask["reason"]), ("fix", workers_mod.DIED))

    def test_pause_stops_workers_when_the_listing_fails(self):
        self.use_gateway()
        a, b = self.start(), self.start()
        w = self.workers()
        with mock.patch.object(w, "_listing", return_value=None):  # the listing failed: unknown, not dead
            out = self.pause(w)
        self.assertEqual(sorted(self.stops()), sorted([a["short_id"], b["short_id"]]))
        self.assertEqual([r["parked"]["kind"] for r in self.records()], ["cap", "cap"])
        self.assertEqual(len(out["stopped"]), 2)

    def pause_during_launch(self):
        """Patch `Workers._launch` so a pause runs on another thread while the launch holds the worker list's lock:
        the launch waits until the pause thread asks for the lock, then launches for real. Returns the patch and a
        dict that gets the pause's result (`out`) or its error (`error`) once `join` is called."""
        real_launch, real_locked = workers_mod.Workers._launch, resources.Registry.locked
        asked, box = threading.Event(), {}

        def run():
            try:
                box["out"] = self.pause()
            except BaseException as e:  # noqa: BLE001 - re-raised on the test's thread
                box["error"] = e

        thread = threading.Thread(target=run, daemon=True)

        def locked(reg):
            if threading.current_thread() is thread:
                asked.set()
            return real_locked(reg)

        def launch(w, *args, **kw):
            thread.start()
            self.assertTrue(asked.wait(10), "the pause never asked for the worker list's lock")
            return real_launch(w, *args, **kw)

        def join():
            thread.join(30)
            self.assertFalse(thread.is_alive(), "the pause did not finish")
            if "error" in box:
                raise box["error"]
            return box["out"]

        patches = contextlib.ExitStack()
        patches.enter_context(mock.patch.object(resources.Registry, "locked", new=locked))
        patches.enter_context(mock.patch.object(workers_mod.Workers, "_launch", autospec=True, side_effect=launch))
        return patches, join

    def test_pause_during_a_start_parks_the_new_worker(self):
        cid = self.add_card()
        patches, join = self.pause_during_launch()
        with patches:
            rec = self.workers().start(cid, "implement", cwd=self.wt, prompt="Do the work.")
            out = join()
        self.assertEqual(out["stopped"], [{"card": cid, "role": "implement"}])
        self.assertIn(rec["short_id"], self.stops())
        self.assertEqual(self.records()[0]["short_id"], rec["short_id"])
        self.assertEqual(self.records()[0]["parked"]["kind"], "cap")

    def test_pause_during_a_dead_workers_resume_parks_the_copy(self):
        a = self.start()
        self.drop_agent(a["short_id"])
        patches, join = self.pause_during_launch()
        with patches:
            entry = self.workers().check()[0]
            out = join()
        self.assertEqual((entry["state"], entry["did"]), ("dead", "resumed"))
        rec = self.records()[0]
        self.assertNotEqual(rec["short_id"], a["short_id"])
        self.assertEqual(out["stopped"], [{"card": a["card"], "role": "implement"}])
        self.assertIn(rec["short_id"], self.stops())
        self.assertEqual((rec["parked"]["kind"], rec["resumes"]), ("cap", 1))

    def test_check_while_paused_never_stops_a_dead_worker(self):
        a = self.start()
        self.drop_agent(a["short_id"])
        self.pause()
        for _ in range(2):
            out = self.workers().check()[0]
            self.assertEqual((out["state"], out["did"]), ("dead", "nothing"))
            self.assertIn("paused", out["note"])
        self.assertEqual(self.stops(), [])
        rec = self.records()[0]
        self.assertEqual((rec["parked"], rec["resumes"]), (None, 0))


class LedgerTest(WorkerCase):
    def test_unreadable_ledger_refuses_the_start(self):
        profiles.save(self.data, gateway("g", daily_cap_usd=500))
        profiles.use(self.data, "g")
        (self.data / spend.FOLDER / spend.LAUNCHES).mkdir(parents=True)  # a folder: cannot be read as the ledger
        with self.assertRaises(ConfigError):
            self.start()
        self.assertEqual(self.calls(), [])  # no discovery, no launch
        self.assertEqual(resources.Registry(self.data).read(), [])


# -- 7. parked workers resume by the active profile ------------------------------------------------------------------------

class ResumeByProfileTest(CapWorkerCase):
    def test_gateway_parked_resume_ignores_subscription_windows(self):
        w = self.workers()
        a = self.start(w)
        self.park_quota(w, a)
        self.clock.advance(4 * 60)  # past the 12:05 resume time
        self.use_gateway()
        high = usage_file(self.tmp / "high.json", 97, resets_at="2026-01-05T17:00:00+00:00", fetched=self.clock())
        out = self.workers().check(usage_file=high)[0]
        self.assertEqual((out["state"], out["did"]), ("parked", "resumed"))
        self.assertEqual(self.records()[0]["profile"], "g")

    def test_subscription_parked_resume_keeps_v1_rules(self):
        w = self.workers()
        a = self.start(w)
        self.park_quota(w, a)
        self.assertEqual(w.check(usage_file=self.tmp / "u.json")[0]["did"], "nothing")  # before its resume time
        self.clock.advance(4 * 60)
        self.assertEqual(w.check()[0]["did"], "nothing")  # no usage file: nothing to judge by
        high = usage_file(self.tmp / "high.json", 97, resets_at="2026-01-05T17:00:00+00:00", fetched=self.clock())
        self.assertEqual(w.check(usage_file=high)[0]["did"], "nothing")
        self.assertEqual(w.check(usage_file=self.quiet())[0]["did"], "resumed")
        self.assertIn(workers_mod.PARKED_RESUME, self.bg_calls()[-1]["argv"][-1])
        self.assertEqual(self.totals_calls, 0)  # a subscription pass parses no spend

    def test_quota_park_resumes_on_a_gateway_profile(self):
        w = self.workers()
        a = self.start(w)
        self.park_quota(w, a)
        self.use_gateway()
        self.assertEqual(self.workers().check()[0]["did"], "nothing")  # its resume time has not come
        self.clock.advance(4 * 60)
        out = self.workers().check()[0]  # no --usage at all
        self.assertEqual((out["state"], out["did"]), ("parked", "resumed"))
        self.assertIn(workers_mod.PARKED_RESUME, self.bg_calls()[-1]["argv"][-1])

    def test_quota_park_waits_at_the_cap_on_a_gateway_profile(self):
        w = self.workers()
        a = self.start(w)
        self.park_quota(w, a)
        self.use_gateway()
        self.clock.advance(4 * 60)
        self.spent = totals(500.0)
        self.assertEqual(self.workers().check()[0]["did"], "nothing")
        self.assertTrue(self.records()[0]["parked"])

    def test_cap_park_never_resumes_while_paused(self):
        self.use_gateway()
        self.start()
        self.pause()
        self.use_subscription()
        self.clock.advance(60)
        out = self.workers().check(usage_file=self.quiet())[0]
        self.assertEqual((out["state"], out["did"]), ("parked", "nothing"))
        self.use_gateway()
        self.assertEqual(self.workers().check()[0]["did"], "nothing")
        self.assertEqual(len(self.bg_calls()), 1)


# -- 8. a limit park resumes like a quota park (lessons 4.1) ---------------------------------------------------------------

class LimitParkTest(CapWorkerCase):
    def limit_park(self, minutes=30):
        a = self.start()
        with resources.Registry(self.data).locked() as records:
            records[0]["parked"] = {"kind": "limit", "reason": "limit", "model": None, "percent": None,
                                    "resets_at": None, "note": "the API refused a turn at a limit",
                                    "resume_at": (self.clock() + timedelta(minutes=minutes)).isoformat(),
                                    "parked_at": cards.stamp(self.clock())}
        return a

    def test_limit_park_resumes_after_resume_at(self):
        self.limit_park()
        self.clock.advance(31)
        out = self.workers().check(usage_file=self.quiet())[0]
        self.assertEqual((out["state"], out["did"]), ("parked", "resumed"))
        self.assertIn(workers_mod.LIMIT_RESUME, self.bg_calls()[-1]["argv"][-1])
        self.assertEqual(workers_mod.LIMIT_RESUME, "You were parked at a usage limit. Continue from where you "
                                                   "stopped; check your worktree and run files first.")

    def test_limit_park_resumes_after_resume_at_on_a_gateway_profile(self):
        self.use_gateway()
        self.limit_park()
        self.clock.advance(31)
        self.assertEqual(self.workers().check()[0]["did"], "resumed")

    def test_limit_park_waits_before_resume_at(self):
        self.limit_park()
        self.clock.advance(10)
        out = self.workers().check(usage_file=self.quiet())[0]
        self.assertEqual((out["state"], out["did"]), ("parked", "nothing"))
        self.use_gateway()
        self.assertEqual(self.workers().check()[0]["did"], "nothing")


# -- 9. parking split by mode ----------------------------------------------------------------------------------------------

def hook_usage(percent=97) -> dict:
    """What `autopilot.budget.read` returns: one session window at `percent`, read a few minutes ago."""
    return {"ok": True, "age_min": 5, "stale": False,
            "windows": [{"kind": "session", "model": None, "percent": percent,
                         "resets_at": "2026-01-05T12:00:00+00:00"}],
            "credits_used_usd": 0, "credits_limit_usd": 0, "spend_limit_reached": False}


class ParkSplitTest(CapWorkerCase):
    def env(self, profile):
        return {"AP2_ROLE": "implement", "AP2_CARD": "C1", "AP2_DATA": str(self.data), "AP2_PROFILE": profile}

    @staticmethod
    def denied(out) -> bool:
        return bool(out) and out["hookSpecificOutput"]["permissionDecision"] == "deny"

    def bash(self, cmd="ls"):
        return {"tool_name": "Bash", "tool_input": {"command": cmd}}

    def test_hook_has_no_opinion_for_a_gateway_worker(self):
        self.use_gateway()
        self.assertIsNone(parkhook.decide(self.bash(), self.env("g"), hook_usage(97), parkhook.PARK_RULES))

    def test_hook_keeps_the_rule_for_a_subscription_worker(self):
        self.use_gateway()  # the active profile does not matter: the worker's own does
        self.assertTrue(self.denied(parkhook.decide(self.bash(), self.env("subscription"), hook_usage(97),
                                                    parkhook.PARK_RULES)))

    def test_hook_keeps_the_rule_when_the_profile_cannot_be_read(self):
        self.use_gateway()
        for env in (self.env("missing"), self.env("Not A Name"), {"AP2_ROLE": "implement", "AP2_CARD": "C1"},
                    dict(self.env("g"), AP2_DATA=str(self.tmp / "nowhere"))):
            self.assertTrue(self.denied(parkhook.decide(self.bash(), env, hook_usage(97), parkhook.PARK_RULES)),
                            env)
        (self.data / "profiles" / "g.json").write_text("{broken")
        self.assertTrue(self.denied(parkhook.decide(self.bash(), self.env("g"), hook_usage(97),
                                                    parkhook.PARK_RULES)))

    def test_hook_does_not_import_spend_or_workers(self):
        code = ("import sys; sys.path.insert(0, %r); import assistant.parkhook; "
                "print(sorted(m for m in sys.modules if m in ('assistant.spend', 'assistant.workers', "
                "'assistant.cap')))" % str(ROOT / "lib"))
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.stdout.strip(), "[]", r.stderr)

    def test_park_refused_for_a_gateway_worker(self):
        self.use_gateway()
        w = self.workers()
        a = self.start(w)
        with self.assertRaisesRegex(WorkerError, "a gateway worker is never parked by the subscription's usage "
                                                 "windows: keep working"):
            w.park(a["card"], "implement", "x", usage_file=usage_file(self.tmp / "u.json", 97))
        self.assertIsNone(self.records()[0]["parked"])

    def test_park_still_works_for_a_subscription_worker_on_a_gateway_profile(self):
        w = self.workers()
        a = self.start(w)  # started on the subscription
        self.use_gateway()  # switching profiles does not change the worker's own
        self.assertEqual(w.park(a["card"], "implement", "x", usage_file=usage_file(self.tmp / "u.json", 97))
                         ["percent"], 97)

    def test_park_works_when_the_workers_profile_is_gone(self):
        self.use_gateway()
        w = self.workers()
        a = self.start(w)
        self.assertEqual(self.records()[0]["profile"], "g")
        self.use_subscription()
        profiles.remove(self.data, "g")  # the worker's own profile cannot be loaded: the subscription's rule
        rec = w.park(a["card"], "implement", "x", usage_file=usage_file(self.tmp / "u.json", 97))
        self.assertEqual(rec["percent"], 97)
        self.assertEqual(self.records()[0]["parked"]["reason"], "quota")


# -- 10. ap2 cap ------------------------------------------------------------------------------------------------------------

class CapCliTest(unittest.TestCase):
    """`ap2 cap` run as a command, with a fake `claude` first on PATH and a temp home folder."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-cap-cli-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.projects = self.tmp / "projects"
        self.projects.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "claude").symlink_to(FAKE)
        self.home = self.tmp / "home"
        self.home.mkdir()
        (self.tmp / "fake").mkdir()
        self.log = self.tmp / "claude.log"
        profiles.save(self.data, gateway("g", daily_cap_usd=500))
        profiles.use(self.data, "g")

    def ap2(self, *args):
        env = {k: v for k, v in os.environ.items() if k not in ("AP2_DATA",)}
        env.update(HOME=str(self.home), PATH=f"{self.bin}{os.pathsep}{env.get('PATH', '')}",
                   FAKE_CLAUDE_HOME=str(self.tmp / "fake"), FAKE_CLAUDE_LOG=str(self.log))
        return subprocess.run([sys.executable, AP2, "cap", *args, "--data", str(self.data)], capture_output=True,
                              text=True, env=env, timeout=60)

    def json_of(self, r):
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        return json.loads(r.stdout)

    def test_cap_prints_todays_state(self):
        out = self.json_of(self.ap2("--json", "--transcripts", str(self.projects)))
        self.assertEqual({k: out[k] for k in ("profile", "mode", "daily_cap_usd", "raise_pct", "limit", "spent_today",
                                              "unknown_today", "card", "card_id", "accept_unknown", "paused")},
                         {"profile": "g", "mode": "gateway", "daily_cap_usd": 500, "raise_pct": 0, "limit": 500.0,
                          "spent_today": 0.0, "unknown_today": False, "card": None, "card_id": None,
                          "accept_unknown": False, "paused": False})
        self.assertEqual(out["day"], datetime.now().strftime("%Y-%m-%d"))
        r = self.ap2("--transcripts", str(self.projects))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("$500.00", r.stdout)

    def test_cap_answer_raise50(self):
        out = self.json_of(self.ap2("answer", "raise50"))
        self.assertEqual((out["answered"], out["raise_pct"]), ("raise50", 50))
        state = self.json_of(self.ap2("--json", "--transcripts", str(self.projects)))
        self.assertEqual((state["raise_pct"], state["limit"], state["card"]), (50, 750.0, "answered"))
        r = self.ap2("answer", "raise75")
        self.assertEqual(r.returncode, 1)
        self.assertTrue(r.stderr.startswith("ap2: "), r.stderr)
        self.assertEqual(len(r.stderr.splitlines()), 1)

    def test_cap_answer_pause_in_shadow_mode_stops_nothing(self):
        with resources.Registry(self.data).locked() as records:
            records.append({"card": "c-20260105-090000-abcdef", "role": "implement", "project": None,
                            "session_name": "x · implement", "session_id": "", "short_id": "s0009",
                            "ports": [7200, 7209], "needs": [], "cwd": str(self.tmp), "branch": "b",
                            "profile": "g", "started_at": "2026-01-05T09:00:00Z", "resumes": 0, "parked": None,
                            "idle_since": None, "blocked_since": None})
        out = self.json_of(self.ap2("answer", "pause"))
        self.assertTrue(out["paused"])
        self.assertIn("no worker was stopped", out["note"])
        self.assertTrue((self.data / cap.PAUSED).exists())
        calls = [json.loads(x)["argv"] for x in self.log.read_text().splitlines()] if self.log.exists() else []
        self.assertEqual([c for c in calls if c[:1] == ["stop"]], [])
        self.assertIsNone(resources.Registry(self.data).read()[0]["parked"])
        state = self.json_of(self.ap2("--json", "--transcripts", str(self.projects)))
        self.assertTrue(state["paused"])

    def test_cap_answer_pause_in_live_mode_stops_and_parks(self):
        (self.data / "config.yaml").write_text("mode: live\n")
        cid = Store(self.data, config.load(self.data)).add(card(), by="test")["id"]
        (self.tmp / "fake" / "agents.json").write_text(json.dumps([
            {"id": "s0009", "sessionId": "", "name": "x · implement", "kind": "background", "state": "working",
             "status": "busy", "cwd": str(self.tmp), "startedAt": 1}]))
        with resources.Registry(self.data).locked() as records:
            records.append({"card": cid, "role": "implement", "project": None,
                            "session_name": "x · implement", "session_id": "", "short_id": "s0009",
                            "ports": [7200, 7209], "needs": [], "cwd": str(self.tmp), "branch": "b",
                            "profile": "g", "started_at": "2026-01-05T09:00:00Z", "resumes": 0, "parked": None,
                            "idle_since": None, "blocked_since": None})
        out = self.json_of(self.ap2("answer", "pause"))
        self.assertTrue(out["paused"])
        self.assertNotIn("note", out)
        self.assertEqual(out["stopped"], [{"card": cid, "role": "implement"}])
        self.assertEqual(out["failed"], [])
        calls = [json.loads(x)["argv"] for x in self.log.read_text().splitlines()]
        self.assertIn(["stop", "s0009"], calls)
        self.assertEqual(resources.Registry(self.data).read()[0]["parked"]["kind"], "cap")

    def test_cap_resume_prints_was(self):
        self.assertEqual(self.json_of(self.ap2("resume")), {"paused": False, "was": False})
        self.json_of(self.ap2("answer", "pause"))
        self.assertEqual(self.json_of(self.ap2("resume")), {"paused": False, "was": True})
        self.assertFalse((self.data / cap.PAUSED).exists())

    def test_cap_is_in_the_usage(self):
        r = subprocess.run([sys.executable, AP2, "--help"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("cap", r.stdout.split("commands:")[1])
        from assistant import cli
        self.assertIn("cap", cli.COMMANDS)


if __name__ == "__main__":
    unittest.main()
