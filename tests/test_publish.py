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

    def test_removed_doc_deleted(self):
        self._mark()
        st = json.loads((self.data / "checkout" / "state.json").read_text())
        st["tasks"] = st["tasks"][:1]
        (self.data / "checkout" / "state.json").write_text(json.dumps(st))
        d = publish.diff(publish.published(), publish.collect(["checkout"]))
        self.assertIn({"collection": "tasks", "doc_id": "checkout:PROJ-124"}, d["delete"])

    def test_new_screenshot_listed_once(self):
        run = self.data / "checkout" / "runs" / "PROJ-123"
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

    def _evidence(self, shots, files):
        run = self.data / "checkout" / "runs" / "PROJ-123"
        (run / "media").mkdir(parents=True, exist_ok=True)
        for f in files:
            (run / "media" / f).write_bytes(b"x")
        (run / "evidence.json").write_text(json.dumps({"verdict": {"en": "ok"}, "what_changed": {"en": "x"}, "screenshots": [
            {"path": "media/" + f, "caption": {"en": f}} for f in shots]}))

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
        self.assertEqual((self.data / "checkout" / "runs" / "PROJ-123" / "media" / "after.png").read_bytes(), b"\x89PNG")

    def test_evidence_add_refuses_an_unknown_task_or_a_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            shot = Path(tmp) / "after.png"
            shot.write_bytes(b"x")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["evidence", "add", "checkout", "../PROJ-123", str(shot)]), 1)
                self.assertEqual(cli.main(["evidence", "add", "checkout", "PROJ-123", str(shot), tmp + "/none.png"]), 1)
        self.assertFalse((self.data / "checkout" / "PROJ-123").exists())
        self.assertFalse((self.data / "checkout" / "runs" / "PROJ-123" / "media" / "after.png").exists())

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
