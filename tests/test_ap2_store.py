from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT  # noqa: F401  (puts lib/ on sys.path)
from assistant import cards, restore
from assistant.cards import CardError, PlainWordsError
from assistant.config import Config
from assistant.index import Index
from assistant.store import Store

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


if __name__ == "__main__":
    unittest.main()
