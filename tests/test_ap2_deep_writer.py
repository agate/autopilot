"""Card files, restore points and input that parse and check but are too deep for the store to copy or write.

`json.loads` and the check's `json.dumps` (no indent) run in C and follow a value much deeper than `copy.deepcopy`
and `json.dump(..., indent=2)`, which are pure Python on 3.12 and 3.13 and stop at the recursion limit. So a card
can be read and checked, and then break the store's copy or its writer with a bare RecursionError. The store now
refuses such a card where it first meets it, with a TooDeepError naming its file, before anything is written.

DEPTH parses and checks on 3.9-3.14 and breaks `copy.deepcopy` on each (about 500 levels at the default recursion
limit). No depth breaks only the indented writer (the copy always stops first), so that half is proven by making
`json.dumps` in the store raise the RecursionError it raises there. Each deep value is built as text (never by
recursion here), the recursion limit is never changed, and every test uses a temp folder and an injected clock.
"""
from __future__ import annotations

import copy
import json
import re
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT  # noqa: F401  (puts lib/ on sys.path)
from tests.test_ap2_deep_files import DeepCase, card
from assistant import cards, gate, restore
from assistant.cards import CardError

UTC = timezone.utc
DEPTH = 800
DEEP = "[" * DEPTH + "]" * DEPTH
MARK = "too deep to copy or write"  # the field value the patched copy or writer refuses
TOO_DEEP = RecursionError("maximum recursion depth exceeded")


def deep_value():
    return json.loads(DEEP)


def with_x(c: dict, text: str) -> str:
    """The card as JSON with one more field, `x`, holding the JSON `text`."""
    return json.dumps(c)[:-1] + ', "x": ' + text + "}"


def marked(obj) -> bool:
    return isinstance(obj, dict) and obj.get("x") == MARK


def copy_breaks():
    """`copy.deepcopy` raises the RecursionError a value too deep raises, for a dict whose `x` is MARK."""
    real = copy.deepcopy

    def deepcopy(obj, *a, **kw):
        if marked(obj):
            raise TOO_DEEP
        return real(obj, *a, **kw)

    return mock.patch("assistant.store.copy.deepcopy", side_effect=deepcopy)


def writer_breaks():
    """The indented `json.dumps` raises the RecursionError a value too deep raises, for a dict whose `x` is MARK;
    the checks' unindented dumps still run as usual."""
    real = json.dumps

    def dumps(obj, *a, **kw):
        if kw.get("indent") is not None and marked(obj):
            raise TOO_DEEP
        return real(obj, *a, **kw)

    return mock.patch("assistant.store.json.dumps", side_effect=dumps)


BREAKS = {"copy": copy_breaks, "indented writer": writer_breaks}


class DepthTest(unittest.TestCase):
    def test_the_depth_parses_and_checks_but_breaks_the_copy(self):
        value = json.loads(with_x(card(audit=[]), DEEP))  # what the store reads
        cards.check_stored(value)  # and checks: no error
        with self.assertRaises(RecursionError):
            copy.deepcopy(value)


class WriterCase(DeepCase):
    def deep_card_file(self):
        """A card with a restore point whose file now holds a field `x` too deep to copy; returns (card id, point
        id, card file)."""
        card_id, point, path = self.card_with_point()
        path.write_text(with_x(self.store.get(card_id), DEEP), encoding="utf-8")
        return card_id, point, path

    def calls(self, card_id):
        return {"get": lambda: self.store.get(card_id),
                "list": lambda: self.store.list(),
                "update": lambda: self.store.update(card_id, {"lane": "running"}, by="w"),
                "search": lambda: self.store.search("vendor")}

    def assert_refused(self, call, want):
        """`call()` raises a TooDeepError whose message is `want`, and writes nothing."""
        before = self.files()
        with self.assertRaises(cards.TooDeepError) as cm:
            call()
        self.assertRegex(str(cm.exception), r"\A" + re.escape(want) + r"\Z")
        self.assertIsInstance(cm.exception, CardError)
        self.assertEqual(self.files(), before)  # no restore point, every card file unchanged


# -- criterion 1: read refuses a card too deep to copy or write ---------------------------------------------------

class ReadTest(WriterCase):
    def test_every_read_refuses_a_card_file_too_deep_to_copy(self):
        card_id, _, path = self.deep_card_file()
        want = f"{path}: not a card file: card is nested too deeply to copy or write"
        for name, call in self.calls(card_id).items():
            with self.subTest(name):
                self.assert_refused(call, want)
        with self.subTest("search after index.sqlite is deleted"):
            (self.data / "index.sqlite").unlink()
            self.assert_refused(lambda: self.store.search("vendor"), want)  # met by the rebuild

    def test_every_read_refuses_a_card_file_the_copy_or_the_indented_writer_breaks_on(self):
        card_id, _, path = self.card_with_point()
        path.write_text(with_x(self.store.get(card_id), json.dumps(MARK)), encoding="utf-8")
        want = f"{path}: not a card file: card is nested too deeply to copy or write"
        for broken, breaks in BREAKS.items():
            for name, call in self.calls(card_id).items():
                with self.subTest(broken=broken, call=name):
                    with breaks():
                        self.assert_refused(call, want)


# -- criteria 2 and 3: restore writes no orphan point --------------------------------------------------------------

class RestoreTest(WriterCase):
    def point_with_x(self, point, text):
        point_path = restore.path(self.data, point)
        saved = json.loads(point_path.read_text(encoding="utf-8"))
        point_path.write_text(json.dumps(saved).replace('"card": {', '"card": {"x": ' + text + ", ", 1),
                              encoding="utf-8")
        self.assertIn("x", restore.load(self.data, point)["card"])  # the point still loads
        return point_path

    def test_a_point_whose_card_is_too_deep_to_copy_is_refused_naming_the_point(self):
        card_id, point, path = self.card_with_point()
        point_path = self.point_with_x(point, DEEP)
        points, card_file = self.points(), path.read_bytes()
        self.assert_refused(lambda: self.store.restore(point, by="user"),
                            f"{point_path}: not a card restore point: card is nested too deeply to copy or write")
        self.assertEqual(self.points(), points)  # no orphan point in restore/
        self.assertEqual(path.read_bytes(), card_file)
        self.assertEqual(self.store.get(card_id)["lane"], "done")

    def test_a_point_the_copy_or_the_indented_writer_breaks_on_is_refused_naming_the_point(self):
        _, point, _ = self.card_with_point()
        point_path = self.point_with_x(point, json.dumps(MARK))
        for broken, breaks in BREAKS.items():
            with self.subTest(broken):
                with breaks():
                    self.assert_refused(lambda: self.store.restore(point, by="user"),
                                        f"{point_path}: not a card restore point: card is nested too deeply to copy "
                                        f"or write")

    def test_restore_over_a_current_card_too_deep_to_copy_writes_no_point(self):
        _, point, path = self.deep_card_file()
        points = self.points()
        self.assert_refused(lambda: self.store.restore(point, by="user"),
                            f"{path}: not a card file: card is nested too deeply to copy or write")
        self.assertEqual(self.points(), points)


# -- criterion 4: add and update refuse deep input before writing --------------------------------------------------

class InputTest(WriterCase):
    def test_add_refuses_a_card_with_a_field_too_deep_to_copy(self):
        self.assert_refused(lambda: self.store.add(card(x=deep_value()), by="w"),
                            "card is nested too deeply to copy or write")
        self.assertEqual(self.files(), {})

    def test_update_refuses_changes_too_deep_to_copy(self):
        card_id, _, path = self.card_with_point()
        self.assert_refused(lambda: self.store.update(card_id, {"x": deep_value()}, by="w"),
                            "changes are nested too deeply to copy or write")
        self.assertEqual(self.store.get(card_id)["lane"], "done")

    def test_add_and_update_refuse_input_the_copy_or_the_indented_writer_breaks_on(self):
        card_id, _, _ = self.card_with_point()
        for broken, breaks in BREAKS.items():
            with self.subTest(broken=broken, call="add"):
                with breaks():
                    self.assert_refused(lambda: self.store.add(card(x=MARK), by="w"),
                                        "card is nested too deeply to copy or write")
            with self.subTest(broken=broken, call="update"):
                with breaks():
                    self.assert_refused(lambda: self.store.update(card_id, {"x": MARK}, by="w"),
                                        "changes are nested too deeply to copy or write")

    def test_changes_json_cannot_write_are_still_refused_by_the_card_check(self):
        card_id, _, _ = self.card_with_point()
        before = self.files()
        with self.assertRaisesRegex(CardError, r"\Atext is not JSON: "):
            self.store.update(card_id, {"x": {1, 2}}, by="w")
        self.assertEqual(self.files(), before)


# -- criterion 5: the gate survives --------------------------------------------------------------------------------

class GateTest(unittest.TestCase):
    def setUp(self):
        self.data = Path(tempfile.mkdtemp(prefix="ap2-deep-writer-gate-")).resolve()
        self.addCleanup(shutil.rmtree, self.data, True)
        (self.data / "routines").mkdir()
        (self.data / "config.yaml").write_text("", encoding="utf-8")
        self.routine = self.data / "routines" / "digest.json"
        self.routine.write_text(json.dumps(
            {"title": "Daily digest", "schedule": {"days": "daily", "at": ["09:00"]}, "tz": "UTC",
             "missed_run": "once_within: 3h", "last_slot": "2026-11-09T09:00", "paused": False}), encoding="utf-8")

    def test_a_due_routine_and_a_card_file_too_deep_to_copy_is_one_routine_error(self):
        card_id = "c-20261101-000000-abcdef"
        bad = self.data / "items" / "2026-11" / f"{card_id}.json"
        bad.parent.mkdir(parents=True)
        bad.write_text(with_x({"id": card_id, "audit": [], "plain": {}}, DEEP), encoding="utf-8")
        out = gate.run(self.data, datetime(2026, 11, 10, 9, 1, tzinfo=UTC))
        self.assertEqual(out["routines"], [])
        self.assertEqual([e for e in out["errors"] if "routine" in e],
                         [{"routine": "digest", "file": str(self.routine),
                           "error": f"{bad}: not a card file: card is nested too deeply to copy or write"}])
        self.assertEqual(sorted(p.name for p in (self.data / "items").glob("*/*.json")), [bad.name])  # no card
        self.assertEqual(json.loads(self.routine.read_text(encoding="utf-8"))["last_slot"], "2026-11-09T09:00")
        self.assertEqual(json.loads((self.data / "gate.json").read_text(encoding="utf-8")), out)


if __name__ == "__main__":
    unittest.main()
