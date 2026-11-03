from __future__ import annotations

import unittest

from autopilot import guard


def bash(cmd):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


class GuardTest(unittest.TestCase):
    W = {"AUTOPILOT_ROLE": "worker"}

    def test_guard_denies_worker_append(self):
        d = guard.decide(bash('echo "- new rule" >> ~/.claude/autopilot-data/RULES.md'), self.W)
        self.assertEqual(d["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_guard_denies_sed_inplace(self):
        self.assertIsNotNone(guard.decide(bash("sed -i '' 's/a/b/' /x/autopilot-data/checkout/RULES.md"), self.W))

    def test_guard_allows_talk_session(self):
        self.assertIsNone(guard.decide(bash('echo x >> ~/.claude/autopilot-data/RULES.md'), {}))

    def test_guard_allows_read(self):
        self.assertIsNone(guard.decide(bash("cat ~/.claude/autopilot-data/RULES.md"), self.W))

    def test_guard_ignores_other_tools(self):
        self.assertIsNone(guard.decide({"tool_name": "Read", "tool_input": {"file_path": "RULES.md"}}, self.W))


if __name__ == "__main__":
    unittest.main()
