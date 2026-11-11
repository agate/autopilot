"""Card files and restore points nested too deeply for Python's JSON reader are damaged files, not crashes.

`json.loads` raises RecursionError, not ValueError, on JSON nested deeper than it can follow. The store and the
restore points turn that into the CardError every caller already handles. Each deep file is built as text (never by
recursion here), and deep enough to fail on every Python the CI runs; the recursion limit is never changed. Every
test uses a temp folder and an injected clock.
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT  # noqa: F401  (puts lib/ on sys.path)
from assistant import cards, gate, restore
from assistant.cards import CardError
from assistant.config import Config
from assistant.store import Store

UTC = timezone.utc
T0 = datetime(2026, 11, 10, 14, 30, 5, tzinfo=UTC)
# Far past any limit: CPython 3.9-3.13 stop near 1,000 or 10,000 levels; 3.14 stops when the C stack runs out.
DEPTH = 1_000_000
DEEP = "[" * DEPTH + "]" * DEPTH
TOO_DEEP = RecursionError("maximum recursion depth exceeded while encoding a JSON object")


def too_deep_to_write(which=lambda card: True):
    """Make `json.dumps` in `cards` raise the RecursionError a value too deep raises, for the cards (a dict with
    `plain`) that `which` picks; `by`, `did` and search text still check as usual."""
    real = json.dumps

    def dumps(obj, *a, **kw):
        if isinstance(obj, dict) and "plain" in obj and which(obj):
            raise TOO_DEEP
        return real(obj, *a, **kw)

    return mock.patch("assistant.cards.json.dumps", side_effect=dumps)


def deep_card_text(card: dict) -> str:
    """The card as JSON with one more field, `x`, nested too deeply to read."""
    return json.dumps(card)[:-1] + ', "x": ' + DEEP + "}"


def plain(what="vendor export runs nightly"):
    return {"what": {"en": what}, "where": {"en": "on the build host"}, "do": {"en": "nothing yet"}}


def card(**over):
    c = {"kind": "code", "lane": "planned", "feed": "jira-epic", "links": [], "pri": "P2", "origin": "source",
         "plain": plain(), "title": "Vendor export"}
    c.update(over)
    return c


class Clock:
    def __init__(self, at=T0):
        self.at = at

    def __call__(self):
        return self.at

    def tick(self, **delta):
        self.at = self.at + timedelta(**delta)
        return self.at


class DeepCase(unittest.TestCase):
    def setUp(self):
        self.data = Path(tempfile.mkdtemp(prefix="ap2-deep-")).resolve()
        self.addCleanup(shutil.rmtree, self.data, True)
        self.clock = Clock()
        self.store = Store(self.data, cfg=Config(), clock=self.clock)

    def points(self) -> list:
        return sorted(p.name for p in (self.data / "restore").glob("r-*.json"))

    def files(self) -> dict:
        return {p: p.read_bytes() for p in sorted(self.data.glob("**/*.json"))}

    def card_with_point(self):
        """A card changed once, so it has one restore point; returns (card id, point id, card file)."""
        added = self.store.add(card(), by="w")
        self.clock.tick(minutes=1)
        point = self.store.update(added["id"], {"lane": "done"}, by="w")["audit"][-1]["restore"]
        return added["id"], point, self.store.path(added["id"])


# -- criterion 1: a card file too deep to parse ---------------------------------------------------------------------

class CardTooDeepToParseTest(DeepCase):
    def test_every_call_refuses_it_naming_the_file_and_writes_no_restore_point(self):
        card_id, point, path = self.card_with_point()
        path.write_text(deep_card_text(self.store.get(card_id)), encoding="utf-8")
        want = re.escape(f"{path}: cannot read the card: nested too deeply")
        calls = {"get": lambda: self.store.get(card_id),
                 "list": lambda: self.store.list(),
                 "search": lambda: self.store.search("vendor"),
                 "update": lambda: self.store.update(card_id, {"lane": "running"}, by="w"),
                 "propose": lambda: self.store.propose_opposite(card_id, "close_pr", ["gh", "pr"], by="w"),
                 "restore": lambda: self.store.restore(point, by="user")}
        for name, call in calls.items():
            with self.subTest(name):
                before = self.points()
                with self.assertRaisesRegex(CardError, want):
                    call()
                self.assertEqual(self.points(), before)  # no orphan restore point
        with self.subTest("search after index.sqlite is deleted"):
            (self.data / "index.sqlite").unlink()
            with self.assertRaisesRegex(CardError, want):
                self.store.search("vendor")  # met by the rebuild

    def test_the_whole_file_one_deep_list_is_refused_too(self):
        card_id, _, path = self.card_with_point()
        path.write_text(DEEP, encoding="utf-8")
        with self.assertRaisesRegex(CardError, re.escape(f"{path}: cannot read the card: nested too deeply")):
            self.store.get(card_id)

    def test_a_card_with_a_deep_field_fails_the_add_check_as_a_card_error(self):
        # The same refusal on the way in: a value JSON cannot write is refused before anything is written.
        before = self.files()
        with too_deep_to_write():
            with self.assertRaisesRegex(CardError, r"\Atext is not valid JSON: nested too deeply\Z"):
                self.store.add(card(), by="w")
        self.assertEqual(self.files(), before)


# -- criterion 2: a card file that parses but is too deep to check -------------------------------------------------

class CardTooDeepToCheckTest(DeepCase):
    """No depth makes `json.loads` pass and the check's `json.dumps` fail on every Python (both stop at the same
    depth on 3.12 and 3.13), so `json.dumps` in `cards` is made to raise the RecursionError it raises there."""

    def test_check_unicode_turns_the_recursion_error_into_a_card_error(self):
        with mock.patch("assistant.cards.json.dumps", side_effect=TOO_DEEP):
            with self.assertRaisesRegex(CardError, r"\Acard is not valid JSON: nested too deeply\Z"):
                cards.check_unicode({"x": []}, "card")

    def test_every_read_refuses_it_naming_the_file(self):
        card_id, point, path = self.card_with_point()
        want = re.escape(f"{path}: not a card file: ") + ".*nested too deeply"
        calls = {"get": lambda: self.store.get(card_id),
                 "list": lambda: self.store.list(),
                 "search": lambda: self.store.search("vendor"),
                 "update": lambda: self.store.update(card_id, {"lane": "running"}, by="w")}
        for name, call in calls.items():
            with self.subTest(name):
                before = self.files()
                with too_deep_to_write():
                    with self.assertRaisesRegex(CardError, want) as cm:
                        call()
                self.assertIsInstance(cm.exception, cards.TooDeepError)  # the type survives the re-raise
                self.assertEqual(self.files(), before)

    def test_a_point_whose_card_is_too_deep_to_check_is_refused_naming_the_point(self):
        _, point, _ = self.card_with_point()
        point_path = restore.path(self.data, point)
        before = self.files()
        # Only the point's card (lane planned) is too deep: the current card file (lane done) still checks.
        with too_deep_to_write(lambda c: c.get("lane") == "planned"):
            with self.assertRaisesRegex(CardError, re.escape(f"{point_path}: not a card restore point: ")
                                        + ".*nested too deeply"):
                self.store.restore(point, by="user")
        self.assertEqual(self.files(), before)


# -- a card file that parses but has a wrong-shaped field too deep to print -----------------------------------------

class TooDeepToPrint:
    """A value whose repr raises the RecursionError a value nested too deeply raises (on 3.14, `json.loads` reads
    deeper than `repr` can print)."""

    def __repr__(self):
        raise RecursionError("Stack overflow while getting the repr of an object")


class FieldTooDeepToPrintTest(DeepCase):
    def test_check_stored_turns_the_recursion_error_into_a_too_deep_error(self):
        for field, rest in {"audit": {"plain": {}},
                            "plain": {"audit": []},
                            "ask": {"audit": [], "plain": {}}}.items():
            with self.subTest(field):
                with self.assertRaisesRegex(cards.TooDeepError, rf"\A{field} must be .*nested too deeply\Z"):
                    cards.check_stored({**rest, field: TooDeepToPrint()})

    def test_a_card_file_with_a_deep_audit_object_is_refused_naming_the_file(self):
        card_id = "c-20261101-000000-abcdef"
        path = self.store.path(card_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Depending on the Python and its stack, this fails at parse, at the check's repr (TooDeepError), or is a
        # plain "not a card file" CardError. In every case it is a CardError naming the file, never a RecursionError.
        depth = 25_000
        path.write_text('{"id": "%s", "audit": ' % card_id + '{"a":' * depth + "1" + "}" * depth + "}",
                        encoding="utf-8")
        with self.assertRaisesRegex(CardError, r"\A" + re.escape(f"{path}: ")):
            self.store.get(card_id)


# -- criterion 3: a restore point too deep to parse ----------------------------------------------------------------

class PointTooDeepTest(DeepCase):
    def test_load_and_restore_refuse_it_naming_the_file(self):
        card_id, point, _ = self.card_with_point()
        point_path = restore.path(self.data, point)
        point_path.write_text(DEEP, encoding="utf-8")
        want = re.escape(f"{point_path}: cannot read the restore point: nested too deeply")
        with self.assertRaisesRegex(CardError, want):
            restore.load(self.data, point)
        before = self.files()
        with self.assertRaisesRegex(CardError, want):
            self.store.restore(point, by="user")
        self.assertEqual(self.files(), before)
        self.assertEqual(self.store.get(card_id)["lane"], "done")

    def test_prune_keeps_it_and_still_removes_the_other_old_points(self):
        card_id, first, _ = self.card_with_point()
        self.clock.tick(minutes=1)
        second = self.store.update(card_id, {"lane": "running"}, by="w")["audit"][-1]["restore"]
        restore.path(self.data, first).write_text(DEEP, encoding="utf-8")
        later = self.clock.tick(days=31)
        self.assertEqual(restore.prune(self.data, later), [second])
        self.assertEqual(self.points(), [f"{first}.json"])
        self.assertEqual(self.store.prune(later), [])  # through the store too, and again kept
        self.assertEqual(self.points(), [f"{first}.json"])


# -- criterion 4: the gate survives both ---------------------------------------------------------------------------

class GateDeepTest(unittest.TestCase):
    def setUp(self):
        self.data = Path(tempfile.mkdtemp(prefix="ap2-deep-gate-")).resolve()
        self.addCleanup(shutil.rmtree, self.data, True)
        (self.data / "routines").mkdir()
        (self.data / "config.yaml").write_text("", encoding="utf-8")
        self.routine = self.data / "routines" / "digest.json"
        self.routine.write_text(json.dumps(
            {"title": "Daily digest", "schedule": {"days": "daily", "at": ["09:00"]}, "tz": "UTC",
             "missed_run": "once_within: 3h", "last_slot": "2026-11-09T09:00", "paused": False}), encoding="utf-8")

    def gate_json(self) -> dict:
        return json.loads((self.data / "gate.json").read_text(encoding="utf-8"))

    def test_a_due_routine_and_a_card_file_nested_too_deeply_is_one_routine_error(self):
        bad = self.data / "items" / "2026-11" / "c-20261101-000000-abcdef.json"
        bad.parent.mkdir(parents=True)
        bad.write_text(DEEP, encoding="utf-8")
        out = gate.run(self.data, datetime(2026, 11, 10, 9, 1, tzinfo=UTC))
        self.assertEqual(out["routines"], [])
        routine_errors = [e for e in out["errors"] if "routine" in e]
        self.assertEqual(routine_errors, [{"routine": "digest", "file": str(self.routine),
                                           "error": f"{bad}: cannot read the card: nested too deeply"}])
        self.assertEqual(sorted(p.name for p in (self.data / "items").glob("*/*.json")), [bad.name])  # no card
        self.assertEqual(json.loads(self.routine.read_text(encoding="utf-8"))["last_slot"], "2026-11-09T09:00")
        self.assertEqual(self.gate_json(), out)

    def test_a_restore_point_nested_too_deeply_is_kept_and_the_run_goes_on(self):
        point = self.data / "restore" / "r-20261001-000000-abcdef.json"
        point.parent.mkdir()
        point.write_text(DEEP, encoding="utf-8")
        out = gate.run(self.data, datetime(2026, 11, 10, 9, 1, tzinfo=UTC))
        self.assertEqual(out["pruned"], [])
        self.assertTrue(point.exists())
        self.assertEqual([(f["routine"], f["action"]) for f in out["routines"]], [("digest", "run")])
        self.assertEqual(self.gate_json(), out)


if __name__ == "__main__":
    unittest.main()
