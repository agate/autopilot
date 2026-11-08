from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT, TempData, write_ap
from autopilot import config, gate, lock, ops, publish, refresh, replies, state

URL = "https://claude.ai/artifact/PAGE"
NOW = datetime(2026, 1, 2, 12, 0).astimezone()


def dec():
    return {"kind": "ask", "title": {"en": "Pick"}, "body": {"en": "Why"},
            "options": [{"id": "a", "label": {"en": "A"}}], "links": []}


def committed(writes, skip=()):
    """The tool's own record of a batch it wrote, as a live transcript holds it in toolUseResult.db_write."""
    return {"op": "batch", "committed": True, "results": [
        {"op": w["op"], "collection": w["collection"], "doc_id": w["doc_id"], "version": 7}
        for w in writes if w["doc_id"] not in skip]}


def transcript(path: Path, plan: dict, url=URL, result=None, is_error=False, drop=None, db_write=None):
    """A batch call and its result shaped like a live transcript: the result text, and in toolUseResult the
    tool's record of what it wrote (a success) or its error string (a refusal)."""
    writes = [w for w in plan["writes"] if w is not drop]
    if result is None:
        result = (f"Database batch committed atomically — {len(writes)} writes; who can read them follows the "
                  "page's access rules (by default, everyone who can open the artifact):\n"
                  + "\n".join(f'- {w["op"]} "{w["collection"]}"/"{w["doc_id"]}" (version 7)' for w in writes))
    if db_write is None and not is_error:
        db_write = committed(writes)
    tur = {"written": {"url": url}, "owned": True, "db_write": db_write} if db_write else f"Error: {result}"
    lines = [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "b1", "name": "ArtifactData", "input": {"action": "batch", "url": url, "writes": writes}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "b1", "content": result,
                                                  "is_error": is_error}]}, "toolUseResult": tur}]
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    return path


class Env(unittest.TestCase):
    def setUp(self):
        self.td = TempData()
        self.data = self.td.__enter__()
        write_ap(self.data, "checkout")
        publish._save_page({"url": URL})
        self.gcfg = config.load_global()

    def tearDown(self):
        self.td.__exit__(None, None, None)

    def publish_now(self, at="2026-01-02T11:59:00Z"):
        publish.mark(publish.collect(config.list_aps()), {}, list(refresh.LIGHT))
        p = publish._page()
        p["published_at"] = at
        publish._save_page(p)


class ChangeTest(Env):
    def test_volatile_numbers_alone_are_not_a_change(self):
        self.publish_now()
        prev = publish._page()["published"]
        cur = copy.deepcopy(prev)
        cur["meta"]["health"]["last_gate_at"] = "2099-01-01T00:00:00"
        cur["meta"]["budget"]["budget"] = {"five_hour": {"percent": 77}}
        cur["meta"]["budget"]["allowance"]["allow"] = 3
        cur["autopilots"]["checkout"]["tokens"] = {"input": 999}
        self.assertFalse(refresh.meaningful_change(prev, cur))

    def test_a_decision_a_task_or_a_pause_is_a_change(self):
        self.publish_now()
        prev = publish._page()["published"]
        ops.decision_add("checkout", dec())
        self.assertTrue(refresh.meaningful_change(prev, publish.collect(["checkout"])))
        cur = copy.deepcopy(prev)
        cur["meta"]["budget"]["allowance"]["reason"] = "credits-test"
        self.assertTrue(refresh.meaningful_change(prev, cur))

    def test_evidence_is_not_light(self):
        self.assertNotIn("evidence", refresh.LIGHT)
        self.assertNotIn("archive", refresh.LIGHT)
        prev = {c: {} for c in publish.COLLECTIONS}
        cur = copy.deepcopy(prev)
        cur["evidence"]["x"] = {"a": 1}
        self.assertFalse(refresh.meaningful_change(prev, cur))


class DueTest(Env):
    def test_due_on_change_and_debounced(self):
        self.publish_now()
        prev = publish._page()["published"]
        ops.decision_add("checkout", dec())
        cur = publish.collect(["checkout"])
        self.assertEqual(refresh.due(self.gcfg, {}, NOW, prev, cur), "change")
        self.assertIsNone(refresh.due(self.gcfg, {"last_at": (NOW - timedelta(minutes=1)).isoformat()}, NOW, prev, cur))
        self.assertEqual(refresh.due(self.gcfg, {"last_at": (NOW - timedelta(minutes=4)).isoformat()}, NOW, prev, cur), "change")

    def test_not_due_for_volatile_only_until_page_refresh_min(self):
        self.publish_now(at=(NOW - timedelta(minutes=10)).astimezone().strftime("%Y-%m-%dT%H:%M:%SZ"))
        p = publish._page()
        p["published_at"] = (NOW - timedelta(minutes=10)).astimezone(tz=__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        publish._save_page(p)
        prev = publish._page()["published"]
        cur = copy.deepcopy(prev)
        cur["meta"]["health"]["last_gate_at"] = "later"
        self.assertIsNone(refresh.due(self.gcfg, {}, NOW, prev, cur))
        p["published_at"] = (NOW - timedelta(minutes=31)).astimezone(tz=__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        publish._save_page(p)
        self.assertEqual(refresh.due(self.gcfg, {}, NOW, prev, cur), "periodic")
        self.assertIsNone(refresh.due(dict(self.gcfg, page_refresh_min=0), {}, NOW, prev, cur))

    def test_backoff_blocks_until_next_at(self):
        self.publish_now()
        prev = publish._page()["published"]
        ops.decision_add("checkout", dec())
        cur = publish.collect(["checkout"])
        rs = {"next_at": (NOW + timedelta(minutes=5)).isoformat()}
        self.assertIsNone(refresh.due(self.gcfg, rs, NOW, prev, cur))
        self.assertEqual(refresh.due(self.gcfg, rs, NOW + timedelta(minutes=6), prev, cur), "change")

    def test_a_failed_refresh_is_retried_while_passes_keep_the_page_fresh(self):
        # Seen live: passes published the whole page every 30 minutes, so nothing changed and the page was never
        # stale; a refresh that had failed was never tried again and its card stayed open.
        self.publish_now(at=(NOW - timedelta(minutes=1)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        prev = publish._page()["published"]
        cur = copy.deepcopy(prev)
        last = (NOW - timedelta(hours=1)).isoformat()
        self.assertEqual(refresh.due(self.gcfg, {"errors": 6, "last_at": last}, NOW, prev, cur), "retry")
        waiting = {"errors": 6, "last_at": last, "next_at": (NOW + timedelta(minutes=5)).isoformat()}
        self.assertIsNone(refresh.due(self.gcfg, waiting, NOW, prev, cur))   # still inside its backoff
        just_tried = {"errors": 6, "last_at": (NOW - timedelta(minutes=1)).isoformat()}
        self.assertIsNone(refresh.due(self.gcfg, just_tried, NOW, prev, cur))   # the debounce still holds
        self.assertIsNone(refresh.due(self.gcfg, {"errors": 0, "last_at": last}, NOW, prev, cur))   # healthy

    def test_config_key(self):
        self.assertEqual(config.DEFAULT_GLOBAL["page_refresh_min"], 30)
        self.assertIn("page_refresh_min", config.DESCRIBE)
        self.assertIn("`page_refresh_min`", (ROOT / "references" / "config.md").read_text())


class PrepareTest(Env):
    def test_one_file_per_document_and_a_manifest(self):
        self.publish_now()
        prev = publish._page()["published"]
        ops.decision_add("checkout", dec())
        cur = publish.collect(["checkout"])
        folder = self.data / "page-refresh"
        plan = refresh.prepare(prev, cur, folder)
        sets = [w for w in plan["writes"] if w["op"] == "set"]
        self.assertTrue(any(w["collection"] == "decisions" for w in sets))
        for w in sets:
            self.assertEqual(json.loads(Path(w["file_path"]).read_text()), cur[w["collection"]][w["doc_id"]])
        self.assertEqual(json.loads((folder / "manifest.json").read_text()), plan)
        self.assertNotIn("evidence", plan["collections"])

    def test_file_paths_are_resolved_through_symbolic_links(self):
        # A file_path spelled through a link makes the tool stop at a permission prompt nobody answers, and the
        # session's read rule names the resolved folder.
        self.publish_now()
        prev = publish._page()["published"]
        ops.decision_add("checkout", dec())
        real = self.data / "real"
        real.mkdir()
        (self.data / "link").symlink_to(real)
        plan = refresh.prepare(prev, publish.collect(["checkout"]), self.data / "link" / "page-refresh")
        sets = [w for w in plan["writes"] if w["op"] == "set"]
        self.assertTrue(sets)
        for w in sets:
            self.assertEqual(w["file_path"], os.path.realpath(w["file_path"]))
            self.assertTrue(w["file_path"].startswith(str(real.resolve() / "page-refresh") + os.sep))

    def test_deletes_have_no_file_and_batches_are_capped(self):
        writes = [{"op": "delete", "collection": "tasks", "doc_id": f"t{i}"} for i in range(120)]
        self.assertEqual([len(b) for b in refresh.batches(writes)], [50, 50, 20])

    def test_prompt_uses_get_for_few_and_list_for_many(self):
        w = [{"op": "delete", "collection": "tasks", "doc_id": "t1"}]
        few = refresh.build_prompt(URL, {"writes": w, "collections": {"tasks": ["t1"]}})
        self.assertIn("action get", few)
        many = refresh.build_prompt(URL, {"writes": w, "collections": {"tasks": [f"t{i}" for i in range(11)]}})
        self.assertIn("action list", many)
        self.assertIn(URL, few)
        self.assertIn("if_version", few)


class VerifyTest(Env):
    def plan(self):
        self.publish_now()
        prev = publish._page()["published"]
        ops.decision_add("checkout", dec())
        return refresh.prepare(prev, publish.collect(["checkout"]), self.data / "page-refresh")

    def test_proven(self):
        plan = self.plan()
        self.assertEqual(refresh.verify(transcript(self.data / "t.jsonl", plan), URL, plan), "")

    def test_failures(self):
        plan = self.plan()
        t = self.data / "t.jsonl"
        self.assertIn("failed", refresh.verify(transcript(t, plan, is_error=True), URL, plan))
        self.assertIn("failed", refresh.verify(transcript(t, plan, result="Refused: document changed (version 4)",
                                                          db_write=False), URL, plan))
        self.assertIn("no ArtifactData batch", refresh.verify(transcript(t, plan, url="other"), URL, plan))
        self.assertIn("never sent", refresh.verify(transcript(t, plan, drop=plan["writes"][0]), URL, plan))
        self.assertIn("no transcript", refresh.verify(self.data / "nope", URL, plan))

    def test_a_fail_word_in_a_document_id_does_not_fail_a_success(self):
        # The result text lists every document it wrote, so a task named "fix-login-error" is in it.
        w = [{"op": "set", "collection": "tasks", "doc_id": "fix-login-error", "file_path": "/x/0000.json"},
             {"op": "set", "collection": "reports", "doc_id": "denied-refunds", "file_path": "/x/0001.json"}]
        plan = {"writes": w, "collections": {"tasks": ["fix-login-error"], "reports": ["denied-refunds"]}}
        self.assertEqual(refresh.verify(transcript(self.data / "t.jsonl", plan), URL, plan), "")

    def test_only_the_tools_record_of_a_write_is_proof(self):
        plan = self.plan()
        t = self.data / "t.jsonl"
        sets = [w for w in plan["writes"] if w["op"] == "set"]
        self.assertIn("failed", refresh.verify(transcript(t, plan, result="Applied 2 writes atomically.",
                                                          db_write=False), URL, plan))
        self.assertIn("failed", refresh.verify(transcript(t, plan, result="Database write not committed.",
                                                          db_write={"op": "batch", "committed": False}), URL, plan))
        skipped = committed(plan["writes"], skip={sets[0]["doc_id"]})
        self.assertIn(sets[0]["doc_id"], refresh.verify(transcript(t, plan, db_write=skipped), URL, plan))

    def test_an_unplanned_write_is_not_proof(self):
        plan = self.plan()
        bad = dict(plan, writes=plan["writes"] + [{"op": "set", "collection": "tasks", "doc_id": "zzz"}])
        self.assertIn("unplanned", refresh.verify(transcript(self.data / "t.jsonl", bad), URL, plan))


class StepTest(Env):
    def setUp(self):
        super().setUp()
        self.publish_now()
        ops.decision_add("checkout", dec())
        self.sessions = []
        self.result = None

    def fake_session(self, gcfg, url, prompt, cwd):
        self.sessions.append(prompt)
        plan = json.loads((Path(cwd) / "manifest.json").read_text())
        if self.result == "ok":
            return {"ok": True, "transcript": transcript(self.data / "t.jsonl", plan)}
        if self.result == "start":   # the error run_session returns when no background session can start
            return {"ok": False, "error": "could not start the refresh: could not start session: Workspace not trusted"}
        return {"ok": False, "error": "the refresh did not finish in time"}

    def step(self, gs, now=NOW):
        with mock.patch.object(refresh, "run_session", self.fake_session):
            return refresh.step(self.gcfg, gs, now)

    def test_success_marks_only_the_light_collections(self):
        self.result = "ok"
        gs = {}
        self.assertTrue(self.step(gs))
        self.assertEqual(len(self.sessions), 1)
        pub = publish._page()["published"]
        self.assertIn("checkout:D1", pub["decisions"])
        self.assertEqual(publish.diff(pub, publish.collect(["checkout"]), ["decisions", "tasks", "workers"])["set"], [])
        self.assertEqual(gs["refresh"]["errors"], 0)

    def test_evidence_stays_unmarked_for_the_next_pass(self):
        self.result = "ok"
        page = publish._page()
        page["published"]["evidence"] = {}
        publish._save_page(page)
        cur = publish.collect(["checkout"])
        cur["evidence"]["checkout:T"] = {"ap": "checkout", "task": "T", "screenshots": [], "logs": []}
        with mock.patch.object(publish, "collect", lambda aps: cur):
            self.step({})
        self.assertEqual(publish._page()["published"].get("evidence"), {})

    def test_skipped_while_a_pass_holds_the_lock(self):
        self.result = "ok"
        lk = self.data / "lock"
        self.assertTrue(lock.acquire(lk, "pass"))
        try:
            with mock.patch.object(lock, "acquire", lambda p, l: False):
                self.assertFalse(self.step({}))
        finally:
            lock.release(lk)
        self.assertEqual(self.sessions, [])

    def test_lock_is_released_after_a_refresh(self):
        self.result = "ok"
        self.step({})
        self.assertIsNone(lock.held(self.data / "lock"))

    def test_failure_backs_off_then_signals_after_three(self):
        gs = {}
        t = NOW
        for n in range(1, 4):
            self.assertTrue(self.step(gs, t))
            self.assertEqual(gs["refresh"]["errors"], n)
            nxt = datetime.fromisoformat(gs["refresh"]["next_at"])
            self.assertGreaterEqual(nxt - t, timedelta(minutes=5))
            self.assertFalse(self.step(gs, t + timedelta(minutes=4)))   # no retry every minute
            t = nxt + timedelta(seconds=1)
        card = [d for d in state.load("checkout")["decisions"] if d["kind"] == "system"]
        self.assertEqual(len(card), 1)
        self.assertEqual(card[0]["access"]["check"], "page_refresh")
        self.assertEqual(publish._page()["published"]["decisions"], {})
        self.result = "ok"
        self.assertTrue(self.step(gs, t + timedelta(hours=1)))
        self.assertEqual(gs["refresh"]["errors"], 0)
        self.assertEqual([d for d in state.load("checkout")["decisions"] if d["kind"] == "system" and d["status"] == "open"], [])

    def fail_three_times(self, kind):
        """Three failed refreshes in a row (the access card is open); returns gs and the last attempt's time."""
        self.result, gs, t = kind, {}, NOW
        for _ in range(3):
            self.assertTrue(self.step(gs, t))
            last, t = t, datetime.fromisoformat(gs["refresh"]["next_at"]) + timedelta(seconds=1)
        self.result = "ok"
        return gs, last

    def open_cards(self):
        return [d for d in state.load("checkout")["decisions"] if d["kind"] == "system" and d["status"] == "open"]

    def test_a_start_error_retries_once_the_reply_check_starts_a_session(self):
        # Seen live: no background session could start for hours. Once they could again, the reply check (the
        # same kind of session) worked every minute, but the refresh waited out its backoff and the card stayed.
        gs, last = self.fail_three_times("start")
        self.assertEqual(len(self.open_cards()), 1)
        gs["replies"] = {"ok_at": (last + timedelta(minutes=1)).isoformat()}
        self.assertTrue(self.step(gs, last + timedelta(minutes=4)))   # the backoff runs to last + 20 minutes
        self.assertEqual(len(self.sessions), 4)
        self.assertEqual(gs["refresh"]["errors"], 0)
        self.assertNotIn("next_at", gs["refresh"])
        self.assertEqual(self.open_cards(), [])

    def test_a_start_error_keeps_its_backoff_without_a_newer_reply_check(self):
        gs, last = self.fail_three_times("start")
        for replies_state in ({}, {"ok_at": (last - timedelta(minutes=1)).isoformat()}):
            gs["replies"] = replies_state
            self.assertFalse(self.step(gs, last + timedelta(minutes=4)), replies_state)
        self.assertEqual(len(self.sessions), 3)
        self.assertEqual(len(self.open_cards()), 1)

    def test_another_error_keeps_its_backoff_whatever_the_reply_check_does(self):
        # The reply check works every minute, so retrying any error on it would undo the backoff.
        gs, last = self.fail_three_times("timeout")
        gs["replies"] = {"ok_at": (last + timedelta(minutes=1)).isoformat()}
        self.assertFalse(self.step(gs, last + timedelta(minutes=4)))
        self.assertEqual(len(self.sessions), 3)
        self.assertEqual(len(self.open_cards()), 1)

    def test_the_card_says_the_refresh_retries_by_itself(self):
        self.fail_three_times("start")
        fix = self.open_cards()[0]["access"]["fix"]
        self.assertNotIn("doctor", fix)   # doctor neither fixes nor retries the refresh
        self.assertIn("retries on its own", fix)
        self.assertIn("background sessions start again", fix)
        self.assertIn("passes keep publishing the page", fix)

    def test_a_success_closes_the_card(self):
        self.fail_three_times("start")
        rs = {"errors": 3, "error": "x", "next_at": NOW.isoformat(), "signaled": True}
        refresh._ok(rs, NOW)
        self.assertEqual(rs, {"errors": 0, "ok_at": NOW.isoformat()})
        self.assertEqual(self.open_cards(), [])

    def test_no_page_yet_no_refresh(self):
        publish._save_page({})
        self.assertFalse(self.step({}))
        self.assertEqual(self.sessions, [])

    def test_same_data_only_moves_the_publish_time(self):
        self.result = "ok"
        self.step({})
        self.sessions.clear()
        page = publish._page()
        page["published_at"] = "2020-01-01T00:00:00Z"
        publish._save_page(page)
        cur = publish.collect(["checkout"])
        with mock.patch.object(publish, "collect", lambda aps: cur):
            self.assertFalse(self.step({}, NOW + timedelta(minutes=40)))
        self.assertEqual(self.sessions, [])
        self.assertNotEqual(publish._page()["published_at"], "2020-01-01T00:00:00Z")


class GateWiringTest(Env):
    def test_gate_refreshes_when_no_pass_runs_and_skips_when_one_does(self):
        seen = []
        write_ap(self.data, "checkout", report_times=[])
        ok = {"allow": 0, "paused_until": None, "reason": "credits"}
        fn = lambda g, aps: [{"started": 0, "free": 0, "ready": 0, "allow": 0, "ended_at": "x"}]
        with mock.patch.object(gate, "_sessions", lambda g: []), mock.patch.object(gate, "notify", lambda m: None), \
                mock.patch.object(gate, "_allowance", lambda g, now: ok), \
                mock.patch.object(refresh, "step", lambda g, gs, now: seen.append(1)):
            gate.run(now=NOW, dispatch_fn=fn)            # first run: a pass is due (first) only if not credits
            n = len(seen)
            (self.data / "tick.json").write_text(json.dumps({"aps": ["checkout"], "at": "2000-01-01T00:00:00Z"}))
            gate.run(now=NOW + timedelta(minutes=1), dispatch_fn=fn)
        self.assertEqual(len(seen), n)   # a pass ran, so no refresh that minute
        self.assertGreaterEqual(n, 1)


class RunbookTest(unittest.TestCase):
    def test_tick_only_after_a_new_answer(self):
        rb = (ROOT / "RUNBOOK.md").read_text()
        self.assertIn("ONLY when this re-read found at least one new answer", rb)
        self.assertIn("never queue a pass from a pass", rb)
        self.assertNotIn("so that pass starts right after this one ends", rb)


class SessionTest(Env):
    """The real run_session against the fake claude, with the listing entries the live CLI showed."""

    @contextlib.contextmanager
    def cli(self, state="done", status="idle", transcript=Path("/x/t.jsonl")):
        self.log = self.data / "claude.log"
        agents = self.data / "agents.json"
        agents.write_text(json.dumps([{"id": "abcd1234", "name": refresh.NAME, "status": status, "state": state,
                                       "sessionId": "sess-1", "kind": "background", "startedAt": 1, "pid": 1}]))
        self.gcfg = dict(self.gcfg, tools={"claude": str(ROOT / "tests" / "bin" / "fake-claude")})
        find = transcript if callable(transcript) else (lambda sid: transcript)
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_LOG": str(self.log), "FAKE_AGENTS_JSON": str(agents)}), \
                mock.patch("autopilot.usage.find_transcript", find), mock.patch.object(refresh, "TIMEOUT_S", 2):
            t = time.monotonic()
            yield
            self.took = time.monotonic() - t

    def run_session(self, cwd=None, **kw):
        with self.cli(**kw):
            return refresh.run_session(self.gcfg, URL, "PROMPT", cwd or self.data, poll_s=0.05)

    def start_line(self):
        return next(l for l in self.log.read_text().splitlines() if l.startswith("--bg"))

    def test_argv_is_a_small_haiku_session(self):
        res = self.run_session()
        self.assertEqual(res, {"ok": True, "transcript": Path("/x/t.jsonl")})
        start = self.start_line()
        for part in ("--model haiku", "--strict-mcp-config", "--setting-sources project", "--allowedTools=ArtifactData",
                     "--disallowedTools=", "--system-prompt", "PROMPT"):
            self.assertIn(part, start)
        self.assertIn("rm abcd1234", self.log.read_text())

    def test_the_session_may_read_its_own_folder_only(self):
        # The tool reads each document's file itself, and a Read deny refuses that whatever the folder.
        real = self.data / "real"
        real.mkdir()
        (self.data / "link").symlink_to(real)
        self.run_session(cwd=self.data / "link")
        start = self.start_line()
        denied = re.search(r"--disallowedTools=(\S+)", start).group(1).split(",")
        self.assertNotIn("Read", denied)
        self.assertIn("Bash", denied)
        self.assertIn(f"--allowedTools=ArtifactData,Read(/{real.resolve()}/**) ", start)
        self.assertIn("Read", replies.DISALLOWED.split(","))   # the answer read still reads no file

    def test_a_turn_that_ended_in_failure_ends_the_wait(self):
        # Seen live: a turn that replied FAILED lists as blocked (or failed, or done) and idle.
        for state in ("blocked", "failed", "done"):
            self.assertEqual(self.run_session(state=state), {"ok": True, "transcript": Path("/x/t.jsonl")}, state)
            self.assertLess(self.took, 1.5)

    def test_a_permission_prompt_fails_fast_with_its_reason(self):
        res = self.run_session(state="blocked", status="waiting")
        self.assertEqual(res, {"ok": False, "error": "the refresh stopped at a permission prompt"})
        self.assertLess(self.took, 1.5)
        self.assertIn("rm abcd1234", self.log.read_text())

    def test_a_failed_turn_records_the_tools_own_error(self):
        self.publish_now()
        ops.decision_add("checkout", dec())
        refused = ("Error: Writing a database document from a file reads file contents, and the Read deny rule from "
                   "CLI argument forbids reading files")
        manifest = self.data / "page-refresh" / "manifest.json"
        lazy = lambda sid: transcript(self.data / "t.jsonl", json.loads(manifest.read_text()), result=refused,
                                      is_error=True)
        gs = {}
        with self.cli(state="blocked", transcript=lazy):
            self.assertTrue(refresh.step(self.gcfg, gs, NOW))
        self.assertIn("Read deny rule", gs["refresh"]["error"])
        self.assertLess(self.took, 1.5)


if __name__ == "__main__":
    unittest.main()
