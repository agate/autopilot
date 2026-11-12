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

    def launch(self, short=SHORT, p=None):
        spend.record_launch(short, "worker", "checkout", p or profile())


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
        self.launch()
        t = spend.totals(NOW)
        # 2026-11-10 (Tue) is this week and month; 2026-11-11 is today. Haiku 4.5 input is $1/Mtok, 1M tokens each.
        self.assertAlmostEqual(t["today"], 1.0)
        self.assertAlmostEqual(t["week"], 2.0)
        self.assertAlmostEqual(t["month"], 2.0)
        self.assertFalse(t["unknown"])

    def test_week_starts_monday_and_month_is_calendar(self):
        self.put()
        self.launch()
        t = spend.totals(datetime(2026, 11, 16, 9, 0, tzinfo=TZ))  # next Monday
        self.assertEqual(t["week"], 0.0)
        self.assertAlmostEqual(t["month"], STREAMING_USD)
        self.assertEqual(spend.totals(datetime(2026, 12, 1, 9, 0, tzinfo=TZ))["month"], 0.0)

    def test_unknown_model_marks_unknown(self):
        self.put("unknown.jsonl")
        self.launch()
        t = spend.totals(NOW)
        self.assertIs(t["unknown"], True)
        self.assertEqual(t["month"], 0.0)

    def test_subscription_launches_excluded(self):
        self.put()
        self.launch(p=profile("sub", "subscription"))
        self.assertEqual(spend.totals(NOW, mode="gateway")["month"], 0.0)
        self.assertAlmostEqual(spend.totals(NOW, mode="subscription")["month"], STREAMING_USD)

    def test_switching_gateway_profiles_sums(self):
        self.put()
        self.launch(p=profile("g1"))
        other = self.home / ".claude" / "projects" / "-tmp-x" / "22222222-0000-0000-0000-00000000bbbb.jsonl"
        shutil.copy(FIX / "days.jsonl", other)
        self.listing.append({"id": "efgh5678", "sessionId": "22222222-0000-0000-0000-00000000bbbb"})
        self.launch("efgh5678", profile("g2"))
        self.assertAlmostEqual(spend.totals(NOW)["month"], STREAMING_USD + 2.0)

    def test_resumed_session_counts_once(self):
        self.put()
        self.launch()
        self.listing.append({"id": "zzzz9999", "sessionId": SID})
        self.launch("zzzz9999")
        self.assertAlmostEqual(spend.totals(NOW)["month"], STREAMING_USD)

    def test_unresolved_short_id_is_unknown_then_found(self):
        self.put()
        self.listing.clear()
        self.launch()
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
        with mock.patch("autopilot.sessions.list_all", return_value=[]) as la:
            spend.totals(NOW)
            self.clock += 300
            spend.totals(NOW)
            self.assertEqual(la.call_count, 0)  # tried at launch time, next try 10 minutes later
            self.clock += 400
            spend.totals(NOW)
            self.assertEqual(la.call_count, 1)
            self.clock += 3 * 86400
            spend.totals(NOW)
            spend.totals(NOW)
            self.assertEqual(la.call_count, 1)  # the launch is older than 2 days: no more tries
            self.assertIs(spend.totals(NOW)["unknown"], True)

    def test_unresolved_launch_from_earlier_month_is_not_unknown(self):
        self.listing.clear()
        self.launch()
        self.assertIs(spend.totals(datetime(2026, 12, 2, 9, 0, tzinfo=TZ))["unknown"], False)

    def test_missing_transcript_without_settled_lines_is_unknown(self):
        self.launch()
        self.assertIs(spend.totals(NOW)["unknown"], True)

    def test_cache_holds_per_day_aggregates_of_this_month_only(self):
        self.put("days.jsonl")
        self.launch()
        spend.totals(NOW)
        ent = list(json.loads((self.data / "spend" / "cache.json").read_text())["files"].values())[0]
        self.assertEqual(sorted(ent["days"]), ["2026-11-10", "2026-11-11"])
        self.assertEqual(sorted(ent["days"]["2026-11-10"]), ["unknown", "usd"])
        spend.totals(datetime(2026, 11, 11, 12, 0, tzinfo=TZ))
        # the same file read in December keeps no November day
        self.clock += 20 * 86400
        spend.totals(datetime(2026, 12, 1, 12, 0, tzinfo=TZ))
        ent = list(json.loads((self.data / "spend" / "cache.json").read_text())["files"].values())[0]
        self.assertEqual(ent["days"], {})

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
        self.launch()
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
        self.launch()
        spend.settle(SHORT)
        self.transcript.unlink()
        self.assertIs(spend.totals(NOW)["unknown"], True)

    def test_settle_without_transcript_records_unsettled_and_stays_unknown(self):
        self.listing.clear()
        self.launch()
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

    def test_record_launch_rejects_unknown_kind(self):
        with self.assertRaises(ValueError):
            spend.record_launch(SHORT, "bogus", None, profile())


if __name__ == "__main__":
    unittest.main()
