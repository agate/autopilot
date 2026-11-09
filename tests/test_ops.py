from __future__ import annotations

import contextlib
import io
import json
import unittest
from datetime import datetime, timedelta, timezone

from tests.helpers import TempData, write_ap
from autopilot import cli, events, ops, paths, state


def src(id, status="todo", **kw):
    t = {"id": id, "title": {"en": id}, "depends_on": [], "status": status, "link": "https://example.com/" + id,
         "needs": [], "critical": False, "phase": 1}
    t.update(kw)
    return t


def dec(**kw):
    d = {"kind": "ask", "title": {"en": "Pick"}, "body": {"en": "Why"},
         "options": [{"id": "a", "label": {"en": "A"}}], "links": [{"label": "x", "url": "https://example.com"}]}
    d.update(kw)
    return d


class OpsTest(unittest.TestCase):
    def setUp(self):
        self.td = TempData()
        self.data = self.td.__enter__()
        write_ap(self.data, "checkout")

    def tearDown(self):
        self.td.__exit__(None, None, None)

    def test_sync_keeps_local_pr_takes_source_status(self):
        ops.tasks_sync("checkout", [src("PROJ-123")])
        ops.task_set("checkout", "PROJ-123", {"pr": 42, "status": "in_review"})
        ops.tasks_sync("checkout", [src("PROJ-123", status="done")])
        t = state.load("checkout")["tasks"][0]
        self.assertEqual(t["pr"], 42)
        self.assertEqual(t["status"], "done")
        self.assertEqual(t["origin"], "source")

    def test_sync_marks_removed_skipped(self):
        ops.tasks_sync("checkout", [src("PROJ-123"), src("PROJ-124")])
        ops.tasks_sync("checkout", [src("PROJ-123")])
        st = state.load("checkout")
        self.assertEqual({t["id"]: t["status"] for t in st["tasks"]}, {"PROJ-123": "todo", "PROJ-124": "skipped"})

    def test_sync_leaves_found_tasks(self):
        ops.task_add("checkout", src("PROJ-129"))
        ops.tasks_sync("checkout", [src("PROJ-123")])
        st = state.load("checkout")
        self.assertEqual({t["id"]: t["status"] for t in st["tasks"]}, {"PROJ-129": "todo", "PROJ-123": "todo"})

    def test_task_set_unknown_key_rejected(self):
        ops.tasks_sync("checkout", [src("PROJ-123")])
        with self.assertRaises(state.StateError):
            ops.task_set("checkout", "PROJ-123", {"colour": "red"})

    def test_decision_ids_increment(self):
        self.assertEqual(ops.decision_add("checkout", dec()), "D1")
        self.assertEqual(ops.decision_add("checkout", dec()), "D2")
        d = state.load("checkout")["decisions"][1]
        self.assertEqual(d["status"], "open")

    def test_resolve(self):
        ops.decision_add("checkout", dec())
        ops.decision_resolve("checkout", "D1", "a", "answered")
        d = state.load("checkout")["decisions"][0]
        self.assertEqual((d["status"], d["answer"]), ("answered", "a"))

    def test_resolve_can_leave_the_decision_being_handled_until_marked_handled(self):
        ops.decision_add("checkout", dec())
        ops.decision_resolve("checkout", "D1", "a", handling=True)
        d = state.load("checkout")["decisions"][0]
        self.assertEqual((d["status"], d["answer"], d["handling"]), ("answered", "a", True))
        self.assertEqual(ops.decision_handled("checkout", "D1"), "handled")
        d = state.load("checkout")["decisions"][0]
        self.assertNotIn("handling", d)
        self.assertEqual((d["status"], d["answer"]), ("answered", "a"))
        self.assertEqual(ops.decision_handled("checkout", "D1"), "unchanged")

    def test_handling_survives_a_repeated_resolve_and_a_plain_resolve_has_none(self):
        ops.decision_add("checkout", dec())
        ops.decision_add("checkout", dec())
        ops.decision_resolve("checkout", "D1", "a", handling=True)
        self.assertEqual(ops.decision_resolve("checkout", "D1", "a", handling=True), "unchanged")
        self.assertTrue(state.load("checkout")["decisions"][0]["handling"])
        ops.decision_resolve("checkout", "D2", "a")
        self.assertNotIn("handling", state.load("checkout")["decisions"][1])

    def test_handled_refuses_an_open_or_unknown_decision(self):
        ops.decision_add("checkout", dec())
        with self.assertRaises(state.StateError):
            ops.decision_handled("checkout", "D1")
        with self.assertRaises(state.StateError):
            ops.decision_handled("checkout", "D9")

    def test_handling_flag_and_handled_command(self):
        ops.decision_add("checkout", dec())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["decision", "resolve", "checkout", "D1", "--answer", "a", "--handling"]), 0)
            self.assertTrue(state.load("checkout")["decisions"][0]["handling"])
            self.assertEqual(cli.main(["decision", "handled", "checkout", "D1"]), 0)
        self.assertNotIn("handling", state.load("checkout")["decisions"][0])

    def test_same_answer_twice_is_unchanged(self):
        # A pass that stopped after resolving but before deleting the reply sees it again.
        ops.decision_add("checkout", dec())
        self.assertEqual(ops.decision_resolve("checkout", "D1", "a"), "resolved")
        first = state.load("checkout")["decisions"][0]["answered_at"]
        self.assertEqual(ops.decision_resolve("checkout", "D1", "a"), "unchanged")
        self.assertEqual(state.load("checkout")["decisions"][0]["answered_at"], first)
        resolved = [e for e in events.since("checkout", 0)[0] if e.get("type") == "decision_resolved"]
        self.assertEqual(len(resolved), 1)

    def test_different_answer_is_a_conflict(self):
        # Two devices answered the same card differently: the second never silently wins.
        ops.decision_add("checkout", dec())
        ops.decision_resolve("checkout", "D1", "a")
        with self.assertRaises(ops.AnswerConflict):
            ops.decision_resolve("checkout", "D1", "b")
        self.assertEqual(state.load("checkout")["decisions"][0]["answer"], "a")

    def test_replace_overwrites_on_purpose(self):
        ops.decision_add("checkout", dec())
        ops.decision_resolve("checkout", "D1", "a")
        self.assertEqual(ops.decision_resolve("checkout", "D1", "b", replace=True), "resolved")
        self.assertEqual(state.load("checkout")["decisions"][0]["answer"], "b")

    def test_undo_twice_is_unchanged(self):
        ops.decision_add("checkout", dec(kind="self", options=[], attention=False))
        self.assertEqual(ops.decision_resolve("checkout", "D1", "undo", "undone"), "resolved")
        self.assertEqual(ops.decision_resolve("checkout", "D1", "undo", "undone"), "unchanged")

    def test_a_self_card_says_whether_it_needs_a_look(self):
        # The page puts the ones worth a look on top and folds the rest; the pass has to choose.
        with self.assertRaises(ValueError):
            ops.decision_add("checkout", dec(kind="self", options=[]))
        with self.assertRaises(ValueError):
            ops.decision_add("checkout", dec(kind="self", options=[], attention="yes"))
        ops.decision_add("checkout", dec(kind="self", options=[], attention=True))
        self.assertIs(state.load("checkout")["decisions"][0]["attention"], True)
        # Other kinds never need it.
        ops.decision_add("checkout", dec())
        self.assertNotIn("attention", state.load("checkout")["decisions"][1])

    def test_attention_is_a_bool_in_the_state_file(self):
        ops.decision_add("checkout", dec(kind="self", options=[], attention=False))
        st = state.load("checkout")
        st["decisions"][0]["attention"] = "no"
        with self.assertRaises(state.StateError):
            state.save("checkout", st)

    def test_a_self_card_saved_before_attention_existed_still_loads(self):
        ops.decision_add("checkout", dec(kind="self", options=[], attention=False))
        st = state.load("checkout")
        del st["decisions"][0]["attention"]
        state.save("checkout", st)
        self.assertNotIn("attention", state.load("checkout")["decisions"][0])

    def test_attention_can_be_set_on_an_old_self_card(self):
        ops.decision_add("checkout", dec(kind="self", options=[], attention=False))
        ops.decision_add("checkout", dec())
        self.assertEqual(ops.decision_attention("checkout", "D1", True), "set")
        self.assertIs(state.load("checkout")["decisions"][0]["attention"], True)
        self.assertEqual(ops.decision_attention("checkout", "D1", True), "unchanged")
        with self.assertRaises(state.StateError):
            ops.decision_attention("checkout", "D2", True)   # not a self card
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["decision", "attention", "checkout", "D1", "false"]), 0)
            self.assertEqual(cli.main(["decision", "attention", "checkout", "D1", "maybe"]), 2)
        self.assertEqual(out.getvalue().split(), ["set"])
        self.assertIs(state.load("checkout")["decisions"][0]["attention"], False)

    def test_keep_marks_self_cards_read(self):
        for _ in range(3):
            ops.decision_add("checkout", dec(kind="self", options=[], attention=False))
        ops.decision_add("checkout", dec())
        ops.decision_resolve("checkout", "D3", "undo", "undone")
        got = ops.decision_keep("checkout", ["D1", "D2", "D3", "D4", "D9"])
        self.assertEqual(got, {"kept": ["D1", "D2"], "unchanged": [], "skipped": ["D3", "D4", "D9"]})
        ds = state.load("checkout")["decisions"]
        self.assertEqual([(d["status"], d.get("answer")) for d in ds[:3]],
                         [("answered", "keep"), ("answered", "keep"), ("undone", "undo")])
        self.assertNotIn("handling", ds[0])   # nothing to act on: never shown as being handled
        self.assertEqual(ops.decision_keep("checkout", ["D1"]), {"kept": [], "unchanged": ["D1"], "skipped": []})
        kept = [e for e in events.since("checkout", 0)[0] if e.get("type") == "decisions_kept"]
        self.assertEqual([e["data"]["ids"] for e in kept], [["D1", "D2"]])

    def test_keep_cli(self):
        ops.decision_add("checkout", dec(kind="self", options=[], attention=False))
        ops.decision_add("checkout", dec())
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["decision", "keep", "checkout", "D1", "D2"]), 0)
            self.assertEqual(cli.main(["decision", "keep", "checkout"]), 2)
        self.assertEqual(json.loads(out.getvalue()), {"kept": ["D1"], "unchanged": [], "skipped": ["D2"]})

    def test_a_kept_self_card_can_still_be_undone(self):
        # OK means "read", not "final": the task's evidence page keeps an Undo button.
        ops.decision_add("checkout", dec(kind="self", options=[], attention=False))
        ops.decision_keep("checkout", ["D1"])
        self.assertEqual(ops.decision_resolve("checkout", "D1", "undo", "undone", handling=True), "resolved")
        d = state.load("checkout")["decisions"][0]
        self.assertEqual((d["status"], d["answer"], d["handling"]), ("undone", "undo", True))
        # An undone card is final: OK after it is skipped, and "keep" is no answer that replaces it.
        self.assertEqual(ops.decision_keep("checkout", ["D1"])["skipped"], ["D1"])
        with self.assertRaises(ops.AnswerConflict):
            ops.decision_resolve("checkout", "D1", "keep")

    def test_undo_of_an_archived_read_card_brings_it_back(self):
        # A read card moves to the archive after a week; its Undo stays on the evidence page, and a pass may
        # also archive it between reading the reply and acting on it.
        ops.decision_add("checkout", dec(kind="self", options=[], attention=False))
        ops.decision_add("checkout", dec(kind="self", options=[], attention=False))
        ops.decision_keep("checkout", ["D1", "D2"])
        st = state.load("checkout")
        for d in st["decisions"]:
            d["answered_at"] = "2020-01-01T00:00:00Z"
        state.save("checkout", st)
        self.assertEqual(ops.archive("checkout"), 2)
        self.assertEqual(ops.decision_resolve("checkout", "D1", "undo", "undone", handling=True), "resolved")
        d = state.load("checkout")["decisions"][0]
        self.assertEqual((d["id"], d["status"], d["answer"], d["handling"]), ("D1", "undone", "undo", True))
        lines = (paths.ap_dir("checkout") / "archive.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(l)["id"] for l in lines], ["D2"])
        # Only an undo of a read self card comes back; anything else stays unknown.
        with self.assertRaises(state.StateError):
            ops.decision_resolve("checkout", "D2", "keep")
        with self.assertRaises(state.StateError):
            ops.decision_resolve("checkout", "D9", "undo", "undone")

    def test_keep_clears_a_handling_flag_left_by_an_early_resolve(self):
        # Step 10 may have recorded the keep with --handling; nothing is left to act on, so it must not stick.
        ops.decision_add("checkout", dec(kind="self", options=[], attention=False))
        ops.decision_resolve("checkout", "D1", "keep", handling=True)
        self.assertEqual(ops.decision_keep("checkout", ["D1"])["unchanged"], ["D1"])
        self.assertNotIn("handling", state.load("checkout")["decisions"][0])

    def test_undo_after_an_answer_to_an_ask_is_still_a_conflict(self):
        ops.decision_add("checkout", dec())
        ops.decision_resolve("checkout", "D1", "a")
        with self.assertRaises(ops.AnswerConflict):
            ops.decision_resolve("checkout", "D1", "undo", "undone")

    def test_unread_counts_open_routine_self_cards(self):
        now = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)
        cfg = {"remind_at": 3, "remind_days": 2}
        st = {"decisions": []}
        self.assertEqual(ops.self_unread(st, cfg, now), {"count": 0, "attention": 0, "oldest_at": None, "remind": False})

        def add(at, attention=False, status="open", kind="self"):
            st["decisions"].append({"kind": kind, "status": status, "created_at": at, "attention": attention})
        add("2026-01-09T12:00:00Z")
        add("2026-01-10T11:00:00Z")
        add("2026-01-01T00:00:00Z", attention=True)    # shown on top: not part of the routine count
        add("2026-01-01T00:00:00Z", status="answered")  # read
        add("2026-01-01T00:00:00Z", kind="ask")
        st["decisions"].append({"kind": "self", "status": "open", "created_at": "2026-01-09T18:00:00Z"})  # no field yet
        got = ops.self_unread(st, cfg, now)
        self.assertEqual(got, {"count": 3, "attention": 1, "oldest_at": "2026-01-09T12:00:00Z", "remind": True})
        st["decisions"].pop()
        got = ops.self_unread(st, cfg, now)
        self.assertEqual((got["count"], got["remind"]), (2, False))   # 2 < 3 and the oldest is 1 day old
        got = ops.self_unread(st, cfg, now + timedelta(days=1, minutes=1))
        self.assertTrue(got["remind"])                                # now the oldest is past 2 days

    def test_unread_cli_uses_the_configured_thresholds(self):
        for _ in range(2):
            ops.decision_add("checkout", dec(kind="self", options=[], attention=False))
        (paths.data_root() / "config.json").write_text(json.dumps({"self_review": {"remind_at": 2}}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["decision", "unread", "checkout"]), 0)
        got = json.loads(out.getvalue())
        self.assertEqual((got["count"], got["remind"]), (2, True))

    def test_resolve_cli_exit_codes(self):
        ops.decision_add("checkout", dec())
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            codes = [cli.main(["decision", "resolve", "checkout", "D1", "--answer", "a"]),
                     cli.main(["decision", "resolve", "checkout", "D1", "--answer", "a"]),
                     cli.main(["decision", "resolve", "checkout", "D1", "--answer", "b"]),
                     cli.main(["decision", "resolve", "checkout", "D1", "--answer", "b", "--replace"])]
        self.assertEqual(codes, [0, 0, 3, 0])
        self.assertEqual(out.getvalue().split(), ["resolved", "unchanged", "resolved"])

    def test_archive_only_old_answered(self):
        for _ in range(3):
            ops.decision_add("checkout", dec())
        st = state.load("checkout")
        old = (datetime.now(timezone.utc) - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
        st["decisions"][0].update(status="answered", answered_at=old, answer="a")
        st["decisions"][1].update(status="answered", answered_at=old.replace(old[:4], old[:4]), answer="a")
        st["decisions"][1]["answered_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        state.save("checkout", st)
        self.assertEqual(ops.archive("checkout"), 1)
        self.assertEqual([d["id"] for d in state.load("checkout")["decisions"]], ["D2", "D3"])
        self.assertIn('"D1"', (paths.ap_dir("checkout") / "archive.jsonl").read_text())

    def test_report_sets_last_report(self):
        rep = {"slot": "2026-01-01-09", "done": [{"en": "x"}], "waiting": [], "next": [], "budget": {"en": "ok"},
               "access": []}
        ops.report_write("checkout", rep)
        self.assertEqual(state.load("checkout")["last_report"], "2026-01-01-09")
        self.assertTrue((paths.ap_dir("checkout") / "reports" / "2026-01-01-09.json").exists())

    def test_report_text_checked(self):
        with self.assertRaises(state.StateError):
            ops.report_write("checkout", {"slot": "2026-01-01-09", "done": ["plain"], "waiting": [], "next": [],
                                          "budget": {"en": "ok"}, "access": []})

    def test_pass_end_writes_fields(self):
        ops.pass_begin()
        ops.pass_end(started=2, free=3, ready=4, allow=None)
        lp = json.loads((paths.data_root() / "last_pass.json").read_text())
        self.assertEqual((lp["started"], lp["free"], lp["ready"], lp["allow"]), (2, 3, 4, None))
        self.assertIn("begun_at", lp)
        self.assertIn("ended_at", lp)


if __name__ == "__main__":
    unittest.main()
