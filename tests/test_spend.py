from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.helpers import TempData, ROOT
from autopilot import config, spend

FIX = ROOT / "tests" / "fixtures" / "spend"
TZ = timezone(timedelta(hours=-5))
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=TZ)
SID = "11111111-0000-0000-0000-00000000aaaa"
SHORT = "abcd1234"
BEFORE = datetime(2026, 11, 10, 8, 0, tzinfo=TZ)   # before every request in the fixtures
HAIKU = json.loads((ROOT / "lib" / "autopilot" / "prices.json").read_text())["models"]["claude-haiku-4-5"]


def usd(**tok):
    return sum(tok.get(k, 0) * HAIKU[k] for k in HAIKU if k != "over_100k") / 1e6


STREAMING_USD = usd(input=1000, output=2000, cache_read=10000, cache_write_5m=3000, cache_write_1h=1000)


def profile(name="g1", mode="gateway"):
    return {"name": name, "mode": mode}


class SpendCase(unittest.TestCase):
    def setUp(self):
        self.td = TempData()
        self.data = self.td.__enter__()
        self.addCleanup(self.td.__exit__, None, None, None)
        self.home = Path(tempfile.mkdtemp(prefix="ap-home-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.listing = [{"id": SHORT, "sessionId": SID, "kind": "background"}]
        p = mock.patch("autopilot.sessions.list_all", side_effect=lambda *_: self.listing)
        p.start()
        self.addCleanup(p.stop)
        self.clock = datetime(2026, 11, 11, 8, 0, tzinfo=TZ).timestamp()
        for target, val in (("autopilot.spend._clock", lambda: self.clock),
                            ("autopilot.spend._now_iso", lambda: datetime.fromtimestamp(self.clock, TZ).isoformat())):
            q = mock.patch(target, side_effect=val)
            q.start()
            self.addCleanup(q.stop)
        self.transcript = self.home / ".claude" / "projects" / "-tmp-x" / f"{SID}.jsonl"

    def put(self, fixture="streaming.jsonl", sub=None):
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIX / fixture, self.transcript)
        if sub:
            d = self.transcript.parent / SID / "subagents"
            d.mkdir(parents=True, exist_ok=True)
            shutil.copy(FIX / sub, d / "agent-1.jsonl")

    def launch(self, short=SHORT, p=None, at: datetime = None):
        """Record a launch now, or at `at` (a session's requests come after its launch)."""
        if at is None:
            spend.record_launch(short, "worker", "checkout", p or profile())
            return
        now, self.clock = self.clock, at.timestamp()
        spend.record_launch(short, "worker", "checkout", p or profile())
        self.clock = now

    def past_grace(self):
        self.clock += spend._NEW_GRACE_S + 1


class RequestCostsTest(SpendCase):
    def test_streaming_duplicates_count_once(self):
        self.put()
        items = spend.request_costs(self.transcript, tz=TZ)
        self.assertEqual(len(items), 1)
        self.assertAlmostEqual(items[0]["usd"], STREAMING_USD)
        self.assertEqual(items[0]["day"], "2026-11-10")
        self.assertEqual(items[0]["model"], "claude-haiku-4-5")

    def test_subagent_transcripts_included(self):
        self.put(sub="subagent.jsonl")
        items = spend.request_costs(self.transcript, tz=TZ)
        self.assertEqual(len(items), 2)
        self.assertAlmostEqual(sum(i["usd"] for i in items), STREAMING_USD + usd(input=500, output=500))

    def test_local_day_bucketing(self):
        self.put("days.jsonl")
        self.assertEqual([i["day"] for i in spend.request_costs(self.transcript, tz=TZ)], ["2026-11-10", "2026-11-11"])

    def test_unknown_model_has_no_cost(self):
        self.put("unknown.jsonl")
        self.assertIsNone(spend.request_costs(self.transcript, tz=TZ)[0]["usd"])

    def test_config_prices_override(self):
        self.put("unknown.jsonl")
        g = config.load_global()
        g["prices"] = {"claude-nonesuch-9": {"input": 1, "output": 1, "cache_read": 0, "cache_write_5m": 0, "cache_write_1h": 0}}
        config.save_global(g)
        self.assertAlmostEqual(spend.request_costs(self.transcript, tz=TZ)[0]["usd"], 0.002)

    def test_long_prompt_tier(self):
        p = self.transcript.parent
        p.mkdir(parents=True)
        self.transcript.write_text(json.dumps({"type": "assistant", "requestId": "r", "timestamp": "2026-11-10T15:00:00Z",
            "message": {"model": "claude-haiku-5-5", "usage": {"input_tokens": 200000, "output_tokens": 0}}}) + "\n")
        self.assertAlmostEqual(spend.request_costs(self.transcript, tz=TZ)[0]["usd"], 0.2 * 0.5)


class TotalsTest(SpendCase):
    def test_totals_windows(self):
        self.put("days.jsonl")
        self.launch(at=BEFORE)
        t = spend.totals(NOW)
        # 2026-11-10 (Tue) is this week and month; 2026-11-11 is today. Haiku 4.5 input is $1/Mtok, 1M tokens each.
        self.assertAlmostEqual(t["today"], 1.0)
        self.assertAlmostEqual(t["week"], 2.0)
        self.assertAlmostEqual(t["month"], 2.0)
        self.assertFalse(t["unknown"])

    def test_week_starts_monday_and_month_is_calendar(self):
        self.put()
        self.launch(at=BEFORE)
        t = spend.totals(datetime(2026, 11, 16, 9, 0, tzinfo=TZ))  # next Monday
        self.assertEqual(t["week"], 0.0)
        self.assertAlmostEqual(t["month"], STREAMING_USD)
        self.assertEqual(spend.totals(datetime(2026, 12, 1, 9, 0, tzinfo=TZ))["month"], 0.0)

    def test_unknown_model_marks_unknown(self):
        self.put("unknown.jsonl")
        self.launch(at=BEFORE)
        t = spend.totals(NOW)
        self.assertIs(t["unknown"], True)
        self.assertEqual(t["month"], 0.0)

    def test_subscription_launches_excluded(self):
        self.put()
        self.launch(p=profile("sub", "subscription"), at=BEFORE)
        self.assertEqual(spend.totals(NOW, mode="gateway")["month"], 0.0)
        self.assertAlmostEqual(spend.totals(NOW, mode="subscription")["month"], STREAMING_USD)

    def test_switching_gateway_profiles_sums(self):
        self.put()
        self.launch(p=profile("g1"), at=BEFORE)
        other = self.home / ".claude" / "projects" / "-tmp-x" / "22222222-0000-0000-0000-00000000bbbb.jsonl"
        shutil.copy(FIX / "days.jsonl", other)
        self.listing.append({"id": "efgh5678", "sessionId": "22222222-0000-0000-0000-00000000bbbb"})
        self.launch("efgh5678", profile("g2"), at=BEFORE)
        self.assertAlmostEqual(spend.totals(NOW)["month"], STREAMING_USD + 2.0)

    def test_resumed_session_counts_once(self):
        self.put()
        self.launch(at=BEFORE)
        self.listing.append({"id": "zzzz9999", "sessionId": SID})
        self.launch("zzzz9999", at=BEFORE)
        self.assertAlmostEqual(spend.totals(NOW)["month"], STREAMING_USD)

    def test_unresolved_short_id_is_unknown_then_found(self):
        self.put()
        self.listing.clear()
        self.launch(at=BEFORE)
        t = spend.totals(NOW)
        self.assertEqual(t["month"], 0.0)
        self.assertIs(t["unknown"], True)
        self.listing.append({"id": SHORT, "sessionId": SID})
        self.clock += 601
        t = spend.totals(NOW)
        self.assertAlmostEqual(t["month"], STREAMING_USD)
        self.assertIs(t["unknown"], False)

    def test_launch_time_resolution(self):
        self.put()
        self.launch()
        cache = json.loads((self.data / "spend" / "cache.json").read_text())
        self.assertEqual(cache["shorts"], {SHORT: SID})

    def test_unresolved_retry_is_rate_limited_and_gives_up(self):
        self.listing.clear()
        self.launch()
        self.past_grace()
        with mock.patch("autopilot.sessions.list_all", return_value=[]) as la:
            spend.totals(NOW)
            self.assertEqual(la.call_count, 1)
            self.clock += 300
            spend.totals(NOW)
            self.assertEqual(la.call_count, 1)  # past its first minutes: the next try 10 minutes later
            self.clock += 400
            spend.totals(NOW)
            self.assertEqual(la.call_count, 2)
            self.clock += 3 * 86400
            spend.totals(NOW)
            spend.totals(NOW)
            self.assertEqual(la.call_count, 2)  # the launch is older than 2 days: no more tries
            self.assertIs(spend.totals(NOW)["unknown"], True)

    def test_unresolved_launch_from_earlier_month_is_not_unknown(self):
        self.listing.clear()
        self.launch()
        self.assertIs(spend.totals(datetime(2026, 12, 2, 9, 0, tzinfo=TZ))["unknown"], False)

    def test_missing_transcript_without_settled_lines_is_unknown(self):
        self.launch()
        self.past_grace()
        self.assertIs(spend.totals(NOW)["unknown"], True)

    def test_unknown_today_only_counts_what_is_dated_today(self):
        self.listing.clear()
        self.launch()                                            # an unresolved launch made today
        self.past_grace()
        t = spend.totals(NOW)
        self.assertIs(t["unknown_today"], True)

    def test_unresolved_launch_from_yesterday_is_unknown_but_not_unknown_today(self):
        self.listing.clear()
        self.clock = datetime(2026, 11, 10, 8, 0, tzinfo=TZ).timestamp()
        self.launch()
        self.past_grace()
        t = spend.totals(NOW)
        self.assertIs(t["unknown"], True)
        self.assertIs(t["unknown_today"], False)

    def test_missing_transcript_unknown_today_follows_the_launch_day(self):
        self.clock = datetime(2026, 11, 10, 8, 0, tzinfo=TZ).timestamp()
        self.launch()
        self.past_grace()
        t = spend.totals(NOW)
        self.assertEqual((t["unknown"], t["unknown_today"]), (True, False))
        self.launch("efgh5678")
        self.listing.append({"id": "efgh5678", "sessionId": "22222222-0000-0000-0000-00000000bbbb"})
        self.clock = datetime(2026, 11, 11, 8, 0, tzinfo=TZ).timestamp()
        self.launch("efgh5678")
        self.past_grace()
        self.assertIs(spend.totals(NOW)["unknown_today"], True)

    def test_cache_holds_the_files_requests_of_this_month_only(self):
        self.put("days.jsonl")
        self.launch()
        spend.totals(NOW)
        ent = list(json.loads((self.data / "spend" / "cache.json").read_text())["files"].values())[0]
        self.assertEqual(sorted(ent["reqs"]), ["req_a", "req_b"])   # the request ids dedupe resumed copies
        spend.totals(datetime(2026, 11, 11, 12, 0, tzinfo=TZ))
        # the same file read in December keeps no November request
        self.clock += 20 * 86400
        spend.totals(datetime(2026, 12, 1, 12, 0, tzinfo=TZ))
        ent = list(json.loads((self.data / "spend" / "cache.json").read_text())["files"].values())[0]
        self.assertEqual(ent["reqs"], {})

    def test_synthetic_lines_are_ignored(self):
        self.put("synthetic.jsonl")
        self.launch()
        self.assertEqual(spend.request_costs(self.transcript, tz=TZ)[0]["model"], "claude-haiku-4-5")
        self.assertEqual(len(spend.request_costs(self.transcript, tz=TZ)), 1)
        self.assertIs(spend.totals(NOW)["unknown"], False)

    def test_incomplete_price_override_is_unknown(self):
        self.put()
        for bad in ({"claude-haiku-4-5": {"input": 1, "output": 1}}, {"claude-haiku-4-5": "free"}):
            g = config.load_global()
            g["prices"] = bad
            config.save_global(g)
            self.assertIsNone(spend.request_costs(self.transcript, tz=TZ)[0]["usd"])

    def test_lines_without_request_id_dedupe_on_message_id(self):
        d = self.transcript.parent
        d.mkdir(parents=True)

        def ln(out):
            return json.dumps({"type": "assistant", "timestamp": "2026-11-10T15:00:00Z", "message": {
                "id": "msg_1", "model": "claude-haiku-4-5", "usage": {"input_tokens": 10, "output_tokens": out}}})
        self.transcript.write_text(ln(5) + "\n" + ln(50) + "\n")
        items = spend.request_costs(self.transcript, tz=TZ)
        self.assertEqual(len(items), 1)
        self.assertAlmostEqual(items[0]["usd"], usd(input=10, output=50))

    def test_cache_rereads_only_changed_files(self):
        self.put()
        self.launch(at=BEFORE)
        spend.totals(NOW)
        with mock.patch("autopilot.spend._parse_file", side_effect=AssertionError("re-read")):
            self.assertAlmostEqual(spend.totals(NOW)["month"], STREAMING_USD)
        with self.transcript.open("a") as f:
            f.write(json.dumps({"type": "assistant", "requestId": "r9", "timestamp": "2026-11-10T16:00:00Z",
                                "message": {"model": "claude-haiku-4-5", "usage": {"input_tokens": 1000000, "output_tokens": 0}}}) + "\n")
        self.assertAlmostEqual(spend.totals(NOW)["month"], STREAMING_USD + 1.0)

    def test_settled_spend_survives_transcript_removal(self):
        self.put(sub="subagent.jsonl")
        self.launch()
        before = spend.totals(NOW)
        spend.settle(SHORT)
        self.transcript.unlink()
        shutil.rmtree(self.transcript.parent / SID)
        after = spend.totals(NOW)
        self.assertAlmostEqual(after["month"], before["month"])
        self.assertAlmostEqual(after["week"], before["week"])
        self.assertTrue((self.data / "spend" / "settled.jsonl").exists())

    def test_settle_keeps_unknown_flag(self):
        self.put("unknown.jsonl")
        self.launch(at=BEFORE)
        spend.settle(SHORT)
        self.transcript.unlink()
        self.assertIs(spend.totals(NOW)["unknown"], True)

    def test_settle_without_transcript_records_unsettled_and_stays_unknown(self):
        self.listing.clear()
        self.launch()
        self.past_grace()
        spend.settle(SHORT)
        line = json.loads((self.data / "spend" / "settled.jsonl").read_text().splitlines()[0])
        self.assertIs(line["unsettled"], True)
        self.assertEqual(line["short"], SHORT)
        self.assertIs(spend.totals(NOW)["unknown"], True)

    def test_record_launch_line(self):
        self.launch()
        line = json.loads((self.data / "spend" / "launches.jsonl").read_text().splitlines()[0])
        self.assertEqual({k: line[k] for k in ("short", "kind", "ap", "profile", "mode")},
                         {"short": SHORT, "kind": "worker", "ap": "checkout", "profile": "g1", "mode": "gateway"})
        self.assertTrue(line["at"])

    def test_record_session_prices_a_p_run_by_session_id(self):
        self.put()
        spend.record_session(SID, "discover", "checkout", profile())
        line = json.loads((self.data / "spend" / "launches.jsonl").read_text().splitlines()[0])
        self.assertEqual((line["short"], line["kind"]), (SID, "discover"))
        self.listing = []
        self.transcript.unlink()   # settled lines outlive the transcript
        t = spend.totals(NOW)
        self.assertAlmostEqual(t["month"], STREAMING_USD)
        self.assertFalse(t["unknown"])

    def test_record_session_without_an_id_is_unknown_spend(self):
        spend.record_session("", "oneoff", "checkout", profile())
        self.assertIs(spend.totals(NOW)["unknown"], True)

    def copy(self, sid, extra=()):
        """A resumed copy: a new session whose transcript repeats the original's requests, then its own."""
        f = self.transcript.parent / f"{sid}.jsonl"
        lines = (FIX / "days.jsonl").read_text().splitlines() + [json.dumps(x) for x in extra]
        f.write_text("\n".join(lines) + "\n")
        return f

    NEW = {"type": "assistant", "requestId": "req_c", "timestamp": "2026-11-11T15:00:00Z",
           "message": {"model": "claude-haiku-4-5", "usage": {"input_tokens": 1000000, "output_tokens": 0}}}
    SID2 = "22222222-0000-0000-0000-00000000cccc"

    def test_resumed_copy_counts_shared_requests_once(self):
        self.put("days.jsonl")
        self.launch(at=BEFORE)
        self.copy(self.SID2, [self.NEW])
        self.listing.append({"id": "zzzz9999", "sessionId": self.SID2})
        self.clock += 60
        self.launch("zzzz9999")
        t = spend.totals(NOW)
        self.assertAlmostEqual(t["month"], 3.0)    # req_a + req_b once, req_c once
        self.assertAlmostEqual(t["today"], 2.0)

    def test_subscription_history_resumed_under_a_gateway_stays_subscription(self):
        self.put("days.jsonl")
        self.launch(p=profile("sub", "subscription"), at=BEFORE)
        self.copy(self.SID2, [self.NEW])
        self.listing.append({"id": "zzzz9999", "sessionId": self.SID2})
        self.clock += 60
        self.launch("zzzz9999", profile("g1"))
        self.assertAlmostEqual(spend.totals(NOW, mode="gateway")["month"], 1.0)
        self.assertAlmostEqual(spend.totals(NOW, mode="subscription")["month"], 2.0)

    def test_dedupe_survives_the_original_being_settled_and_removed(self):
        self.put("days.jsonl")
        self.launch(at=BEFORE)
        self.copy(self.SID2, [self.NEW])
        self.listing.append({"id": "zzzz9999", "sessionId": self.SID2})
        self.clock += 60
        self.launch("zzzz9999")
        spend.settle(SHORT)
        self.transcript.unlink()
        self.assertAlmostEqual(spend.totals(NOW)["month"], 3.0)

    def test_a_line_without_a_usable_timestamp_is_unknown_not_dropped(self):
        d = self.transcript.parent
        d.mkdir(parents=True)
        self.transcript.write_text(json.dumps({"type": "assistant", "requestId": "r", "timestamp": "garbage",
            "message": {"model": "claude-haiku-4-5", "usage": {"input_tokens": 10, "output_tokens": 1}}}) + "\n")
        self.launch()
        t = spend.totals(NOW)
        self.assertEqual((t["unknown"], t["unknown_today"]), (True, True))

    def test_cache_temp_file_is_per_process(self):
        seen = []
        real = os.replace
        with mock.patch("autopilot.spend.os.replace", side_effect=lambda a, b: (seen.append(str(a)), real(a, b))):
            self.launch()
        self.assertTrue(seen and all(f".{os.getpid()}." in x for x in seen), seen)

    def test_a_launch_without_a_session_id_stops_blocking_today_after_30_minutes(self):
        spend.record_session("", "oneoff", "checkout", profile())
        t = spend.totals(NOW)
        self.assertEqual((t["unknown_today"], t["unknown_launches"]), (True, 1))
        self.clock += 31 * 60
        t = spend.totals(NOW)
        self.assertEqual((t["unknown"], t["unknown_today"]), (True, False))

    def test_a_workers_launch_without_a_session_id_keeps_blocking_the_whole_day(self):
        for kind in ("worker", "dispatcher"):
            spend.record_session("", kind, "checkout", profile())
        self.clock += 5 * 3600
        t = spend.totals(NOW)
        self.assertEqual((t["unknown"], t["unknown_today"]), (True, True))

    def test_totals_name_the_unpriced_models_of_today(self):
        self.put("unknown.jsonl")
        self.launch(at=BEFORE)
        self.assertEqual(spend.totals(NOW)["unknown_models"], [])   # its request is dated yesterday
        self.assertEqual(spend.totals(datetime(2026, 11, 10, 20, 0, tzinfo=TZ))["unknown_models"], ["claude-nonesuch-9"])

    def events(self):
        f = self.data / "events-global.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def test_a_failed_record_is_logged_and_a_gateway_launch_counts_as_unknown(self):
        from autopilot import workers
        with mock.patch.object(spend, "record_launch", side_effect=OSError("disk full")):
            workers._record(SHORT, "worker", "checkout", profile())
        ev = [e for e in self.events() if e["type"] == "spend_record_failed"]
        self.assertEqual(len(ev), 1)
        self.assertIn("disk full", ev[0]["data"]["error"])
        t = spend.totals(NOW)
        self.assertEqual((t["unknown"], t["unknown_today"]), (True, True))
        self.assertEqual(spend.totals(NOW, mode="subscription")["unknown"], False)

    def test_a_failed_subscription_record_is_logged_only(self):
        from autopilot import workers
        with mock.patch.object(spend, "record_launch", side_effect=OSError("disk full")):
            workers._record(SHORT, "worker", None, profile("sub", "subscription"))
        self.assertEqual([e["type"] for e in self.events()], ["spend_record_failed"])
        self.assertFalse((self.data / "spend" / "launches.jsonl").exists())   # no fallback row: no cap to keep

    def test_a_failed_settle_is_logged(self):
        from autopilot import workers
        with mock.patch.object(spend, "settle", side_effect=ValueError("bad cache")):
            workers._settle(SHORT)
        ev = [e for e in self.events() if e["type"] == "spend_record_failed"]
        self.assertEqual((len(ev), ev[0]["data"]["op"]), (1, "settle"))

    def test_record_launch_rejects_unknown_kind(self):
        with self.assertRaises(ValueError):
            spend.record_launch(SHORT, "bogus", None, profile())


def req(rid, at: datetime, tokens=1_000_000):
    """One Haiku 4.5 request line: 1M input tokens cost $1."""
    return {"type": "assistant", "requestId": rid, "timestamp": at.astimezone(timezone.utc).isoformat(),
            "message": {"model": "claude-haiku-4-5", "usage": {"input_tokens": tokens, "output_tokens": 0}}}


class NewLaunchGraceTest(SpendCase):
    """A launch only minutes old is not unknown spend yet: the listing or its transcript may not show it."""

    def test_an_unresolved_launch_in_its_first_minutes_is_not_unknown(self):
        self.listing.clear()
        self.launch()
        t = spend.totals(NOW)
        self.assertEqual((t["unknown"], t["unknown_today"], t["unknown_launches"]), (False, False, 0))
        self.clock += spend._NEW_GRACE_S + 1
        t = spend.totals(NOW)
        self.assertEqual((t["unknown"], t["unknown_today"], t["unknown_launches"]), (True, True, 1))

    def test_a_resolved_launch_without_a_transcript_yet_is_not_unknown_in_its_first_minutes(self):
        self.launch()
        t = spend.totals(NOW)
        self.assertEqual((t["unknown"], t["unknown_today"], t["unknown_launches"]), (False, False, 0))
        self.clock += spend._NEW_GRACE_S + 1
        t = spend.totals(NOW)
        self.assertEqual((t["unknown"], t["unknown_today"], t["unknown_launches"]), (True, True, 1))

    def test_a_young_launch_is_retried_every_minute(self):
        self.listing.clear()
        self.launch()
        with mock.patch("autopilot.sessions.list_all", return_value=[]) as la:
            self.clock += 30
            spend.totals(NOW)
            self.assertEqual(la.call_count, 0)   # tried at launch time
            self.clock += 31
            spend.totals(NOW)
            self.assertEqual(la.call_count, 1)
            self.clock += 61
            spend.totals(NOW)
            self.assertEqual(la.call_count, 2)
            self.clock = self.clock - 122 + spend._NEW_GRACE_S + 1   # past the grace: every 10 minutes again
            spend.totals(NOW)
            self.assertEqual(la.call_count, 3)
            self.clock += 61
            spend.totals(NOW)
            self.assertEqual(la.call_count, 3)

    def test_a_launch_found_in_its_first_minutes_is_priced(self):
        self.listing.clear()
        self.launch()
        self.put()
        self.listing.append({"id": SHORT, "sessionId": SID})
        self.clock += 61
        self.assertEqual(spend.totals(NOW)["unknown"], False)
        self.assertEqual(json.loads((self.data / "spend" / "cache.json").read_text())["shorts"], {SHORT: SID})

    def test_a_row_without_a_session_id_gets_no_grace(self):
        spend.record_session("", "worker", "checkout", profile())
        self.assertIs(spend.totals(NOW)["unknown_today"], True)


class CopiedHistoryTest(SpendCase):
    """A resumed copy starts with the old conversation's requests, under the new session id and with their old
    times. Requests from before the session's first launch are that copy, not new spend."""

    def write(self, lines, path=None):
        path = path or self.transcript
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(x) + "\n" for x in lines))

    def at(self, minutes: float) -> datetime:
        return datetime.fromtimestamp(self.clock, TZ) + timedelta(minutes=minutes)

    def test_requests_from_before_the_first_launch_are_not_counted(self):
        self.write([req("old_1", self.at(-300)), req("old_2", self.at(-30)), req("near", self.at(-2)),
                    req("new_1", self.at(10))])
        self.launch()
        t = spend.totals(NOW)
        self.assertAlmostEqual(t["today"], 2.0)   # near (inside the skew) and new_1
        self.assertAlmostEqual(t["month"], 2.0)
        self.assertFalse(t["unknown"])

    def test_an_unpriced_copied_request_does_not_make_spend_unknown(self):
        old = req("old_1", self.at(-60))
        old["message"]["model"] = "claude-nonesuch-9"
        self.write([old, req("new_1", self.at(10))])
        self.launch()
        t = spend.totals(NOW)
        self.assertAlmostEqual(t["today"], 1.0)
        self.assertEqual((t["unknown"], t["unknown_today"], t["unknown_models"]), (False, False, []))

    def test_the_first_launch_of_the_session_decides(self):
        # The same session launched again later (resumed under its own id): its requests in between count.
        self.write([req("a", self.at(10)), req("b", self.at(70))])
        self.launch()
        self.clock += 3600
        self.listing.append({"id": "zzzz9999", "sessionId": SID})
        self.launch("zzzz9999")
        self.assertAlmostEqual(spend.totals(NOW)["today"], 2.0)

    def test_settled_requests_drop_the_copy_too(self):
        self.write([req("old_1", self.at(-60)), req("new_1", self.at(10))])
        self.launch()
        spend.settle(SHORT)
        self.transcript.unlink()
        t = spend.totals(NOW)
        self.assertAlmostEqual(t["today"], 1.0)
        self.assertFalse(t["unknown"])

    def test_a_p_run_recorded_after_it_ended_keeps_its_requests(self):
        # `claude -p` rows are written when the run ends, so its requests come before the row.
        self.write([req("p_1", self.at(-20))])
        spend.record_session(SID, "discover", "checkout", profile())
        self.assertAlmostEqual(spend.totals(NOW)["today"], 1.0)

    def test_a_copy_of_a_session_with_no_launch_row_bills_only_its_own_requests(self):
        # The original ran before launches were recorded; the copy runs on a gateway.
        sid2 = "22222222-0000-0000-0000-00000000cccc"
        self.write([req("old_1", self.at(-400)), req("old_2", self.at(-100))])
        self.write([req("old_1", self.at(-400)), req("old_2", self.at(-100)), req("new_1", self.at(5))],
                   self.transcript.parent / f"{sid2}.jsonl")
        self.listing.append({"id": "zzzz9999", "sessionId": sid2})
        self.launch("zzzz9999")
        self.assertAlmostEqual(spend.totals(NOW)["today"], 1.0)


if __name__ == "__main__":
    unittest.main()
