from __future__ import annotations

import json
import unittest

from unittest import mock

from tests.helpers import ROOT, TempData, write_ap
from autopilot import paths, perms


MERGE = "Bash(gh pr merge --match-head-commit:*)"


class PermsTest(unittest.TestCase):
    def test_patterns_match_generic_names(self):
        tools = ["mcp__X__createJiraIssue", "mcp__X__addCommentToJiraIssue", "mcp__chat__slack_send_message",
                 "mcp__chat__slack_schedule_message", "mcp__chat__slack_create_canvas", "mcp__X__getJiraIssue",
                 "Read", "mcp__chat__slack_read_channel"]
        self.assertEqual(perms.denied_tools(tools), ["mcp__X__createJiraIssue", "mcp__X__addCommentToJiraIssue",
                                                     "mcp__chat__slack_send_message",
                                                     "mcp__chat__slack_schedule_message",
                                                     "mcp__chat__slack_create_canvas"])

    def test_atlassian_tool_names_old_and_new(self):
        # The newer Atlassian MCP server renamed its tools; the older server's names must stay caught.
        table = [
            # newer server
            ("addOrEditJiraIssueComment", "deny"), ("createConfluenceContent", "deny"),
            ("updateConfluenceContent", "deny"), ("executeWrite", "deny"), ("executeDestructive", "deny"),
            ("createJiraIssue", "deny"), ("transitionJiraIssue", "status"),
            ("addGraphContext", None), ("atlassianUserInfo", None), ("discover", None), ("editJiraIssue", None),
            ("executeRead", None), ("getAccessibleAtlassianResources", None), ("getCollaborators", None),
            ("getConfluenceContent", None), ("getGraphContext", None), ("getGraphObject", None),
            ("getJiraIssue", None), ("getLoomObjectsContent", None), ("getPeopleInfo", None),
            ("getWorkActivity", None), ("getWorkPortfolio", None), ("listJiraIssueComments", None),
            ("search", None), ("searchConfluence", None), ("searchJiraIssuesUsingJql", None),
            # older server
            ("addCommentToJiraIssue", "deny"), ("createConfluencePage", "deny"), ("updateConfluencePage", "deny"),
            ("createConfluenceFooterComment", "deny"), ("createConfluenceInlineComment", "deny"),
            ("getConfluencePageFooterComments", None), ("getConfluencePageInlineComments", None),
            ("getConfluenceCommentChildren", None), ("getTransitionsForJiraIssue", None),
            # other servers the patterns already catch
            ("slack_send_message", "deny"), ("slack_send_message_draft", "deny"),
            ("slack_schedule_message", "deny"), ("slack_create_canvas", "deny"), ("slack_update_canvas", "deny"),
            ("notion-create-comment", "deny"), ("notion-create-pages", "deny"), ("notion-update-page", "deny"),
            ("appstore_reply_review", "deny"), ("playstore_reply_review", "deny"),
            ("slack_read_channel", None), ("notion-get-comments", None), ("appstore_reviews", None),
        ]
        for name, kind in table:
            with self.subTest(name):
                tool = f"mcp__X__{name}"
                self.assertEqual(perms.denied_tools([tool]), [tool] if kind == "deny" else [])
                self.assertEqual(perms.status_tools([tool]), [tool] if kind == "status" else [])

    def test_generate_includes_static_rules(self):
        deny = perms.generate(["mcp__X__createJiraIssue"])["permissions"]["deny"]
        self.assertIn("mcp__X__createJiraIssue", deny)
        self.assertTrue(any(d.startswith("Edit(//") and d.endswith("/**/RULES.md)") for d in deny))
        self.assertIn("Bash(gh pr comment:*)", deny)

    def test_one_off_excludes_tool(self):
        deny = perms.generate(["mcp__X__createJiraIssue", "mcp__chat__slack_send_message"],
                              exclude={"mcp__X__createJiraIssue"})["permissions"]["deny"]
        self.assertNotIn("mcp__X__createJiraIssue", deny)
        self.assertIn("mcp__chat__slack_send_message", deny)

    def test_settings_files_valid_json(self):
        for name in ("dispatcher.json", "worker.json"):
            data = json.loads((ROOT / "settings" / name).read_text())
            self.assertIn("Edit(~/.claude/autopilot-data/**/RULES.md)", data["permissions"]["deny"])
            self.assertIn(data["env"]["AUTOPILOT_ROLE"], ("dispatcher", "worker"))

    def test_floor_has_no_write_rules(self):
        # Claude Code matches only Edit(path) rules for file tools (they cover Write too) and warns on every
        # session start about each Write(path) rule.
        deny = perms.generate([])["permissions"]["deny"] + perms.static_settings("worker")["permissions"]["deny"]
        self.assertEqual([d for d in deny if d.startswith("Write(")], [])

    def test_static_settings_files_match_generator(self):
        for role in ("dispatcher", "worker"):
            data = json.loads((ROOT / "settings" / f"{role}.json").read_text())
            self.assertEqual(data, perms.static_settings(role))

    def test_discover_reads_init_tools(self):
        line = json.dumps({"type": "system", "subtype": "init", "tools": ["Read", "mcp__X__createJiraIssue"]})
        self.assertEqual(perms.tools_from_stream(line + "\n" + json.dumps({"type": "result"})),
                         ["Read", "mcp__X__createJiraIssue"])

    def test_status_tools_match_on_the_tools_own_name(self):
        tools = ["mcp__X__transitionJiraIssue", "mcp__X__getTransitionsForJiraIssue", "mcp__T__transition_issue",
                 "mcp__T__jira_transition_issue", "mcp__X__getJiraIssue", "mcp__X__editJiraIssue", "Read"]
        self.assertEqual(perms.status_tools(tools), ["mcp__X__transitionJiraIssue", "mcp__T__transition_issue",
                                                     "mcp__T__jira_transition_issue"])

    def test_dispatcher_floor_allows_its_own_steps(self):
        allow = perms.generate(["mcp__X__transitionJiraIssue", "mcp__X__createJiraIssue"],
                               role="dispatcher")["permissions"]["allow"]
        self.assertEqual(allow, ["mcp__X__transitionJiraIssue", "Bash(gh pr ready:*)",
                                 f"Bash({paths.package_root() / 'bin' / 'ap'} worker end:*)"])

    def test_dispatcher_floor_never_allows_a_raw_session_stop(self):
        allow = perms.generate(["mcp__X__transitionJiraIssue"], role="dispatcher")["permissions"]["allow"]
        self.assertEqual([a for a in allow if a.startswith("Bash(claude") or "claude stop" in a], [])

    def test_authority_can_drop_the_allow_rules(self):
        p = perms.generate(["mcp__X__transitionJiraIssue"], role="dispatcher",
                           authority={"source_status": False, "open_prs": False})["permissions"]
        self.assertEqual(p["allow"], [f"Bash({paths.package_root() / 'bin' / 'ap'} worker end:*)"])

    def test_worker_floor_allows_only_the_park_command(self):
        self.assertEqual(perms.generate(["mcp__X__transitionJiraIssue"], role="worker")["permissions"]["allow"], [f"Bash({paths.package_root() / 'bin' / 'ap'} worker park:*)"])

    def test_allow_and_deny_never_overlap(self):
        tools = ["mcp__X__transitionJiraIssue", "mcp__X__createJiraIssue", "mcp__chat__slack_send_message"]
        p = perms.generate(tools, role="dispatcher")["permissions"]
        self.assertEqual(set(p["allow"]) & set(p["deny"]), set())

    def test_write_for_tightens_the_shared_dispatcher_floor(self):
        with TempData() as data:
            write_ap(data, "alpha")
            write_ap(data, "beta", authority={"source_status": False})
            with mock.patch.object(perms, "discover", return_value=(["mcp__X__transitionJiraIssue"], ["X"])):
                perms.write_for("alpha")
            disp = json.loads((data / "settings-dispatcher.json").read_text())["permissions"]
            self.assertNotIn("mcp__X__transitionJiraIssue", disp["allow"])
            self.assertIn("Bash(gh pr ready:*)", disp["allow"])
            worker = json.loads((data / "projects" / "alpha" / "settings.json").read_text())["permissions"]
            self.assertEqual(worker["allow"], [f"Bash({paths.package_root() / 'bin' / 'ap'} worker park:*)"])


    def test_write_for_records_the_servers_the_passes_use(self):
        from autopilot import access
        with TempData() as data:
            write_ap(data, "alpha")
            tools = ["mcp__claude_ai_Tracker__transitionJiraIssue", "mcp__chat__slack_send_message", "Read"]
            with mock.patch.object(perms, "discover", return_value=(tools, ["claude.ai Tracker", "chat"])):
                perms.write_for("alpha")
            self.assertEqual(access.servers("alpha"), ["claude.ai Tracker"])

    def test_servers_from_the_init_event(self):
        line = json.dumps({"type": "system", "subtype": "init", "tools": ["Read"],
                           "mcp_servers": [{"name": "X", "status": "connected"}, {"name": "Y", "status": "needs-auth"}]})
        self.assertEqual(perms.servers_from_stream(line), ["X", "Y"])

    def test_floor_protects_the_access_record(self):
        deny = perms.generate([])["permissions"]["deny"]
        self.assertTrue(any(d.endswith("/**/access.json)") for d in deny))


    def test_merge_rule_requires_the_head_sha_guard(self):
        # A prefix rule: every merge it allows starts with --match-head-commit, so it merges only the
        # head the merge check saw. A bare `gh pr merge <n>` is not matched by it.
        self.assertEqual(MERGE, "Bash(gh pr merge --match-head-commit:*)")
        allow = perms.generate([], role="dispatcher", authority={"merge": True})["permissions"]["allow"]
        self.assertNotIn("Bash(gh pr merge:*)", allow)
        self.assertIn(MERGE, allow)

    def test_merge_rule_only_when_merge_authority_is_true(self):
        ap_end = f"Bash({paths.package_root() / 'bin' / 'ap'} worker end:*)"
        on = perms.generate([], role="dispatcher", authority={"merge": True})["permissions"]["allow"]
        self.assertEqual(on, ["Bash(gh pr ready:*)", MERGE, ap_end])
        for auth in ({}, {"merge": False}, {"merge": "yes"}, None):
            allow = perms.generate([], role="dispatcher", authority=auth)["permissions"]["allow"]
            self.assertNotIn(MERGE, allow, auth)
        self.assertEqual(perms.generate([], role="worker", authority={"merge": True})["permissions"]["allow"], [f"Bash({paths.package_root() / 'bin' / 'ap'} worker park:*)"])

    def test_shared_floor_merges_only_when_every_autopilot_may(self):
        def disp_allow(data):
            with mock.patch.object(perms, "discover", return_value=([], [])):
                perms.write_for("alpha")
            return json.loads((data / "settings-dispatcher.json").read_text())["permissions"]["allow"]
        with TempData() as data:
            write_ap(data, "alpha", authority={"merge": True})
            write_ap(data, "beta", authority={"merge": True})
            self.assertIn(MERGE, disp_allow(data))
            write_ap(data, "beta", authority={})
            self.assertNotIn(MERGE, disp_allow(data))
            write_ap(data, "beta", authority={"merge": True})
            (paths.ap_dir("beta") / "config.json").write_text("{ broken")
            self.assertNotIn(MERGE, disp_allow(data))


if __name__ == "__main__":
    unittest.main()
