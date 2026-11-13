"""Scan sessions (design 5.3, 8.0): the plan and brief, the save-raw hook and the take, from recorded hook payloads.
No session is started and no tool is called: every reply here is a fixture."""
from __future__ import annotations

import contextlib
import io
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


def records(plan: dict, name: str = "slack-mentions") -> Path:
    return scan.records_path(Path(plan["folder"]), name)


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

    def test_records_files_go_in_the_records_folder_and_the_brief_names_no_plan(self):
        self.settings("slack-mentions")
        out = self.plan("slack-mentions")
        folder = Path(out["folder"])
        self.assertTrue((folder / "records").is_dir())  # made by code, the only folder the session writes in
        self.assertEqual(out["calls"][0]["records"], str(folder / "records" / "slack-mentions.records.json"))
        self.assertNotIn("plan.json", out["brief"])
        self.assertNotIn("Plan:", out["brief"])

    def test_the_brief_says_what_the_session_may_do(self):
        self.settings()
        brief = self.plan()["brief"]
        for line in ("Call only the tools listed below, with exactly the arguments given.",
                     "Do not copy tool replies into files.",
                     "write that file as a JSON list",
                     "Copy id, thread_id, at and place_id (or place, when there is no place_id) exactly as the "
                     "tool returned them",
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

    def test_the_engines_own_read_keys_are_not_call_arguments(self):
        self.user("vendor", """\
            source: vendor
            brings: messages
            read: { via: mcp, server: vendor, tool: list_incidents, timeout: 30, run: x, status: open }
            fill: paths
            form: { id: .id, at: .at, text: .text }
            link: none
            sort: { all: alert }
            """)
        out = self.plan("vendor")
        self.assertEqual(out["errors"], [])
        [call] = out["calls"]
        self.assertEqual(call["args"], {"status": "open"})

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
        self.user("both-tools", """\
            source: both-tools
            brings: messages
            read: { via: mcp, server: vendor, tool: list_incidents, tools: [get_incident] }
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
        out = self.plan("slack-mentions", "github-my-prs", "jira-epic", "vendor", "both-tools", "skill-paths",
                        "no-such")
        self.assertEqual([c["source"] for c in out["calls"]], ["slack-mentions"])
        errors = {e["source"]: e["error"] for e in out["errors"]}
        self.assertEqual(sorted(errors), ["both-tools", "github-my-prs", "jira-epic", "no-such", "skill-paths",
                                          "vendor"])
        self.assertEqual(errors["github-my-prs"], "github-my-prs: read.via is command; the gate runs it with ap2 "
                                                  "source read github-my-prs, not a scan session")
        self.assertEqual(errors["jira-epic"], "jira-epic: settings.site is not set; add it in "
                                              "<data>/sources/jira-epic.yaml (extends: jira-epic)")
        self.assertEqual(errors["vendor"], "vendor: fill: paths reads one tool's reply; give exactly one read.tool "
                                           "(and no read.tools)")
        self.assertEqual(errors["both-tools"], "both-tools: fill: paths reads one tool's reply; give exactly one "
                                               "read.tool (and no read.tools)")
        self.assertEqual(errors["skill-paths"], "skill-paths: via: skill gives text, not one tool's JSON reply; use "
                                                "fill: ai")
        self.assertIn("no source named 'no-such'", errors["no-such"])
        self.assertEqual(json.loads(Path(out["plan"]).read_text())["errors"], out["errors"])

    def test_a_server_or_tool_that_cannot_be_in_an_mcp_tool_name_is_an_error(self):
        self.settings("slack-mentions")
        self.user("jira-mine", "extends: jira-ticket\n" + SETTINGS["jira-ticket"]
                  + 'read: { server: "claude.ai Atlassian" }\n')
        self.user("any-tool", 'source: any-tool\nbrings: messages\nread: { via: mcp, server: vendor, tool: "*" }\n'
                              "fill: ai\nform: { id: x, at: x, text: x }\nlink: none\nsort: { all: question }\n")
        self.user("bad-list", "source: bad-list\nbrings: messages\n"
                              'read: { via: connector, tools: [slack_read_thread, "slack read"] }\n'
                              "fill: ai\nform: { id: x, at: x, text: x }\nlink: none\nsort: { all: question }\n")
        self.user("dotted", "extends: jira-ticket\n" + SETTINGS["jira-ticket"]
                  + "read: { server: claude.ai_Atlassian }\n")
        self.user("dashed", "source: dashed\nbrings: messages\n"
                            "read: { via: mcp, server: my-vendor, tool: get-incident }\n"
                            "fill: ai\nform: { id: x, at: x, text: x }\nlink: none\nsort: { all: question }\n")
        out = self.plan("slack-mentions", "jira-mine", "any-tool", "bad-list", "dotted", "dashed")
        self.assertEqual([c["source"] for c in out["calls"]], ["slack-mentions", "dashed"])
        dashed = next(c for c in out["calls"] if c["source"] == "dashed")
        self.assertEqual(dashed["allowed_tools"], ["mcp__my-vendor__get-incident"])
        errors = {e["source"]: e["error"] for e in out["errors"]}
        self.assertEqual(errors, {
            "jira-mine": "jira-mine: read.server must be the server's name as it appears in its tool names "
                         "(mcp__<server>__<tool>), such as claude_ai_Atlassian, not 'claude.ai Atlassian'",
            "any-tool": "any-tool: read.tool must be the tool's name as it appears in its tool names "
                        "(mcp__<server>__<tool>), such as get_issue, not '*'",
            "bad-list": "bad-list: a read.tools entry must be the tool's name as it appears in its tool names "
                        "(mcp__<server>__<tool>), such as get_issue, not 'slack read'",
            "dotted": "dotted: read.server must be the server's name as it appears in its tool names "
                      "(mcp__<server>__<tool>), such as claude_ai_Atlassian, not 'claude.ai_Atlassian'"})

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

    def test_a_lookalike_tool_name_is_not_kept(self):
        for name in ("mcp__claude_ai_Slack__slack_search_public_v2", "mcp__x__myslack_read_thread"):
            with self.subTest(name):
                hook = dict(json.loads(payload("hook-slack-search.json")), tool_name=name)
                self.assertFalse(scan.save_raw(self.p["plan"], json.dumps(hook).encode())["kept"])
        self.assertFalse(self.log.exists())

    def test_a_source_that_names_its_server_keeps_only_that_servers_tool(self):
        self.settings("jira-ticket")
        p = self.plan("jira-ticket")
        for name in ("mcp__other__get_issue", "mcp__Atlassian__get_issue",
                     "mcp__my_atlassian__get_issue"):  # another server's get_issue
            with self.subTest(name):
                other = dict(json.loads(payload("hook-jira-object.json")), tool_name=name)
                self.assertFalse(scan.save_raw(p["plan"], json.dumps(other).encode())["kept"])
        self.assertTrue(scan.save_raw(p["plan"], payload("hook-jira-object.json"))["kept"])
        self.assertEqual([c["tool_name"] for c in scan.read_log(Path(p["folder"]))[0]], ["mcp__atlassian__get_issue"])

    def test_a_tools_list_that_names_its_server_keeps_only_that_servers_tools(self):
        self.user("vendor", "source: vendor\nbrings: messages\n"
                            "read: { via: mcp, server: vendor, tools: [list_incidents, get_incident] }\n"
                            "fill: ai\nform: { id: x, at: x, text: x }\nlink: none\nsort: { all: question }\n")
        p = self.plan("vendor")
        for name, kept in (("mcp__other__get_incident", False), ("mcp__Vendor__get_incident", False),
                           ("mcp__other_vendor__get_incident", False),
                           ("mcp__vendor__get_incident", True)):
            with self.subTest(name):
                hook = {"tool_name": name, "tool_input": {}, "tool_response": "incident 1"}
                self.assertEqual(scan.save_raw(p["plan"], json.dumps(hook).encode())["kept"], kept)
        self.assertEqual([c["tool_name"] for c in scan.read_log(Path(p["folder"]))[0]], ["mcp__vendor__get_incident"])

    def test_a_connector_source_keeps_its_tool_on_any_server(self):
        for name in ("mcp__claude_ai_Slack__slack_search_public", "mcp__x__slack_search_public"):
            with self.subTest(name):
                hook = dict(json.loads(payload("hook-slack-search.json")), tool_name=name)
                self.assertTrue(scan.save_raw(self.p["plan"], json.dumps(hook).encode())["kept"])
        self.assertEqual(len(self.lines()), 2)

    def test_a_symlinked_log_is_refused_and_its_target_is_unchanged(self):
        target = self.tmp / "elsewhere.txt"
        target.write_text("keep me\n")
        self.log.symlink_to(target)
        with self.assertRaisesRegex(ScanError, r"\Athe scan log .*calls\.jsonl is a symbolic link; the hook writes "
                                               r"only in the scan folder\Z"):
            scan.save_raw(self.p["plan"], payload("hook-slack-search.json"))
        self.assertEqual(target.read_text(), "keep me\n")
        self.assertTrue(self.log.is_symlink())

    def test_a_skill_plan_keeps_every_tool_but_the_sessions_own_writes(self):
        self.user("standup", "source: standup\nbrings: messages\nread: { via: skill, skill: team-standup }\n"
                             "fill: ai\nform: { id: x, at: x, text: x }\nlink: none\nsort: { all: question }\n")
        p = self.plan("standup")
        self.assertTrue(scan.save_raw(p["plan"], payload("hook-not-planned.json"))["kept"])  # an ordinary tool
        for own in ("Write", "Edit", "MultiEdit", "NotebookEdit", "TodoWrite"):
            hook = json.loads(payload("hook-write.json"))
            hook["tool_name"] = own
            self.assertFalse(scan.save_raw(p["plan"], json.dumps(hook).encode())["kept"])
        self.assertEqual(len(scan.read_log(Path(p["folder"]))[0]), 1)

    def test_a_skill_plan_never_keeps_a_call_that_names_the_scan_folder(self):
        self.user("standup", "source: standup\nbrings: messages\nread: { via: skill, skill: team-standup }\n"
                             "fill: ai\nform: { id: x, at: x, text: x }\nlink: none\nsort: { all: question }\n")
        p = self.plan("standup")
        own = str(records(p, "standup"))
        reads = ({"file_path": own},  # the session reading back its own records file
                 {"command": f"cat {Path(own).relative_to(self.data)}"},
                 {"nested": [{"paths": [own]}]})
        for tool_input in reads:
            with self.subTest(tool_input):
                hook = {"tool_name": "Read", "tool_input": tool_input,
                        "tool_response": '[{"id": "1730000999.000001"}]'}
                self.assertFalse(scan.save_raw(p["plan"], json.dumps(hook).encode())["kept"])
        hook = {"tool_name": "Read", "tool_input": {"file_path": str(self.tmp / "notes.md")}, "tool_response": "x"}
        self.assertTrue(scan.save_raw(p["plan"], json.dumps(hook).encode())["kept"])  # an ordinary read still is
        self.assertEqual([c["text"] for c in scan.read_log(Path(p["folder"]))[0]], ["x"])

    STANDUP = ("source: standup\nbrings: messages\nread: { via: skill, skill: team-standup }\n"
               "fill: ai\nform: { id: x, at: x, text: x }\nlink: none\nsort: { all: question }\n")

    def read_backs(self, p: dict) -> dict:
        """Hook payloads of a skill scan reading back its own records file, each spelled so that the case-sensitive
        folder-name test of round one missed it."""
        own = str(records(p, "standup"))
        name = Path(p["folder"]).name
        forged = '[{"id": "PROJ-4242", "at": "2026-11-10T09:00:00Z", "text": "x", "category": "all"}]'
        lower = own.replace(name, name.lower())  # APFS is case-insensitive: the same file
        grep = {"mode": "content", "numFiles": 1, "content": f"{own}:{forged}", "numLines": 1}
        return {
            "read in lower case": {"tool_name": "Read", "tool_input": {"file_path": lower},
                                   "tool_response": {"type": "text", "file": {"filePath": lower, "content": forged}}},
            "grep on .scan": {"tool_name": "Grep",
                              "tool_input": {"pattern": "4242", "path": str(self.data / "sources" / ".scan"),
                                             "output_mode": "content"},
                              "tool_response": grep},
            "grep on sources": {"tool_name": "Grep",
                                "tool_input": {"pattern": "4242", "path": str(self.data / "sources"),
                                               "output_mode": "content"},
                                "tool_response": grep},
            "grep on the data folder": {"tool_name": "Grep",
                                        "tool_input": {"pattern": "4242", "path": str(self.data),
                                                       "output_mode": "content"},
                                        "tool_response": dict(grep, content=f"{lower}:{forged}")},
        }

    def test_a_skill_plan_never_keeps_a_read_back_whatever_its_input_spells(self):
        self.data = datadir.ensure(self.tmp / "Data")  # a capital in the path on any OS, so case folding is tested
        self.user("standup", self.STANDUP)
        p = self.plan("standup")
        for what, hook in self.read_backs(p).items():
            with self.subTest(what):
                self.assertFalse(scan.save_raw(p["plan"], json.dumps(hook).encode())["kept"])
        name = Path(p["folder"]).name
        for tool_input in ({"command": f"cat {name}/records/standup.records.json"},  # the folder's name alone
                           {"command": f"cat {name.lower()}/records/standup.records.json"},
                           {"command": f"ls {str(self.data).upper()}/sources/.raw"},  # the data folder, any case
                           {"path": f"{str(self.data).swapcase()}/items"},
                           {"command": f"cat {self.data}-backup/x {self.data}/items/y"}):  # the second one names it
            with self.subTest(tool_input):
                hook = {"tool_name": "Bash", "tool_input": tool_input, "tool_response": "x"}
                self.assertFalse(scan.save_raw(p["plan"], json.dumps(hook).encode())["kept"])
        for tool_input, reply in (({"command": f"rg --no-filename PROJ- {self.data}"}, "PROJ-4242"),  # at the end
                                  ({"command": "rg PROJ- sources"},  # only the reply names the scan folder
                                   f"sources/.scan/{name}/records/standup.records.json:PROJ-4242")):
            with self.subTest(tool_input):
                hook = {"tool_name": "Bash", "tool_input": tool_input, "tool_response": reply}
                self.assertFalse(scan.save_raw(p["plan"], json.dumps(hook).encode())["kept"])
        hook = {"tool_name": "Bash", "tool_input": {"command": "standup-report"},
                "tool_response": {"stdout": "PROJ-5 is done", "stderr": ""}}
        self.assertTrue(scan.save_raw(p["plan"], json.dumps(hook).encode())["kept"])  # an ordinary skill tool
        self.assertEqual(len(scan.read_log(Path(p["folder"]))[0]), 1)

    def test_a_planned_tool_whose_reply_names_scan_is_still_kept(self):
        self.user("standup", self.STANDUP)
        p = self.plan("standup", "slack-mentions")
        hook = json.loads(payload("hook-slack-search.json"))
        hook["tool_response"] = [{"type": "text", "text": f"Message_ts: 1730000000.123456\nText: see sources/.scan/ "
                                                          f"and {self.data}/items/"}]
        self.assertTrue(scan.save_raw(p["plan"], json.dumps(hook).encode())["kept"])  # the source's own text
        hook["tool_input"] = {"query": str(records(p, "standup"))}
        self.assertFalse(scan.save_raw(p["plan"], json.dumps(hook).encode())["kept"])  # but not its input
        self.assertEqual(len(scan.read_log(Path(p["folder"]))[0]), 1)

    def test_a_skill_plan_never_keeps_a_read_of_the_report_or_the_cards(self):
        self.user("standup", self.STANDUP)
        p = self.plan("standup")
        link = self.tmp / "link"
        link.symlink_to(self.data)
        linked = link / Path(p["plan"]).relative_to(self.data)  # the plan as the launch slice may spell it
        report = Path("sources") / ".raw" / "standup" / "x.report.json"
        drop = '"reason": "form.id \'PROJ-4242\' is not in the raw output"'
        reads = {"absolute": str(link / report), "resolved": str(self.data / report)}
        for what, path in reads.items():
            with self.subTest(what):
                hook = {"tool_name": "Read", "tool_input": {"file_path": path},
                        "tool_response": {"type": "text", "file": {"filePath": path, "content": drop}}}
                self.assertFalse(scan.save_raw(linked, json.dumps(hook).encode())["kept"])
        with self.subTest("home"), mock.patch.dict(os.environ, {"HOME": str(self.tmp)}):
            hook = {"tool_name": "Read", "tool_input": {"file_path": f"~/data/{report}"}, "tool_response": drop}
            self.assertFalse(scan.save_raw(p["plan"], json.dumps(hook).encode())["kept"])
        card = f'{self.data}/items/standup/PROJ-4242.json:  "text": "PROJ-4242 is done"'
        greps = {"on the cards": {"pattern": "PROJ-", "path": f"{self.data}/items", "output_mode": "content"},
                 "with no path": {"pattern": "PROJ-", "output_mode": "content"}}  # only its reply names the data
        for what, tool_input in greps.items():
            with self.subTest(what):
                hook = {"tool_name": "Grep", "tool_input": tool_input,
                        "tool_response": {"mode": "content", "content": card, "numLines": 1}}
                self.assertFalse(scan.save_raw(p["plan"], json.dumps(hook).encode())["kept"])
        self.assertEqual(scan.read_log(Path(p["folder"]))[0], [])

    def test_a_skill_plan_keeps_another_folder_and_a_word_like_deps_scanner(self):
        self.user("standup", self.STANDUP)
        p = self.plan("standup")
        kept = ({"tool_name": "Read", "tool_input": {"file_path": f"{self.data}-backup/items/x.json"},
                 "tool_response": f"{self.data}-backup/items/x.json: PROJ-5"},
                {"tool_name": "Read", "tool_input": {"file_path": f"{self.data}2/items/x.json"}, "tool_response": "x"},
                {"tool_name": "Read", "tool_input": {"file_path": f"{self.data}.bak/x.json"}, "tool_response": "x"},
                {"tool_name": "Read", "tool_input": {"file_path": f"{self.data}_old/x.json"}, "tool_response": "x"},
                {"tool_name": "Read", "tool_input": {"file_path": f"{self.data}x/x.json"}, "tool_response": "x"},
                {"tool_name": "Bash", "tool_input": {"command": "standup-report --repo acme/image.scanner"},
                 "tool_response": {"stdout": "PROJ-5 is done; bumped deps.scanner to 2.1", "stderr": ""}})
        for hook in kept:
            with self.subTest(hook["tool_input"]):
                self.assertTrue(scan.save_raw(p["plan"], json.dumps(hook).encode())["kept"])
        self.assertEqual(len(scan.read_log(Path(p["folder"]))[0]), 6)

    def test_a_planned_argument_like_deps_scanner_is_kept_and_taken(self):
        self.user("slack-mentions", 'extends: slack-mentions\nsettings: { me: U0EXAMPLE1 }\n'
                                    'read: { query: "<@{me}> in:#deps.scanner" }\n')
        p = self.plan("slack-mentions")
        [call] = p["calls"]
        self.assertEqual(call["args"], {"query": "<@U0EXAMPLE1> in:#deps.scanner"})
        hook = json.loads(payload("hook-slack-search.json"))
        hook["tool_input"] = {"query": "<@U0EXAMPLE1> in:#deps.scanner"}
        self.assertTrue(scan.save_raw(p["plan"], json.dumps(hook).encode())["kept"])
        shutil.copy(SCAN / "slack-mentions.records.json", records(p))
        out = self.take(p)
        self.assertEqual(out["errors"], [])
        self.assertTrue(out["reads"][0]["items"])

    def test_a_second_scan_reading_the_report_back_makes_no_card_from_an_invented_id(self):
        self.user("standup", self.STANDUP)
        real = {"tool_name": "Bash", "tool_input": {"command": "standup-report"},
                "tool_response": "id PROJ-5 at 2026-11-10T08:00:00Z: PROJ-5 is done"}
        forged = [{"id": "PROJ-4242", "at": "2026-11-10T09:00:00Z", "text": "PROJ-4242", "category": "all"},
                  {"id": "PROJ-5", "at": "2026-11-10T08:00:00Z", "text": "PROJ-5 is done", "category": "all"}]
        first = self.plan("standup")
        scan.save_raw(first["plan"], json.dumps(real).encode())
        records(first, "standup").write_text(json.dumps(forged))
        [read] = self.take(first)["reads"]
        self.assertEqual(read["dropped"], [{"item": 0, "reason": "form.id 'PROJ-4242' is not in the raw output"}])
        [report] = (self.data / "sources" / ".raw" / "standup").glob("*.report.json")
        self.assertIn("PROJ-4242", report.read_text())
        second = self.plan("standup")
        back = {"tool_name": "Read", "tool_input": {"file_path": str(report)},
                "tool_response": {"type": "text", "file": {"filePath": str(report), "content": report.read_text()}}}
        grep = {"tool_name": "Grep", "tool_input": {"pattern": "PROJ-", "path": str(self.data / "items"),
                                                    "output_mode": "content"},
                "tool_response": {"mode": "content", "content": f"{report}: PROJ-4242", "numLines": 1}}
        for hook in (back, grep, real):
            scan.save_raw(second["plan"], json.dumps(hook).encode())
        records(second, "standup").write_text(json.dumps(forged))
        out = self.take(second)
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["reads"][0]["dropped"],
                         [{"item": 0, "reason": "form.id 'PROJ-4242' is not in the raw output"}])
        self.assertEqual([c["key"] for c in self.store().list()], ["standup:PROJ-5"])

    def test_a_read_back_makes_no_card_from_an_invented_id(self):
        self.user("standup", self.STANDUP)
        p = self.plan("standup")
        for hook in self.read_backs(p).values():
            scan.save_raw(p["plan"], json.dumps(hook).encode())
        real = {"tool_name": "Bash", "tool_input": {"command": "standup-report"},
                "tool_response": "id PROJ-5 at 2026-11-10T08:00:00Z: PROJ-5 is done"}
        scan.save_raw(p["plan"], json.dumps(real).encode())
        records(p, "standup").write_text(json.dumps([
            {"id": "PROJ-4242", "at": "2026-11-10T09:00:00Z", "text": "x", "category": "all"},
            {"id": "PROJ-5", "at": "2026-11-10T08:00:00Z", "text": "PROJ-5 is done", "category": "all"}]))
        out = self.take(p)
        self.assertEqual(out["errors"], [])
        [read] = out["reads"]
        self.assertEqual(read["dropped"], [{"item": 0, "reason": "form.id 'PROJ-4242' is not in the raw output"}])
        self.assertEqual([c["key"] for c in self.store().list()], ["standup:PROJ-5"])
        self.assertNotIn("PROJ-4242", json.dumps(self.store().list()))

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

    def test_the_command_reads_one_byte_past_the_cap_and_marks_the_log_cut_short(self):
        from assistant import cli
        cap = 100
        stdin = io.TextIOWrapper(io.BytesIO(b"x" * (cap + 1)), encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("assistant.engine.MAX_OUTPUT", cap), mock.patch.object(sys, "stdin", stdin), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["scan", "save-raw", "--plan", self.p["plan"]])
        self.assertEqual((code, out.getvalue()), (1, ""))
        self.assertRegex(err.getvalue(), rf"\Aap2: the hook payload is larger than {cap} bytes; [^\n]*\n\Z")
        self.assertEqual(json.loads(self.log.read_text().splitlines()[-1]), {"cut_short": True})

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


class OversizedPayloadTest(ScanCase):
    """A payload past engine.MAX_OUTPUT arrives cut off: only its top-level tool_name is read, from the start."""

    CAP = 200

    def setUp(self):
        super().setUp()
        self.settings("slack-mentions", "jira-epic")
        self.p = self.plan("slack-mentions", "jira-epic")

    @staticmethod
    def big_read() -> bytes:
        hook = {"session_id": "s", "hook_event_name": "PostToolUse", "tool_name": "Read",
                "tool_input": {"file_path": "/tmp/big.log"}, "tool_response": "x" * 10_000}
        return json.dumps(hook).encode()

    def save(self, plan_file, data: bytes):
        with mock.patch("assistant.engine.MAX_OUTPUT", self.CAP):
            return scan.save_raw(plan_file, data[:self.CAP + 1])

    def test_an_unkept_tools_oversized_reply_is_skipped(self):
        out = self.save(self.p["plan"], self.big_read())
        self.assertEqual(out, {"kept": False, "tool_name": "Read", "too_large": True})
        self.assertEqual(scan.read_log(Path(self.p["folder"])), ([], False))

    def test_a_later_normal_call_still_logs(self):
        self.save(self.p["plan"], self.big_read())
        self.assertTrue(scan.save_raw(self.p["plan"], payload("hook-slack-search.json"))["kept"])
        logged, cut = scan.read_log(Path(self.p["folder"]))
        self.assertEqual((len(logged), cut), (1, False))

    def test_the_command_prints_the_skip_and_exits_0(self):
        from assistant import cli
        stdin = io.TextIOWrapper(io.BytesIO(self.big_read()), encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("assistant.engine.MAX_OUTPUT", self.CAP), mock.patch.object(sys, "stdin", stdin), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["scan", "save-raw", "--plan", self.p["plan"]])
        self.assertEqual((code, err.getvalue()), (0, ""))
        self.assertEqual(json.loads(out.getvalue()), {"kept": False, "tool_name": "Read", "too_large": True})
        self.assertEqual(scan.read_log(Path(self.p["folder"])), ([], False))

    def test_a_planned_tools_oversized_reply_marks_the_log_cut_short(self):
        hook = json.loads(payload("hook-slack-search.json"))
        hook["tool_response"] = "x" * 10_000
        with self.assertRaisesRegex(ScanError, rf"larger than {self.CAP} bytes; the scan log .* is marked cut short"):
            self.save(self.p["plan"], json.dumps(hook).encode())
        self.assertEqual(scan.read_log(Path(self.p["folder"])), ([], True))

    def test_a_skill_plan_marks_the_log_cut_short(self):
        self.user("standup", SaveRawTest.STANDUP)
        p = self.plan("standup")
        for name in ("Read", "Write"):  # a tool the skill would keep, and one it would not
            with self.subTest(name):
                with self.assertRaisesRegex(ScanError,
                                            rf"larger than {self.CAP} bytes; the scan log .* is marked cut short"):
                    self.save(p["plan"], self.big_read().replace(b'"Read"', json.dumps(name).encode()))
                self.assertEqual(scan.read_log(Path(p["folder"])), ([], True))

    def test_an_unreadable_name_marks_the_log_cut_short(self):
        tail = ', "tool_input": {}, "tool_response": "' + "x" * 10_000 + '"}'
        cases = {
            "x bytes": (b"x" * 10_000, None),
            "a JSON list": (('["tool_name", "Read", "' + "x" * 10_000 + '"]').encode(), None),
            "a number": (('{"session_id": "s", "tool_name": 5' + tail).encode(), None),
            "an empty name": (('{"session_id": "s", "tool_name": ""' + tail).encode(), None),
            "a list name": (('{"session_id": "s", "tool_name": ["Read"]' + tail).encode(), None),
            "past the look-ahead": (('{"session_id": "' + "s" * 100 + '", "tool_name": "Read"' + tail).encode(), 64),
            "only inside tool_input": (('{"tool_input": {"tool_name": "Read"}, "tool_response": "'
                                        + "x" * 10_000 + '", "tool_name": "Read"}').encode(), None),
            "a bad key": (('{"session_id" "s", "tool_name": "Read"' + tail).encode(), None),
            "no colon": (('{"session_id"; "s", "tool_name": "Read"' + tail).encode(), None),
            "no comma": (('{"session_id": "s"; "tool_name": "Read"' + tail).encode(), None),
            "not an object": (('x"tool_name": "Read"' + tail).encode(), None),
            "bad UTF-8 before the name": (b'{"session_id": "\xff", "tool_name": "Read"' + tail.encode(), None),
            "the name cut off": (('{"session_id": "s", "tool_name": "Re' + "a" * 10_000 + '"}').encode(), None),
            "cut after a key": (('{"session_id": "s", "tool_name": "Read"' + tail).encode(), len('{"session_id"')),
            "cut after a value": (('{"session_id": "s", "tool_name": "Read"' + tail).encode(),
                                  len('{"session_id": "s"')),
            "cut after a comma": (('{"session_id": "s", "tool_name": "Read"' + tail).encode(),
                                  len('{"session_id": "s", ')),
            "cut after the name's colon": (('{"session_id": "s", "tool_name": "Read"' + tail).encode(),
                                           len('{"session_id": "s", "tool_name": ')),
        }
        for name, (data, lookahead) in cases.items():
            with self.subTest(name):
                p = self.plan("slack-mentions", "jira-epic")
                patch = mock.patch.object(scan, "NAME_LOOKAHEAD", lookahead) if lookahead else \
                    contextlib.nullcontext()
                with patch, self.assertRaisesRegex(
                        ScanError, rf"\Athe hook payload is larger than {self.CAP} bytes; the scan log .* is marked "
                                   rf"cut short\Z"):
                    self.save(p["plan"], data)
                self.assertEqual(scan.read_log(Path(p["folder"])), ([], True))

    def test_a_value_nested_too_deeply_before_the_name_marks_the_log_cut_short(self):
        self.CAP = 200_000  # save() keeps CAP + 1 bytes: enough nesting to hit the recursion limit
        with self.assertRaisesRegex(
                ScanError, rf"\Athe hook payload is larger than {self.CAP} bytes; the scan log .* is marked "
                           rf"cut short\Z"):
            self.save(self.p["plan"], b'{"session_id": ' + b"[" * self.CAP)
        self.assertEqual(scan.read_log(Path(self.p["folder"])), ([], True))

    def test_a_cut_multibyte_character_at_the_look_ahead_is_not_an_error(self):
        data = ('{"session_id": "s", "tool_name": "Read", "tool_response": "' + "é" * 5_000 + '"}').encode()
        with mock.patch.object(scan, "NAME_LOOKAHEAD", 60):  # inside a two-byte character
            self.assertEqual(data[59:61], "é".encode())
            self.assertEqual(self.save(self.p["plan"], data),
                             {"kept": False, "tool_name": "Read", "too_large": True})

    def test_the_look_ahead_stops_at_tool_name_and_at_its_byte_limit(self):
        real = json.JSONDecoder.raw_decode
        seen = []

        def spy(self, s, idx=0):
            seen.append((s, idx))
            return real(self, s, idx)

        with mock.patch.object(json.JSONDecoder, "raw_decode", spy):
            self.save(self.p["plan"], self.big_read())
        text = self.big_read()[:self.CAP + 1].decode()
        offsets = [idx for s, idx in seen if text.startswith(s) and s.startswith('{"session_id"')]
        name_value = text.index('"Read"')
        self.assertEqual(offsets, [text.index('"session_id"'), text.index('"s"'), text.index('"hook_event_name"'),
                                   text.index('"PostToolUse"'), text.index('"tool_name"'), name_value])
        self.assertLess(max(offsets), text.index('"tool_input"'))  # tool_input and tool_response never decoded

        seen.clear()
        big = ('{"session_id": "' + "s" * 10_000 + '", "tool_name": "Read"}').encode()
        with mock.patch.object(json.JSONDecoder, "raw_decode", spy), mock.patch.object(scan, "NAME_LOOKAHEAD", 100), \
                self.assertRaises(ScanError):
            self.save(self.plan("slack-mentions")["plan"], big)
        mine = [s for s, idx in seen if s.startswith('{"session_id"')]
        self.assertTrue(mine)
        self.assertTrue(all(len(s) <= 100 for s in mine))  # nothing past the byte limit is handed to the decoder

    def test_a_payload_within_the_cap_is_not_looked_ahead(self):
        self.assertEqual(scan.save_raw(self.p["plan"], self.big_read()), {"kept": False, "tool_name": "Read"})
        with self.assertRaisesRegex(ScanError, "tool_input must be a JSON object"):
            scan.save_raw(self.p["plan"], b'{"tool_name": "Read", "tool_input": "x", "tool_response": ""}')
        self.assertEqual(scan.read_log(Path(self.p["folder"])), ([], False))


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
        shutil.copy(SCAN / "slack-mentions.records.json", records(self.p))

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
        records(self.p).unlink()
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
        records(p).write_text('{"id": 1}')
        out = self.take(p)
        self.assertIn("slack-mentions: the records file", {e["source"]: e["error"] for e in out["errors"]}
                      ["slack-mentions"])
        records(p).write_text("[{")
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

    def test_a_lookalike_line_in_the_log_is_not_in_the_raw(self):
        line = {"tool_name": "mcp__claude_ai_Slack__slack_search_public_v2", "tool_input": {"query": "x"},
                "text": "Message_ts: 1730000999.000001"}
        with open(Path(self.p["folder"]) / "calls.jsonl", "a") as f:
            f.write(json.dumps(line) + "\n")
        out = self.take(self.p)
        slack = {r["source"]: r for r in out["reads"]}["slack-mentions"]
        self.assertEqual((self.data / slack["raw"]).read_text(), (SCAN / "slack-search.txt").read_text() + "\n\n"
                         + json.loads(payload("hook-slack-thread.json"))["tool_response"])

    def test_an_ai_call_with_another_value_for_a_planned_argument_is_not_in_the_raw(self):
        self.settings("slack-alerts")
        p = self.plan("slack-alerts")
        planned = {"tool_name": "mcp__claude_ai_Slack__slack_read_channel", "tool_input": {"channel_id": "C0444ALRT"},
                   "tool_response": "Channel: #alerts (C0444ALRT)\nMessage_ts: 1730000500.000100\n"
                                    "Text: checkout errors high\n"}
        private = {"tool_name": "mcp__claude_ai_Slack__slack_read_channel", "tool_input": {"channel_id": "C0PRIVATE"},
                   "tool_response": "Channel: #secret (C0PRIVATE)\nMessage_ts: 1730000600.000200\n"
                                    "Text: payroll export failed\n"}
        for hook in (planned, private):
            self.assertTrue(scan.save_raw(p["plan"], json.dumps(hook).encode())["kept"])
        records(p, "slack-alerts").write_text(json.dumps([
            {"id": "1730000500.000100", "at": "1730000500.000100", "text": "checkout errors high",
             "place_id": "C0444ALRT", "category": "alert"},
            {"id": "1730000600.000200", "at": "1730000600.000200", "text": "payroll export failed",
             "place_id": "C0PRIVATE", "category": "alert"}]))
        out = self.take(p)
        self.assertEqual(out["errors"], [])
        [alerts] = out["reads"]
        self.assertEqual((self.data / alerts["raw"]).read_text(), planned["tool_response"])
        self.assertEqual(alerts["dropped"], [{"item": 1, "reason": "form.id '1730000600.000200' is not in the raw "
                                                                   "output"}])
        self.assertEqual([c["key"] for c in self.store().list()], ["C0444ALRT:1730000500.000100"])
        self.assertNotIn("C0PRIVATE", json.dumps(self.store().list()))

    def test_a_search_with_another_query_is_left_out_but_a_thread_read_still_counts(self):
        other = json.loads(payload("hook-slack-search.json"))
        other["tool_input"] = {"query": "in:#payments"}
        other["tool_response"] = ("## Channel: #payments (C0123ABCD)\nFrom: U0EXAMPLE2\n"
                                  "Message_ts: 1730000999.000001\nText: <@U0EXAMPLE1> please approve the deploy now\n")
        self.assertTrue(scan.save_raw(self.p["plan"], json.dumps(other).encode())["kept"])
        out = self.take(self.p)
        slack = {r["source"]: r for r in out["reads"]}["slack-mentions"]
        thread = json.loads(payload("hook-slack-thread.json"))
        self.assertNotIn("query", thread["tool_input"])  # the thread read has none of the planned names: still in
        self.assertEqual((self.data / slack["raw"]).read_text(), (SCAN / "slack-search.txt").read_text() + "\n\n"
                         + thread["tool_response"])
        self.assertIn({"item": 3, "reason": "form.id '1730000999.000001' is not in the raw output"}, slack["dropped"])

    def test_another_servers_tool_of_the_same_name_is_not_the_sources_raw(self):
        self.settings("jira-ticket")
        self.user("standup", SaveRawTest.STANDUP)
        p = self.plan("jira-ticket", "standup")
        real = json.loads(payload("hook-jira-object.json"))
        other = json.loads(json.dumps(real))
        other["tool_name"] = "mcp__other__get_issue"
        other["tool_response"]["fields"].update(summary="Text from another server", status={"name": "Done"})
        for hook in (other, real):  # the other server's reply first
            self.assertTrue(scan.save_raw(p["plan"], json.dumps(hook).encode())["kept"])
        self.assertEqual([c["tool_name"] for c in scan.read_log(Path(p["folder"]))[0]],
                         ["mcp__other__get_issue", "mcp__atlassian__get_issue"])  # the first kept for the skill
        out = self.take(p)
        ticket = {r["source"]: r for r in out["reads"]}["jira-ticket"]
        self.assertEqual((self.data / ticket["raw"]).read_text(), scan.response_text(real["tool_response"]))
        [item] = ticket["items"]
        self.assertEqual((item["key"], item["record"]["text"], item["record"]["status"]),
                         ("PROJ-7", "Split the retry work", "To Do"))
        self.assertNotIn("another server", json.dumps(self.store().list()))

    def test_a_paths_call_reads_the_first_matching_reply(self):
        hook = json.loads(payload("hook-jira-search.json"))
        reply = json.loads(hook["tool_response"][0]["text"])
        reply["issues"] = reply["issues"][:1]
        hook["tool_response"][0]["text"] = json.dumps(reply)
        scan.save_raw(self.p["plan"], json.dumps(hook).encode())  # a second, later search with one issue
        jira = {r["source"]: r for r in self.take(self.p)["reads"]}["jira-epic"]
        self.assertEqual([i["key"] for i in jira["items"]], ["PROJ-123", "PROJ-124", "PROJ-125"])

    def test_a_paths_call_whose_only_reply_lacks_the_planned_argument_names_has_no_raw(self):
        call = {"via": "mcp", "fill": "paths", "server": "atlassian", "tool": "get_issue", "args": {"issue": "PROJ-7"}}
        logged = [{"tool_name": "mcp__atlassian__get_issue", "tool_input": {"issueIdOrKey": "PROJ-9"}, "text": "PROJ-9"}]
        self.assertIsNone(scan.raw_for(call, logged))

    def test_an_ai_call_keeps_only_the_reply_with_every_planned_argument(self):
        call = {"via": "mcp", "fill": "ai", "server": "vendor", "tool": "list_issues",
                "args": {"team": "core", "state": "open"}}
        logged = [{"tool_name": "mcp__vendor__list_issues", "tool_input": {"team": "core", "state": "closed"},
                   "text": "closed issues"},
                  {"tool_name": "mcp__vendor__list_issues", "tool_input": {"team": "core", "state": "open"},
                   "text": "open issues"}]
        self.assertEqual(scan.raw_for(call, logged), "open issues")

    def test_a_skill_call_keeps_every_logged_text_whatever_its_input(self):
        call = {"via": "skill", "fill": "ai", "skill": "team-standup", "args": {"day": "today"}}
        logged = [{"tool_name": "Bash", "tool_input": {"day": "yesterday"}, "text": "x"}]
        self.assertEqual(scan.raw_for(call, logged), "x")

    def tamper(self, change) -> None:
        path = Path(self.p["plan"])
        p = json.loads(path.read_text())
        change(p)
        path.write_text(json.dumps(p))

    def test_a_plan_call_the_session_added_or_changed_is_refused(self):
        forged = {"source": "github-review-requests", "via": "skill", "fill": "paths", "args": {}}
        self.tamper(lambda p: p["calls"].append(forged))
        with open(Path(self.p["folder"]) / "calls.jsonl", "a") as f:
            f.write(json.dumps({"tool_name": "Bash", "tool_input": {},
                                "text": '[{"number": 1, "url": "https://evil.example.com/"}]'}) + "\n")
        out = self.take(self.p)
        self.assertEqual([e["source"] for e in out["errors"]], ["github-review-requests"])
        self.assertTrue(out["errors"][0]["error"].startswith(
            "github-review-requests: the plan's call does not match the source as it loads now ("))
        self.assertEqual(sorted(r["source"] for r in out["reads"]), ["jira-epic", "slack-mentions"])
        self.assertNotIn("evil.example.com", json.dumps(self.store().list()))

        def changed_args(p):
            jira = next(c for c in p["calls"] if c["source"] == "jira-epic")
            jira["args"]["jql"] = "parent = PROJ-9"
        self.tamper(changed_args)
        out = self.take(self.p)
        self.assertIn({"source": "jira-epic", "error": "jira-epic: the plan's call does not match the source as it "
                                                       "loads now"}, out["errors"])
        self.assertIn("slack-mentions", [r["source"] for r in out["reads"]])

    def test_a_symlinked_raw_path_is_replaced_and_its_target_is_unchanged(self):
        target = self.tmp / "dotfile"
        target.write_text("keep me\n")
        link = Path(self.p["folder"]) / "slack-mentions.raw.txt"
        link.symlink_to(target)
        out = self.take(self.p)
        self.assertEqual(out["errors"], [])
        self.assertEqual(target.read_text(), "keep me\n")
        self.assertFalse(link.is_symlink())
        self.assertTrue(link.read_text().startswith((SCAN / "slack-search.txt").read_text()))

    def test_a_symlinked_records_file_is_an_error(self):
        target = self.tmp / "records.json"
        shutil.move(records(self.p), target)
        records(self.p).symlink_to(target)
        out = self.take(self.p)
        self.assertEqual([e["source"] for e in out["errors"]], ["slack-mentions"])
        self.assertRegex(out["errors"][0]["error"], r"^slack-mentions: the records file .* is a symbolic link")
        self.assertEqual([r["source"] for r in out["reads"]], ["jira-epic"])


    def test_a_symlinked_records_folder_is_an_error(self):
        folder = Path(self.p["folder"]) / "records"
        elsewhere = self.tmp / "elsewhere"
        shutil.move(folder, elsewhere)
        folder.symlink_to(elsewhere, target_is_directory=True)
        out = self.take(self.p)
        self.assertEqual([e["source"] for e in out["errors"]], ["slack-mentions"])
        self.assertRegex(out["errors"][0]["error"], r"^slack-mentions: the records folder .* is a symbolic link")
        self.assertEqual([r["source"] for r in out["reads"]], ["jira-epic"])

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

    def test_take_exits_0_when_a_call_is_in_errors(self):
        self.settings("slack-alerts")
        p = self.plan("slack-mentions", "jira-epic", "slack-alerts")
        for name in ("hook-slack-search.json", "hook-slack-thread.json", "hook-jira-search.json"):
            scan.save_raw(p["plan"], payload(name))
        shutil.copy(SCAN / "slack-mentions.records.json", records(p))
        code, out, err = self.run_ap2("scan", "take", p["plan"])
        self.assertEqual((code, err), (0, ""))
        took = json.loads(out)
        self.assertEqual([e["source"] for e in took["errors"]], ["slack-alerts"])  # no reply was logged for it
        self.assertEqual(sorted(r["source"] for r in took["reads"]), ["jira-epic", "slack-mentions"])

    def test_a_symlinked_log_is_one_line_and_exit_1(self):
        p = self.plan("slack-mentions")
        target = self.tmp / "elsewhere.txt"
        target.write_text("keep me\n")
        (Path(p["folder"]) / "calls.jsonl").symlink_to(target)
        code, out, err = self.run_ap2("scan", "save-raw", "--plan", p["plan"], stdin=payload("hook-slack-search.json"))
        self.assertEqual((code, out), (1, ""))
        self.assertRegex(err, r"\Aap2: the scan log [^\n]* is a symbolic link; [^\n]*\n\Z")
        self.assertEqual(target.read_text(), "keep me\n")

    def test_a_bad_payload_is_one_line_and_exit_1_with_the_log_unchanged(self):
        p = self.plan("slack-mentions")
        code, out, err = self.run_ap2("scan", "save-raw", "--plan", p["plan"], stdin=b"{not json")
        self.assertEqual((code, out), (1, ""))
        self.assertRegex(err, r"\Aap2: the hook payload is not JSON: [^\n]*\n\Z")
        self.assertFalse((Path(p["folder"]) / "calls.jsonl").exists())


# -- invisible text: a read value of a scan source follows cards.blank ---------------------------------------------

class InvisibleTextTest(ScanCase):
    def test_a_skill_of_only_invisible_characters_is_missing(self):
        for value in ("\\ufeff", "\\ufeff \\x1c"):
            with self.subTest(value=value):
                self.user("standup", f'source: standup\nbrings: messages\nread: {{ via: skill, skill: "{value}" }}\n'
                                     "fill: ai\nform: { id: x, at: x, text: x }\nlink: none\nsort: { all: question }\n")
                out = self.plan("standup")
                self.assertEqual(out["calls"], [])
                self.assertEqual([e["error"] for e in out["errors"]],
                                 ["standup: via: skill needs read.skill, the skill to run"])

    def test_a_skill_with_words_next_to_an_invisible_character_is_planned(self):
        for value in ("\\ufeffT1", "a\\ufeff"):
            with self.subTest(value=value):
                self.user("standup", f'source: standup\nbrings: messages\nread: {{ via: skill, skill: "{value}" }}\n'
                                     "fill: ai\nform: { id: x, at: x, text: x }\nlink: none\nsort: { all: question }\n")
                out = self.plan("standup")
                self.assertEqual(out["errors"], [])
                self.assertEqual([c["source"] for c in out["calls"]], ["standup"])


if __name__ == "__main__":
    unittest.main()
