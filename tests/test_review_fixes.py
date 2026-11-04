from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT, TempData, write_ap
from autopilot import cli, config, guard, mergecheck, ops, paths, perms, publish, sessions, state, workers

FAKE = str(ROOT / "tests" / "bin" / "fake-claude")


def src(id, status="todo"):
    return {"id": id, "title": {"en": id}, "depends_on": [], "status": status, "link": "", "needs": [],
            "critical": False, "phase": 1}


class Env(unittest.TestCase):
    def setUp(self):
        self.td = TempData()
        self.data = self.td.__enter__()
        self.tmp = Path(tempfile.mkdtemp())
        write_ap(self.data, "checkout", repo=str(self.tmp))
        g = config.load_global()
        g["tools"]["claude"] = FAKE
        config.save_global(g)
        self.log = self.tmp / "log"
        self.agents = self.tmp / "agents.json"
        self.agents.write_text("[]")
        os.environ.update(FAKE_CLAUDE_LOG=str(self.log), FAKE_AGENTS_JSON=str(self.agents))
        (self.data / "checkout" / "settings.json").write_text("{}")

    def tearDown(self):
        for k in ("FAKE_CLAUDE_LOG", "FAKE_AGENTS_JSON"):
            os.environ.pop(k, None)
        self.td.__exit__(None, None, None)


class PermissionFloorTest(Env):
    def test_rules_are_absolute(self):
        deny = perms.generate([])["permissions"]["deny"]
        root = str(paths.data_root())
        self.assertIn(f"Edit(/{root}/**/RULES.md)", deny)
        self.assertIn(f"Write(/{root}/**/config.json)", deny)
        self.assertIn(f"Edit(/{root}/**/settings*.json)", deny)
        self.assertIn(f"Write(/{root}/**/state.json)", deny)
        self.assertIn(f"Edit(/{paths.package_root()}/**)", deny)

    def test_settings_carry_role_env(self):
        s = perms.generate([], role="worker")
        self.assertEqual(s["env"]["AUTOPILOT_ROLE"], "worker")
        self.assertEqual(s["env"]["AUTOPILOT_DATA"], str(paths.data_root()))

    def test_worker_refused_without_settings(self):
        (self.data / "checkout" / "settings.json").unlink()
        with self.assertRaises(RuntimeError):
            workers.build_start(config.load_global(), config.load_ap("checkout"), "checkout", "PROJ-1", "implement", "x")

    def test_broad_outward_patterns(self):
        tools = ["mcp__j__jira_create_issue", "mcp__gh__add_issue_comment", "mcp__gh__create_issue",
                 "mcp__teams__post_message", "mcp__n__notion-create-pages", "mcp__c__createConfluencePage",
                 "mcp__a__getJiraIssue", "mcp__a__transitionJiraIssue", "mcp__a__editJiraIssue",
                 "mcp__a__searchJiraIssuesUsingJql"]
        self.assertEqual(sorted(perms.denied_tools(tools)), sorted(tools[:6]))
        self.assertIn("Bash(gh issue create:*)", perms.generate([])["permissions"]["deny"])

    def test_discovery_without_init_fails(self):
        with self.assertRaises(RuntimeError):
            perms.parse_discovery("no init event here")


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.td = TempData()
        self.data = self.td.__enter__()

    def tearDown(self):
        self.td.__exit__(None, None, None)

    def bash(self, cmd):
        return {"tool_name": "Bash", "tool_input": {"command": cmd}}

    def test_blocks_shell_write_to_config(self):
        d = guard.decide(self.bash(f"echo '{{}}' > {self.data}/checkout/config.json"), {"AUTOPILOT_ROLE": "worker"})
        self.assertIsNotNone(d)

    def test_blocks_python_write(self):
        cmd = f"python3 -c \"open('{self.data}/RULES.md','a').write('x')\""
        self.assertIsNotNone(guard.decide(self.bash(cmd), {"AUTOPILOT_ROLE": "worker"}))

    def test_repo_rules_file_is_fine(self):
        self.assertIsNone(guard.decide(self.bash("sed -i '' 's/a/b/' docs/RULES.md"), {"AUTOPILOT_ROLE": "worker"}))


class SessionsTest(Env):
    def test_failed_listing_is_unknown_not_dead(self):
        w = {"task": "PROJ-1", "role": "implement", "session_name": "checkout · PROJ-1 · implement",
             "started_at": "2026-01-01T00:00:00Z"}
        self.assertEqual(sessions.classify(w, None, self.tmp), "unknown")

    def test_missing_pid_is_not_dead(self):
        w = {"task": "PROJ-1", "role": "implement", "session_name": "n", "started_at": "2026-01-01T00:00:00Z"}
        rows = [{"name": "n", "kind": "background", "status": "busy", "state": "working", "startedAt": 1}]
        self.assertEqual(sessions.classify(w, rows, self.tmp), "running")

    def test_resume_runs_in_worker_cwd(self):
        wt = self.tmp / "wt"
        wt.mkdir()
        rec = workers.start("checkout", "PROJ-1", "implement", str(wt), "do it")
        self.assertEqual(rec["cwd"], str(wt))
        with mock.patch.object(workers, "_launch", return_value="new11111") as launch:
            workers.resume("checkout", rec)
        self.assertEqual(launch.call_args[0][1], str(wt))

    def test_new_session_matched_by_short_id(self):
        self.agents.write_text(json.dumps([
            {"id": "old00000", "name": "checkout · PROJ-1 · implement", "kind": "background", "sessionId": "sid-old",
             "startedAt": 999},
            {"id": "abcd1234", "name": "checkout · PROJ-1 · implement", "kind": "background", "sessionId": "sid-new",
             "startedAt": 5}]))
        rec = workers.start("checkout", "PROJ-1", "implement", str(self.tmp), "do it")
        self.assertEqual(rec["session_id"], "sid-new")


class SyncTest(Env):
    def test_source_does_not_move_local_progress_back(self):
        ops.tasks_sync("checkout", [src("PROJ-1")])
        ops.task_set("checkout", "PROJ-1", {"status": "in_review", "pr": 4})
        ops.tasks_sync("checkout", [src("PROJ-1", status="in_progress")])
        self.assertEqual(state.load("checkout")["tasks"][0]["status"], "in_review")
        ops.tasks_sync("checkout", [src("PROJ-1", status="todo")])
        self.assertEqual(state.load("checkout")["tasks"][0]["status"], "in_review")
        ops.tasks_sync("checkout", [src("PROJ-1", status="done")])
        self.assertEqual(state.load("checkout")["tasks"][0]["status"], "done")


class OneOffTest(Env):
    def _decision(self, answer):
        did = ops.decision_add("checkout", {"kind": "ask", "title": {"en": "Create the ticket?"}, "body": {"en": "b"},
                                            "options": [{"id": "yes", "label": {"en": "Create"}},
                                                        {"id": "no", "label": {"en": "No"}}],
                                            "links": [{"label": "x", "url": "https://example.com"}],
                                            "action": {"tool": "mcp__x__createJiraIssue", "option": "yes",
                                                       "prompt": "Create exactly this issue: ..."}})
        ops.decision_resolve("checkout", did, answer)
        return did

    def test_refuses_non_approving_answer(self):
        did = self._decision("no")
        with self.assertRaises(ValueError):
            perms.run_one_off("checkout", did)

    def test_runs_once_in_dont_ask_mode(self):
        did = self._decision("yes")
        perms.run_one_off("checkout", did)
        argv = self.log.read_text()
        self.assertIn("--permission-mode dontAsk", argv)
        self.assertIn("--allowedTools mcp__x__createJiraIssue", argv)
        with self.assertRaises(ValueError):
            perms.run_one_off("checkout", did)


class MergeCheckTest(unittest.TestCase):
    HEAD = "abc"

    def pr(self, reviews, decision="", checks=("SUCCESS",)):
        return {"headRefOid": self.HEAD, "reviewDecision": decision,
                "statusCheckRollup": [{"conclusion": c} for c in checks],
                "reviews": [{"state": s, "commit": {"oid": oid}, "author": {"login": who}, "submittedAt": at}
                            for who, s, oid, at in reviews]}

    def test_changes_requested_without_branch_protection(self):
        ok, why = mergecheck.evaluate(self.pr([("t", "CHANGES_REQUESTED", "abc", "1")]), [], [], False)
        self.assertIn("changes_requested", why)

    def test_latest_review_per_author_counts(self):
        p = self.pr([("t", "APPROVED", "abc", "1"), ("t", "CHANGES_REQUESTED", "abc", "2")], decision="CHANGES_REQUESTED")
        self.assertFalse(mergecheck.evaluate(p, [], [], True)[0])

    def test_no_checks_yet_is_not_green(self):
        self.assertIn("checks_not_green", mergecheck.evaluate(self.pr([], checks=()), [], [], False)[1])


class NextNamesTest(Env):
    def test_starts_only_for_named_autopilots(self):
        write_ap(self.data, "beta", repo=str(self.tmp / "other"))
        ops.tasks_sync("checkout", [src("PROJ-1")])
        ops.tasks_sync("beta", [src("PROJ-9")])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), mock.patch("autopilot.budget.allowance",
                                                         return_value={"allow": None, "reason": "ok"}):
            cli.main(["next", "checkout", "--json"])
        self.assertEqual([tuple(s) for s in json.loads(buf.getvalue())["starts"]], [("checkout", "PROJ-1")])


class EvidenceTest(Env):
    def test_evidence_check_needs_both_languages(self):
        g = config.load_global()
        g["reader_lang"] = "zh"
        config.save_global(g)
        run = paths.ap_dir("checkout") / "runs" / "PROJ-1"
        run.mkdir(parents=True)
        (run / "evidence.json").write_text(json.dumps({"verdict": {"en": "ok"}, "what_changed": {"en": "x", "zh": "x"},
                                                       "criteria": [], "screenshots": []}))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["evidence", "check", "checkout", "PROJ-1"]), 1)
        self.assertIn("verdict", buf.getvalue())

    def test_screenshot_outside_run_dir_ignored(self):
        run = paths.ap_dir("checkout") / "runs" / "PROJ-1"
        run.mkdir(parents=True)
        ops.tasks_sync("checkout", [src("PROJ-1")])
        (run / "evidence.json").write_text(json.dumps({"screenshots": [{"path": "../../../../etc/hosts",
                                                                        "caption": {"en": "x"}}]}))
        d = publish.diff({}, publish.collect(["checkout"]))
        self.assertEqual(d["assets"], [])


class PageTest(unittest.TestCase):
    def test_links_are_scheme_checked(self):
        page = (ROOT / "templates" / "page.html").read_text()
        self.assertIn("function safeUrl", page)
        self.assertNotIn("href: t.link", page)
        self.assertNotIn("href: l.url", page)


if __name__ == "__main__":
    unittest.main()
