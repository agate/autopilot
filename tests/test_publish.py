from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tests.helpers import TempData, write_ap
from autopilot import cli, config, ops, paths, publish


def src(id):
    return {"id": id, "title": {"en": id}, "depends_on": [], "status": "todo", "link": "", "needs": [],
            "critical": False, "phase": 1}


class PublishTest(unittest.TestCase):
    def setUp(self):
        self.td = TempData()
        self.data = self.td.__enter__()
        write_ap(self.data, "checkout")
        ops.tasks_sync("checkout", [src("PROJ-123"), src("PROJ-124")])

    def tearDown(self):
        self.td.__exit__(None, None, None)

    def _mark(self):
        publish.mark(publish.collect(["checkout"]), {})

    def test_doc_ids(self):
        cur = publish.collect(["checkout"])
        self.assertIn("checkout:PROJ-123", cur["tasks"])
        self.assertEqual(cur["tasks"]["checkout:PROJ-123"]["ap"], "checkout")
        self.assertIn("checkout", cur["autopilots"])

    def test_page_learns_when_to_remind_about_unread_self_decisions(self):
        meta = publish.collect(["checkout"])["meta"]
        self.assertEqual(meta["self_review"], {"remind_at": 10, "remind_days": 3})

    def test_unchanged_empty_diff(self):
        self._mark()
        d = publish.diff(publish.published(), publish.collect(["checkout"]))
        self.assertEqual((d["set"], d["delete"]), ([], []))

    def test_changed_task_one_set(self):
        self._mark()
        ops.task_set("checkout", "PROJ-123", {"status": "in_progress"})
        d = publish.diff(publish.published(), publish.collect(["checkout"]))
        sets = [(s["collection"], s["doc_id"]) for s in d["set"]]
        self.assertIn(("tasks", "checkout:PROJ-123"), sets)
        self.assertNotIn(("tasks", "checkout:PROJ-124"), sets)

    def test_only_limits_the_diff_to_the_named_collections(self):
        self._mark()
        ops.task_set("checkout", "PROJ-123", {"status": "in_progress"})
        ops.decision_add("checkout", {"kind": "ask", "title": {"en": "t"}, "body": {"en": "b"}, "options": [],
                                      "links": []})
        cur = publish.collect(["checkout"])
        d = publish.diff(publish.published(), cur, only=["decisions"])
        self.assertEqual([(s["collection"], s["doc_id"]) for s in d["set"]], [("decisions", "checkout:D1")])
        self.assertEqual((d["delete"], d["assets"]), ([], []))

    def test_only_never_lists_assets(self):
        run = self.data / "checkout" / "runs" / "PROJ-123"
        (run / "media").mkdir(parents=True)
        (run / "media" / "after.png").write_bytes(b"png")
        (run / "evidence.json").write_text(json.dumps({"verdict": {"en": "ok"}, "screenshots": [
            {"path": "media/after.png", "caption": {"en": "after"}}]}))
        cur = publish.collect(["checkout"])
        self.assertEqual(publish.diff({}, cur, only=["decisions"])["assets"], [])
        self.assertEqual(len(publish.diff({}, cur)["assets"]), 1)

    def test_partial_mark_keeps_the_rest_for_the_full_diff(self):
        self._mark()
        ops.task_set("checkout", "PROJ-123", {"status": "in_progress"})
        ops.decision_add("checkout", {"kind": "ask", "title": {"en": "t"}, "body": {"en": "b"}, "options": [],
                                      "links": []})
        publish.mark(publish.collect(["checkout"]), {}, only=["decisions"])
        d = publish.diff(publish.published(), publish.collect(["checkout"]))
        left = [(s["collection"], s["doc_id"]) for s in d["set"]]
        self.assertIn(("tasks", "checkout:PROJ-123"), left)
        self.assertNotIn(("decisions", "checkout:D1"), left)
        self.assertEqual(publish.diff(publish.published(), publish.collect(["checkout"]), only=["decisions"])["set"], [])

    def test_partial_mark_records_a_removed_decision(self):
        ops.decision_add("checkout", {"kind": "ask", "title": {"en": "t"}, "body": {"en": "b"}, "options": [],
                                      "links": []})
        self._mark()
        st = json.loads((self.data / "checkout" / "state.json").read_text())
        st["decisions"] = []
        (self.data / "checkout" / "state.json").write_text(json.dumps(st))
        cur = publish.collect(["checkout"])
        self.assertEqual(publish.diff(publish.published(), cur, only=["decisions"])["delete"],
                         [{"collection": "decisions", "doc_id": "checkout:D1"}])
        publish.mark(cur, {}, only=["decisions"])
        self.assertNotIn("checkout:D1", publish.published()["decisions"])

    def test_cli_only_flag(self):
        self._mark()
        ops.task_set("checkout", "PROJ-123", {"status": "in_progress"})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["publish", "diff", "--only", "decisions"]), 0)
            self.assertEqual(json.loads(buf.getvalue())["set"], [])
            self.assertEqual(cli.main(["publish", "diff", "--only", "bogus"]), 2)
            self.assertEqual(cli.main(["publish", "mark", "--only", "decisions"]), 0)
        rest = publish.diff(publish.published(), publish.collect(["checkout"]))["set"]
        self.assertIn(("tasks", "checkout:PROJ-123"), [(s["collection"], s["doc_id"]) for s in rest])

    def test_cli_url_sets_and_prints(self):
        page = "https://claude.ai/artifact/PAGE"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["publish", "url", page]), 0)
            self.assertEqual(cli.main(["publish", "url"]), 0)
        self.assertEqual(buf.getvalue().splitlines(), [page, page])

    def test_cli_url_refuses_a_non_page_argument(self):
        page = "https://claude.ai/artifact/PAGE"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["publish", "url", page]), 0)
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["publish", "url", "checkout"]), 2)
        self.assertIn("run `ap publish url` with no argument", err.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["publish", "url"]), 0)
        self.assertEqual(out.getvalue().strip(), page)

    def test_removed_doc_deleted(self):
        self._mark()
        st = json.loads((self.data / "projects" / "checkout" / "state.json").read_text())
        st["tasks"] = st["tasks"][:1]
        (self.data / "projects" / "checkout" / "state.json").write_text(json.dumps(st))
        d = publish.diff(publish.published(), publish.collect(["checkout"]))
        self.assertIn({"collection": "tasks", "doc_id": "checkout:PROJ-124"}, d["delete"])

    def test_new_screenshot_listed_once(self):
        run = self.data / "projects" / "checkout" / "runs" / "PROJ-123"
        (run / "media").mkdir(parents=True)
        (run / "media" / "after.png").write_bytes(b"png")
        (run / "evidence.json").write_text(json.dumps({"verdict": {"en": "ok"}, "screenshots": [
            {"path": "media/after.png", "caption": {"en": "after"}}]}))
        d = publish.diff({}, publish.collect(["checkout"]))
        self.assertEqual([a["key"] for a in d["assets"]], ["checkout/PROJ-123/after.png"])
        publish.mark(publish.collect(["checkout"]), {"checkout/PROJ-123/after.png": "0123456789abcdef0123456789abcdef"})
        d2 = publish.diff(publish.published(), publish.collect(["checkout"]))
        self.assertEqual(d2["assets"], [])
        ev = publish.collect(["checkout"])["evidence"]["checkout:PROJ-123"]
        self.assertEqual(ev["screenshots"][0]["asset_id"], "0123456789abcdef0123456789abcdef")

    def _evidence(self, shots, files, logs=()):
        run = self.data / "projects" / "checkout" / "runs" / "PROJ-123"
        (run / "media").mkdir(parents=True, exist_ok=True)
        for f in files:
            (run / "media" / f).write_bytes(b"x")
        entry = lambda f: {"path": "media/" + f, "caption": {"en": f}}
        (run / "evidence.json").write_text(json.dumps({"verdict": {"en": "ok"}, "what_changed": {"en": "x"},
                                                       "screenshots": [entry(f) for f in shots],
                                                       "logs": [entry(f) for f in logs]}))
        return run

    def test_logs_are_listed_and_uploaded_as_text(self):
        # A log is shown as text. The asset store refuses .tap and .log, so those go up under a .txt name.
        names = ["teeth.txt", "suite.tap", "run.log", "data.json", "notes.md", "after.png"]
        self._evidence([], names, logs=names)
        ev = publish.collect(["checkout"])["evidence"]["checkout:PROJ-123"]
        self.assertEqual([s["path"] for s in ev["logs"]], ["media/" + n for n in names[:5]])
        d = publish.diff({}, publish.collect(["checkout"]))
        self.assertEqual([(a["key"].split("/")[-1], a["upload_name"]) for a in d["assets"]],
                         [("teeth.txt", "teeth.txt"), ("suite.tap", "suite.tap.txt"), ("run.log", "run.log.txt"),
                          ("data.json", "data.json"), ("notes.md", "notes.md")])

    def test_a_log_reuses_the_asset_it_had_as_a_screenshot(self):
        # Moving an entry from screenshots to logs keeps its key, so nothing is uploaded twice.
        aid = "0123456789abcdef0123456789abcdef"
        publish.mark({}, {"checkout/PROJ-123/teeth.txt": aid})
        self._evidence([], ["teeth.txt"], logs=["teeth.txt"])
        d = publish.diff({}, publish.collect(["checkout"]))
        self.assertEqual(d["assets"], [])
        ev = publish.collect(["checkout"])["evidence"]["checkout:PROJ-123"]
        self.assertEqual(ev["logs"][0]["asset_id"], aid)

    def test_evidence_check_wants_text_files_as_logs(self):
        self._evidence([], ["teeth.txt", "after.png"], logs=["teeth.txt", "after.png", "gone.log"])
        errs = publish.evidence_errors("checkout", "PROJ-123")
        self.assertEqual(len(errs), 2, errs)
        self.assertIn("logs[1].path: media/after.png is not a log", errs[0])
        self.assertIn("logs[2].path: media/gone.log is not a file in the run directory", errs[1])

    def test_handback_criteria_show_as_criteria(self):
        # Before evidence.json exists the page shows the hand-back's criteria, with the reviewer's status.
        run = self.data / "projects" / "checkout" / "runs" / "PROJ-123"
        run.mkdir(parents=True)
        (run / "handback.json").write_text(json.dumps({"criteria": [
            {"id": "c1", "status": "met", "evidence": "test_a"}, {"id": "c2", "status": "met", "evidence": "test_b"}]}))
        (run / "review.json").write_text(json.dumps({"criteria": {"c2": {"status": "partial", "note": "n"}}}))
        ev = publish.collect(["checkout"])["evidence"]["checkout:PROJ-123"]
        self.assertEqual(ev["criteria"], [{"text": "c1", "ok": True, "proof": "test_a"},
                                          {"text": "c2", "ok": False, "proof": "test_b"}])

    def test_only_images_become_screenshots(self):
        # The page shows every screenshot as an <img>: a log or test output there is a broken image.
        names = ["after.png", "Before.JPG", "teeth.txt", "suite.tap", "notes.md"]
        self._evidence(names, names)
        d = publish.diff({}, publish.collect(["checkout"]))
        self.assertEqual([a["key"] for a in d["assets"]], ["checkout/PROJ-123/after.png", "checkout/PROJ-123/Before.JPG"])
        ev = publish.collect(["checkout"])["evidence"]["checkout:PROJ-123"]
        self.assertEqual([s["path"] for s in ev["screenshots"]], ["media/after.png", "media/Before.JPG"])

    def test_evidence_check_wants_image_files_in_the_run_dir(self):
        self._evidence(["after.png", "teeth.txt", "gone.png"], ["after.png", "teeth.txt"])
        errs = publish.evidence_errors("checkout", "PROJ-123")
        self.assertEqual(len(errs), 2, errs)
        self.assertIn("media/teeth.txt is not an image", errs[0])
        self.assertIn("media/gone.png is not a file in the run directory", errs[1])

    def test_recorded_assets_reach_the_next_diff_before_anything_is_published(self):
        # The batch goes out after the uploads, so its rows must carry the new ids; mark comes only after it.
        self._evidence(["after.png"], ["after.png"])
        self.assertEqual([a["key"] for a in publish.diff({}, publish.collect(["checkout"]))["assets"]],
                         ["checkout/PROJ-123/after.png"])
        aid = "0123456789abcdef0123456789abcdef"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["publish", "assets", "checkout/PROJ-123/after.png=/_blob/" + aid]), 0)
        self.assertEqual(publish.published(), {})
        d = publish.diff(publish.published(), publish.collect(["checkout"]))
        self.assertEqual(d["assets"], [])
        row = next(s for s in d["set"] if s["collection"] == "evidence")
        self.assertEqual(row["data"]["screenshots"][0]["asset_id"], aid)

    def test_mark_stores_the_asset_id_when_given_its_url(self):
        # The page builds "/_blob/<id>" itself, so a stored url would double the prefix.
        self._evidence(["after.png"], ["after.png"])
        aid = "0123456789abcdef0123456789abcdef"
        publish.mark(publish.collect(["checkout"]), {"checkout/PROJ-123/after.png": "/_blob/" + aid})
        ev = publish.collect(["checkout"])["evidence"]["checkout:PROJ-123"]
        self.assertEqual(ev["screenshots"][0]["asset_id"], aid)

    def test_evidence_add_copies_files_into_media(self):
        # A worker's shell may not write into the data folder and its Write tool cannot write a binary file.
        with tempfile.TemporaryDirectory() as tmp:
            shot = Path(tmp) / "after.png"
            shot.write_bytes(b"\x89PNG")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(cli.main(["evidence", "add", "checkout", "PROJ-123", str(shot)]), 0)
        self.assertEqual(buf.getvalue().split(), ["media/after.png"])
        self.assertEqual((self.data / "projects" / "checkout" / "runs" / "PROJ-123" / "media" / "after.png").read_bytes(), b"\x89PNG")

    def test_evidence_add_refuses_an_unknown_task_or_a_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            shot = Path(tmp) / "after.png"
            shot.write_bytes(b"x")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["evidence", "add", "checkout", "../PROJ-123", str(shot)]), 1)
                self.assertEqual(cli.main(["evidence", "add", "checkout", "PROJ-123", str(shot), tmp + "/none.png"]), 1)
        self.assertFalse((self.data / "projects" / "checkout" / "PROJ-123").exists())
        self.assertFalse((self.data / "projects" / "checkout" / "runs" / "PROJ-123" / "media" / "after.png").exists())

    def test_labels_merged_with_reader_lang(self):
        g = config.load_global()
        g["reader_lang"] = "zh"
        config.save_global(g)
        (self.data / "labels.zh.json").write_text(json.dumps({"cap_warning": "x"}))
        labels = publish.collect(["checkout"])["meta"]["labels"]
        self.assertIn("cap_warning", labels["en"])
        self.assertEqual(labels["zh"]["cap_warning"], "x")
        self.assertEqual(labels["reader_lang"], "zh")


if __name__ == "__main__":
    unittest.main()
