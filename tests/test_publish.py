from __future__ import annotations

import json
import unittest

from tests.helpers import TempData, write_ap
from autopilot import config, ops, paths, publish


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
        publish.mark(publish.collect(["checkout"]), {"checkout/PROJ-123/after.png": "https://claude.ai/asset/x"})
        d2 = publish.diff(publish.published(), publish.collect(["checkout"]))
        self.assertEqual(d2["assets"], [])
        ev = publish.collect(["checkout"])["evidence"]["checkout:PROJ-123"]
        self.assertEqual(ev["screenshots"][0]["asset_url"], "https://claude.ai/asset/x")

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
