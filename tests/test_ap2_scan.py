"""Scan sessions (design 5.3, 8.0): the plan and brief, the save-raw hook and the take, from recorded hook payloads.
No session is started and no tool is called: every reply here is a fixture."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT
from tests.test_ap2_builtin_sources import Clock, tree
from tests.test_ap2_cli import temp_inside_the_accounts_claude
from assistant import datadir, scan
from assistant.config import Config
from assistant.scan import ScanError
from assistant.store import Store

AP2 = ROOT / "bin" / "ap2"
SCAN = ROOT / "tests" / "fixtures" / "ap2" / "scan"
GOLDEN = SCAN / "plan-golden.json"
SETTINGS = {
    "slack-mentions": "settings: { me: U0EXAMPLE1 }\nread:\n  skip_channels: [\"#random\"]\n",
    "slack-alerts": "settings: { alert_channel: C0444ALRT }\n",
    "jira-epic": "settings: { site: jira.example.com, epic: PROJ-1 }\n",
    "jira-ticket": "settings: { site: jira.example.com, ticket: PROJ-7 }\n",
}


def payload(name: str) -> bytes:
    return (SCAN / name).read_bytes()


class ScanCase(unittest.TestCase):
    cfg = Config()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-scan-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = datadir.ensure(self.tmp / "data")
        self.clock = Clock()

    def user(self, name: str, text: str) -> Path:
        p = self.data / "sources" / f"{name}.yaml"
        p.write_text(textwrap.dedent(text), encoding="utf-8")
        return p

    def settings(self, *names):
        for name in names or SETTINGS:
            self.user(name, f"extends: {name}\n" + SETTINGS[name])

    def plan(self, *names) -> dict:
        return scan.plan(self.data, names, cfg=self.cfg, clock=self.clock)

    def take(self, plan, dry_run=False) -> dict:
        return scan.take(self.data, plan["plan"], dry_run=dry_run, cfg=self.cfg, clock=self.clock)

    def store(self):
        return Store(self.data, cfg=self.cfg, clock=self.clock)


def normalized(out: dict) -> dict:
    """The plan with its scan folder (a fresh stamp and random hex) written as <scan>."""
    folder = out["folder"]
    return json.loads(json.dumps(out).replace(folder, "<scan>"))


class PlanTest(ScanCase):
    def test_golden_plan_and_brief_for_the_builtins_with_settings_filled_in(self):
        self.settings()
        out = self.plan()
        folder = Path(out["folder"])
        self.assertEqual(folder.parent, self.data / "sources" / ".scan")
        self.assertRegex(folder.name, r"\A20261110T143005Z-[0-9a-f]{6}\Z")
        self.assertEqual(out["plan"], str(folder / "plan.json"))
        on_disk = json.loads((folder / "plan.json").read_text())
        self.assertEqual({"plan": out["plan"], **on_disk}, out)
        got = normalized(out)
        if os.environ.get("AP2_WRITE_GOLDEN"):  # regenerate: AP2_WRITE_GOLDEN=1 python3 -m unittest ...
            GOLDEN.write_text(json.dumps(got, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
        self.assertEqual(got, json.loads(GOLDEN.read_text()))
        self.assertEqual(out["errors"], [])
        self.assertEqual([c["source"] for c in out["calls"]],
                         ["jira-epic", "jira-ticket", "slack-alerts", "slack-mentions"])

    def test_the_brief_says_what_the_session_may_do(self):
        self.settings()
        brief = self.plan()["brief"]
        for line in ("Call only the tools listed below, with exactly the arguments given.",
                     "Do not copy tool replies into files.",
                     "write that file as a JSON list",
                     "Copy id, thread_id, at and place_id exactly as the tool returned them",
                     "Take category from the call's categories.",
                     "Follow a call's instructions when it has some.",
                     "Post, send, create, edit or comment nowhere. Write no file other than the records files."):
            self.assertIn(line, brief.replace("\n   ", " "))

    def test_no_writes_or_never_text_reaches_a_plan(self):
        self.settings()
        text = json.dumps(self.plan())
        for word in ("on_start", "on_pr", "on_merge", "transition", "remote_link", "create_ticket", '"never"',
                     "skip_channels", "#random", "status_map", "In Review"):
            self.assertNotIn(word, text)

    def test_instructions_reach_the_call_and_the_brief(self):
        self.user("standup", """\
            source: standup
            brings: messages
            read: { via: skill, skill: team-standup, day: today }
            fill: ai
            form: { id: required, at: required, text: required }
            link: none
            sort: { all: question }
            instructions: Keep only the items that name you.
            """)
        out = self.plan("standup")
        [call] = out["calls"]
        self.assertEqual((call["skill"], call["args"], call["allowed_tools"], call["instructions"]),
                         ("team-standup", {"day": "today"}, ["Skill"], "Keep only the items that name you."))
        self.assertIn("1. standup (skill): run the skill team-standup.", out["brief"])
        self.assertIn("   Instructions: Keep only the items that name you.", out["brief"])

    def test_each_error_is_listed_and_the_other_calls_are_still_planned(self):
        self.settings("slack-mentions")
        self.user("vendor", """\
            source: vendor
            brings: messages
            read: { via: mcp, server: vendor, tools: [list_incidents, get_incident] }
            fill: paths
            form: { id: .id, at: .at, text: .text }
            link: none
            sort: { all: alert }
            """)
        self.user("skill-paths", """\
            source: skill-paths
            brings: messages
            read: { via: skill, skill: team-standup }
            fill: paths
            form: { id: .id, at: .at, text: .text }
            link: none
            sort: { all: question }
            """)
        out = self.plan("slack-mentions", "github-my-prs", "jira-epic", "vendor", "skill-paths", "no-such")
        self.assertEqual([c["source"] for c in out["calls"]], ["slack-mentions"])
        errors = {e["source"]: e["error"] for e in out["errors"]}
        self.assertEqual(sorted(errors), ["github-my-prs", "jira-epic", "no-such", "skill-paths", "vendor"])
        self.assertEqual(errors["github-my-prs"], "github-my-prs: read.via is command; the gate runs it with ap2 "
                                                  "source read github-my-prs, not a scan session")
        self.assertEqual(errors["jira-epic"], "jira-epic: settings.site is not set; add it in "
                                              "<data>/sources/jira-epic.yaml (extends: jira-epic)")
        self.assertEqual(errors["vendor"], "vendor: fill: paths reads one tool's reply; give exactly one read.tool "
                                           "(and no read.tools)")
        self.assertEqual(errors["skill-paths"], "skill-paths: via: skill gives text, not one tool's JSON reply; use "
                                                "fill: ai")
        self.assertIn("no source named 'no-such'", errors["no-such"])
        self.assertEqual(json.loads(Path(out["plan"]).read_text())["errors"], out["errors"])

    def test_no_name_plans_every_scan_source_and_skips_command_sources(self):
        self.settings("slack-mentions")
        out = self.plan()
        self.assertEqual([c["source"] for c in out["calls"]], ["slack-mentions"])
        self.assertEqual([e["source"] for e in out["errors"]], ["jira-epic", "jira-ticket", "slack-alerts"])

    def test_plan_starts_no_process(self):
        self.settings()
        with mock.patch("subprocess.Popen") as popen, mock.patch("subprocess.run") as run, \
                mock.patch("os.system") as system, mock.patch("os.posix_spawn", create=True) as spawn:
            self.plan()
        for m in (popen, run, system, spawn):
            m.assert_not_called()

    def test_with_nothing_to_call_the_brief_says_so(self):
        out = self.plan("github-my-prs")
        self.assertEqual(out["calls"], [])
        self.assertIn("There is nothing to call. End now.", out["brief"])


class SaveRawTest(ScanCase):
    def setUp(self):
        super().setUp()
        self.settings("slack-mentions", "jira-epic")
        self.p = self.plan("slack-mentions", "jira-epic")
        self.log = Path(self.p["folder"]) / "calls.jsonl"

    def lines(self):
        return [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_text_and_block_forms(self):
        self.assertEqual(scan.save_raw(self.p["plan"], payload("hook-slack-search.json")),
                         {"kept": True, "tool_name": "mcp__claude_ai_Slack__slack_search_public"})
        scan.save_raw(self.p["plan"], payload("hook-slack-thread.json"))
        blocks, text = self.lines()
        self.assertEqual(set(blocks), {"tool_name", "tool_input", "text"})
        self.assertEqual(blocks["text"], (SCAN / "slack-search.txt").read_text())  # one block: its text as is
        self.assertEqual(blocks["tool_input"], {"query": "<@U0EXAMPLE1>"})
        self.assertEqual(text["text"], json.loads(payload("hook-slack-thread.json"))["tool_response"])

    def test_blocks_are_joined_by_a_newline_and_anything_else_is_its_json(self):
        self.assertEqual(scan.response_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]), "a\nb")
        self.assertEqual(scan.response_text({"b": 1, "a": [2]}), '{"a": [2], "b": 1}')
        self.assertEqual(scan.response_text([{"type": "image", "data": "x"}]),
                         '[{"data": "x", "type": "image"}]')
        self.assertEqual(scan.response_text([]), "[]")
        scan.save_raw(self.p["plan"], payload("hook-jira-object.json").replace(b"get_issue", b"search_issues"))
        [line] = self.lines()
        self.assertEqual(json.loads(line["text"])["key"], "PROJ-7")

    def test_a_tool_not_in_the_plan_and_the_sessions_own_write_are_not_kept(self):
        for name in ("hook-not-planned.json", "hook-write.json"):
            with self.subTest(name):
                out = scan.save_raw(self.p["plan"], payload(name))
                self.assertFalse(out["kept"])
        self.assertFalse(self.log.exists())

    def test_a_skill_plan_keeps_every_tool_but_the_sessions_own_writes(self):
        self.user("standup", "source: standup\nbrings: messages\nread: { via: skill, skill: team-standup }\n"
                             "fill: ai\nform: { id: x, at: x, text: x }\nlink: none\nsort: { all: question }\n")
        p = self.plan("standup")
        self.assertTrue(scan.save_raw(p["plan"], payload("hook-not-planned.json"))["kept"])
        for own in ("Write", "Edit", "NotebookEdit"):
            hook = json.loads(payload("hook-write.json"))
            hook["tool_name"] = own
            self.assertFalse(scan.save_raw(p["plan"], json.dumps(hook).encode())["kept"])
        self.assertEqual(len(scan.read_log(Path(p["folder"]))[0]), 1)

    def test_the_cap_marks_the_log_cut_short(self):
        size = len(payload("hook-slack-search.json"))
        cap = max(size, len(payload("hook-slack-thread.json"))) + 100
        self.assertLess(cap, 2 * size)  # one search fits, two do not
        with mock.patch("assistant.engine.MAX_OUTPUT", cap):
            self.assertTrue(scan.save_raw(self.p["plan"], payload("hook-slack-search.json"))["kept"])
            out = scan.save_raw(self.p["plan"], payload("hook-slack-search.json"))
            self.assertEqual(out, {"kept": False, "cut_short": True,
                                   "tool_name": "mcp__claude_ai_Slack__slack_search_public"})
            self.assertFalse(scan.save_raw(self.p["plan"], payload("hook-slack-thread.json"))["kept"])
        logged, cut = scan.read_log(Path(self.p["folder"]))
        self.assertEqual((len(logged), cut), (1, True))
        self.assertEqual(self.log.read_text().count("cut_short"), 1)  # marked once

    def test_a_payload_past_the_cap_marks_the_log_cut_short(self):
        with mock.patch("assistant.engine.MAX_OUTPUT", 100):
            with self.assertRaisesRegex(ScanError, "larger than 100 bytes; the scan log .* is marked cut short"):
                scan.save_raw(self.p["plan"], payload("hook-slack-search.json"))
        self.assertEqual(scan.read_log(Path(self.p["folder"])), ([], True))

    def test_bad_payloads_are_refused_and_the_log_is_unchanged(self):
        scan.save_raw(self.p["plan"], payload("hook-slack-search.json"))
        before = self.log.read_bytes()
        good = json.loads(payload("hook-slack-search.json"))
        for bad, reason in ((b"not json", "not JSON"), (b"\xff", "not JSON"), (b"[]", "must be a JSON object"),
                            (json.dumps({**good, "tool_name": 5}).encode(), "no tool_name"),
                            (json.dumps({**good, "tool_input": "x"}).encode(), "tool_input must be a JSON object"),
                            (json.dumps({k: v for k, v in good.items() if k != "tool_response"}).encode(),
                             "no tool_response")):
            with self.subTest(bad[:40]):
                with self.assertRaisesRegex(ScanError, reason):
                    scan.save_raw(self.p["plan"], bad)
        self.assertEqual(self.log.read_bytes(), before)

    def test_a_file_that_is_not_a_plan_is_refused(self):
        other = self.tmp / "plan.json"
        other.write_text(Path(self.p["plan"]).read_text())
        with self.assertRaisesRegex(ScanError, "not a scan plan"):
            scan.save_raw(other, payload("hook-slack-search.json"))
        self.assertFalse((self.tmp / "calls.jsonl").exists())
        Path(self.p["plan"]).write_text('{"version": 1, "calls": [{"source": "../x", "via": "mcp"}]}')
        with self.assertRaisesRegex(ScanError, "a call is not a planned call"):
            scan.save_raw(self.p["plan"], payload("hook-slack-search.json"))

    def test_it_writes_only_inside_the_scan_folder(self):
        before = {p: p.stat().st_mtime_ns for p in self.data.rglob("*") if p.is_file()}
        scan.save_raw(self.p["plan"], payload("hook-slack-search.json"))
        changed = sorted(str(p.relative_to(self.data)) for p in self.data.rglob("*")
                         if p.is_file() and before.get(p) != p.stat().st_mtime_ns)
        self.assertEqual(changed, [str(self.log.relative_to(self.data))])


class TakeTest(ScanCase):
    """From a recorded session: a Slack search, a thread read and a Jira search, plus the records the model
    wrote, one of them forged."""

    def setUp(self):
        super().setUp()
        self.settings("slack-mentions", "jira-epic")
        self.p = self.plan("slack-mentions", "jira-epic")
        for name in ("hook-slack-search.json", "hook-jira-other-epic.json", "hook-slack-thread.json",
                     "hook-write.json", "hook-jira-search.json"):
            scan.save_raw(self.p["plan"], payload(name))
        shutil.copy(SCAN / "slack-mentions.records.json", Path(self.p["folder"]) / "slack-mentions.records.json")

    def test_a_take_makes_the_cards_and_drops_the_forged_record(self):
        out = self.take(self.p)
        self.assertEqual(out["errors"], [])
        reads = {r["source"]: r for r in out["reads"]}
        slack = reads["slack-mentions"]
        self.assertEqual(slack["dropped"], [
            {"item": 2, "reason": "form.place 'random' is in read.skip_channels"},
            {"item": 3, "reason": "form.id '1730000999.000001' is not in the raw output"}])
        self.assertEqual([(i["item"], i["key"], i["action"]) for i in slack["items"]],
                         [(0, "C0123ABCD:1730000000.123456", "add"), (1, "C0123ABCD:1730000000.123456", "unchanged")])  # one thread: one card
        # the raw is the logged texts only, joined by a blank line, and copied under .raw/
        raw = (self.data / slack["raw"]).read_text()
        self.assertEqual(raw, (SCAN / "slack-search.txt").read_text() + "\n\n"
                         + json.loads(payload("hook-slack-thread.json"))["tool_response"])
        self.assertTrue(slack["raw"].startswith("sources/.raw/slack-mentions/"))
        jira = reads["jira-epic"]
        self.assertEqual(jira["dropped"], [])
        self.assertEqual([i["key"] for i in jira["items"]], ["PROJ-123", "PROJ-124", "PROJ-125"])
        keys = sorted(c["key"] for c in self.store().list())
        self.assertEqual(keys, ["C0123ABCD:1730000000.123456", "PROJ-123", "PROJ-124", "PROJ-125"])
        self.assertNotIn("please approve the deploy", json.dumps(self.store().list()))  # the forged one

    def test_a_second_take_is_unchanged(self):
        self.take(self.p)
        cards = self.store().list()
        self.clock.tick()
        again = self.take(self.p)
        self.assertEqual(again["errors"], [])
        self.assertEqual({i["action"] for r in again["reads"] for i in r["items"]}, {"unchanged"})
        self.assertEqual(self.store().list(), cards)

    def test_dry_run_writes_nothing(self):
        before = tree(self.data)
        out = self.take(self.p, dry_run=True)
        self.assertEqual(out["errors"], [])
        self.assertEqual([i["action"] for r in out["reads"] for i in r["items"]], ["add", "unchanged", "add", "add", "add"])
        self.assertEqual(tree(self.data), before)

    def test_errors_are_listed_and_the_other_sources_still_read(self):
        (Path(self.p["folder"]) / "slack-mentions.records.json").unlink()
        self.settings("slack-alerts")
        p = self.plan("slack-mentions", "jira-epic", "slack-alerts")
        for name in ("hook-slack-search.json", "hook-jira-search.json"):
            scan.save_raw(p["plan"], payload(name))
        out = self.take(p)
        errors = {e["source"]: e["error"] for e in out["errors"]}
        self.assertEqual(sorted(errors), ["slack-alerts", "slack-mentions"])
        self.assertRegex(errors["slack-mentions"], "^slack-mentions: the session wrote no records file ")
        self.assertEqual(errors["slack-alerts"], "slack-alerts: no logged call matches slack_read_channel with the "
                                                 "planned arguments")
        self.assertEqual([r["source"] for r in out["reads"]], ["jira-epic"])
        # a bad records file
        (Path(p["folder"]) / "slack-mentions.records.json").write_text('{"id": 1}')
        out = self.take(p)
        self.assertIn("slack-mentions: the records file", {e["source"]: e["error"] for e in out["errors"]}
                      ["slack-mentions"])
        (Path(p["folder"]) / "slack-mentions.records.json").write_text("[{")
        out = self.take(p)
        self.assertIn("cannot read the records", {e["source"]: e["error"] for e in out["errors"]}["slack-mentions"])

    def test_a_paths_call_needs_its_planned_arguments(self):
        p = self.plan("jira-epic")
        scan.save_raw(p["plan"], payload("hook-jira-other-epic.json"))
        out = self.take(p)
        self.assertEqual(out["errors"], [{"source": "jira-epic", "error": "jira-epic: no logged call matches "
                                                                           "search_issues with the planned arguments"}])

    def test_a_cut_short_log(self):
        with open(Path(self.p["folder"]) / "calls.jsonl", "a") as f:
            f.write('{"cut_short": true}\n')
        out = self.take(self.p)
        self.assertEqual([e["source"] for e in out["errors"]], ["slack-mentions"])
        self.assertIn("was cut short", out["errors"][0]["error"])
        self.assertEqual([r["source"] for r in out["reads"]], ["jira-epic"])  # its call is whole: still read

    def test_a_plan_from_another_data_folder_is_refused(self):
        other = datadir.ensure(self.tmp / "other")
        with self.assertRaisesRegex(ScanError, "the plan is not in this data folder"):
            scan.take(other, self.p["plan"], cfg=self.cfg)


class ScanCommandTest(ScanCase):
    def setUp(self):
        if temp_inside_the_accounts_claude():
            self.skipTest("the temp folder is inside the account's ~/.claude, so ap2 refuses every temp data folder")
        super().setUp()
        self.settings("slack-mentions", "jira-epic")

    def run_ap2(self, *args, stdin=b""):
        env = {k: v for k, v in os.environ.items() if k not in ("AP2_DATA", "LC_ALL")}
        env.update(HOME=str(self.tmp), AP2_DATA=str(self.data), PYTHONIOENCODING="utf-8", PYTHON_COLORS="0")
        r = subprocess.run([sys.executable, str(AP2), *args], input=stdin, capture_output=True, env=env, timeout=60)
        return r.returncode, r.stdout.decode("utf-8"), r.stderr.decode("utf-8")

    def test_plan_save_raw_and_take_from_the_command_line(self):
        code, out, err = self.run_ap2("scan", "plan", "slack-mentions", "jira-epic")
        self.assertEqual((code, err), (0, ""))
        p = json.loads(out)
        self.assertEqual([c["source"] for c in p["calls"]], ["slack-mentions", "jira-epic"])
        for name in ("hook-slack-search.json", "hook-slack-thread.json", "hook-jira-search.json"):
            code, out, err = self.run_ap2("scan", "save-raw", "--plan", p["plan"], stdin=payload(name))
            self.assertEqual((code, err), (0, ""))
            self.assertTrue(json.loads(out)["kept"])
        shutil.copy(SCAN / "slack-mentions.records.json", p["records"] if "records" in p else
                    Path(p["calls"][0]["records"]))
        before = tree(self.data)
        code, out, err = self.run_ap2("scan", "take", p["plan"], "--dry-run")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(tree(self.data), before)
        code, out, err = self.run_ap2("scan", "take", p["plan"])
        self.assertEqual((code, err), (0, ""))
        took = json.loads(out)
        self.assertEqual(took["errors"], [])
        self.assertEqual(len(self.store().list()), 4)

    def test_a_bad_payload_is_one_line_and_exit_1_with_the_log_unchanged(self):
        p = self.plan("slack-mentions")
        code, out, err = self.run_ap2("scan", "save-raw", "--plan", p["plan"], stdin=b"{not json")
        self.assertEqual((code, out), (1, ""))
        self.assertRegex(err, r"\Aap2: the hook payload is not JSON: [^\n]*\n\Z")
        self.assertFalse((Path(p["folder"]) / "calls.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
