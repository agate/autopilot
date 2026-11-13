from __future__ import annotations

import time
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


    def test_guard_allows_reads_with_stderr_redirects(self):
        for cmd in ("ls -la ~/.claude/autopilot-data/x/runs/PROJ-1 2>&1",
                    "cat $HOME/.claude/autopilot-data/replies-check 2>/dev/null",
                    "ls ~/.claude/autopilot-data/ >/dev/null 2>&1",
                    "rg -n x ~/.claude/autopilot-data/x &>/dev/null"):
            self.assertIsNone(guard.decide(bash(cmd), self.W), cmd)

    def test_guard_still_denies_a_write_next_to_a_redirect(self):
        for cmd in ("echo x > ~/.claude/autopilot-data/RULES.md 2>&1",
                    "echo x 2>/dev/null >> ~/.claude/autopilot-data/RULES.md"):
            self.assertIsNotNone(guard.decide(bash(cmd), self.W), cmd)

    def test_guard_denies_a_marker_split_by_quotes_or_backslashes(self):
        for cmd in ('echo x > "$AUTOPILOT_"DATA/config.yaml',
                    'echo x >> ~/.claude/"autopilot-data"/RULES.md',
                    "echo x > ~/.claude/autopilot\\-data/RULES.md",
                    'echo x > "${AUTOPILOT_DATA}"/x',
                    "echo x > ~/.claude/'autopilot'-data/RULES.md",
                    "echo x > ~/.claude/$'autopilot-data'/RULES.md",
                    'echo x > ~/.claude/$"autopilot-data"/RULES.md',
                    "echo x > ~/.claude/autopilot\\\n-data/RULES.md"):
            d = guard.decide(bash(cmd), self.W)
            self.assertIsNotNone(d, cmd)
            self.assertEqual(d["hookSpecificOutput"]["permissionDecision"], "deny", cmd)

    def test_guard_denies_a_write_verb_split_by_quotes_or_backslashes(self):
        for cmd in ('c""p a ~/.claude/autopilot-data/x',
                    "c\\p a ~/.claude/autopilot-data/x",
                    "'rm' ~/.claude/autopilot-data/x",
                    't"ee" ~/.claude/autopilot-data/x',
                    "$'rm' ~/.claude/autopilot-data/x",
                    "s'ed' -i '' 's/a/b/' ~/.claude/autopilot-data/x",
                    "'dd' if=/dev/zero o\"f\"=~/.claude/autopilot-data/x"):
            self.assertIsNotNone(guard.decide(bash(cmd), self.W), cmd)

    def test_guard_still_denies_dd_open_and_sed_in_place_forms(self):
        for cmd in ("dd if=/dev/zero of=~/.claude/autopilot-data/x bs=1 count=1",
                    "python3 -c \"open('/x/autopilot-data/y', 'w').close()\"",
                    "python3 -c \"open('/x/autopilot-data/y', 'a')\"",
                    "python3 -c \"open('/x/autopilot-data/y',\"x\")\"",
                    "sed -i.bak 's/a/b/' ~/.claude/autopilot-data/x",
                    "sed -n -i 's/a/b/' ~/.claude/autopilot-data/x",
                    "sed -e 's/a/b/' -i ~/.claude/autopilot-data/x",
                    "sed -ni p ~/.claude/autopilot-data/x",
                    "sed --in-place 's/a/b/' ~/.claude/autopilot-data/x"):
            self.assertIsNotNone(guard.decide(bash(cmd), self.W), cmd)

    def test_guard_allows_near_misses_of_the_dd_open_and_sed_forms(self):
        for cmd in ("dd if=~/.claude/autopilot-data/x | cat",
                    "dd if=~/.claude/autopilot-data/x; echo of=1",
                    "python3 -c \"open('/x/autopilot-data/y')\"",
                    "python3 -c \"print(open('/x/autopilot-data/y').read(), 'w')\"",
                    "sed -n p ~/.claude/autopilot-data/x",
                    "sed -n p ~/.claude/autopilot-data/x | rg -i y"):
            self.assertIsNone(guard.decide(bash(cmd), self.W), cmd)

    def _assert_fast(self, cmd):
        start = time.monotonic()
        guard.decide(bash(cmd), self.W)
        self.assertLess(time.monotonic() - start, 1.0, cmd[:40])

    def test_guard_answers_a_256_kib_command_in_linear_time(self):
        size = 256 << 10
        for unit in ("dd ", "open(", "sed -x "):
            self._assert_fast((unit * (size // len(unit) + 1))[:size])
        self._assert_fast("1" * size + ">")

    def test_guard_answers_a_64_kib_command_that_is_matched_in_linear_time(self):
        mark = " ~/.claude/autopilot-data/x "
        size = guard.MAX_COMMAND - len(mark) - 1
        for unit in ("dd ", "open(", "sed -x "):
            cmd = mark + (unit * (size // len(unit) + 1))[:size]
            self.assertLessEqual(len(cmd), guard.MAX_COMMAND)
            self._assert_fast(cmd)
        cmd = mark + "1" * size + ">"
        self.assertEqual(len(cmd), guard.MAX_COMMAND)
        self._assert_fast(cmd)

    def test_guard_judges_a_command_too_long_to_check_by_its_markers(self):
        pad = "x" * (65 << 10)
        self.assertIsNotNone(guard.decide(bash("cat ~/.claude/autopilot-data/RULES.md " + pad), self.W))
        self.assertIsNotNone(guard.decide(bash('cat ~/.claude/"autopilot-data"/RULES.md ' + pad), self.W))
        self.assertIsNone(guard.decide(bash("echo x > /tmp/y " + pad), self.W))
        self.assertIsNone(guard.decide(bash("cat ~/.claude/autopilot-data/RULES.md " + pad), {}))

    def test_guard_reads_a_command_of_exactly_max_command_as_today(self):
        head = "cat ~/.claude/autopilot-data/RULES.md "
        read = head + "x" * (guard.MAX_COMMAND - len(head))
        self.assertEqual(len(read), guard.MAX_COMMAND)
        self.assertIsNone(guard.decide(bash(read), self.W))
        head = "echo x > ~/.claude/autopilot-data/RULES.md "
        write = head + "x" * (guard.MAX_COMMAND - len(head))
        self.assertIsNotNone(guard.decide(bash(write), self.W))

    def test_guard_allows_quoted_reads_and_ap(self):
        for cmd in ('cat "$AUTOPILOT_"DATA/RULES.md',
                    'rg -n x ~/.claude/"autopilot-data"/x 2>/dev/null',
                    "rg -n x ~/.claude/autopilot\\-data/x 2>\"/dev/null\"",
                    '"/path/bin/ap" decision add v2 x.json',
                    "'ap' task set v2 PROJ-1 status=done",
                    '"/path/bin/ap" decision add v2 ~/.claude/"autopilot-data"/x.json',
                    "'ap' task set v2 PROJ-1 status=done > ~/.claude/autopilot-data/x"):
            self.assertIsNone(guard.decide(bash(cmd), self.W), cmd)

    def test_guard_still_denies_a_write_after_an_ap_line_with_an_even_backslash_run(self):
        self.assertIsNotNone(guard.decide(bash("ap x \\\\\nrm ~/.claude/autopilot-data/y"), self.W))


if __name__ == "__main__":
    unittest.main()
