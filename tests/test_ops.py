from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

from tests.helpers import TempData, write_ap
from autopilot import ops, paths, state


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
