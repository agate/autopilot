from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT  # noqa: F401  (puts lib/ on sys.path)
from assistant import cards, datadir, restore
from assistant.cards import CardError, PlainWordsError
from assistant.config import Config
from assistant.index import Index
from assistant.store import Store, fcntl

T0 = datetime(2026, 11, 10, 14, 30, 5, tzinfo=timezone.utc)


class Clock:
    """A clock the test moves by hand."""

    def __init__(self, at=T0):
        self.at = at

    def __call__(self):
        return self.at

    def tick(self, **delta):
        self.at = self.at + timedelta(**delta)
        return self.at


def plain(langs=("en",), what="checkout retries a failed card payment", where="11 new tests pass",
          do="nothing; it merges after review"):
    return {"what": {lang: f"{what} ({lang})" for lang in langs},
            "where": {lang: f"{where} ({lang})" for lang in langs},
            "do": {lang: f"{do} ({lang})" for lang in langs}}


def card(**over):
    c = {"kind": "code", "lane": "planned", "feed": "jira-epic", "links": [], "pri": "P2", "origin": "source",
         "plain": plain(), "title": "Retry failed payments"}
    c.update(over)
    return c


class StoreCase(unittest.TestCase):
    cfg = Config()

    def setUp(self):
        self.data = Path(tempfile.mkdtemp(prefix="ap2-store-")).resolve()
        self.addCleanup(shutil.rmtree, self.data, True)
        self.clock = Clock()
        self.store = Store(self.data, cfg=self.cfg, clock=self.clock)


class RoundTripTest(StoreCase):
    def test_add_then_get_round_trips(self):
        added = self.store.add(card(), by="worker")
        self.assertRegex(added["id"], r"^c-20261110-143005-[0-9a-f]{6}$")
        self.assertEqual(added["created_at"], "2026-11-10T14:30:05Z")
        self.assertEqual(added["updated_at"], "2026-11-10T14:30:05Z")
        got = self.store.get(added["id"])
        self.assertEqual(got, added)
        for key, value in card().items():
            self.assertEqual(got[key], value)

    def test_one_json_file_per_card_in_its_creation_month(self):
        added = self.store.add(card(), by="worker")
        path = self.data / "items" / "2026-11" / f"{added['id']}.json"
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        self.assertEqual(json.loads(text), added)
        self.assertEqual(text, json.dumps(added, sort_keys=True, indent=2, ensure_ascii=False) + "\n")
        self.clock.at = datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
        later = self.store.add(card(), by="worker")
        self.assertTrue((self.data / "items" / "2026-12" / f"{later['id']}.json").is_file())

    def test_month_is_utc(self):
        # 2026-11-30 20:00 in New York is 2026-12-01 01:00 UTC.
        self.clock.at = datetime(2026, 11, 30, 20, 0, tzinfo=timezone(timedelta(hours=-5)))
        added = self.store.add(card(), by="worker")
        self.assertTrue(added["id"].startswith("c-20261201-010000-"))
        self.assertTrue((self.data / "items" / "2026-12" / f"{added['id']}.json").is_file())

    def test_utf8_text_is_written_as_is(self):
        store = Store(self.data, cfg=Config(secondary_language="zh"), clock=self.clock)
        added = store.add(card(plain=plain(("en", "zh"), what="结账时重试")), by="worker")
        text = self.store.path(added["id"]).read_bytes().decode("utf-8")
        self.assertIn("结账时重试", text)

    def test_write_is_atomic_via_replace(self):
        added = self.store.add(card(), by="worker")
        with mock.patch("assistant.store.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(CardError):
                self.store.update(added["id"], {"lane": "running"}, by="worker")
        self.assertEqual(self.store.get(added["id"]), added)  # the old file is whole
        leftovers = [p.name for p in self.store.path(added["id"]).parent.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_unknown_keys_are_kept(self):
        added = self.store.add(card(key="org/repo#12"), by="worker")
        self.assertEqual(self.store.get(added["id"])["key"], "org/repo#12")

    def test_list_filters_by_lane_kind_parent(self):
        a = self.store.add(card(lane="needs", kind="question"), by="w")
        b = self.store.add(card(lane="running", parent=a["id"]), by="w")
        c = self.store.add(card(lane="running", kind="question", parent=a["id"]), by="w")
        ids = lambda cs: [x["id"] for x in cs]  # noqa: E731
        self.assertEqual(sorted(ids(self.store.list())), sorted([a["id"], b["id"], c["id"]]))
        self.assertEqual(sorted(ids(self.store.list(lane="running"))), sorted([b["id"], c["id"]]))
        self.assertEqual(sorted(ids(self.store.list(kind="question"))), sorted([a["id"], c["id"]]))
        self.assertEqual(sorted(ids(self.store.list(parent=a["id"]))), sorted([b["id"], c["id"]]))
        self.assertEqual(ids(self.store.list(lane="running", kind="question", parent=a["id"])), [c["id"]])
        with self.assertRaises(CardError):
            self.store.list(lane="later")

    def test_list_is_oldest_first(self):
        first = self.store.add(card(), by="w")
        self.clock.tick(seconds=1)
        second = self.store.add(card(), by="w")
        self.assertEqual([c["id"] for c in self.store.list()], [first["id"], second["id"]])

    def test_update_sets_fields(self):
        added = self.store.add(card(), by="w")
        self.clock.tick(minutes=5)
        new = self.store.update(added["id"], {"lane": "running", "pri": "P0"}, by="w")
        self.assertEqual((new["lane"], new["pri"]), ("running", "P0"))
        self.assertEqual(new["updated_at"], "2026-11-10T14:35:05Z")
        self.assertEqual(new["created_at"], added["created_at"])
        self.assertEqual(self.store.get(added["id"]), new)

    def test_update_that_changes_nothing_writes_nothing(self):
        added = self.store.add(card(), by="w")
        self.assertEqual(self.store.update(added["id"], {"lane": "planned"}, by="w"), added)
        self.assertEqual(list((self.data / "restore").glob("*.json")) if (self.data / "restore").exists() else [],
                         [])

    def test_get_unknown_and_bad_ids(self):
        with self.assertRaisesRegex(CardError, "no card"):
            self.store.get("c-20261110-143005-abcdef")
        for bad in ("../../etc/passwd", "c-2026", "C-20261110-143005-ABCDEF", "", None):
            with self.assertRaisesRegex(CardError, "not a card id"):
                self.store.get(bad)


class BadValueTest(StoreCase):
    """One test per bad enum value, and the other shape rules."""

    def assert_refused(self, message, **over):
        with self.assertRaisesRegex(CardError, message):
            self.store.add(card(**over), by="w")
        self.assertFalse((self.data / "items").exists() and any((self.data / "items").rglob("*.json")))

    def test_bad_lane(self):
        self.assert_refused("lane must be one of needs, running, planned, done", lane="later")

    def test_bad_pri(self):
        self.assert_refused("pri must be one of P0, P1, P2, P3", pri="P4")

    def test_bad_pri_lower_case(self):
        self.assert_refused("pri must be one of", pri="p1")

    def test_bad_origin(self):
        self.assert_refused("origin must be one of source, found", origin="made-up")

    def test_bad_ask_type(self):
        self.assert_refused("ask.type must be one of confirm, approve, answer, fix", ask={"type": "maybe"})

    def test_ask_without_type(self):
        self.assert_refused("ask.type must be one of", ask={"text": "ok?"})

    def test_good_ask_types_pass(self):
        for t in cards.ASK_TYPES:
            self.store.add(card(ask={"type": t}), by="w")

    def test_bad_enum_on_update(self):
        added = self.store.add(card(), by="w")
        with self.assertRaisesRegex(CardError, "lane must be one of"):
            self.store.update(added["id"], {"lane": "later"}, by="w")
        self.assertEqual(self.store.get(added["id"]), added)

    def test_required_fields(self):
        for key in cards.REQUIRED:
            if key == "plain":
                continue
            c = card()
            del c[key]
            with self.assertRaisesRegex(CardError, f"^{key} missing$"):
                self.store.add(c, by="w")

    def test_store_keys_are_refused(self):
        for key in cards.STORE_KEYS:
            with self.assertRaisesRegex(CardError, f"{key} is set by the store"):
                self.store.add(card(**{key: "x"}), by="w")
        added = self.store.add(card(), by="w")
        with self.assertRaisesRegex(CardError, "audit cannot be changed"):
            self.store.update(added["id"], {"audit": []}, by="w")

    def test_other_shape_rules(self):
        self.assert_refused("kind must be a non-empty text", kind="")
        self.assert_refused("feed must be a non-empty text", feed=" ")
        self.assert_refused("links must be a list", links="c-20261110-143005-abcdef")
        self.assert_refused("links: not a card id", links=["nope"])
        self.assert_refused("parent must be a card id or null", parent="nope")
        self.assert_refused("title must be a text", title=3)
        self.assert_refused("ask must be an object", ask="approve")
        with self.assertRaisesRegex(CardError, "a card is a JSON object"):
            self.store.add(["not", "a", "card"], by="w")
        with self.assertRaisesRegex(CardError, "by must name"):
            self.store.add(card(), by="")


class PlainWordsTest(StoreCase):
    """Design 3.7: what, where and do, in the primary language and the secondary one when set."""

    cfg = Config(primary_language="en", secondary_language="zh")
    CASES = [
        # (name, how to break the plain block, the message part expected)
        ("what missing", lambda p: p.pop("what"), "plain.what.en missing; plain.what.zh missing"),
        ("where missing", lambda p: p.pop("where"), "plain.where.en missing; plain.where.zh missing"),
        ("do missing", lambda p: p.pop("do"), "plain.do.en missing; plain.do.zh missing"),
        ("where.zh missing", lambda p: p["where"].pop("zh"), "plain.where.zh missing"),
        ("what.en missing", lambda p: p["what"].pop("en"), "plain.what.en missing"),
        ("do.zh missing", lambda p: p["do"].pop("zh"), "plain.do.zh missing"),
        ("what.en null", lambda p: p["what"].update(en=None), "plain.what.en missing"),
        ("do.en empty", lambda p: p["do"].update(en=""), "plain.do.en is empty"),
        ("where.zh blank", lambda p: p["where"].update(zh="  \n"), "plain.where.zh is empty"),
        ("what.zh not text", lambda p: p["what"].update(zh=3), "plain.what.zh is empty"),
        ("where not a map", lambda p: p.update(where="here"), "plain.where.en missing; plain.where.zh missing"),
    ]

    def test_each_missing_line_and_language_is_refused_on_add(self):
        for name, breaks, message in self.CASES:
            with self.subTest(name):
                p = plain(("en", "zh"))
                breaks(p)
                with self.assertRaises(PlainWordsError) as e:
                    self.store.add(card(plain=p), by="w")
                self.assertEqual(str(e.exception), message)
        self.assertFalse(any((self.data / "items").rglob("*.json")) if (self.data / "items").exists() else False)

    def test_each_missing_line_and_language_is_refused_on_update(self):
        added = self.store.add(card(plain=plain(("en", "zh"))), by="w")
        for name, breaks, message in self.CASES:
            with self.subTest(name):
                p = plain(("en", "zh"))
                breaks(p)
                with self.assertRaises(PlainWordsError) as e:
                    self.store.update(added["id"], {"plain": p}, by="w")
                self.assertEqual(str(e.exception), message)
        self.assertEqual(self.store.get(added["id"]), added)

    def test_no_plain_block_names_every_line(self):
        c = card()
        del c["plain"]
        with self.assertRaises(PlainWordsError) as e:
            self.store.add(c, by="w")
        for line in ("what", "where", "do"):
            for lang in ("en", "zh"):
                self.assertIn(f"plain.{line}.{lang} missing", str(e.exception))

    def test_a_plain_that_is_not_an_object_names_every_line(self):
        for bad in (None, "x"):
            with self.subTest(plain=bad):
                with self.assertRaises(PlainWordsError) as e:
                    self.store.add(card(plain=bad), by="w")
                self.assertIn("plain.what.en missing", str(e.exception))
                self.assertIn("plain.do.zh missing", str(e.exception))
        self.assertEqual(list(self.data.glob("items/*/*.json")), [])

    def test_one_error_class(self):
        self.assertTrue(issubclass(PlainWordsError, CardError))

    def test_both_languages_pass(self):
        self.store.add(card(plain=plain(("en", "zh"))), by="w")

    def test_primary_only_passes_when_no_secondary_is_set(self):
        store = Store(self.data, cfg=Config(primary_language="en"), clock=self.clock)
        store.add(card(plain=plain(("en",))), by="w")

    def test_primary_only_is_refused_when_secondary_is_set(self):
        with self.assertRaisesRegex(PlainWordsError, "plain.what.zh missing"):
            self.store.add(card(plain=plain(("en",))), by="w")

    def test_other_primary_language(self):
        store = Store(self.data, cfg=Config(primary_language="pt-BR"), clock=self.clock)
        with self.assertRaisesRegex(PlainWordsError, "plain.what.pt-BR missing"):
            store.add(card(plain=plain(("en",))), by="w")
        store.add(card(plain=plain(("pt-BR",))), by="w")

    def test_languages_come_from_config_yaml(self):
        (self.data / "config.yaml").write_text("secondary_language: zh\n")
        store = Store(self.data, clock=self.clock)
        with self.assertRaisesRegex(PlainWordsError, "plain.do.zh missing"):
            store.add(card(plain=plain(("en",))), by="w")


class TitleLanguagesTest(StoreCase):
    """Design 3.1: a title is null, one text (the same words in every language) or one text per language."""

    cfg = Config(primary_language="en", secondary_language="zh")

    def zh_card(self, **over):
        return card(plain=plain(("en", "zh")), **over)

    def test_a_text_an_object_and_null_are_accepted(self):
        for title in ("Retry failed payments", {"en": "Retry failed payments", "zh": "重试失败的付款"}, None):
            with self.subTest(title=title):
                added = self.store.add(self.zh_card(title=title), by="w")
                self.assertEqual(self.store.get(added["id"])["title"], title)

    def test_a_title_with_extra_languages_is_accepted(self):
        title = {"en": "Retry", "zh": "重试", "ja": "再試行"}
        self.assertEqual(self.store.add(self.zh_card(title=title), by="w")["title"], title)

    def test_a_bad_shape_is_refused_naming_the_title(self):
        for bad in (3, [], {}, {"en": 3}, {"en": None}, {"en": ["x"]}, {3: "x"}, True):
            with self.subTest(title=bad):
                with self.assertRaises(CardError) as e:
                    self.store.add(self.zh_card(title=bad), by="w")
                self.assertRegex(str(e.exception), r"^title(\.\w+)? must be ")
        self.assertEqual(list(self.data.glob("items/*/*.json")), [])

    def test_the_bad_shape_message(self):
        with self.assertRaises(CardError) as e:
            self.store.add(self.zh_card(title=3), by="w")
        self.assertEqual(str(e.exception), "title must be a text, an object of one text per language, or null, "
                                           "not 3")
        with self.assertRaisesRegex(CardError, r"^title\.en must be a text, not 3$"):
            self.store.add(self.zh_card(title={"en": 3}), by="w")

    def test_an_object_title_needs_every_language(self):
        for title, message in (({"en": "Retry"}, "title.zh missing"),
                               ({"zh": "重试"}, "title.en missing"),
                               ({"en": "", "zh": "重试"}, "title.en is empty"),
                               ({"en": "Retry", "zh": "  \n"}, "title.zh is empty"),
                               ({"ja": "再試行"}, "title.en missing; title.zh missing"),
                               ({"en": " ", "ja": "x"}, "title.en is empty; title.zh missing")):
            with self.subTest(title=title):
                with self.assertRaises(CardError) as e:
                    self.store.add(self.zh_card(title=title), by="w")
                self.assertEqual(str(e.exception), message)
                self.assertNotIsInstance(e.exception, PlainWordsError)
        self.assertEqual(list(self.data.glob("items/*/*.json")), [])

    def test_an_object_title_missing_a_language_is_refused_on_update(self):
        added = self.store.add(self.zh_card(), by="w")
        with self.assertRaisesRegex(CardError, r"^title\.zh missing$"):
            self.store.update(added["id"], {"title": {"en": "New words"}}, by="w")
        self.assertEqual(self.store.get(added["id"]), added)

    def test_a_text_title_is_not_checked_against_the_languages(self):
        added = self.store.add(self.zh_card(title="One text for both"), by="w")
        updated = self.store.update(added["id"], {"lane": "running"}, by="w")
        self.assertEqual(updated["title"], "One text for both")
        back = self.store.restore(updated["audit"][-1]["restore"], by="user")
        self.assertEqual((back["lane"], back["title"]), ("planned", "One text for both"))

    def test_a_card_stored_with_one_language_still_updates_its_other_fields(self):
        en = Store(self.data, cfg=Config(primary_language="en"), clock=self.clock)
        added = en.add(card(title="Old text title"), by="w")
        updated = self.store.update(added["id"], {"plain": plain(("en", "zh"))}, by="w")
        self.assertEqual(updated["title"], "Old text title")

    def test_restore_does_not_recheck_the_title_languages(self):
        en = Store(self.data, cfg=Config(primary_language="en"), clock=self.clock)
        added = en.add(card(title={"en": "English only"}), by="w")
        self.clock.tick(minutes=1)
        changed = en.update(added["id"], {"title": {"en": "Changed"}}, by="w")
        self.clock.tick(minutes=1)
        back = self.store.restore(changed["audit"][-1]["restore"], by="user")  # zh is set now
        self.assertEqual(back["title"], {"en": "English only"})

    def test_one_language_store_checks_only_the_primary(self):
        en = Store(self.data, cfg=Config(primary_language="en"), clock=self.clock)
        en.add(card(title={"en": "English only"}), by="w")
        with self.assertRaisesRegex(CardError, r"^title\.en missing$"):
            en.add(card(title={"zh": "只有中文"}), by="w")

    def test_search_finds_a_word_only_the_zh_slot_holds(self):
        title = {"en": "Retry failed payments", "zh": "结账时重试 heron"}
        added = self.store.add(self.zh_card(title=title), by="w")
        other = self.store.add(self.zh_card(title={"en": "Something else", "zh": "别的"}), by="w")
        self.assertEqual([c["id"] for c in self.store.search("heron")], [added["id"]])
        self.assertEqual([c["id"] for c in self.store.search("重试")], [added["id"]])
        self.assertEqual([c["id"] for c in self.store.search("retry")], [added["id"]])
        self.assertEqual([c["id"] for c in self.store.search("别的")], [other["id"]])


class TitleForTest(unittest.TestCase):
    """cards.title_for: the title in one language, the primary one when that language has none."""

    def test_an_object_gives_the_languages_slot(self):
        c = {"title": {"en": "Retry", "zh": "重试"}}
        self.assertEqual(cards.title_for(c, "zh", "en"), "重试")
        self.assertEqual(cards.title_for(c, "en", "en"), "Retry")

    def test_a_missing_or_empty_slot_falls_back_to_the_primary(self):
        for title in ({"en": "Retry"}, {"en": "Retry", "zh": ""}, {"en": "Retry", "zh": "  "},
                      {"en": "Retry", "zh": None}, {"en": "Retry", "zh": 3}):
            with self.subTest(title=title):
                self.assertEqual(cards.title_for({"title": title}, "zh", "en"), "Retry")

    def test_no_slot_and_no_primary_gives_null(self):
        for title in ({"ja": "再試行"}, {"en": "", "zh": " "}, {}):
            with self.subTest(title=title):
                self.assertIsNone(cards.title_for({"title": title}, "zh", "en"))

    def test_a_text_is_returned_as_it_is(self):
        self.assertEqual(cards.title_for({"title": "Same words"}, "zh", "en"), "Same words")
        self.assertEqual(cards.title_for({"title": "Same words"}, "en", "en"), "Same words")

    def test_null_and_anything_else_give_null(self):
        for c in ({}, {"title": None}, {"title": 3}, {"title": ["x"]}, {"title": True}, None, "card"):
            with self.subTest(card=c):
                self.assertIsNone(cards.title_for(c, "en", "en"))


class TitleSearchTextTest(unittest.TestCase):
    def base(self, title):
        return {"kind": "code", "feed": "chat", "plain": {}, "title": title}

    def test_an_object_title_gives_every_slot_sorted_by_language(self):
        text = cards.search_text(self.base({"zh": "重试", "en": "Retry", "ja": "再試行"}))
        self.assertEqual(text.split("\n"), ["Retry", "再試行", "重试", "code", "chat"])

    def test_a_text_title_is_indexed_as_before(self):
        self.assertEqual(cards.search_text(self.base("Retry")).split("\n"), ["Retry", "code", "chat"])

    def test_a_slot_that_is_not_text_is_left_out(self):
        self.assertEqual(cards.search_text(self.base({"en": "Retry", "zh": 3})).split("\n"),
                         ["Retry", "code", "chat"])
        self.assertEqual(cards.search_text(self.base(None)).split("\n"), ["code", "chat"])


class AuditTest(StoreCase):
    def test_audit_after_each_step(self):
        added = self.store.add(card(), by="scan")
        self.assertEqual(added["audit"], [{"at": "2026-11-10T14:30:05Z", "by": "scan", "did": "add",
                                           "restore": None}])
        self.clock.tick(minutes=1)
        updated = self.store.update(added["id"], {"lane": "running"}, by="dispatcher")
        self.assertEqual(len(updated["audit"]), 2)
        entry = updated["audit"][1]
        self.assertEqual(set(entry), {"at", "by", "did", "restore"})
        self.assertEqual((entry["at"], entry["by"], entry["did"]), ("2026-11-10T14:31:05Z", "dispatcher",
                                                                   "update: lane"))
        self.assertRegex(entry["restore"], r"^r-20261110-143105-[0-9a-f]{6}$")
        self.assertTrue((self.data / "restore" / f"{entry['restore']}.json").is_file())
        self.clock.tick(minutes=1)
        back = self.store.restore(entry["restore"], by="user")
        self.assertEqual(len(back["audit"]), 3)
        last = back["audit"][2]
        self.assertEqual((last["at"], last["by"], last["did"]), ("2026-11-10T14:32:05Z", "user",
                                                                f"restore {entry['restore']}"))
        self.assertRegex(last["restore"], r"^r-20261110-143205-[0-9a-f]{6}$")
        self.assertNotEqual(last["restore"], entry["restore"])
        self.assertEqual(self.store.get(added["id"])["audit"], back["audit"])


class RestoreTest(StoreCase):
    def test_change_then_restore(self):
        added = self.store.add(card(), by="w")
        self.clock.tick(minutes=1)
        changed = self.store.update(added["id"], {"lane": "done", "title": "Changed"}, by="w")
        point_id = changed["audit"][-1]["restore"]
        point = json.loads((self.data / "restore" / f"{point_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(point["id"], point_id)
        self.assertEqual(point["at"], "2026-11-10T14:31:05Z")
        self.assertEqual(point["kind"], "card")
        self.assertEqual(point["card_id"], added["id"])
        self.assertEqual(point["card"], added)  # the whole file as it was before the change
        self.clock.tick(minutes=1)
        back = self.store.restore(point_id, by="user")
        self.assertEqual((back["lane"], back["title"]), ("planned", "Retry failed payments"))
        self.assertEqual(self.store.get(added["id"]), back)

    def test_restore_the_restore_undoes_the_undo(self):
        added = self.store.add(card(), by="w")
        self.clock.tick(minutes=1)
        changed = self.store.update(added["id"], {"lane": "done"}, by="w")
        self.clock.tick(minutes=1)
        undone = self.store.restore(changed["audit"][-1]["restore"], by="user")
        self.assertEqual(undone["lane"], "planned")
        self.clock.tick(minutes=1)
        redone = self.store.restore(undone["audit"][-1]["restore"], by="user")
        self.assertEqual(redone["lane"], "done")
        self.assertEqual([e["did"].split(" ")[0] for e in redone["audit"]],
                         ["add", "update:", "restore", "restore"])
        self.assertEqual(self.store.get(added["id"])["lane"], "done")
        self.assertEqual(len(list((self.data / "restore").glob("r-*.json"))), 3)

    def test_unknown_and_bad_point_ids(self):
        with self.assertRaisesRegex(CardError, "no restore point"):
            self.store.restore("r-20261110-143005-abcdef", by="user")
        with self.assertRaisesRegex(CardError, "not a restore point id"):
            self.store.restore("../items/x", by="user")

    def test_restore_keeps_the_restored_card_findable_by_its_old_words(self):
        added = self.store.add(card(title="Original words"), by="w")
        changed = self.store.update(added["id"], {"title": "Replacement"}, by="w")
        self.assertEqual(self.store.search("original"), [])
        self.store.restore(changed["audit"][-1]["restore"], by="user")
        self.assertEqual([c["id"] for c in self.store.search("original")], [added["id"]])
        self.assertEqual(self.store.search("replacement"), [])

    def prune_after(self, days):
        added = self.store.add(card(), by="w")
        self.store.update(added["id"], {"lane": "done"}, by="w")
        point_id = self.store.get(added["id"])["audit"][-1]["restore"]
        removed = self.store.prune(now=T0 + timedelta(days=days))
        return point_id, removed, (self.data / "restore" / f"{point_id}.json").exists()

    def test_prune_keeps_a_29_day_old_point(self):
        _, removed, exists = self.prune_after(29)
        self.assertEqual(removed, [])
        self.assertTrue(exists)

    def test_prune_removes_a_31_day_old_point(self):
        point_id, removed, exists = self.prune_after(31)
        self.assertEqual(removed, [point_id])
        self.assertFalse(exists)

    def test_prune_uses_the_injected_clock(self):
        added = self.store.add(card(), by="w")
        self.store.update(added["id"], {"lane": "done"}, by="w")
        self.clock.tick(days=29)
        self.assertEqual(self.store.prune(), [])
        self.clock.tick(days=2)
        self.assertEqual(len(self.store.prune()), 1)
        self.assertEqual(self.store.get(added["id"])["lane"], "done")  # pruning never touches the cards


class OppositeActionTest(StoreCase):
    def test_proposal_lands_as_an_approve_ask_and_nothing_runs(self):
        tmp = Path(tempfile.mkdtemp(prefix="ap2-opposite-")).resolve()
        self.addCleanup(shutil.rmtree, tmp, True)
        data = tmp / "data"
        store = Store(data, cfg=self.cfg, clock=self.clock)
        added = store.add(card(), by="w")
        boom = AssertionError("nothing may run or reach the network")
        patches = [mock.patch.object(subprocess, name, side_effect=boom)
                   for name in ("Popen", "run", "call", "check_call", "check_output")]
        patches += [mock.patch.object(os, name, side_effect=boom)
                    for name in ("system", "popen", "execv", "execve", "execvp", "execvpe", "spawnv", "spawnve",
                                 "posix_spawn", "posix_spawnp", "fork", "forkpty") if hasattr(os, name)]
        patches += [mock.patch.object(socket, "socket", side_effect=boom),
                    mock.patch.object(socket, "create_connection", side_effect=boom)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        before = set(tmp.rglob("*"))
        new = store.propose_opposite(added["id"], "close_pr", ["gh", "pr", "close", "12"], by="dispatcher",
                                          text="Close the PR opened by mistake")
        self.assertEqual(new["ask"], {"type": "approve", "action": "close_pr", "run": ["gh", "pr", "close", "12"],
                                      "proposed": True, "text": "Close the PR opened by mistake"})
        self.assertEqual(store.get(added["id"])["ask"], new["ask"])
        self.assertEqual(new["audit"][-1]["did"], "propose close_pr")
        self.assertIsNotNone(new["audit"][-1]["restore"])
        written = set(tmp.rglob("*")) - before
        self.assertTrue(written)  # the card and its restore point changed
        outside = [p for p in written if data not in p.parents]
        self.assertEqual(outside, [])

    def test_every_opposite_action_is_accepted(self):
        self.assertEqual(restore.OPPOSITE_ACTIONS,
                         ("move_ticket_back", "close_pr", "force_push_saved_ref", "revert_pr"))
        for action in restore.OPPOSITE_ACTIONS:
            added = self.store.add(card(), by="w")
            self.assertEqual(self.store.propose_opposite(added["id"], action, ["echo", action], by="w")["ask"]["type"],
                             "approve")

    def test_unknown_action_and_bad_run_are_refused(self):
        added = self.store.add(card(), by="w")
        with self.assertRaisesRegex(CardError, "action must be one of"):
            self.store.propose_opposite(added["id"], "delete_repo", ["rm"], by="w")
        for run in ([], "gh pr close 12", [""], [3]):
            with self.assertRaisesRegex(CardError, "run must be a non-empty list"):
                self.store.propose_opposite(added["id"], "close_pr", run, by="w")
        self.assertEqual(self.store.get(added["id"]), added)

    def test_an_open_ask_is_not_overwritten(self):
        added = self.store.add(card(ask={"type": "answer", "text": "which repo?"}), by="w")
        with self.assertRaisesRegex(CardError, "already has an open answer ask"):
            self.store.propose_opposite(added["id"], "close_pr", ["gh", "pr", "close", "12"], by="w")
        self.assertEqual(self.store.get(added["id"]), added)


class CheckedUpdateTest(StoreCase):
    """`update_checked` reads the card, asks `check(card)` and writes, all under one hold of the store lock, so no
    other writer can land between the look and the change."""

    def test_a_check_that_says_yes_writes_the_change(self):
        added = self.store.add(card(), by="w")
        new = self.store.update_checked(added["id"], {"lane": "running"}, by="w", check=lambda c: True,
                                        did="move it")
        self.assertEqual(new["lane"], "running")
        self.assertEqual(self.store.get(added["id"]), new)
        self.assertEqual((new["audit"][-1]["by"], new["audit"][-1]["did"]), ("w", "move it"))
        self.assertIsNotNone(new["audit"][-1]["restore"])

    def test_a_check_that_says_no_writes_nothing(self):
        added = self.store.add(card(), by="w")
        before = sorted((self.data / "restore").glob("*")) if (self.data / "restore").exists() else []
        self.assertIsNone(self.store.update_checked(added["id"], {"lane": "running"}, by="w",
                                                    check=lambda c: False))
        self.assertEqual(self.store.get(added["id"]), added)
        after = sorted((self.data / "restore").glob("*")) if (self.data / "restore").exists() else []
        self.assertEqual(after, before)  # no restore point either

    def test_the_check_sees_the_card_on_disk_with_the_store_lock_held(self):
        import fcntl
        added = self.store.add(card(), by="w")
        self.store.update(added["id"], {"ask": {"type": "answer", "text": "which repo?"}}, by="user")
        seen = []

        def check(c):
            fd = os.open(str(self.data / ".store.lock"), os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):  # held by this very update
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
            seen.append(c["ask"])
            return c["ask"] is None

        self.assertIsNone(self.store.update_checked(added["id"], {"ask": {"type": "fix", "text": "x"}}, by="w",
                                                    check=check))
        self.assertEqual(seen, [{"type": "answer", "text": "which repo?"}])
        self.assertEqual(self.store.get(added["id"])["ask"], {"type": "answer", "text": "which repo?"})

    def test_a_check_may_say_what_the_change_did(self):
        added = self.store.add(card(), by="w")
        new = self.store.update_checked(added["id"], {"lane": "running"}, by="w", did="planned",
                                        check=lambda c: f"moved from {c['lane']}")
        self.assertEqual(new["audit"][-1]["did"], f"moved from {added['lane']}")

    def test_it_refuses_what_update_refuses(self):
        added = self.store.add(card(), by="w")
        with self.assertRaisesRegex(CardError, "changes must be a non-empty object"):
            self.store.update_checked(added["id"], {}, by="w", check=lambda c: True)
        with self.assertRaisesRegex(CardError, "cannot be changed; the store sets it"):
            self.store.update_checked(added["id"], {"audit": []}, by="w", check=lambda c: True)
        with self.assertRaisesRegex(CardError, "no card"):
            self.store.update_checked("c-20260101-000000-abcdef", {"lane": "running"}, by="w", check=lambda c: True)
        self.assertEqual(self.store.get(added["id"]), added)


class SearchIndexTest(StoreCase):
    def test_search_finds_a_new_card(self):
        added = self.store.add(card(plain=plain(what="the nightly export to the vendor bucket")), by="w")
        self.assertTrue((self.data / "index.sqlite").is_file())
        self.assertEqual([c["id"] for c in self.store.search("VENDOR bucket")], [added["id"]])
        self.assertEqual([c["id"] for c in self.store.search("export")], [added["id"]])
        self.assertEqual(self.store.search("vendor nothere"), [])
        self.assertEqual(self.store.search("   "), [])

    def test_search_covers_title_kind_and_feed(self):
        added = self.store.add(card(title="Quarterly numbers", feed="slack-mentions", kind="question"), by="w")
        for text in ("quarterly", "slack-mentions", "question"):
            self.assertEqual([c["id"] for c in self.store.search(text)], [added["id"]], text)

    def test_search_finds_chinese_words_inside_a_sentence(self):
        store = Store(self.data, cfg=Config(secondary_language="zh"), clock=self.clock)
        added = store.add(card(plain=plain(("en", "zh"), what="结账时重试失败的付款")), by="w")
        self.assertEqual([c["id"] for c in store.search("付款")], [added["id"]])

    def test_like_wildcards_are_plain_text(self):
        self.store.add(card(), by="w")
        self.assertEqual(self.store.search("%"), [])
        self.assertEqual(self.store.search("_"), [])
        with_pct = self.store.add(card(title="100% done"), by="w")
        self.assertEqual([c["id"] for c in self.store.search("100%")], [with_pct["id"]])

    def test_updated_cards_old_words_stop_matching(self):
        added = self.store.add(card(title="Zebra crossing"), by="w")
        self.store.update(added["id"], {"title": "Pelican crossing"}, by="w")
        self.assertEqual(self.store.search("zebra"), [])
        self.assertEqual(self.store.index.search("zebra"), [])  # the index row itself follows the update
        self.assertEqual([c["id"] for c in self.store.search("pelican")], [added["id"]])

    def test_a_crash_between_the_card_write_and_the_index_write_never_shows_old_words(self):
        added = self.store.add(card(title="Zebra crossing"), by="w")
        with mock.patch.object(Index, "put", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.store.update(added["id"], {"title": "Pelican crossing"}, by="w")
        self.assertEqual(self.store.get(added["id"])["title"], "Pelican crossing")
        self.assertEqual(self.store.search("zebra"), [])

    def test_a_crash_between_the_card_write_and_the_index_write_is_caught_up(self):
        # The stale marker written before the card file makes the next use rebuild, so the new words are found.
        added = self.store.add(card(title="Zebra crossing"), by="w")
        with mock.patch.object(Index, "put", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.store.update(added["id"], {"title": "Pelican crossing"}, by="w")
        self.assertTrue((self.data / ".index.stale").exists())
        fresh = Store(self.data, cfg=self.cfg, clock=self.clock)
        self.assertEqual([c["id"] for c in fresh.search("pelican")], [added["id"]])
        self.assertFalse((self.data / ".index.stale").exists())

    def test_a_crash_before_a_write_is_caught_up_by_the_next_write(self):
        first = self.store.add(card(title="Zebra crossing"), by="w")
        with mock.patch.object(Index, "put", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.store.update(first["id"], {"title": "Pelican crossing"}, by="w")
        self.clock.tick(seconds=1)
        second = self.store.add(card(title="Pelican two"), by="w")
        self.assertFalse((self.data / ".index.stale").exists())
        self.assertEqual(self.store.index.search("pelican"), [first["id"], second["id"]])

    def test_the_stale_marker_is_gone_after_each_good_write(self):
        added = self.store.add(card(), by="w")
        self.store.update(added["id"], {"lane": "done"}, by="w")
        self.assertFalse((self.data / ".index.stale").exists())

    def test_a_damaged_card_file_does_not_fail_another_cards_add(self):
        a = self.store.add(card(title="Lighthouse"), by="w")
        self.store.path(a["id"]).write_text("{half")
        (self.data / "index.sqlite").unlink()
        self.clock.tick(seconds=1)
        b = self.store.add(card(title="Second"), by="w")
        self.assertTrue(self.store.path(b["id"]).is_file())
        self.assertEqual(self.store.get(b["id"])["title"], "Second")
        self.assertEqual(list(self.data.rglob(".index.sqlite.*.tmp")), [])

    def test_deleted_index_is_rebuilt(self):
        added = self.store.add(card(title="Lighthouse"), by="w")
        (self.data / "index.sqlite").unlink()
        fresh = Store(self.data, cfg=self.cfg, clock=self.clock)
        self.assertEqual([c["id"] for c in fresh.search("lighthouse")], [added["id"]])
        self.assertTrue((self.data / "index.sqlite").is_file())

    def test_corrupt_index_is_rebuilt(self):
        added = self.store.add(card(title="Lighthouse"), by="w")
        (self.data / "index.sqlite").write_bytes(b"this is not a database" * 100)
        self.assertEqual([c["id"] for c in self.store.search("lighthouse")], [added["id"]])

    def test_corrupt_index_is_rebuilt_on_the_next_write(self):
        first = self.store.add(card(title="Lighthouse"), by="w")
        (self.data / "index.sqlite").write_bytes(b"\0" * 4096)
        self.clock.tick(seconds=1)
        second = self.store.add(card(title="Lighthouse keeper"), by="w")
        self.assertEqual([c["id"] for c in self.store.search("lighthouse")], [first["id"], second["id"]])

    def test_index_of_another_version_is_rebuilt(self):
        added = self.store.add(card(title="Lighthouse"), by="w")
        import sqlite3
        conn = sqlite3.connect(str(self.data / "index.sqlite"))
        with conn:
            conn.execute("UPDATE meta SET value = '0' WHERE key = 'version'")
            conn.execute("DELETE FROM cards")
        conn.close()
        self.assertEqual([c["id"] for c in self.store.search("lighthouse")], [added["id"]])

    def test_the_card_files_are_the_truth(self):
        added = self.store.add(card(title="Lighthouse"), by="w")
        # A stale row: the index says "lighthouse" but the file was changed by hand (or by an old index).
        path = self.store.path(added["id"])
        on_disk = json.loads(path.read_text())
        on_disk["title"] = "Hand edited"
        path.write_text(json.dumps(on_disk))
        self.assertEqual(self.store.search("lighthouse"), [])  # the file does not hold the words, so no match
        self.assertEqual(self.store.index.search("lighthouse"), [added["id"]])  # the stale row is still there
        on_disk["title"] = "Lighthouse, edited"
        path.write_text(json.dumps(on_disk))
        self.assertEqual([c["title"] for c in self.store.search("lighthouse")], ["Lighthouse, edited"])
        path.unlink()
        self.assertEqual(self.store.search("lighthouse"), [])

    def test_a_failed_index_write_leaves_the_card_and_rebuilds_later(self):
        import sqlite3
        with mock.patch.object(Index, "_connect", side_effect=sqlite3.OperationalError("locked")):
            added = self.store.add(card(title="Lighthouse"), by="w")
        self.assertEqual(self.store.get(added["id"])["title"], "Lighthouse")
        self.assertEqual([c["id"] for c in self.store.search("lighthouse")], [added["id"]])



class DamagedFileTest(StoreCase):
    """A card file that is JSON with the right id but a structure the store cannot use is refused, naming it."""

    SHAPES = {"no audit": lambda c: c.pop("audit"), "plain null": lambda c: c.update(plain=None),
              "ask text": lambda c: c.update(ask="hi")}

    def damaged(self, damage):
        added = self.store.add(card(title="Vendor export"), by="w")
        self.clock.tick(minutes=1)
        point = self.store.update(added["id"], {"lane": "done"}, by="w")["audit"][-1]["restore"]
        path = self.store.path(added["id"])
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        damage(on_disk)
        path.write_text(json.dumps(on_disk), encoding="utf-8")
        return added["id"], point, path

    def points(self):
        return sorted(p.name for p in (self.data / "restore").glob("r-*.json"))

    def test_each_damaged_shape_is_refused_by_every_call(self):
        for name, damage in self.SHAPES.items():
            with self.subTest(name):
                card_id, point, path = self.damaged(damage)
                calls = {"get": lambda: self.store.get(card_id),
                         "list": lambda: self.store.list(),
                         "search": lambda: self.store.search("vendor"),
                         "update": lambda: self.store.update(card_id, {"lane": "running"}, by="w"),
                         "propose": lambda: self.store.propose_opposite(card_id, "close_pr", ["gh", "pr"], by="w"),
                         "restore": lambda: self.store.restore(point, by="user")}
                for call_name, call in calls.items():
                    before = self.points()
                    with self.assertRaisesRegex(CardError, f"{path}: not a card file", msg=call_name):
                        call()
                    self.assertEqual(self.points(), before, call_name)  # no orphan restore point
                (self.data / "index.sqlite").unlink()
                with self.assertRaisesRegex(CardError, f"{path}: not a card file"):
                    self.store.search("vendor")  # met by the rebuild
                path.unlink()

    def test_search_text_ignores_a_plain_that_is_not_an_object(self):
        self.assertEqual(cards.search_text({"plain": None, "title": "Vendor"}), "Vendor")


class RestoreAnyPointTest(StoreCase):
    def test_a_point_holding_a_hand_edited_enum_can_be_put_back(self):
        added = self.store.add(card(), by="w")
        self.clock.tick(minutes=1)
        r1 = self.store.update(added["id"], {"lane": "done"}, by="w")["audit"][-1]["restore"]
        path = self.store.path(added["id"])
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        on_disk["lane"] = "blocked"
        path.write_text(json.dumps(on_disk), encoding="utf-8")
        self.clock.tick(minutes=1)
        undone = self.store.restore(r1, by="user")
        self.assertEqual(undone["lane"], "planned")
        r2 = undone["audit"][-1]["restore"]
        self.clock.tick(minutes=1)
        redone = self.store.restore(r2, by="user")
        self.assertEqual(redone["lane"], "blocked")
        self.assertEqual(len(redone["audit"]), len(undone["audit"]) + 1)
        self.assertEqual(redone["audit"][-1]["did"], f"restore {r2}")
        self.assertEqual(self.store.get(added["id"])["lane"], "blocked")


class MismatchedPointTest(StoreCase):
    """A restore point is used only when its id is its file name and its card_id is its card's id."""

    def files(self):
        return {p: p.read_bytes() for p in sorted(self.data.glob("**/*.json"))}

    def test_a_point_labelled_for_one_card_does_not_restore_another(self):
        a = self.store.add(card(title="Card A"), by="w")
        b = self.store.add(card(title="Card B"), by="w")
        self.clock.tick(minutes=1)
        point_id = self.store.update(a["id"], {"lane": "done"}, by="w")["audit"][-1]["restore"]
        point_path = self.data / "restore" / f"{point_id}.json"
        point = json.loads(point_path.read_text(encoding="utf-8"))
        point["card"] = dict(b, title="Overwritten")  # labelled for card A, holds card B
        point_path.write_text(json.dumps(point), encoding="utf-8")
        before = self.files()
        with self.assertRaisesRegex(CardError, f"{point_path}: not a card restore point"):
            self.store.restore(point_id, by="user")
        self.assertEqual(self.files(), before)
        self.assertEqual(self.store.get(b["id"]), b)

    def test_a_point_whose_id_is_not_its_file_name_is_refused(self):
        a = self.store.add(card(), by="w")
        self.clock.tick(minutes=1)
        point_id = self.store.update(a["id"], {"lane": "done"}, by="w")["audit"][-1]["restore"]
        other = "r-20261110-143105-000000" if not point_id.endswith("000000") else "r-20261110-143105-111111"
        other_path = self.data / "restore" / f"{other}.json"
        shutil.copyfile(self.data / "restore" / f"{point_id}.json", other_path)
        before = self.files()
        with self.assertRaisesRegex(CardError, f"{other_path}: not a card restore point"):
            self.store.restore(other, by="user")
        self.assertEqual(self.files(), before)


class BadPointCardTest(StoreCase):
    """A point's card is checked like a card file before anything is written, and the error names the point."""

    SHAPES = {"lone surrogate": lambda c: c.update(title="x \ud800"), "plain null": lambda c: c.update(plain=None)}

    def test_a_point_holding_an_unusable_card_is_refused_naming_the_point(self):
        for name, damage in self.SHAPES.items():
            with self.subTest(name):
                added = self.store.add(card(), by="w")
                self.clock.tick(minutes=1)
                point_id = self.store.update(added["id"], {"lane": "done"}, by="w")["audit"][-1]["restore"]
                point_path = self.data / "restore" / f"{point_id}.json"
                point = json.loads(point_path.read_text(encoding="utf-8"))
                damage(point["card"])
                point_path.write_text(json.dumps(point), encoding="utf-8")  # ensure_ascii: the \\ud800 escape
                points = sorted(p.name for p in (self.data / "restore").glob("r-*.json"))
                card_file = self.store.path(added["id"]).read_bytes()
                with self.assertRaises(CardError) as e:
                    self.store.restore(point_id, by="user")
                self.assertIn(f"{point_id}.json", str(e.exception))
                self.assertEqual(sorted(p.name for p in (self.data / "restore").glob("r-*.json")), points)
                self.assertEqual(self.store.path(added["id"]).read_bytes(), card_file)


class BadUnicodeTest(StoreCase):
    def assert_nothing_written(self):
        self.assertEqual(list((self.data / "restore").glob("r-*.json")), [])
        self.assertFalse((self.data / ".index.stale").exists())

    def test_add_with_a_lone_surrogate_is_refused(self):
        with self.assertRaisesRegex(CardError, "not valid Unicode"):
            self.store.add(card(title="bad \ud800"), by="w")
        with self.assertRaisesRegex(CardError, "not valid Unicode"):
            self.store.add(card(), by="\udcff")
        self.assertEqual(list(self.data.glob("items/*/*.json")), [])
        self.assert_nothing_written()

    def test_update_with_a_lone_surrogate_is_refused_before_any_point(self):
        added = self.store.add(card(), by="w")
        with self.assertRaisesRegex(CardError, "not valid Unicode"):
            self.store.update(added["id"], {"title": "bad \ud800"}, by="w")
        with self.assertRaisesRegex(CardError, "not valid Unicode"):
            self.store.update(added["id"], {"lane": "done"}, by="\udcff")
        self.assertEqual(self.store.get(added["id"]), added)
        self.assert_nothing_written()

    def test_search_with_a_lone_surrogate_is_refused(self):
        self.store.add(card(), by="w")
        with self.assertRaisesRegex(CardError, "not valid Unicode"):
            self.store.search("retry \udcff")

    def test_a_card_file_holding_a_lone_surrogate_is_refused(self):
        added = self.store.add(card(title="Vendor"), by="w")
        path = self.store.path(added["id"])
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        on_disk["title"] = "Vendor \ud800"
        path.write_text(json.dumps(on_disk), encoding="utf-8")  # ensure_ascii: the file holds the \\ud800 escape
        (self.data / "index.sqlite").unlink()
        for call in (lambda: self.store.get(added["id"]), lambda: self.store.search("vendor"),
                     lambda: self.store.update(added["id"], {"lane": "done"}, by="w")):
            with self.assertRaisesRegex(CardError, f"{path}: not a card file: .*not valid Unicode"):
                call()
        self.assertEqual(list((self.data / "restore").glob("r-*.json")), [])


class BadDidTest(StoreCase):
    def test_a_bad_did_is_refused_before_any_write(self):
        added = self.store.add(card(), by="w")
        for did in ("x \ud800", ["update"], "", "  "):
            with self.subTest(did=did):
                with self.assertRaises(CardError):
                    self.store.update(added["id"], {"lane": "running"}, by="w", did=did)
                self.assertEqual(list((self.data / "restore").glob("r-*.json")), [])
                self.assertFalse((self.data / ".index.stale").exists())
                self.assertEqual(self.store.get(added["id"]), added)

    def test_a_good_did_is_kept(self):
        added = self.store.add(card(), by="w")
        new = self.store.update(added["id"], {"lane": "running"}, by="w", did="started work")
        self.assertEqual(new["audit"][-1]["did"], "started work")


class NotStandardJsonTest(StoreCase):
    def test_nan_and_infinity_are_refused(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(CardError, "not valid JSON: NaN or Infinity"):
                    self.store.add(card(n=value), by="w")
        self.assertEqual(list(self.data.glob("items/*/*.json")), [])

    def test_nan_on_update_is_refused_before_any_point(self):
        added = self.store.add(card(), by="w")
        with self.assertRaisesRegex(CardError, "not valid JSON"):
            self.store.update(added["id"], {"n": float("nan")}, by="w")
        self.assertEqual(self.store.get(added["id"]), added)
        self.assertEqual(list((self.data / "restore").glob("r-*.json")), [])


class NotSortableJsonTest(StoreCase):
    """Values json.dumps cannot write with sorted keys: mixed key types and sets."""
    BAD = ({"a": 1, 2: "b"}, {"x", "y"})

    def assert_nothing_written(self):
        self.assertEqual(list((self.data / "restore").glob("r-*.json")), [])
        self.assertFalse((self.data / ".index.stale").exists())

    def test_add_is_refused_before_any_write(self):
        for value in self.BAD:
            with self.subTest(value=value):
                with self.assertRaisesRegex(CardError, "is not JSON: "):
                    self.store.add(card(extra=value), by="w")
        self.assertEqual(list(self.data.glob("items/*/*.json")), [])
        self.assert_nothing_written()

    def test_update_is_refused_before_any_point(self):
        added = self.store.add(card(), by="w")
        path = self.store.path(added["id"])
        before = path.read_bytes()
        for value in self.BAD:
            with self.subTest(value=value):
                with self.assertRaisesRegex(CardError, "is not JSON: "):
                    self.store.update(added["id"], {"extra": value}, by="w")
                self.assertEqual(path.read_bytes(), before)
                self.assert_nothing_written()


class AsciiIdTest(unittest.TestCase):
    CARD_IDS = ("c-\u0662\u0660\u0662\u0666\u0661\u0661\u0661\u0660-\u0661\u0664\u0663\u0660\u0660\u0665-abcdef",
                "c-\uff12\uff10\uff12\uff16\uff11\uff11\uff11\uff10-143005-abcdef")
    POINT_IDS = ("r-\u0662\u0660\u0662\u0666\u0661\u0661\u0661\u0660-143005-abcdef",
                 "r-20261110-\uff11\uff14\uff13\uff10\uff10\uff15-abcdef")

    def test_card_ids_use_ascii_digits_only(self):
        for card_id in self.CARD_IDS:
            with self.subTest(card_id=card_id):
                with self.assertRaisesRegex(CardError, "not a card id"):
                    cards.check_id(card_id)
                with self.assertRaisesRegex(CardError, "not a card id"):
                    cards.month(card_id)
        self.assertEqual(cards.check_id("c-20261110-143005-abcdef"), "c-20261110-143005-abcdef")

    def test_point_ids_use_ascii_digits_only(self):
        for point_id in self.POINT_IDS:
            with self.subTest(point_id=point_id):
                with self.assertRaisesRegex(CardError, "not a restore point id"):
                    restore.check_point_id(point_id)


class UnreadableFolderTest(StoreCase):
    def test_an_unreadable_month_folder_fails_instead_of_hiding_its_cards(self):
        added = self.store.add(card(title="Vendor"), by="w")
        month = self.store.path(added["id"]).parent
        (self.data / "index.sqlite").unlink()
        os.chmod(month, 0)
        self.addCleanup(os.chmod, month, 0o755)  # runs before the temp folder is removed
        try:
            os.listdir(month)
        except PermissionError:
            pass
        else:
            self.skipTest("chmod 000 does not stop this user reading the folder (root?)")
        with self.assertRaisesRegex(CardError, "cannot use the card store"):
            self.store.list()
        with self.assertRaisesRegex(CardError, "cannot use the card store"):
            self.store.search("vendor")
        self.assertFalse((self.data / "index.sqlite").exists())  # no empty index left behind
        os.chmod(month, 0o755)
        self.assertEqual([c["id"] for c in self.store.search("vendor")], [added["id"]])
        self.assertEqual([c["id"] for c in self.store.list()], [added["id"]])


class JunkIndexRowTest(StoreCase):
    def test_junk_and_duplicate_rows_are_skipped(self):
        import sqlite3
        added = self.store.add(card(title="Vendor"), by="w")
        conn = sqlite3.connect(str(self.data / "index.sqlite"))
        with conn:
            conn.execute("DROP TABLE cards")
            conn.execute("CREATE TABLE cards(id TEXT, text TEXT NOT NULL)")  # no primary key: duplicates fit
            conn.executemany("INSERT INTO cards VALUES (?, ?)",
                             [("bogus", "vendor"), (added["id"], "vendor"), (added["id"], "vendor")])
        conn.close()
        self.assertEqual([c["id"] for c in self.store.search("vendor")], [added["id"]])


def files(folder: Path) -> dict:
    """Every file under `folder` (links not followed) as {relative path: bytes}, and every folder as None."""
    out = {}
    for root, dirs, names in os.walk(folder):
        for name in dirs + names:
            full = Path(root) / name
            out[str(full.relative_to(folder))] = None if full.is_dir() else full.read_bytes()
    return out


class LinkCase(StoreCase):
    """A data folder holding one card, changed once (so one restore point), and a folder outside it."""

    def setUp(self):
        super().setUp()
        self.outside = Path(tempfile.mkdtemp(prefix="ap2-outside-")).resolve()
        self.addCleanup(shutil.rmtree, self.outside, True)
        self.added = self.store.add(card(title="Vendor export"), by="w")
        self.clock.tick(minutes=1)
        self.point = self.store.update(self.added["id"], {"lane": "running"}, by="w")["audit"][-1]["restore"]
        self.clock.tick(minutes=1)

    def calls(self):
        card_id, point = self.added["id"], self.point
        return {"add": lambda: self.store.add(card(), by="w"),
                "get": lambda: self.store.get(card_id),
                "list": lambda: self.store.list(),
                "search": lambda: self.store.search("vendor"),
                "update": lambda: self.store.update(card_id, {"lane": "done"}, by="w"),
                "update_checked": lambda: self.store.update_checked(card_id, {"lane": "done"}, "w", lambda c: True),
                "propose": lambda: self.store.propose_opposite(card_id, "close_pr", ["gh", "pr"], by="w"),
                "ask_about": lambda: self.store.ask_about(card_id, "why?", by="w"),
                "restore": lambda: self.store.restore(point, by="user"),
                "prune": lambda: self.store.prune(now=T0 + timedelta(days=60))}

    def assert_every_call_refused(self, link: Path, target: Path, only=None):
        """Each call raises one CardError line naming `link`, and nothing under `target` changes."""
        before = files(target) if target.exists() else None
        for name, call in self.calls().items():
            if only is not None and name not in only:
                continue
            with self.subTest(call=name):
                with self.assertRaisesRegex(CardError, f"^{re.escape(str(link))} is a symlink[^\n]*$"):
                    call()
                self.assertTrue(link.is_symlink())  # refused, never replaced
                self.assertEqual(files(target) if target.exists() else None, before)


class LinkedItemsTest(LinkCase):
    def test_a_linked_items_folder_is_refused_by_every_call(self):
        items = self.data / "items"
        shutil.move(str(items), str(self.data / "items.real"))
        items.symlink_to(self.outside, target_is_directory=True)
        self.assert_every_call_refused(items, self.outside)
        self.assertEqual(files(self.outside), {})

    def test_a_dangling_items_link_is_refused_and_nothing_is_made_at_its_target(self):
        items = self.data / "items"
        shutil.rmtree(items)
        target = self.outside / "gone"
        items.symlink_to(target, target_is_directory=True)
        self.assert_every_call_refused(items, target)
        self.assertFalse(target.exists())
        self.assertEqual(files(self.outside), {})


class LinkedCardTest(LinkCase):
    CARD_CALLS = ("add", "get", "list", "search", "update", "update_checked", "propose", "ask_about", "restore")

    def test_a_linked_month_folder_is_refused_and_its_target_is_unchanged(self):
        month = self.store.path(self.added["id"]).parent
        target = self.outside / month.name
        shutil.move(str(month), str(target))
        month.symlink_to(target, target_is_directory=True)
        self.assert_every_call_refused(month, target, only=self.CARD_CALLS)
        (self.data / "index.sqlite").unlink()
        with self.assertRaisesRegex(CardError, f"^{re.escape(str(month))} is a symlink"):
            self.store.search("vendor")  # met by the rebuild too

    def test_a_linked_card_file_is_refused_and_its_target_is_unchanged(self):
        path = self.store.path(self.added["id"])
        target = self.outside / path.name
        shutil.move(str(path), str(target))
        path.symlink_to(target)
        self.assert_every_call_refused(path, self.outside, only=self.CARD_CALLS[1:])
        (self.data / "index.sqlite").unlink()
        with self.assertRaisesRegex(CardError, f"^{re.escape(str(path))} is a symlink"):
            self.store.search("vendor")  # met by the rebuild too

    def test_a_linked_point_file_is_refused(self):
        path = restore.path(self.data, self.point)
        target = self.outside / path.name
        shutil.move(str(path), str(target))
        path.symlink_to(target)
        card_before = self.store.path(self.added["id"]).read_bytes()
        self.assert_every_call_refused(path, self.outside, only=("restore",))
        self.assertEqual(self.store.path(self.added["id"]).read_bytes(), card_before)


class LinkedRestoreTest(LinkCase):
    def check(self, target: Path):
        restore_dir = self.data / "restore"
        path = self.store.path(self.added["id"])
        card_before = path.read_bytes()
        self.assert_every_call_refused(restore_dir, target, only=("update", "restore", "prune", "ask_about",
                                                                 "propose", "update_checked"))
        self.assertEqual(path.read_bytes(), card_before)  # refused before the card write

    def test_a_linked_restore_folder_is_refused_before_the_card_is_written(self):
        restore_dir = self.data / "restore"
        shutil.move(str(restore_dir), str(self.outside / "points"))
        restore_dir.symlink_to(self.outside / "points", target_is_directory=True)
        self.check(self.outside)

    def test_a_dangling_restore_link_is_refused(self):
        restore_dir = self.data / "restore"
        shutil.rmtree(restore_dir)
        restore_dir.symlink_to(self.outside / "gone", target_is_directory=True)
        self.check(self.outside)
        self.assertEqual(files(self.outside), {})


class LinkedStoreFileTest(LinkCase):
    NAMES = (".store.lock", "index.sqlite", "index.sqlite-journal", "index.sqlite-wal", "index.sqlite-shm",
             ".index.stale")

    def test_each_linked_store_file_is_refused_and_its_target_keeps_its_bytes(self):
        for name in self.NAMES:
            with self.subTest(name=name):
                link = self.data / name
                target = self.outside / name
                target.write_bytes(b"v1 owns this file\n")
                with contextlib.suppress(FileNotFoundError):
                    link.unlink()
                link.symlink_to(target)
                self.assert_every_call_refused(link, self.outside)
                self.assertEqual(target.read_bytes(), b"v1 owns this file\n")
                link.unlink()
                self.store.get(self.added["id"])  # the link gone, the store works again

    def test_each_dangling_store_file_link_is_refused_and_nothing_is_made(self):
        for name in self.NAMES:
            with self.subTest(name=name):
                link = self.data / name
                with contextlib.suppress(FileNotFoundError):
                    link.unlink()
                link.symlink_to(self.outside / name)
                self.assert_every_call_refused(link, self.outside)
                self.assertFalse((self.outside / name).exists())
                link.unlink()

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW") and fcntl is not None, "needs O_NOFOLLOW and flock")
    def test_the_lock_is_opened_without_following_a_link(self):
        # A link swapped in after the check: _locked() alone (no lstat first) still refuses it.
        link = self.data / ".store.lock"
        target = self.outside / "lock"
        target.write_bytes(b"v1 owns this file\n")
        link.unlink()
        link.symlink_to(target)
        with self.assertRaisesRegex(CardError, f"^{re.escape(str(link))} is a symlink"):
            with self.store._locked():
                self.fail("the lock was taken through a link")
        self.assertEqual(target.read_bytes(), b"v1 owns this file\n")

    def test_the_stale_marker_is_made_without_following_a_link(self):
        # A link swapped in after the check: mark_stale() alone still refuses to make the marker at its target.
        link = self.data / ".index.stale"
        link.symlink_to(self.outside / "stale")
        with self.assertRaises(OSError):
            self.store.index.mark_stale()
        self.assertFalse((self.outside / "stale").exists())


class LinkedDataFolderTest(unittest.TestCase):
    def test_a_data_folder_reached_through_a_link_still_works(self):
        real = Path(tempfile.mkdtemp(prefix="ap2-real-")).resolve()
        self.addCleanup(shutil.rmtree, real, True)
        holder = Path(tempfile.mkdtemp(prefix="ap2-holder-")).resolve()
        self.addCleanup(shutil.rmtree, holder, True)
        link = holder / "data"
        link.symlink_to(real, target_is_directory=True)
        for word, data in (("alpha", link), ("bravo", datadir.resolve(str(link)))):
            with self.subTest(data=str(data)):
                store = Store(data, cfg=Config(), clock=Clock())
                added = store.add(card(title=f"Through a link {word}"), by="w")
                self.assertEqual(store.get(added["id"]), added)
                self.assertEqual([c["id"] for c in store.search(f"link {word}")], [added["id"]])
                self.assertTrue((real / "items" / "2026-11" / f"{added['id']}.json").is_file())
        self.assertEqual(datadir.resolve(str(link)), real)


if __name__ == "__main__":
    unittest.main()
