"""What the end of a worker's transcript means (`apistop.py`): the API errors after its last normal reply, whether
the last one is a usage limit or a gateway's safety-guard refusal, the reset time a limit message names, and the
back-off for a limit with no readable reset. Every test reads temp folders only."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests import helpers  # noqa: F401 - puts lib/ on the path
from assistant import apistop

SID = "11111111-0000-4000-8000-00000000aaaa"
# Lesson 4.1's line as the transcript holds it (verbatim), with the line's type and time added.
LIMIT_TEXT = "You've hit your individual spend limit · ... · your 5-hour limit resets 8:40pm (America/Los_Angeles)"
LIMIT_LINE = ('{"isApiErrorMessage": true, "error": "rate_limit", "apiErrorStatus": 429, "message": {"model": '
              '"<synthetic>", "content": [{"text": "You\'ve hit your individual spend limit · ... · your 5-hour limit '
              'resets 8:40pm (America/Los_Angeles)"}]}}')
# Lesson 4.3's text (verbatim): `apiErrorStatus: 400`, `API Error: 400 Request blocked by safety guard`.
REFUSED_TEXT = "API Error: 400 Request blocked by safety guard"
ERR_AT = "2026-11-12T02:32:42.322Z"
SINCE = datetime(2026, 11, 12, 1, 0, tzinfo=timezone.utc)


def limit_line(at=ERR_AT, **over) -> dict:
    return dict(json.loads(LIMIT_LINE), type="assistant", timestamp=at, **over)


def refused_line(at=ERR_AT, **over) -> dict:
    return dict({"type": "assistant", "error": "unknown", "apiErrorStatus": 400,
                 "message": {"model": "<synthetic>", "content": [{"type": "text", "text": REFUSED_TEXT}]},
                 "timestamp": at}, **over)


def reply(at="2026-11-12T02:00:00Z", text="working on it") -> dict:
    return {"type": "assistant", "message": {"model": "claude-opus-5-5", "content": [{"type": "text", "text": text}]},
            "timestamp": at}


def error_line(text, at=ERR_AT, **over) -> dict:
    """An API error line with `text` and no status or error code unless given."""
    return dict({"type": "assistant", "isApiErrorMessage": True,
                 "message": {"model": "<synthetic>", "content": [{"type": "text", "text": text}]}, "timestamp": at},
                **over)


class Case(unittest.TestCase):
    def setUp(self):
        self.projects = Path(tempfile.mkdtemp(prefix="ap2-apistop-")).resolve()
        self.addCleanup(shutil.rmtree, self.projects, True)
        self.folder = self.projects / "-tmp-worktree"
        self.folder.mkdir()

    def transcript(self, lines, sid=SID) -> Path:
        p = self.folder / f"{sid}.jsonl"
        p.write_text("".join((x if isinstance(x, str) else json.dumps(x)) + "\n" for x in lines), encoding="utf-8")
        return p

    def tail(self, sid=SID):
        return apistop.tail(self.projects, sid)


class TailTest(Case):
    def test_lesson_4_1s_limit_line_is_an_error_after_the_last_reply(self):
        self.transcript([{"type": "user", "message": {"content": "go"}}, reply(), LIMIT_LINE[:-1] +
                         f', "type": "assistant", "timestamp": "{ERR_AT}"}}'])
        t = self.tail()
        self.assertEqual(len(t["errors"]), 1)
        e = t["errors"][0]
        self.assertEqual((e["status"], e["error"], e["text"]), (429, "rate_limit", LIMIT_TEXT))
        self.assertEqual(e["at"], datetime(2026, 11, 12, 2, 32, 42, 322000, tzinfo=timezone.utc))
        self.assertEqual(t["reply_at"], datetime(2026, 11, 12, 2, 0, tzinfo=timezone.utc))

    def test_lesson_4_3s_refusal_is_an_error(self):
        self.transcript([refused_line()])
        e = self.tail()["errors"][0]
        self.assertEqual((e["status"], e["error"], e["text"]), (400, "unknown", REFUSED_TEXT))
        self.assertIsNone(self.tail()["reply_at"])

    def test_any_one_of_the_three_marks_makes_an_error_line(self):
        plain = {"type": "assistant", "message": {"model": "m", "content": [{"type": "text", "text": "x"}]},
                 "timestamp": ERR_AT}
        for mark in ({"isApiErrorMessage": True}, {"error": "rate_limit"},
                     {"message": {"model": "<synthetic>", "content": [{"type": "text", "text": "x"}]}}):
            self.transcript([dict(plain, **mark)])
            self.assertEqual(len(self.tail()["errors"]), 1, mark)
        for not_one in ({"isApiErrorMessage": "yes"}, {"error": ""}, {"error": 5}):
            self.transcript([dict(plain, **not_one)])
            self.assertEqual(self.tail()["errors"], [], not_one)

    def test_a_system_line_can_be_an_error_and_other_types_are_skipped(self):
        self.transcript([dict(refused_line(), type="system"), dict(limit_line(), type="user")])
        self.assertEqual([e["status"] for e in self.tail()["errors"]], [400])

    def test_errors_are_oldest_first(self):
        self.transcript([reply(), refused_line("2026-11-12T02:10:00Z"), limit_line()])
        self.assertEqual([e["status"] for e in self.tail()["errors"]], [400, 429])

    def test_the_status_is_an_int_and_never_a_bool(self):
        for raw, want in ((429, 429), (True, None), ("429", None), (None, None)):
            self.transcript([limit_line(apiErrorStatus=raw)])
            self.assertEqual(self.tail()["errors"][0]["status"], want, raw)

    def test_the_text_joins_the_content_text_parts(self):
        self.transcript([error_line("", message={"model": "<synthetic>", "content": [
            {"type": "text", "text": "API Error:"}, {"type": "image"}, "not a part", {"type": "text", "text": "429"}]})])
        self.assertEqual(self.tail()["errors"][0]["text"], "API Error: 429")
        self.transcript([error_line("", message={"model": "<synthetic>", "content": "API Error: 500"})])
        self.assertEqual(self.tail()["errors"][0]["text"], "API Error: 500")

    def test_a_recovered_transcript_has_no_errors(self):
        self.transcript([limit_line(), reply("2026-11-12T03:00:00Z")])
        t = self.tail()
        self.assertEqual(t["errors"], [])
        self.assertEqual(t["reply_at"], datetime(2026, 11, 12, 3, 0, tzinfo=timezone.utc))

    def test_only_the_last_64_kib_are_read_and_the_cut_line_is_dropped(self):
        filler = reply(text="x" * 500)
        big = [reply("2026-11-12T01:30:00Z")] + [filler] * 300 + [limit_line()]
        p = self.transcript(big)
        self.assertGreater(p.stat().st_size, apistop.TAIL_BYTES)
        self.assertEqual(apistop.TAIL_BYTES, 65536)
        t = self.tail()
        self.assertEqual([e["status"] for e in t["errors"]], [429])
        # A line longer than the tail: the cut one is dropped, not read as garbage or as a reply.
        self.transcript([reply(text="y" * (apistop.TAIL_BYTES + 10)), limit_line()])
        t = self.tail()
        self.assertEqual(([e["status"] for e in t["errors"]], t["reply_at"]), ([429], None))

    def test_a_file_that_fits_keeps_its_first_line(self):
        self.transcript([limit_line()])
        self.assertEqual(len(self.tail()["errors"]), 1)

    def test_a_garbage_line_is_skipped(self):
        self.transcript(["not json {", "[1, 2]", "7", limit_line()])
        self.assertEqual(len(self.tail()["errors"]), 1)

    def test_a_missing_or_unreadable_transcript_is_none(self):
        self.assertIsNone(self.tail())
        self.assertIsNone(apistop.tail(self.projects, ""))
        self.assertIsNone(apistop.tail(self.projects, None))
        self.assertIsNone(apistop.tail(self.projects, "../escape"))
        self.assertIsNone(apistop.tail(self.projects / "nowhere", SID))
        self.assertIsNone(apistop.tail(None, SID))  # no projects folder at all: None, never a TypeError
        p = self.transcript([limit_line()])
        p.chmod(0)
        self.addCleanup(p.chmod, 0o600)
        try:
            p.read_bytes()
        except OSError:
            self.assertIsNone(self.tail())  # not when running as root, which reads it anyway

    def test_a_line_with_a_naive_or_bad_time(self):
        self.transcript([limit_line(at="nonsense")])
        self.assertIsNone(self.tail()["errors"][0]["at"])
        self.transcript([limit_line(at=None)])
        self.assertIsNone(self.tail()["errors"][0]["at"])
        self.transcript([limit_line(at="2026-11-12T02:32:42")])
        self.assertIsNotNone(self.tail()["errors"][0]["at"].tzinfo)


class StopTest(Case):
    def stop(self, lines, since=SINCE):
        self.transcript(lines)
        return apistop.stop(self.tail(), since)

    def test_kinds(self):
        for lines, kind in (([limit_line()], "limit"),
                            ([refused_line()], "gateway_refused"),
                            ([error_line("Rate limited")], None),
                            ([error_line("Usage limit reached, resets 9pm (UTC)", error="unknown")], "limit"),
                            ([error_line("x", error="rate_limit")], "limit"),
                            ([error_line("x", apiErrorStatus=429)], "limit"),
                            ([error_line("API Error: 500 overloaded", apiErrorStatus=500)], None),
                            ([error_line("API Error: 401 Invalid API key", error="authentication_failed",
                                         apiErrorStatus=401)], None),
                            ([error_line(REFUSED_TEXT)], "gateway_refused"),  # 400 in the text only
                            ([error_line("Request blocked by safety guard", apiErrorStatus=400)], "gateway_refused"),
                            ([error_line("Request blocked by safety guard")], None),  # no 400 anywhere
                            ([error_line("API Error: 400 bad request", apiErrorStatus=400)], None),
                            ([limit_line(), reply("2026-11-12T03:00:00Z")], None)):
            got = self.stop(lines)
            self.assertEqual((got or {}).get("kind"), kind, lines)

    def test_the_stop_names_its_text_and_time(self):
        got = self.stop([limit_line()])
        self.assertEqual(got, {"kind": "limit", "text": LIMIT_TEXT,
                               "at": datetime(2026, 11, 12, 2, 32, 42, 322000, tzinfo=timezone.utc)})

    def test_only_the_last_error_counts(self):
        self.assertEqual(self.stop([limit_line("2026-11-12T02:00:00Z"), refused_line()])["kind"], "gateway_refused")
        self.assertIsNone(self.stop([limit_line("2026-11-12T02:00:00Z"), error_line("API Error: 500")]))

    def test_an_error_from_before_the_run_is_history(self):
        # A resumed copy repeats the old conversation, its last error included, with the old times.
        late = datetime(2026, 11, 12, 4, 0, tzinfo=timezone.utc)
        self.assertIsNone(self.stop([limit_line()], late))
        self.assertIsNone(self.stop([limit_line(at=None)], SINCE))  # no time: cannot be told apart from history
        at = datetime(2026, 11, 12, 2, 32, 42, 322000, tzinfo=timezone.utc)
        self.assertEqual(self.stop([limit_line()], at)["kind"], "limit")  # at `since` counts

    def test_no_tail_or_no_errors(self):
        self.assertIsNone(apistop.stop(None, SINCE))
        self.assertIsNone(apistop.stop({"errors": [], "reply_at": None}, SINCE))
        self.assertIsNone(apistop.stop({}, SINCE))


class ResetTimeTest(unittest.TestCase):
    LA = "America/Los_Angeles"

    @staticmethod
    def at(iso):
        return datetime.fromisoformat(iso)

    def test_lesson_4_1s_reset_is_the_next_wall_time_in_the_named_zone(self):
        got = apistop.reset_time(LIMIT_TEXT, self.at("2026-11-12T02:32:42+00:00"))
        self.assertEqual(got, self.at("2026-11-12T04:40:00+00:00"))  # 20:40 PST the same evening
        self.assertEqual(str(got.tzinfo), self.LA)

    def test_a_time_already_past_that_day_is_the_next_day_across_midnight(self):
        got = apistop.reset_time(LIMIT_TEXT, self.at("2026-11-12T05:00:00+00:00"))  # 21:00 PST
        self.assertEqual(got, self.at("2026-11-13T04:40:00+00:00"))
        got = apistop.reset_time("resets 12:15am (UTC)", self.at("2026-12-31T23:50:00+00:00"))
        self.assertEqual(got, self.at("2027-01-01T00:15:00+00:00"))

    def test_forms(self):
        base = self.at("2026-11-12T02:32:42+00:00")
        for txt, want in (("resets 9pm (America/Los_Angeles)", "2026-11-12T05:00:00+00:00"),
                          ("resets 12am (America/Los_Angeles)", "2026-11-12T08:00:00+00:00"),
                          ("resets 12:15pm (UTC)", "2026-11-12T12:15:00+00:00"),
                          ("resets 21:30 (Europe/Berlin)", "2026-11-12T20:30:00+00:00"),
                          ("resets Nov 14, 9am (UTC)", "2026-11-14T09:00:00+00:00"),
                          ("your weekly limit resets Nov 14, 9am (America/Los_Angeles)", "2026-11-14T17:00:00+00:00"),
                          ("resets November 14th at 9:30 a.m. (UTC)", "2026-11-14T09:30:00+00:00"),
                          ("resets Jan 2 at 9am (America/Los_Angeles)", "2027-01-02T17:00:00+00:00")):
            self.assertEqual(apistop.reset_time(txt, base), self.at(want), txt)

    def test_a_dated_reset_across_the_year_end(self):
        base = self.at("2026-12-31T20:00:00+00:00")
        self.assertEqual(apistop.reset_time("resets Jan 2, 9am (UTC)", base), self.at("2027-01-02T09:00:00+00:00"))
        self.assertEqual(apistop.reset_time("resets Dec 31, 9pm (UTC)", base), self.at("2026-12-31T21:00:00+00:00"))

    def test_unreadable_is_none(self):
        base = self.at("2026-11-12T02:32:42+00:00")
        for txt in ("resets soon", "resets 8:40pm", "resets 8:40pm (Mars/Olympus)", "resets 25:00 (UTC)",
                    "resets 13pm (UTC)", "resets 0am (UTC)", "resets 9:61 (UTC)", "resets Foo 3, 9am (UTC)",
                    "resets Feb 30, 9am (UTC)", "resets 9am (../../etc/passwd)", "", None):
            self.assertIsNone(apistop.reset_time(txt, base), txt)


class BackoffTest(unittest.TestCase):
    def test_doubling_from_30_minutes_to_4_hours(self):
        self.assertEqual([apistop.backoff(n) / timedelta(minutes=1) for n in range(1, 7)],
                         [30, 60, 120, 240, 240, 240])

    def test_odd_counts(self):
        self.assertEqual(apistop.backoff(0), timedelta(minutes=30))
        self.assertEqual(apistop.backoff(-3), timedelta(minutes=30))
        self.assertEqual(apistop.backoff(10 ** 6), timedelta(hours=4))  # no overflow


if __name__ == "__main__":
    unittest.main()
