"""The built-in source files (design 4.2, 5.7, appendix A and B), their settings, Slack thread keys, the
word-for-word token rule and the Jira status_map / writes / never checks. Every read here is a recorded reply."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT
from tests.test_ap2_cli import temp_inside_the_accounts_claude
from assistant import datadir, engine, form, sources
from assistant.config import Config, ShadowModeError
from assistant.engine import Engine
from assistant.sources import Loader, SourceError
from assistant.store import Store

AP2 = ROOT / "bin" / "ap2"
SCAN = ROOT / "tests" / "fixtures" / "ap2" / "scan"
BUILTIN = ("github-my-prs", "github-review-requests", "jira-epic", "jira-ticket", "slack-alerts", "slack-mentions")
TEMPLATES = ROOT / "lib" / "assistant" / "builtin" / "templates"


class Clock:
    def __init__(self):
        self.at = datetime(2026, 11, 10, 14, 30, 5, tzinfo=timezone.utc)

    def __call__(self):
        return self.at

    def tick(self, seconds=60):
        self.at += timedelta(seconds=seconds)


def tree(folder: Path) -> dict:
    return {str(p.relative_to(folder)): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


class BuiltinCase(unittest.TestCase):
    cfg = Config()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-builtin-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = datadir.ensure(self.tmp / "data")
        self.clock = Clock()

    def engine(self, cfg=None):
        # plugin_dir left out: the real built-in folder, sources.PLUGIN_DIR
        return Engine(self.data, cfg=cfg or self.cfg, clock=self.clock)

    def store(self):
        return Store(self.data, cfg=self.cfg, clock=self.clock)

    def user(self, name: str, text: str) -> Path:
        p = self.data / "sources" / f"{name}.yaml"
        p.write_text(textwrap.dedent(text), encoding="utf-8")
        return p

    def copy(self, name: str) -> Path:
        dest = self.tmp / name
        shutil.copy(SCAN / name, dest)
        return dest

    def epic(self, name="jira-epic", epic="PROJ-1"):
        return self.user(name, f"extends: jira-epic\nsettings: {{ site: jira.example.com, epic: {epic} }}\n")


class BuiltinFilesTest(BuiltinCase):
    def test_the_builtin_folder_holds_the_six_files_and_nothing_else(self):
        self.assertEqual(sorted(p.name for p in sources.PLUGIN_DIR.iterdir()), [f"{n}.yaml" for n in BUILTIN])

    def test_every_file_loads_through_the_real_plugin_dir(self):
        loaded = Loader(self.data).load_all()
        self.assertEqual([s.name for s in loaded], list(BUILTIN))
        for s in loaded:
            self.assertEqual(s.origin, "built-in")
            self.assertEqual(s.file.parent, sources.PLUGIN_DIR)

    def test_every_sort_target_is_a_builtin_template_kind(self):
        kinds = {p.stem for p in TEMPLATES.glob("*.yaml")}
        for s in Loader(self.data).load_all():
            for cat, to in s.sort.items():
                with self.subTest(source=s.name, category=cat):
                    self.assertTrue(to in sources.SORT_SPECIAL or to in kinds, to)

    def test_the_files_say_what_the_design_says(self):
        load = Loader(self.data).load
        mentions = load("slack-mentions").data
        self.assertEqual(mentions["read"]["via"], "connector")
        self.assertEqual(mentions["read"]["tools"], ["slack_search_public", "slack_read_thread"])
        self.assertEqual(mentions["read"]["query"], "<@{me}>")
        self.assertEqual(mentions["fill"], "ai")
        self.assertIn("place_id", mentions["form"])
        self.assertEqual(mentions["key"], "{place_id}:{thread_or_id}")
        self.assertIn("thread_ts={thread_or_id}", mentions["link"])
        alerts = load("slack-alerts").data
        self.assertEqual((alerts["read"]["via"], alerts["read"]["tools"], alerts["read"]["channel_id"]),
                         ("connector", ["slack_read_channel"], "{alert_channel}"))
        self.assertEqual(alerts["sort"], {"alert": "alert", "recovered": "update_card", "fyi": "none"})
        self.assertEqual(alerts["key"], "{place_id}:{thread_or_id}")
        for name, flag in (("github-review-requests", "--review-requested=@me"), ("github-my-prs", "--author=@me")):
            gh = load(name).data
            self.assertEqual(gh["read"]["run"], f"gh search prs {flag} --state=open "
                                                f"--json number,title,url,author,createdAt,repository")
            self.assertEqual(gh["form"], {"id": ".number", "at": ".createdAt", "text": ".title",
                                          "author": ".author.login", "place": ".repository.nameWithOwner"})
            self.assertEqual((gh["link"], gh["key"], gh["sort"]), (".url", "{place}#{id}", {"all": "pr-review"}))
        epic = load("jira-epic").data
        self.assertEqual(epic["form"]["at"], ".issues[].fields.updated")
        self.assertEqual(epic["sort"], {"all": "code"})
        ticket = load("jira-ticket").data
        self.assertEqual((ticket["read"]["via"], ticket["read"]["issue"], ticket["plan"]),
                         ("mcp", "{ticket}", "split_here"))
        self.assertNotIn("tools", ticket["read"])
        self.assertNotIn("[]", "".join(ticket["form"].values()).replace("[type.", ""))  # one issue, one item
        for name in ("jira-epic", "jira-ticket"):
            d = load(name).data
            self.assertEqual(d["status_map"], {"To Do": "todo", "In Progress": "in_progress",
                                               "In Review": "in_review", "Done": "done"})
            self.assertEqual(d["writes"], {"on_start": {"transition": "In Progress", "assign": "me"},
                                           "on_pr": {"transition": "In Review", "remote_link": "pr"},
                                           "on_merge": {"transition": "Done"}})
            self.assertEqual(d["never"], ["create_ticket", "comment"])

    def test_source_list_lists_all_six_as_builtin(self):
        if temp_inside_the_accounts_claude():
            self.skipTest("the temp folder is inside the account's ~/.claude, so ap2 refuses every temp data folder")
        env = {k: v for k, v in os.environ.items() if k not in ("AP2_DATA", "LC_ALL")}
        env.update(HOME=str(self.tmp), AP2_DATA=str(self.data), PYTHONIOENCODING="utf-8", PYTHON_COLORS="0")
        r = subprocess.run([sys.executable, str(AP2), "source", "list"], capture_output=True, text=True, env=env,
                           encoding="utf-8", timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual([(s["source"], s["origin"]) for s in out], [(n, "built-in") for n in BUILTIN])


class FillPathsTest(BuiltinCase):
    """Each fill: paths built-in fills its recorded reply into the expected records (no command is run)."""

    def test_github_review_requests_and_my_prs(self):
        for name in ("github-review-requests", "github-my-prs"):
            with self.subTest(name):
                result = self.engine().read_saved(name, self.copy("gh-search-prs.json"), dry_run=True)
                self.assertEqual(result["dropped"], [])
                self.assertEqual([(i["record"], i["key"], i["link"], i["becomes"]) for i in result["items"]], [
                    ({"id": "42", "at": "2026-11-09T17:02:11Z", "text": "Retry failed card payments once, 2 seconds "
                      "later", "author": "octo-dev", "place": "example/widgets"}, "example/widgets#42",
                     "https://github.com/example/widgets/pull/42", "pr-review"),
                    ({"id": "7", "at": "2026-11-10T08:45:00Z", "text": "Show the delivery date on the order page",
                      "author": "octo-ops", "place": "example/gadgets"}, "example/gadgets#7",
                     "https://github.com/example/gadgets/pull/7", "pr-review")])

    def test_jira_epic(self):
        self.epic()
        result = self.engine().read_saved("jira-epic", self.copy("jira-epic-issues.json"))
        self.assertEqual(result["dropped"], [])
        self.assertEqual([i["record"] for i in result["items"]], [
            {"id": "PROJ-123", "at": "2026-11-10T10:00:00.000+0000", "text": "Retry failed card payments",
             "status": "In Progress", "depends_on": ["PROJ-120", "PROJ-121"]},
            {"id": "PROJ-124", "at": "2026-11-10T11:00:00.000+0000", "text": "Show the retry in the order history",
             "status": "To Do"},
            {"id": "PROJ-125", "at": "2026-11-10T12:00:00.000+0000", "text": "Write the retry runbook",
             "status": "Blocked by vendor"}])
        cards = {c["key"]: c for c in self.store().list()}
        self.assertEqual(cards["PROJ-123"]["source"]["link"], "https://jira.example.com/browse/PROJ-123")
        self.assertEqual({k: (c["kind"], c["lane"], c["status"]) for k, c in cards.items()}, {
            "PROJ-123": ("code", "planned", "in_progress"), "PROJ-124": ("code", "planned", "todo"),
            "PROJ-125": ("code", "planned", "Blocked by vendor")})  # not in the map: kept as written

    def test_jira_epic_fails_loudly_on_an_error_reply(self):
        self.epic()
        bad = self.tmp / "error.json"
        bad.write_text('{"errorMessages": ["The value \'PROJ-1\' does not exist for the field \'parent\'."]}')
        with self.assertRaisesRegex(SourceError, r"^jira-epic: the reply has no list at \.issues"):
            self.engine().read_saved("jira-epic", bad)
        self.assertEqual(self.store().list(), [])

    def test_jira_ticket_is_one_issue(self):
        self.user("jira-ticket", "extends: jira-ticket\nsettings: { site: jira.example.com, ticket: PROJ-7 }\n")
        result = self.engine().read_saved("jira-ticket", self.copy("jira-issue.json"))
        self.assertEqual(result["dropped"], [])
        [item] = result["items"]
        self.assertEqual(item["record"], {"id": "PROJ-7", "at": "2026-11-10T09:00:00.000+0000",
                                          "text": "Split the retry work into PR-sized tasks", "status": "In Review",
                                          "depends_on": ["PROJ-5"]})
        self.assertEqual((item["key"], item["link"], item["becomes"]),
                         ("PROJ-7", "https://jira.example.com/browse/PROJ-7", "code"))
        [card] = self.store().list()
        self.assertEqual(card["status"], "in_review")


class SettingsTest(BuiltinCase):
    def test_builtin_jira_epic_with_no_settings_is_refused_before_the_copy(self):
        reply = self.copy("jira-epic-issues.json")
        with self.assertRaises(SourceError) as cm:
            self.engine().read_saved("jira-epic", reply)
        self.assertEqual(str(cm.exception), "jira-epic: settings.site is not set; add it in "
                                            "<data>/sources/jira-epic.yaml (extends: jira-epic)")
        self.assertFalse((self.data / "sources" / ".raw").exists())
        self.assertEqual(self.store().list(), [])

    def test_a_user_file_with_one_setting_missing_is_told_its_own_file(self):
        p = self.user("jira-epic", "extends: jira-epic\nsettings: { site: jira.example.com }\n")
        with self.assertRaises(SourceError) as cm:
            self.engine().read_saved("jira-epic", self.copy("jira-epic-issues.json"))
        self.assertEqual(str(cm.exception), f"jira-epic: settings.epic is not set; add it in {p}")
        self.assertFalse((self.data / "sources" / ".raw").exists())

    def test_with_site_and_epic_the_card_link_is_the_sites(self):
        self.epic()
        self.engine().read_saved("jira-epic", self.copy("jira-epic-issues.json"))
        cards = {c["key"]: c for c in self.store().list()}
        self.assertEqual(cards["PROJ-123"]["source"]["link"], "https://jira.example.com/browse/PROJ-123")

    def test_a_setting_goes_into_the_link_as_written(self):
        self.user("jira-epic", "extends: jira-epic\nsettings: { site: \"jira.example.com:8443/tracker\", epic: PROJ-1 }\n")
        result = self.engine().read_saved("jira-epic", self.copy("jira-epic-issues.json"), dry_run=True)
        self.assertEqual(result["items"][0]["link"], "https://jira.example.com:8443/tracker/browse/PROJ-123")

    def test_two_epic_files_make_two_sources(self):
        self.epic("epic-payments", "PROJ-1")
        self.epic("epic-orders", "PROJ-2")
        loader = Loader(self.data)
        a, b = loader.load("epic-payments"), loader.load("epic-orders")
        self.assertEqual((sources.settings(a), sources.settings(b)),
                         ({"site": "jira.example.com", "epic": "PROJ-1"}, {"site": "jira.example.com", "epic": "PROJ-2"}))
        self.assertEqual((a.origin, b.origin), ("user, extends jira-epic", "user, extends jira-epic"))
        self.assertEqual(sources.fill_args(a)["jql"],
                         "parent = PROJ-1 AND (assignee = currentUser() OR assignee is EMPTY)")
        self.assertEqual(sources.fill_args(b)["jql"],
                         "parent = PROJ-2 AND (assignee = currentUser() OR assignee is EMPTY)")
        self.assertIn("epic-orders", [s.name for s in loader.load_all()])
        self.assertIn("epic-payments", [s.name for s in loader.load_all()])

    def test_a_setting_named_like_a_field_is_refused_with_its_line(self):
        for name in ("id", "place_id", "thread_or_id", "source", "id_digits", "status"):
            with self.subTest(name):
                p = self.user("jira-epic", f"extends: jira-epic\nsettings:\n  site: jira.example.com\n  {name}: x\n")
                with self.assertRaises(SourceError) as cm:
                    Loader(self.data).load("jira-epic")
                self.assertIn(f"{p} (merged over {sources.PLUGIN_DIR / 'jira-epic.yaml'}): line 4: settings.{name}: "
                              f"'{name}' is a form or derived field", str(cm.exception))

    def test_bad_settings_are_refused(self):
        for text, reason in (("settings: [a]\n", "settings must map each name to its text"),
                             ("settings: { Site: x }\n", "line 2: settings: 'Site' must be lowercase letters"),
                             ("settings: { site: [a] }\n", "line 2: settings.site must be a text")):
            with self.subTest(text):
                self.user("jira-epic", "extends: jira-epic\n" + text)
                with self.assertRaisesRegex(SourceError, reason):
                    Loader(self.data).load("jira-epic")

    def test_a_name_that_is_neither_a_field_nor_a_setting_still_drops_the_record(self):
        self.user("jira-epic", 'extends: jira-epic\nsettings: { site: jira.example.com, epic: PROJ-1 }\n'
                               'link: "https://{host}/browse/{id}"\n')
        result = self.engine().read_saved("jira-epic", self.copy("jira-epic-issues.json"), dry_run=True)
        self.assertEqual(result["items"], [])
        self.assertEqual({d["reason"] for d in result["dropped"]}, {"link: {host} missing"})

    def test_an_arg_naming_no_setting_is_refused_in_the_args(self):
        self.user("jira-epic", 'extends: jira-epic\nsettings: { site: jira.example.com, epic: PROJ-1 }\n'
                               'read: { jql: "parent = {project}" }\n')
        with self.assertRaisesRegex(SourceError, r"^jira-epic: read\.jql names \{project\}, which is not in settings"):
            sources.fill_args(Loader(self.data).load("jira-epic"))


class SlackThreadTest(BuiltinCase):
    def setUp(self):
        super().setUp()
        self.user("slack-mentions", """\
            extends: slack-mentions
            settings: { me: U0EXAMPLE1 }
            read:
              skip_channels: ["#random"]
            """)
        self.records = json.loads((SCAN / "slack-mentions.records.json").read_text())

    def test_thread_or_id_is_derived_by_code(self):
        self.assertIn("thread_or_id", form.DERIVED)
        self.assertEqual(form.derived({"id": "1.2"}, "s")["thread_or_id"], "1.2")
        self.assertEqual(form.derived({"id": "1.2", "thread_id": "  "}, "s")["thread_or_id"], "1.2")
        self.assertEqual(form.derived({"id": "1.2", "thread_id": "1.1"}, "s")["thread_or_id"], "1.1")
        self.assertNotIn("thread_id", form.derived({"id": "1.2"}, "s"))  # {thread_id} keeps its meaning

    def test_a_top_level_message_and_a_reply_give_one_card(self):
        raw = self.copy("slack-search.txt")
        result = self.engine().read_saved("slack-mentions", raw, records=self.records[:2])
        self.assertEqual(result["dropped"], [])
        self.assertEqual([(i["item"], i["key"], i["action"]) for i in result["items"]],
                         [(0, "C0123ABCD:1730000000.123456", "add"), (1, "C0123ABCD:1730000000.123456", "unchanged")])
        [card] = self.store().list()
        self.assertEqual(card["key"], "C0123ABCD:1730000000.123456")
        self.assertEqual(card["source"]["record"]["id"], "1730000100.000200")  # the latest message's words
        # The reply alone (a later read, the top-level message gone from the search) keys on the same card.
        self.clock.tick()
        again = self.engine().read_saved("slack-mentions", raw, records=self.records[1:2])
        self.assertEqual([(i["action"], i["card"]) for i in again["items"]], [("unchanged", card["id"])])
        # The order the model wrote them in does not matter: a re-read is unchanged.
        again = self.engine().read_saved("slack-mentions", raw, records=self.records[1::-1])
        self.assertEqual([i["action"] for i in again["items"]], ["unchanged", "unchanged"])
        self.assertEqual(len(self.store().list()), 1)
        self.assertEqual(len(self.store().get(card["id"])["audit"]), 1)  # added once, never rewritten

    def test_one_thread_in_one_read_keeps_the_first_card_making_category(self):
        self.user("slack-alerts", "extends: slack-alerts\nsettings: { alert_channel: C0444ALRT }\n")
        raw = self.tmp / "alerts.txt"
        raw.write_text("Channel: #alerts (C0444ALRT)\nMessage_ts: 1730000500.000100\nText: checkout errors high\n"
                       "Message_ts: 1730000900.000200 thread_ts: 1730000500.000100\nText: recovered\n")
        recovered = {"id": "1730000900.000200", "thread_id": "1730000500.000100", "at": "1730000900.000200",
                     "text": "recovered", "place_id": "C0444ALRT", "category": "recovered"}
        alert = {"id": "1730000500.000100", "at": "1730000500.000100", "text": "checkout errors high",
                 "place_id": "C0444ALRT", "category": "alert"}
        result = self.engine().read_saved("slack-alerts", raw, records=[recovered, alert])
        self.assertEqual([(i["item"], i["category"], i["becomes"], i["action"]) for i in result["items"]],
                         [(0, "alert", "alert", "add"), (1, "alert", "alert", "unchanged")])
        [card] = self.store().list()
        self.assertEqual((card["kind"], card["title"]), ("alert", "recovered"))

    def test_a_top_level_messages_link_uses_its_own_ts(self):
        result = self.engine().read_saved("slack-mentions", self.copy("slack-search.txt"), records=self.records[:1],
                                          dry_run=True)
        [item] = result["items"]
        self.assertEqual(item["link"], "https://slack.com/archives/C0123ABCD/p1730000000123456"  # checkpkg: ignore
                                       "?thread_ts=1730000000.123456&cid=C0123ABCD")

    def test_a_record_from_a_skipped_channel_is_dropped(self):
        result = self.engine().read_saved("slack-mentions", self.copy("slack-search.txt"), records=self.records[:3])
        self.assertEqual(result["dropped"], [{"item": 2, "reason": "form.place 'random' is in read.skip_channels"}])
        by_id = dict(self.records[2], place="#elsewhere")
        result = self.engine().read_saved("slack-mentions", self.copy("slack-search.txt"),
                                          records=[{**by_id, "place_id": "C0999RAND"}], dry_run=True)
        self.assertEqual(result["dropped"], [])  # skip_channels names #random, not its id: kept
        self.user("slack-mentions", "extends: slack-mentions\nsettings: { me: U0EXAMPLE1 }\n"
                                    "read: { skip_channels: [C0999RAND] }\n")
        result = self.engine().read_saved("slack-mentions", self.copy("slack-search.txt"),
                                          records=[{**by_id, "place_id": "C0999RAND"}], dry_run=True)
        self.assertEqual(result["dropped"], [{"item": 0, "reason": "form.place_id 'C0999RAND' is in "
                                                                   "read.skip_channels"}])

    def test_skip_channels_must_be_a_list_of_texts(self):
        self.user("slack-mentions", "extends: slack-mentions\nread: { skip_channels: \"#random\" }\n")
        with self.assertRaisesRegex(SourceError, "line 2: read.skip_channels must be a list"):
            Loader(self.data).load("slack-mentions")

    def test_slack_alerts_recovery_updates_the_alert_card(self):
        self.user("slack-alerts", "extends: slack-alerts\nsettings: { alert_channel: C0444ALRT }\n")
        raw = self.tmp / "alerts.txt"
        raw.write_text("Channel: #alerts (C0444ALRT)\nMessage_ts: 1730000500.000100\nText: checkout errors high\n"
                       "Message_ts: 1730000900.000200 thread_ts: 1730000500.000100\nText: recovered\n")
        alert = {"id": "1730000500.000100", "at": "1730000500.000100", "text": "checkout errors high",
                 "place": "alerts", "place_id": "C0444ALRT", "category": "alert"}
        self.engine().read_saved("slack-alerts", raw, records=[alert])
        recovered = {"id": "1730000900.000200", "thread_id": "1730000500.000100", "at": "1730000900.000200",
                     "text": "recovered", "place": "alerts", "place_id": "C0444ALRT", "category": "recovered"}
        result = self.engine().read_saved("slack-alerts", raw, records=[recovered])
        self.assertEqual([(i["becomes"], i["action"]) for i in result["items"]], [("update_card", "update")])
        [card] = self.store().list()
        self.assertEqual((card["kind"], card["title"]), ("alert", "recovered"))

    def test_a_recovery_then_an_fyi_reply_still_updates_the_alert_card_in_either_order(self):
        raw = self.tmp / "alerts.txt"
        raw.write_text("Channel: #alerts (C0444ALRT)\nMessage_ts: 1730000500.000100\nText: checkout errors high\n"
                       "Message_ts: 1730000900.000200 thread_ts: 1730000500.000100\nText: recovered\n"
                       "Message_ts: 1730000950.000300 thread_ts: 1730000500.000100\nText: thanks all\n")
        alert = {"id": "1730000500.000100", "at": "1730000500.000100", "text": "checkout errors high",
                 "place_id": "C0444ALRT", "category": "alert"}
        recovered = {"id": "1730000900.000200", "thread_id": "1730000500.000100", "at": "1730000900.000200",
                     "text": "recovered", "place_id": "C0444ALRT", "category": "recovered"}
        fyi = {"id": "1730000950.000300", "thread_id": "1730000500.000100", "at": "1730000950.000300",
               "text": "thanks all", "place_id": "C0444ALRT", "category": "fyi"}
        for n, records in enumerate(([recovered, fyi], [fyi, recovered])):
            with self.subTest([r["category"] for r in records]):
                self.data = datadir.ensure(self.tmp / f"data-{n}")  # a fresh store for each order
                self.user("slack-alerts", "extends: slack-alerts\nsettings: { alert_channel: C0444ALRT }\n")
                self.engine().read_saved("slack-alerts", raw, records=[alert])
                self.clock.tick()
                result = self.engine().read_saved("slack-alerts", raw, records=records)
                self.assertEqual([(i["category"], i["becomes"], i["action"]) for i in result["items"]],
                                 [("recovered", "update_card", "update"), ("recovered", "update_card", "unchanged")])
                [card] = self.store().list()
                self.assertEqual((card["kind"], card["title"]), ("alert", "thanks all"))  # the latest's words


    def test_with_no_card_making_category_a_thread_takes_the_latest_update_card_one(self):
        self.user("pings", "source: pings\nbrings: messages\nread: { via: connector, tool: read_pings }\n"
                           "fill: ai\nform: { id: x, thread_id: x, at: x, text: x }\nlink: none\n"
                           "key: \"{thread_or_id}\"\n"
                           "sort: { a: alert, r1: update_card, r2: update_card, f: none }\n")
        raw = self.tmp / "pings.txt"
        raw.write_text("1730000100.000001 thread 1730000000.000001\n1730000200.000002 thread 1730000000.000001\n"
                       "1730000300.000003 thread 1730000000.000001\n")
        thread = "1730000000.000001"
        r1 = {"id": "1730000100.000001", "thread_id": thread, "at": "1730000100.000001", "text": "x", "category": "r1"}
        r2 = {"id": "1730000200.000002", "thread_id": thread, "at": "1730000200.000002", "text": "y", "category": "r2"}
        f = {"id": "1730000300.000003", "thread_id": thread, "at": "1730000300.000003", "text": "z", "category": "f"}
        for records in ([r1, r2, f], [f, r2, r1]):
            with self.subTest([r["category"] for r in records]):
                result = self.engine().read_saved("pings", raw, records=records, dry_run=True)
                self.assertEqual(result["dropped"], [])
                self.assertEqual([i["category"] for i in result["items"]], ["r2", "r2", "r2"])

class WordForWordTest(BuiltinCase):
    def test_place_id_is_checked_word_for_word(self):
        self.assertIn("place_id", engine.WORD_FOR_WORD)

    def test_a_place_that_stands_in_for_place_id_is_checked_word_for_word(self):
        self.user("slack-mentions", "extends: slack-mentions\nsettings: { me: U0EXAMPLE1 }\n")
        record = {k: v for k, v in json.loads((SCAN / "slack-mentions.records.json").read_text())[0].items()
                  if k != "place_id"}
        made_up = dict(record, place="C0NOTINRAW")
        result = self.engine().read_saved("slack-mentions", self.copy("slack-search.txt"),
                                          records=[made_up, record], dry_run=True)
        self.assertEqual(result["dropped"], [{"item": 0, "reason": "form.place 'C0NOTINRAW' is not in the raw "
                                                                   "output"}])  # the field the record has
        self.assertEqual([(i["item"], i["key"]) for i in result["items"]],
                         [(1, "payments:1730000000.123456")])  # "#payments" is in the raw: kept

    def test_the_token_edge_table(self):
        cases = [
            ("1730000000", "ts 1730000000.123456 here", False),
            ("C0123", "in C0123ABCD", False),
            ("C0123", "in <#C0123|x>", True),
            ("1730000000.123456", "It ended at 1730000000.123456.", True),
            ("1730000000.123456", "1730000000.123456", True),
            ("1730000000.123456", "x1730000000.123456", False),
            ("1730000000.123456", "11730000000.123456", False),
            ("1730000000.123456", "a.1730000000.123456", False),
            ("123456", "1730000000.123456", False),
            ("1730000000.123456", "(1730000000.123456)", True),
            ("1730000000.123456", "ts=1730000000.123456,", True),
            ("1730000000.123456", "1730000000.1234567", False),
            ("1730000000.123456", "1730000000.123456:x", False),
            ("1730000000.123456", "1730000000.123456: done", True),
            ("1730000000.123456", "1730000000.123456-", True),
            ("1730000000.123456", "1730000000.123456_2", False),
            ("PROJ-1", "PROJ-12 and PROJ-1.", True),
            ("PROJ-1", "PROJ-12", False),
            ("C0123ABCD", "C0123ABCDE C0123ABCD", True),
            ("", "anything", True),
            ("é1", "café1", False),
        ]
        for value, text, found in cases:
            with self.subTest(value=value, text=text):
                self.assertIs(engine.word_for_word(value, text), found)

    def test_the_reason_text_is_unchanged(self):
        self.user("slack-mentions", "extends: slack-mentions\nsettings: { me: U0EXAMPLE1 }\n")
        raw = self.tmp / "raw.txt"
        raw.write_text("Message_ts: 1730000000.123456 Channel: C0123ABCD\n")
        part = {"id": "1730000000", "at": "1730000000.123456", "text": "x", "place_id": "C0123ABCD",
                "category": "question"}
        short_channel = {"id": "1730000000.123456", "at": "1730000000.123456", "text": "x", "place_id": "C0123",
                         "category": "question"}
        result = self.engine().read_saved("slack-mentions", raw, records=[part, short_channel], dry_run=True)
        self.assertEqual(result["dropped"], [
            {"item": 0, "reason": "form.id '1730000000' is not in the raw output"},
            {"item": 1, "reason": "form.place_id 'C0123' is not in the raw output"}])


class JiraWritesTest(BuiltinCase):
    def test_apply_writes_on_the_builtin_file_is_refused_in_shadow_mode(self):
        self.epic()
        eng = self.engine()
        eng.read_saved("jira-epic", self.copy("jira-epic-issues.json"))
        cards = self.store().list()
        outside = tree(sources.PLUGIN_DIR)
        outside_files = sorted(p.name for p in self.tmp.iterdir())
        for event in ("on_start", "on_pr", "on_merge"):
            with self.subTest(event):
                with mock.patch("subprocess.run") as run, mock.patch("subprocess.Popen") as popen:
                    with self.assertRaises(ShadowModeError) as cm:
                        eng.apply_writes("jira-epic", event, cards[0]["id"])
                run.assert_not_called()
                popen.assert_not_called()
                self.assertIn(f"shadow mode: refusing to apply jira-epic's writes.{event}", str(cm.exception))
        self.assertEqual(tree(sources.PLUGIN_DIR), outside)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), outside_files)
        self.assertEqual(self.store().list(), cards)

    def test_an_unknown_write_action_is_refused(self):
        p = self.user("jira-epic", "extends: jira-epic\nwrites:\n  on_start: { transition: x, delete: all }\n")
        with self.assertRaises(SourceError) as cm:
            Loader(self.data).load("jira-epic")
        self.assertIn(f"{p} (merged over", str(cm.exception))
        self.assertIn("line 3: writes.on_start: unknown action 'delete' (actions: transition, assign, remote_link)",
                      str(cm.exception))

    def test_an_action_listed_in_never_is_refused_with_its_line(self):
        p = self.user("jira-epic", "extends: jira-epic\nwrites:\n  on_pr:\n    comment: \"PR is up: {pr}\"\n")
        with self.assertRaises(SourceError) as cm:
            Loader(self.data).load("jira-epic")
        self.assertIn(f"{p} (merged over", str(cm.exception))
        self.assertIn("line 4: writes.on_pr.comment: 'comment' is listed in never, so no write may do it",
                      str(cm.exception))

    def test_a_write_can_be_turned_off_and_values_must_be_text(self):
        self.user("jira-epic", "extends: jira-epic\nwrites: { on_pr: null }\n")
        self.assertIsNone(Loader(self.data).load("jira-epic").data["writes"]["on_pr"])
        self.user("jira-epic", "extends: jira-epic\nwrites: { on_start: { assign: null } }\n")
        on_start = Loader(self.data).load("jira-epic").data["writes"]["on_start"]
        self.assertEqual(on_start, {"transition": "In Progress", "assign": None})  # one action off, the rest kept
        self.user("jira-epic", "extends: jira-epic\nsettings: { site: jira.example.com, epic: PROJ-1 }\n"
                               "never: [create_ticket, comment, assign]\nwrites: { on_start: { assign: null } }\n")
        on_start = Loader(self.data).load("jira-epic").data["writes"]["on_start"]
        self.assertEqual(on_start, {"transition": "In Progress", "assign": None})  # a null is no write, so never allows it
        self.user("jira-epic", "extends: jira-epic\nwrites: { on_pr: { transition: [a] } }\n")
        with self.assertRaisesRegex(SourceError, "writes.on_pr.transition must be a text"):
            Loader(self.data).load("jira-epic")
        self.user("jira-epic", "extends: jira-epic\nwrites: { on_pr: done }\n")
        with self.assertRaisesRegex(SourceError, "writes.on_pr must map an action"):
            Loader(self.data).load("jira-epic")

    def test_a_status_reread_changes_status_but_never_the_lane(self):
        self.epic()
        self.engine().read_saved("jira-epic", self.copy("jira-epic-issues.json"))
        [card] = [c for c in self.store().list() if c["key"] == "PROJ-124"]
        self.store().update(card["id"], {"lane": "running"}, by="test")  # the user started it
        reply = json.loads((SCAN / "jira-epic-issues.json").read_text())
        reply["issues"][1]["fields"]["status"]["name"] = "In Review"
        moved = self.tmp / "moved.json"
        moved.write_text(json.dumps(reply))
        self.clock.tick()
        result = self.engine().read_saved("jira-epic", moved)
        self.assertEqual({i["key"]: i["action"] for i in result["items"]},
                         {"PROJ-123": "unchanged", "PROJ-124": "update", "PROJ-125": "unchanged"})
        after = self.store().get(card["id"])
        self.assertEqual((after["status"], after["lane"]), ("in_review", "running"))


if __name__ == "__main__":
    unittest.main()
