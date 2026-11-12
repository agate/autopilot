"""A worker whose turn ended on an API error: parked at a usage limit, or marked as refused by a gateway."""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT, TempData, write_ap
from autopilot import budget, cli, config, events, gate, park, paths, profile, sessions, snapshot, state, text, usage, workers

FAKE = str(ROOT / "tests" / "bin" / "fake-claude")
SID = "11111111-0000-0000-0000-00000000aaaa"
SHORT = "abcd1234"
NAME = "checkout · PROJ-1 · implement"
# The worker started at 01:00Z; the limit hit at 02:32Z (18:32 in Los Angeles), resetting at 20:40 there.
STARTED = "2026-11-12T01:00:00+00:00"
ERR_AT = "2026-11-12T02:32:42.322Z"
NOW = datetime(2026, 11, 12, 2, 40, tzinfo=timezone.utc)
LIMIT_TEXT = ("You've hit your individual spend limit · run /usage-credits to ask your admin for a higher limit "
              "· your 5-hour limit resets 8:40pm (America/Los_Angeles)")


def limit_line(text=LIMIT_TEXT, at=ERR_AT):
    return {"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit", "apiErrorStatus": 429,
            "message": {"model": "<synthetic>", "content": [{"type": "text", "text": text}]}, "timestamp": at}


def refused_line(at=ERR_AT):
    return {"type": "assistant", "error": "unknown", "apiErrorStatus": 400,
            "message": {"model": "<synthetic>", "content": [{"type": "text",
                        "text": "API Error: 400 Request blocked by safety guard"}]}, "timestamp": at}


def reply(at="2026-11-12T02:00:00Z", text="working on it"):
    return {"type": "assistant", "message": {"model": "claude-opus-4-1", "content": [{"type": "text", "text": text}]},
            "timestamp": at}


def listed(state_="blocked", status="idle", **kw):
    return dict({"id": SHORT, "kind": "background", "name": NAME, "sessionId": SID, "state": state_,
                 "status": status, "startedAt": 1}, **kw)


class Base(unittest.TestCase):
    def setUp(self):
        self.td = TempData()
        self.data = self.td.__enter__()
        self.addCleanup(self.td.__exit__, None, None, None)
        write_ap(self.data, "checkout")
        g = config.load_global()
        g["tools"]["claude"] = FAKE
        config.save_global(g)
        self.home = Path(tempfile.mkdtemp(prefix="ap-home-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        p = mock.patch.dict(os.environ, {"HOME": str(self.home), "FAKE_CLAUDE_LOG": str(self.home / "log")})
        p.start()
        self.addCleanup(p.stop)
        self.proj = self.home / ".claude" / "projects" / "-tmp-x"
        self.proj.mkdir(parents=True)

    def transcript(self, lines, sid=SID):
        (self.proj / f"{sid}.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))

    def add_worker(self, **kw):
        w = {"task": "PROJ-1", "role": "implement", "session_name": NAME, "session_id": SID, "short_id": SHORT,
             "ports": [7110, 7119], "needs": [], "started_at": STARTED, "resumes": 0, "cwd": "/tmp"}
        w.update(kw)
        with state.mutate("checkout") as st:
            st["workers"].append(w)

    def worker(self) -> dict:
        return state.load("checkout")["workers"][0]

    def note(self, listing, now=NOW):
        workers.note_api_stops("checkout", listing, now)
        return self.worker()


class TailTest(Base):
    def test_the_tail_holds_the_errors_after_the_last_reply(self):
        self.transcript([{"type": "user"}, reply(), limit_line()])
        t = usage.transcript_tail(SID)
        self.assertEqual(len(t["errors"]), 1)
        e = t["errors"][0]
        self.assertEqual((e["status"], e["error"]), (429, "rate_limit"))
        self.assertIn("resets 8:40pm", e["text"])
        self.assertEqual(e["at"], datetime(2026, 11, 12, 2, 32, 42, 322000, tzinfo=timezone.utc))
        self.assertEqual(t["reply_at"], datetime(2026, 11, 12, 2, 0, tzinfo=timezone.utc))

    def test_a_reply_after_the_error_means_recovered(self):
        self.transcript([limit_line(), reply("2026-11-12T03:00:00Z")])
        self.assertEqual(usage.transcript_tail(SID)["errors"], [])

    def test_no_transcript(self):
        self.assertIsNone(usage.transcript_tail(SID))
        self.assertIsNone(usage.transcript_tail(""))

    def test_kinds(self):
        since = datetime(2026, 11, 12, 1, 0, tzinfo=timezone.utc)
        for lines, kind in (([limit_line()], "limit"),
                            ([refused_line()], "gateway_refused"),
                            ([dict(limit_line("Rate limited"), error=None, apiErrorStatus=None)], None),
                            ([dict(limit_line("Usage limit reached, resets 9pm (UTC)"), error="unknown",
                                   apiErrorStatus=None)], "limit"),
                            ([dict(limit_line("API Error: 500 overloaded"), error="unknown", apiErrorStatus=500)], None),
                            ([dict(limit_line("API Error: 401 Invalid API key"), error="authentication_failed",
                                   apiErrorStatus=401)], None),
                            ([limit_line(), reply("2026-11-12T03:00:00Z")], None)):
            self.transcript(lines)
            stop = sessions.api_stop(usage.transcript_tail(SID), since)
            self.assertEqual((stop or {}).get("kind"), kind, lines)

    def test_an_error_from_before_the_run_is_history(self):
        # A resumed copy repeats the old conversation, its last error included, with the old times.
        self.transcript([limit_line()])
        late = datetime(2026, 11, 12, 4, 0, tzinfo=timezone.utc)
        self.assertIsNone(sessions.api_stop(usage.transcript_tail(SID), late))
        self.transcript([dict(limit_line(), timestamp=None)])
        self.assertIsNone(sessions.api_stop(usage.transcript_tail(SID), late - timedelta(hours=3)))

    def test_auth_check_reads_the_same_tail(self):
        self.transcript([dict(limit_line("API Error: 401 Invalid API key"), error="authentication_failed")])
        self.assertTrue(profile.session_auth_failed(SID))
        self.transcript([limit_line()])
        self.assertFalse(profile.session_auth_failed(SID))


class ResetTimeTest(unittest.TestCase):
    LA = "America/Los_Angeles"

    def at(self, iso):
        return datetime.fromisoformat(iso)

    def test_next_wall_time_in_the_named_zone(self):
        got = sessions.reset_time(LIMIT_TEXT, self.at("2026-11-12T02:32:42+00:00"))
        self.assertEqual(got, self.at("2026-11-12T04:40:00+00:00"))   # 20:40 PST the same evening
        self.assertEqual(str(got.tzinfo), self.LA)

    def test_a_time_already_past_that_day_is_the_next_day(self):
        got = sessions.reset_time(LIMIT_TEXT, self.at("2026-11-12T05:00:00+00:00"))   # 21:00 PST
        self.assertEqual(got, self.at("2026-11-13T04:40:00+00:00"))

    def test_forms(self):
        base = self.at("2026-11-12T02:32:42+00:00")
        for txt, want in (("resets 9pm (America/Los_Angeles)", "2026-11-12T05:00:00+00:00"),
                          ("resets 12am (America/Los_Angeles)", "2026-11-12T08:00:00+00:00"),
                          ("resets 12:15pm (UTC)", "2026-11-12T12:15:00+00:00"),
                          ("resets 21:30 (Europe/Berlin)", "2026-11-12T20:30:00+00:00"),
                          ("your weekly limit resets Nov 14, 9am (America/Los_Angeles)", "2026-11-14T17:00:00+00:00"),
                          ("resets Jan 2 at 9am (America/Los_Angeles)", "2027-01-02T17:00:00+00:00")):
            self.assertEqual(sessions.reset_time(txt, base), self.at(want), txt)

    def test_unreadable_is_none(self):
        base = self.at("2026-11-12T02:32:42+00:00")
        for txt in ("resets soon", "resets 8:40pm", "resets 8:40pm (Mars/Olympus)", "resets 25:00 (UTC)",
                    "resets 13pm (UTC)", "resets Foo 3, 9am (UTC)", ""):
            self.assertIsNone(sessions.reset_time(txt, base), txt)


class LimitParkTest(Base):
    def test_a_worker_stopped_at_a_limit_is_parked_until_the_reset(self):
        self.transcript([reply(), limit_line()])
        self.add_worker()
        w = self.note([listed()])
        pk = w["parked"]
        self.assertEqual(pk["reason"], "limit")
        self.assertEqual(pk["note"], LIMIT_TEXT)
        self.assertEqual(pk["role"], "implement")
        self.assertEqual(datetime.fromisoformat(pk["resets_at"]), datetime(2026, 11, 12, 4, 40, tzinfo=timezone.utc))
        self.assertEqual(datetime.fromisoformat(pk["resume_at"]), datetime(2026, 11, 12, 4, 45, tzinfo=timezone.utc))
        for k in ("kind", "model", "percent", "parked_at"):
            self.assertIn(k, pk)   # the shape of a quota park
        self.assertEqual(w["limit_parks"], 1)
        rec = json.loads((paths.ap_dir("checkout") / "runs" / "PROJ-1" / "parked.json").read_text())
        self.assertEqual(rec, pk)
        evs = [e for e in events.since("checkout", 0)[0] if e["type"] == "worker_parked"]
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["data"]["reason"], "limit")

    def test_it_is_idempotent(self):
        self.transcript([limit_line()])
        self.add_worker()
        first = self.note([listed()])
        again = self.note([listed()], NOW + timedelta(minutes=5))
        self.assertEqual(first, again)
        self.assertEqual(len([e for e in events.since("checkout", 0)[0] if e["type"] == "worker_parked"]), 1)

    def test_nothing_is_written_when_nothing_changes(self):
        self.transcript([reply()])
        self.add_worker()
        with mock.patch.object(state, "save", side_effect=AssertionError("saved")):
            workers.note_api_stops("checkout", [listed("done")], NOW)

    def test_an_unreadable_reset_backs_off_and_doubles(self):
        self.transcript([limit_line("You've hit your limit")])
        for before, minutes in ((None, 30), (1, 60), (2, 120), (3, 240), (6, 240)):
            with state.mutate("checkout") as st:
                st["workers"] = []
            kw = {"limit_parks": before} if before else {}
            self.add_worker(**kw)
            w = self.note([listed()])
            self.assertEqual(datetime.fromisoformat(w["parked"]["resume_at"]), NOW + timedelta(minutes=minutes), before)
            self.assertIsNone(w["parked"]["resets_at"])
            self.assertEqual(w["limit_parks"], (before or 0) + 1)

    def test_a_working_session_is_left_alone(self):
        self.transcript([limit_line()])
        self.add_worker()
        for s in (listed("working", "busy"), listed("working", "idle"), listed("idle", "busy")):
            self.assertNotIn("parked", self.note([s]))

    def test_a_session_not_listed_any_more_is_parked_too(self):
        self.transcript([limit_line()])
        self.add_worker()
        self.assertEqual(self.note([])["parked"]["reason"], "limit")

    def test_an_unreadable_listing_does_nothing(self):
        self.transcript([limit_line()])
        self.add_worker()
        self.assertNotIn("parked", self.note(None))

    def test_a_recovered_session_is_not_parked(self):
        self.transcript([limit_line(), reply("2026-11-12T02:35:00Z")])
        self.add_worker()
        self.assertNotIn("parked", self.note([listed("done")]))

    def test_a_copied_error_from_before_this_run_is_not_parked(self):
        self.transcript([limit_line()])
        self.add_worker(started_at="2026-11-12T04:50:00+00:00")
        self.assertNotIn("parked", self.note([listed()]))

    def test_a_result_file_wins(self):
        self.transcript([limit_line()])
        self.add_worker()
        run = paths.ap_dir("checkout") / "runs" / "PROJ-1"
        run.mkdir(parents=True)
        (run / "handback.json").write_text("{}")
        self.assertNotIn("parked", self.note([listed()]))

    def test_a_normal_reply_in_this_run_clears_the_count(self):
        self.transcript([limit_line(at="2026-11-11T20:00:00Z"), reply("2026-11-12T02:00:00Z")])
        self.add_worker(limit_parks=3)
        self.assertNotIn("limit_parks", self.note([listed("working", "busy")]))

    def test_a_reply_then_a_new_limit_starts_the_count_again(self):
        self.transcript([reply("2026-11-12T02:00:00Z"), limit_line("You've hit your limit")])
        self.add_worker(limit_parks=3)
        w = self.note([listed()])
        self.assertEqual(w["limit_parks"], 1)
        self.assertEqual(datetime.fromisoformat(w["parked"]["resume_at"]), NOW + timedelta(minutes=30))

    def test_only_copied_replies_keep_the_count(self):
        self.transcript([reply("2026-11-11T20:00:00Z"), limit_line(at="2026-11-11T20:01:00Z")])
        self.add_worker(limit_parks=2)
        self.assertEqual(self.note([listed("working", "busy")])["limit_parks"], 2)

    def test_listing_parks_and_shows_it(self):
        self.transcript([limit_line()])
        self.add_worker()
        with mock.patch.object(sessions, "list_all", return_value=[listed()]):
            rows = workers.listing("checkout")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.main(["worker", "list", "checkout"])
        self.assertEqual(rows[0]["state"], "parked")
        self.assertEqual(rows[0]["parked"]["reason"], "limit")
        self.assertIn("parked", out.getvalue())

    def test_the_gate_parks_before_it_fingerprints(self):
        self.transcript([limit_line()])
        self.add_worker()
        with mock.patch.object(gate, "_sessions", return_value=[listed()]), \
                mock.patch.object(gate, "pr_fingerprint", return_value="x"):
            fps, broken = gate._load_all(config.load_global(), NOW)
        self.assertEqual(broken, {})
        self.assertEqual(fps["checkout"]["workers"], [f"{NAME}=running"])   # quiet while it waits
        self.assertEqual(self.worker()["parked"]["reason"], "limit")
        with mock.patch.object(gate, "_sessions", return_value=[listed()]), \
                mock.patch.object(gate, "pr_fingerprint", return_value="x"):
            fps, _ = gate._load_all(config.load_global(), NOW + timedelta(hours=3))
        self.assertEqual(fps["checkout"]["workers"], [f"{NAME}=parked_due"])

    def test_resume_due_treats_a_limit_park_like_a_quota_park(self):
        cfg = config.load_global()
        quiet = budget.read()
        w = {"parked": {"reason": "limit", "resume_at": "2026-11-12T04:45:00+00:00", "note": "n"}}
        self.assertFalse(park.resume_due(w, NOW, cfg, quiet))
        self.assertTrue(park.resume_due(w, NOW + timedelta(hours=3), cfg, quiet))
        with mock.patch.object(budget, "workers_allowance", return_value=(None, None, {"allow": 0})):
            self.assertFalse(park.resume_due(w, NOW + timedelta(hours=3), cfg, quiet))

    def test_a_resumed_worker_keeps_its_count_and_ending_it_drops_it(self):
        self.transcript([limit_line()])
        self.add_worker(limit_parks=1)
        self.note([listed()])
        agents = self.home / "agents.json"
        agents.write_text(json.dumps([listed("working", "busy", id=SHORT, sessionId="sid-2")]))
        with mock.patch.dict(os.environ, {"FAKE_AGENTS_JSON": str(agents)}):
            rec = workers.start("checkout", "PROJ-1", "implement", str(self.data),
                                "You were parked at a usage limit.", resume=True)
        self.assertNotIn("parked", rec)
        self.assertEqual(rec["limit_parks"], 2)
        self.assertIn(f"--resume {SID}", (self.home / "log").read_text())
        workers.end("checkout", "PROJ-1", "implement")
        self.assertEqual(state.load("checkout")["workers"], [])

    def test_state_schema_allows_the_count(self):
        self.add_worker(limit_parks=2)
        self.assertEqual(self.worker()["limit_parks"], 2)

    def test_snapshot_names_a_limit_park(self):
        self.assertIn("usage limit", snapshot._parked({"parked": {"reason": "limit", "resume_at": "T"}}))


class GatewayRefusedTest(Base):
    def test_listing_marks_it_blocked_with_a_reason(self):
        self.transcript([reply(), refused_line()])
        self.add_worker()
        for s in (listed("blocked"), listed("done"), listed("idle")):
            with mock.patch.object(sessions, "list_all", return_value=[s]):
                rows = workers.listing("checkout")
            self.assertEqual((rows[0]["state"], rows[0].get("blocked_reason")), ("blocked", "gateway_refused"), s)
            self.assertNotIn("parked", self.worker())

    def test_json_listing_carries_the_reason(self):
        self.transcript([refused_line()])
        self.add_worker()
        out = io.StringIO()
        with mock.patch.object(sessions, "list_all", return_value=[listed()]), contextlib.redirect_stdout(out):
            cli.main(["worker", "list", "checkout", "--json"])
        self.assertEqual(json.loads(out.getvalue())[0]["blocked_reason"], "gateway_refused")

    def test_a_plain_blocked_worker_has_no_reason(self):
        self.transcript([reply()])
        self.add_worker()
        with mock.patch.object(sessions, "list_all", return_value=[listed()]):
            rows = workers.listing("checkout")
        self.assertEqual(rows[0]["state"], "blocked")
        self.assertNotIn("blocked_reason", rows[0])

    def test_a_working_session_is_not_marked(self):
        self.transcript([refused_line()])
        self.add_worker()
        with mock.patch.object(sessions, "list_all", return_value=[listed("working", "busy")]):
            rows = workers.listing("checkout")
        self.assertEqual(rows[0]["state"], "running")

    def test_the_gate_sees_it_blocked(self):
        self.transcript([refused_line()])
        self.add_worker()
        with mock.patch.object(gate, "_sessions", return_value=[listed("done")]), \
                mock.patch.object(gate, "pr_fingerprint", return_value="x"):
            fps, _ = gate._load_all(config.load_global(), NOW)
        self.assertEqual(fps["checkout"]["workers"], [f"{NAME}=blocked"])


class DocsTest(unittest.TestCase):
    def step2(self) -> str:
        rb = (ROOT / "RUNBOOK.md").read_text()
        return rb[rb.index("2. **Check workers.**"):rb.index("3. **Implementer done**")]

    def test_runbook_resumes_a_limit_park_like_a_quota_park(self):
        s = self.step2()
        parked = s.split("`parked`")[1].split("`blocked`")[0]
        self.assertIn("`limit`", parked)
        self.assertIn("You were parked at a usage limit", parked)

    def test_runbook_handles_a_gateway_refusal(self):
        s = self.step2()
        self.assertIn('{"key": "worker_gateway_refused", "args": {"session": "<name>"}}', s)
        self.assertIn("`blocked_reason`", s)
        self.assertIn("ap profile use subscription", s)
        self.assertIn("ap worker resume", s)

    def test_labels(self):
        labels = text.labels()
        self.assertEqual(set(re.findall(r"\{(\w+)\}", labels["worker_gateway_refused"])), {"session"})
        body = labels["worker_gateway_refused_body"]
        self.assertEqual(set(re.findall(r"\{(\w+)\}", body)), {"ap", "task", "role", "profile"})
        self.assertIn("ap profile use subscription", body)


if __name__ == "__main__":
    unittest.main()
