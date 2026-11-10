from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT
from assistant import datadir
from assistant.config import Config, ShadowModeError
from assistant.engine import Engine, plain_words, snippet
from assistant.sources import Loader, SourceError
from assistant.store import Store

FIXTURES = ROOT / "tests" / "fixtures" / "ap2" / "sources"
REPLIES = FIXTURES / "replies"

# A fake command: prints the file in argv[1], and writes its working folder to argv[2] when given.
FAKE = """\
import os, sys
sys.stdout.buffer.write(open(sys.argv[1], "rb").read())
if len(sys.argv) > 2:
    open(sys.argv[2], "w").write(os.getcwd())
if len(sys.argv) > 3:
    sys.stderr.write("it went wrong\\n")
    sys.exit(int(sys.argv[3]))
"""


class Clock:
    def __init__(self):
        self.at = datetime(2026, 11, 10, 14, 30, 5, tzinfo=timezone.utc)

    def __call__(self):
        return self.at

    def tick(self, seconds=60):
        self.at += timedelta(seconds=seconds)


def tree(folder: Path) -> dict:
    """Every file under `folder` with its bytes."""
    return {str(p.relative_to(folder)): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


class EngineCase(unittest.TestCase):
    cfg = Config()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-engine-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.plugin = self.tmp / "plugin"
        shutil.copytree(FIXTURES / "plugin", self.plugin)
        self.data = datadir.ensure(self.tmp / "data")
        self.fake = self.tmp / "bin" / "fake.py"
        self.fake.parent.mkdir()
        self.fake.write_text(FAKE)
        self.clock = Clock()

    def engine(self, cfg=None):
        return Engine(self.data, cfg=cfg or self.cfg, plugin_dir=self.plugin, clock=self.clock)

    def store(self):
        return Store(self.data, cfg=self.cfg, clock=self.clock)

    def command(self, reply: Path, *extra) -> str:
        return " ".join(shlex.quote(str(a)) for a in (sys.executable, self.fake, reply, *extra))

    def user_source(self, name: str, text: str) -> Path:
        p = self.data / "sources" / f"{name}.yaml"
        p.write_text(textwrap.dedent(text), encoding="utf-8")
        return p

    def status_source(self, reply: Path, *extra, name="vendor-status"):
        text = (FIXTURES / "user" / "vendor-status.yaml").read_text()
        run = self.command(reply, *extra)
        text = text.replace("run: curl -s https://status.example.com/api/v2/incidents/unresolved.json",
                            f"run: '{run}'")
        self.user_source(name, text.replace("source: vendor-status", f"source: {name}"))

    def reply_copy(self, name: str) -> Path:
        p = self.tmp / name
        shutil.copy(REPLIES / name, p)
        return p


class CommandReadTest(EngineCase):
    def test_runs_the_command_and_saves_its_raw_stdout(self):
        reply = self.reply_copy("status-incidents.json")
        cwd_file = self.tmp / "cwd.txt"
        self.status_source(reply, cwd_file)
        result = self.engine().read("vendor-status")
        self.assertEqual(Path(cwd_file.read_text()).resolve(), self.data)  # the data folder is the working folder
        raw = self.data / result["raw"]
        self.assertRegex(result["raw"], r"^sources/\.raw/vendor-status/20261110T143005Z-[0-9a-f]{6}\.json$")
        self.assertEqual(raw.read_bytes(), reply.read_bytes())
        report = json.loads(raw.with_name(raw.stem + ".report.json").read_text())
        self.assertEqual(report, result)
        self.assertEqual([i["key"] for i in result["items"]], ["inc01abcd", "inc02efgh"])
        self.assertEqual(result["dropped"], [{"item": 2, "reason": "form.text missing"}])

    def test_the_raw_file_is_written_before_the_records_are_filled(self):
        reply = self.reply_copy("status-incidents.json")
        self.status_source(reply)
        seen = []
        real = Engine.fill

        def fill(src, data):
            seen.extend(p.read_bytes() for p in (self.data / "sources" / ".raw" / "vendor-status").glob("*.json"))
            return real(src, data)

        with mock.patch.object(Engine, "fill", staticmethod(fill)):
            self.engine().read("vendor-status")
        self.assertEqual(seen, [reply.read_bytes()])

    def test_output_that_is_not_json_is_still_saved(self):
        bad = self.tmp / "bad.json"
        bad.write_text("<html>maintenance</html>")
        self.status_source(bad)
        with self.assertRaisesRegex(SourceError, r"^vendor-status: the output is not JSON \(saved as sources/\.raw/"):
            self.engine().read("vendor-status")
        raws = list((self.data / "sources" / ".raw" / "vendor-status").glob("*.json"))
        self.assertEqual([p.read_text() for p in raws], ["<html>maintenance</html>"])
        self.assertEqual(self.store().list(), [])

    def test_a_failing_command_saves_its_stdout_and_fails(self):
        reply = self.reply_copy("status-incidents.json")
        self.status_source(reply, self.tmp / "cwd.txt", 3)
        with self.assertRaises(SourceError) as cm:
            self.engine().read("vendor-status")
        self.assertEqual(str(cm.exception), "vendor-status: read.run exited with 3: it went wrong")
        self.assertEqual(len(list((self.data / "sources" / ".raw" / "vendor-status").glob("*.json"))), 1)
        self.assertEqual(self.store().list(), [])

    def test_no_shell_runs_the_command(self):
        marker = self.tmp / "shell-ran"
        self.user_source("echo", f"""\
            source: echo
            brings: messages
            read:
              via: command
              run: "echo '[]' ; touch {marker}"
            fill: paths
            form: {{ id: .id, at: .at, text: .text }}
            link: none
            sort: {{ all: alert }}
            """)
        with self.assertRaisesRegex(SourceError, "^echo: the output is not JSON"):
            self.engine().read("echo")
        [raw] = (self.data / "sources" / ".raw" / "echo").glob("*.json")
        self.assertEqual(raw.read_text(), f"[] ; touch {marker}\n")  # echo got every word, ';' included
        self.assertFalse(marker.exists())

    def test_a_missing_program_and_a_timeout(self):
        self.user_source("gone", """\
            source: gone
            brings: messages
            read: { via: command, run: no-such-program-ap2 --json }
            fill: paths
            form: { id: .id, at: .at, text: .text }
            link: none
            sort: { all: alert }
            """)
        with self.assertRaisesRegex(SourceError, "^gone: cannot run 'no-such-program-ap2'"):
            self.engine().read("gone")
        slow = self.tmp / "slow.py"
        slow.write_text("import sys, time; sys.stdout.write('[1, 2'); sys.stdout.flush(); time.sleep(30)\n")
        self.user_source("slow", f"""\
            source: slow
            brings: messages
            read: {{ via: command, run: '{sys.executable} {slow}', timeout: 1 }}
            fill: paths
            form: {{ id: .id, at: .at, text: .text }}
            link: none
            sort: {{ all: alert }}
            """)
        with self.assertRaisesRegex(SourceError, "^slow: read.run took longer than 1 seconds; stopped$"):
            self.engine().read("slow", dry_run=True)
        self.assertFalse((self.data / "sources" / ".raw" / "slow").exists())  # a dry run keeps nothing
        with self.assertRaisesRegex(SourceError, r"^slow: read.run took longer than 1 seconds; stopped \(partial "
                                                 r"output saved as sources/\.raw/slow/[0-9TZ]+-[0-9a-f]{6}\.json\)$"):
            self.engine().read("slow")
        [raw] = (self.data / "sources" / ".raw" / "slow").glob("*.json")
        self.assertEqual(raw.read_text(), "[1, 2")

    def test_other_vias_are_not_run_here(self):
        with self.assertRaisesRegex(SourceError, r"read\.via is connector; only a scan session can make that read"):
            self.engine().read("slack-mentions")


class SavedReadTest(EngineCase):
    def test_read_saved_fills_from_a_file_and_keeps_a_copy(self):
        # An mcp-style source: the scan session saved the reply; the engine fills it with code.
        self.user_source("tracker", f"""\
            source: tracker
            brings: work
            read: {{ via: mcp, server: tracker, tool: search_issues }}
            fill: paths
            form:
              id: .key
              at: .fields.updated
              text: .fields.summary
              status: .fields.status.name
              depends_on: .fields.issuelinks[is blocked by].key
            link: "https://tracker.example.com/browse/{{id}}"
            key: "{{id}}"
            status_map: {{ "To Do": todo, "In Progress": in_progress }}
            sort: {{ all: code }}
            """)
        saved = self.reply_copy("jira-search.json")
        result = self.engine().read_saved("tracker", saved)
        self.assertEqual((self.data / result["raw"]).read_bytes(), saved.read_bytes())
        self.assertTrue(result["raw"].startswith("sources/.raw/tracker/"))
        cards = {c["key"]: c for c in self.store().list()}
        self.assertEqual(sorted(cards), ["PROJ-123", "PROJ-124"])
        first = cards["PROJ-123"]
        self.assertEqual((first["kind"], first["lane"], first["status"]), ("code", "planned", "in_progress"))
        self.assertEqual(first["source"]["record"]["depends_on"], ["PROJ-120", "PROJ-121"])
        self.assertEqual(first["source"]["link"], "https://tracker.example.com/browse/PROJ-123")
        self.assertEqual(cards["PROJ-124"]["status"], "todo")
        # a file already under .raw/<source>/ is not copied again
        again = self.engine().read_saved("tracker", self.data / result["raw"])
        self.assertEqual(again["raw"], result["raw"])

    SLACK_RAW = ("Message_ts: 1730000000.123456 thread_ts: 1729999000.000100 From: U0EXAMPLE1 Channel: C0123ABCD\n"
                 "Text: Can you look at the retry change?\n"
                 "Message_ts: 1730000001.000200 thread_ts: 1730000001.000200 Text: lunch?\n"
                 "Message_ts: 1730000002.000300 Text: x\n")

    def test_fill_ai_takes_the_scan_sessions_records(self):
        raw = self.tmp / "slack.txt"
        raw.write_text(self.SLACK_RAW)
        records = [
            {"id": "1730000000.123456", "thread_id": "1729999000.000100", "author": "U0EXAMPLE1",
             "at": "1730000000.123456", "text": "Can you look at the retry change?", "place": "C0123ABCD",
             "category": "question"},
            {"id": "1730000001.000200", "thread_id": "1730000001.000200", "at": "1730000001.000200",
             "text": "lunch?", "place": "C0123ABCD", "category": "fyi"},
            {"id": "1730000002.000300", "at": "1730000002.000300", "text": "x", "category": "gossip"},
            {"id": "", "at": "1", "text": "x", "category": "question"},
            # Invented by the model: not in what the tool returned.
            {"id": "9999999999.000001", "thread_id": "1729999000.000100", "at": "1730000000.123456",
             "text": "made up", "place": "C0123ABCD", "category": "question"},
            {"id": "1730000002.000300", "thread_id": "1111111111.000001", "at": "1730000002.000300",
             "text": "x", "place": "C0123ABCD", "category": "question"},
            {"id": "1730000002.000300", "at": "2026-11-10T14:00:00Z", "text": "x", "category": "question"},
            {"id": "1730000002.000300", "thread_id": "1730000002.000300", "at": "1730000002.000300",
             "text": "x", "place": "C0123ABCD", "category": ["question"]},
        ]
        result = self.engine().read_saved("slack-mentions", raw, records=records)
        self.assertEqual([(i["category"], i["becomes"], i["action"]) for i in result["items"]],
                         [("question", "question", "add"), ("fyi", "none", "none")])
        self.assertEqual(result["dropped"], [
            {"item": 2, "reason": "category 'gossip' is not one of sort's (question, ask_to_do, alert, fyi)"},
            {"item": 3, "reason": "form.id is empty"},
            {"item": 4, "reason": "form.id '9999999999.000001' is not in the raw output"},
            {"item": 5, "reason": "form.thread_id '1111111111.000001' is not in the raw output"},
            {"item": 6, "reason": "form.at '2026-11-10T14:00:00Z' is not in the raw output"},
            {"item": 7, "reason": "category ['question'] is not one of sort's (question, ask_to_do, alert, fyi)"}])
        [card] = self.store().list()
        self.assertEqual(card["source"]["link"], "https://slack.com/archives/C0123ABCD/p1730000000123456"  # checkpkg: ignore
                                                 "?thread_ts=1729999000.000100&cid=C0123ABCD")
        self.assertEqual(card["key"], "C0123ABCD:1729999000.000100")
        with self.assertRaisesRegex(SourceError, "fill: ai records are filled by a scan session"):
            self.engine().read_saved("slack-mentions", raw)

    def test_an_empty_thread_id_drops_the_record_instead_of_merging_items(self):
        # Two different messages with an empty thread_id would both get the key "C0123ABCD:" and become one card.
        raw = self.tmp / "slack.txt"
        raw.write_text(self.SLACK_RAW)
        records = [{"id": "1730000000.123456", "thread_id": "", "at": "1730000000.123456",
                    "text": "Can you look at the retry change?", "place": "C0123ABCD", "category": "question"},
                   {"id": "1730000002.000300", "thread_id": "", "at": "1730000002.000300", "text": "x",
                    "place": "C0123ABCD", "category": "question"}]
        result = self.engine().read_saved("slack-mentions", raw, records=records)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["dropped"], [{"item": 0, "reason": "key: {thread_id} is empty"},
                                             {"item": 1, "reason": "key: {thread_id} is empty"}])
        self.assertEqual(self.store().list(), [])

    def test_an_invented_id_makes_no_card(self):
        raw = self.tmp / "slack.txt"
        raw.write_text(self.SLACK_RAW)
        made_up = {"id": "9999999999.000001", "thread_id": "9999999999.000001", "at": "9999999999.000001",
                   "text": "made up", "place": "C0123ABCD", "category": "question"}
        result = self.engine().read_saved("slack-mentions", raw, records=[made_up])
        self.assertEqual(result["items"], [])
        self.assertEqual([d["item"] for d in result["dropped"]], [0])
        self.assertEqual(self.store().list(), [])

    def test_fill_paths_refuses_records(self):
        with self.assertRaisesRegex(SourceError, "fill: paths fills its own records"):
            self.engine().read_saved("gh-mine", self.reply_copy("gh-pr-list.json"), records=[])

    def test_a_missing_saved_file(self):
        with self.assertRaisesRegex(SourceError, "^gh-mine: cannot read the saved output"):
            self.engine().read_saved("gh-mine", self.tmp / "nope.json")


class SortToCardTest(EngineCase):
    def test_a_read_makes_cards_and_a_second_read_updates_them(self):
        reply = self.reply_copy("status-incidents.json")
        self.status_source(reply)
        first = self.engine().read("vendor-status")
        self.assertEqual([(i["action"], i["becomes"], i["category"]) for i in first["items"]],
                         [("add", "alert", "all"), ("add", "alert", "all")])
        cards = self.store().list()
        self.assertEqual(len(cards), 2)
        card = next(c for c in cards if c["key"] == "inc01abcd")
        self.assertEqual({k: card[k] for k in ("kind", "lane", "feed", "pri", "origin", "links", "title")},
                         {"kind": "alert", "lane": "needs", "feed": "vendor-status", "pri": "P2", "origin": "source",
                          "links": [], "title": "Elevated error rates on the payments API"})
        self.assertEqual(card["source"], {
            "name": "vendor-status", "link": "https://status.example.com/incidents/inc01abcd", "raw": first["raw"],
            "record": {"id": "inc01abcd", "at": "2026-11-10T13:40:05.000Z",
                       "text": "Elevated error rates on the payments API"}})
        self.assertEqual(card["plain"]["what"]["en"],
                         "New from vendor-status: Elevated error rates on the payments API")
        self.assertEqual(card["audit"][0]["by"], "source:vendor-status")

        # Same items again: nothing changes, so nothing is written (no audit entry, no restore point).
        self.clock.tick()
        again = self.engine().read("vendor-status")
        self.assertEqual([i["action"] for i in again["items"]], ["unchanged", "unchanged"])
        self.assertEqual(self.store().list(), cards)

        # The incident is renamed: the same key updates the same card instead of adding one.
        changed = json.loads(reply.read_text())
        changed["incidents"][0]["name"] = "Payments API errors are falling"
        reply.write_text(json.dumps(changed))
        self.clock.tick()
        third = self.engine().read("vendor-status")
        self.assertEqual([i["action"] for i in third["items"]], ["update", "unchanged"])
        self.assertEqual(third["items"][0]["card"], card["id"])
        after = self.store().list()
        self.assertEqual(len(after), 2)
        updated = self.store().get(card["id"])
        self.assertEqual(updated["title"], "Payments API errors are falling")
        self.assertEqual(updated["source"]["raw"], third["raw"])
        self.assertEqual(updated["audit"][-1]["did"], "read vendor-status")
        self.assertIsNotNone(updated["audit"][-1]["restore"])
        self.assertEqual(len(updated["audit"]), 2)

    def test_a_same_key_update_leaves_kind_lane_pri_and_ask_alone(self):
        reply = self.reply_copy("status-incidents.json")
        self.status_source(reply)
        self.engine().read("vendor-status")
        card = next(c for c in self.store().list() if c["key"] == "inc01abcd")
        ask = {"type": "answer", "text": "Is this still happening?"}
        self.clock.tick()
        moved = self.store().update(card["id"], {"lane": "done", "pri": "P0", "ask": ask, "kind": "question"},
                                    by="tester")
        changed = json.loads(reply.read_text())
        changed["incidents"][0]["name"] = "Payments API errors are falling"
        reply.write_text(json.dumps(changed))
        self.clock.tick()
        result = self.engine().read("vendor-status")
        self.assertEqual(result["items"][0]["action"], "update")
        after = self.store().get(card["id"])
        self.assertEqual({k: after[k] for k in ("kind", "lane", "pri", "ask")},
                         {"kind": "question", "lane": "done", "pri": "P0", "ask": ask})
        for k in ("title", "plain", "source"):
            self.assertNotEqual(after[k], moved[k], k)
        self.assertEqual(after["title"], "Payments API errors are falling")

    def test_the_same_key_twice_in_one_read_is_one_card(self):
        reply = self.tmp / "dup.json"
        incidents = json.loads((REPLIES / "status-incidents.json").read_text())
        incidents["incidents"][1]["id"] = "inc01abcd"
        reply.write_text(json.dumps(incidents))
        self.status_source(reply)
        result = self.engine().read("vendor-status")
        self.assertEqual([i["action"] for i in result["items"]], ["add", "update"])
        self.assertEqual(len(self.store().list()), 1)

    def test_update_card_only_updates_and_none_makes_nothing(self):
        reply = self.reply_copy("status-incidents.json")
        self.status_source(reply)
        self.engine().read("vendor-status")
        before = self.store().list()
        text = (self.data / "sources" / "vendor-status.yaml").read_text()
        self.user_source("vendor-updates", text.replace("source: vendor-status", "source: vendor-updates")
                         .replace("sort: { all: alert }", "sort: { all: update_card }"))
        changed = json.loads(reply.read_text())
        changed["incidents"][0]["name"] = "Fixed"
        changed["incidents"][1]["id"] = "inc09new"
        reply.write_text(json.dumps(changed))
        result = self.engine().read("vendor-updates")
        self.assertEqual([(i["action"], i.get("reason")) for i in result["items"]],
                         [("update", None), ("none", "no card has this key")])
        self.assertEqual(len(self.store().list()), len(before))
        self.user_source("vendor-quiet", text.replace("source: vendor-status", "source: vendor-quiet")
                         .replace("sort: { all: alert }", "sort: { all: none }"))
        quiet = self.engine().read("vendor-quiet")
        self.assertEqual({i["action"] for i in quiet["items"]}, {"none"})
        self.assertEqual(len(self.store().list()), len(before))

    def test_several_categories_on_fill_paths_make_no_card(self):
        reply = self.reply_copy("status-incidents.json")
        text = (FIXTURES / "user" / "vendor-status.yaml").read_text()
        self.user_source("vendor-status", text.replace("run: curl -s https://status.example.com/api/v2/incidents/"
                                                       "unresolved.json", f"run: '{self.command(reply)}'")
                         .replace("sort: { all: alert }", "sort: { alert: alert, fyi: none }"))
        result = self.engine().read("vendor-status")
        self.assertEqual({(i["action"], i["reason"]) for i in result["items"]},
                         {("none", "sort has 2 categories; fill: paths cannot pick one")})
        self.assertEqual(self.store().list(), [])

    def test_plain_words_in_both_languages(self):
        cfg = Config(secondary_language="zh-Hans")
        reply = self.reply_copy("gh-pr-list.json")
        text = (self.plugin / "gh-mine.yaml").read_text().replace(
            "run: gh pr list --author @me --json number,title,url,author,createdAt,headRepository",
            f"run: '{self.command(reply)}'")
        (self.plugin / "gh-mine.yaml").write_text(text)
        self.engine(cfg).read("gh-mine")
        card = next(c for c in Store(self.data, cfg=cfg).list() if c["key"] == "widgets#42")
        self.assertEqual(card["plain"], {
            "what": {"en": "New from gh-mine by octo-dev in widgets: Retry failed card payments once, 2 seconds later",
                     "zh-Hans": "来自 gh-mine 的新内容，作者 octo-dev，在 widgets：Retry failed card payments once, 2 seconds later"},
            "where": {"en": "It was written at 2026-11-09T17:02:11Z. Nothing has been done with it yet.",
                      "zh-Hans": "写于 2026-11-09T17:02:11Z。还没有人处理它。"},
            "do": {"en": "Read it at the link and decide what it needs.",
                   "zh-Hans": "看一下（点链接看全文），再决定要怎么处理。"}})
        self.assertEqual(card["kind"], "pr_review")

    def test_a_language_without_code_made_words_is_refused_before_anything(self):
        reply = self.reply_copy("status-incidents.json")
        self.status_source(reply)
        with self.assertRaisesRegex(SourceError, r"no code-made plain words for fr yet \(built in: en, zh\)"):
            self.engine(Config(secondary_language="fr")).read("vendor-status")
        self.assertEqual(self.store().list(), [])
        self.assertFalse((self.data / "sources" / ".raw").exists())  # refused before the read ran

    def test_snippet(self):
        self.assertEqual(snippet("\n  first   line \nsecond"), "first line")
        self.assertEqual(snippet("x" * 200, 10), "xxxxxxxxx…")

    def test_work_plain_words_name_the_status(self):
        self.user_source("w", ShadowTest.WORK.format(run="echo []"))
        src = Loader(self.data, plugin_dir=self.plugin)
        words = plain_words(src.load("w"), {"id": "P", "at": "t", "text": "x", "status": "To Do"}, None, ("en",))
        self.assertEqual(words["where"]["en"], 'It was written at t. Its status there is "To Do". Nothing has been '
                                               "done with it yet.")
        self.assertEqual(words["do"]["en"], "Nothing yet; it waits in Planned until it is started.")


class DryRunTest(EngineCase):
    def test_a_dry_run_saves_nothing_and_says_what_each_item_would_become(self):
        reply = self.reply_copy("status-incidents.json")
        self.status_source(reply)
        before = tree(self.data)
        result = self.engine().read("vendor-status", dry_run=True)
        self.assertEqual(tree(self.data), before)
        self.assertTrue(result["dry_run"])
        self.assertIsNone(result["raw"])
        self.assertEqual([(i["action"], i["card"], i["would_add"]["kind"]) for i in result["items"]],
                         [("add", None, "alert"), ("add", None, "alert")])
        self.engine().read("vendor-status")
        changed = json.loads(reply.read_text())
        changed["incidents"][0]["name"] = "Renamed"
        reply.write_text(json.dumps(changed))
        before = tree(self.data)
        result = self.engine().read("vendor-status", dry_run=True)
        self.assertEqual(tree(self.data), before)
        self.assertEqual([(i["action"], i.get("would_change")) for i in result["items"]],
                         [("update", ["plain", "source", "title"]), ("unchanged", None)])


class ShadowTest(EngineCase):
    WORK = """\
        source: w
        brings: work
        read:
          via: command
          run: '{run}'
        fill: paths
        form:
          id: .key
          at: .fields.updated
          text: .fields.summary
          status: .fields.status.name
        link: "https://tracker.example.com/browse/{{id}}"
        key: "{{id}}"
        sort: {{ all: code }}
        writes:
          on_start: {{ transition: "In Progress", assign: me }}
          on_merge: {{ transition: "Done" }}
        never: [create_ticket, comment]
        """

    def test_writes_are_refused_in_shadow_mode_and_nothing_outside_the_data_folder_changes(self):
        reply = self.reply_copy("jira-search.json")
        self.user_source("w", self.WORK.format(run=self.command(reply)))
        outside = {p: tree(p) for p in (self.tmp / "bin", self.plugin)}
        outside_files = sorted(p.name for p in self.tmp.iterdir())
        engine = self.engine()
        engine.read("w")  # reading is not a write: shadow mode allows it
        cards = self.store().list()
        self.assertEqual(len(cards), 2)
        for event in ("on_start", "on_merge"):
            with self.subTest(event):
                with mock.patch("subprocess.run") as run, mock.patch("subprocess.Popen") as popen:
                    with self.assertRaises(ShadowModeError) as cm:
                        engine.apply_writes("w", event, cards[0]["id"])
                run.assert_not_called()
                popen.assert_not_called()
                self.assertIn(f"shadow mode: refusing to apply w's writes.{event} for {cards[0]['id']}",
                              str(cm.exception))
        self.assertIsNone(engine.apply_writes("w", "on_pr"))  # nothing to write for that event
        self.assertEqual({p: tree(p) for p in (self.tmp / "bin", self.plugin)}, outside)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), outside_files)
        self.assertEqual(self.store().list(), cards)  # and no card changed either

    def test_require_live_is_called_first_and_live_mode_still_runs_nothing(self):
        reply = self.reply_copy("jira-search.json")
        self.user_source("w", self.WORK.format(run=self.command(reply)))
        with mock.patch("assistant.config.require_live", side_effect=ShadowModeError("stop")) as live:
            with self.assertRaises(ShadowModeError):
                self.engine(Config(mode="live")).apply_writes("w", "on_start")
        live.assert_called_once()
        with mock.patch("subprocess.run") as run:
            with self.assertRaisesRegex(SourceError, "writes.on_start is not run by this version of ap2"):
                self.engine(Config(mode="live")).apply_writes("w", "on_start")
        run.assert_not_called()
        with self.assertRaisesRegex(SourceError, "unknown write event 'on_close'"):
            self.engine().apply_writes("w", "on_close")


if __name__ == "__main__":
    unittest.main()
