from __future__ import annotations

import json
import unittest

from tests.helpers import ROOT
from autopilot import perms


class PermsTest(unittest.TestCase):
    def test_patterns_match_generic_names(self):
        tools = ["mcp__X__createJiraIssue", "mcp__X__addCommentToJiraIssue", "mcp__chat__slack_send_message",
                 "mcp__chat__slack_schedule_message", "mcp__chat__slack_create_canvas", "mcp__X__getJiraIssue",
                 "Read", "mcp__chat__slack_read_channel"]
        self.assertEqual(perms.denied_tools(tools), ["mcp__X__createJiraIssue", "mcp__X__addCommentToJiraIssue",
                                                     "mcp__chat__slack_send_message",
                                                     "mcp__chat__slack_schedule_message",
                                                     "mcp__chat__slack_create_canvas"])

    def test_generate_includes_static_rules(self):
        deny = perms.generate(["mcp__X__createJiraIssue"])["permissions"]["deny"]
        self.assertIn("mcp__X__createJiraIssue", deny)
        self.assertIn("Write(**/autopilot-data/**/RULES.md)", deny)
        self.assertIn("Bash(gh pr comment:*)", deny)

    def test_one_off_excludes_tool(self):
        deny = perms.generate(["mcp__X__createJiraIssue", "mcp__chat__slack_send_message"],
                              exclude={"mcp__X__createJiraIssue"})["permissions"]["deny"]
        self.assertNotIn("mcp__X__createJiraIssue", deny)
        self.assertIn("mcp__chat__slack_send_message", deny)

    def test_settings_files_valid_json(self):
        for name in ("dispatcher.json", "worker.json"):
            data = json.loads((ROOT / "settings" / name).read_text())
            self.assertIn("Write(**/autopilot-data/**/RULES.md)", data["permissions"]["deny"])

    def test_discover_reads_init_tools(self):
        line = json.dumps({"type": "system", "subtype": "init", "tools": ["Read", "mcp__X__createJiraIssue"]})
        self.assertEqual(perms.tools_from_stream(line + "\n" + json.dumps({"type": "result"})),
                         ["Read", "mcp__X__createJiraIssue"])


if __name__ == "__main__":
    unittest.main()
