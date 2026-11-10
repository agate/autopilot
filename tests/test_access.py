from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from tests.helpers import TempData, write_ap
from autopilot import access, cli, config, state


def done(code=0, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


MCP_OK = "Tracker:\n  Scope: Project config\n  Status: ✔ Connected\n  Type: http\n"
MCP_AUTH = "Tracker:\n  Scope: Project config\n  Status: ! Needs authentication\n  Type: http\n"
MCP_FAIL = "Tracker:\n  Scope: Project config\n  Status: ✘ Failed to connect\n  Type: stdio\n"
AUTH_OK = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})


class Base(unittest.TestCase):
    def setUp(self):
        self.td = TempData()
        self.data = self.td.__enter__()
        self.repo = Path(tempfile.mkdtemp())
        self.notes = []
        p = mock.patch.object(access, "_notify", self.notes.append)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.td.__exit__(None, None, None)

    def ap(self, name="alpha", **cfg):
        write_ap(self.data, name, repo=str(self.repo), **cfg)
        return name

    def system_cards(self, ap="alpha", status=None):
        return [d for d in state.load(ap)["decisions"]
                if d["kind"] == "system" and (status is None or d["status"] == status)]

    def events(self, ap="alpha"):
        f = self.data / "projects" / ap / "events.jsonl"
        return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []


class ChecksTest(Base):
    def test_default_checks_are_github_and_the_page_login(self):
        self.assertEqual([c["id"] for c in access.checks(self.ap())], ["gh", "page"])

    def test_no_page_check_for_another_page_host(self):
        g = config.load_global()
        g["page_host"] = "elsewhere"
        config.save_global(g)
        self.assertEqual([c["id"] for c in access.checks(self.ap())], ["gh"])

    def test_servers_and_declared_checks(self):
        ap = self.ap(mcp_servers=["Docs"],
                     health_checks=[{"name": "cloud", "command": "true", "fix": "log in to the cloud"}])
        access.record_servers(ap, ["Tracker"])
        ids = [c["id"] for c in access.checks(ap)]
        self.assertEqual(ids, ["gh", "page", "mcp:Tracker", "mcp:Docs", "check:cloud"])

    def test_no_check_starts_a_login(self):
        ap = self.ap(mcp_servers=["Tracker"])
        for c in access.checks(ap):
            self.assertNotIn("login", c.get("argv") or [])
            self.assertNotIn("authenticate", " ".join(c.get("argv") or []).lower())

    def test_mcp_status_lines(self):
        c = next(c for c in access.checks(self.ap(mcp_servers=["Tracker"])) if c["id"] == "mcp:Tracker")
        self.assertEqual(c["argv"][1:], ["mcp", "get", "Tracker"])
        self.assertTrue(access.judge(c, done(0, MCP_OK))[0])
        ok, err, fix = access.judge(c, done(0, MCP_AUTH))
        self.assertFalse(ok)
        self.assertIn("Needs authentication", err)
        self.assertIn("/mcp", fix)
        self.assertFalse(access.judge(c, done(0, MCP_FAIL))[0])
        self.assertFalse(access.judge(c, done(0, "Tracker:\n  Status: ! Connected · tools fetch failed\n"))[0])
        ok, err, _ = access.judge(c, done(1, "", 'No MCP server named "Tracker".'))
        self.assertFalse(ok)
        self.assertIn("No MCP server named", err)

    def test_page_login(self):
        c = next(c for c in access.checks(self.ap()) if c["id"] == "page")
        self.assertTrue(access.judge(c, done(0, AUTH_OK))[0])
        self.assertFalse(access.judge(c, done(0, json.dumps({"loggedIn": False})))[0])
        self.assertFalse(access.judge(c, done(0, json.dumps({"loggedIn": True, "authMethod": "api_key"})))[0])
        self.assertFalse(access.judge(c, done(0, "not json"))[0])

    def test_declared_check_runs_its_command_in_the_repo(self):
        ap = self.ap(health_checks=[{"name": "cloud", "command": "exit 3"}])
        c = next(c for c in access.checks(ap) if c["id"] == "check:cloud")
        ok, err, fix = access.run_check(c)
        self.assertFalse(ok)
        self.assertIn("exit 3", err)
        self.assertEqual(fix, "exit 3")
        self.assertEqual(c["cwd"], str(self.repo))

    def test_timeout_is_a_failure(self):
        c = access.checks(self.ap())[0]
        with mock.patch.object(access.subprocess, "run", side_effect=subprocess.TimeoutExpired("gh", 1)):
            ok, err, _ = access.run_check(c)
        self.assertFalse(ok)
        self.assertIn("timed out", err)

    def test_bad_declared_checks_are_skipped(self):
        ap = self.ap(health_checks=[{"name": "no command"}, {"command": "true"}, "x"])
        self.assertEqual([c["id"] for c in access.checks(ap)], ["gh", "page"])


class CardsTest(Base):
    def test_one_check_per_pass_oldest_first(self):
        self.ap()
        ran = []
        with mock.patch.object(access, "run_check", lambda c: ran.append(c["id"]) or (True, "", "")):
            for _ in range(3):
                access.run_due(["alpha"])
        self.assertEqual(ran, ["gh", "page", "gh"])

    def test_broken_access_opens_one_card(self):
        self.ap()
        for _ in range(2):
            access.record("alpha", "gh", False, "not logged in", "gh auth login")
        cards = self.system_cards(status="open")
        self.assertEqual(len(cards), 1)
        a = cards[0]["access"]
        self.assertEqual((a["check"], a["fix"]), ("gh", "gh auth login"))
        self.assertEqual(cards[0]["title"], {"key": "access_broken", "args": {"name": "GitHub (gh)"}})
        self.assertEqual(cards[0]["options"], [])
        self.assertEqual(a["blocks"], {"key": "blocks_gh"})
        self.assertEqual([e["type"] for e in self.events()].count("access_broken"), 1)
        self.assertEqual(len(self.notes), 1)
        self.assertEqual(state.validate(state.load("alpha"), {"en"}), [])

    def test_fail_takes_text_in_two_languages(self):
        self.ap()
        info = {"error": {"en": "fetch fails", "zh": "拉取失败"}, "fix": {"en": "run it", "zh": "运行它"}}
        with redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["access", "fail", "alpha", "gh", json.dumps(info)]), 0)
        card = self.system_cards(status="open")[0]
        self.assertEqual(card["access"]["error"], info["error"])
        self.assertEqual(card["access"]["fix"], info["fix"])
        self.assertEqual(card["body"], {"key": "access_body", "args": {"error": info["error"]}})

    def test_fail_takes_blocks_as_a_label_or_languages(self):
        self.ap()
        for blocks in ({"key": "blocks_gh"}, {"en": "pushing"}):
            with redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["access", "fail", "alpha", "gh",
                                           json.dumps({"error": "x", "blocks": blocks})]), 0, blocks)

    def test_fail_refuses_text_that_is_neither_a_string_nor_languages(self):
        self.ap()
        bads = [{"error": {"zh": "只有中文"}}, {"error": ["a"]}, {"fix": {"en": 3}}, {"error": {"en": ""}},
                {"blocks": ["y"]}, {"blocks": {"en": {"en": "y"}}}, {"blocks": {"key": "blocks_check"}}, {"error": {"key": "blocks_gh"}}, []]
        for raw in [json.dumps(b) for b in bads] + ["not json"]:
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertEqual(cli.main(["access", "fail", "alpha", "gh", raw]), 2, raw)
            self.assertIn("ap access fail", err.getvalue())
        self.assertEqual(self.system_cards(), [])

    def test_since_is_the_first_failure(self):
        self.ap()
        access.record("alpha", "gh", False, "a", "gh auth login")
        first = self.system_cards()[0]["access"]["since"]
        access.record("alpha", "gh", False, "b", "gh auth login")
        card = self.system_cards()[0]
        self.assertEqual(card["access"]["since"], first)
        self.assertEqual(card["body"], {"key": "access_body", "args": {"error": "b"}})

    def test_working_again_resolves_the_card(self):
        self.ap()
        access.record("alpha", "gh", False, "not logged in", "gh auth login")
        access.record("alpha", "gh", True)
        self.assertEqual(self.system_cards(status="open"), [])
        card = self.system_cards()[0]
        self.assertEqual((card["status"], card["answer"]), ("resolved", "restored"))
        self.assertIn("answered_at", card)
        ev = [e for e in self.events() if e["type"] == "access_restored"]
        self.assertEqual(ev[0]["data"]["check"], "gh")
        self.assertIn("since", ev[0]["data"])
        self.assertEqual(state.validate(state.load("alpha"), {"en"}), [])

    def test_ok_without_a_card_changes_nothing(self):
        self.ap()
        access.record("alpha", "gh", True)
        self.assertEqual(self.system_cards(), [])
        self.assertEqual([e["type"] for e in self.events()], [])

    def test_a_declared_check_says_what_it_blocks(self):
        ap = self.ap(health_checks=[{"name": "cloud", "command": "false", "fix": "log in", "blocks": "deploy checks"}])
        access.record(ap, "check:cloud", False, "expired", "log in")
        a = self.system_cards()[0]["access"]
        self.assertEqual(a["blocks"], {"en": "deploy checks"})

    def test_cli_fail_and_ok(self):
        self.ap()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["access", "fail", "alpha", "artifact",
                                       json.dumps({"error": "Artifact tools missing", "fix": "/login"})]), 0)
        self.assertEqual(len(self.system_cards(status="open")), 1)
        self.assertIn("access_failure", [e["type"] for e in self.events()])
        self.assertEqual(cli.main(["access", "ok", "alpha", "artifact"]), 0)
        self.assertEqual(self.system_cards(status="open"), [])
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["access", "fail", "alpha"]), 2)

    def test_cli_check_runs_one(self):
        self.ap()
        out = io.StringIO()
        with mock.patch.object(access, "run_check", return_value=(True, "", "")), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["access", "check", "alpha"]), 0)
        self.assertIn("gh", out.getvalue())


    def test_cli_check_runs_a_named_check(self):
        self.ap(health_checks=[{"name": "cloud", "command": "exit 4", "fix": "log in"}])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["access", "check", "alpha", "check:cloud"]), 1)
        res = json.loads(out.getvalue())
        self.assertEqual((res["check"], res["ok"]), ("check:cloud", False))
        self.assertEqual(len(self.system_cards(status="open")), 1)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["access", "check", "alpha", "check:nope"]), 2)


if __name__ == "__main__":
    unittest.main()
