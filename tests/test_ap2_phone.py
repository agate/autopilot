"""The phone page's data (design 9.2): the window, the full cards, hiding what people wrote, read-only."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import types
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT

from assistant import cards, datadir, engine, phone, server
from assistant.config import Config, ConfigError
from assistant.engine import Engine
from assistant.store import Store

FIXTURES = ROOT / "tests" / "fixtures" / "ap2"
NOW = datetime(2026, 11, 10, 14, 30, 5, tzinfo=timezone.utc)
MARKER = "Zephyrquill"  # a made-up word put in a record's text, to find wherever that text went
FIELDS = {"generated_at", "mode", "primary_language", "secondary_language", "languages", "settings", "lanes",
          "projects", "heartbeat", "cards", "full"}


def plain(text="a thing", langs=("en",)) -> dict:
    return {line: {lang: f"{line} {text} ({lang})" for lang in langs} for line in cards.PLAIN_LINES}


def new_card(langs=("en",), **over) -> dict:
    card = {"kind": "errand", "lane": "needs", "feed": "chat", "links": [], "pri": "P2", "origin": "source",
            "plain": plain(langs=langs), "title": "A card"}
    card.update(over)
    return card


def tree(folder: Path) -> dict:
    """Every file and folder under `folder` with its bytes (None for a folder)."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(folder):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            rel = str(p.relative_to(folder))
            if p.is_symlink():
                out[rel] = ("link", os.readlink(p))
            elif p.is_dir():
                out[rel] = None
            else:
                out[rel] = p.read_bytes()
    return out


def as_json(value):
    return json.loads(json.dumps(value, sort_keys=True, ensure_ascii=False))


class PhoneCase(unittest.TestCase):
    cfg = Config()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-phone-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.plugin = self.tmp / "plugin"
        shutil.copytree(FIXTURES / "sources" / "plugin", self.plugin)
        self.data = datadir.ensure(self.tmp / "data")

    def add(self, age=timedelta(hours=1), cfg=None, **over) -> dict:
        """Add a card whose `updated_at` is `age` before NOW."""
        cfg = cfg or self.cfg
        at = NOW - age
        return Store(self.data, cfg=cfg, clock=lambda: at).add(new_card(langs=cards.languages(cfg), **over),
                                                                by="test")

    def build(self, cfg=None, **settings) -> dict:
        cfg = cfg or Config(**{**self.cfg.__dict__, **settings})
        return phone.build(self.data, cfg=cfg, now=NOW, plugin_dir=self.plugin)

    def read_gh(self, cfg=None, marker=True, age=timedelta(hours=1)) -> list:
        """The gh-mine fixture's cards, read by the engine; with `marker`, PR 42's title holds MARKER."""
        reply = self.tmp / "gh-pr-list.json"
        items = json.loads((FIXTURES / "sources" / "replies" / "gh-pr-list.json").read_text(encoding="utf-8"))
        if marker:
            items[0]["title"] = f"Retry the {MARKER} payments once"
        reply.write_text(json.dumps(items), encoding="utf-8")
        at = NOW - age
        Engine(self.data, cfg=cfg or self.cfg, plugin_dir=self.plugin, clock=lambda: at).read_saved("gh-mine", reply)
        return [c for c in Store(self.data, cfg=cfg or self.cfg).list() if c.get("feed") == "gh-mine"]

    def pr42(self, cfg=None) -> dict:
        return next(c for c in self.read_gh(cfg) if c["source"]["record"]["id"] == "42")

    def server_reply(self, card_id: str, cfg=None) -> dict:
        """What `GET /api/cards/<id>` replies for this data folder, as JSON (the handler, without a socket)."""
        handler = server.Handler.__new__(server.Handler)
        handler.server = types.SimpleNamespace(data=self.data, cfg=cfg or self.cfg, clock=lambda: NOW,
                                               plugin_dir=self.plugin, url="")
        return as_json(handler.card(card_id))


# --- criterion 2: the window -----------------------------------------------------------------------------------

class WindowTest(PhoneCase):
    def ids(self, data) -> set:
        return {r["id"] for r in data["cards"]}

    def test_history_is_the_last_three_days_and_open_cards_always_show(self):
        recent = self.add(age=timedelta(days=2, hours=23), lane="done", title="recent history")
        old = self.add(age=timedelta(days=3, hours=1), lane="done", title="old history")
        open_old = self.add(age=timedelta(days=365), lane="needs", title="a year old, still open")
        planned = self.add(age=timedelta(days=40), lane="planned")
        running = self.add(age=timedelta(days=9), lane="running")
        data = self.build()
        self.assertEqual(self.ids(data), {recent["id"], open_old["id"], planned["id"], running["id"]})
        self.assertNotIn(old["id"], data["full"])

    def test_the_edge_of_the_window_is_in(self):
        edge = self.add(age=timedelta(days=3), lane="done")
        past = self.add(age=timedelta(days=3, seconds=1), lane="done")
        self.assertEqual(self.ids(self.build()), {edge["id"]})
        self.assertNotIn(past["id"], self.ids(self.build()))

    def test_zero_days_shows_no_history(self):
        self.add(age=timedelta(0), lane="done")
        self.add(age=timedelta(minutes=5), lane="done")
        open_card = self.add(lane="needs")
        self.assertEqual(self.ids(self.build(phone_history_days=0)), {open_card["id"]})

    def test_more_days_show_more_history(self):
        old = self.add(age=timedelta(days=10), lane="done")
        self.assertNotIn(old["id"], self.ids(self.build()))
        self.assertIn(old["id"], self.ids(self.build(phone_history_days=30)))

    def test_lanes_setting_picks_the_lanes(self):
        needs = self.add(lane="needs")
        self.add(lane="running")
        self.add(lane="planned")
        self.add(lane="done")
        data = self.build(phone_lanes=("needs",))
        self.assertEqual(self.ids(data), {needs["id"]})
        self.assertEqual(data["lanes"], {"needs": 1, "running": 0, "planned": 0, "done": 0})
        self.assertEqual(data["settings"], {"history_days": 3, "lanes": ["needs"], "hide_quotes": False})

    def test_a_done_card_without_a_readable_time_stays_off(self):
        card = self.add(lane="done")
        path = Store(self.data, cfg=self.cfg).path(card["id"])
        for bad in ("yesterday", None, 5):
            with self.subTest(bad):
                stored = json.loads(path.read_text(encoding="utf-8"))
                stored["updated_at"] = bad
                path.write_text(json.dumps(stored), encoding="utf-8")
                self.assertNotIn(card["id"], self.ids(self.build()))

    def test_the_data_has_its_fields_and_the_counts_match_the_rows(self):
        project = self.add(kind="project", lane="running", title="Move the shop")
        self.add(parent=project["id"], lane="done", age=timedelta(days=1), title="done, in")
        self.add(parent=project["id"], lane="done", age=timedelta(days=10), title="done, out")
        self.add(parent=project["id"], lane="planned", title="still to do")
        self.add(lane="needs")
        self.add(lane="done", age=timedelta(days=20))
        (self.data / "gate.json").write_text(json.dumps({"last_run_at": cards.stamp(NOW - timedelta(seconds=25))}))
        data = self.build()
        self.assertEqual(set(data), FIELDS)
        self.assertEqual(data["generated_at"], "2026-11-10T14:30:05Z")
        self.assertEqual((data["mode"], data["primary_language"], data["secondary_language"], data["languages"]),
                         ("shadow", "en", None, ["en"]))
        self.assertEqual(data["settings"], {"history_days": 3, "lanes": ["needs", "running", "planned", "done"],
                                            "hide_quotes": False})
        counted = Counter(r["lane"] for r in data["cards"])
        self.assertEqual(data["lanes"], {lane: counted.get(lane, 0) for lane in cards.LANES})
        self.assertEqual(sum(data["lanes"].values()), len(data["cards"]))
        self.assertEqual(data["projects"], [{"id": project["id"], "title": "Move the shop", "done": 1, "total": 2}])
        self.assertEqual(data["heartbeat"], server.heartbeat(self.data, NOW))
        self.assertEqual(data["heartbeat"]["age_seconds"], 25)
        self.assertEqual(set(data["full"]), {r["id"] for r in data["cards"]})

    def test_rows_are_the_servers_rows_in_the_servers_order(self):
        for lane, pri, age in (("done", "P2", 1), ("needs", "P3", 2), ("needs", "P0", 3), ("running", "P1", 4),
                               ("planned", "P2", 5), ("done", "P2", 2)):
            self.add(lane=lane, pri=pri, age=timedelta(hours=age))
        found = Store(self.data, cfg=self.cfg).list()
        self.assertEqual(self.build()["cards"], [server.row(c, ("en",)) for c in server.order(found)])

    def test_no_cards_yet(self):
        data = self.build()
        self.assertEqual((data["cards"], data["full"], data["projects"]), ([], {}, []))
        self.assertTrue(data["heartbeat"]["late"])

    def test_a_damaged_card_file_is_a_card_error(self):
        card = self.add()
        Store(self.data, cfg=self.cfg).path(card["id"]).write_text("{not json", encoding="utf-8")
        with self.assertRaises(cards.CardError) as caught:
            self.build()
        self.assertIn(card["id"], str(caught.exception))

    def test_the_config_is_read_from_the_data_folder_when_not_given(self):
        (self.data / "config.yaml").write_text("phone_lanes: [running]\nmode: live\n", encoding="utf-8")
        running = self.add(lane="running")
        self.add(lane="needs")
        data = phone.build(self.data, now=NOW, plugin_dir=self.plugin)
        self.assertEqual([r["id"] for r in data["cards"]], [running["id"]])
        self.assertEqual(data["mode"], "live")


# --- criterion 3: full cards -----------------------------------------------------------------------------------

class FullCardTest(PhoneCase):
    def test_a_full_card_is_the_servers_reply_without_mac_fields_and_off_phone_neighbours(self):
        card = self.pr42()
        child_in = self.add(title="child on the phone", parent=card["id"])
        child_old = self.add(title="old finished child", parent=card["id"], lane="done", age=timedelta(days=10))
        link_in = self.add(title="linked, open")
        link_old = self.add(title="linked, old history", lane="done", age=timedelta(days=10))
        gone = "c-20250101-000000-abcdef"
        at = NOW - timedelta(minutes=30)
        store = Store(self.data, cfg=self.cfg, clock=lambda: at)
        card = store.update(card["id"], {"links": [link_in["id"], link_old["id"], gone],
                                         "sessions": {"review": {"id": "s-1", "cwd": "/tmp/x", "state": "ended"}}},
                            by="test")
        store.ask_about(card["id"], "Is this safe to merge?", by="page")
        self.assertIsInstance(card["source"]["raw"], str)  # the engine kept the raw file's path on the card

        reply = self.server_reply(card["id"])
        full = as_json(self.build()["full"][card["id"]])

        expect = json.loads(json.dumps(reply))
        del expect["card"]["sessions"]
        del expect["card"]["source"]["raw"]
        off = {child_old["id"], link_old["id"]}
        expect["children"] = [{"id": r["id"], "off_phone": True} if r["id"] in off else r for r in reply["children"]]
        expect["links"] = [{"id": r["id"], "off_phone": True} if r["id"] in off else r for r in reply["links"]]
        self.assertEqual(full, expect)

        self.assertNotIn("sessions", full["card"])
        self.assertNotIn("raw", full["card"]["source"])
        self.assertEqual(full["children"], [server.row(Store(self.data, cfg=self.cfg).get(child_in["id"]), ("en",)),
                                            {"id": child_old["id"], "off_phone": True}])
        self.assertEqual(full["links"][0]["title"], "linked, open")
        self.assertEqual(full["links"][1:], [{"id": link_old["id"], "off_phone": True}, {"id": gone, "missing": True}])
        self.assertNotIn("old finished child", json.dumps(self.build(), ensure_ascii=False))
        self.assertNotIn("linked, old history", json.dumps(self.build(), ensure_ascii=False))
        # Questions and their answers stay (design 9.4 point 4).
        self.assertEqual([q["text"] for q in full["card"]["questions"]], ["Is this safe to merge?"])
        self.assertIn("questions", full["view"])

    def test_a_link_to_a_lane_left_off_is_off_phone(self):
        planned = self.add(lane="planned", title="planned work")
        card = self.add(lane="needs", links=[planned["id"]])
        full = self.build(phone_lanes=("needs",))["full"][card["id"]]
        self.assertEqual(full["links"], [{"id": planned["id"], "off_phone": True}])

    def test_a_card_with_a_template_gets_the_templates_view(self):
        shutil.copy(FIXTURES / "templates" / "parcel.yaml", self.data / "templates" / "parcel.yaml")
        card = self.add(kind="parcel")
        full = as_json(self.build()["full"][card["id"]])
        self.assertEqual(full, self.server_reply(card["id"]))
        self.assertEqual(full["template"]["kind"], "parcel")

    def test_a_bad_template_still_serves_the_card(self):
        (self.data / "templates" / "parcel.yaml").write_text("kind: parcel\n", encoding="utf-8")
        card = self.add(kind="parcel")
        other = self.add(kind="errand")
        data = self.build()
        self.assertIn("template_error", data["full"][card["id"]])
        self.assertEqual(as_json(data["full"][card["id"]]), self.server_reply(card["id"]))
        self.assertEqual(as_json(data["full"][other["id"]]), self.server_reply(other["id"]))

    def test_the_store_is_not_changed_by_the_mac_only_fields_leaving(self):
        card = self.pr42()
        self.build()
        self.assertIn("raw", Store(self.data, cfg=self.cfg).get(card["id"])["source"])


# --- criterion 4: hide what people wrote -----------------------------------------------------------------------

class HideTest(PhoneCase):
    def test_with_hiding_on_the_words_appear_nowhere(self):
        card = self.pr42()
        self.add(title="plain card", links=[card["id"]])
        data = self.build(phone_hide_quotes=True)
        self.assertNotIn(MARKER, json.dumps(data, ensure_ascii=False))
        self.assertNotIn(MARKER.lower(), json.dumps(data, ensure_ascii=False).lower())
        row = next(r for r in data["cards"] if r["id"] == card["id"])
        phrase = "octo-dev wrote a message in widgets"
        self.assertIsNone(row["title"])
        self.assertEqual(row["title_hidden"], {"en": phrase})
        full = data["full"][card["id"]]
        self.assertEqual(full["card"]["title"], phrase)
        self.assertEqual(full["card"]["plain"]["what"], {"en": phrase})
        self.assertNotIn("text", full["card"]["source"]["record"])
        sb = full["source_block"]
        self.assertIsNone(sb["text"])
        self.assertNotIn("text", sb["record"])
        # Who, where, when and the Open-in-source link stay.
        self.assertEqual((sb["name"], sb["author"], sb["place"], sb["at"], sb["link"]),
                         ("gh-mine", "octo-dev", "widgets", "2026-11-09T17:02:11Z",
                          "https://github.com/example/widgets/pull/42"))
        self.assertEqual(full["card"]["source"]["link"], "https://github.com/example/widgets/pull/42")
        self.assertEqual(data["settings"]["hide_quotes"], True)

    def test_a_link_row_to_a_hidden_card_is_hidden_too(self):
        card = self.pr42()
        plain_card = self.add(title="plain card", links=[card["id"]], parent=None)
        data = self.build(phone_hide_quotes=True)
        link = data["full"][plain_card["id"]]["links"][0]
        self.assertIsNone(link["title"])
        self.assertEqual(link["title_hidden"], {"en": "octo-dev wrote a message in widgets"})

    def test_with_hiding_off_the_words_are_in_the_row_and_the_full_card(self):
        card = self.pr42()
        data = self.build()
        row = next(r for r in data["cards"] if r["id"] == card["id"])
        self.assertIn(MARKER, row["title"])
        self.assertNotIn("title_hidden", row)
        full = data["full"][card["id"]]
        self.assertIn(MARKER, full["card"]["title"])
        self.assertIn(MARKER, full["card"]["plain"]["what"]["en"])
        self.assertIn(MARKER, full["source_block"]["text"])

    def test_cards_without_a_source_record_are_unchanged(self):
        hand = self.add(title=f"my own {MARKER} note")
        no_record = self.add(title="a source with no record", source={"name": "chat", "link": None})
        on, off = self.build(phone_hide_quotes=True), self.build()
        self.assertEqual(on["cards"], off["cards"])
        self.assertEqual(on["full"], off["full"])
        self.assertIn(MARKER, on["full"][hand["id"]]["card"]["title"])
        self.assertEqual(on["full"][no_record["id"]]["card"]["title"], "a source with no record")

    def test_each_language_gets_its_own_phrase(self):
        cfg = Config(secondary_language="zh", phone_hide_quotes=True)
        card = self.pr42(cfg=cfg)
        data = self.build(cfg=cfg)
        self.assertNotIn(MARKER, json.dumps(data, ensure_ascii=False))
        row = next(r for r in data["cards"] if r["id"] == card["id"])
        self.assertEqual(row["title_hidden"], {"en": "octo-dev wrote a message in widgets",
                                               "zh": "octo-dev 在 widgets 写了一条消息"})
        full = data["full"][card["id"]]
        self.assertEqual(full["card"]["title"], "octo-dev wrote a message in widgets")  # the primary language only
        self.assertEqual(full["card"]["plain"]["what"], row["title_hidden"])

    def test_the_shorter_forms(self):
        record = {"id": "1", "at": "2026-11-09T17:02:11Z", "text": f"{MARKER}!"}
        cases = [({"author": "Ana", "place": "#team"}, "messages", "Ana wrote a message in #team"),
                 ({"author": "Ana"}, "messages", "Ana wrote a message"),
                 ({"place": "#team"}, "work", "Someone wrote an item in #team"),
                 ({}, "work", "Someone wrote an item"),
                 ({"author": "Ana"}, None, "Ana wrote something")]
        for extra, brings, want in cases:
            with self.subTest(want):
                self.assertEqual(engine.hidden_text("en", {**record, **extra}, brings), want)
        self.assertEqual(engine.hidden_text("zh-Hans", {**record, "place": "#team"}, "work"), "有人在 #team 写了一个条目")
        self.assertEqual(engine.hidden_text("zh", record, "messages"), "有人写了一条消息")
        self.assertIsNone(engine.hidden_phrases("fr"))
        self.assertIs(engine.hidden_phrases("en-GB"), engine.hidden_phrases("en"))

    def test_a_source_that_cannot_be_loaded_is_something(self):
        card = self.add(title=f"{MARKER} from a source that is gone",
                        source={"name": "gone-source", "link": None, "record": {"text": MARKER, "author": "Ana"}})
        data = self.build(phone_hide_quotes=True)
        self.assertNotIn(MARKER, json.dumps(data, ensure_ascii=False))
        self.assertEqual(data["full"][card["id"]]["card"]["title"], "Ana wrote something")

    def test_a_language_with_no_phrases_is_refused(self):
        cfg = Config(primary_language="fr", phone_hide_quotes=True)
        with self.assertRaises(ConfigError) as caught:
            self.build(cfg=cfg)
        msg = str(caught.exception)
        self.assertIn("fr", msg)
        self.assertIn("phone_hide_quotes", msg)
        self.assertEqual(len(msg.splitlines()), 1)
        self.build(cfg=Config(primary_language="fr"))  # with hiding off, no phrases are needed

    def test_a_hidden_project_title_is_hidden_in_the_projects_list(self):
        card = self.add(kind="project", title=f"{MARKER} project",
                        source={"name": "gh-mine", "link": None, "record": {"text": MARKER, "author": "Ana"}})
        data = self.build(phone_hide_quotes=True)
        self.assertEqual(data["projects"], [{"id": card["id"], "title": None,
                                             "title_hidden": {"en": "Ana wrote a message"}, "done": 0, "total": 0}])
        self.assertNotIn(MARKER, json.dumps(data, ensure_ascii=False))


# --- criterion 6: read-only, in shadow and live mode -----------------------------------------------------------

class ReadOnlyTest(PhoneCase):
    def test_build_writes_nothing_and_runs_nothing(self):
        home = self.tmp / "home"
        home.mkdir()
        self.read_gh()
        card = self.add(lane="done")
        self.add(links=[card["id"]], sessions={"x": {"id": "s"}})
        (self.data / "gate.json").write_text(json.dumps({"last_run_at": cards.stamp(NOW)}))
        for name in (".store.lock", "index.sqlite", ".index.stale"):
            path = self.data / name
            if path.exists():
                path.unlink()
        for mode in ("shadow", "live"):
            for hide in (False, True):
                with self.subTest(mode=mode, hide=hide):
                    before_data, before_home = tree(self.data), tree(home)
                    with mock.patch.dict(os.environ, {"HOME": str(home)}), \
                            mock.patch.object(subprocess, "Popen", side_effect=AssertionError("a process")) as popen, \
                            mock.patch.object(socket, "create_connection",
                                              side_effect=AssertionError("a connection")) as connect:
                        data = self.build(mode=mode, phone_hide_quotes=hide)
                    self.assertEqual(data["mode"], mode)
                    self.assertGreater(len(data["cards"]), 0)
                    popen.assert_not_called()
                    connect.assert_not_called()
                    self.assertEqual(tree(self.data), before_data)
                    self.assertEqual(tree(home), before_home)
                    self.assertFalse((self.data / ".store.lock").exists())
                    self.assertFalse((self.data / "index.sqlite").exists())

    def test_a_missing_data_folder_creates_nothing(self):
        missing = self.tmp / "nowhere"
        data = phone.build(missing, cfg=Config(), now=NOW, plugin_dir=self.plugin)
        self.assertEqual(data["cards"], [])
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
