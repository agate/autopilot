from __future__ import annotations

import json
import unittest

from tests.helpers import TempData, write_ap
from autopilot import config, events, state, text


def _task(**kw):
    t = {"id": "PROJ-123", "title": {"en": "Do it"}, "depends_on": [], "status": "todo",
         "link": "https://example.com/PROJ-123", "needs": [], "critical": False, "phase": 1, "origin": "source"}
    t.update(kw)
    return t


def _decision(**kw):
    d = {"id": "D1", "kind": "ask", "title": {"en": "Pick one"}, "body": {"en": "Why"},
         "options": [{"id": "a", "label": {"en": "A"}, "recommended": True}],
         "links": [{"label": "ticket", "url": "https://example.com"}], "status": "open",
         "created_at": "2026-01-01T00:00:00Z"}
    d.update(kw)
    return d


class StateTest(unittest.TestCase):
    def test_empty_is_valid(self):
        self.assertEqual(state.validate(state.empty(), {"en"}), [])

    def test_unknown_top_key_named(self):
        st = state.empty()
        st["bogus"] = 1
        errs = state.validate(st, {"en"})
        self.assertTrue(any("bogus" in e for e in errs), errs)

    def test_bad_task_status(self):
        st = state.empty()
        st["tasks"].append(_task(status="finished"))
        self.assertTrue(state.validate(st, {"en"}))

    def test_text_requires_reader_lang(self):
        st = state.empty()
        st["decisions"].append(_decision())
        errs = state.validate(st, {"en", "zh"})
        self.assertTrue(any("zh" in e for e in errs), errs)

    def test_text_en_reader_ok(self):
        st = state.empty()
        st["tasks"].append(_task())
        st["decisions"].append(_decision())
        self.assertEqual(state.validate(st, {"en"}), [])

    def test_label_key_must_exist(self):
        self.assertEqual(text.check({"key": "dispatcher_stuck"}, {"en", "zh"}, "x"), [])
        self.assertTrue(text.check({"key": "no_such_label"}, {"en"}, "x"))

    def test_save_atomic_on_invalid(self):
        with TempData() as d:
            write_ap(d, "checkout")
            state.save("checkout", state.empty())
            before = (d / "projects" / "checkout" / "state.json").read_text()
            bad = state.empty()
            bad["bogus"] = True
            with self.assertRaises(state.StateError):
                state.save("checkout", bad)
            self.assertEqual((d / "projects" / "checkout" / "state.json").read_text(), before)

    def test_corrupt_json_raises(self):
        with TempData() as d:
            write_ap(d, "checkout")
            (d / "projects" / "checkout" / "state.json").write_text("{not json")
            with self.assertRaises(state.StateError):
                state.load("checkout")

    def test_events_since_offsets(self):
        with TempData() as d:
            write_ap(d, "checkout")
            events.append("checkout", "a", {"n": 1})
            off = events.append("checkout", "b", {"n": 2})
            got, new = events.since("checkout", 0)
            self.assertEqual([e["type"] for e in got], ["a", "b"])
            self.assertEqual(new, off)
            got2, _ = events.since("checkout", off)
            self.assertEqual(got2, [])

    def test_required_langs(self):
        self.assertEqual(text.required_langs({"reader_lang": "en"}), {"en"})
        self.assertEqual(text.required_langs({"reader_lang": "zh"}), {"en", "zh"})


if __name__ == "__main__":
    unittest.main()
