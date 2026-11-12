"""The spend ledger (design section 12, spend ledger): ap2 prices its own workers' sessions from their transcripts,
settles each one before its `claude stop` and its `claude rm`, and `ap2 spend` prints the totals. Ported from v1's
`tests/test_spend.py` (v1 commit 519346b); every folder is a temp folder, and the session listing is a function the
test passes in, so no test runs `claude`."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
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
from tests.test_ap2_workers import FAKE, PROJECT, WorkerCase
from assistant import cli, profiles, spend, transcripts

FIX = ROOT / "tests" / "fixtures" / "spend"
TZ = timezone(timedelta(hours=-5))
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=TZ)  # a Wednesday
L0 = datetime(2026, 11, 10, 8, 0, tzinfo=TZ)  # the default launch: before every fixture request
TODAY_8 = datetime(2026, 11, 11, 8, 0, tzinfo=TZ)
SID = "11111111-0000-0000-0000-00000000aaaa"
SID_B = "22222222-0000-0000-0000-00000000bbbb"
SID2 = "22222222-0000-0000-0000-00000000cccc"
SHORT = "abcd1234"
SHIPPED = json.loads((ROOT / "lib" / "assistant" / "prices.json").read_text())["models"]
HAIKU = SHIPPED["claude-haiku-4-5"]
LONG = SHIPPED["claude-haiku-5-5"]  # a model with an over_100k tier
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

    def test_long_prompt_tier_counts_the_whole_prompt_and_starts_over_the_limit(self):
        def ln(rid, **u):
            return json.dumps({"type": "assistant", "requestId": rid, "timestamp": "2026-11-10T15:00:00Z",
                               "message": {"model": "claude-haiku-5-5", "usage": dict(u, output_tokens=0)}})
        self.transcript.parent.mkdir(parents=True)
        self.transcript.write_text("\n".join([
            ln("over", input_tokens=1000, cache_read_input_tokens=100_000),  # 101,000 prompt tokens
            ln("at", input_tokens=50_000, cache_read_input_tokens=50_000),  # exactly 100,000: not over
        ]) + "\n")
        over, at = LONG["over_100k"], LONG
        self.assertEqual(len(self.costs()), 2)
        self.assertAlmostEqual(self.costs()[0]["usd"], (1000 * over["input"] + 100_000 * over["cache_read"]) / 1e6)
        self.assertAlmostEqual(self.costs()[1]["usd"], (50_000 * at["input"] + 50_000 * at["cache_read"]) / 1e6)

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
        # A count that is a finite number but whose price is too big for one: unknown, never an infinite total.
        out = json.dumps(dict(NEW, requestId="r_out")).replace('"output_tokens": 0', '"output_tokens": 1e308')
        digits = json.dumps(dict(NEW, requestId="r_309")).replace('"output_tokens": 0',
                                                                  '"output_tokens": 1' + "0" * 308)
        self.transcript.write_text("\n".join([line, big, out, digits, json.dumps(NEW)]) + "\n")
        self.launch()
        t = self.totals()
        self.assertAlmostEqual(t["month"], 1.0)  # the honest request only
        self.assertEqual((t["unknown"], t["unknown_today"]), (True, True))
        for k in ("today", "week", "month"):
            self.assertTrue(math.isfinite(t[k]), k)

    def test_a_negative_count_is_unknown_never_a_discount(self):
        self.transcript.parent.mkdir(parents=True)

        def neg(rid, **u):
            return dict(NEW, requestId=rid, message=dict(NEW["message"], usage=u))
        lines = [NEW, neg("r_in", input_tokens=-10_000_000_000, output_tokens=1),
                 neg("r_out", input_tokens=10, output_tokens=-1),
                 neg("r_read", input_tokens=10, cache_read_input_tokens=-1),
                 neg("r_cc", input_tokens=10, cache_creation_input_tokens=-1),
                 neg("r_1h", input_tokens=10, cache_creation={"ephemeral_1h_input_tokens": -1}),
                 neg("r_5m", input_tokens=10, cache_creation={"ephemeral_5m_input_tokens": -1}),
                 # Fewer cache tokens in all than in the 1h kind is v1's rule, not a negative count: priced.
                 neg("r_ok", input_tokens=10, cache_creation_input_tokens=100,
                     cache_creation={"ephemeral_1h_input_tokens": 200})]
        self.transcript.write_text("".join(json.dumps(x) + "\n" for x in lines))
        items = self.costs()
        self.assertAlmostEqual(items[0]["usd"], 1.0)
        self.assertEqual([i["usd"] for i in items[1:-1]], [None] * 6)
        self.assertAlmostEqual(items[-1]["usd"], usd(input=10, cache_write_1h=200))
        self.launch()
        t = self.totals()
        self.assertAlmostEqual(t["month"], 1.0 + usd(input=10, cache_write_1h=200))
        self.assertEqual((t["unknown"], t["unknown_today"]), (True, True))

    def test_a_timestamp_past_what_a_date_can_show_is_unknown_not_an_error(self):
        self.transcript.parent.mkdir(parents=True)
        far = dict(NEW, requestId="r_far", timestamp="9999-12-31T23:59:59-14:00")
        old = dict(NEW, requestId="r_old", timestamp="1970-01-01T00:00:00Z")
        self.transcript.write_text("".join(json.dumps(x) + "\n" for x in (NEW, far, old)))
        self.assertEqual([(i["day"], i["usd"]) for i in self.costs()],
                         [("2026-11-11", 1.0), (None, None), (None, None)])
        self.launch()
        for now in (NOW, datetime(2026, 11, 11, 12, 0), None):  # aware, naive and no `now`
            t = spend.totals(self.data, self.projects, now, listing=self.lister)
            self.assertIs(t["unknown"], True, now)
        t = self.totals()
        self.assertAlmostEqual(t["month"], 1.0)
        self.settle(short=SHORT)
        self.transcript.unlink()
        t = self.totals()
        self.assertAlmostEqual(t["month"], 1.0)
        self.assertIs(t["unknown"], True)

    def test_a_settled_timestamp_past_what_a_date_can_show_is_unknown_not_an_error(self):
        self.launch()
        self.settle(session_id=SID)  # no transcript: unsettled; then a row as an older version wrote it
        far = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone(timedelta(hours=-14))).timestamp()
        with (self.data / "spend" / "settled.jsonl").open("a") as f:
            f.write(json.dumps({"batch": spend._iso(NOW), "session": SID, "short": SHORT,
                                "reqs": {"r_far": [far, 5.0], "r_inf": [1e400, 5.0], "r_ok": [NOW.timestamp(), 1.0]}})
                    + "\n")
        for now in (NOW, datetime(2026, 11, 11, 12, 0)):
            t = spend.totals(self.data, self.projects, now, listing=self.lister)
            self.assertIs(t["unknown"], True, now)
        t = self.totals()
        self.assertAlmostEqual(t["month"], 1.0)
        self.assertEqual((t["unknown"], t["unknown_today"]), (True, False))

    def test_a_subagent_path_that_is_not_a_regular_file_never_blocks(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("os.mkfifo is not available")
        self.put()
        d = self.transcript.parent / SID / "subagents"
        d.mkdir(parents=True)
        fifo = d / "agent-x.jsonl"
        os.mkfifo(fifo)
        (d / "agent-y.jsonl").symlink_to(fifo)
        if Path("/dev/zero").exists():
            (d / "agent-z.jsonl").symlink_to("/dev/zero")
        self.launch()
        self.assertEqual(spend._files(self.transcript), [self.transcript])
        out = {}

        def run():
            out["parse"] = spend._parse_file(fifo, spend.prices(self.data))
            out["settle"] = self.settle(short=SHORT)
            out["totals"] = self.totals()
        th = threading.Thread(target=run, daemon=True)
        th.start()
        th.join(5)
        blocked = th.is_alive()
        if blocked:  # give the blocked reader an end of file, so it does not outlive the test
            for _ in range(50):
                try:
                    os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
                except OSError:
                    pass
                th.join(0.1)
        self.assertFalse(blocked, "blocked on a FIFO")
        self.assertEqual(out["parse"], [])
        self.assertEqual(sorted(out["settle"]["reqs"]), ["req_1"])
        self.assertAlmostEqual(out["totals"]["month"], STREAMING_USD)

    def test_model_keys_drop_the_context_tag_and_the_date(self):
        self.assertEqual(spend._model_key("claude-haiku-4-5[1m]"), "claude-haiku-4-5")
        self.assertEqual(spend._model_key("claude-haiku-4-5-20251001"), "claude-haiku-4-5")

    def test_a_model_name_of_many_brackets_is_read_in_linear_time(self):
        hostile = "[" * 200000
        start = time.monotonic()
        keys = [spend._model_key(hostile), spend._model_key(hostile + "x]")]
        self.assertLess(time.monotonic() - start, 2.0)  # a quadratic scan of these takes about a minute
        self.assertEqual(keys, [hostile, hostile[:-1]])

    def test_a_line_nested_too_deep_is_skipped_not_an_error(self):
        self.transcript.parent.mkdir(parents=True)
        lines = ["[" * 100000] + (FIX / "streaming.jsonl").read_text().splitlines()  # first: the rest still counts
        self.transcript.write_text("\n".join(lines) + "\n")
        self.launch()
        self.assertAlmostEqual(self.totals()["month"], STREAMING_USD)
        self.assertEqual(sorted(self.settle(session_id=SID)["reqs"]), ["req_1"])
        self.assertEqual(sorted(self.rows("settled.jsonl")[-1]["reqs"]), ["req_1"])


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

    def across_a_month_start(self):
        def at(day, rid, model="claude-haiku-4-5"):  # noon here, $1.00 for a priced model
            return dict(NEW, requestId=rid, timestamp=f"{day}T17:00:00Z", message=dict(NEW["message"], model=model))
        self.transcript.parent.mkdir(parents=True)
        self.transcript.write_text("".join(json.dumps(x) + "\n" for x in (
            at("2026-09-29", "r_tue"), at("2026-09-30", "r_wed"), at("2026-10-01", "r_thu"),
            at("2026-09-30", "r_wed_u", "claude-nonesuch-9"))))
        self.launch(at=datetime(2026, 9, 28, 8, 0, tzinfo=TZ))  # Monday

    def test_a_week_that_began_last_month_counts_its_days_from_last_month(self):
        self.across_a_month_start()
        thu = datetime(2026, 10, 1, 18, 0, tzinfo=TZ)
        t = self.totals(thu)
        self.assertAlmostEqual(t["week"], 3.0)
        self.assertAlmostEqual(t["month"], 1.0)
        self.assertAlmostEqual(t["today"], 1.0)
        self.assertIs(t["unknown"], False)  # September's unpriced request is not this month's
        self.settle(short=SHORT, now=thu)
        self.transcript.unlink()
        t = self.totals(thu)
        self.assertEqual((round(t["week"], 6), round(t["month"], 6), round(t["today"], 6), t["unknown"]),
                         (3.0, 1.0, 1.0, False))

    def test_a_cache_built_last_month_keeps_the_weeks_days(self):
        self.across_a_month_start()
        t = self.totals(datetime(2026, 9, 30, 18, 0, tzinfo=TZ))
        self.assertEqual((round(t["week"], 6), round(t["month"], 6), t["unknown"]), (2.0, 2.0, True))
        with mock.patch("assistant.spend._parse_file", side_effect=AssertionError("re-read")):
            t = self.totals(datetime(2026, 10, 1, 18, 0, tzinfo=TZ))
        self.assertEqual((round(t["week"], 6), round(t["month"], 6), round(t["today"], 6), t["unknown"]),
                         (3.0, 1.0, 1.0, False))
        t = self.totals(datetime(2026, 10, 5, 9, 0, tzinfo=TZ))  # the next Monday: a new week
        self.assertEqual((t["week"], round(t["month"], 6)), (0.0, 1.0))
        self.assertEqual(sorted(list(self.cache()["files"].values())[0]["reqs"]), ["r_thu"])

    def test_a_naive_now_counts_a_week_across_a_month_start(self):
        if not hasattr(time, "tzset"):
            self.skipTest("time.tzset is not available")
        patch = mock.patch.dict(os.environ, {"TZ": "America/Chicago"})
        patch.start()
        self.addCleanup(time.tzset)  # runs last: after the zone is restored
        self.addCleanup(patch.stop)
        time.tzset()
        self.across_a_month_start()
        t = self.totals(datetime(2026, 10, 1, 18, 0))  # naive: local time, CDT
        self.assertEqual((round(t["week"], 6), round(t["month"], 6), round(t["today"], 6), t["unknown"]),
                         (3.0, 1.0, 1.0, False))

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

    def test_an_unprintable_model_name_is_an_unnamed_model(self):
        surrogate = {"model": "\ud800", "usage": {"input_tokens": 10, "output_tokens": 0}}
        escapes = dict(surrogate, model="x\u001b[2J\u001b[H  today  $0.00\u001b]0;pwned\u0007")
        self.put(extra=[dict(NEW, requestId="req_s", message=surrogate),
                        dict(NEW, requestId="req_e", message=escapes)])
        self.launch()
        t = self.totals()
        self.assertEqual(t["unknown_models"], ["an unnamed model"])
        self.assertFalse(any("\x1b" in m for m in t["unknown_models"]))
        json.dumps(t).encode("utf-8")
        ", ".join(t["unknown_models"]).encode("utf-8")  # what ap2 spend prints

    def test_a_non_ascii_model_name_is_an_unnamed_model(self):
        wide = {"model": "claude-\u6a21\u578b", "usage": {"input_tokens": 10, "output_tokens": 0}}
        self.put(extra=[dict(NEW, requestId="req_w", message=wide)])
        self.launch()
        t = self.totals()
        self.assertEqual(t["unknown_models"], ["an unnamed model"])
        ", ".join(t["unknown_models"]).encode("ascii")  # ap2 spend prints it under any locale

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
        self.assertEqual((spend.GRACE_S, spend._RETRY_S, spend._YOUNG_RETRY_S, spend._GIVE_UP_S, spend.COPY_SLACK_S),
                         (900, 600, 60, 2 * 86400, 300))
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
        mon = datetime(2026, 11, 30, 8, 0, tzinfo=TZ)  # the Monday of the week that holds Dec 2
        self.launch(session="", at=mon)
        self.launch("efgh5678", session=SID_B, at=mon)  # resolved, but no transcript and nothing settled
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

    def test_a_subscription_launch_never_makes_gateway_spend_unknown(self):
        self.listing.clear()
        past_grace = NOW - timedelta(seconds=960)  # the grace is 900 s
        self.launch(p=profile("sub", "subscription"), session="", at=past_grace)  # unresolved
        self.launch("efgh5678", p=profile("sub", "subscription"), session=SID_B, at=past_grace)  # no transcript
        t = self.totals(mode="gateway")
        self.assertEqual((t["unknown"], t["unknown_today"], t["unknown_launches"]), (False, False, 0))
        t = self.totals(mode="subscription")
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

    def test_an_undated_request_of_a_session_launched_last_month_is_not_this_months_unknown(self):
        self.transcript.parent.mkdir(parents=True)
        dated = dict(NEW, requestId="r_dated", timestamp="2026-10-06T17:00:00Z")  # noon here, $1.00
        undated = dict(NEW, requestId="r_undated", timestamp="garbage")
        self.transcript.write_text(json.dumps(dated) + "\n" + json.dumps(undated) + "\n")
        self.launch(at=datetime(2026, 10, 5, 8, 0, tzinfo=TZ))
        self.settle(short=SHORT, now=datetime(2026, 10, 6, 18, 0, tzinfo=TZ))
        self.transcript.unlink()
        t = self.totals()  # November: the October session's undated request is not this month's
        self.assertEqual((t["month"], t["unknown"], t["unknown_today"]), (0.0, False, False))
        t = self.totals(datetime(2026, 10, 7, 9, 0, tzinfo=TZ))  # its own month: still unknown
        self.assertEqual((round(t["month"], 6), t["unknown"], t["unknown_today"]), (1.0, True, False))

    def test_settle_without_transcript_records_unsettled_and_stays_unknown(self):
        self.listing.clear()
        self.launch(session="")
        self.settle(short=SHORT)
        line = self.rows("settled.jsonl")[0]
        self.assertIs(line["unsettled"], True)
        self.assertEqual(line["short"], SHORT)
        self.assertIs(self.totals()["unknown"], True)
        # An unsettled row that names its session: only it can keep spend unknown once SHORT is found and priced.
        self.launch("efgh5678", session=SID_B)
        self.assertIs(self.settle(session_id=SID_B)["unsettled"], True)
        self.listing.append({"id": SHORT, "sessionId": SID})
        self.put()
        self.assertIs(self.totals(NOW + timedelta(seconds=601))["unknown"], True)

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
        before = self.listed
        t = self.totals()
        self.assertEqual(self.listed, before)  # the settled row names the session: no listing is asked
        self.assertAlmostEqual(t["month"], STREAMING_USD)
        self.assertEqual((t["unknown"], t["unknown_launches"]), (False, 0))

    def test_a_request_id_that_utf_8_cannot_hold_still_settles(self):
        self.put(extra=[dict(NEW, requestId="\ud800")])
        self.launch()
        row = self.settle(session_id=SID)
        self.assertIn("req_1", row["reqs"])
        self.assertIn("\ud800", row["reqs"])  # the surrogate-id request itself is settled
        self.assertIn("\ud800", self.rows("settled.jsonl")[-1]["reqs"])
        self.assertEqual(self.rows("settled.jsonl")[-1]["reqs"], row["reqs"])
        self.transcript.unlink()
        self.assertAlmostEqual(self.totals()["month"], STREAMING_USD + 1.0)

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


class _ReadSpy:
    """A binary file that records the size each read asks for; it has no `__iter__`, so a read must name a size."""

    def __init__(self, f, asks):
        self.f, self.asks = f, asks

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self.f.__exit__(*exc)

    def fileno(self):
        return self.f.fileno()

    def read(self, n=-1):
        self.asks.append(n)
        return self.f.read(n)

    def readline(self, n=-1):
        self.asks.append(n)
        return self.f.readline(n)


class HardeningTest(SpendCase):
    """Inputs a worker controls never break the ledger: a huge line or file, a folder it made unenterable, a sum
    too big for a number."""

    CAP = 4096

    def patch(self, obj, name, value):
        p = mock.patch.object(obj, name, value)
        p.start()
        self.addCleanup(p.stop)

    def spy(self):
        asks, real = [], os.fdopen

        def fdopen(fd, mode="r", *a, **kw):  # only a transcript's binary read is watched
            f = real(fd, mode, *a, **kw)
            return _ReadSpy(f, asks) if mode == "rb" else f
        self.patch(spend.os, "fdopen", fdopen)
        return asks

    def line(self, rid, size=None):
        """NEW as request `rid`; padded to `size` bytes with its newline (a priced line, were it parsed)."""
        s = json.dumps(dict(NEW, requestId=rid))
        if size is not None:
            s = s[:-1] + ', "pad": "' + "x" * (size - len(s) - 12) + '"}'
            self.assertEqual(len(s) + 1, size)
        return s + "\n"

    def write(self, *lines):
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        self.transcript.write_text("".join(lines))

    def test_a_line_over_the_cap_is_one_unknown_request_and_the_rest_keep_their_cost(self):
        self.patch(spend, "MAX_LINE_BYTES", self.CAP)
        self.write(self.line("req_a"), self.line("req_long", self.CAP + 1), self.line("req_b"))
        items = spend._parse_file(self.transcript, spend.prices(self.data))
        self.assertEqual([i["id"] for i in items], ["req_a", "~1", "req_b"])
        self.assertEqual(items[1], {"id": "~1", "ts": None, "model": "", "usd": None})
        self.assertEqual([round(i["usd"], 6) for i in (items[0], items[2])], [1.0, 1.0])
        self.launch(at=TODAY_8)
        t = self.totals()
        self.assertAlmostEqual(t["month"], 2.0)
        self.assertEqual((t["unknown"], t["unknown_today"]), (True, True))

    def test_a_line_over_the_cap_still_counts_toward_the_line_numbers_after_it(self):
        self.patch(spend, "MAX_LINE_BYTES", self.CAP)
        bare = dict(NEW, message={k: v for k, v in NEW["message"].items() if k != "id"})
        bare.pop("requestId", None)  # no request id and no message id: keyed by its line number
        self.write(self.line("req_a"), self.line("req_long", self.CAP + 1), json.dumps(bare) + "\n")
        items = spend._parse_file(self.transcript, spend.prices(self.data))
        self.assertEqual([(i["id"], i["usd"] is None) for i in items], [("req_a", False), ("~1", True), ("~2", False)])

    def test_a_line_at_the_cap_is_parsed_and_a_long_last_line_is_skipped(self):
        self.patch(spend, "MAX_LINE_BYTES", self.CAP)
        last = self.line("req_end", 3 * self.CAP + 7)[:-1]  # several chunks long, and no newline at the end
        self.write(self.line("req_at", self.CAP), self.line("req_b"), last)
        items = spend._parse_file(self.transcript, spend.prices(self.data))
        self.assertEqual([i["id"] for i in items], ["req_at", "req_b", "~2"])
        self.assertEqual([i["usd"] for i in items][2], None)
        self.write(self.line("req_a"), self.line("req_at", self.CAP + 1)[:-1])  # exactly the cap, then the end of file
        self.assertEqual([i["id"] for i in spend._parse_file(self.transcript, spend.prices(self.data))],
                         ["req_a", "req_at"])

    def test_no_read_asks_for_more_than_the_line_cap(self):
        self.patch(spend, "MAX_LINE_BYTES", self.CAP)
        asks = self.spy()
        self.write(self.line("req_a"), self.line("req_long", 5 * self.CAP), self.line("req_b"))
        items = spend._parse_file(self.transcript, spend.prices(self.data))
        self.assertEqual([i["id"] for i in items], ["req_a", "~1", "req_b"])
        self.assertTrue(asks)
        self.assertTrue(all(isinstance(n, int) and 0 < n <= self.CAP for n in asks), asks)

    def test_a_file_over_the_size_cap_is_not_read_and_is_unknown(self):
        self.patch(spend, "MAX_FILE_BYTES", 1 << 20)
        self.write(self.line("req_a"))
        os.truncate(self.transcript, (1 << 20) + 1)  # sparse: a hole after the line
        asks = self.spy()
        self.assertEqual(self.costs(), [{"day": None, "model": "", "usd": None}])
        self.assertEqual(asks, [])  # not read at all
        self.launch(at=TODAY_8)
        t = self.totals()
        self.assertEqual((t["month"], t["unknown"], t["unknown_today"]), (0.0, True, True))
        self.assertEqual(t["unknown_models"], ["an unnamed model"])

    def test_a_line_separator_inside_a_json_string_keeps_its_line(self):
        said = dict(NEW, message=dict(NEW["message"], content=[{"type": "text", "text": "a\u2028b\x85c"}]))
        self.write(json.dumps(said, ensure_ascii=False) + "\n")
        self.assertEqual([round(i["usd"], 6) for i in self.costs()], [1.0])

    def test_reading_past_the_file_cap_stops_with_one_unknown(self):
        f = io.BytesIO(b"a\n" * 10)  # a file that grew after its size was checked
        self.assertEqual(list(spend._lines(f, 8, 7)), [(0, b"a\n"), (1, b"a\n"), (2, b"a\n"), (None, None)])
        self.assertEqual(list(spend._lines(io.BytesIO(b"a\n" + b"x" * 30), 8, 1000)), [(0, b"a\n"), (1, None)])
        f = io.BytesIO(b"x" * 100)  # one long line: skipping it stops once the cap is passed
        self.assertEqual(list(spend._lines(f, 8, 20)), [(None, None)])
        self.assertEqual(f.tell(), 24)
        f = io.BytesIO(b"x" * 100)  # the skip reaches the cap exactly at a chunk's end: still one unknown
        self.assertEqual(list(spend._lines(f, 8, 16)), [(None, None)])
        self.assertEqual(f.tell(), 24)
        # exactly the cap is still read
        self.assertEqual(list(spend._lines(io.BytesIO(b"a\n" * 4), 8, 8)), [(i, b"a\n") for i in range(4)])

    def test_a_file_that_grows_past_the_cap_keeps_the_requests_read_before_it(self):
        self.write(self.line("req_a"), self.line("req_b"))
        table = spend.prices(self.data)
        self.patch(spend, "MAX_FILE_BYTES", len(self.line("req_a")) + 1)  # the cap falls inside the second line
        real = os.fstat

        def fstat(fd):  # the size check sees a tiny file, as if the file grew after it was checked
            st = list(real(fd))
            st[6] = 1
            return os.stat_result(st)
        self.patch(spend.os, "fstat", fstat)
        items = spend._parse_file(self.transcript, table)
        self.assertEqual([i["id"] for i in items], ["req_a", "~file"])
        self.assertEqual(round(items[0]["usd"], 6), 1.0)
        self.assertIsNone(items[1]["usd"])

    def test_a_file_exactly_at_the_size_cap_is_read(self):
        self.patch(spend, "MAX_FILE_BYTES", 1 << 20)
        self.write(self.line("req_a"))
        os.truncate(self.transcript, 1 << 20)  # the zero-filled rest is one broken line, skipped
        items = spend._parse_file(self.transcript, spend.prices(self.data))
        self.assertEqual([i["id"] for i in items], ["req_a"])

    def unenterable(self, d):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root enters any folder")
        mode = d.stat().st_mode & 0o7777
        d.chmod(0o600)
        self.addCleanup(d.chmod, mode)  # runs before the temp folder's removal

    def test_an_unenterable_transcript_folder_is_a_missing_transcript(self):
        self.put()
        self.launch()
        self.assertAlmostEqual(self.totals()["month"], STREAMING_USD)  # the cache now holds the file
        self.unenterable(self.transcript.parent)
        t = self.totals()
        self.assertEqual((t["month"], t["unknown"]), (0.0, True))
        self.assertNotIn(str(self.transcript), self.cache()["files"])
        row = self.settle(session_id=SID)
        self.assertIs(row["unsettled"], True)
        self.assertIs(self.rows("settled.jsonl")[-1]["unsettled"], True)

    def test_a_subagent_file_that_cannot_be_checked_is_left_out(self):
        self.put(sub="subagent.jsonl")
        self.unenterable(self.transcript.parent / SID / "subagents")
        self.assertEqual(spend._files(self.transcript), [self.transcript])
        self.launch()
        self.assertAlmostEqual(self.totals()["month"], STREAMING_USD)
        self.assertEqual(sorted(self.settle(session_id=SID)["reqs"]), ["req_1"])

    def test_a_subagents_folder_that_cannot_be_listed_leaves_only_the_main_transcript(self):
        # Some Python versions let `glob` raise on an unreadable folder instead of skipping it.
        self.put(sub="subagent.jsonl")
        with mock.patch.object(Path, "glob", side_effect=PermissionError("denied")):
            self.assertEqual(spend._files(self.transcript), [self.transcript])

    def test_a_sum_too_big_for_a_number_is_clamped_and_unknown(self):
        self.write(self.line("req_a"), self.line("req_b"))
        self.launch(at=TODAY_8)
        with mock.patch.object(spend, "_cost", return_value=sys.float_info.max):
            t = self.totals()
        for k in ("today", "week", "month"):
            self.assertEqual(t[k], sys.float_info.max, k)
        self.assertEqual((t["unknown"], t["unknown_today"]), (True, True))
        json.loads(json.dumps(t), parse_constant=_no_constant)

    def test_a_week_too_big_for_a_number_is_unknown_but_not_today(self):
        old = dict(NEW, requestId="req_old", timestamp="2026-11-10T15:00:00Z")  # Tuesday
        self.write(json.dumps(old) + "\n", self.line("req_c"))
        self.launch()
        fake = {"req_old": sys.float_info.max, "req_c": sys.float_info.max}
        real = spend._parse_file

        def parse(path, table):
            return [dict(it, usd=fake.get(it["id"], it["usd"])) for it in real(path, table)]
        with mock.patch.object(spend, "_parse_file", parse):
            t = self.totals(NOW + timedelta(days=1))  # Thursday: Tuesday plus Wednesday overflows the week
        self.assertEqual(t["week"], sys.float_info.max)
        self.assertEqual((t["unknown"], t["unknown_today"], t["today"]), (True, False, 0.0))


def _no_constant(name):
    raise ValueError(f"not JSON: {name}")


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

    def main(self, *args):
        """`ap2 spend` in this process (so a test can patch the ledger), with no `claude` on PATH."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("shutil.which", return_value=None), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = cli.main(["spend", "--data", str(self.data), "--transcripts", str(self.projects), *args])
        return code, out.getvalue(), err.getvalue()

    def test_a_transcript_error_names_the_transcripts_folder(self):
        self.session()
        bad = PermissionError(13, "Permission denied", str(self.projects / "-tmp-x" / f"{SID}.jsonl"))
        with mock.patch.object(spend, "totals", side_effect=bad):
            code, out, err = self.main()
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(len(err.splitlines()), 1, err)
        self.assertTrue(err.startswith(f"ap2: cannot read the transcripts in {self.projects}: "), err)
        self.assertNotIn("spend ledger", err)

    def test_a_ledger_error_names_the_ledger_and_an_unnamed_error_names_both(self):
        for name, says, not_says in ((str(self.data / "spend" / "cache.json"), "spend ledger", "transcripts"),
                                     (None, "transcripts", None), ("/elsewhere/x.jsonl", "transcripts", None)):
            bad = OSError(5, "Input/output error", name) if name else OSError("disk gone")
            with mock.patch.object(spend, "totals", side_effect=bad):
                code, out, err = self.main()
            self.assertEqual((code, out, len(err.splitlines())), (1, "", 1), err)
            self.assertTrue(err.startswith("ap2: cannot "), err)
            self.assertIn(says, err)
            self.assertIn(str(self.data / "spend"), err)
            if not_says:
                self.assertNotIn(not_says, err)
            else:
                self.assertIn(str(self.projects), err)

    def test_an_unenterable_transcript_folder_is_not_an_error(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root enters any folder")
        self.session()
        d = self.projects / "-tmp-x"
        d.chmod(0o600)
        self.addCleanup(d.chmod, 0o700)
        r = self.ap2("--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual((out["today"], out["unknown"]), (0.0, False))  # launched a minute ago: in its grace

    def test_spend_json_with_a_sum_too_big_for_a_number_is_valid_json(self):
        self.session()
        line = dict(NEW, requestId="req_d", timestamp=(datetime.now(self.tz) - timedelta(seconds=4)).isoformat())
        with open(self.projects / "-tmp-x" / f"{SID}.jsonl", "a") as f:
            f.write(json.dumps(line) + "\n")
        with mock.patch.object(spend, "_cost", return_value=sys.float_info.max):
            code, out, err = self.main("--json")
        self.assertEqual((code, err), (0, ""))
        t = json.loads(out, parse_constant=_no_constant)
        self.assertEqual(max(t["today"], t["week"], t["month"]), sys.float_info.max)  # local time: one of them
        self.assertIs(t["unknown"], True)

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
