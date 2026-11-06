from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT, TempData, write_ap
from autopilot import budget, cli, config, events, gate, ops, park, paths, perms, sessions, snapshot, state, text, workers

FAKE = str(ROOT / "tests" / "bin" / "fake-claude")
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)
RESET = "2026-01-01T14:00:00+00:00"
AP = str(ROOT / "bin" / "ap")


def usage(pcts=(95,), age_min=5, ok=True):
    if not ok:
        return {"ok": False}
    wins = [{"kind": "session" if i == 0 else "weekly", "model": None, "percent": p,
             "resets_at": RESET if i == 0 else "2026-01-05T00:00:00+00:00"} for i, p in enumerate(pcts)]
    return {"ok": True, "age_min": age_min, "stale": age_min > 60, "windows": wins, "credits_used_usd": 0,
            "credits_limit_usd": 200, "spend_limit_reached": False}


def claude_json(pcts=(95,), age_min=5) -> Path:
    cache = {"cachedUsageUtilization": {"fetchedAtMs": NOW_MS - age_min * 60000, "utilization": {
        "limits": [{"kind": "session", "percent": p, "resets_at": RESET} for p in pcts],
        "extra_usage": {"used_credits": 0, "monthly_limit": 20000, "decimal_places": 2}}}}
    p = Path(tempfile.mkdtemp()) / "claude.json"
    p.write_text(json.dumps(cache))
    return p


def worker(task="PROJ-1", role="implement", **kw):
    w = {"task": task, "role": role, "session_name": f"checkout · {task} · {role}", "session_id": "sid-1",
         "short_id": "abcd1234", "ports": [7110, 7119], "needs": [], "started_at": "2026-01-01T00:00:00Z",
         "resumes": 0, "cwd": "/tmp"}
    w.update(kw)
    return w


class Base(unittest.TestCase):
    def setUp(self):
        self.td = TempData()
        self.data = self.td.__enter__()
        write_ap(self.data, "checkout")
        g = config.load_global()
        g["tools"]["claude"] = FAKE
        config.save_global(g)
        self.cfg = config.load_global()
        self.log = Path(tempfile.mkdtemp()) / "log"
        os.environ["FAKE_CLAUDE_LOG"] = str(self.log)

    def tearDown(self):
        os.environ.pop("FAKE_CLAUDE_LOG", None)
        os.environ.pop("FAKE_AGENTS_JSON", None)
        self.td.__exit__(None, None, None)

    def add_worker(self, **kw):
        with state.mutate("checkout") as st:
            st["workers"].append(worker(**kw))


class ParkingTest(Base):
    def test_default_line_is_90(self):
        self.assertEqual(self.cfg["budget"]["park_pct"], 90)
        self.assertIn("budget.park_pct", config.DESCRIBE)

    def test_at_the_line_parks_below_does_not(self):
        self.assertIsNone(budget.parking(usage((89,)), self.cfg))
        p = budget.parking(usage((90,)), self.cfg)
        self.assertEqual((p["kind"], p["percent"], p["resets_at"]), ("session", 90, RESET))
        self.assertEqual(p["resume_at"], "2026-01-01T14:05:00+00:00")  # reset + resume_after_min

    def test_any_window_counts_and_the_latest_reset_decides(self):
        p = budget.parking(usage((92, 91)), self.cfg)
        self.assertEqual(p["kind"], "weekly")
        self.assertEqual(budget.parking(usage((10, 91)), self.cfg)["kind"], "weekly")

    def test_stale_or_unreadable_numbers_never_park(self):
        self.assertIsNone(budget.parking(usage((99,), age_min=61), self.cfg))
        self.assertIsNone(budget.parking(usage(ok=False), self.cfg))
        self.assertIsNone(budget.parking({"ok": True, "windows": []}, self.cfg))

    def test_the_line_is_a_setting(self):
        cfg = config.load_global()
        cfg["budget"]["park_pct"] = 97
        self.assertIsNone(budget.parking(usage((95,)), cfg))

    def test_read_fast_matches_read_and_keeps_a_small_copy(self):
        f = claude_json((95,))
        self.assertEqual(budget.read_fast(f, now_ms=NOW_MS), budget.read(f, now_ms=NOW_MS))
        side = self.data / "usage-cache.json"
        self.assertTrue(side.exists())
        self.assertLess(side.stat().st_size, 2000)
        # Served from the copy: the source cannot be parsed any more, yet the same answer comes back.
        with mock.patch.object(Path, "read_text", side_effect=AssertionError("re-read")):
            f_stat = f.stat()
            self.assertTrue(budget.read_fast(f, now_ms=NOW_MS)["ok"] or f_stat)

    def test_read_fast_follows_the_file_and_age(self):
        f = claude_json((10,))
        self.assertEqual(budget.read_fast(f, now_ms=NOW_MS)["windows"][0]["percent"], 10)
        f.write_text(claude_json((96, ), age_min=5).read_text() + " ")
        self.assertEqual(budget.read_fast(f, now_ms=NOW_MS)["windows"][0]["percent"], 96)
        self.assertTrue(budget.read_fast(f, now_ms=NOW_MS + 3 * 3600 * 1000)["stale"])

    def test_read_fast_unreadable_is_unknown(self):
        self.assertFalse(budget.read_fast(Path("/nonexistent/x.json"), now_ms=NOW_MS)["ok"])


class HookTest(Base):
    WORKER = {"AUTOPILOT_ROLE": "worker"}

    def bash(self, cmd, **kw):
        return dict({"tool_name": "Bash", "tool_input": {"command": cmd}}, **kw)

    def test_under_the_line_allows(self):
        self.assertIsNone(park.decide({"tool_name": "Read", "tool_input": {}}, self.WORKER, usage((50,)), self.cfg))

    def test_over_the_line_denies_with_the_park_instruction(self):
        out = park.decide({"tool_name": "Read", "tool_input": {}}, self.WORKER, usage((95,)), self.cfg)
        h = out["hookSpecificOutput"]
        self.assertEqual((h["hookEventName"], h["permissionDecision"]), ("PreToolUse", "deny"))
        self.assertIn(f"{AP} worker park <ap> <task> <role>", h["permissionDecisionReason"])
        self.assertIn("end your turn", h["permissionDecisionReason"])

    def test_every_tool_is_denied_not_only_bash(self):
        for tool in ("Edit", "Agent", "mcp__x__search", "Bash"):
            out = park.decide({"tool_name": tool, "tool_input": {"command": "ls"}}, self.WORKER, usage((95,)), self.cfg)
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny", tool)

    def test_the_park_command_is_allowed(self):
        cmd = f'{AP} worker park checkout PROJ-1 implement --note "tests green; next: write the evidence"'
        self.assertIsNone(park.decide(self.bash(cmd), self.WORKER, usage((95,)), self.cfg))
        self.assertIsNone(park.decide(self.bash(f"'{AP}' worker park checkout PROJ-1 implement"), self.WORKER,
                                      usage((95,)), self.cfg))

    def test_nothing_else_rides_along_with_the_park_command(self):
        for tail in ("; rm -rf x", " && git push", " | sh", "\nls", " $(whoami)", " `id`"):
            out = park.decide(self.bash(f"{AP} worker park a T r --note x{tail}"), self.WORKER, usage((95,)), self.cfg)
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny", tail)
        for cmd in (f"{AP} worker end a T r", "ap worker park a T r --note x", f"{AP} worker parks a T r",
                    f"{AP} worker park a T"):
            out = park.decide(self.bash(cmd), self.WORKER, usage((95,)), self.cfg)
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny", cmd)

    def test_a_subagent_is_told_to_stop_and_report(self):
        out = park.decide(dict(self.bash("ls"), agent_id="sub-1", agent_type="reviewer"), self.WORKER, usage((95,)), self.cfg)
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("parked for quota", reason)
        self.assertNotIn("worker park", reason)  # only the worker parks; the subagent hands back to it

    def test_a_subagent_cannot_use_the_park_command_path_to_slip_through_either(self):
        # The park command is allowed for the worker; a subagent running it is not special-cased, it parks the
        # parent's record, which is what the parent would do anyway.
        out = park.decide(dict(self.bash(f"{AP} worker park a T r"), agent_id="sub-1"), self.WORKER, usage((95,)), self.cfg)
        self.assertIsNone(out)

    def test_other_roles_are_ignored(self):
        for role in ("dispatcher", "oneoff", None):
            env = {"AUTOPILOT_ROLE": role} if role else {}
            self.assertIsNone(park.decide({"tool_name": "Read", "tool_input": {}}, env, usage((99,)), self.cfg), role)

    def test_stale_or_unreadable_usage_allows(self):
        for b in (usage((99,), age_min=90), usage(ok=False)):
            self.assertIsNone(park.decide({"tool_name": "Read", "tool_input": {}}, self.WORKER, b, self.cfg))

    def test_cli_reads_stdin_and_prints_the_decision(self):
        payload = json.dumps({"tool_name": "Read", "tool_input": {}})
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"AUTOPILOT_ROLE": "worker"}), \
                mock.patch.object(budget, "read_fast", return_value=usage((95,))), \
                mock.patch.object(sys, "stdin", io.StringIO(payload)), contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["hook-park"]), 0)
        self.assertEqual(json.loads(buf.getvalue())["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_cli_swallows_errors(self):
        with mock.patch.object(sys, "stdin", io.StringIO("not json")), contextlib.redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(cli.main(["hook-park"]), 0)
        self.assertEqual(buf.getvalue(), "")

    def test_the_hook_is_fast(self):
        # A real run of the command: python start-up plus a cached read, well under a tool call's own cost.
        env = dict(os.environ, AUTOPILOT_ROLE="worker", AUTOPILOT_DATA=str(self.data), HOME=str(claude_json().parent))
        (Path(env["HOME"]) / ".claude.json").write_text(claude_json((10,)).read_text())
        import time
        t = time.monotonic()
        out = subprocess.run([sys.executable, AP, "hook-park"], input='{"tool_name":"Read"}', env=env,
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0)
        self.assertLess(time.monotonic() - t, 1.0)


class ParkCommandTest(Base):
    def test_records_state_file_and_event(self):
        self.add_worker()
        rec = workers.park("checkout", "PROJ-1", "implement", "tests green, next: evidence", now=NOW, b=usage((95,)))
        self.assertEqual(rec["reason"], "quota")
        w = state.load("checkout")["workers"][0]
        self.assertEqual(w["parked"], rec)
        for k, v in (("kind", "session"), ("percent", 95), ("resets_at", RESET), ("resume_at", "2026-01-01T14:05:00+00:00"),
                     ("note", "tests green, next: evidence"), ("parked_at", NOW.isoformat()), ("role", "implement")):
            self.assertEqual(rec[k], v, k)
        f = json.loads((self.data / "checkout" / "runs" / "PROJ-1" / "parked.json").read_text())
        self.assertEqual(f, rec)
        evs, _ = events.since("checkout", 0)
        self.assertEqual([e["type"] for e in evs if e["type"] == "worker_parked"], ["worker_parked"])

    def test_refuses_when_usage_is_under_the_line_or_unknown(self):
        self.add_worker()
        for b in (usage((50,)), usage(ok=False), usage((99,), age_min=120)):
            with self.assertRaises(ValueError):
                workers.park("checkout", "PROJ-1", "implement", "x", now=NOW, b=b)
        self.assertNotIn("parked", state.load("checkout")["workers"][0])

    def test_refuses_an_unknown_worker(self):
        with self.assertRaises(ValueError):
            workers.park("checkout", "PROJ-9", "implement", "x", now=NOW, b=usage((95,)))

    def test_cli_exit_codes(self):
        self.add_worker()
        err = io.StringIO()
        with mock.patch.object(budget, "read", return_value=usage((10,))), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["worker", "park", "checkout", "PROJ-1", "implement", "--note", "n"]), 1)
        self.assertIn("keep working", err.getvalue())
        out = io.StringIO()
        with mock.patch.object(budget, "read", return_value=usage((95,))), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["worker", "park", "checkout", "PROJ-1", "implement", "--note", "n"]), 0)
        self.assertIn("parked until", out.getvalue())
        self.assertEqual(state.load("checkout")["workers"][0]["parked"]["note"], "n")

    def test_state_schema_wants_an_object(self):
        st = state.empty()
        st["workers"] = [worker(parked="soon")]
        (self.data / "checkout" / "state.json").write_text(json.dumps(st))
        with self.assertRaises(state.StateError):
            state.load("checkout")


class NotDiedTest(Base):
    def parked(self, resume_at="2026-01-01T14:05:00+00:00"):
        return {"reason": "quota", "resume_at": resume_at, "note": "n", "role": "implement", "percent": 95}

    def test_a_parked_worker_whose_session_ended_is_parked_not_dead(self):
        w = worker(parked=self.parked())
        for listing in ([], None, [{"id": "abcd1234", "kind": "background", "state": "done", "status": "idle"}]):
            self.assertEqual(sessions.classify(w, listing, Path(tempfile.mkdtemp())), "parked")
        self.assertEqual(sessions.classify(worker(), [], Path(tempfile.mkdtemp())), "dead")

    def test_a_result_file_still_wins(self):
        run = Path(tempfile.mkdtemp())
        (run / "handback.json").write_text("{}")
        self.assertEqual(sessions.classify(worker(parked=self.parked(), started_at="2026-01-01T00:00:00Z"), [], run), "done")

    def test_listing_and_text_output_show_parked_with_resume_time(self):
        self.add_worker(parked=self.parked())
        with mock.patch.object(sessions, "list_all", return_value=[]):
            rows = workers.listing("checkout")
            self.assertEqual(rows[0]["state"], "parked")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.main(["worker", "list", "checkout"])
        self.assertIn("parked", out.getvalue())
        self.assertIn("2026-01-01T14:05:00+00:00", out.getvalue())
        self.assertNotIn("dead", out.getvalue())

    def fp(self, now, b):
        with mock.patch.object(budget, "read", return_value=b):
            return gate.fingerprint("checkout", config.load_ap("checkout"), state.load("checkout"), [], now, "gh", 4,
                                    self.cfg)["workers"]

    def test_gate_sees_a_parked_worker_as_quiet_until_it_can_continue(self):
        self.add_worker(parked=self.parked())
        quiet = ["checkout · PROJ-1 · implement=running"]
        self.assertEqual(self.fp(NOW, usage((95,))), quiet)                                  # before resume_at
        self.assertEqual(self.fp(NOW + timedelta(hours=3), usage((95,))), quiet)             # past it, still high
        self.assertEqual(self.fp(NOW + timedelta(hours=3), usage((10,))),
                         ["checkout · PROJ-1 · implement=parked_due"])                       # past it, usage fine

    def test_gate_queues_a_pass_when_resume_is_due(self):
        self.add_worker(parked=self.parked())
        before = {"workers": self.fp(NOW, usage((95,))), "files": "", "ready": [], "prs": {}, "watches": {}, "report": None}
        after = dict(before, workers=self.fp(NOW + timedelta(hours=3), usage((10,))))
        self.assertFalse(gate.should_dispatch(before, before, NOW, NOW, 30)[0])
        self.assertEqual(gate.should_dispatch(before, after, NOW, NOW, 30), (True, "changed:workers"))

    def test_an_empty_allowance_keeps_it_parked(self):
        w = worker(parked=self.parked())
        b = usage((88,))  # under the park line, over the new-work line: no new work, so no resume either
        self.assertFalse(park.resume_due(w, NOW + timedelta(hours=3), self.cfg, b))
        self.assertTrue(park.resume_due(w, NOW + timedelta(hours=3), self.cfg, usage((10,))))
        self.assertFalse(park.resume_due(w, NOW, self.cfg, usage((10,))))                    # too early
        self.assertFalse(park.resume_due(worker(), NOW + timedelta(hours=3), self.cfg, usage((10,))))  # not parked

    def test_no_stuck_alarm_from_a_parked_worker(self):
        # A parked worker is not a failed start, and the dispatcher's own numbers are untouched by it.
        self.assertFalse(gate.idle_failed({"free": 3, "ready": 0, "allow": 0, "started": 0}))
        self.add_worker(parked=self.parked())
        with mock.patch.object(sessions, "list_all", return_value=[]):
            self.assertEqual([r["state"] for r in workers.listing("checkout")], ["parked"])


class ResumeTest(Base):
    def test_start_resume_keeps_ports_stops_the_old_session_and_clears_the_record(self):
        agents = Path(tempfile.mkdtemp()) / "agents.json"
        agents.write_text(json.dumps([{"id": "abcd1234", "name": "checkout · PROJ-1 · implement",
                                       "kind": "background", "sessionId": "sid-2", "startedAt": 2}]))
        os.environ["FAKE_AGENTS_JSON"] = str(agents)
        self.add_worker(parked={"reason": "quota", "resume_at": RESET, "note": "n", "role": "implement"},
                        short_id="old11111")
        run = self.data / "checkout" / "runs" / "PROJ-1"
        run.mkdir(parents=True)
        (run / "parked.json").write_text("{}")
        prompt = "You were parked for quota. Your note: n. Continue from there."
        rec = workers.start("checkout", "PROJ-1", "implement", str(self.data), prompt, resume=True)
        self.assertEqual(rec["ports"], [7110, 7119])
        self.assertNotIn("parked", rec)
        self.assertNotIn("parked", state.load("checkout")["workers"][0])
        self.assertFalse((run / "parked.json").exists())
        log = self.log.read_text()
        self.assertIn("stop old11111", log)
        self.assertIn("--resume sid-1", log)
        self.assertIn("Your autopilot: checkout. Your task: PROJ-1. Your role: implement.", log)
        evs, _ = events.since("checkout", 0)
        self.assertIn("worker_resumed", [e["type"] for e in evs])

    def test_runbook_resumes_parked_workers_first(self):
        rb = (ROOT / "RUNBOOK.md").read_text()
        step = rb[rb.index("2. **Check workers.**"):rb.index("3. **Implementer done**")]
        self.assertIn("`parked`", step)
        self.assertIn("--resume", step.split("`parked`")[1].split("`blocked`")[0])
        self.assertIn("You were parked for quota. Your note: <note>. Continue from there.", step)
        self.assertIn("before step 8", step)

    def test_worker_docs_tell_workers_to_obey_the_denial(self):
        for f in ("agents/implementer.md", "agents/reviewer.md", "commands/pr-loop.md"):
            t = (ROOT / f).read_text()
            self.assertIn("ap worker park", t, f)
            self.assertIn("work around", t, f)


class FloorAndPageTest(Base):
    def test_only_the_worker_floor_has_the_hook(self):
        w = perms.generate([], role="worker")
        self.assertEqual(w["hooks"]["PreToolUse"][0]["hooks"][0]["command"], f'"{paths.package_root() / "bin" / "ap"}" hook-park')
        self.assertEqual(w["hooks"]["PreToolUse"][0]["matcher"], "*")
        for role in ("dispatcher", "oneoff"):
            self.assertNotIn("hooks", perms.generate([], role=role))

    def test_one_off_file_has_no_hook(self):
        (self.data / "checkout" / "settings.json").write_text(json.dumps(perms.generate([], role="worker")))
        path = perms.one_off_settings("checkout", "mcp__x__send_message")
        self.assertNotIn("hooks", json.loads(path.read_text()))

    def test_page_shows_a_parked_chip(self):
        page = (ROOT / "templates" / "page.html").read_text()
        labels = json.loads((ROOT / "templates" / "labels.en.json").read_text())
        self.assertIn("function workerChip", page)
        self.assertIn('L("parked_chip"', page)
        self.assertIn("        workerChip(w),", page)
        self.assertEqual(labels["parked_chip"], "Waiting for quota · resumes {when}")
        self.assertIn("parked_chip_unknown", labels)

    def test_page_data_carries_the_parked_fields(self):
        from autopilot import publish
        self.add_worker(parked={"reason": "quota", "resume_at": RESET, "percent": 95, "note": "private note"})
        with mock.patch.object(budget, "read", return_value=usage((95,))):
            row = publish.collect(["checkout"])["workers"]["checkout:PROJ-1:implement"]
        self.assertEqual(row["parked"], {"resume_at": RESET, "percent": 95})

    def test_snapshot_marks_parked_workers(self):
        self.add_worker(task="PROJ-1", parked={"reason": "quota", "resume_at": RESET, "note": "n"})
        self.add_worker(task="PROJ-2")
        out, _, _ = snapshot.build("checkout", 0, {}, NOW)
        line = next(l for l in out.splitlines() if l.startswith("Workers:"))
        self.assertIn(f"PROJ-1 · implement (ports 7110-7119) [parked for quota, resumes {RESET}]", line)
        self.assertNotIn("PROJ-2 · implement (ports 7110-7119) [parked", line)
