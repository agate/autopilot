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
                    "echo x > ~/.claude/autopilot$'-data'/x",
                    'echo x > ~/.claude/autopilot$"-data"/x',
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
                    "c$'p' a ~/.claude/autopilot-data/x",
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
                    "sed --in-place 's/a/b/' ~/.claude/autopilot-data/x",
                    "sed -I '' 's/a/b/' ~/.claude/autopilot-data/x",
                    "sed -Ei 's/a/b/' ~/.claude/autopilot-data/x",
                    "sed -nEi p ~/.claude/autopilot-data/x",
                    "cd /tmp && sed -i 's/a/b/' ~/.claude/autopilot-data/x",
                    "find ~/.claude/autopilot-data/ -name '*.md' -exec sed -i 's/a/b/' {} +",
                    "cd /tmp && dd if=/dev/zero of=~/.claude/autopilot-data/x",
                    "find ~/.claude/autopilot-data/ -name x -exec dd if=/dev/zero of={} +"):
            self.assertIsNotNone(guard.decide(bash(cmd), self.W), cmd)

    def test_guard_allows_near_misses_of_the_dd_open_and_sed_forms(self):
        for cmd in ("dd if=~/.claude/autopilot-data/x | cat",
                    "dd if=~/.claude/autopilot-data/x; echo of=1",
                    "dd if=~/.claude/autopilot-data/x & echo of=1",
                    "python3 -c \"open('/x/autopilot-data/y')\"",
                    "python3 -c \"print(open('/x/autopilot-data/y').read(), 'w')\"",
                    "sed -n p ~/.claude/autopilot-data/x",
                    "sed -n p ~/.claude/autopilot-data/x | rg -i y",
                    "sed -n 1p ~/.claude/autopilot-data/runs/P-1/run-index.md",
                    "find ~/.claude/autopilot-data/ -iname '*.md' -exec sed -n 1p {} +",
                    "rg -n parsed -i ~/.claude/autopilot-data/x"):
            self.assertIsNone(guard.decide(bash(cmd), self.W), cmd)

    def test_guard_allows_reads_with_other_harmless_redirects(self):
        for cmd in ("cat ~/.claude/autopilot-data/x >&2",
                    "rg -n x ~/.claude/autopilot-data/x >>/dev/null",
                    "cat ~/.claude/autopilot-data/x 2> /dev/null",
                    "cat ~/.claude/autopilot-data/x > /dev/null"):
            self.assertIsNone(guard.decide(bash(cmd), self.W), cmd)

    def test_guard_still_denies_every_write_verb(self):
        for cmd in ("mv a ~/.claude/autopilot-data/x",
                    "touch ~/.claude/autopilot-data/x",
                    "chmod 600 ~/.claude/autopilot-data/x",
                    "ln -s a ~/.claude/autopilot-data/x",
                    "install a ~/.claude/autopilot-data/x",
                    "truncate -s 0 ~/.claude/autopilot-data/x",
                    "perl -pi -e 's/a/b/' ~/.claude/autopilot-data/x",
                    "python3 -c \"import pathlib; pathlib.Path('/x/autopilot-data/y').write_text('')\"",
                    "python3 -c \"open('/x/autopilot-data/y', 'r+').write('')\""):
            d = guard.decide(bash(cmd), self.W)
            self.assertIsNotNone(d, cmd)
            self.assertEqual(d["hookSpecificOutput"]["permissionDecision"], "deny", cmd)

    def test_guard_still_denies_a_redirect_into_a_file_with_ampersand(self):
        for cmd in ("echo x >& ~/.claude/autopilot-data/x",
                    "echo x >&~/.claude/autopilot-data/x"):
            d = guard.decide(bash(cmd), self.W)
            self.assertIsNotNone(d, cmd)
            self.assertEqual(d["hookSpecificOutput"]["permissionDecision"], "deny", cmd)

    def _assert_fast(self, cmd):
        start = time.monotonic()
        guard.decide(bash(cmd), self.W)
        self.assertLess(time.monotonic() - start, 1.0, cmd[:40])

    def test_guard_answers_a_256_kib_command_in_linear_time(self):
        size = 256 << 10
        mark = " ~/.claude/autopilot-data/x "
        for unit in ("dd ", "open(", "sed -x "):
            self._assert_fast(mark + (unit * (size // len(unit) + 1))[:size])
        self._assert_fast(mark + "1" * size + ">")

    def test_guard_finds_dd_and_open_writes_in_linear_time(self):
        size = 256 << 10
        for unit in ("dd ", "open("):
            text = (unit * (size // len(unit) + 1))[:size]
            start = time.monotonic()
            guard._writes(text)
            self.assertLess(time.monotonic() - start, 1.0, unit)

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
        self.assertIsNotNone(guard.decide(bash("cat ~/.claude/autopilot\\\n-data/RULES.md " + pad), self.W))
        cont = ": " + "a" * (64 << 10) + "; echo x > ~/.claude/autopilot$\\\n'-data/x'"
        self.assertIsNotNone(guard.decide(bash(cont), self.W))
        self.assertIsNone(guard.decide(bash("echo x > /tmp/y " + pad), self.W))
        self.assertIsNone(guard.decide(bash("cat ~/.claude/autopilot-data/RULES.md " + pad), {}))

    def test_guard_reads_a_command_of_exactly_max_command_as_today(self):
        self.assertEqual(guard.MAX_COMMAND, 64 << 10)
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
                    '"/path/bin/ap" journal add v2 X "rm ~/.claude/"autopilot-data"/x"',
                    "'ap' task set v2 PROJ-1 status=done > ~/.claude/autopilot-data/x"):
            self.assertIsNone(guard.decide(bash(cmd), self.W), cmd)

    def test_guard_still_denies_a_write_after_an_ap_line_with_an_even_backslash_run(self):
        self.assertIsNotNone(guard.decide(bash("ap x \\\\\nrm ~/.claude/autopilot-data/y"), self.W))

    def test_guard_does_not_join_a_line_after_an_escaped_backslash(self):
        cmd = 'ap task show X \\\\\nc""p x ~/.claude/"autopilot-data"/y'
        self.assertIsNotNone(guard.decide(bash(cmd), self.W))
        cmd = "echo x > /tmp/a" + "\\\\" + "\n" + "cat ~/.claude/autopilot-data/x"
        self.assertIsNone(guard.decide(bash(cmd), self.W))

    def test_guard_joins_backslash_runs_of_64_kib_in_linear_time(self):
        mark = " ~/.claude/autopilot-data/x "
        size = guard.MAX_COMMAND - len(mark)
        for unit in ("\\\\", "\\\n"):
            self._assert_fast(mark + (unit * (size // len(unit) + 1))[:size])

    def test_guard_checks_an_open_mode_as_written_when_the_path_is_quoted(self):
        cmd = "python3 -c \"open('$HOME/.claude/\"autopilot-data\"/x', 'w')\""
        self.assertIsNotNone(guard.decide(bash(cmd), self.W))
        self.assertIsNone(guard.decide(bash("python3 -c \"open('$HOME/.claude/\"autopilot-data\"/x')\""), self.W))

    def test_guard_reads_a_line_after_a_comment_ending_in_a_backslash_on_its_own(self):
        # bash does not join a `\` + newline inside a comment, so the next line runs as its own command
        for cmd in ('ap task list v2 # see \\\necho x > ~/.claude/"autopilot-data"/x',
                    'ap task list v2 # \\\nc""p a ~/.claude/autopilot-data/x',
                    # no space before the `\`: joined, the verb glues to the comment, so only the split reading sees it
                    "ap task list v2 # see\\\nrm ~/.claude/'autopilot-data'/x",
                    'echo hi # note\\\nc""p a ~/.claude/autopilot-data/x'):
            self.assertIsNotNone(guard.decide(bash(cmd), self.W), cmd)
        self.assertIsNone(guard.decide(bash("ap decision add v2 \\\n~/.claude/autopilot-data/x.json"), self.W))

    def test_guard_reads_a_joined_ap_line_like_any_other(self):
        # an `ap` segment a continuation was joined into is not exempt, whatever a `#` or a split `&&` hides
        for cmd in ("ap note v2 x # see below \\\ncp /tmp/evil.yaml \\\n~/.claude/autopilot-data/config.yaml",
                    "ap note v2 x # \\\nrm -rf \\\n~/.claude/autopilot-data/projects",
                    "ap note v2 x #\\\ncp /tmp/evil.yaml \\\n~/.claude/autopilot-data/config.yaml",
                    "ap note v2 x\t# see \\\ncp /tmp/evil.yaml \\\n~/.claude/autopilot-data/config.yaml",
                    # the glued line repeated on its own (a comment line in bash) does not make it exempt
                    "ap x # \\\ncp a \\\n~/.claude/autopilot-data/y\nap x # cp a ~/.claude/autopilot-data/y",
                    # bash joins `&\` + newline + `&` into `&&`, so the write runs on its own
                    "ap x &\\\n& r\\\nm -rf ~/.claude/autopilot-data/x",
                    "ap x &\\\n& echo y > ~/.claude/autopilot\\\n-data/x",
                    # a separator inside the comment does not end the joined `ap` segment
                    "# ; ap \\\ncp a \\\n~/.claude/autopilot-data/x",
                    "ap x # ; ap \\\ncp a \\\n~/.claude/autopilot-data/x",
                    # the comment sits on a later, joined-in line
                    "ap x \\\n# \\\ncp a \\\n~/.claude/autopilot-data/y"):
            self.assertIsNotNone(guard.decide(bash(cmd), self.W), cmd)
        for cmd in ('ap task set v2 PROJ-1 note="see #3" \\\nstatus=done',
                    "cat ~/.claude/autopilot-data/x \\\n| head"):
            self.assertIsNone(guard.decide(bash(cmd), self.W), cmd)

    def test_guard_allows_a_one_line_ap_call_with_a_hash(self):
        # no continuation is joined into these, so they stay exempt
        for cmd in ('ap journal add v2 X "PR #101: rm stale lock under ~/.claude/autopilot-data/locks"',
                    'ap task note v2 X "fix #12: moved ~/.claude/autopilot-data/a -> ~/.claude/autopilot-data/b"',
                    'ap note v2 "see #3: cp notes into ~/.claude/autopilot-data/x"'):
            self.assertIsNone(guard.decide(bash(cmd), self.W), cmd)

    def test_guard_checks_an_open_mode_with_escaped_quotes(self):
        cmd = 'python3 -c "import json; json.dump({}, open(\\"$AUTOPILOT_DATA/x.json\\", \\"w\\"))"'
        self.assertIsNotNone(guard.decide(bash(cmd), self.W))
        self.assertIsNone(guard.decide(bash('python3 -c "open(\\"$AUTOPILOT_DATA/x.json\\").read()"'), self.W))


if __name__ == "__main__":
    unittest.main()
