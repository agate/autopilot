"""The spend ledger (design section 12, spend ledger): ap2 prices its own workers' sessions from their transcripts,
settles each one before its `claude stop` and its `claude rm`, and `ap2 spend` prints the totals. Ported from v1's
`tests/test_spend.py` (v1 commit 519346b); every folder is a temp folder, and the session listing is a function the
test passes in, so no test runs `claude`."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT
from tests.test_ap2_profiles import gateway
from tests.test_ap2_workers import FAKE, PROJECT, WorkerCase
from assistant import profiles, spend, transcripts

FIX = ROOT / "tests" / "fixtures" / "spend"
TZ = timezone(timedelta(hours=-5))
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=TZ)  # a Wednesday
L0 = datetime(2026, 11, 10, 8, 0, tzinfo=TZ)  # the default launch: before every fixture request
TODAY_8 = datetime(2026, 11, 11, 8, 0, tzinfo=TZ)
SID = "11111111-0000-0000-0000-00000000aaaa"
SID_B = "22222222-0000-0000-0000-00000000bbbb"
SID2 = "22222222-0000-0000-0000-00000000cccc"
SHORT = "abcd1234"
HAIKU = json.loads((ROOT / "lib" / "assistant" / "prices.json").read_text())["models"]["claude-haiku-4-5"]
# SHA-256 of v1's lib/autopilot/prices.json at commit 519346b, for a checkout that does not hold that commit.
V1_PRICES_DIGEST = "88886fe049d8e3697ac0126de26fc1738527e30a390bbc85628396d19fb7b97f"
TOTALS_KEYS = {"today", "week", "month", "unknown", "unknown_today", "unknown_models", "unknown_launches"}


def usd(**tok):
    return sum(tok.get(k, 0) * HAIKU[k] for k in HAIKU if k != "over_100k") / 1e6


STREAMING_USD = usd(input=1000, output=2000, cache_read=10000, cache_write_5m=3000, cache_write_1h=1000)
NEW = {"type": "assistant", "requestId": "req_c", "timestamp": "2026-11-11T15:00:00Z",
       "message": {"model": "claude-haiku-4-5", "usage": {"input_tokens": 1000000, "output_tokens": 0}}}


def profile(name="g1", mode="gateway"):
    return {"name": name, "mode": mode}


class SpendCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-spend-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.projects = self.tmp / "projects"
        self.transcript = self.projects / "-tmp-x" / f"{SID}.jsonl"
        self.listing = [{"id": SHORT, "sessionId": SID, "kind": "background"}]
        self.listed = 0

    def lister(self):
        self.listed += 1
        return [dict(r) for r in self.listing]

    def put(self, fixture="streaming.jsonl", sub=None, sid=SID, extra=()):
        f = self.projects / "-tmp-x" / f"{sid}.jsonl"
        f.parent.mkdir(parents=True, exist_ok=True)
        lines = (FIX / fixture).read_text().splitlines() + [json.dumps(x) for x in extra]
        f.write_text("\n".join(lines) + "\n")
        if sub:
            d = f.parent / sid / "subagents"
            d.mkdir(parents=True, exist_ok=True)
            shutil.copy(FIX / sub, d / "agent-1.jsonl")
        return f

    def launch(self, short=SHORT, p=None, session=SID, at=L0):
        spend.record_launch(self.data, short, "worker", "shop", p or profile(), session_id=session, at=at)

    def totals(self, now=NOW, mode="gateway"):
        return spend.totals(self.data, self.projects, now, mode, listing=self.lister)

    def settle(self, short="", session_id="", now=NOW):
        return spend.settle(self.data, self.projects, short=short, session_id=session_id, listing=self.lister,
                            now=now)

    def costs(self, path=None):
        return spend.request_costs(self.data, path or self.transcript, tz=TZ)

    def rows(self, name):
        f = self.data / "spend" / name
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def cache(self):
        return json.loads((self.data / "spend" / "cache.json").read_text())


# -- 1. request pricing ---------------------------------------------------------------------------------------------

class RequestCostsTest(SpendCase):
    def test_streaming_duplicates_count_once(self):
        self.put()
        items = self.costs()
        self.assertEqual(len(items), 1)
        self.assertAlmostEqual(items[0]["usd"], STREAMING_USD)
        self.assertEqual(items[0]["day"], "2026-11-10")
        self.assertEqual(items[0]["model"], "claude-haiku-4-5")

    def test_subagent_transcripts_included(self):
        self.put(sub="subagent.jsonl")
        items = self.costs()
        self.assertEqual(len(items), 2)
        self.assertAlmostEqual(sum(i["usd"] for i in items), STREAMING_USD + usd(input=500, output=500))

    def test_local_day_bucketing(self):
        self.put("days.jsonl")
        self.assertEqual([i["day"] for i in self.costs()], ["2026-11-10", "2026-11-11"])

    def test_unknown_model_has_no_cost(self):
        self.put("unknown.jsonl")
        self.assertIsNone(self.costs()[0]["usd"])

    def test_long_prompt_tier(self):
        self.transcript.parent.mkdir(parents=True)
        self.transcript.write_text(json.dumps({"type": "assistant", "requestId": "r", "timestamp": "2026-11-10T15:00:00Z",
            "message": {"model": "claude-haiku-5-5", "usage": {"input_tokens": 200000, "output_tokens": 0}}}) + "\n")
        self.assertAlmostEqual(self.costs()[0]["usd"], 0.2 * 0.5)

    def test_synthetic_lines_are_ignored(self):
        self.put("synthetic.jsonl")
        self.launch()
        self.assertEqual(self.costs()[0]["model"], "claude-haiku-4-5")
        self.assertEqual(len(self.costs()), 1)
        self.assertIs(self.totals()["unknown"], False)

    def test_lines_without_request_id_dedupe_on_message_id(self):
        self.transcript.parent.mkdir(parents=True)

        def ln(out):
            return json.dumps({"type": "assistant", "timestamp": "2026-11-10T15:00:00Z", "message": {
                "id": "msg_1", "model": "claude-haiku-4-5", "usage": {"input_tokens": 10, "output_tokens": out}}})
        self.transcript.write_text(ln(5) + "\n" + ln(50) + "\n")
        items = self.costs()
        self.assertEqual(len(items), 1)
        self.assertAlmostEqual(items[0]["usd"], usd(input=10, output=50))

    def test_a_line_without_a_usable_timestamp_is_unknown_not_dropped(self):
        self.transcript.parent.mkdir(parents=True)
        self.transcript.write_text(json.dumps({"type": "assistant", "requestId": "r", "timestamp": "garbage",
            "message": {"model": "claude-haiku-4-5", "usage": {"input_tokens": 10, "output_tokens": 1}}}) + "\n")
        self.assertEqual(self.costs(), [{"day": None, "model": "claude-haiku-4-5", "usd": None}])
        self.launch(at=TODAY_8)
        t = self.totals()
        self.assertEqual((t["unknown"], t["unknown_today"]), (True, True))

    def test_a_count_too_big_for_a_number_is_unknown_not_an_error(self):
        self.transcript.parent.mkdir(parents=True)
        line = json.dumps(dict(NEW, requestId="r_inf")).replace('"input_tokens": 1000000', '"input_tokens": 1e400')
        big = json.dumps(dict(NEW, requestId="r_big")).replace("1000000", "9" * 400)
        self.transcript.write_text(line + "\n" + big + "\n")
        self.launch()
        t = self.totals()
        self.assertEqual(t["month"], 0.0)
        self.assertEqual((t["unknown"], t["unknown_today"]), (True, True))

    def test_model_keys_drop_the_context_tag_and_the_date(self):
        self.assertEqual(spend._model_key("claude-haiku-4-5[1m]"), "claude-haiku-4-5")
        self.assertEqual(spend._model_key("claude-haiku-4-5-20251001"), "claude-haiku-4-5")


# -- 2. prices ------------------------------------------------------------------------------------------------------

class PricesTest(SpendCase):
    def override(self, models):
        (self.data / "prices.json").write_text(json.dumps(models))

    def test_prices_json_is_v1s_file(self):
        mine = (ROOT / "lib" / "assistant" / "prices.json").read_bytes()
        out = subprocess.run(["git", "-C", str(ROOT), "show", "519346b:lib/autopilot/prices.json"],
                             capture_output=True, timeout=30)
        if out.returncode == 0:
            self.assertEqual(json.loads(mine), json.loads(out.stdout))
            self.assertEqual(mine, out.stdout)  # byte for byte
        else:  # a shallow checkout without v1's commit: its file's digest, taken from that commit
            self.assertEqual(hashlib.sha256(mine).hexdigest(), V1_PRICES_DIGEST)
        doc = json.loads(mine)
        self.assertEqual(doc["as_of"], "2026-11-11")
        self.assertTrue(doc["source"])
        self.assertIn("claude-haiku-4-5", doc["models"])

    def test_data_folder_prices_override(self):
        self.put("unknown.jsonl")
        self.override({"claude-nonesuch-9": {"input": 1, "output": 1, "cache_read": 0, "cache_write_5m": 0,
                                             "cache_write_1h": 0}})
        self.assertAlmostEqual(self.costs()[0]["usd"], 0.002)
        self.assertEqual(spend.prices(self.data)["claude-haiku-4-5"], HAIKU)  # one model at a time

    def test_incomplete_price_override_is_unknown(self):
        self.put()
        for bad in ({"claude-haiku-4-5": {"input": 1, "output": 1}}, {"claude-haiku-4-5": "free"},
                    {"claude-haiku-4-5": dict(HAIKU, over_100k={"input": 1})}):
            self.override(bad)
            self.assertIsNone(self.costs()[0]["usd"], bad)

    def test_a_broken_override_is_an_error_not_free(self):
        (self.data / "prices.json").write_text("{not json")
        with self.assertRaises(spend.SpendError):
            spend.prices(self.data)
        (self.data / "prices.json").write_text("[]")
        with self.assertRaises(spend.SpendError):
            spend.prices(self.data)


# -- 3, 4. totals ---------------------------------------------------------------------------------------------------

class TotalsTest(SpendCase):
    def test_totals_windows(self):
        self.put("days.jsonl")
        self.launch()
        t = self.totals()
        # 2026-11-10 (Tue) is this week and month; 2026-11-11 is today. Haiku 4.5 input is $1/Mtok, 1M tokens each.
        self.assertAlmostEqual(t["today"], 1.0)
        self.assertAlmostEqual(t["week"], 2.0)
        self.assertAlmostEqual(t["month"], 2.0)
        self.assertFalse(t["unknown"])
        self.assertEqual(set(t), TOTALS_KEYS)

    def test_week_starts_monday_and_month_is_calendar(self):
        sunday = dict(NEW, requestId="req_sun", timestamp="2026-11-15T20:00:00Z")  # Sunday 15:00 here, $1.00
        self.put(extra=[sunday])
        self.launch()
        t = self.totals(datetime(2026, 11, 15, 18, 0, tzinfo=TZ))  # that Sunday: still the week of Monday the 9th
        self.assertAlmostEqual(t["week"], STREAMING_USD + 1.0)
        t = self.totals(datetime(2026, 11, 16, 9, 0, tzinfo=TZ))  # next Monday: a new week, without the Sunday
        self.assertEqual(t["week"], 0.0)
        self.assertAlmostEqual(t["month"], STREAMING_USD + 1.0)
        self.assertEqual(self.totals(datetime(2026, 12, 1, 9, 0, tzinfo=TZ))["month"], 0.0)

    def test_unknown_model_marks_unknown(self):
        self.put("unknown.jsonl")
        self.launch()
        t = self.totals()
        self.assertIs(t["unknown"], True)
        self.assertEqual(t["month"], 0.0)

    def test_subscription_launches_excluded(self):
        self.put()
        self.launch(p=profile("sub", "subscription"))
        self.assertEqual(self.totals(mode="gateway")["month"], 0.0)
        self.assertAlmostEqual(self.totals(mode="subscription")["month"], STREAMING_USD)

    def test_switching_gateway_profiles_sums(self):
        self.put()
        self.launch(p=profile("g1"))
        self.put("days.jsonl", sid=SID_B)
        self.launch("efgh5678", profile("g2"), session=SID_B)
        self.assertAlmostEqual(self.totals()["month"], STREAMING_USD + 2.0)

    def test_resumed_session_counts_once(self):
        self.put()
        self.launch(p=profile("sub", "subscription"))
        self.launch("zzzz9999", at=L0 + timedelta(minutes=1))  # the same session, resumed under a gateway
        self.assertAlmostEqual(self.totals()["month"], STREAMING_USD)  # in the mode of its latest launch
        self.assertEqual(self.totals(mode="subscription")["month"], 0.0)

    def test_resumed_copy_counts_shared_requests_once(self):
        self.put("days.jsonl")
        self.launch()
        self.put("days.jsonl", sid=SID2, extra=[NEW])
        self.launch("zzzz9999", session=SID2, at=L0 + timedelta(minutes=1))
        t = self.totals()
        self.assertAlmostEqual(t["month"], 3.0)  # req_a + req_b once, req_c once
        self.assertAlmostEqual(t["today"], 2.0)

    def test_subscription_history_resumed_under_a_gateway_stays_subscription(self):
        self.put("days.jsonl")
        self.launch(p=profile("sub", "subscription"))
        self.put("days.jsonl", sid=SID2, extra=[NEW])
        self.launch("zzzz9999", profile("g1"), session=SID2, at=L0 + timedelta(minutes=1))
        self.assertAlmostEqual(self.totals(mode="gateway")["month"], 1.0)
        self.assertAlmostEqual(self.totals(mode="subscription")["month"], 2.0)

    def test_unknown_today_only_counts_what_is_dated_today(self):
        self.listing.clear()
        self.launch(session="", at=TODAY_8)  # an unresolved launch made today, past the grace
        t = self.totals()
        self.assertEqual((t["unknown"], t["unknown_today"], t["unknown_launches"]), (True, True, 1))
        # The same launch made yesterday: unknown this month, but it does not hold today.
        shutil.rmtree(self.data / "spend")
        self.launch(session="")
        t = self.totals()
        self.assertEqual((t["unknown"], t["unknown_today"], t["unknown_launches"]), (True, False, 0))

    def test_missing_transcript_unknown_today_follows_the_launch_day(self):
        self.launch()  # yesterday, resolved, but no transcript and nothing settled
        t = self.totals()
        self.assertEqual((t["unknown"], t["unknown_today"]), (True, False))
        self.launch("efgh5678", session=SID_B, at=TODAY_8)
        t = self.totals()
        self.assertEqual((t["unknown_today"], t["unknown_launches"]), (True, 1))

    def test_totals_name_the_unpriced_models_of_today(self):
        self.put("unknown.jsonl")
        self.launch()
        self.assertEqual(self.totals()["unknown_models"], [])  # its request is dated yesterday
        self.assertEqual(self.totals(datetime(2026, 11, 10, 20, 0, tzinfo=TZ))["unknown_models"],
                         ["claude-nonesuch-9"])

    def test_a_naive_now_is_local_time(self):
        self.put()
        self.launch()
        self.assertEqual(set(spend.totals(self.data, self.projects, datetime(2026, 11, 11, 12, 0),
                                          listing=self.lister)), TOTALS_KEYS)

    def test_a_naive_now_buckets_each_moment_by_the_local_rules_of_its_time(self):
        if not hasattr(time, "tzset"):
            self.skipTest("time.tzset is not available")
        patch = mock.patch.dict(os.environ, {"TZ": "America/Los_Angeles"})
        patch.start()
        self.addCleanup(time.tzset)  # runs last: after the zone is restored
        self.addCleanup(patch.stop)
        time.tzset()
        # Daylight saving time ends at 02:00 on 2026-11-01. 07:30Z is 00:30 that day (PDT), not 23:30 the day
        # before (PST, the offset at `now`); 06:50Z is 23:50 on October 31 (PDT).
        early = dict(NEW, requestId="req_oct", timestamp="2026-11-01T06:50:00Z")
        self.put(extra=[dict(NEW, timestamp="2026-11-01T07:30:00Z"), early])
        self.launch(at=datetime(2026, 11, 1, 6, 40, tzinfo=timezone.utc))
        now = datetime(2026, 11, 1, 10, 0)  # naive: local time, PST
        t = self.totals(now)
        self.assertAlmostEqual(t["today"], 1.0)
        self.assertAlmostEqual(t["month"], 1.0)
        self.assertIs(t["unknown"], False)
        # A launch at 00:20 PDT that cannot be matched is a launch of today, not of October.
        self.listing.clear()
        self.launch("efgh5678", session="", at=datetime(2026, 11, 1, 7, 20, tzinfo=timezone.utc))
        t = self.totals(now)
        self.assertEqual((t["unknown"], t["unknown_today"], t["unknown_launches"]), (True, True, 1))


class ResolveTest(SpendCase):
    def test_unresolved_short_id_is_unknown_then_found(self):
        self.put()
        self.listing.clear()
        self.launch(session="")
        t = self.totals()
        self.assertEqual(t["month"], 0.0)
        self.assertIs(t["unknown"], True)
        self.listing.append({"id": SHORT, "sessionId": SID})
        t = self.totals(NOW + timedelta(seconds=601))
        self.assertAlmostEqual(t["month"], STREAMING_USD)
        self.assertIs(t["unknown"], False)
        self.assertEqual(self.cache()["shorts"], {SHORT: SID})

    def test_unresolved_retry_is_rate_limited_and_gives_up(self):
        self.listing.clear()
        self.launch(session="")
        self.totals()
        self.assertEqual(self.listed, 1)
        self.totals(NOW + timedelta(seconds=300))
        self.assertEqual(self.listed, 1)  # past the grace: the next try is 10 minutes later
        self.totals(NOW + timedelta(seconds=700))
        self.assertEqual(self.listed, 2)
        late = NOW + timedelta(days=3)
        self.totals(late)
        self.totals(late + timedelta(hours=1))
        self.assertEqual(self.listed, 2)  # the launch is older than 2 days: no more tries
        self.assertIs(self.totals(late)["unknown"], True)

    def test_unresolved_launch_from_earlier_month_is_not_unknown(self):
        self.listing.clear()
        self.launch(session="")
        self.assertIs(self.totals(datetime(2026, 12, 2, 9, 0, tzinfo=TZ))["unknown"], False)

    def test_a_row_with_a_session_id_needs_no_listing(self):
        self.put()
        self.launch()
        self.assertAlmostEqual(self.totals()["month"], STREAMING_USD)
        self.settle(short=SHORT)
        self.assertEqual(self.listed, 0)
        self.assertEqual(self.rows("settled.jsonl")[-1]["session"], SID)

    def test_no_listing_resolves_nothing_and_tries_nothing(self):
        self.put()
        self.launch(session="")
        t = spend.totals(self.data, self.projects, NOW, listing=None)
        self.assertIs(t["unknown"], True)
        cache = self.data / "spend" / "cache.json"
        self.assertEqual(self.cache()["tries"] if cache.exists() else {}, {})

    # Spec addendum 1: new launches are not unknown during a grace.
    def test_a_new_unresolved_launch_is_not_unknown_within_the_grace(self):
        self.listing.clear()
        self.launch(session="", at=NOW - timedelta(minutes=5))
        t = self.totals()
        self.assertEqual((t["unknown"], t["unknown_today"], t["unknown_launches"]), (False, False, 0))
        # Resolved, but its transcript is not written yet: the same.
        self.launch("efgh5678", session=SID_B, at=NOW - timedelta(minutes=5))
        t = self.totals()
        self.assertEqual((t["unknown"], t["unknown_today"], t["unknown_launches"]), (False, False, 0))

    def test_an_unresolved_launch_past_the_grace_is_unknown(self):
        self.listing.clear()
        self.launch(session="", at=NOW - timedelta(seconds=960))  # the grace is 900 s
        t = self.totals()
        self.assertEqual((t["unknown"], t["unknown_today"], t["unknown_launches"]), (True, True, 1))
        self.launch("efgh5678", session=SID_B, at=NOW - timedelta(seconds=960))
        t = self.totals()
        self.assertEqual((t["unknown"], t["unknown_today"], t["unknown_launches"]), (True, True, 2))

    def test_young_launches_retry_resolution_every_minute(self):
        self.listing.clear()
        at = NOW - timedelta(minutes=1)
        self.launch(session="", at=at)
        self.totals()
        self.assertEqual(self.listed, 1)
        self.totals(NOW + timedelta(seconds=30))
        self.assertEqual(self.listed, 1)
        self.totals(NOW + timedelta(seconds=61))
        self.assertEqual(self.listed, 2)
        old = at + timedelta(seconds=spend.GRACE_S + 60)  # past the grace: every 10 minutes again
        self.totals(old)
        self.assertEqual(self.listed, 3)
        self.totals(old + timedelta(seconds=120))
        self.assertEqual(self.listed, 3)


# Spec addendum 2: a resumed copy's copied history is not new spend.
class CopyTest(SpendCase):
    def test_requests_older_than_the_first_launch_are_copies(self):
        self.put("days.jsonl", extra=[NEW])  # req_a, req_b at 04:50Z and 05:10Z; req_c at 15:00Z
        self.launch(at=TODAY_8)  # 13:00Z: req_a and req_b are older than the launch, minus 300 s
        t = self.totals()
        self.assertAlmostEqual(t["month"], 1.0)
        self.assertAlmostEqual(t["today"], 1.0)
        self.assertIs(t["unknown"], False)

    def test_a_copy_of_a_session_with_no_launch_row_is_not_billed(self):
        self.put("days.jsonl")  # the original, started before profiles: no launch row
        self.put("days.jsonl", sid=SID2, extra=[NEW])
        self.launch("zzzz9999", session=SID2, at=TODAY_8)
        self.assertAlmostEqual(self.totals()["month"], 1.0)

    def test_in_place_resume_keeps_the_sessions_own_requests(self):
        self.put("days.jsonl")
        self.launch()  # first launch: before both requests
        self.launch("zzzz9999", at=TODAY_8)  # the last launch is after them: the first one decides
        self.assertAlmostEqual(self.totals()["month"], 2.0)

    def test_a_request_just_before_the_launch_is_not_a_copy(self):
        self.put("days.jsonl")
        # req_b is at 05:10Z; a launch 4 minutes later still counts it (the 300 s slack), not req_a (24 minutes).
        self.launch(at=datetime(2026, 11, 11, 5, 14, tzinfo=timezone.utc))
        self.assertAlmostEqual(self.totals()["month"], 1.0)

    def test_settled_requests_older_than_the_first_launch_are_copies(self):
        self.put("days.jsonl", sid=SID2, extra=[NEW])
        self.launch("zzzz9999", session=SID2, at=TODAY_8)
        self.settle(session_id=SID2)
        (self.projects / "-tmp-x" / f"{SID2}.jsonl").unlink()
        self.assertAlmostEqual(self.totals()["month"], 1.0)


# -- 5. settle ------------------------------------------------------------------------------------------------------

class SettleTest(SpendCase):
    def test_settled_spend_survives_transcript_removal(self):
        self.put(sub="subagent.jsonl")
        self.launch()
        before = self.totals()
        self.settle(short=SHORT)
        self.transcript.unlink()
        shutil.rmtree(self.transcript.parent / SID)
        after = self.totals()
        self.assertGreater(before["month"], 0)
        self.assertAlmostEqual(after["month"], before["month"])
        self.assertAlmostEqual(after["week"], before["week"])
        self.assertIs(after["unknown"], False)
        self.assertTrue((self.data / "spend" / "settled.jsonl").exists())

    def test_settle_keeps_unknown_flag(self):
        self.put("unknown.jsonl")
        self.launch()
        self.settle(short=SHORT)
        self.transcript.unlink()
        self.assertIs(self.totals()["unknown"], True)

    def test_settle_without_transcript_records_unsettled_and_stays_unknown(self):
        self.listing.clear()
        self.launch(session="")
        self.settle(short=SHORT)
        line = self.rows("settled.jsonl")[0]
        self.assertIs(line["unsettled"], True)
        self.assertEqual(line["short"], SHORT)
        self.assertIs(self.totals()["unknown"], True)

    def test_dedupe_survives_the_original_being_settled_and_removed(self):
        self.put("days.jsonl")
        self.launch()
        self.put("days.jsonl", sid=SID2, extra=[NEW])
        self.launch("zzzz9999", session=SID2, at=L0 + timedelta(minutes=1))
        self.settle(short=SHORT)
        self.transcript.unlink()
        self.assertAlmostEqual(self.totals()["month"], 3.0)

    def test_a_launch_resolved_only_by_its_settle_keeps_its_cost(self):
        self.put()
        self.listing.clear()
        self.launch(session="")  # not listed yet when it started
        self.settle(short=SHORT, session_id=SID)  # the stop found its session id
        self.transcript.unlink()  # removed, and no longer listed
        (self.data / "spend" / "cache.json").unlink(missing_ok=True)
        t = spend.totals(self.data, self.projects, NOW, listing=lambda: [])
        self.assertAlmostEqual(t["month"], STREAMING_USD)
        self.assertEqual((t["unknown"], t["unknown_launches"]), (False, 0))

    def test_settle_by_session_id(self):
        self.put()
        self.launch()
        self.settle(session_id=SID)
        self.transcript.unlink()
        self.assertAlmostEqual(self.totals()["month"], STREAMING_USD)
        self.assertEqual(self.listed, 0)

    def test_the_latest_settle_wins(self):
        self.put("days.jsonl")
        self.launch()
        self.settle(session_id=SID, now=NOW)
        self.put("days.jsonl", extra=[NEW])
        self.settle(session_id=SID, now=NOW + timedelta(minutes=1))
        self.transcript.unlink()
        self.assertAlmostEqual(self.totals()["month"], 3.0)

    def test_settle_needs_a_short_or_a_session_id(self):
        with self.assertRaises(ValueError):
            self.settle()


# -- 6. cache and the ledger's folder -------------------------------------------------------------------------------

class LedgerTest(SpendCase):
    def test_record_launch_line(self):
        self.launch(session="", at=NOW)
        line = self.rows("launches.jsonl")[0]
        at = line.pop("at")
        self.assertEqual(line, {"short": SHORT, "session": "", "kind": "worker", "project": "shop", "profile": "g1",
                                "mode": "gateway"})
        self.assertEqual(datetime.fromisoformat(at), NOW)  # local time, with its offset
        self.assertEqual(datetime.fromisoformat(at).utcoffset(), NOW.astimezone().utcoffset())

    def test_record_launch_rejects_unknown_kind(self):
        with self.assertRaises(ValueError):
            spend.record_launch(self.data, SHORT, "bogus", None, profile())
        self.assertEqual(spend.KINDS, ("worker", "dispatcher", "replies", "refresh", "discover", "oneoff"))

    def test_cache_rereads_only_changed_files(self):
        self.put()
        self.launch()
        self.totals()
        with mock.patch("assistant.spend._parse_file", side_effect=AssertionError("re-read")):
            self.assertAlmostEqual(self.totals()["month"], STREAMING_USD)
        with self.transcript.open("a") as f:
            f.write(json.dumps({"type": "assistant", "requestId": "r9", "timestamp": "2026-11-10T16:00:00Z",
                                "message": {"model": "claude-haiku-4-5",
                                            "usage": {"input_tokens": 1000000, "output_tokens": 0}}}) + "\n")
        self.assertAlmostEqual(self.totals()["month"], STREAMING_USD + 1.0)

    def test_cache_holds_the_files_requests_of_this_month_only(self):
        self.put("days.jsonl")
        self.launch()
        self.totals()
        ent = list(self.cache()["files"].values())[0]
        self.assertEqual(sorted(ent["reqs"]), ["req_a", "req_b"])  # the request ids dedupe resumed copies
        self.totals(datetime(2026, 12, 1, 12, 0, tzinfo=TZ))  # the same file read in December keeps no November one
        ent = list(self.cache()["files"].values())[0]
        self.assertEqual(ent["reqs"], {})

    def test_cache_temp_file_is_per_process(self):
        self.put()
        self.launch()
        seen = []
        real = os.replace
        with mock.patch("assistant.spend.os.replace", side_effect=lambda a, b: (seen.append(str(a)), real(a, b))):
            self.totals()
        self.assertTrue(seen and all(f".{os.getpid()}." in x for x in seen), seen)
        self.assertEqual(sorted(p.name for p in (self.data / "spend").iterdir()),
                         ["cache.json", "launches.jsonl"])  # no temp file left

    def test_spend_folder_symlink_is_refused(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (self.data / "spend").symlink_to(elsewhere)
        with self.assertRaisesRegex(spend.SpendError, "symlink"):
            self.launch()
        with self.assertRaisesRegex(spend.SpendError, "symlink"):
            self.totals()
        self.assertEqual(list(elsewhere.iterdir()), [])
        (self.data / "spend").unlink()
        (self.data / "spend").mkdir()
        for name in ("launches.jsonl", "settled.jsonl", "cache.json"):
            target = elsewhere / name
            target.write_text("")
            (self.data / "spend" / name).symlink_to(target)
            with self.assertRaisesRegex(spend.SpendError, "symlink"):
                self.launch()
                self.settle(session_id=SID)
                self.totals()
            (self.data / "spend" / name).unlink()
            self.assertEqual(target.read_text(), "", name)
        (self.data / "prices.json").symlink_to(elsewhere / "cache.json")
        with self.assertRaisesRegex(spend.SpendError, "symlink"):
            spend.prices(self.data)

    def test_a_broken_line_is_skipped_and_an_unreadable_ledger_is_an_error(self):
        self.put()
        self.launch()
        with (self.data / "spend" / "launches.jsonl").open("a") as f:
            f.write("not json\n[1]\n")
        self.assertAlmostEqual(self.totals()["month"], STREAMING_USD)
        (self.data / "spend" / "settled.jsonl").mkdir()
        with self.assertRaises(spend.SpendError):
            self.totals()

    def test_a_broken_cache_is_rebuilt(self):
        self.put()
        self.launch()
        (self.data / "spend").mkdir(exist_ok=True)
        (self.data / "spend" / "cache.json").write_text("{broken")
        self.assertAlmostEqual(self.totals()["month"], STREAMING_USD)
        self.assertIn("files", self.cache())


# -- 7. workers record every launch and settle every stop and rm -----------------------------------------------------

class WorkerSpendTest(WorkerCase):
    def use_gateway(self, name="g"):
        profiles.save(self.data, gateway(name))
        profiles.use(self.data, name)

    def launches(self):
        f = self.data / "spend" / "launches.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def settled(self):
        f = self.data / "spend" / "settled.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def test_start_records_a_worker_launch(self):
        (self.data / "projects").mkdir()
        (self.data / "projects" / "shop.yaml").write_text(PROJECT)
        self.use_gateway()
        cid = self.add_card(project="shop")
        rec = self.workers().start(cid, "implement", cwd=self.wt, prompt="Do the work.")
        rows = self.launches()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual({k: row[k] for k in ("short", "session", "kind", "project", "profile", "mode")},
                         {"short": rec["short_id"], "session": rec["session_id"], "kind": "worker",
                          "project": "shop", "profile": "g", "mode": "gateway"})
        self.assertTrue(rec["session_id"])
        self.assertEqual(datetime.fromisoformat(row["at"]), self.clock())

    def test_resume_records_a_new_launch(self):
        a = self.start()
        self.use_gateway()
        self.drop_agent(a["short_id"])  # dead: an in-place resume
        self.assertEqual(self.workers().check()[0]["did"], "resumed")
        rows = self.launches()
        self.assertEqual([(r["profile"], r["mode"]) for r in rows], [("subscription", "subscription"),
                                                                     ("g", "gateway")])
        new = self.workers().registry.read()[0]
        self.assertEqual((rows[1]["short"], rows[1]["session"]), (new["short_id"], new["session_id"]))
        self.assertNotEqual(rows[1]["short"], rows[0]["short"])
        # The resume stopped the old session first: it was settled then.
        self.assertIn(a["session_id"], [r.get("session") for r in self.settled()])

    def test_a_resume_settles_the_old_session_by_its_id(self):
        a = self.start()
        (self.data / "spend" / "launches.jsonl").unlink()  # started before the ledger: no row names its session
        self.drop_agent(a["short_id"])  # dead, and the listing no longer shows it
        self.assertEqual(self.workers().check()[0]["did"], "resumed")
        old = [r for r in self.settled() if r.get("session") == a["session_id"]]
        self.assertEqual(len(old), 1, self.settled())
        self.assertIn("reqs", old[0])
        self.assertNotIn("unsettled", old[0])

    def test_stop_settles_the_session(self):
        a = self.start()
        self.assertEqual(self.settled(), [])
        real, at_stop = subprocess.run, []

        def run(argv, *args, **kw):
            if argv[1:2] == ["stop"]:
                at_stop.append(len(self.settled()))
            return real(argv, *args, **kw)
        with mock.patch("assistant.workers.subprocess.run", side_effect=run):
            self.workers().end(a["card"], "implement")
        self.assertEqual(at_stop, [0])  # settled after the stop returned, not before
        rows = self.settled()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["session"], rows[0]["short"]), (a["session_id"], a["short_id"]))
        self.assertIn("reqs", rows[0])
        self.assertNotIn("unsettled", rows[0])

    def test_a_stop_that_raised_still_settles(self):
        a = self.start()
        w = self.workers()
        real = subprocess.run

        def run(argv, *args, **kw):
            if argv[1:2] == ["stop"]:
                raise subprocess.TimeoutExpired(argv, 60)
            return real(argv, *args, **kw)
        with mock.patch("assistant.workers.subprocess.run", side_effect=run):
            with self.assertRaises(subprocess.TimeoutExpired):
                w._kill(a["short_id"])
        self.assertEqual([r["session"] for r in self.settled()], [a["session_id"]])

    def test_finish_settles_before_rm(self):
        a = self.start()
        w = self.workers()
        w.end(a["card"], "implement")
        before = len(self.settled())
        seen = []

        def rm(cfg, claude, sid):
            seen.append((sid, [r.get("session") for r in self.settled()[before:]]))
        with mock.patch.object(transcripts, "remove_original", side_effect=rm):
            w.finish(a["card"])
        self.assertEqual(seen, [(a["session_id"], [a["session_id"]])])

    def test_a_failing_ledger_never_fails_a_start(self):
        for exc in (OSError("disk full"), spend.SpendError("disk full")):
            err = io.StringIO()
            with mock.patch.object(spend, "record_launch", side_effect=exc), contextlib.redirect_stderr(err):
                rec = self.start()
            self.assertTrue(rec["short_id"])
            lines = err.getvalue().splitlines()
            self.assertEqual(len(lines), 1, lines)
            self.assertTrue(lines[0].startswith(f"ap2: spend ledger: record failed for {rec['short_id']}: "),
                            lines[0])
            self.assertIn("disk full", lines[0])
            self.assertLessEqual(len(lines[0]), 300)
        err = io.StringIO()
        with mock.patch.object(spend, "settle", side_effect=ValueError("x" * 1000)), contextlib.redirect_stderr(err):
            self.workers().end(rec["card"], "implement")
        lines = err.getvalue().splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith("ap2: spend ledger: settle failed for "), lines[0])
        self.assertLessEqual(len(lines[0]), 300)

    def test_an_unwritable_spend_folder_never_fails_a_start(self):
        (self.data / "spend").write_text("not a folder")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rec = self.start()
        self.assertEqual(self.workers().registry.read()[0]["short_id"], rec["short_id"])
        self.assertIn("ap2: spend ledger: record failed for", err.getvalue())

    def test_the_normal_path_prints_nothing(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            a = self.start()
            w = self.workers()
            w.end(a["card"], "implement")
            w.finish(a["card"])
        self.assertEqual(err.getvalue(), "")

    def test_ledger_is_edit_denied(self):
        self.use_gateway()
        w = self.workers()
        for role in ("implement", "review", "pr"):
            cid = self.add_card()
            w.start(cid, role, cwd=self.wt)
            deny = self.deny(cid, role)
            self.assertIn(f"Edit(/{self.data}/spend/**)", deny, role)
            self.assertIn(f"Edit(/{self.data}/prices.json)", deny, role)


# -- 8. ap2 spend ---------------------------------------------------------------------------------------------------

class SpendCliTest(unittest.TestCase):
    """`ap2 spend` run as a command, with a fake `claude` first on PATH and a temp home folder. It runs in a fixed
    offset zone where it is about HOUR o'clock now, so "a minute ago" is always today, whatever the real time."""

    HOUR = 12

    def setUp(self):
        h = self.HOUR - datetime.now(timezone.utc).hour  # -11 .. +12: a valid offset for both forms below
        self.tz = timezone(timedelta(hours=h))
        self.tzenv = f"<{'+' if h >= 0 else '-'}{abs(h):02d}>{-h:+d}"  # POSIX TZ: the sign is inverted
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-spend-cli-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.projects = self.tmp / "projects"
        (self.projects / "-tmp-x").mkdir(parents=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "claude").symlink_to(FAKE)
        self.home = self.tmp / "home"
        self.home.mkdir()
        (self.tmp / "fake").mkdir()

    def ap2(self, *args):
        env = {k: v for k, v in os.environ.items() if k != "AP2_DATA"}
        env.update(HOME=str(self.home), PATH=f"{self.bin}{os.pathsep}{env.get('PATH', '')}",
                   FAKE_CLAUDE_HOME=str(self.tmp / "fake"), TZ=self.tzenv)
        return subprocess.run([sys.executable, str(ROOT / "bin" / "ap2"), "spend", "--data", str(self.data),
                               "--transcripts", str(self.projects), *args], capture_output=True, text=True,
                              env=env, timeout=60)

    def session(self, mode="gateway", sid=SID):
        """A session launched a minute ago that made one $1.00 request a few seconds ago."""
        now = datetime.now(self.tz)
        spend.record_launch(self.data, "s0001", "worker", None, profile("p", mode), session_id=sid,
                            at=now - timedelta(minutes=1))
        line = dict(NEW, timestamp=(now - timedelta(seconds=5)).isoformat())
        (self.projects / "-tmp-x" / f"{sid}.jsonl").write_text(json.dumps(line) + "\n")
        return now - timedelta(seconds=5)

    def test_spend_prints_today_week_month(self):
        self.session()
        r = self.ap2()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        lines = r.stdout.splitlines()
        self.assertEqual(lines[0], "gateway spend (local time):")
        self.assertEqual(lines[1:4], ["  today  $1.00", "  week   $1.00", "  month  $1.00"])
        self.assertEqual(len(lines), 4)

    def test_spend_shows_unknown_with_its_models_and_launches(self):
        now = datetime.now(self.tz)
        spend.record_launch(self.data, "zz99", "worker", None, profile(), at=now - timedelta(hours=1))
        line = dict(NEW, timestamp=(now - timedelta(seconds=5)).isoformat())
        line["message"] = dict(NEW["message"], model="claude-nonesuch-9")
        (self.projects / "-tmp-x" / f"{SID}.jsonl").write_text(json.dumps(line) + "\n")
        spend.record_launch(self.data, "s0001", "worker", None, profile(), session_id=SID,
                            at=now - timedelta(minutes=1))
        r = self.ap2()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("  unknown: some spend is not counted", r.stdout)
        self.assertIn("unpriced models today: claude-nonesuch-9", r.stdout)
        self.assertIn("launches not matched today: 1", r.stdout)

    def test_spend_json_is_the_totals_dict(self):
        self.session()
        r = self.ap2("--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(set(out), TOTALS_KEYS)
        self.assertAlmostEqual(out["today"], 1.0)
        self.assertEqual((out["unknown"], out["unknown_models"], out["unknown_launches"]), (False, [], 0))

    def test_spend_mode_subscription(self):
        self.session(mode="subscription")
        r = self.ap2("--mode", "subscription")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("subscription spend (local time):", r.stdout)
        self.assertIn("  today  $1.00", r.stdout)
        self.assertIn("  today  $0.00", self.ap2().stdout)
        self.assertEqual(self.ap2("--mode", "other").returncode, 2)

    def test_a_broken_ledger_prints_one_line_and_exits_1(self):
        self.session()
        (self.data / "spend" / "settled.jsonl").mkdir()
        r = self.ap2()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, "")
        self.assertEqual(len(r.stderr.splitlines()), 1, r.stderr)
        self.assertTrue(r.stderr.startswith("ap2: "), r.stderr)

    def test_usage_lists_spend(self):
        r = subprocess.run([sys.executable, str(ROOT / "bin" / "ap2"), "--help"], capture_output=True, text=True,
                           env=dict(os.environ, HOME=str(self.home)), timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("spend", r.stdout)
        self.assertIn("print what workers cost today, this week and this month", r.stdout)

    def test_spend_on_an_empty_data_folder(self):
        r = self.ap2("--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["month"], 0.0)


if __name__ == "__main__":
    unittest.main()
