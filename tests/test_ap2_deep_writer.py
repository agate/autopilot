"""Card files, restore points and input nested too deeply for the store to copy or write.

`json.loads` and the check's `json.dumps` (no indent) run in C and follow a value much deeper than `copy.deepcopy`
and `json.dump(..., indent=2)`, which are pure Python on 3.12 and 3.13 and stop at the recursion limit. Where they
stop depends on how deep the caller's stack already is, so the store does not try them: it refuses any card, card
file, restore point or `changes` whose dicts and lists nest past MAX_DEPTH (the card itself is depth 1), with a
TooDeepError naming its file, before anything is written. That bound is far under where the copy and the writer
stop, so what one call writes every other call reads, however deep its stack.

DEPTH parses and checks on 3.9-3.14 and breaks `copy.deepcopy` on each (about 500 levels at the default recursion
limit). Each deep value is built as text (never by recursion here), the recursion limit is never changed, and every
test uses a temp folder and an injected clock. Extra stack is added by calling through plain frames.
"""
from __future__ import annotations

import copy
import json
import re
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT  # noqa: F401  (puts lib/ on sys.path)
from tests.test_ap2_deep_files import DeepCase, card
from assistant import cards, gate, restore, store
from assistant.cards import CardError
from assistant.store import MAX_DEPTH, check_writable

UTC = timezone.utc
DEPTH = 800
DEEP = "[" * DEPTH + "]" * DEPTH
FRAMES = 300  # extra stack a deeper caller has


def lists(n: int) -> str:
    """JSON text of `n` lists, each inside the one before."""
    return "[" * n + "]" * n


def deep_value():
    return json.loads(DEEP)


def with_x(c: dict, text: str) -> str:
    """The card as JSON with one more field, `x`, holding the JSON `text`."""
    return json.dumps(c)[:-1] + ', "x": ' + text + "}"


def deeper(frames: int, call):
    """`call()`, run `frames` Python frames deeper than the caller."""
    return call() if frames <= 0 else deeper(frames - 1, call)


def cut_short(n: int) -> list:
    """`[A, B]` with A = [S, L], B = [S], S = [X], X = [A] (a cycle) and L `n` nested lists. The walk meets S first
    from A, where X's back edge to A stops it; from B, A is off the path, so S's deepest simple path runs on through
    A into L: top, B, S, X, A, L, 5 + n lists deep (the top is depth 1). Through A alone it is only 2 + n."""
    s, x = [], []
    a, b = [s, json.loads(lists(n))], [s]
    s.append(x)
    x.append(a)
    return [a, b]


# The card and the `changes` holding `x` are depth 1, so `x` holds one level fewer than the bound.
AT_BOUND = lists(MAX_DEPTH - 1)
PAST_BOUND = lists(MAX_DEPTH)


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

    def point_with_x(self, point, text):
        """Put a field `x` holding the JSON `text` into the card of restore point `point`; returns its file."""
        point_path = restore.path(self.data, point)
        saved = json.loads(point_path.read_text(encoding="utf-8"))
        point_path.write_text(json.dumps(saved).replace('"card": {', '"card": {"x": ' + text + ", ", 1),
                              encoding="utf-8")
        self.assertIn("x", restore.load(self.data, point)["card"])  # the point still loads
        return point_path

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


# -- criteria 2 and 3: restore writes no orphan point --------------------------------------------------------------

class RestoreTest(WriterCase):
    def test_a_point_whose_card_is_too_deep_to_copy_is_refused_naming_the_point(self):
        card_id, point, path = self.card_with_point()
        point_path = self.point_with_x(point, DEEP)
        points, card_file = self.points(), path.read_bytes()
        self.assert_refused(lambda: self.store.restore(point, by="user"),
                            f"{point_path}: not a card restore point: card is nested too deeply to copy or write")
        self.assertEqual(self.points(), points)  # no orphan point in restore/
        self.assertEqual(path.read_bytes(), card_file)
        self.assertEqual(self.store.get(card_id)["lane"], "done")

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

    def test_changes_json_cannot_write_are_still_refused_by_the_card_check(self):
        card_id, _, _ = self.card_with_point()
        before = self.files()
        with self.assertRaisesRegex(CardError, r"\Atext is not JSON: "):
            self.store.update(card_id, {"x": {1, 2}}, by="w")
        self.assertEqual(self.files(), before)


# -- the bound: MAX_DEPTH is written and read, one level more is refused ------------------------------------------

class BoundTest(WriterCase):
    def test_the_design_states_200(self):
        self.assertEqual(MAX_DEPTH, 200)

    def test_a_card_at_the_bound_is_written_and_read_by_every_call(self):
        added = self.store.add(card(x=json.loads(AT_BOUND)), by="w")
        self.assertEqual(self.store.get(added["id"]), added)
        self.assertEqual(self.store.list(), [added])
        self.assertEqual(self.store.search("vendor"), [added])
        (self.data / "index.sqlite").unlink()
        self.assertEqual(self.store.search("vendor"), [added])  # the rebuild reads it too
        self.clock.tick(minutes=1)
        changed = self.store.update(added["id"], {"lane": "done"}, by="w")
        self.assertEqual(changed["x"], added["x"])
        self.clock.tick(minutes=1)
        back = self.store.restore(changed["audit"][-1]["restore"], by="user")
        self.assertEqual((back["lane"], back["x"]), ("planned", added["x"]))
        self.assertEqual(self.store.get(added["id"]), back)

    def test_changes_at_the_bound_are_written(self):
        card_id, _, _ = self.card_with_point()
        self.clock.tick(minutes=1)
        changed = self.store.update(card_id, {"x": json.loads(AT_BOUND)}, by="w")
        self.assertEqual(changed["x"], json.loads(AT_BOUND))
        self.assertEqual(self.store.get(card_id), changed)

    def test_a_point_at_the_bound_is_restored(self):
        card_id, point, _ = self.card_with_point()
        self.point_with_x(point, AT_BOUND)
        self.clock.tick(minutes=1)
        back = self.store.restore(point, by="user")
        self.assertEqual(back["x"], json.loads(AT_BOUND))
        self.assertEqual(self.store.get(card_id), back)

    def test_one_level_past_the_bound_is_refused_and_writes_nothing(self):
        card_id, point, path = self.card_with_point()
        with self.subTest("add"):
            self.assert_refused(lambda: self.store.add(card(x=json.loads(PAST_BOUND)), by="w"),
                                "card is nested too deeply to copy or write")
        with self.subTest("add, nested tuples"):
            nested = ()
            for _ in range(250):
                nested = (nested,)
            self.assert_refused(lambda: self.store.add(card(x=nested), by="w"),
                                "card is nested too deeply to copy or write")
        with self.subTest("update"):
            self.assert_refused(lambda: self.store.update(card_id, {"x": json.loads(PAST_BOUND)}, by="w"),
                                "changes are nested too deeply to copy or write")
        with self.subTest("restore point"):
            point_path = self.point_with_x(point, PAST_BOUND)
            self.assert_refused(lambda: self.store.restore(point, by="user"),
                                f"{point_path}: not a card restore point: card is nested too deeply to copy or "
                                f"write")
        path.write_text(with_x(self.store.get(card_id), PAST_BOUND), encoding="utf-8")
        want = f"{path}: not a card file: card is nested too deeply to copy or write"
        for name, call in self.calls(card_id).items():
            with self.subTest(f"card file, {name}"):
                self.assert_refused(call, want)
        with self.subTest("card file, search after index.sqlite is deleted"):
            (self.data / "index.sqlite").unlink()
            self.assert_refused(lambda: self.store.search("vendor"), want)


# -- the same answer whatever the caller's stack -------------------------------------------------------------------

class StackTest(WriterCase):
    def test_what_add_writes_every_read_reads_from_a_deeper_stack(self):
        added = self.store.add(card(x=json.loads(AT_BOUND)), by="w")
        reads = {"get": lambda: [self.store.get(added["id"])],
                 "list": lambda: self.store.list(),
                 "search": lambda: self.store.search("vendor")}
        for name, call in reads.items():
            with self.subTest(name):
                self.assertEqual(deeper(FRAMES, call), [added])
        with self.subTest("search after index.sqlite is deleted"):
            (self.data / "index.sqlite").unlink()
            self.assertEqual(deeper(FRAMES, reads["search"]), [added])

    def test_add_update_and_restore_at_the_bound_work_from_a_deeper_stack(self):
        with self.subTest("add"):
            added = deeper(FRAMES, lambda: self.store.add(card(x=json.loads(AT_BOUND)), by="w"))
            self.assertEqual(self.store.get(added["id"]), added)
        card_id, point, _ = self.card_with_point()
        with self.subTest("update"):
            self.clock.tick(minutes=1)
            changed = deeper(FRAMES, lambda: self.store.update(card_id, {"x": json.loads(AT_BOUND)}, by="w"))
            self.assertEqual(changed["x"], json.loads(AT_BOUND))
            self.assertEqual(self.store.get(card_id), changed)
        with self.subTest("restore"):
            self.point_with_x(point, AT_BOUND)
            self.clock.tick(minutes=1)
            back = deeper(FRAMES, lambda: self.store.restore(point, by="user"))
            self.assertEqual(back["x"], json.loads(AT_BOUND))
            self.assertEqual(self.store.get(card_id), back)

    def test_the_check_gives_the_same_answer_at_any_stack(self):
        at, past = json.loads(with_x(card(), AT_BOUND)), json.loads(with_x(card(), PAST_BOUND))
        for frames in (0, FRAMES):
            with self.subTest(frames=frames, depth=MAX_DEPTH):
                self.assertIsNone(deeper(frames, lambda: check_writable(at)))
            with self.subTest(frames=frames, depth=MAX_DEPTH + 1):
                with self.assertRaisesRegex(cards.TooDeepError, r"\Acard is nested too deeply to copy or write\Z"):
                    deeper(frames, lambda: check_writable(past))


# -- propose_opposite and cycles go through the same check ---------------------------------------------------------

class ProposeTest(WriterCase):
    def test_propose_opposite_refuses_a_text_too_deep(self):
        card_id, _, _ = self.card_with_point()
        # `changes` is {"ask": {..., "text": <text>}}, two levels above the text.
        for name, text in {"one level past the bound": lists(MAX_DEPTH - 1), "too deep to copy": DEEP}.items():
            with self.subTest(name):
                self.assert_refused(
                    lambda: self.store.propose_opposite(card_id, "close_pr", ["gh", "pr", "close"], by="w",
                                                        text=json.loads(text)),
                    "changes are nested too deeply to copy or write")
        self.assertIsNone(self.store.get(card_id).get("ask"))


class CycleTest(WriterCase):
    def test_cyclic_changes_are_left_to_the_card_check_as_before(self):
        card_id, _, _ = self.card_with_point()
        d = {}
        d["x"] = d
        loop = []
        loop.append(loop)
        self.assertIsNone(check_writable({"x": d}))
        self.assertIsNone(check_writable(loop))
        before = self.files()
        with self.assertRaises(CardError) as cm:
            self.store.update(card_id, {"x": d}, by="w")
        self.assertNotIsInstance(cm.exception, RecursionError)
        self.assertEqual(str(cm.exception), "text is not valid JSON: NaN or Infinity")  # the check's words, as before
        self.assertEqual(self.files(), before)

    def test_a_shared_value_cut_short_by_a_cycle_is_counted_along_its_other_route(self):
        # Via A, S's walk stops at X's back edge to A; via B, A is off the path, so S goes on through A into L.
        for name, value in {"top": cut_short(MAX_DEPTH - 4), "inside a list": [cut_short(MAX_DEPTH - 5)]}.items():
            with self.subTest(name):
                with self.assertRaisesRegex(cards.TooDeepError, r"\Avalue is nested too deeply to copy or write\Z"):
                    check_writable(value, "value is")
        self.assert_refused(lambda: self.store.add(card(x=cut_short(MAX_DEPTH - 5)), by="w"),
                            "card is nested too deeply to copy or write")
        self.assertEqual(self.files(), {})

    def test_the_same_shape_at_the_bound_passes(self):
        self.assertIsNone(check_writable(cut_short(MAX_DEPTH - 5)))  # deepest simple path: 200
        self.assertIsNone(check_writable([cut_short(MAX_DEPTH - 6)]))

    def test_a_cycle_with_more_paths_than_the_budget_is_refused_fast(self):
        top = bottom = [1]
        for _ in range(40):  # each level holds the one below twice: 2**40 paths, 41 lists
            top = [top, top]
        bottom.append(top)  # one back edge from the bottom to the top
        start = time.monotonic()
        with self.assertRaisesRegex(cards.TooDeepError, r"\Acard is nested too deeply to copy or write\Z"):
            check_writable(top)
        self.assertLess(time.monotonic() - start, 2)

    def test_a_cycle_through_wide_containers_is_refused_fast(self):
        filler = [0] * 1000
        top = bottom = list(filler)
        for _ in range(20):  # each level holds the one below twice and 1000 numbers: the budget caps items, not pushes
            top = [top, top] + filler
        bottom.append(top)  # one back edge from the bottom to the top
        start = time.monotonic()
        with self.assertRaisesRegex(cards.TooDeepError, r"\Acard is nested too deeply to copy or write\Z"):
            check_writable(top)
        self.assertLess(time.monotonic() - start, 2)

    def test_the_budget_counts_only_items_looked_at_after_the_first_cycle(self):
        d = {}
        d["x"] = d
        loop = []
        loop.append(loop)
        before = [1]
        for _ in range(40):
            before = [before, before]  # walked with the memo: the cycle comes after it
        with mock.patch.object(store, "CYCLE_WALK_BUDGET", 0):
            self.assertIsNone(check_writable({"x": d}))
            self.assertIsNone(check_writable(loop))
            self.assertIsNone(check_writable([before, loop]))
            with self.assertRaises(cards.TooDeepError):
                check_writable(cut_short(2))  # after the cycle at X, L and the route via B are walked
        # items looked at after the cycle at X: in A, L and L's []; then B, and via B: S, X, A, S (on the path), L, []
        with mock.patch.object(store, "CYCLE_WALK_BUDGET", 9):
            self.assertIsNone(check_writable(cut_short(2)))
        with mock.patch.object(store, "CYCLE_WALK_BUDGET", 8):
            with self.assertRaisesRegex(cards.TooDeepError, r"\Acard is nested too deeply to copy or write\Z"):
                check_writable(cut_short(2))
        # after the cycle at d, both the key "b" and the value 1 count: two items
        with mock.patch.object(store, "CYCLE_WALK_BUDGET", 2):
            self.assertIsNone(check_writable({"a": d, "b": 1}))
        with mock.patch.object(store, "CYCLE_WALK_BUDGET", 1):
            with self.assertRaises(cards.TooDeepError):
                check_writable({"a": d, "b": 1})

    def test_the_budget_is_a_fixed_constant(self):
        self.assertEqual(store.CYCLE_WALK_BUDGET, 100_000)


class SharedTest(WriterCase):
    def test_a_list_met_twice_counts_where_it_is_deepest(self):
        shared = json.loads(lists(150))
        wrap = json.loads(lists(100))
        inner = wrap
        while inner:
            inner = inner[0]
        inner.append(shared)  # `shared` is 3 levels deep in the card first, then 103
        self.assert_refused(lambda: self.store.add(card(x=[shared, wrap]), by="w"),
                            "card is nested too deeply to copy or write")
        self.assertEqual(self.files(), {})

    def test_a_value_shared_on_every_level_is_walked_once_per_depth(self):
        card_id, _, _ = self.card_with_point()
        ints, bad = [1], [float("nan")]
        for _ in range(40):  # each level holds the one below twice: 2**40 paths, 41 lists
            ints, bad = [ints, ints], [bad, bad]
        before = self.files()
        start = time.monotonic()
        self.assertIsNone(check_writable(ints))
        with self.assertRaisesRegex(CardError, r"\Atext is not valid JSON: NaN or Infinity\Z") as cm:
            self.store.update(card_id, {"x": bad}, by="w")
        self.assertLess(time.monotonic() - start, 2)  # walking every path would not end
        self.assertNotIsInstance(cm.exception, cards.TooDeepError)
        self.assertEqual(self.files(), before)

    def test_a_shared_value_one_level_deeper_the_second_time_is_refused(self):
        shared = json.loads(lists(MAX_DEPTH - 2))  # 3 levels deep in the card first (deepest list 200), then 4
        self.assert_refused(lambda: self.store.add(card(x=[shared, [shared]]), by="w"),
                            "card is nested too deeply to copy or write")
        self.assertEqual(self.files(), {})

    def test_a_shared_value_met_twice_at_the_same_depth_is_written_and_read(self):
        shared = json.loads(lists(MAX_DEPTH - 2))  # 3 levels deep in the card both times: deepest list 200
        added = self.store.add(card(x=[shared, shared]), by="w")
        self.assertEqual(self.store.get(added["id"])["x"], [shared, shared])

    def test_only_values_from_python_callers_are_walked_with_the_memo(self):
        card_id, point, _ = self.card_with_point()
        self.clock.tick(minutes=1)
        with mock.patch("assistant.store.check_writable", wraps=store.check_writable) as check:
            self.store.get(card_id)
            self.assertEqual([c.kwargs.get("shared", True) for c in check.call_args_list], [False])  # _read
            check.reset_mock()
            self.store.add(card(), by="w")
            self.assertTrue(check.call_args_list)
            self.assertTrue(all(c.kwargs.get("shared", True) for c in check.call_args_list))  # memo on
            check.reset_mock()
            self.store.update(card_id, {"lane": "running"}, by="w")
            self.assertIn(mock.call({"lane": "running"}, "changes are"), check.call_args_list)  # memo on
            check.reset_mock()
            self.clock.tick(minutes=1)
            self.store.restore(point, by="user")
            self.assertTrue(check.call_args_list)
            self.assertTrue(all(c.kwargs.get("shared") is False for c in check.call_args_list))  # the point, _read


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
