"""A card error shows at most 200 characters of a bad value (design 12, the card file bullet).

A hand-edited card can hold a huge or deeply nested value. Its full repr in one error line would land, whole, in
`gate.json`, the routine error and the terminal. So every message in `cards.py` that shows a value shows its repr cut
to the first 200 characters, then `...`. A value whose repr is 200 characters or fewer shows exactly as before. A
value too deep to print at all still gives a TooDeepError naming its field. Deep values are built as JSON text and
read with `json.loads`, never by recursion here; the recursion limit is never changed. Every test uses a temp folder.
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.helpers import ROOT  # noqa: F401  (puts lib/ on sys.path)
from assistant import cards
from assistant.cards import ASK_TYPES, LANES, ORIGINS, PRIS, CardError
from assistant.config import Config
from assistant.store import Store

LONG = "x" * 100_000
LIMIT = 300  # the longest message allowed for a long value: the words around it plus 200 characters and `...`
CARD_ID = "c-20261101-000000-abcdef"


def deep_dict(levels: int):
    """{"a": {"a": ... 1}} nested `levels` deep, read from JSON text."""
    return json.loads('{"a":' * levels + "1" + "}" * levels)


def plain():
    return {"what": {"en": "vendor export runs nightly"}, "where": {"en": "on the build host"},
            "do": {"en": "nothing yet"}}


def card(**over):
    c = {"kind": "code", "lane": "planned", "feed": "jira-epic", "links": [], "pri": "P2", "origin": "source",
         "plain": plain(), "title": "Vendor export"}
    c.update(over)
    return c


class TooDeepToPrint:
    """A value whose repr raises the RecursionError a value nested too deeply raises."""

    def __repr__(self):
        raise RecursionError("Stack overflow while getting the repr of an object")


def message(call) -> str:
    try:
        call()
    except CardError as e:
        return str(e)
    raise AssertionError("no CardError")


# Each message that shows a value, by the field it names: how to make it fail with a given bad value. Each recipe's
# value is wrong for its field whatever it is (a list is never text, a card id, an enum value or an object; text is
# never a list or an object).
def shape(**over):
    return lambda: cards.check_shape(card(**over))


SITES = {
    "card id": lambda v: lambda: cards.check_id([v]),
    "kind": lambda v: shape(kind=[v]),
    "feed": lambda v: shape(feed=[v]),
    "lane": lambda v: shape(lane=[v]),
    "pri": lambda v: shape(pri=[v]),
    "origin": lambda v: shape(origin=[v]),
    "links": lambda v: shape(links={"k": v}),
    "links: a link": lambda v: shape(links=[[v]]),
    "parent": lambda v: shape(parent=[v]),
    "title": lambda v: shape(title=[v]),
    "title.<lang>": lambda v: shape(title={"en": [v]}),
    "ask": lambda v: shape(ask=[v]),
    "ask.type": lambda v: shape(ask={"type": [v]}),
    "plain": lambda v: shape(plain=[v]),
    "stored audit": lambda v: lambda: cards.check_stored({"plain": {}, "audit": {"k": v}}),
    "stored plain": lambda v: lambda: cards.check_stored({"audit": [], "plain": [v]}),
    "stored ask": lambda v: lambda: cards.check_stored({"audit": [], "plain": {}, "ask": [v]}),
}


def before(site: str, v) -> str:
    """The message as the base wrote it, with the f-string it used, for the value `SITES[site]` puts in."""
    return {
        "card id": f"not a card id: {[v]!r} (expected c-YYYYMMDD-HHMMSS-xxxxxx)",
        "kind": f"kind must be a non-empty text, not {[v]!r}",
        "feed": f"feed must be a non-empty text, not {[v]!r}",
        "lane": f"lane must be one of {', '.join(LANES)}, not {[v]!r}",
        "pri": f"pri must be one of {', '.join(PRIS)}, not {[v]!r}",
        "origin": f"origin must be one of {', '.join(ORIGINS)}, not {[v]!r}",
        "links": f"links must be a list of card ids, not {({'k': v})!r}",
        "links: a link": f"links: not a card id: {[v]!r}",
        "parent": f"parent must be a card id or null, not {[v]!r}",
        "title": f"title must be a text, an object of one text per language, or null, not {[v]!r}",
        "title.<lang>": f"title.en must be a text, not {[v]!r}",
        "ask": f"ask must be an object with a type, or null, not {[v]!r}",
        "ask.type": f"ask.type must be one of {', '.join(ASK_TYPES)}, not {[v]!r}",
        "plain": f"plain must be an object with what, where and do, not {[v]!r}",
        "stored audit": f"audit must be a list, not {({'k': v})!r}",
        "stored plain": f"plain must be an object, not {[v]!r}",
        "stored ask": f"ask must be an object or null, not {[v]!r}",
    }[site]


class ShownValueTest(unittest.TestCase):
    def test_a_repr_of_200_characters_or_fewer_is_shown_whole(self):
        for size in (0, 1, 198):  # repr adds the two quotes: 2, 3 and 200 characters
            with self.subTest(size):
                value = "y" * size
                self.assertEqual(cards.shown(value), repr(value))

    def test_a_longer_repr_is_cut_to_its_first_200_characters_then_dots(self):
        for size in (199, 1000, 100_000):
            with self.subTest(size):
                value = "y" * size
                self.assertEqual(cards.shown(value), repr(value)[:200] + "...")
        self.assertEqual(cards.SHOWN_CHARS, 200)


class StoredLongValueTest(unittest.TestCase):
    """Criterion 1: check_stored on a long bad value."""

    def test_audit_holding_a_long_text_is_cut(self):
        msg = message(lambda: cards.check_stored({"plain": {}, "audit": {"k": LONG}}))
        self.assertTrue(msg.startswith("audit must be a list, not {'"), msg[:80])
        self.assertLessEqual(len(msg), LIMIT)
        self.assertTrue(msg.endswith("..."))

    def test_plain_holding_a_long_text_is_cut(self):
        msg = message(lambda: cards.check_stored({"audit": [], "plain": [LONG]}))
        self.assertTrue(msg.startswith("plain must be an object, not ['"), msg[:80])
        self.assertLessEqual(len(msg), LIMIT)
        self.assertTrue(msg.endswith("..."))

    def test_ask_holding_a_long_text_is_cut(self):
        msg = message(lambda: cards.check_stored({"audit": [], "plain": {}, "ask": [LONG]}))
        self.assertTrue(msg.startswith("ask must be an object or null, not ['"), msg[:80])
        self.assertLessEqual(len(msg), LIMIT)
        self.assertTrue(msg.endswith("..."))


class EveryMessageIsCutTest(unittest.TestCase):
    """Criterion 2: every value-showing message in check_id, _enum, _text and check_shape."""

    def test_each_message_on_a_long_value_is_at_most_300_characters_ending_in_dots(self):
        for site, recipe in SITES.items():
            with self.subTest(site):
                msg = message(recipe(LONG))
                self.assertLessEqual(len(msg), LIMIT, msg[:80])
                # The value is last in every message but the card id's, which keeps its `(expected ...)` hint.
                tail = " (expected c-YYYYMMDD-HHMMSS-xxxxxx)" if site == "card id" else ""
                self.assertTrue(msg.endswith("..." + tail), msg[-40:])
                # The words before the value are kept: the message still names its field.
                self.assertTrue(msg.startswith(re.split(r"[\[{]", before(site, ""))[0]), msg[:80])

    def test_each_message_on_a_deep_value_is_cut_too(self):
        deep = deep_dict(400)
        for site, recipe in SITES.items():
            with self.subTest(site):
                msg = message(recipe(deep))
                self.assertLessEqual(len(msg), LIMIT, msg[:80])

    def test_a_long_run_of_text_that_is_not_unicode_is_cut(self):
        msg = message(lambda: cards.check_unicode({"t": "\ud800" * 1000}, "the card"))
        self.assertTrue(msg.startswith("the card is not valid Unicode: '\\ud800"), msg[:80])
        self.assertLessEqual(len(msg), LIMIT)
        self.assertTrue(msg.endswith("..."))


class ShortValuesUnchangedTest(unittest.TestCase):
    """Criterion 3: a value whose repr is 200 characters or fewer shows exactly as the base showed it."""

    def test_each_message_on_a_short_value_is_what_the_base_wrote(self):
        # Each site wraps the value in a list or dict (2 or 7 more characters), so 190 stays within 200.
        for value in ("nope", "", 7, None, {"a": [1, 2]}, "z" * 190):
            for site, recipe in SITES.items():
                with self.subTest(site=site, value=repr(value)[:20]):
                    self.assertLessEqual(len(repr({"k": value})), 200)
                    self.assertEqual(message(recipe(value)), before(site, value))

    def test_a_short_run_of_text_that_is_not_unicode_shows_as_before(self):
        msg = message(lambda: cards.check_unicode("a\ud800b", "text"))
        self.assertEqual(msg, "text is not valid Unicode: " + repr("\ud800"))


class DeepStoredCardTest(unittest.TestCase):
    """Criterion 4: a deep stored card gives a short error naming the file, through the store."""

    def setUp(self):
        self.data = Path(tempfile.mkdtemp(prefix="ap2-messages-")).resolve()
        self.addCleanup(shutil.rmtree, self.data, True)
        self.store = Store(self.data, cfg=Config())

    def test_a_card_file_whose_audit_is_400_levels_deep_gives_a_short_named_error(self):
        path = self.store.path(CARD_ID)
        path.parent.mkdir(parents=True, exist_ok=True)
        levels = 400
        path.write_text('{"id": "%s", "plain": {}, "audit": ' % CARD_ID + '{"a":' * levels + "1" + "}" * levels
                        + "}", encoding="utf-8")
        with self.assertRaises(CardError) as caught:
            self.store.get(CARD_ID)
        msg = str(caught.exception)
        head = f"{path}: not a card file: audit must be a list, not "
        self.assertTrue(msg.startswith(head), msg[:200])
        self.assertLessEqual(len(msg), LIMIT + len(str(path)), f"message is {len(msg)} characters")
        self.assertTrue(msg.endswith("..."))


class TooDeepToPrintTest(unittest.TestCase):
    """Criterion 5: a value whose repr raises RecursionError gives a TooDeepError naming its field."""

    def test_lane_too_deep_to_print_is_a_too_deep_error_naming_it(self):
        with self.assertRaisesRegex(cards.TooDeepError, r"\Alane must be .*nested too deeply\Z"):
            cards.check_shape(card(lane=TooDeepToPrint()))

    def test_every_message_on_a_value_too_deep_to_print_is_a_too_deep_error_naming_its_field(self):
        names = {"card id": "not a card id", "links: a link": "links", "stored audit": "audit",
                 "stored plain": "plain", "stored ask": "ask", "title.<lang>": "title.en"}
        for site, recipe in SITES.items():  # the value sits in a list or dict, whose repr fails with it
            with self.subTest(site):
                with self.assertRaises(cards.TooDeepError) as caught:
                    recipe(TooDeepToPrint())()
                msg = str(caught.exception)
                self.assertTrue(msg.startswith(names.get(site, site)), msg)
                self.assertIn("nested too deeply", msg)
                self.assertIsInstance(caught.exception, RecursionError)

    def test_stored_fields_keep_their_words(self):
        for field, rest, what in (("audit", {"plain": {}}, "a list"), ("plain", {"audit": []}, "an object"),
                                  ("ask", {"audit": [], "plain": {}}, "an object or null")):
            with self.subTest(field):
                with self.assertRaisesRegex(cards.TooDeepError,
                                            r"\A" + re.escape(f"{field} must be {what}, and is nested too deeply")
                                            + r"\Z"):
                    cards.check_stored({**rest, field: TooDeepToPrint()})


if __name__ == "__main__":
    unittest.main()
