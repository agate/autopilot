"""`ap2 hook-guard` (design 12, workers): the PreToolUse hook on Bash that denies a v2 worker's shell write into the
data folder or the package. Unit tests call `guard.decide` over a table of commands; the command tests run the real
`bin/ap2 hook-guard` with a payload on stdin and a temp HOME."""
from __future__ import annotations

import json
import os
import random
import re
import resource
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import LIB, ROOT
from assistant import guard
from assistant import workers as workers_mod

AP2 = str(ROOT / "bin" / "ap2")
PKG = str(ROOT)
DATA = "/Users/someone/Documents/Assistant"
RUN = f"{DATA}/sessions/C1"
WORKER = {"AP2_ROLE": "implement", "AP2_CARD": "C1", "AP2_DATA": DATA, "AP2_RUN_DIR": RUN,
          "HOME": "/Users/someone"}


def bash(cmd: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


def denied(out) -> bool:
    return bool(out) and out["hookSpecificOutput"]["permissionDecision"] == "deny"


class DecideTest(unittest.TestCase):
    def check(self, cmds, deny: bool, env=None):
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                out = guard.decide(bash(cmd), env or WORKER)
                self.assertEqual(denied(out), deny, out)
                if not deny:
                    self.assertIsNone(out)

    def test_writes_into_the_data_folder_or_the_package_are_denied(self):
        self.check([f"echo x > {DATA}/config.yaml",
                    f"cp a {DATA}/items/x",
                    f"sed -i s/a/b/ {DATA}/routines/r.yaml",
                    "rm -rf $AP2_DATA/items",
                    f"true && tee {PKG}/lib/x",
                    "echo x >> ${AP2_DATA}/gate.json",
                    "mv a ~/Documents/Assistant/chat/x",
                    "touch $HOME/Documents/Assistant/index.sqlite-wal",
                    f"echo x 2>/dev/null >> {DATA}/workers.json",
                    f"echo x > {DATA}/config.yaml 2>&1",
                    f"python3 -c \"open('{DATA}/config.yaml', 'w').write('x')\"",
                    f"ls\ncp a '{DATA}/projects/p.yaml'",
                    f"cat a | tee \"{DATA}/sources/s.yaml\"",
                    f"dd if=/dev/zero of={DATA}/index.sqlite",
                    f"ln -s /tmp/x {DATA}/templates/t",
                    f"chmod 777 {DATA}",
                    # A separator inside quotes does not split the segment: the verb and its target stay together.
                    f"sed -i 's|old|new|' {DATA}/config.yaml",
                    "sed -i \"s|x|y|g\" $AP2_DATA/projects/p.yaml",
                    f"sed -i 's/a;b/c/' {DATA}/routines/r.yaml",
                    f"perl -pi -e 's|a|b|' {DATA}/config.yaml",
                    f"cp 'a|b' {DATA}/config.yaml",
                    f"cp \"$AP2_RUN_DIR/a;\" {DATA}/config.yaml",
                    f"cp a\\;b {DATA}/config.yaml",
                    f"cp 'a&&b' {DATA}/config.yaml",
                    f"cp \"a\nb\" {DATA}/config.yaml"], deny=True)

    def test_segments_split_only_at_unquoted_separators(self):
        self.assertEqual(guard._segments("a | b; c && d || e\nf"), ["a ", " b", " c ", " d ", " e", "f"])
        self.assertEqual(guard._segments("sed 's|a;b|' x | wc"), ["sed 's|a;b|' x ", " wc"])
        self.assertEqual(guard._segments('echo "a\\"|b" ; c'), ['echo "a\\"|b" ', " c"])
        self.assertEqual(guard._segments("echo a\\|b"), ["echo a\\|b"])
        self.assertEqual(guard._segments("echo 'open | x"), ["echo 'open | x"])  # unterminated: one segment
        self.assertEqual(guard._segments("echo '\\'|b"), ["echo '\\'", "b"])  # no escape inside single quotes

    def test_comments_and_heredoc_bodies_are_read_without_quotes(self):
        """Bash reads no quotes in a comment or a heredoc body, so neither can hide the next command."""
        self.check([f"ap2 card list # don't\ncp y {DATA}/config.yaml",
                    f"{PKG}/bin/ap2 card list # it's fine\nrm -rf {DATA}/items",
                    f"ap2 card note C1 <<'EOF'\nap2 isn't it\nEOF\ncp y {DATA}/config.yaml",
                    f"cat <<-\"EOF\" > /tmp/x\n\tdon't\n\tEOF\ncp y {DATA}/config.yaml",
                    f"cat <<EOF # it's\nbody\nEOF\ncp y {DATA}/config.yaml",
                    # A body line is data, not a command: the ap2 exemption does not apply to it.
                    f"cat <<EOF\nap2 x; cp y {DATA}/config.yaml\nEOF",
                    f"echo $((1<<2))\nap2 x; cp y {DATA}/config.yaml",
                    # A here-string `<<<` opens no heredoc, so the next line is scanned as code.
                    f"cat <<<x\nsed -i 's/a/b/\n' {DATA}/config.yaml",
                    # After `$(...)` a `#` stays in the word (bash prints `a#x` for `echo $(echo a)#x`).
                    f"echo $(date)#x; cp y {DATA}/config.yaml",
                    # Nor does a `<<` inside `$((...))`, an arithmetic shift.
                    f"echo $((1<<2))\nsed -i 's/a/b/\n' {DATA}/config.yaml"], deny=True)
        self.check([f"cat >> $AP2_RUN_DIR/journal.md <<'EOF'\nDECISION: don't cache it; rm the temp file.\nEOF\n"
                    f"ls {DATA}/items",
                    f"ls {DATA} # then: cp x > {DATA}/config.yaml",
                    "echo a#b > /tmp/x", f"cat {DATA}/config.yaml <<< 'x' # don't",
                    f"jq -r .id <<<\"$j\"\nls {DATA}/items | tee /tmp/list",
                    f"grep -q x <<<\"$out\"\n{PKG}/bin/ap2 card note C1 'cp it'",
                    f"echo $((1<<2))\n{PKG}/bin/ap2 card note C1 'rm it'",
                    f"(( n = 1 << 2 ))\n{PKG}/bin/ap2 card note C1 'rm it'"], deny=False)
        self.assertIs(type(guard._segments("cat <<<x\nb")[-1]), str)
        self.assertIs(type(guard._segments("echo $((1<<2))\nb")[-1]), str)
        self.assertEqual(guard._segments("(true)# x; y\nb"), ["(true)# x", " y", "b"])  # `)` then `#`: in the word
        self.assertEqual(guard._segments("a # x ; y\nb"), ["a ", "b"])
        self.assertEqual(guard._segments("echo $# ${#x}; b"), ["echo $# ${#x}", " b"])  # not comments
        self.assertEqual(guard._segments("cat <<E; x\nl1; 'q\nE\nz"), ["cat <<E", " x", "l1; 'q", "E", "z"])
        self.assertEqual(guard._segments("cat <<E\nno end; 'q"), ["cat <<E", "no end; 'q"])
        self.check([f"cat > /tmp/msg <<'EOF'\nx\nEOF\nsed -i 's/a/b/\n' {DATA}/config.yaml"], deny=True)
        self.assertEqual(guard._segments("cat <<'E'\nx\nE\ny"), ["cat <<'E'", "x", "E", "y"])
        self.assertIs(type(guard._segments("cat <<'E'\nx\nE\ny")[-1]), str)
        # `<<-` strips the body's leading tabs, so a tab-indented delimiter line ends it.
        self.check(["cat <<-EOF > /tmp/x\n\tbody\n\tEOF\n" + f"{PKG}/bin/ap2 card note C1 'rm it'"], deny=False)

    def test_a_heredoc_opens_and_ends_where_bash_opens_and_ends_it(self):
        """No heredoc inside `${...}` or `$[...]`; the delimiter keeps its backticks; a body line that ends in a
        backslash joins the next one, as bash joins an odd run of them (an unquoted heredoc turns `\\\\` into `\\`, so
        the shell fed the body joins an even run too); inside `$(...)` a `EOF)` line ends the body."""
        self.check(['echo ${x/<</y}\nrm -rf \\\n"$AP2_DATA/victim"',
                    'echo $[1<<2]\nrm -rf \\\n"$AP2_DATA/victim"',
                    'cat <<a`b`\nhi\na`b`\nrm -rf \\\n"$AP2_DATA/victim"',
                    'cat <<EOF\nrm -rf \\\n"$AP2_DATA/victim"\nEOF',
                    'bash <<EOF\nrm -rf \\\n\\\n$AP2_DATA/victim\nEOF',
                    'bash <<EOF\nrm -rf \\\\\n$AP2_DATA/victim\nEOF',
                    "bash <<'EOF'\nrm -rf $AP2_\\\nDATA/victim\nEOF", "bash <<'EOF'\nrm -rf $AP2_DA\\\nTA/victim\nEOF",
                    # An unterminated heredoc: its last line still counts.
                    'bash <<EOF\nrm -rf $AP2_DATA/victim \\',
                    # `EOF)` ends a body only inside `$(...)`.
                    "cat <<EOF\nEOF); ap2 x '\nEOF\ncp a $AP2_DATA/c #'",
                    'x=$(cat <<EOF\nhi\nEOF)\ncp a $AP2_DATA/config.yaml',
                    'cat <<"a\\b"\nab\nap2 card note C1 \'\na\\b\ncp a $AP2_DATA/config.yaml # \'',
                    # A `$'...'` delimiter: bash ends the body at its decoded word (`EO`, `a'b`).
                    'cat <<$"EO"\nbody\nEO\ntouch "\n" $AP2_DATA/x4\n',
                    "cat <<$'E\\x4f'\nbody\nEO\ntouch \"\n\" $AP2_DATA/x3\n",
                    "cat <<$'\\105O'\nbody\nEO\ntouch \"\n\" $AP2_DATA/x3\n",
                    "cat <<$'a\\'b'\nbody\na'b\ntouch \"\n\" $AP2_DATA/x6\n",
                    # A delimiter it cannot decode (a NUL, `\\u`; `$"..."`, above, whose `$` zsh keeps) opens no
                    # body: the next lines are code.
                    "cat <<$'E\\x00'\nE\ntouch \"\n\" $AP2_DATA/x3\n",
                    "cat <<$'\\u0045'\nE\ntouch \"\n\" $AP2_DATA/x3\n"],
                   deny=True)
        for word, delim in (("$'E\\x4f'", "EO"), ('$"EO"', None), ("$'\\105O'", "EO"), ("$'a\\'b'", "a'b"),
                            ("$'\\x41BC'", "ABC"), ("$'\\1050'", "E0"), ("$'\\cA\\ca\\c1'", None),
                            ("$'\\a\\b\\e\\E\\f\\n\\r\\t\\v\\\\\\\"\\?'", "\a\b\x1b\x1b\f\n\r\t\v\\\"?"),
                            ("$'\\q'", None), ("$'\\8'", None), ("$'\\xg'", None), ("$'\\C-a'", None),
                            ("$'E\\.F'", None), ("$'\\M-a'", None), ("$$'\\'", "$$\\"), ('$$"x"', "$$x"),
                            ("$'E\\x00'", None), ("$'\\u0045'", None), ("$'\\777'", None), ("$'\\c'", None),
                            ("$'\\c?'", None), ("$'\\cé'", None),
                            ("$'abc", None)):
            with self.subTest(word=word):
                self.assertEqual(guard._unquote(word), delim)
        self.assertEqual(guard._segments("cat <<$'a\\'b'\nx\na'b\nz"), ["cat <<$'a\\'b'", "x", "a'b", "z"])
        self.assertIs(type(guard._segments("cat <<$'\\u0045'\nE\nz")[-1]), str)
        # Joined (`ls \$AP2_DATA/x`, a fail-closed read of the even run), but `ls` writes nothing.
        self.check(['cat <<EOF\nls \\\\\n$AP2_DATA/x\nEOF', "cat <<'EOF'\nls \\\\\n$AP2_DATA/x\nEOF",
                    'x=$(cat <<EOF\nhi\nEOF)\nls $AP2_DATA; echo "$x" > /tmp/y',
                    'x=$(cat <<-EOF\nhi\n\tEOF)\nls $AP2_DATA; echo "$x" > /tmp/y'], deny=False)
        # A delimiter with a backtick opens no body (bash reads a blank inside it as part of the word): the lines
        # are code, and the command is marked (`<<`) so it is denied when it names a guarded path.
        self.assertEqual(guard._segments("cat <<a`b`\nhi\na`b`\nz"), ["cat <<a`b`", "hi", "b", "a`_`", "z"])
        self.assertEqual(guard._segments('cat <<"a\\$b"\nhi\na$b\nz'), ['cat <<"a\\$b"', "hi", "a$b", "z"])
        # Inside `"..."` a backslash before `b` is kept: the delimiter is `a\b`, so `ab` does not end the body.
        segs = guard._segments('cat <<"a\\b"\nab\na\\b\nz')
        self.assertEqual(segs, ['cat <<"a\\b"', "ab", "a\\b", "z"])
        self.assertIs(type(segs[-1]), str)
        self.assertEqual(guard._segments("x=$(cat <<E\nhi\nE)\nz"), ["cat <<E", "hi", "E", "x=$(_)", "z"])
        self.assertIs(type(guard._segments("x=$(cat <<E\nhi\nE)\nz")[-1]), str)
        self.assertEqual(guard._segments("echo ${x/<</y}\nz"), ["echo ${x/<</y}", "z"])

    def test_a_line_continuation_joins_the_word(self):
        """A backslash-newline outside single quotes is no character at all: bash joins the two lines' words."""
        self.check(["rm -rf $AP2_\\\nDATA/victim", 'rm -rf "$AP2_\\\nDATA/victim"',
                    f"rm -rf {DATA[:10]}\\\n{DATA[10:]}/victim", f"cp a {DATA}/con\\\nfig.yaml"], deny=True)
        self.check(["echo 'a\\\nb' > /tmp/x"], deny=False)
        self.assertEqual(guard._words("a$AP2_\\\nDATA \"x\\\ny\" 'p\\\nq'"), ["a$AP2_DATA", "xy", "p\\\nq"])
        pad = "dd " * (guard.MAX_COMMAND // 3 + 1)
        self.assertTrue(denied(guard.decide(bash(pad + "rm -rf $AP2_\\\nDATA/victim"), WORKER)))
        raw = json.dumps(bash("rm -rf $AP2_\\\nDATA/victim")).encode()
        self.assertTrue(denied(guard._fallback(raw, WORKER)))
        self.assertIsNone(guard._fallback(raw.replace(b"AP2_", b"NOPE_"), WORKER))

    def test_a_separator_inside_a_parameter_expansion_does_not_split(self):
        self.check(['rm -rf ${x:-;} "$AP2_DATA/victim"', 'rm -rf ${x:-|} "$AP2_DATA/victim"',
                    'rm -rf ${x:-&} "$AP2_DATA/victim"',
                    # `$$` is the PID, so `$${` opens no expansion: bash splits at the `;`.
                    f"ap2 status $${{x:-; cp a {DATA}/config.yaml"], deny=True)
        self.assertEqual(guard._segments("a ${x:-;|&} b; c"), ["a ${x:-;|&} b", " c"])
        # A newline inside `${...}` does not split either.
        self.check(["rm -rf ${x:-\n} $AP2_DATA/victim"], deny=True)

    def test_a_parameter_expansion_inside_double_quotes_holds_its_own_quotes(self):
        """In `"${x:-...}"` bash reads quotes afresh (`"${x:-'}'}"` prints `'}'`, `"${x:-"a; b"}"` holds a nested
        quote), up to the `}` that closes it."""
        self.check(['rm -f "${x:-"a; b"}" $AP2_DATA/items/keep', 'cp a "${x:-"z|y"}" $AP2_DATA/items/',
                    'rm -rf "${x:-" #"}" $AP2_DATA/items',
                    'touch "${OUT:-"$AP2_RUN_DIR/x"}" "${x:-"; "}" $AP2_DATA/items/new',
                    "ap2 card list --q \"${q:-'\"'}\"\nrm -rf $AP2_DATA/items\necho '",
                    "ap2 card list \"${x:-'\"'}\"; cp a $AP2_DATA/items/ # '",
                    'ap2 card list "${x:-$(cp a $AP2_DATA/c)}"',
                    # A `"${` or `"$$(` the reading leaves open keeps no exemption.
                    'ap2 card list "${x:-\ncp a $AP2_DATA/c', 'ap2 card list "$$(\ncp a $AP2_DATA/c'], deny=True)
        self.check(['cp a "${HOME}/x" && ls $AP2_DATA', 'echo "${#arr[@]}" > /tmp/x; ls $AP2_DATA',
                    'echo "${x//\\"/}" > /tmp/x; ls $AP2_DATA', 'echo "${x:-a b}" > "$AP2_RUN_DIR/x.txt"',
                    'ap2 card note C1 --data $AP2_DATA --text "${msg:-"a; b"} done"'], deny=False)
        self.assertEqual(guard._segments('a "${x:-"b; c"}"; d'), ['a "${x:-"b; c"}"', " d"])
        self.assertEqual(guard._words('a "${x:-"b c"} d" e'), ["a", "${x:-b c} d", "e"])

    def test_a_segment_cut_inside_a_command_substitution_keeps_its_quotes(self):
        """A separator inside `"$(...)"` splits, and the next segment starts inside that `$(`: its `)"` closes the
        quote, it opens none."""
        self.check(['echo "n=$(ls | wc -l)" > "$AP2_RUN_DIR/count.txt" 2>"$AP2_RUN_DIR/err.txt"',
                    'jq -n --arg pr "$(gh pr view --json url | jq -r .url)" \'{status: "done", pr: $pr}\' '
                    '> "$AP2_RUN_DIR/handback.json" 2>"$AP2_RUN_DIR/jq.err"',
                    'x="$(ls; echo "${y:-$(date; true)}")" && echo "$x" > "$AP2_RUN_DIR/a" 2>"$AP2_RUN_DIR/b"'],
                   deny=False)
        self.check(['echo "$(ls | cp a $AP2_DATA/c)" > "$AP2_RUN_DIR/x"',
                    'echo "$(ls; ap2 card list > $AP2_DATA/c)" > /tmp/x',
                    'echo "$(ls; cp a "$AP2_DATA/c")" > /tmp/x'], deny=True)
        # The inner commands are segments of their own, and the outer command one segment, its verb and all its
        # words together, with each substitution's text replaced by `_`.
        self.assertEqual(guard._segments('a "$(b; c)" d'), ["b", " c", 'a "$(_)" d'])
        self.assertEqual(guard._segments("a `b; c` d; e"), ["b", " c", "a `_` d", " e"])

    def test_a_separator_inside_a_substitution_keeps_the_outer_command_whole(self):
        """A `;` or `|` inside `$(...)` or backticks splits only the inner commands: the outer verb still sees all
        its words."""
        self.check(['cp a "$(true; true)" $AP2_DATA/c', "cp a $(true; true) $AP2_DATA/c",
                    "cp a `true; true` $AP2_DATA/c", 'cp a "`true; true`" $AP2_DATA/c',
                    'tee "$(a | b)" $AP2_DATA/x < /dev/null', "cp a $(x; $(y; z)) $AP2_DATA/c",
                    'echo "$( (ls; ap2 x) > $AP2_DATA/c )"', 'echo "$( (ls; ap2 x) >$AP2_DATA/c)"',
                    'echo "$( ((cd /tmp; ap2 x) ) ; cp a $AP2_DATA/c )"',
                    'echo "$( ((cd /tmp; ap2 x) ); cp a $AP2_DATA/c )"',
                    # In bash 3.2 nothing in `"$$(...)"` splits, so the write is one command.
                    'tee "$$(x ; y " ; " z)" $AP2_DATA/x < /dev/null', 'tee "$$(x #y " ; " z)" $AP2_DATA/x < /dev/null',
                    'cp a "$$(x ; y " ; " z)" $AP2_DATA/'], deny=True)
        self.check(['echo "$(echo "${y:-)}"; ap2 x --data $AP2_DATA > /tmp/y)"',
                    "x=$(cat <<EOF\nhi\nEOF); ap2 card note C1 'it\n...\n'"], deny=False)

    def test_a_heredoc_body_is_read_at_the_depth_it_was_opened(self):
        """A heredoc opened before a `$(` has its body after the `$(...)` closes: bash reads the substitution whole
        first. One opened inside a `$(` that closes on its line has no body (bash runs the next lines)."""
        self.check(['cat <<EOF $(\ntouch x "\nEOF\n" $AP2_DATA/c)\nEOF',
                    'cat <<EOF >/dev/null $(\ntouch x "\nEOF\n" $AP2_DATA/c)\nbody\nEOF',
                    "echo $(cat <<EOF)\ncp a $AP2_DATA/c\nEOF",
                    "x=$(cat <<EOF)\nx'\nEOF\nap2 card note C1 y'; cp a $AP2_DATA/c\n'\n'"], deny=True)
        self.assertEqual(guard._segments("a <<E $(b\nc)\nl\nE\nz"), ["b", "c", "a <<E $(_)", "l", "E", "z"])
        segs = guard._segments("x=$(cat <<E)\nl\nE\nz")
        self.assertEqual(segs, ["cat <<E", "x=$(_)", "l", "E", "z"])
        self.assertIs(type(segs[2]), str)  # the heredoc closed with its `$(`: its next line is code

    def test_ap2_by_its_full_path_in_command_position_is_no_package_mention(self):
        """After a keyword, inside `$(...)` or after `xargs`, `<pkg>/bin/ap2` is run, not written."""
        self.check([f"for c in C1 C2; do {PKG}/bin/ap2 card show $c > /tmp/$c.json; done",
                    f"if [ -f x ]; then {PKG}/bin/ap2 card show C1 > /tmp/c.json; fi",
                    f"time {PKG}/bin/ap2 card list > /tmp/x",
                    f"echo \"$({PKG}/bin/ap2 card show C1 --json)\" > /tmp/card.json",
                    f"echo `{PKG}/bin/ap2 card show C1` > /tmp/card.json",
                    f"{PKG}/bin/ap2 card list --ids | xargs -n1 {PKG}/bin/ap2 card show > $AP2_RUN_DIR/all.txt",
                    f"{PKG}/bin/ap2 card list --ids | xargs -n 1 -P 4 {PKG}/bin/ap2 card show > /tmp/all.txt",
                    f"while true; do ap2 card list --data $AP2_DATA > /tmp/x; done"], deny=False)
        self.check([f"cp x {PKG}/bin/ap2", f"echo x > {PKG}/bin/ap2", f"sed -i s/a/b/ {PKG}/bin/ap2",
                    f"> {PKG}/bin/ap2 echo x", f"echo \"$(> {PKG}/bin/ap2 true)\"", f"cp \"$()\" {PKG}/bin/ap2",
                    f"xargs cp x {PKG}/bin/ap2", f"do ap2 card list > $AP2_DATA/x",
                    # A `$(...)` or backticks that close before a command name leave the next word an argument.
                    f"tee \"$(time)\" {PKG}/bin/ap2 < x", f"tee \"`time`\" {PKG}/bin/ap2 < x",
                    f"do ap2(){{ cp a {DATA}/config.yaml; }}; ap2", f"then {{ ap2 () {{ rm -rf {DATA}/items; }}; }}",
                    # An assignment or a runner before a command that is not ap2 leaves the path an argument.
                    f"FOO=1 cp x {PKG}/bin/ap2", f"env cp x {PKG}/bin/ap2", f"nohup cp x {PKG}/bin/ap2",
                    f"env -S 'cp x' {PKG}/bin/ap2", f"command -v {PKG}/bin/ap2 > {PKG}/bin/ap2",
                    # A runner flag's value (`xargs -I ap2`) or an unknown flag (`env -P`) is no command name.
                    "ls | xargs -I ap2 cp a $AP2_DATA/c", "ls | xargs -E ap2 cp a $AP2_DATA/c",
                    "ls | xargs -J ap2 cp a $AP2_DATA/c", "env -P ap2 /bin/cp a $AP2_DATA/c",
                    "env -S ap2 cp a $AP2_DATA/c"],
                   deny=True)

    def test_ap2_after_an_assignment_or_a_runner_is_still_run(self):
        """`NAME=value` words, `env`, `nohup`, `command`, `nice` and `time -p` come before the command bash runs."""
        self.check([f"AP2_DEBUG=1 {PKG}/bin/ap2 card show C1 > /tmp/c.json",
                    f"nohup {PKG}/bin/ap2 card list > $AP2_RUN_DIR/cards.txt 2>&1",
                    f"env FOO=1 {PKG}/bin/ap2 card list > /tmp/x", f"env -i -u X {PKG}/bin/ap2 card list > /tmp/x",
                    f"command {PKG}/bin/ap2 card show C1 > $AP2_RUN_DIR/c.json",
                    f"nice -n 5 {PKG}/bin/ap2 card list > /tmp/x", f"nohup nice {PKG}/bin/ap2 card list > /tmp/x",
                    f"time -p {PKG}/bin/ap2 card list > /tmp/x", "A=1 B=2 ap2 card list --data $AP2_DATA > /tmp/x",
                    "if ap2 card list --data $AP2_DATA > /tmp/x; then :; fi", "! ap2 card list --data $AP2_DATA > /tmp/x",
                    f"{{ {PKG}/bin/ap2 card list > /tmp/x; }}", f"ls | /usr/bin/xargs {PKG}/bin/ap2 card show > /tmp/x",
                    "if false; then :; else ap2 card list --data $AP2_DATA > /tmp/x; fi",
                    "if false; then :; elif ap2 card list --data $AP2_DATA > /tmp/x; then :; fi",
                    "while ap2 card list --data $AP2_DATA > /tmp/x; do break; done",
                    "until ap2 card list --data $AP2_DATA > /tmp/x; do break; done",
                    "nohup ap2 card list --data $AP2_DATA > /tmp/x", "env A=1 ap2 card list --data $AP2_DATA > /tmp/x"],
                   deny=False)

    def test_a_function_named_ap2_after_any_keyword_is_no_ap2_call(self):
        self.check(["if ap2(){ cp a $AP2_DATA/config.yaml; }; ap2; then :; fi",
                    "while ap2(){ cp a $AP2_DATA/config.yaml; }; ap2; do break; done",
                    "until ap2(){ cp a $AP2_DATA/config.yaml; }; ap2; do :; done",
                    "if false; then :; else ap2(){ cp a $AP2_DATA/config.yaml; }; ap2; fi",
                    "if false; then :; elif ap2(){ cp a $AP2_DATA/config.yaml; }; ap2; then :; fi",
                    "! ap2(){ cp a $AP2_DATA/config.yaml; }; ap2", "time ap2(){ cp a $AP2_DATA/config.yaml; }; ap2",
                    "time -p ap2(){ cp a $AP2_DATA/config.yaml; }; ap2",
                    # `function` is no keyword before a command: the segment's command is `function`, not ap2.
                    "function ap2 { cp a $AP2_DATA/c; }; ap2", "function ap2 () { cp a $AP2_DATA/c; }; ap2"],
                   deny=True)
        for word in guard._KEYWORDS:
            with self.subTest(word):
                self.assertTrue(guard._FUNC.match(f"{word} ap2(){{ x; }}"))

    def test_a_case_pattern_named_ap2_is_no_ap2_call(self):
        """A `)` that closes no `(` of the segment and has a command after it is a case pattern's end."""
        self.check(["case ap2 in x) ;; ap2) cp a $AP2_DATA/c;; esac", "case ap2 in\nx) ;;\nap2) cp a $AP2_DATA/c;;\nesac",
                    "case ap2 in x) ;; (ap2) cp a $AP2_DATA/c;; esac", "case ap2 in\n(ap2) cp a $AP2_DATA/c;;\nesac",
                    # A redirect before the pattern's command still leaves a command after the `)`.
                    "case ap2 in x) ;; ap2) 2>/dev/null cp a $AP2_DATA/c;; esac",
                    "case ap2 in x) ;; ap2) </dev/null cp a $AP2_DATA/c;; esac",
                    "case ap2 in x) ;; ap2) &>/dev/null cp a $AP2_DATA/c;; esac",
                    # A redirect operator of several characters (`>&`, `>|`) is read whole before its target.
                    "case x in x) ;; ap2) 2>&1 cp a $AP2_DATA/c;; esac",
                    "case x in x) ;; ap2) >&2 cp a $AP2_DATA/c;; esac",
                    "case x in x) ;; ap2) >|/tmp/x cp a $AP2_DATA/c;; esac"],
                   deny=True)
        self.check(["(cd /tmp; ap2 card list --data $AP2_DATA > /tmp/x)",
                    "(ap2 card list --data $AP2_DATA) 2> /tmp/err"], deny=False)

    def test_a_redirect_before_the_command_is_still_a_write(self):
        self.check(['>"$AP2_DATA/gate.json"', "> $AP2_DATA/config.yaml echo x", ">$AP2_DATA/x ap2 card list"],
                   deny=True)
        self.check(["ap2 card list > /tmp/cards.txt --data $AP2_DATA"], deny=False)

    def test_a_backtick_inside_double_quotes_reads_its_own_quotes(self):
        self.check(['echo "`echo " #"`"; rm -rf $AP2_DATA/items',
                    'git commit -m "`printf "%s #%s" fix 12`"; rm -rf $AP2_DATA/items',
                    'cp "`echo "x;y"`" $AP2_DATA/config.yaml',
                    'echo "`cp a $AP2_DATA/config.yaml`"',
                    'ap2 card note C1 --text "`echo ok`"; cp a $AP2_DATA/config.yaml'], deny=True)
        self.check(['echo "`date +"%F #"`" > /tmp/x; ls $AP2_DATA'], deny=False)
        self.assertEqual(guard._segments('a "`b; c`" d; e'), ["b", " c", 'a "`_`" d', " e"])

    def test_a_backtick_outside_quotes_ends_at_the_next_backtick_too(self):
        """Bash ends a backtick substitution at the first unescaped backtick, whatever quotes lie between, and
        reads its text as commands of its own; only the opening backtick stays in the outer word."""
        self.check(["touch `#'`/Users/someone/Documents/Assis\\tant/c`#'`", "touch `#'`$AP2_RUN_DIR/\\.\\./c\\'",
                    "cp x $AP2_RUN_DIR`echo /../..`/config.yaml", "cp x $AP2_RUN_DIR/`echo ../..`/config.yaml"],
                   deny=True)
        self.check(["cp `ls $AP2_RUN_DIR/media/*.png` /tmp/", "echo `cat $AP2_RUN_DIR/handback.json` > /tmp/h",
                    f"cat >> $AP2_RUN_DIR/journal.md <<'EOF'\n- `{RUN}/media/shot.png` -> attached\nEOF",
                    # Accepted: a path built at run time, as with `$(...)`.
                    "cp x `echo $AP2_RUN_DIR`/../../config.yaml"], deny=False)
        self.assertEqual(guard._words("a`b 'c`d e"), ["a`", "b", "c", "d", "e"])
        self.assertEqual(guard._words('a "x`b "c d"`y" e'), ["a", "x`", "b", "c d", "y", "e"])

    def test_a_double_paren_that_bash_runs_as_two_subshells_is_no_arithmetic(self):
        """`((cd x; ls) )` closes with `) )`, not `))`: two subshells, so a later `<<` opens a heredoc."""
        self.check([f"((cd /tmp; ls) ); cat <<EOF\nit's\nEOF\nsed -i 's/a/b/\n' {DATA}/config.yaml",
                    f"((cd /tmp && make) 2>&1)\ncat <<EOF > /tmp/x\ndon't\nEOF\necho x > {DATA}/config.yaml",
                    f"echo $(( (n+1) << 2 ))\nsed -i 's/a/b/\n' {DATA}/config.yaml",
                    # An arithmetic `|`, `&` or `#` neither splits nor starts a comment (bash: `((#))` is one
                    # arithmetic error, then the next command runs); `((ap2 ...` is no ap2 call.
                    'rm -rf $((1|0)) "$AP2_DATA/victim"', 'rm -rf $((1&1)) "$AP2_DATA/victim"',
                    '((x=1|2)); rm -rf "$AP2_DATA/victim"', '((x=1|2)) && rm -rf "$AP2_DATA/victim"',
                    '((ap2 status; rm -rf "$AP2_DATA/victim") )', '((ap2 status | rm -rf "$AP2_DATA/victim") )',
                    '((ap2 status && rm -rf "$AP2_DATA/victim") )',
                    '((#)); rm -rf "$AP2_DATA/victim"', '(( #)); rm -rf "$AP2_DATA/victim"',
                    # Old-style `$[...]` arithmetic: its `<<` is a shift, its `|` no pipe.
                    'echo $[1<<2]\nrm -rf \\\n"$AP2_DATA/victim"', 'rm -rf $[1|0] "$AP2_DATA/victim"',
                    # `$$` is the PID, so `$$[` opens nothing: bash runs the `|` as a pipe.
                    f"ap2 status $$[1| cp a {DATA}/config.yaml",
                    'rm -rf $[a[1]|0] "$AP2_DATA/victim"', "echo \"$(echo $[(1)]) ; ap2 x '\" ; cp a $AP2_DATA/c #'",
                    "ap2 x $[(1)] | cp a $AP2_DATA/c", "ap2 card note C1 $[(n+1)*2] && cp a $AP2_DATA/config.yaml",
                    # Bash keeps arithmetic open across a newline: the `#` there starts no comment.
                    "((1\n#)); rm -rf $AP2_DATA/x", "(( n +\n#)) || rm -rf $AP2_DATA/x"],
                   deny=True)
        self.check([f"x=$((a<<b)); cat <<EOF\nit's\nEOF\nls {DATA}/items",
                    f"echo $(( (1+2) << 1 )) $(((1+2)))\ncat <<EOF\nit's\nEOF\nls {DATA}",
                    f"(( n = 1 +\n2 ))\n{PKG}/bin/ap2 card note C1 'rm it'",
                    f"(( n = 1 <<\n2 )); cat <<EOF\nit's\nEOF\nls {DATA}/items"], deny=False)
        self.assertEqual(guard._segments("((1\n#)); b"), ["((1\n#))", " b"])

    def test_a_double_paren_found_to_be_two_subshells_is_read_again_as_them(self):
        """The `((` is read as arithmetic (no comment, heredoc or newline split) until a single `)` shows it was
        two subshells; then its text is read again as code, so a `#` there starts a comment and a quote in it
        hides nothing. A `"$((cmd) )"` splits like `"$(`."""
        self.check(["((x; ap2 x --data $AP2_DATA # it's\ncp a $AP2_DATA/c # '\n) )",
                    "((x # )\ncp a $AP2_DATA/c\n) )", "(( (x # )\ncp a $AP2_DATA/c\n) ) )",
                    "((cat <<E\n'\nE\ncp a $AP2_DATA/c\n) )",
                    'echo "$((cp a b; cp x $AP2_DATA/c) )"', "cp a \"$((true; true) )\" $AP2_DATA/c",
                    "cp a $((true; true) ) $AP2_DATA/c"], deny=True)
        self.check(['ap2 card note C1 --text "rm step $((n+1)) failed"', 'echo "$((1<<2))" > /tmp/x; ls $AP2_DATA',
                    'echo "$(( (1+2) * 3 ))" "$((a; b) )" > /tmp/x'], deny=False)
        self.assertEqual(guard._segments('a "$((b; c) )" d'), ["(b", " c) ", 'a "$(_)" d'])
        self.assertEqual(guard._segments("((a # )\nb\n) )"), ["((a ", "b", ") )"])

    def test_a_double_paren_read_again_too_often_is_judged_like_a_command_too_long_to_check(self):
        """Each nested `((` that turns out to be two subshells is read again; past four times the command's length
        in all, the command is denied when it names a guarded path anywhere, as one past MAX_COMMAND is."""
        deep = "(( " * 12 + "x; ap2 x --data $AP2_DATA # it's\ncp a $AP2_DATA/c # '\n" + " )" * 24
        with self.assertRaises(guard._Deep):
            guard._segments(deep)
        self.check([deep], deny=True)
        self.check(["(( " * 12 + "x" + " )" * 24 + "; ls /tmp"], deny=False)
        # The same past `decide`'s own budget: `_command_writes` runs out of it, and `decide` denies.
        deeper = "(( " * 80 + "x; cp src $AP2_DATA/c\n" + " )" * 160
        with self.assertRaises(guard._Deep):
            guard._command_writes(deeper, guard._Marks(WORKER), 0,
                                  guard._Budget(guard.BUDGET * len(deeper) + guard.BUDGET_EXTRA))
        self.check([deeper], deny=True)

    def test_a_background_ampersand_splits_but_a_redirect_does_not(self):
        self.check([f"ap2 card list & cp y {DATA}/config.yaml",
                    f"ap2 card list &>/dev/null & rm {DATA}/config.yaml",
                    f"ap2 card list |& tee {DATA}/config.yaml"], deny=True)
        self.check([f"ls {DATA} 2>&1 >&2", f"rg x {DATA} &>/dev/null", f"rg x {DATA} &>>/dev/null",
                    f"ls {DATA} 0<&3 &"], deny=False)
        self.assertEqual(guard._segments("a & b |& c 2>&1 >&2 &>/dev/null &>>x <&3"),
                         ["a ", " b ", " c 2>&1 >&2 &>/dev/null &>>x <&3"])
        self.assertEqual(guard._segments("echo \\>& b"), ["echo \\>", " b"])  # an escaped `>` is no redirect

    def test_a_noclobber_redirect_is_a_write_not_a_pipe(self):
        self.check([f"echo x >| {DATA}/config.yaml", f"echo x 2>|{DATA}/config.yaml",
                    f"ap2 card list >| {DATA}/config.yaml"], deny=True)
        self.assertEqual(guard._segments("echo x >| f | wc"), ["echo x >| f ", " wc"])

    def test_a_command_substitution_starts_fresh_quotes_even_inside_double_quotes(self):
        self.check([f"who=\"$(git log -1 --format=\"%an's change\")\"\nls {DATA}/items\necho \"$who\" > /tmp/who.txt",
                    f"ap2 card note C1 \"$(cat <<'EOF'\nit's\nEOF\n)\"\nls {DATA}/items"], deny=False)
        self.check([f"echo \"$(date +\"%F #\")\" > {DATA}/config.yaml",
                    f"ap2 card note C1 \"$(cat <<'EOF'\nit's\nEOF\n)\"\ncp a {DATA}/config.yaml"], deny=True)
        self.assertEqual(guard._words('a "b $(c "d e") f" g'), ["a", "b $", "c", "d e", " f", "g"])

    def test_an_ansi_c_quote_keeps_an_escaped_apostrophe(self):
        self.check([f"printf $'a\\'b\\n' > /tmp/x\nls {DATA}/items"], deny=False)
        self.check([f"ap2 card add --title $'it\\'s' ; cp a {DATA}/config.yaml",
                    f"ap2 card add --title \\$'it\\' ; cp a {DATA}/config.yaml # ''",
                    # `$$` is the PID: the quote after it is a plain one, where a backslash is just a character.
                    "ap2 status $$'\\'; rm -rf \"$AP2_DATA/victim\"; echo $$'\\'",
                    "echo $$'\\'' #'; rm -rf $AP2_DATA/items",
                    f"ap2 status \\$$$'\\'; cp a {DATA}/config.yaml #'"], deny=True)
        self.check([f"printf $$$'a\\'b' > /tmp/x\nls {DATA}/items"], deny=False)
        self.assertEqual(guard._words("printf $'a\\'b' \"$'x'\""), ["printf", "a'b", "$'x'"])

    def test_a_hash_that_does_not_start_a_word_is_no_comment(self):
        self.check([f"echo x\\ #y > {DATA}/config.yaml",
                    f"echo ${{x:- #}}; cp a {DATA}/config.yaml",
                    f"echo ${{x// #/}}; cp a {DATA}/config.yaml",
                    f"v=`echo a #`; cp a {DATA}/config.yaml",
                    f"touch /tmp/a\\ #1 && cp a {DATA}/config.yaml"], deny=True)
        self.check([f"ap2 card show ${{C}} # don't\ncp a {DATA}/config.yaml # it's ok"], deny=True)
        self.check([f"(true) # cp a {DATA}/config.yaml", f"echo a \\\\ # cp a {DATA}/config.yaml",
                    f"ls ${{HOME}} # cp x > {DATA}/config.yaml"], deny=False)

    def test_an_ap2_segment_that_runs_other_code_loses_its_exemption(self):
        self.check([f"ap2 card add --title \"$(cp a {DATA}/config.yaml)\"",
                    f"ap2 card add --title `cp a {DATA}/config.yaml`",
                    f"ap2 card show C1 <(rm -rf {DATA}/items)",
                    f"ap2(){{ cp a {DATA}/config.yaml; }}; ap2",
                    f"ap2 () {{ rm -rf {DATA}/items; }}; ap2",
                    f"ap2 card add --title 'it; cp a {DATA}/config.yaml"], deny=True)
        self.check(["ap2 card add --body \"$(cat $AP2_RUN_DIR/b.md)\"",
                    f"{PKG}/bin/ap2 card add --title x --body {DATA}/sessions/C1/b.md",
                    f"ap2 card add --title '$(cp a {DATA}/config.yaml)'",
                    f"ap2 card add --title \"$((1+2))\" --data {DATA}",
                    f"{PKG}/bin/ap2 worker park C1 implement --note \"can't rm the lock; needs a human\"",
                    f"{PKG}/bin/ap2 worker park C1 implement --note $'can\\'t rm the lock'",
                    f"{PKG}/bin/ap2 worker park C1 implement --note can\\'t-rm",
                    f"{PKG}/bin/ap2 worker park C1 implement --note \"rm step $((n+1)) failed\"",
                    # The same notes with a data-folder mention, so the exemption is what decides.
                    "ap2 card note C1 --data $AP2_DATA --text \"can't rm the lock; needs a human\"",
                    "ap2 card note C1 --data $AP2_DATA --text $'can\\'t rm the lock'",
                    "ap2 card note C1 --data $AP2_DATA --text can\\'t-rm",
                    "ap2 card note C1 --data $AP2_DATA --text \"rm step $((n+1)) failed\"",
                    "ap2 card note C1 --data $AP2_DATA --text \"x=$(( (n+1) * 2 ))\"",
                    # `$$` is the PID: in `"$$(cp)"` the `(cp)` is text, nothing runs.
                    'ap2 card note C1 --data $AP2_DATA --text "pid $$(cp)"'], deny=False)
        self.check([# Inside `"..."` too, a `$((` that closes with `) )` runs a command: `$((cmd) )`.
                    'ap2 card add --title "$((cp a $AP2_DATA/c) )"'], deny=True)
        # The `$(...)` text is a segment of its own (`cp a b` names nothing guarded); the ap2 segment around it
        # loses its exemption but writes nothing.
        self.check(["ap2 card note C1 --data $AP2_DATA --text \"$(cp a b)\""], deny=False)

    def test_a_pid_before_a_paren_inside_double_quotes_still_reads_its_quotes(self):
        """bash 3.2 reads `"$$(...)"` as the PID and then a `(...)` that still holds its own quotes, so the `"'"`
        in there does not open a single quote; bash 5.3 at times reads it as plain text, so both readings count."""
        self.check(["ap2 x \"$$(echo \"'\")\" ; cp a $AP2_DATA/c #'",
                    # A plain `$$'` quote (no ANSI-C escape) and an escaped `\$'`, with a real `$(` after it.
                    "ap2 status $$'\\' > $AP2_DATA/config.yaml #'",
                    "ap2 x $$'\\' \"$(cp a $AP2_DATA/c)\" \"$(echo \"'\")\"",
                    "ap2 x \\$'\\' \"$(cp a $AP2_DATA/c)\" \"$(echo \"'\")\"",
                    # Nothing in `"$$(...)"` runs or splits: the `;` there is text, the write is one command.
                    "cp a \"$$(;)\" $AP2_DATA/x",
                    # bash 5.3 reads this `"$$("` as plain text and runs the next line: both readings are judged.
                    'ap2 x "$$("\ncp a $AP2_DATA/c\necho ")"'], deny=True)

    def test_a_parameter_expansion_of_the_data_folder_is_a_mention(self):
        self.check(["rm -rf \"${AP2_DATA:?}/items\"", "rm -rf \"${AP2_DATA:?}\"/items",
                    "cp a \"${AP2_DATA%/}/config.yaml\"", "echo x > \"${AP2_RUN_DIR%/*}/C2/x\"",
                    # Accepted false deny: a brace after the run directory is never trusted.
                    "echo x > ${AP2_RUN_DIR:?}/x", "echo x > ${AP2_RUN_DIR[0]}/x",
                    'rm -rf "${AP2_DATA?}/items"', 'rm -rf "${AP2_DATA-}/items"', 'cp a "${AP2_DATA#}/config.yaml"',
                    'rm -rf "${AP2_DATA//x/x}/items"',
                    # An array subscript of the variable is the variable too.
                    'rm -rf "${AP2_DATA[0]}/victim"', "rm -rf ${AP2_DATA[@]}", 'rm -rf "${AP2_DATA[*]}/x"',
                    'echo pwned > "${AP2_RUN_DIR[0]}/../../victim/f"',
                    # `${HOME` with an operator before the data folder's path under HOME is the data folder too.
                    "cp src ${HOME:?}/Documents/Assistant/config.yaml", "cp src ${HOME%/}/Documents/Assistant/c",
                    'cp src "${HOME:-/x}/Documents/Assistant/config.yaml"',
                    "echo x > ${HOME:?}/Documents/Assistant/sessions/C1/x.txt",
                    # Each operator bash or zsh takes after the name.
                    "cp src ${HOME-}/Documents/Assistant/config.yaml", "cp src ${HOME?}/Documents/Assistant/config.yaml",
                    "cp src ${HOME#}/Documents/Assistant/config.yaml", "cp src ${HOME/x/x}/Documents/Assistant/c",
                    "cp src ${HOME+$HOME}/Documents/Assistant/config.yaml",
                    "cp src ${HOME=}/Documents/Assistant/config.yaml",
                    "cp src ${HOME[1,-1]}/Documents/Assistant/config.yaml",
                    "cp src ${HOME:-$HOME}/Documents/Assistant/config.yaml",
                    "cp src ${HOME^}/Documents/Assistant/config.yaml", "cp src ${HOME,}/Documents/Assistant/c"],
                   deny=True)
        # The package's path under HOME, by a `${HOME` expansion.
        home = str(Path(PKG).parent)
        self.check([f"cp src ${{HOME:?}}/{Path(PKG).name}/lib/assistant/guard.py"], deny=True,
                   env=dict(WORKER, HOME=home))
        self.check(["echo ${AP2_DATA_OLD:-x} > /tmp/x", "cp src ${HOME:?}/Documents/Assistant2/x",
                    "cp src ${HOME:?}/x"], deny=False)

    def test_reads_and_writes_elsewhere_are_allowed(self):
        self.check([f"cat {DATA}/config.yaml",
                    f"ls {DATA} 2>&1",
                    f"ls -la {DATA} >/dev/null 2>&1",
                    f"rg -n x {DATA}/items &>/dev/null",
                    f"{PKG}/bin/ap2 card add --title x",
                    f"\"{PKG}/bin/ap2\" worker park C1 implement --note 'x'",
                    "ap2 card list > /tmp/cards.txt",
                    "echo x > /tmp/y",
                    f"cat {DATA}/config.yaml | wc -l && echo done > /tmp/z",
                    "git status",
                    ""], deny=False)

    def test_a_path_that_only_starts_like_the_data_folder_is_not_it(self):
        self.check([f"echo x > {DATA}2/config.yaml", f"cp a {DATA}-old/x", "rm -rf $AP2_DATA_OLD/items"],
                   deny=False)

    def test_the_workers_own_run_directory_stays_writable(self):
        self.check(["cp shot.png $AP2_RUN_DIR/media/",
                    "cp shot.png ${AP2_RUN_DIR}/media/shot.png",
                    f"echo {{}} > {RUN}/question.json",
                    f"echo {{}} > '{RUN}/handback.json'",
                    f'cp "my shot.png" "{RUN}/media/my shot.png"',
                    'cp a.png "$AP2_RUN_DIR"/media/',
                    f"mkdir -p {RUN}/media && cp a.png {RUN}/media/",
                    "echo x > $AP2_DATA/sessions/C1/journal.md",
                    "echo x > ~/Documents/Assistant/sessions/C1/journal.md",
                    f"echo x > {RUN}",
                    # A quoted `{`, `}` or `,` is text: only an unquoted `{` (not that of `${`) brace-expands.
                    'echo \'{"text": "Is $AP2_RUN_DIR/media/a.png right?"}\' > "$AP2_RUN_DIR/question.json"',
                    f'echo \'{{"status": "done", "evidence": ["{RUN}/media/shot.png"]}}\' '
                    '> "$AP2_RUN_DIR/handback.json"',
                    f'jq -n \'{{evidence: ["{RUN}/media/a.png"]}}\' > "{RUN}/handback.json"',
                    f"python3 - <<'PY'\nwith open(\"{RUN}/handback.json\", \"w\") as f:\n"
                    "    json.dump({\"status\": \"done\"}, f)\nPY",
                    f"python3 -c 'import json; json.dump({{}}, open(\"{RUN}/handback.json\", \"w\"))'",
                    'echo "- screenshot: $AP2_RUN_DIR/media/a.png, attached" >> "$AP2_RUN_DIR/journal.md"',
                    "touch $AP2_RUN_DIR/a,b.txt", 'echo "${x:-a,b}" > $AP2_RUN_DIR/x',
                    "touch \\{$AP2_RUN_DIR/a,b}"], deny=False)
        # Accepted false denies: the unquoted `{` is looked for per segment (`{}` here), and a heredoc body line is
        # read as shell code, so an unquoted `{` there is one too (fail-closed, whatever the delimiter).
        self.check(["echo {} > $AP2_RUN_DIR/a,b", f"python3 - <<'PY'\njson.dump({{}}, open(\"{RUN}/x\", \"w\"))\nPY"],
                   deny=True)

    def test_outside_the_own_run_directory_is_still_denied(self):
        self.check(["cp x $AP2_RUN_DIR/../other/",
                    f"cp x {RUN}/../C2/handback.json",
                    f"cp x {RUN}/media/../../C2/x",
                    f"echo x > {DATA}/sessions/C2/handback.json",
                    f"echo x > {RUN}2/handback.json",
                    f"cp {RUN}/a.png {DATA}/items/x",  # one mention is the run dir, the other is not
                    "rm -rf $AP2_RUN_DIR $AP2_DATA/items",
                    # Quotes and backslashes do not end the path: bash reads each of these as one word.
                    'cp x "$AP2_RUN_DIR"/../C2/x',
                    "cp x '$AP2_RUN_DIR'/'..'/C2/x",
                    'cp x "$AP2_RUN_DIR/my dir/../../C2/x"',
                    "cp x $AP2_RUN_DIR/my\\ dir/../../C2/x",
                    "cp x ${AP2_RUN_DIR}/\\.\\./C2/x",
                    'echo x > "$AP2_"DATA/config.yaml',
                    f"echo x >'{DATA[:5]}'{DATA[5:]}/config.yaml",
                    # Mentions match case-insensitively (macOS folders are).
                    f"echo x > {DATA.lower()}/config.yaml", f"echo x > {RUN.upper()}/../C2/x",
                    # Best effort: it cannot tell which path a command writes, so a copy out of the data folder
                    # is write-shaped too (`cat` it instead, or use Read).
                    f"cp {DATA}/config.yaml {RUN}/config-copy.yaml",
                    f"cp {DATA}/config.yaml /tmp/backup.yaml"], deny=True)

    def test_a_brace_or_a_longer_name_after_the_run_directory_is_not_it(self):
        """A brace that opens before the mention can still make `..` (`{$AP2_RUN_DIR/..,x}`), and bash ends a
        `$NAME` at the first character that is not a name character: `$AP2_RUN_DIR.bak` is another folder."""
        self.check(["touch {$AP2_RUN_DIR/..,x}/c", "touch {x,$AP2_RUN_DIR/..}/c", 'touch {"$AP2_RUN_DIR"/..,x}/c',
                    f"touch {{{RUN}/..,x}}/c", "touch {$AP2_RUN_DIR/settings-implement.json,x}",
                    "touch $AP2_RUN_DIR/{a,..}/x",
                    "touch $AP2_RUN_DIR.bak", "cp -r $AP2_RUN_DIR $AP2_RUN_DIR.old", "touch $AP2_RUN_DIR-old",
                    "touch ${AP2_RUN_DIR}x", "echo x > ${AP2_RUN_DIR}0/handback.json",
                    # Accepted false deny: `$AP2_DATA.bak` is another folder, but a `.` after the name is a mention.
                    "touch $AP2_DATA.bak"], deny=True)
        self.check(["touch $AP2_RUN_DIRX/x", "touch $AP2_DATA_OLD/x", "touch ${AP2_RUN_DIR}/x.bak"], deny=False)

    def test_extglob_turned_on_with_a_guarded_path_is_denied(self):
        """With extglob on, `@(...)` and the like are one word that bash globs (it can match `..` or a floor file),
        with no comment, split or word break inside: a command that turns it on and names a guarded path is denied,
        and a run-directory path never holds an unquoted extglob."""
        on = "shopt -s extglob\n"
        self.check([on + "cp a $AP2_RUN_DIR/.@(.)/../config.yaml", on + "cp a $AP2_RUN_DIR/.+(.)/../config.yaml",
                    on + "cp a $AP2_RUN_DIR/.!(x)/../config.yaml", on + 'cp a "$AP2_RUN_DIR"/.@(.)/../config.yaml',
                    on + f"cp a {RUN}/.@(.)/../config.yaml", on + "echo hi > $AP2_RUN_DIR/.@(.)/../config.yaml",
                    on + "cp a $AP2_RUN_DIR/settings-implement.js@(on)",
                    on + "cp a $AP2_RUN_DIR/@(settings-implement.json)", on + "echo @(#) > $AP2_DATA/config.yaml",
                    on + "touch @(#) $AP2_DATA/x2", on + "cp @(a|zz) $AP2_DATA/config.yaml",
                    "bash -O extglob -c 'cp a $AP2_RUN_DIR/.@(.)/../config.yaml'",
                    "shopt -s ext\\glob\ncp a $AP2_RUN_DIR/.@(.)/../config.yaml",
                    "shopt -s ext'gl'ob\ncp a $AP2_RUN_DIR/.@(.)/../config.yaml",
                    # The option name built at run time: the run-directory rule still refuses an unquoted extglob.
                    "o=ext; shopt -s ${o}glob\ncp a $AP2_RUN_DIR/.@(.)/../config.yaml",
                    "o=ext; shopt -s ${o}glob\ncp a $AP2_RUN_DIR/*(settings-implement.json)",
                    "o=ext; shopt -s ${o}glob\ncp a $AP2_RUN_DIR/x/?(.)./../../config.yaml"], deny=True)
        self.check([on + "ls /tmp/@(a|b) > /tmp/x", "echo $((2*(3))) > $AP2_RUN_DIR/n.txt",
                    "python3 -c 'print(2*(3)+(1))' > $AP2_RUN_DIR/n.txt",
                    f"python3 -c 'open(\"{RUN}/n.txt\", \"w\").write(str(2*(3)+(1)))'"], deny=False)
        self.assertEqual(guard._words("cp a $X/.@(. |x)/../c b"), ["cp", "a", "$X/.@(. |x)/../c", "b"])
        self.assertEqual(guard._words("a@(b(c)d)e f"), ["a@(b(c)d)e", "f"])

    def test_an_extglob_opener_with_a_guarded_path_is_denied_whatever_turns_it_on(self):
        """An unquoted `@(`, `+(`, `!(`, `?(` or `*(` outside arithmetic, in a command that names a guarded path, is
        denied: extglob may be on (`shopt -s ${o}glob`), and then `#` and `|` in there are no comment or pipe."""
        on, rm = "o=ext; shopt -s ${o}glob\n", "r" + "m"
        self.check([on + "echo @(#) > $AP2_DATA/config.yaml", on + "cp @(a|zz) $AP2_DATA/config.yaml",
                    on + "touch @(#) $AP2_DATA/x2",
                    on + rm + " $AP2_RUN_DIR/!(handback.json)", on + "cp a $AP2_RUN_DIR/+(settings-implement.json)",
                    on + "cp a $AP2_RUN_DIR/@(settings-implement.json)",
                    on + "cp a $AP2_RUN_DIR/?(settings-implement.json)",
                    # The literal option name, spelled so only the quote and continuation stripping finds it.
                    "shopt -s ext$'g'lob\ncp @(a|zz) $AP2_DATA/config.yaml",
                    "shopt -s ext\\\nglob\necho @(#) > $AP2_DATA/config.yaml",
                    "shopt -s extglob\ncp @(a|zz) $AP2_\\\nDATA/config.yaml"], deny=True)
        self.check([on + "ls /tmp/@(a|b) > /tmp/x", "echo $((2*(3))) > $AP2_RUN_DIR/n.txt",
                    "echo $((2*(3+1)*(2))) > $AP2_RUN_DIR/n.txt", "echo '@(x)' > $AP2_RUN_DIR/n.txt",
                    'echo "a @(x)" > $AP2_RUN_DIR/n.txt', "echo a\\@\\(x\\) > $AP2_RUN_DIR/n.txt"], deny=False)
        # `_extglob` reads the option name through quotes and line continuations.
        for cmd in ("shopt -s ext$'g'lob", "shopt -s ext\\\nglob", "shopt -s ext'gl'ob", "shopt -s ext\\glob"):
            with self.subTest(cmd=cmd):
                self.assertTrue(guard._extglob(cmd))
        # `_segments` marks each opener read outside quotes and arithmetic.
        for opener in "@+!?*":
            with self.subTest(opener=opener):
                seen = set()
                guard._segments(f"ls x{opener}(a|b)", seen=seen)
                self.assertIn("@", seen)
        for cmd in ("echo $((2*(3)))", "((x=2*(3)))", "echo '@(a)' \"*(b)\"", "echo \\@(", 'echo "${x:-@(a)}"'):
            with self.subTest(cmd=cmd):
                seen = set()
                guard._segments(cmd, seen=seen)
                self.assertNotIn("@", seen)
        # The run-directory rule refuses a `(` after the run directory when the segment has an unquoted one.
        marks = guard._Marks(WORKER)
        for opener in "@+!?*":
            word = f"$ap2_run_dir/{opener}(settings-implement.json)"
            flags = set()
            self.assertEqual(guard._words(f"cp a {word}", flags=flags), ["cp", "a", word])
            self.assertIn("(", flags)
            self.assertFalse(marks._own_run(word, True, True))
        self.assertTrue(marks._own_run("$ap2_run_dir/@(x)", True, False))  # with no unquoted `(`, it is text

    def test_zsh_forms_that_write_are_read_too(self):
        """Claude Code runs a worker's Bash call in zsh on this Mac: `=(...)` runs a command, `>!` is a noclobber
        redirect, `{ ... } always { ... }` runs its second block, and `(...)` in a word is a glob group."""
        self.check(["ap2 card list =(cp src $AP2_DATA/config.yaml)",
                    f"{PKG}/bin/ap2 card list =(cp src $AP2_DATA/config.yaml)",
                    "ap2 card list >! $AP2_DATA/config.yaml", "ap2 card list 2>! $AP2_DATA/config.yaml",
                    "ap2 card list >>! $AP2_DATA/config.yaml", "ap2 card list &>! $AP2_DATA/config.yaml",
                    "ap2 card list >!$AP2_DATA/config.yaml", "echo x >! $AP2_DATA/config.yaml",
                    "{ ap2 card list } always { cp src $AP2_DATA/config.yaml }",
                    "cp src $AP2_RUN_DIR/(settings-implement.json)", "cp src $AP2_RUN_DIR/settings-implement(.json)",
                    'cp src "$AP2_RUN_DIR"/(settings-implement.json)',
                    # zsh reads a `}` that ends a word as a closing brace (even with no `{` open).
                    "{# c\necho x >! $AP2_RUN_DIR/settings-implement.json}",
                    "echo y > $AP2_RUN_DIR/settings-implement.json}\\\n", "{ cp src $AP2_RUN_DIR/x/..}"], deny=True)
        self.check(["ap2 card note C1 --text 'a =(b)' --data $AP2_DATA", "ap2 card note C1 --text always > /tmp/x",
                    "ap2 card list >! /tmp/x", 'echo "always (x)" > $AP2_RUN_DIR/n.txt',
                    "echo x > ${AP2_RUN_DIR}", "echo x > ${AP2_RUN_DIR}/a.txt}"], deny=False)
        targets = []
        guard._words("a >! x >>! y 2>! z", targets)
        self.assertEqual(targets, ["x", "y", "z"])

    def test_a_brace_inside_a_quoted_parameter_expansion_is_a_brace(self):
        """bash 3.2 ends the quote at the `"` inside `"${x:-"..."}"`, so a `{` in there brace-expands
        (`x=; echo "${x:-"{a,b}"}"` prints `a b`)."""
        self.check(['echo x | tee "${x:-"$AP2_RUN_DIR/{../..,o}/config.yaml"}"',
                    'touch "${x:-"$AP2_RUN_DIR/{../..,o}/config.yaml"}"',
                    'cp src "${x:-"$AP2_RUN_DIR/"{settings-implement.json,o}}"',
                    "bash -c 'echo x | tee \"${x:-\"$AP2_RUN_DIR/{../..,o}/config.yaml\"}\"'",
                    # A shell run on a quoted script reads its braces and groups unquoted.
                    "bash -c 'touch {$AP2_RUN_DIR/..,x}/c'", "sh -c 'touch $AP2_RUN_DIR/{..,x}/c'",
                    "zsh -c 'cp src $AP2_RUN_DIR/(settings-implement.json)'",
                    "eval 'touch $AP2_RUN_DIR/{..,x}/c'",
                    # The script a shell or `eval` runs is read as a command of its own: its words, line
                    # continuations and final backslash as that shell reads them.
                    "bash -c 'tee $AP2_RUN_DIR/settings-implement.json < src'",
                    "sh -c 'mv src2 $AP2_RUN_DIR/\\.\\./\\.\\./config.yaml'",
                    "bash -c 'touch $AP2_\\\nDATA/config.yaml'",
                    "eval 'touch $AP2_RUN_DIR/settings-implement.json\\'",
                    "zsh -c 'echo x >! $AP2_\\\nDATA/config.yaml'",
                    "eval touch '$AP2_RUN_DIR/settings-implement.json'", "eval touch $AP2_RUN_DIR/\\'..\\'/c",
                    "bash -c \"sh -c 'tee $AP2_RUN_DIR/settings-implement.json < src'\"",
                    # zsh's `=sh` is the path of `sh`; `$@` is empty: still a shell's name.
                    "=sh -c 'echo x > $AP2_RUN_DIR/\\.\\./\\.\\./config.yaml'",
                    "$@eval 'echo x >! $AP2_RUN_DIR/settings-implement.json\\'",
                    "${x}bash -c 'tee $AP2_RUN_DIR/settings-implement.json < src'",
                    "bash -c \"sh -c \\\"zsh -c 'tee $AP2_RUN_DIR/settings-implement.json < src'\\\"\"",
                    # Each shell by name or by path, its script hiding a line continuation.
                    "ksh -c 'cp src $AP2_\\\nDATA/config.yaml'", "dash -c 'cp src $AP2_\\\nDATA/config.yaml'",
                    "/bin/sh -c 'cp src $AP2_\\\nDATA/config.yaml'", "/bin/bash -c 'cp src $AP2_\\\nDATA/config.yaml'",
                    # A third shell level is past MAX_DEPTH: it is a write when it names a guarded path.
                    "bash -c \"sh -c \\\"zsh -c 'echo hi > \\$AP2_RUN_DIR/x'\\\"\""], deny=True)
        self.check(['echo "${x:-a,b}" > $AP2_RUN_DIR/x', "cp ${f} $AP2_RUN_DIR/a,b.txt",
                    "bash -c 'echo hi > $AP2_RUN_DIR/x'", "bash -c 'ap2 card list --data $AP2_DATA'",
                    "bash -c \"sh -c 'echo hi > $AP2_RUN_DIR/x'\"",
                    "ssh host 'cat x' > $AP2_RUN_DIR/c.txt; ls $AP2_DATA"], deny=False)

    def test_a_floor_glob_with_brackets_at_the_top_level_is_denied(self):
        """Python's fnmatch reads `[^x]` and `[[:lower:]]` otherwise than bash: at the run directory's top level a
        part with a `[` is refused (no floor-file write needs one)."""
        self.check(["cp src $AP2_RUN_DIR/[^x]ettings-implement.json",
                    "cp src $AP2_RUN_DIR/[[:lower:]]ettings-implement.json", "cp src $AP2_RUN_DIR/[[:alpha:]]*",
                    "cp src $AP2_RUN_DIR/[s]ettings-implement.json", "cp src $AP2_RUN_DIR/x[ab].png"], deny=True)
        self.check(["cp a $AP2_RUN_DIR/media/[ab].png", "rm -f $AP2_RUN_DIR/media/[!x]*.png"], deny=False)

    def test_a_quoted_glob_bracket_brace_or_paren_after_the_run_directory_is_text(self):
        """Bash and zsh glob and brace-expand no quoted character, so JSON or code in quotes after a run-directory
        path is text; `..`, a `$` and a `settings-*.json` name still count however they are quoted."""
        self.check([f"python3 -c \"import json; d=json.load(open('{RUN}/handback.json')); d['status']='done'; "
                    f"json.dump(d, open('{RUN}/handback.json','w'))\"",
                    f"python3 -c \"open('{RUN}/out.txt','w').write(str([1,2]))\"",
                    f"echo '{{\"status\": \"done\", \"evidence\": [\"{RUN}/test.txt\"], \"notes\": [\"x\"]}}' "
                    f"> {RUN}/handback.json",
                    'echo "- tests: $AP2_RUN_DIR/test.txt [pass]" >> $AP2_RUN_DIR/journal.md',
                    f'echo "- shot: {RUN}/home.png; PR: [#12](https://example.com/pull/12)" >> {RUN}/journal.md',
                    f"echo '{{\"text\": \"Keep {RUN}/a.png as the hero [yes or no]?\"}}' > {RUN}/question.json",
                    "bash -c \"python3 -c \\\"open('$AP2_RUN_DIR/x','w').write('[1]')\\\"\"",
                    f". venv/bin/activate && python -c \"import json; "
                    f"print(json.load(open('{RUN}/handback.json'))['status'])\"",
                    "cp src $AP2_RUN_DIR/x\\[ab\\].png", 'echo x > "$AP2_RUN_DIR/a $AP2_RUN_DIR/*"'], deny=False)
        self.check(["cp src $AP2_RUN_DIR/[^x]ettings-implement.json",
                    "cp src $AP2_RUN_DIR/[[:lower:]]ettings-implement.json", "cp src $AP2_RUN_DIR/[[:alpha:]]*",
                    "touch {$AP2_RUN_DIR/..,x}/c", "cp src $AP2_RUN_DIR/(settings-implement.json)",
                    'cp x "$AP2_RUN_DIR/../c"', 'cp x "$AP2_RUN_DIR/settings-implement.json"',
                    'cp x "$AP2_RUN_DIR/settings-*.json"', 'cp x "$AP2_RUN_DIR"/[s]"ettings-implement.json"',
                    'cp x "$AP2_RUN_DIR/$f"', 'echo y > "$AP2_RUN_DIR/x"/..', "cp x $AP2_RUN_DIR/\\.\\./c"], deny=True)
        word = guard._words("echo '[a]'b\\*\"{,}\"(")[1]
        self.assertEqual((word, word.mask), ("[a]b*{,}(", "\x00a\x00b\x00\x00\x00\x00("))
        self.assertIsNone(guard._words("a b")[1].mask)

    def test_a_quote_inside_a_quoted_expansion_or_arithmetic_hides_no_substitution(self):
        """Inside `"${...}"`, `$[...]`, `$((...))` and `((...))` bash keeps a `'` only to find the end: a `$(...)` or
        backtick in there runs."""
        self.check([f"ap2 card list \"${{x:-'$(cp src {DATA}/config.yaml)'}}\"",
                    f"ap2 card list \"${{x:-'`cp src {DATA}/config.yaml`'}}\"",
                    f"ap2 card list $[ '$(cp src {DATA}/config.yaml)' ]",
                    "echo \"${x:-'$(cp src $AP2_RUN_DIR/settings-implement.json)'}\"",
                    "echo $(( '$(cp src $AP2_RUN_DIR/settings-implement.json)' ))",
                    "(( '$(cp src $AP2_RUN_DIR/settings-implement.json)' ))",
                    "echo \"$(( '$(cp src $AP2_RUN_DIR/settings-implement.json)' ))\"",
                    "echo $[ '`cp src $AP2_RUN_DIR/settings-implement.json`' ]",
                    # A backslash before a `'` there escapes nothing (bash closes the quote at it).
                    "echo \"${x:-'a\\'$(cp src $AP2_RUN_DIR/settings-implement.json)}\"",
                    # bash 3.2 can drop a backslash level in a backtick there: `\\*` globs the floor file.
                    ": \"${x:-'}\"; `cp src $AP2_RUN_DIR/\\*`; : \"'}\"", ': "${x:-"`cp src $AP2_RUN_DIR/\\*`"}"',
                    ': "${x:-a"`cp src $AP2_RUN_DIR/\\*`"}"'], deny=True)
        self.check(["echo \"${x:-'\\$(cp src $AP2_RUN_DIR/settings-implement.json)'}\" > /tmp/x",
                    "echo \"${x:-'}'}\" > $AP2_RUN_DIR/a.txt", "ap2 card note C1 --data $AP2_DATA --text \"${x:-'a'}\""],
                   deny=False)
        self.assertEqual(guard._segments("a \"${x:-'$(b; c)'}\" d"), ["b", " c", "a \"${x:-'$(_)'}\" d"])

    def test_zsh_reads_a_quote_inside_a_quoted_expansion_as_a_plain_character(self):
        """zsh closes `"${x:-'}"` at its first `}`: the command is also judged in that reading."""
        self.check([f"ap2 card list \"${{x:-'}}\"; cp src {DATA}/config.yaml; : \"'}}\"",
                    "echo \"${x:-'}\"; cp src $AP2_RUN_DIR/settings-implement.json; : \"'}\""], deny=True)
        self.assertEqual(guard._segments("a \"${x:-'}\"; b; \"'}\"", sq=False), ["a \"${x:-'}\"", " b", " \"'}\""])

    def test_zsh_flags_and_option_changes_with_a_guarded_path_are_denied(self):
        """zsh's `${(e)...}` runs the value as code, and `setopt`, `unsetopt`, `emulate`, `options[...]` and
        `set -o` can turn on glob qualifiers (`/(e:'cmd':)`): with a guarded path in the command, they deny."""
        self.check([f"ap2 card list \"${{(e)x:-\\$(cp src {DATA}/config.yaml)}}\"",
                    f"ap2 card list \"${{(e):-\\$(cp src {DATA}/config.yaml)}}\"",
                    "echo \"${(e):-\\$(cp src $AP2_RUN_DIR/settings-implement.json)}\"",
                    "setopt bareglobqual; ap2 card list /(e:'cp src $AP2_DATA/config.yaml':)",
                    "setopt extendedglob; ap2 card list /(#qe:'cp src $AP2_DATA/config.yaml':)",
                    "unsetopt no_bare_glob_qual; ap2 card list /(e:'cp src $AP2_DATA/config.yaml':)",
                    "set -o bareglobqual; ap2 card list /(e:'cp src $AP2_DATA/config.yaml':)",
                    "set +o nobareglobqual; ap2 card list /(e:'cp src $AP2_DATA/config.yaml':)",
                    "set -eo extendedglob; ap2 card list /(#qe:'cp src $AP2_DATA/config.yaml':)",
                    "options[bareglobqual]=on; ap2 card list /(e:'cp src $AP2_DATA/config.yaml':)",
                    "options=(bareglobqual on); ap2 card list /(e:'cp src $AP2_DATA/config.yaml':)",
                    "set -euo pipefail; set -o bareglobqual; ap2 card list /(e:'cp src $AP2_DATA/config.yaml':)",
                    "emulate ksh; ap2 card list --data $AP2_DATA", "s\\etopt x; ap2 card list --data $AP2_DATA",
                    # A `${(` with a line continuation in it, or in an unquoted heredoc body (zsh expands it).
                    f"ap2 card list \"${{\\\n(e)x:-\\$(cp src {DATA}/config.yaml)}}\"",
                    f"cat > /tmp/x <<EOF\n${{(e)x}}\nEOF\nls {DATA}",
                    # The zsh reading of a `'` inside `"${...}"`.
                    "ap2 card list \"a${x:-'}\"; cp src $AP2_DATA/config.yaml; : \"'}\"",
                    # A fresh zsh has bare glob qualifiers on: its script with a `(` and a guarded path is a write.
                    "zsh -c \"ap2 card list /(e:'c\\\"\\\"p src \\$AP2_DATA/config.yaml':)\"",
                    "/bin/zsh -c \"ap2 card list /(e:'cp src \\$AP2_DATA/config.yaml':)\"",
                    "echo \"ap2 card list /(e:'cp src \\$AP2_DATA/config.yaml':)\" | zsh",
                    # Accepted false deny: the words are looked for in the text, quoted or not, as `extglob` is.
                    "echo 'setopt' > $AP2_RUN_DIR/n.txt"], deny=True)
        self.check(["set -euo pipefail; echo x >> $AP2_RUN_DIR/journal.md", "set -x; ap2 card list --data $AP2_DATA",
                    "set +e; set -o errexit -o nounset; ls $AP2_DATA",
                    "git config set x; ls $AP2_DATA",
                    # `set` flags that change no word's reading.
                    "set -Eeuo pipefail; make test > $AP2_RUN_DIR/test.txt 2>&1",
                    f"set -Eeuo pipefail; {PKG}/bin/ap2 card show C1", f"set -Eeuo pipefail; cat {DATA}/config.yaml",
                    "set -o noclobber; echo hi > $AP2_RUN_DIR/x.txt", "set -C; echo hi > $AP2_RUN_DIR/x.txt",
                    "set -o errtrace; echo hi > $AP2_RUN_DIR/x.txt", "set -E; trap 'echo err' ERR; make > $AP2_RUN_DIR/m.log",
                    "set -T -o functrace -o allexport; echo hi > $AP2_RUN_DIR/x.txt",
                    # A `${(` in single quotes or in a quoted-delimiter heredoc body is text (JavaScript here).
                    "node -e 'const ms=1234; console.log(`took ${(ms/1000).toFixed(2)}s`)' > \"$AP2_RUN_DIR/timing.txt\"",
                    "cat > \"$AP2_RUN_DIR/r.js\" <<'EOF'\nconsole.log(`${(p*100).toFixed(1)}%`)\nEOF",
                    "cat > \"$AP2_RUN_DIR/r.js\" <<\\EOF\nconst s = `${(p*100).toFixed(1)}%`;\nEOF",
                    "zsh -c 'echo hi > $AP2_RUN_DIR/x'"], deny=False)
        for cmd in ("setopt x", "set -o x", "set -G", "options+=(x on)", "options=(x on)", "set -o extendedglob"):
            with self.subTest(cmd=cmd):
                self.assertTrue(guard._options(cmd))
        for cmd in ("set -euo pipefail", "set -- a b", "reset -o x", "echo set", "offset -x", "set -Eeuo pipefail",
                    "set -aCT", "set -o noclobber -o errtrace -o allexport -o functrace"):
            with self.subTest(cmd=cmd):
                self.assertFalse(guard._options(cmd))
        # zsh's `${(` counts outside single quotes and quoted-delimiter heredoc bodies only.
        def flag_group(cmd):
            return any(guard._flag_group(guard._Parsed(seg, True, True)) for seg in guard._joined(guard._segments(cmd)))
        for cmd in ("a=${(e)b}", 'echo "${(e)b}"', "echo ${\\\n(e)b}", "cat <<E\n${(e)x}\nE", "cat <<E\n${(e)x}\\\nE"):
            with self.subTest(cmd=cmd):
                self.assertTrue(flag_group(cmd))
        for cmd in ("echo '${(e)b}'", "echo $'${(e)b}'", "cat <<'E'\n${(e)x}\nE", 'cat <<"E"\n${(e)x}\nE',
                    "cat <<\\E\n${(e)x}\nE", "echo ${x}(y)"):
            with self.subTest(cmd=cmd):
                self.assertFalse(flag_group(cmd))

    def test_a_zsh_ampersand_noclobber_redirect_is_a_write_not_a_pipe(self):
        """zsh's `>&|`, `>>&|`, `>&!` and `>>&!` redirect both outputs into the next word."""
        self.check(["echo x >&| $AP2_DATA/config.yaml", "echo x >>&| $AP2_DATA/config.yaml",
                    "ap2 card list >&! $AP2_DATA/config.yaml", "ap2 card list >>&| $AP2_DATA/config.yaml",
                    "ap2 card list >&|$AP2_DATA/config.yaml"], deny=True)
        self.check(["ap2 card list >&| /tmp/x --data $AP2_DATA", "echo x \\>&| cat"], deny=False)
        self.assertEqual(guard._segments("a >&| f | b"), ["a >&| f ", " b"])
        targets = []
        guard._words("a >&| x >>&! y", targets)
        self.assertEqual(targets, ["x", "y"])

    def test_a_script_piped_into_a_shell_is_read_as_a_script(self):
        """A shell with no `-c` reads its script from its input: the words piped into it are read as one."""
        self.check(["echo 'tee $AP2_RUN_DIR/settings-implement.json < f' | sh",
                    "printf '%s\\n' 'cp src $AP2_\\\nDATA/config.yaml' | bash -e",
                    "echo 'cp src $AP2_\\\nDATA/config.yaml' | tr a a | /bin/zsh",
                    # `|&` pipes too, and a shell after a runner still reads its input.
                    "printf '%s\\n' 'cp src $AP2_\\\nDATA/config.yaml' |& sh",
                    "printf '%s\\n' 'cp src $AP2_\\\nDATA/config.yaml' | env sh",
                    "printf '%s\\n' 'cp src $AP2_\\\nDATA/config.yaml' | command bash",
                    # zsh closes a brace list at a word-final `}`: `sh}` is `sh`.
                    "{ printf '%s\\n' 'cp src $AP2_RUN_DIR/*.json' | sh}",
                    "{ echo 'cp src $AP2_RUN_DIR/settings-*' | zsh}",
                    # Words piped into a zsh: its script reads `(` as a glob qualifier.
                    "echo 'c=cp; ls /(e:\"$c src $AP2_DATA/config.yaml\":)' | zsh"], deny=True)
        self.check(["cat $AP2_RUN_DIR/x | sh -c 'wc -l'", "echo hi | bash", "cat $AP2_RUN_DIR/x | sh",
                    "echo 'cp src $AP2_\\\nDATA/config.yaml' || sh", "echo 'cp src $AP2_\\\nDATA/c' | eval wc"],
                   deny=False)
        self.assertIs(type(guard._segments("a | b")[0]), guard._Pipe)
        self.assertIs(type(guard._segments("a || b")[0]), str)

    def test_a_shell_script_that_writes_only_the_run_directory_is_judged_on_its_own(self):
        """A shell's script (or the words piped into a shell) that writes nothing guarded once read as a command of
        its own is left out of the outer segment's reading, where one word held its run-directory path and a later
        `$?`, `$f` or `$(date)`; the outer segment's redirects still count."""
        self.check(["bash -c 'make > \"$AP2_RUN_DIR/m.log\"; echo \"exit $?\"'",
                    "bash -c 'cp a.png \"$AP2_RUN_DIR/media/\"; echo $?'",
                    "sh -c 'echo started >> \"$AP2_RUN_DIR/journal.md\"; make; echo \"done $(date)\" >> "
                    "\"$AP2_RUN_DIR/journal.md\"'",
                    "bash -c 'for f in \"$AP2_RUN_DIR\"/media/*.png; do echo \"$f\"; done' > $AP2_RUN_DIR/list.txt",
                    "echo 'make > \"$AP2_RUN_DIR/m.log\"; echo \"exit $?\"' | sh",
                    "echo 'cp a.png \"$AP2_RUN_DIR/media/\"; echo $?' | bash",
                    "printf '%s\\n' 'echo \"done $(date)\" >> \"$AP2_RUN_DIR/journal.md\"' | sh",
                    # The zsh reading (a `'` and a `${`) and the budget: a `bash -c` heredoc into the run directory.
                    "bash -c \"echo 'it is ${HOME}' > $AP2_RUN_DIR/x.txt\"",
                    "bash -c \"cat > $AP2_RUN_DIR/notes.md <<'EOF'\n" + "it's ${HOME} and more text here\n" * 480
                    + "EOF\""], deny=False)
        self.check(["cp bash $AP2_DATA/x", "bash -c 'cp a $AP2_DATA/c'", "bash -c 'echo hi' > $AP2_DATA/c",
                    "bash -c 'make > \"$AP2_RUN_DIR/m.log\"' > \"$AP2_RUN_DIR/$f\"",
                    "export PATH=/tmp/b:$PATH; bash -c 'ap2 src $AP2_DATA/config.yaml'",
                    "echo 'echo hi' > $AP2_DATA/c | sh", "cp a $AP2_DATA/c | sh"], deny=True)
        self.assertGreater(len("bash -c \"cat > $AP2_RUN_DIR/notes.md <<'EOF'\n"
                               + "it's ${HOME} and more text here\n" * 480 + "EOF\""), 15 << 10)

    def test_a_shells_positional_arguments_stay_in_the_outer_reading(self):
        """Only the script word of a `-c` shell leaves the outer reading: the words after it are the script's `$0`,
        `$1`, ... (a quoted `#`, `;` or blank there hides nothing from the script, which reads them as values), so
        the outer verb with an unsafe argument is a write."""
        rm = "r" + "m"
        self.check(['sh -c \'cp "$1" "$2" && echo ok\' _ new.json "$AP2_RUN_DIR/settings-implement.json"',
                    'sh -c \'cp "$1" "$2" && echo ok\' _ a "$AP2_DATA/x"',
                    'bash -c \'cp "$1" "$2"; echo copied\' _ a "$AP2_DATA/x"',
                    'ls | xargs -I{} sh -c \'cp "$1" "$2" && echo ok\' _ {} "$AP2_DATA/x"',
                    'for f in *.png; do sh -c \'mv "$1" "$2"; echo moved\' _ "$f" "$AP2_DATA/media/"; done',
                    'sh -c \'cp "$1" "$2" || exit 1\' _ a "$AP2_DATA/x"',
                    'sh -c \'cp "$1" "$2"\necho ok\' _ a "$AP2_DATA/x"',
                    'sh -c \'cp "$1" "$2" && echo ok\' _ a ~/Documents/Assistant/x',
                    f'sh -c \'cp "$1" "$2" && echo ok\' _ a {PKG}/lib/x',
                    'zsh -c \'cp "$1" "$2" && echo ok\' _ a "$AP2_DATA/x"',
                    'sh -c \'tee "$1" >/dev/null && echo ok\' _ "$AP2_DATA/x" < a',
                    "bash -c 'cp \"$@\"' '#' src $AP2_DATA/config.yaml",
                    'bash -c \'cp "$1" "$2"\' _ src "$AP2_RUN_DIR/a b/../../../config.yaml"',
                    "while { bash -c true '#' } { cp src $AP2_DATA/config.yaml; break }",
                    "{ bash -c true '#' } always { cp src $AP2_DATA/config.yaml }",
                    # Flags and option values before or after the `-c`, and a verb the script quotes.
                    'bash -e -o pipefail -c \'cp "$1" "$2"; echo ok\' _ a "$AP2_DATA/x"',
                    'bash -c -o pipefail \'cp "$1" "$2"; echo ok\' _ a "$AP2_DATA/x"',
                    'sh -c \'c""p "$1" "$2"; echo ok\' _ a "$AP2_DATA/x"',
                    'sh -c \'c\\p "$@"; echo ok\' _ a "$AP2_DATA/x"',
                    f'sh -c \'{rm} -f "$1" && echo gone\' _ "$AP2_DATA/config.yaml"',
                    # A shell with no `-c` runs a script file: every word after it stays.
                    'sh -e install.sh "$AP2_DATA/x"'], deny=True)
        self.check(['sh -c \'cp "$1" "$2" && echo ok\' _ a "$AP2_RUN_DIR/media/a.png"',
                    'bash -o pipefail -c \'make > "$AP2_RUN_DIR/m.log"; echo "exit $?"\'',
                    'bash -ec \'cp a.png "$AP2_RUN_DIR/media/"; echo $?\'',
                    'sh -c \'cp "$1" "$2"\' _ "$AP2_RUN_DIR/a.png" "$AP2_RUN_DIR/media/b.png"'], deny=False)

    def test_only_an_echo_or_printf_piped_into_a_shell_leaves_the_outer_reading(self):
        """A segment piped into a shell runs before the shell reads its output: only an `echo` or a `printf` (they
        print their words and write nothing) leaves its words to the script reading; any other command keeps its
        own (`touch a '#' <data>/x | sh` touches the file whatever the script reading finds), and only a segment a
        `|` ends is fed."""
        self.check(['touch a \'#\' "$AP2_DATA/x" | sh', 'touch a \';\' "$AP2_DATA/x" | sh',
                    'echo x | tee \';\' "$AP2_DATA/config.yaml" | sh', 'echo x | tee \'#\' "$AP2_DATA/config.yaml" | sh',
                    'cp src "$AP2_RUN_DIR/a b/../../../config.yaml" | sh',
                    'cp src "$AP2_RUN_DIR/a b/../../../config.yaml"; echo ls | sh',
                    "echo 'ls' > \"$AP2_DATA/x\" | sh"], deny=True)
        self.check(["echo 'make > \"$AP2_RUN_DIR/m.log\"; echo \"exit $?\"' | sh",
                    "printf '%s\\n' 'echo \"done $(date)\" >> \"$AP2_RUN_DIR/journal.md\"' | sh",
                    "echo 'echo $? > \"$AP2_RUN_DIR/rc.txt\"' | bash"], deny=False)

    def test_a_quoted_or_escaped_verb_is_still_its_verb(self):
        """The shell removes quotes and backslashes before it runs a word: `c\\p` and `c''p` are `cp`."""
        rm = "r" + "m"
        self.check(["c\\p src $AP2_DATA/config.yaml", "c''p src $AP2_DATA/config.yaml",
                    "m\\v src $AP2_DATA/config.yaml", "t\\ee $AP2_DATA/config.yaml < src",
                    "c\\\np src $AP2_DATA/config.yaml", "sed '-i' '' 's/orig/NEW/' $AP2_DATA/config.yaml",
                    "c''p src $AP2_RUN_DIR/settings-implement.json", f"\\{rm} {DATA}/config.yaml",
                    "export PATH=/tmp/b:$PATH; c\\p src $AP2_RUN_DIR/$f"], deny=True)
        self.check(["c\\at $AP2_DATA/config.yaml", "'ls' $AP2_DATA"], deny=False)

    def test_sed_in_place_is_found_anywhere_after_sed(self):
        """`sed -e expr -i f` edits `f` in place too: `-i`, a flag cluster with an `i` (`-ni`) or `--in-place` after
        the `sed`, in its segment, is a write."""
        self.check(["sed -e 's/orig/NEW/' -i '' $AP2_DATA/config.yaml", f"sed -E -e 's/a/b/' -i.bak {DATA}/x",
                    f"sed --in-place 's/a/b/' {DATA}/x", f"sed --in-place=.bak 's/a/b/' {DATA}/x",
                    f"sed -ni 's/a/b/p' {DATA}/x", f"sed -n 's/a/b/p' {DATA}/x -i"], deny=True)
        self.check([f"sed -n 's/a/b/p' {DATA}/config.yaml", f"sed -e 's/a/b/' {DATA}/config.yaml | head",
                    f"sed --expression='s/i/x/' {DATA}/config.yaml"], deny=False)

    def test_a_zsh_short_loop_after_an_ap2_brace_list_is_no_ap2_call(self):
        """zsh reads a `}` that ends a word as the end of a `{ ... }` list, and runs the next `{ ... }` as the loop's
        body: `while { ap2 card list } { cmd }` runs `cmd`."""
        rm = "r" + "m"
        self.check(["while { ap2 card list } { cp src $AP2_DATA/config.yaml; break }",
                    "while { ap2 card list} { cp src $AP2_DATA/config.yaml; break}",
                    "while { ap2 card list } { tee $AP2_DATA/config.yaml < src; break }",
                    "while { ap2 card list } { " + rm + " $AP2_DATA/config.yaml; break }",
                    "while { ap2 card list } { cp src $AP2_RUN_DIR/settings-implement.json; break }",
                    "until { ap2 card list } { cp src $AP2_DATA/config.yaml; break }",
                    "until { ap2 card list} { cp src $AP2_DATA/config.yaml; break}",
                    "while { ap2 card list } 2>/dev/null { cp src $AP2_DATA/config.yaml; break }",
                    "while { ap2 card list }>/dev/null { cp src $AP2_DATA/config.yaml; break }",
                    "while { ap2 card list }</dev/null { cp src $AP2_DATA/config.yaml; break }"], deny=True)
        self.check(["{ ap2 card list --data $AP2_DATA }", "{ ap2 card list --data $AP2_DATA } > /tmp/x",
                    "{ ap2 card list --data $AP2_DATA; } 2>&1", "ap2 card show ${C} --data $AP2_DATA",
                    "for c in C1; do { ap2 card show $c --data $AP2_DATA } ; done",
                    "ap2 card note C1 --data $AP2_DATA --text 'a} b'"], deny=False)

    def test_a_rebound_name_keeps_run_directory_paths_built_at_run_time_judged_by_their_verb(self):
        """After `export PATH=...` or `source`, a data-folder or package mention is still denied outright, but a
        run-directory path with a `$` after it is judged by the segment's verb, as without the rebinding."""
        self.check(["export PATH=$PWD/node_modules/.bin:$PATH; for p in home about; do "
                    "playwright screenshot http://localhost:3000/$p $AP2_RUN_DIR/media/$p.png; done",
                    'source .venv/bin/activate && python scripts/shot.py --out "$AP2_RUN_DIR/media/shot-$(date +%s).png"',
                    'export PATH="$HOME/.local/bin:$PATH"; ls $AP2_RUN_DIR/media/$NAME',
                    # Accepted, best effort: `ls` runs `cp`, and `$f` is built at run time.
                    "alias ls=cp; ls a $AP2_RUN_DIR/$f"], deny=False)
        self.check(["source .venv/bin/activate && cp x $AP2_RUN_DIR/$f", "export PATH=/tmp/b:$PATH; ap2 x $AP2_DATA",
                    "export PATH=/tmp/b:$PATH; ap2 card list $AP2_RUN_DIR/../../config.yaml",
                    f"export PATH=/tmp/b:$PATH; {PKG}/bin/ap2 card list"], deny=True)

    def test_a_name_rebound_in_the_same_command_is_no_ap2_call(self):
        """A function, an alias, a `hash` entry or a not-found handler can make `ap2` (or any name) run anything: a
        command that defines one is denied when a segment names a guarded path."""
        self.check(['ap2() { cp "$@"; }; ap2 src $AP2_DATA/config.yaml',
                    'function ap2 { cp "$@"; }; ap2 src $AP2_DATA/config.yaml',
                    "hash -p /bin/cp ap2; ap2 src $AP2_DATA/config.yaml",
                    "shopt -s expand_aliases\nalias ap2=cp\nap2 src $AP2_DATA/config.yaml",
                    "hash ap2=/bin/cp; ap2 src $AP2_DATA/config.yaml",
                    'command_not_found_handler() { shift; cp "$@"; }; ap2 src $AP2_DATA/config.yaml',
                    'command_not_found_handle () { shift; cp "$@"; }; ap2 src $AP2_DATA/config.yaml',
                    'function command_not_found_handle { shift; cp "$@"; }; ap2 src $AP2_DATA/config.yaml',
                    "'ap2'() { cp \"$@\"; }; ap2 src $AP2_DATA/config.yaml",
                    "functions[ap2]='cp \"$@\"'; ap2 src $AP2_DATA/config.yaml",
                    "commands[ap2]=/bin/cp; ap2 src $AP2_DATA/config.yaml",
                    "ln -s /bin/cp /tmp/b/ap2; PATH=/tmp/b:$PATH ap2 src $AP2_DATA/config.yaml",
                    "export PATH=/tmp/b:$PATH; ap2 src $AP2_DATA/config.yaml",
                    "enable -n echo; ap2 src $AP2_DATA/config.yaml", "unalias ap2; ap2 src $AP2_DATA/config.yaml",
                    "builtin alias ap2=cp\nap2 src $AP2_DATA/config.yaml",
                    "eval 'ap2() { cp \"$@\"; }'; ap2 src $AP2_DATA/config.yaml",
                    f'function {PKG}/bin/ap2 {{ cp "$@"; }}; {PKG}/bin/ap2 src $AP2_DATA/config.yaml',
                    'f() { cp "$@"; }; f src $AP2_DATA/config.yaml', 'x ap2 () { cp "$@"; }; ap2 src $AP2_DATA/c',
                    # `source` and `.` run a file that can define one; zsh's `functions -c` copies one.
                    "echo 'ap2(){ cp \"$@\"; }' > /tmp/f.sh; source /tmp/f.sh; ap2 src $AP2_DATA/config.yaml",
                    "echo 'ap2(){ cp \"$@\"; }' > /tmp/f.sh; . /tmp/f.sh; ap2 src $AP2_DATA/config.yaml",
                    'f(){ cp "$@"; }; functions -c f ap2; ap2 src $AP2_DATA/config.yaml',
                    # zsh's arrays `path`, `commands` and `functions`, set whole or added to.
                    "path=(/tmp/b $path); ap2 src $AP2_DATA/config.yaml",
                    "commands+=(ap2 /bin/cp); ap2 src $AP2_DATA/config.yaml",
                    "functions+=(ap2 'cp \"$@\"'); ap2 src $AP2_DATA/config.yaml",
                    # Blanks inside a definition's `()`.
                    'ap2(  ) { cp "$@"; }; ap2 src $AP2_DATA/config.yaml',
                    'ap2 (\t) { cp "$@"; }; ap2 src $AP2_DATA/config.yaml',
                    # A rebinding word as a command word (after a zsh precommand modifier, `command`, an
                    # assignment, a backslash, a word that may expand to nothing), or anywhere in a segment with
                    # an unquoted `)`, `]]` or `}` (after a case pattern, a zsh short loop or condition).
                    'case x in x) function ap2 { cp "$@"; };; esac; ap2 src $AP2_DATA/config.yaml',
                    "case x in x) hash -p /bin/cp ap2;; esac; ap2 src $AP2_DATA/config.yaml",
                    "case x in x) hash ap2=/bin/cp;; esac; ap2 src $AP2_DATA/config.yaml",
                    "noglob hash ap2=/bin/cp; ap2 src $AP2_DATA/config.yaml",
                    "nocorrect hash ap2=/bin/cp; ap2 src $AP2_DATA/config.yaml",
                    "- hash ap2=/bin/cp; ap2 src $AP2_DATA/config.yaml",
                    "repeat 1 hash ap2=/bin/cp; ap2 src $AP2_DATA/config.yaml",
                    "for x (1) hash ap2=/bin/cp; ap2 src $AP2_DATA/config.yaml",
                    "noglob builtin hash ap2=/bin/cp; ap2 src $AP2_DATA/config.yaml",
                    "if [[ -n x ]] hash ap2=/bin/cp; ap2 src $AP2_DATA/config.yaml",
                    "while { true } { hash ap2=/bin/cp; break }; ap2 src $AP2_DATA/config.yaml",
                    "\\hash ap2=/bin/cp; ap2 src $AP2_DATA/config.yaml", "command hash ap2=/bin/cp; ap2 src $AP2_DATA/c",
                    "x=1 hash ap2=/bin/cp; ap2 src $AP2_DATA/config.yaml", "alias ap2=cp; ap2 src $AP2_DATA/c",
                    "$e hash -p /bin/cp ap2; ap2 src $AP2_DATA/config.yaml",
                    "repeat $n hash ap2=/bin/cp; ap2 src $AP2_DATA/config.yaml",
                    # zsh's autoload: a function file found on `fpath`.
                    "fpath=(/tmp/fn); autoload -U ap2; ap2 src $AP2_DATA/config.yaml",
                    "FPATH=/tmp/fn; autoload -U ap2; ap2 src $AP2_DATA/config.yaml",
                    "noglob autoload -U ap2; ap2 src $AP2_DATA/config.yaml",
                    "case x in x) autoload -U ap2;; esac; ap2 src $AP2_DATA/config.yaml",
                    # Accepted false deny: in a command that may rebind, a data-folder read is denied too.
                    f"set -a; source .env; set +a; cat {DATA}/config.yaml"],
                   deny=True)
        # `functions -c f ap2` on its own (`f` defined elsewhere, such as in the shell's startup files) rebinds too.
        for seg in ("functions -c f ap2", "source x", ". x", "path+=(/tmp/b)", "fpath+=(/tmp/fn)", "autoload -U ap2",
                    "case x in x) hash ap2=/bin/cp", "noglob source x"):
            with self.subTest(seg=seg):
                self.assertTrue(guard._rebinds([guard._Parsed(seg, True, True)]))
        for seg in ("find . -name x", "echo . ok", 'echo "source of truth"', "ap2 source list", "grep -i hash f",
                    "grep -c 'x) hash' f", "echo '}' eval"):
            with self.subTest(seg=seg):
                self.assertFalse(guard._rebinds([guard._Parsed(seg, True, True)]))
        self.check(["ap2 card list > /tmp/x", 'f() { echo hi; }; f > "$AP2_RUN_DIR/x"',
                    'echo "f() and alias" > /tmp/x; ap2 card list --data $AP2_DATA',
                    "ap2 card note C1 --text 'see hash()' --data $AP2_DATA",
                    'x=$(date); ap2 card list --data $AP2_DATA',
                    "cp \"$()\" /tmp/x; ap2 card list --data $AP2_DATA",
                    'echo "source of truth: $AP2_RUN_DIR/x" >> $AP2_RUN_DIR/journal.md',
                    "echo source of truth: $AP2_RUN_DIR/x >> $AP2_RUN_DIR/journal.md",
                    "set -a; source .env; set +a; python3 app.py > $AP2_RUN_DIR/app.log",
                    # zsh's precommand modifiers and `repeat N` run the next word: still an ap2 call.
                    "noglob ap2 card list --data $AP2_DATA > /tmp/x", "nocorrect ap2 card list --data $AP2_DATA",
                    "- ap2 card list --data $AP2_DATA", "repeat 2 ap2 card list --data $AP2_DATA > /tmp/x",
                    # A rebinding name as an argument rebinds nothing.
                    f"{PKG}/bin/ap2 source list", f"{PKG}/bin/ap2 source show gh | jq .",
                    f'"{PKG}/bin/ap2" source read gh', f"{PKG}/bin/ap2 search hash --json",
                    f"{PKG}/bin/ap2 card list | grep -c eval", "grep -i hash $AP2_DATA/items/C1.yaml",
                    "cat $AP2_DATA/items/C1.yaml | grep -i source", "grep -rw source $AP2_DATA/sources",
                    f"rg -n alias {PKG}/lib", "grep -rw hash $AP2_DATA/items"],
                   deny=False)

    def test_ap2_keeps_its_exemption_only_when_nothing_in_the_command_can_rebind_it(self):
        """Whatever the position: a word that can rebind a name anywhere in the command (a `_REBIND` word, `ap2` as
        a name, a `.` that runs, a name table or a search-path word, a function definition) ends every `ap2`
        exemption, and an `ap2` segment that then names a guarded path is denied."""
        tail = "; ap2 src $AP2_DATA/config.yaml"
        self.check([# A redirect first.
                    ">/dev/null hash ap2=/bin/cp" + tail, "2>&1 hash ap2=/bin/cp" + tail,
                    "</dev/null hash ap2=/bin/cp" + tail, "<<<x hash ap2=/bin/cp" + tail,
                    ">/dev/null builtin hash ap2=/bin/cp" + tail, "2>/dev/null hash -p /bin/cp ap2" + tail,
                    "2>/dev/null eval 'hash ap2=/bin/cp'" + tail,
                    # Assignments, also with a subscript, `+=` or a substitution in them.
                    "x+=1 hash ap2=/bin/cp" + tail, "a[1]=1 hash ap2=/bin/cp" + tail,
                    "a[0]=1 hash -p /bin/cp ap2" + tail,
                    "x=$(true) hash -p /bin/cp ap2" + tail, "f=$(pwd)/x hash -p /bin/cp ap2" + tail,
                    # A substitution first (it may expand to nothing).
                    "$(true) hash -p /bin/cp ap2" + tail, "`printf ''` hash -p /bin/cp ap2" + tail,
                    "$(true) source ./evil.sh" + tail,
                    # `command --`.
                    "command -- hash -p /bin/cp ap2" + tail, "command -p -- hash -p /bin/cp ap2" + tail,
                    "command -- eval 'hash -p /bin/cp ap2'" + tail,
                    # zsh's name tables and the search path in an expansion, and the search path as a plain word.
                    ": ${commands[ap2]::=/bin/cp}" + tail, ": ${functions[ap2]::=/bin/cp \\$@}" + tail,
                    ": ${PATH::=./b:$PATH}" + tail, "for PATH in ./b; do ap2 src $AP2_DATA/config.yaml; done",
                    "read PATH <<< ./b" + tail,
                    # Inside a process substitution (zsh's `=(` too).
                    f"cat <(hash -p /bin/cp ap2; ap2 /tmp/x {DATA}/config.yaml)",
                    f"echo >(hash -p /bin/cp ap2; ap2 /tmp/x {DATA}/c)",
                    f"cat =(hash ap2=/bin/cp; ap2 /tmp/x {DATA}/c)",
                    f"cat <(builtin hash -p /bin/cp ap2; ap2 /tmp/x {DATA}/c)",
                    f"cat <(source ./e.sh; ap2 /tmp/x {DATA}/c)", f"cat <(. ./e.sh; ap2 /tmp/x {DATA}/c)",
                    f'cat <(function ap2 {{ cp "$@"; }}; ap2 /tmp/x {DATA}/c)', f"cat <(eval 'x'; ap2 /tmp/x {DATA}/c)",
                    f"cat <(hash -p /bin/cp ap2; {PKG}/bin/ap2 card list) ; true",
                    # zsh's `repeat` with a count that is no plain number.
                    "repeat 1+1 hash ap2=/bin/cp" + tail, "repeat 0x1 hash ap2=/bin/cp" + tail,
                    # Found by the shell differential: a `.` after two modifiers, or after two empty expansions.
                    "echo >(x=$(true) nocorrect - . ./f.sh; ap2 src $AP2_DATA/config.yaml)",
                    "$(true) $(true) . ./f.sh" + tail, "`printf ''` command -- . ./f.sh" + tail,
                    # Accepted false deny: a rebinding word anywhere, as an argument too.
                    "ap2 card list --data $AP2_DATA | grep source"], deny=True)
        self.check([f"{PKG}/bin/ap2 source list", "ap2 search hash", f"{PKG}/bin/ap2 card list | grep -c eval",
                    # An ap2 argument that holds `ap2` keeps the exemption.
                    'ap2 card note C1 --text "ap2 is great" --data $AP2_DATA',
                    "ap2 card list --data $AP2_DATA > /tmp/x; ap2 card show C1 --data $AP2_DATA"], deny=False)

    def test_a_renamed_command_is_seen_at_every_command_position(self):
        """For another name than `ap2` (`alias ls=cp; ls a <data>/x`) a rebinding word counts where the shell runs
        it: after redirects, assignments (`x+=1`, `a[1]=1`, `x=$(...)`), a substitution that may expand to nothing,
        `command --`, a process substitution's opener and zsh's `repeat N`."""
        tail = "; ls a $AP2_DATA/x"
        self.check([">/dev/null alias ls=cp" + tail, "2>&1 alias ls=cp" + tail, "</dev/null alias ls=cp" + tail,
                    "<<<x alias ls=cp" + tail, "x+=1 alias ls=cp" + tail, "a[1]=1 alias ls=cp" + tail,
                    "x=$(true) alias ls=cp" + tail, "f=$(pwd)/x alias ls=cp" + tail, "$(true) alias ls=cp" + tail,
                    "`true` alias ls=cp" + tail, "command -- alias ls=cp" + tail, "command -p -- alias ls=cp" + tail,
                    "repeat 1+1 alias ls=cp" + tail, ": ${commands[ls]::=/bin/cp}" + tail,
                    # One modifier after another, and a substitution that may be empty after another.
                    "nocorrect - alias ls=cp" + tail, "$(true) $(true) alias ls=cp" + tail,
                    "`true` command -- alias ls=cp" + tail], deny=True)
        for seg in ("repeat 1+1 alias ls=cp", "repeat 0x1 alias ls=cp", "$e alias ls=cp", ">x alias ls=cp",
                    "x+=1 alias ls=cp", "cat <(alias ls=cp)", "cat =(alias ls=cp)", "echo >(alias ls=cp)",
                    "command -- alias ls=cp", "nocorrect - alias ls=cp", "$(true) $(true) . f",
                    "`true` command -- . f"):
            with self.subTest(seg=seg):
                self.assertTrue(guard._rebinds([guard._Parsed(seg, True, True)]))
        for seg in ("cat <(grep -l hash x)", "ls $(pwd) .", "echo x=1 hash", "find . -exec grep -l eval {} +",
                    "xargs -I{} grep -l source x/{}", "rg -n source x/{a,b}", "(rg -n source x)"):
            with self.subTest(seg=seg):
                self.assertFalse(guard._rebinds([guard._Parsed(seg, True, True)]))

    def test_a_closer_counts_only_before_the_rebinding_word(self):
        """A rebinding word is loose (may run) only after an unquoted `)`, `]]` or `}` of its segment, and the `}`
        of an empty `{}` is none: `find ... -exec grep -l hash {} +` names `hash` as an argument."""
        self.check(["find $AP2_DATA/items -name '*.yaml' -exec grep -l hash {} +",
                    f"find {PKG}/lib -name '*.py' -exec grep -n eval {{}} +",
                    f"ls {DATA}/items | xargs -I{{}} grep -l source {DATA}/items/{{}}",
                    f"(cd {PKG} && rg -n eval lib)", f"(rg -n source {DATA}/items)", f"({PKG}/bin/ap2 source list)",
                    f"cat <(grep -l hash {DATA}/items)", f"rg -n source {DATA}/{{items,routines}}",
                    f"find . -name '*.ts' -exec grep -l function {{}} + ; {PKG}/bin/ap2 card list"], deny=False)
        self.check(["case x in x) hash ap2=/bin/cp;; esac; ls $AP2_DATA/x",
                    "for x (1) hash ls=/bin/cp; ls a $AP2_DATA/x", "if [[ -n x ]] hash ls=/bin/cp; ls a $AP2_DATA/x",
                    "while { true } { hash ls=/bin/cp; break }; ls a $AP2_DATA/x"], deny=True)

    def test_only_a_plain_printer_piped_into_a_shell_leaves_the_outer_reading(self):
        """A fed segment leaves its words out only when it is one `echo` or `printf` command and nothing else: zsh's
        `always` block (or a short loop after a `}`) runs a command of its own."""
        self.check(["{ echo '#' } always { cp src $AP2_DATA/config.yaml } | sh",
                    "echo x | tee sh -c \"$AP2_DATA/config.yaml\""], deny=True)
        self.check(["bash -O nullglob -c 'cp a.png \"$AP2_RUN_DIR/media/\"; echo $?'",
                    "bash --norc -c 'make > \"$AP2_RUN_DIR/m.log\"; echo \"exit $?\"'",
                    "bash -c -- 'make > \"$AP2_RUN_DIR/m.log\"; echo \"exit $?\"'",
                    "printf '%s\\n' 'make > \"$AP2_RUN_DIR/m.log\"; echo \"exit $?\"' | sh"], deny=False)

    def test_a_quoted_brace_bracket_or_angle_ends_a_file_name_in_code(self):
        """In Perl's `q{...}`, `q[...]`, `q(...)` and Ruby's `%q{...}` a quoted `}`, `]`, `)` ends the file name."""
        self.check(['perl -e "open(F, q{>$AP2_RUN_DIR/settings-implement.json}); print F 1"',
                    'perl -e "open(F, q[>$AP2_RUN_DIR/settings-implement.json]); print F 1"',
                    'perl -e "open(F, q(>$AP2_RUN_DIR/settings-implement.json)); print F 1"',
                    'ruby -e "File.write(%q{$AP2_RUN_DIR/settings-implement.json}, 1)"',
                    'ruby -e "File.write(%q($AP2_RUN_DIR/settings-implement.json), 1)"'], deny=True)
        self.check(['perl -e "open(F, q{>$AP2_RUN_DIR/out.txt}); print F 1"',
                    'ruby -e "File.write(%q{$AP2_RUN_DIR/out.json}, 1)"'], deny=False)

    def test_a_heredoc_fed_to_a_shell_is_read_as_one_script(self):
        """A heredoc body that a shell reads (`bash <<'EOF'`, `bash -s <<'EOF'`, `cat <<'EOF' | bash`) is code: the
        whole body is read as a script of its own, so a helper function, a rebinding and a quoted word across lines
        are seen; a script that writes only the run directory stays allowed."""
        body = 'save() { cp "$1" "$2"; }\nsave src "$AP2_DATA/config.yaml"\n'
        self.check(["bash <<'EOF'\n" + body + "EOF", "cat <<'EOF' | bash\n" + body + "EOF",
                    "bash -s <<'EOF'\n" + body + "EOF", "bash <<EOF\n" + body + "EOF",
                    "sh <<'EOF'\nsave() {\n  cp \"$1\" \"$2\"\n}\nsave src \"$AP2_DATA/config.yaml\"\nEOF",
                    "bash <<'EOF'\nhash -p /bin/cp ap2\nap2 src $AP2_DATA/config.yaml\nEOF",
                    "zsh <<'EOF'\nhash ap2=/bin/cp\nap2 src $AP2_DATA/config.yaml\nEOF",
                    "bash <<'EOF'\ntouch \"\n\" $AP2_DATA/x\nEOF", "bash <<-'EOF'\n\ttouch \"\n\t\" $AP2_DATA/x\n\tEOF",
                    "x=$(bash <<'EOF'\ntouch \"\n\" $AP2_DATA/x\nEOF\n)"], deny=True)
        self.check(["bash <<'EOF'\nset -e\nmake > \"$AP2_RUN_DIR/m.log\" 2>&1\necho \"exit $?\" >> \"$AP2_RUN_DIR/m.log\"\n"
                    "EOF",
                    "cat <<'EOF' | bash\nfor f in \"$AP2_RUN_DIR\"/media/*.png; do\n  echo \"$f\"\ndone > "
                    "\"$AP2_RUN_DIR/list.txt\"\nEOF",
                    "bash -s <<'EOF'\nsave() { cp \"$1\" \"$2\"; }\nsave a.png \"$AP2_RUN_DIR/media/a.png\"\nEOF",
                    # A body fed to anything but a shell is data, line by line.
                    "cat > \"$AP2_RUN_DIR/notes.md\" <<'EOF'\nsave() { cp \"$1\" \"$2\"; }\nEOF\nls $AP2_DATA"], deny=False)
        segs = guard._segments("bash <<'E'\na\nb\nE\nz")
        self.assertEqual([s for s in segs if isinstance(s, guard._Script)], ["a\nb\n"])
        # The body lines stay data lines of the outer command (a child shell rebinds nothing in it); the script is
        # judged as one of its own.
        self.assertEqual([type(s) for s in segs], [str, guard._Body, guard._Body, guard._Body, guard._Script, str])
        self.assertFalse([s for s in guard._segments("cat <<'E'\na\nE\nz") if isinstance(s, guard._Script)])

    def test_an_unquoted_heredoc_body_runs_its_substitutions_across_lines(self):
        """In a heredoc with an unquoted delimiter the shell runs each `$(...)` and backtick of the body, read as a
        `"..."` word is: a substitution can run across lines, and a `'` or `"` outside one is a plain character."""
        self.check(['cat <<EOF >/dev/null\n$(touch "\n" $AP2_DATA/x)\nEOF',
                    'cat <<EOF > /dev/null\n$(cp src ${x+\n}$AP2_DATA/config.yaml)\nEOF',
                    'cat <<EOF\n$(cp src $(\n)$AP2_DATA/config.yaml)\nEOF',
                    'cat <<EOF >/dev/null\n`touch "\n" $AP2_DATA/x`\nEOF',
                    "cat <<EOF >/dev/null\nit's $(touch \"\n\" $AP2_DATA/x)\nEOF",
                    "cat <<EOF >/dev/null\nsay \"hi $(touch \"\n\" $AP2_DATA/x)\nEOF"], deny=True)
        self.check(["cat <<EOF > \"$AP2_RUN_DIR/notes.md\"\nbuilt at $(date)\nit's done, \"$(git rev-parse HEAD)\"\nEOF",
                    "cat <<'EOF' >/dev/null\n$(touch \"\n\" $AP2_DATA/x)\nEOF"], deny=False)
        self.assertIn('touch "\n" $AP2_DATA/x', guard._segments("cat <<E\nit's $(touch \"\n\" $AP2_DATA/x)\nE"))

    def test_a_locale_or_ansi_c_quote_in_a_word_reads_as_the_shell_passes_it(self):
        """`$"..."` is `"..."` (the `$` goes), and `$'...'` is decoded (`$'\\x63p'` is `cp`); a `\\u` that decodes
        to an ASCII character is read as decoded, and may rebind a name (bash 3.2 may or may not decode `\\u`: it
        depends on the locale)."""
        self.check(['c$"p" src $AP2_DATA/config.yaml', f'cp src {DATA[:-1]}$"{DATA[-1]}"/config.yaml',
                    "$'\\x63p' src $AP2_DATA/config.yaml", "$'\\143p' src $AP2_DATA/config.yaml",
                    "$'\\x68ash' -p /bin/cp $'\\x61p2'; ap2 src $AP2_DATA/config.yaml",
                    "cp src $AP2_RUN_DIR/$'\\x2e\\x2e'/config.yaml",
                    "cp src $AP2_RUN_DIR/$'settings-implement\\x2ejson'",
                    # An ASCII `\\u` is read as decoded and flagged: a guarded path next to one is denied.
                    "$'\\u0063p' src $AP2_DATA/config.yaml", "cp src $AP2_RUN_DIR/$'\\u002e\\u002e'/config.yaml",
                    "$'\\u0068ash' -p /bin/cp $'\\u0061p2'; ap2 src $AP2_DATA/config.yaml"], deny=True)
        self.check(["printf $'a\\tb\\n' > \"$AP2_RUN_DIR/x.txt\"", "printf $'\\u2713 done\\n' >> \"$AP2_RUN_DIR/journal.md\"",
                    'echo $"done" > "$AP2_RUN_DIR/x.txt"; ls $AP2_DATA'], deny=False)
        self.assertEqual(guard._words('c$"p" $$"x" \\$"y"'), ["cp", "$$x", "$y"])
        self.assertEqual(guard._words("$'\\x63p' $'a\\'b' $'\\u0041'"), ["cp", "a'b", "A"])

    def test_zsh_flags_before_the_name_are_a_mention(self):
        """zsh's `${=X}`, `${~X}` and `${^X}` (split, glob, rc-expand) expand `X`: a mention that is never the own run
        directory, as `${AP2_DATA:?}` is; likewise for `${HOME`."""
        self.check(["cp src ${=AP2_DATA}/config.yaml", "cp src ${~AP2_DATA}/config.yaml",
                    "cp src ${^AP2_DATA}/config.yaml", "cp src ${^~=AP2_DATA}/config.yaml",
                    "cp src ${=AP2_DATA:-x}/config.yaml", "echo x > ${~AP2_RUN_DIR}/x",
                    "cp src ${=HOME}/Documents/Assistant/config.yaml", "cp src ${~HOME:-x}/Documents/Assistant/c"],
                   deny=True)
        self.check(["cp src ${=HOME}/Documents/Assistant2/x", "echo ${=AP2_DATA_OLD} > /tmp/x"], deny=False)

    def test_a_quoted_end_right_after_the_run_directory_is_text(self):
        """The run directory itself followed by a quoted `"`, `'`, `)`, `]`, `}` or `,` (JSON or code naming it) is
        the run directory; a path after the quote is still judged."""
        self.check([f"echo '{{\"run_dir\": \"{RUN}\"}}' > {RUN}/meta.json",
                    f"python3 -c \"from pathlib import Path; p = Path('{RUN}'); (p / 'result.json').write_text('{{}}')\"",
                    "python3 -c \"import os, json; json.dump(os.listdir('$AP2_RUN_DIR'), "
                    "open('$AP2_RUN_DIR/ls.json', 'w'))\"",
                    f"echo '[{RUN}]' > {RUN}/a.md", f"echo 'files in {RUN}, see' > {RUN}/a.md"], deny=False)
        self.check([f"python3 -c \"open('{RUN}' + '/../config.yaml','w')\"", f"cp a '{RUN}\"/../x'",
                    f"> {RUN}/settings-implement.json", f"cp a {RUN}'\"'/../x",
                    # A path given to a command (the mention starts its word, or comes after text that may be
                    # nothing or is only `/` and `.`) makes a file next to the run directory: no JSON or code there.
                    "touch \"$AP2_RUN_DIR'\"", f"touch '{RUN}\"'", "touch $e\"$AP2_RUN_DIR'\"",
                    "touch {,}\"$AP2_RUN_DIR'\"", "touch `x`\"$AP2_RUN_DIR'\"", "touch /\"$AP2_RUN_DIR'\"",
                    "touch ./\"$AP2_RUN_DIR,x\""], deny=True)

    def test_only_a_rebinding_shaped_word_ends_the_ap2_trust(self):
        """`ap2` inside prose (a commit message, an echo), a plain `path` argument or a quoted `"path="` pattern
        rebinds nothing; `ap2` as a whole word, `ap2=`, `ap2(`, `[ap2]`, an unquoted search-path assignment or a
        search-path name a setter takes still do."""
        self.check([f"git commit -qm 'fix(v2): ap2 guard reads quotes' && {PKG}/bin/ap2 card note C1 --file "
                    "\"$AP2_RUN_DIR/note-$(date +%s).md\"",
                    f"git log --oneline -- path | head -1; {PKG}/bin/ap2 card note C1 --file $AP2_RUN_DIR/$f",
                    f"echo 'posting via ap2'; {PKG}/bin/ap2 worker park C1 implement --note \"pushed; notes in "
                    "$AP2_RUN_DIR, next: review\"",
                    f"rg -n --type=py \"path=\" {PKG}/lib", f"rg -n 'PATH=' {PKG}/lib",
                    f"git log -- path; ap2 card note C1 --text x --data $AP2_DATA"], deny=False)
        tail = "; ap2 src $AP2_DATA/config.yaml"
        self.check(["typeset -fu ap2" + tail, "declare -fu ap2" + tail, "alias 'ap2'='cp'" + tail,
                    "export \"PATH=/tmp/b:$PATH\"" + tail, "env \"PATH=/tmp/b\" ap2 src $AP2_DATA/config.yaml",
                    "exec env PATH=/tmp/b ap2 src $AP2_DATA/config.yaml", "printf -v PATH %s /tmp/b" + tail,
                    "path[1]=/tmp/b" + tail, "trap 'PATH=/tmp/b' DEBUG" + tail, "set -A path /tmp/b" + tail,
                    "exec env PATH=/tmp/b ls a $AP2_DATA/x"], deny=True)

    def test_each_finding_row_pins_its_rule(self):
        """Rows that fail when the rule they name is taken out."""
        # A level-1 run-directory path (`$f`) in an `ap2` segment that may run anything.
        self.check(['f=settings-implement.json; hash -p /bin/cp ap2; ap2 a "$AP2_RUN_DIR/$f"',
                    # A quoted `>` ends a file name in code (Ruby's `%q<...>`, Perl's `q<...>`).
                    'ruby -e "File.write(%q<$AP2_RUN_DIR/settings-implement.json>, 1)"',
                    "perl -e \"open(F, '>' . q<$AP2_RUN_DIR/settings-implement.json>); print F 1\"",
                    # A name table in an unquoted heredoc body.
                    ": <<E\n${commands[ap2]::=/bin/cp}\nE\nap2 src $AP2_DATA/config.yaml",
                    "read x <<E\n${commands[ap2]::=/bin/cp}\nE\nap2 src $AP2_DATA/config.yaml",
                    # A word glued after a backtick is still the assignment before the command.
                    "x=`true`/b alias ls=cp; ls a $AP2_DATA/x"], deny=True)
        self.check(['ruby -e "File.write(%q<$AP2_RUN_DIR/out.json>, 1)"',
                    ": <<'E'\n${commands[ap2]::=/bin/cp}\nE\nap2 src $AP2_DATA/config.yaml",
                    # A `.` before a subshell's `)` is an argument, not a command after a closer.
                    f"(cd {DATA}/items && ls .)", f"(cd {DATA} && grep -rn TODO .)"], deny=False)
        self.assertTrue(guard._rebinds([guard._Parsed("x=`true`/b alias ls=cp", True, True)]))

    def test_a_heredoc_delimiter_the_reader_may_end_elsewhere_opens_no_body_and_denies_a_guarded_path(self):
        """A delimiter word that holds `$(`, `${`, `$[` or a backtick, or that a `(` follows, can end where the shell
        ends it and `_DELIM` does not (`<<$(x)`, `<<${x:-a b}`): no body is opened, and a command with one that names
        a guarded path is denied (lines read as code can hide a write behind a quote the body held)."""
        f = 'f() { cp "$@"; }\nf /tmp/src $AP2_DATA/config.yaml\n'
        self.check(["cat <<$(x) >/dev/null\n$(x)\n" + f, "cat <<${x:-a b} >/dev/null\n${x:-a b}\n" + f,
                    'cat <<$(x) >/dev/null\n$(x)\nap2() { cp "$@"; }\nap2 /tmp/src $AP2_DATA/config.yaml\n',
                    "cat <<`x` >/dev/null\n`x`\n" + f, "cat <<$[1] >/dev/null\n$[1]\n" + f,
                    "cat <<\"$(x)\" >/dev/null\n$(x)\n" + f, "cat <<a$(x) >/dev/null\na$(x)\n" + f,
                    "cat <<$(x)\nit's\n$(x)\ncp a $AP2_DATA/c # '\n",
                    # Read as code, the body's `'` would hide the rebinding from the `ap2` trust check.
                    "cat <<$(x)\nit's\n$(x)\nhash -p /bin/cp ap2 #'\nap2 src $AP2_DATA/config.yaml",
                    # An undecodable `$'...'` delimiter opens no body either, and is marked the same way.
                    "cat <<$'\\u0045'\nit's\nE\nhash -p /bin/cp ap2 #'\nap2 src $AP2_DATA/config.yaml"], deny=True)
        self.check(["cat <<$(x) >/dev/null\n$(x)\nls /tmp\n", "cat <<'EOF' > $AP2_RUN_DIR/a.md\nit's\nEOF"],
                   deny=False)
        for cmd in ("cat <<$(x)\nb\n$(x)\n", "cat <<${x}\nb\n", "cat <<`x`\nb\n", "cat <<$[1]\nb\n", "cat <<x(y)\n",
                    "cat <<$'\\u0045'\nb\n"):
            with self.subTest(cmd=cmd):
                seen = set()
                guard._segments(cmd, seen=seen)
                self.assertIn("<<", seen)
                self.assertFalse(any(isinstance(s, guard._Body) for s in guard._segments(cmd)))
        seen = set()
        guard._segments("cat <<'E'\nb\nE\n", seen=seen)
        self.assertNotIn("<<", seen)

    def test_a_shell_reads_its_input_unless_a_c_comes_among_its_options(self):
        """Only the option words before the first `--`, `-` or other word (an option's value aside) can be a `-c`:
        a `-c` after them is a positional argument, and a `-s` cluster reads the script from the input."""
        f = 'f() { cp "$@"; }\nf /tmp/src $AP2_DATA/config.yaml\n'
        self.check(["bash -s -- -c <<'EOF'\n" + f + "EOF\n", "cat <<'EOF' | bash -s -- -c\n" + f + "EOF\n",
                    "zsh -s -- -c <<'EOF'\n" + f + "EOF\n", "bash -s -- -recursive <<'EOF'\n" + f + "EOF\n",
                    "bash -s x -c <<'EOF'\n" + f + "EOF\n", "bash -o posix -s -- -c <<'EOF'\n" + f + "EOF\n",
                    "bash -sc <<'EOF'\n" + f + "EOF\n"], deny=True)
        for words, shell in ((["bash", "-s", "--", "-c"], "bash"), (["bash", "-s", "x", "-c"], "bash"),
                             (["bash", "--", "-c"], "bash"), (["bash", "-", "-c"], "bash"),
                             (["bash", "-o", "c", "-x"], "bash"), (["bash", "-sc", "x"], "bash"),
                             (["bash", "-c", "x"], None), (["bash", "-ec", "x"], None),
                             (["bash", "-o", "pipefail", "-c", "x"], None), (["bash", "+O", "x", "-c", "y"], None)):
            with self.subTest(words=words):
                self.assertEqual(guard._stdin_shell(words), shell)

    def test_a_path_given_after_an_equals_sign_or_climbing_to_the_root_is_no_text(self):
        """`of=`, `x=` or `--flag=` before the run directory gives a command a real path, and so does text that ends
        in `..` (`/usr/..` is `/`): the run directory there must be followed by `/` or nothing."""
        self.check(["dd if=/dev/zero of=\"$AP2_RUN_DIR'\" count=1", "cp src --target-directory=\"$AP2_RUN_DIR'\"",
                    f"dd if=/dev/zero of='{RUN}\"' count=1", "touch \"/usr/..$AP2_RUN_DIR'\"",
                    "touch /usr/..$AP2_RUN_DIR.bak", "touch x/../..$AP2_RUN_DIR.bak", "touch /usr/../$AP2_RUN_DIR.bak",
                    "touch /usr/../.$AP2_RUN_DIR.bak"], deny=True)
        self.check(["dd if=/dev/zero of=$AP2_RUN_DIR/x.bin count=1", "dd if=/dev/zero of=\"$AP2_RUN_DIR/x\" count=1"],
                   deny=False)

    def test_zsh_flags_without_braces_are_a_mention(self):
        """zsh's `$=X`, `$~X` and `$^X` expand `X` as `${=X}` does: a mention that is never the own run directory."""
        self.check(["cp src $=AP2_DATA/config.yaml", "cp src $~AP2_DATA/config.yaml", "cp src $^AP2_DATA/config.yaml",
                    "cp src $^~AP2_DATA/config.yaml", "cp src $=AP2_RUN_DIR/../x", "echo x > $=AP2_RUN_DIR/x",
                    'cp src "$~AP2_DATA"/config.yaml', "cp src $=HOME/Documents/Assistant/config.yaml",
                    "cp src $~HOME/Documents/Assistant/config.yaml"], deny=True)
        self.check(["echo $=AP2_DATA_OLD > /tmp/x", "cp src $=HOME/Documents/Assistant2/x", "echo $=x > /tmp/y"],
                   deny=False)

    def test_a_shell_fed_a_heredoc_rebinds_nothing_in_the_outer_command(self):
        """A child shell fed a heredoc cannot rebind the outer shell's names, search path or functions: its own
        rebinding is judged by the script reading one level deeper, and the outer `ap2` keeps its exemption."""
        venv = "source .venv/bin/activate\npytest -q > $AP2_RUN_DIR/pytest.log 2>&1\n"
        npm = 'export PATH="$PWD/node_modules/.bin:$PATH"\nnpm test > $AP2_RUN_DIR/t.log 2>&1\n'
        log = 'log() { echo "$*" >> "$AP2_RUN_DIR/log.txt"; }\nlog started\n'
        note = f"{PKG}/bin/ap2 card note C1 --file $AP2_RUN_DIR/pytest.log"
        park = f"{PKG}/bin/ap2 worker park C1 implement --note 'tests pass'"
        self.check([f"bash <<'EOF'\n{venv}EOF\n{note}", f"bash <<'EOF'\n{npm}EOF\n{note}",
                    f"bash <<'EOF'\n{log}EOF\n{note}", f"bash <<'EOF'\n{venv}EOF\ncat {DATA}/items/C1.yaml",
                    f"bash <<'EOF'\n{venv}EOF\n{park}", f"bash <<'EOF' && {note}\n{venv}EOF",
                    f"cat <<'EOF' | bash\n{venv}EOF\n{note}", f"bash -s <<'EOF'\n{npm}EOF\n{park}",
                    f"bash <<EOF\n{log}EOF\n{note}"], deny=False)
        # The script's own rebinding still counts inside it.
        self.check(["bash <<'EOF'\nhash -p /bin/cp ap2\nap2 src $AP2_DATA/config.yaml\nEOF",
                    "bash <<'EOF'\nexport PATH=/tmp/b:$PATH\nap2 src $AP2_DATA/config.yaml\nEOF",
                    "cat <<'EOF' | bash\nap2() { cp \"$@\"; }\nap2 src $AP2_DATA/config.yaml\nEOF"], deny=True)

    def test_an_ansi_c_nul_or_non_ascii_code_rebinds_nothing(self):
        """A code past 0x7F is passed as it is and a `$'...'` word that is nothing but NULs is empty: neither can spell
        a name. bash 3.2 may or may not decode `\\u` (it depends on the locale) while zsh decodes it, so only a `\\u`
        or `\\U` that decodes to an ASCII character may rebind a name. A NUL inside a longer word is read otherwise
        by each shell: that word keeps its `$`."""
        self.check([f"find {DATA}/items -name '*.yaml' -print0 | while IFS= read -r -d $'\\0' f; do wc -l \"$f\"; done",
                    f"grep -c $'\\0' {DATA}/index.sqlite", f"tr -d $'\\0' < {DATA}/x.log | head",
                    f"tr -d $'\\x00\\000' < {DATA}/x.log | head",
                    f"{PKG}/bin/ap2 card note C1 --text $'Done \\u2014 tests pass' --file $AP2_RUN_DIR/n.md",
                    f"{PKG}/bin/ap2 card note C1 --text $'Done — tests pass' --file $AP2_RUN_DIR/n.md",
                    f"{PKG}/bin/ap2 worker park C1 implement --note $'tests pass ✅\\nnext: review' --data $AP2_DATA",
                    f"echo $'\\xe2\\x9c\\x93 ok' >> $AP2_RUN_DIR/journal.md; ls {DATA}/items",
                    f"echo $'✓ ok' >> $AP2_RUN_DIR/journal.md; ls {DATA}/items",
                    f"{PKG}/bin/ap2 card note C1 --text $'\\U0001F600 done' --file $AP2_RUN_DIR/n.md"], deny=False)
        self.check(["$'cp' src $AP2_DATA/config.yaml", "$'\\543p' src $AP2_DATA/config.yaml",
                    "$'\\U00000063p' src $AP2_DATA/config.yaml", "cp src $AP2_RUN_DIR/$'\\U0000002e.'/config.yaml",
                    "$'\\x63p\\0x' src $AP2_DATA/config.yaml",
                    # Found by the shell differential: zsh's `..`, which a verb-only judgement let through.
                    "$'\\u0063p' src $AP2_RUN_DIR/$'\\u002e\\u002e'/x",
                    "f() { cp \"$@\"; }\nf src $AP2_RUN_DIR/$'\\u002e\\u002e'/x",
                    # A `$x` that may expand to nothing before a decoded verb (bash and zsh run `cp`).
                    "$x$'\\x63p' src $AP2_DATA/config.yaml", "$=x$'\\543p' src $AP2_DATA/config.yaml",
                    "$x$'\\x74'ouch $AP2_DATA/c", "$1$'\\x63p' src $AP2_DATA/config.yaml",
                    "$x'c'p src $AP2_DATA/config.yaml", "c$x\"p\" src $AP2_DATA/config.yaml"], deny=True)
        self.assertEqual(guard._words("$'a\\0b' $'\\x00' $'c\\000\\'d' $'\\543p' $'\\u2014' $'\\xe2'"),
                         ["$a0b", "$c000'd", "cp", "\\u2014", "\xe2"])
        flags = set()
        guard._words("$'\\u2014 \\U0001F600'", flags=flags)
        self.assertNotIn("$'", flags)
        guard._words("$'\\u0063'", flags=flags)
        self.assertIn("$'", flags)

    def test_prose_naming_the_run_directory_mid_sentence_is_text(self):
        """Inside a word of prose, JSON or code the run directory may be followed by any character that is no name
        character and no `/`; a path after a later `/` is still judged."""
        self.check(['echo "Results are in $AP2_RUN_DIR now" >> $AP2_RUN_DIR/journal.md',
                    'echo "- screenshots in $AP2_RUN_DIR/media; journal in $AP2_RUN_DIR." >> $AP2_RUN_DIR/journal.md',
                    f'echo "Run dir: {RUN}." >> {RUN}/journal.md',
                    f"echo '- run dir is {RUN} (own)' >> {RUN}/journal.md",
                    'echo "cd $AP2_RUN_DIR && ls" >> $AP2_RUN_DIR/journal.md',
                    'echo "notes (in $AP2_RUN_DIR)" >> $AP2_RUN_DIR/journal.md',
                    'echo "<$AP2_RUN_DIR>" > $AP2_RUN_DIR/a.md', 'echo "{dir: $AP2_RUN_DIR}" > $AP2_RUN_DIR/a.md'],
                   deny=False)
        self.check(['echo "Results are in $AP2_RUN_DIR /../../config.yaml" > $AP2_RUN_DIR/a.md',
                    'echo "in $AP2_RUN_DIR-old" > $AP2_RUN_DIR/a.md', 'echo "in ${AP2_RUN_DIR}x" > $AP2_RUN_DIR/a.md',
                    "touch \"$AP2_RUN_DIR now\"", "touch $AP2_RUN_DIR.bak", 'echo "in $AP2_RUN_DIR/../x" > /tmp/a'],
                   deny=True)

    def test_a_setter_name_counts_only_where_it_runs(self):
        """A word such as `set`, `export` or `read` makes a plain `path` word a search-path setting only where it runs
        as a command (or after an unquoted closer): as an argument (`git grep -n set -- path`) it sets nothing."""
        note = f"; {PKG}/bin/ap2 card note C1 --file $AP2_RUN_DIR/n.md"
        self.check(["git grep -n set -- path" + note, "git log -S export -- path" + note, "rg -w env path" + note,
                    "grep -rn trap path" + note, "pytest -k 'read' path" + note,
                    "rg -n --type py -e for -e path lib" + note], deny=False)
        tail = "; ap2 src $AP2_DATA/config.yaml"
        self.check(["case x in x) read PATH <<< ./b;; esac" + tail, "x=1 export \"PATH=/tmp/b\"" + tail,
                    "command export \"PATH=/tmp/b\"" + tail,
                    "do for PATH in ./b; do ap2 src $AP2_DATA/config.yaml; done",
                    "$e export \"PATH=/tmp/b\"" + tail], deny=True)

    def test_more_finding_rows_pin_their_rules(self):
        """Rows that fail when the rule they name is taken out: empty backticks read as one only when empty, a path
        to a file named `ap2`, an unquoted search-path assignment whose value holds quoted text, a `$` before a
        quote taken out for the text search, and a `$` that ends a run of ordinary word characters."""
        self.check(["echo ```cp a $AP2_DATA/x`", "x=```touch $AP2_DATA/x`",
                    "ln -s /bin/cp ~/bin/ap2; ap2 src $AP2_DATA/config.yaml",
                    "cp /bin/cp /usr/local/bin/ap2; ap2 src $AP2_DATA/config.yaml",
                    'PATH="/opt/tools (arm)/bin:$PATH"; ap2 src $AP2_DATA/config.yaml',
                    "PATH='/tmp/b,c'; ap2 src $AP2_DATA/config.yaml"], deny=True)
        self.check(["source .venv/bin/activate && playwright screenshot http://localhost:3000 "
                    "$AP2_RUN_DIR/media/home-$(date +%s).png",
                    "export PATH=$PWD/node_modules/.bin:$PATH; playwright screenshot http://localhost:3000/a "
                    "$AP2_RUN_DIR/media/a-$(git rev-parse --short HEAD).png"], deny=False)
        self.assertTrue(guard._Marks(WORKER).in_text(f'cp a {DATA[:-1]}$"{DATA[-1]}"/c'))

    def test_a_final_lone_backslash_adds_nothing(self):
        self.check(["cp ./src $AP2_RUN_DIR/settings-implement.json\\"], deny=True)
        self.assertEqual(guard._words("a b\\"), ["a", "b"])
        self.assertEqual(guard._words("a b\\\\"), ["a", "b\\"])

    def test_an_ansi_c_escape_bash_and_zsh_read_differently_is_undecodable(self):
        """bash 3.2 and zsh decode only a shared set of `$'...'` escapes alike; zsh drops the backslash of any other
        one (`\\d` is `d`), reads `\\C-X`, `\\CX` and `\\M-X`, reads `\\cX` as `c` and X, and makes a NUL of `\\x`,
        `\\u` and `\\U` with no digit, `\\u0000` and `\\C-@` (an external command's path ends there). Any of those
        keeps the word's `$`, may rebind a name, and denies a command that names a guarded path anywhere."""
        esc = DATA.replace("Documents", "Docu\\ments")
        floor = "cp /tmp/src $AP2_RUN_DIR/settings-implement.json"
        self.check([f"echo x > $'{esc}/config.yaml'", f"cp /tmp/src $'{esc}/config.yaml'",
                    "cp /tmp/src ~/Documents/$'\\A'ssistant/config.yaml",
                    "cp /tmp/src $AP2_RUN_DIR/$'\\.\\.'/$'\\.\\.'/config.yaml",
                    "cp /tmp/src $AP2_RUN_DIR/$'\\C-'../$'\\C-'../config.yaml",
                    "cp /tmp/src $AP2_RUN_DIR/$'\\M-'../$'\\M-'../config.yaml",
                    "cp /tmp/src $AP2_RUN_DIR/..$'\\C-'/..$'\\C-'/config.yaml",
                    "echo hi > $AP2_RUN_DIR/settings-implement.json$'\\C-@'",
                    floor + "$'\\C-@x'", floor + "$'\\u0000x'", floor + "$'\\U00000000x'", floor + "$'\\x'",
                    floor + "$'\\u'", floor + "$'\\0'x", floor + "$'\\cA'", "cp /tmp/src $AP2_RUN_DIR/.$'.\\C-@x'",
                    "$'c\\p' /tmp/src $AP2_DATA/config.yaml", "$'t\\ouch' $AP2_DATA/newfile",
                    "$'eva\\l' $'a\\p2() { cp \"$@\"; }'; ap2 /tmp/src $AP2_DATA/config.yaml",
                    "$'eva\\l' $'a\\p2() { cp \"$@\"; }'\nap2 /tmp/src $AP2_DATA/config.yaml",
                    # Found by the shell differential: the text spells the data folder only as one shell decodes it
                    # (bash ends the quote's text at a NUL, `\\c@` too; zsh reads `\\/` as `/` and `\\C` at the end
                    # as nothing).
                    "cp /tmp/src ~/Documents/Assis$'t\\x00'ant/config.yaml",
                    "echo x > ~/Documents/Assista$'n\\c@'t/newfile", "cp /tmp/src ~/Documents/As$'s\\0'istant/c",
                    "cp /tmp/src ~/Document$'s\\057\\/As'sistant/config.yaml",
                    "cp /tmp/src ~/Documents/Assist$'\\u61n\\C't/config.yaml"], deny=True)
        self.check(["printf $'a\\tb\\n' >> $AP2_RUN_DIR/x", f"IFS=$'\\n'; for f in $(ls {DATA}/items); do wc -l $f; done",
                    f"find {DATA}/items -print0 | while IFS= read -r -d $'\\0' f; do echo \"$f\"; done",
                    f"{PKG}/bin/ap2 card note C1 --text $'—\\nnext' --data $AP2_DATA",
                    f"{PKG}/bin/ap2 card note C1 --text $'café' --data $AP2_DATA",
                    f"tr -d $'\\0' < {DATA}/x.log | head", f"echo -e 'a\\tb' >> $AP2_RUN_DIR/x"], deny=False)
        for word in ("$'\\d'", "$'\\C-a'", "$'\\Ca'", "$'\\M-a'", "$'\\ca'", "$'\\x'", "$'\\u'", "$'\\U'",
                     "$'\\u0000'", "$'\\8'", "$'a\\0'", "x$'\\0'", "$'\\0'x"):
            with self.subTest(word=word):
                flags = set()
                self.assertTrue(guard._words(word, flags=flags)[0].startswith(("$", "x$")))
                self.assertIn("$'", flags)
        self.assertEqual(guard._words("$'\\0' $'\\x00\\000' $'\\a\\e\\E\\?\\\"' $'\\x41\\101'"), ["\a\x1b\x1b?\"", "AA"])
        self.assertEqual(guard._shell_readings("a$'t\\0x\\d'b $'s\\057\\/\\C-a\\M-\\C-bc'"),
                         ["atb s/\\/\\C-a\\M-\\C-bc", "at\x00xdb s//\x01\x01c"])

    def test_a_heredoc_delimiter_with_an_escape_zsh_reads_otherwise_opens_no_body(self):
        """A `$'...'` delimiter with an escape outside the shared set, and a `$"..."` one (zsh keeps its `$`), open
        no body: the lines are code, and a command that names a guarded path is denied."""
        tail = "ap2() { cp \"$@\"; }\n"
        self.check(["cat <<$'E\\OF'\nEOF\n" + tail + "E\\OF\nap2 /tmp/src $AP2_DATA/config.yaml\n",
                    "cat <<$\"EOF\"\n$EOF\n" + tail + "EOF\nap2 /tmp/src $AP2_DATA/config.yaml\n",
                    "cat <<$'E\\cAOF'\nEcAOF\n" + tail + "E\\x01OF\nap2 /tmp/src $AP2_DATA/config.yaml\n",
                    # `\\.` is no shared escape (bash keeps the backslash, zsh drops it): no body, `E.F` ends none.
                    "cat <<$'E\\.F'\nE\\.F\n" + tail + "E.F\nap2 /tmp/src $AP2_DATA/config.yaml\n"], deny=True)
        for cmd in ("cat <<$\"EOF\"\nb\nEOF\n", "cat <<$'E\\OF'\nb\n", "cat <<$'\\cA'\nb\n"):
            with self.subTest(cmd=cmd):
                seen = set()
                guard._segments(cmd, seen=seen)
                self.assertIn("<<", seen)

    def test_finding_rows_of_the_thirteenth_round_pin_their_rules(self):
        """Rows that fail when the rule they name is taken out."""
        # `\\u` reads up to four digits (`\\u0074ee` is `tee`, not one code), `\\U` up to eight.
        self.check(["$'\\u0074ee' $AP2_DATA/config.yaml < /dev/null",
                    "$'\\u0064d' if=/dev/zero of=$AP2_DATA/index.sqlite count=1",
                    # Another card's run directory by its full path: a digit or `_` goes on with the name.
                    "python3 -c \"open('${AP2_RUN_DIR}2/notes.md', 'w').write('x')\"",
                    'echo "in ${AP2_RUN_DIR}2" > $AP2_RUN_DIR/a.md', 'echo "in ${AP2_RUN_DIR}_old" > $AP2_RUN_DIR/a.md',
                    # zsh's `^` flag before HOME, and `~` / `^` before a name `_verb` takes out.
                    "cp src $^HOME/Documents/Assistant/config.yaml",
                    "$~x'c'p src $AP2_DATA/config.yaml", "$^x'c'p src $AP2_DATA/config.yaml",
                    # The run directory right after an `=` and then a non-name character: an accepted false deny
                    # (a narrower rule lets `echo "x of=<run>'" | xargs dd` through).
                    f"echo '{{\"cmd\": \"pytest --basetemp={RUN}\"}}' > {RUN}/c.json"], deny=True)
        self.assertEqual(guard._words("$'\\u0074ee'"), ["tee"])
        # A `\\u` past 0x7F spells no ASCII name: no rebinding, so `<pkg>/bin/ap2` stays no package mention.
        self.check([f"{PKG}/bin/ap2 card note C1 --text $'caf\\u00e9 done' --file $AP2_RUN_DIR/n.md",
                    'echo "Saved to..$AP2_RUN_DIR now" >> $AP2_RUN_DIR/journal.md', "echo $=AP2_DATA2 > /tmp/x"],
                   deny=False)

    def test_a_trap_action_is_code_that_may_rebind_a_name(self):
        """`trap` runs its action text as code in the current shell, as `eval` does: it is a rebinding word, so `ap2`
        is not trusted after it and a segment that names the data folder or the package is denied. A command whose
        only guarded mentions are run-directory paths stays allowed."""
        cp = "'ap2 (){ cp \"$@\"; }'"
        self.check([f"trap {cp} DEBUG; ap2 /tmp/src $AP2_DATA/config.yaml",
                    "trap 'ap2 () { cp \"$@\"; }' DEBUG\nap2 /tmp/src $AP2_DATA/config.yaml",
                    f"trap {cp} ERR; false; ap2 /tmp/src {DATA}/config.yaml",
                    f"trap {cp} DEBUG; ap2 /tmp/src $AP2_RUN_DIR/settings-implement.json",
                    "trap 'hash -p /bin/cp ap2' DEBUG; ap2 /tmp/src $AP2_DATA/config.yaml",
                    "trap 'ap2() { cp \"$@\"; }' RETURN; ap2 /tmp/src $AP2_DATA/config.yaml",
                    f"command trap {cp} EXIT; ap2 /tmp/src $AP2_DATA/config.yaml"], deny=True)
        self.check(["set -E; trap 'echo err' ERR; make > $AP2_RUN_DIR/m.log",
                    "trap 'rm -f /tmp/x' EXIT; python3 run.py > $AP2_RUN_DIR/y", "trap - EXIT",
                    "grep -rn trap lib > $AP2_RUN_DIR/t.txt", f"{PKG}/bin/ap2 card note C1 --text 'trap fixed'"],
                   deny=False)
        # `ap2` and blanks before a `(` is a function definition's name.
        for word in ("ap2 (){", "ap2  ()", "ap2\t()", "ap2()"):
            with self.subTest(word=word):
                self.assertTrue(guard._ap2_bound(word))
        for word in ("ap2 card", "ap2x (", "fix ap2 guard"):
            with self.subTest(word=word):
                self.assertFalse(guard._ap2_bound(word))

    def test_finding_rows_of_the_fourteenth_round_pin_their_rules(self):
        """Rows that fail when the rule they name is taken out."""
        # zsh's and bash's readings are searched with their quotes taken out (`"ant"` after a `$'...'`).
        self.check(["cp /tmp/src ~/Documents/Assis$'t\\0x'\"ant\"/config.yaml",
                    "cp /tmp/src ~/Documents/\"Assis\"$'t\\0x'ant/config.yaml",
                    # zsh's `\\u` reads four hex digits (`\\u006e` is `n`, then `\\C` at the end is nothing),
                    # `\\U` eight.
                    "cp /tmp/src ~/Documents/Assista$'\\u006e\\C't/config.yaml",
                    "cp /tmp/src ~/Documents/Assista$'\\U0000006e\\C't/config.yaml"], deny=True)

    def test_each_run_directory_mention_in_a_word_is_judged_on_its_own(self):
        """A quoted journal line can name the run directory twice: each mention that stays inside it is fine, and a
        later one that leaves it is still denied."""
        self.check(['echo "- screenshot: $AP2_RUN_DIR/media/a.png (before), $AP2_RUN_DIR/media/b.png (after)" '
                    '>> "$AP2_RUN_DIR/journal.md"',
                    'echo "see $AP2_RUN_DIR/a.png and $AP2_RUN_DIR/b.png" >> "$AP2_RUN_DIR/journal.md"',
                    f'echo "see {RUN}/a.png and $AP2_RUN_DIR/b.png, {RUN}/c.png" >> "{RUN}/journal.md"'], deny=False)
        self.check(['cp x "$AP2_RUN_DIR/$f"', 'echo x > "$AP2_RUN_DIR/a $AP2_RUN_DIR/../../config.yaml"',
                    'echo x > "$AP2_RUN_DIR/a $AP2_DATA/config.yaml"', "echo x > $AP2_RUN_DIR/..$AP2_RUN_DIR",
                    'echo x > "$AP2_RUN_DIR/a $AP2_RUN_DIR/settings-implement.json"',
                    'echo x > "$AP2_RUN_DIR/a $AP2_RUN_DIR/"*', "echo x > $AP2_RUN_DIR/a$AP2_RUN_DIR$f"], deny=True)

    def test_a_variable_after_the_run_directory_is_not_trusted(self):
        self.check(["cp a.png $AP2_RUN_DIR/$f", "cp a.png ${AP2_RUN_DIR}/media/${f}",
                    # A command substitution after the run directory, either spelling, can print `/../..`.
                    "cp x $AP2_RUN_DIR`echo /../..`/config.yaml", "cp x $AP2_RUN_DIR$(echo /../..)/config.yaml",
                    "cp x $AP2_RUN_DIR/`echo ../..`/config.yaml", 'cp x "$AP2_RUN_DIR/`echo ../..`/config.yaml"'],
                   deny=True)

    def test_the_floor_in_the_run_directory_is_not_writable(self):
        self.check(["echo {} > $AP2_RUN_DIR/settings-implement.json", f"rm {RUN}/settings-review.json",
                    "cp x ${AP2_RUN_DIR}/media/settings-x.json",
                    # A glob or brace that can name a settings file or `..`.
                    "rm $AP2_RUN_DIR/*", "rm $AP2_RUN_DIR/settings*", "echo {} > $AP2_RUN_DIR/settings-implement.js?n",
                    "cp x $AP2_RUN_DIR/.*/config.yaml", "cp x $AP2_RUN_DIR/.*/.*/config.yaml",
                    "cp x $AP2_RUN_DIR/{..,a}/config.yaml",
                    # `//` and `/./` read as `/`, so they do not hide the top level.
                    "rm $AP2_RUN_DIR//*", "rm $AP2_RUN_DIR/./settings*",
                    # A glob past 64 characters at the top level is denied outright.
                    "rm $AP2_RUN_DIR/" + "*" * 65, "rm $AP2_RUN_DIR/settings-" + "?" * 60 + "*",
                    # A path part cut at a quote, comma or paren that a quote made text (Python's `open('...', 'w')`).
                    "python3 -c \"open('$AP2_RUN_DIR/settings-implement.json', 'w').write('NEW')\"",
                    f"python3 -c \"open('{RUN}/settings-implement.json', 'w').write('NEW')\"",
                    "python3 -c \"open('$AP2_RUN_DIR/settings-review.json','w')\"",
                    'python3 -c "open(\\"$AP2_RUN_DIR/settings-pr.json\\", \\"w\\")"',
                    "python3 -c \"p='$AP2_RUN_DIR/..';open(p+'/config.yaml','w')\""], deny=True)
        # The settings files live at the run directory's top level only: a glob below it is fine.
        self.check(["rm $AP2_RUN_DIR/media/*.png", "cp a.png $AP2_RUN_DIR/media/shot-?.png",
                    "rm -f $AP2_RUN_DIR/media/*", "cp $AP2_RUN_DIR/media/* /tmp/out/"], deny=False)

    def test_the_roles_are_the_ones_workers_has(self):
        self.assertEqual(guard.ROLES, workers_mod.ROLES)

    def test_an_ap2_segment_may_name_the_data_folder_but_not_redirect_into_it(self):
        self.check([f"ap2 evidence add C1 {DATA}/sessions/C1/a.png", f"ap2 card show C1 --data {DATA}",
                    "ap2 card list > $AP2_RUN_DIR/cards.txt",
                    # ap2 by its full path (the worker prompt's park command): that path is no write target.
                    f"{PKG}/bin/ap2 card show C1 > $AP2_RUN_DIR/card.json",
                    f"{PKG}/bin/ap2 card list 2> $AP2_RUN_DIR/err.txt",
                    f"{PKG}/bin/ap2 card add --title 'a -> b'",
                    f"{PKG}/bin/ap2 worker park v2 C1 pr --note \"limit hit at $(date +%H:%M); will rm tmp after\"",
                    # Only a redirect's target counts, not any `>` in the segment.
                    "ap2 card list --data \"$AP2_DATA\" > /tmp/cards.json", "ap2 card list --data $AP2_DATA 2>&1",
                    "ap2 card add --data $AP2_DATA --title 'a -> b' >/dev/null"], deny=False)
        self.check([f"ap2 card list > {DATA}/config.yaml", f"{PKG}/bin/ap2 card list >> $AP2_DATA/items/x",
                    f"{PKG}/bin/ap2 card list >> $AP2_DATA/x", f"cp x {PKG}/bin/ap2",
                    "ap2 card list >& $AP2_DATA/x", "ap2 card list &> $AP2_DATA/x", "ap2 card list <> $AP2_DATA/x",
                    "ap2 card list 2>$AP2_DATA/x", 'ap2 card list >"$AP2_"DATA/x', "ap2 card list >| $AP2_DATA/x",
                    "ap2 card list &>> $AP2_DATA/x"], deny=True)

    def test_no_run_directory_means_no_exemption(self):
        env = {k: v for k, v in WORKER.items() if k != "AP2_RUN_DIR"}
        self.check([f"echo {{}} > {RUN}/question.json", "cp x $AP2_RUN_DIR/../../config.yaml"], deny=True, env=env)

    def test_the_deny_names_ap2(self):
        out = guard.decide(bash(f"echo x > {DATA}/config.yaml"), WORKER)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertIn("ap2", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_no_opinion_outside_a_v2_worker_or_on_another_tool(self):
        cmd = bash(f"echo x > {DATA}/config.yaml")
        for name, env in {"user session": {"AP2_DATA": DATA}, "v1 worker": {"AUTOPILOT_ROLE": "worker",
                                                                            "AP2_DATA": DATA},
                          "empty role": dict(WORKER, AP2_ROLE="")}.items():
            with self.subTest(name):
                self.assertIsNone(guard.decide(cmd, env))
        for payload in ({"tool_name": "Edit", "tool_input": {"file_path": f"{DATA}/config.yaml"}},
                        {"tool_name": "Write", "tool_input": {"file_path": f"{DATA}/config.yaml",
                                                              "content": "> x"}},
                        {"tool_name": "Write", "tool_input": {"command": f"echo x > {DATA}/config.yaml"}},
                        {"tool_name": "Other", "tool_input": {"command": f"echo x > {DATA}/config.yaml"}},
                        {"tool_name": "Bash"}, {"tool_name": "Bash", "tool_input": "x"},
                        {"tool_name": "Bash", "tool_input": {"command": 5}}, {}):
            with self.subTest(payload=payload):
                self.assertIsNone(guard.decide(payload, WORKER))

    def fast(self, call, *args, limit: float = 2.0):
        """`call(*args)`, asserted to take well under the hook's 5 s timeout (`limit` seconds). CPU time, not wall
        time: the point is that each step is linear (the quadratic shapes took 4-14 s), and a loaded machine does not
        change CPU time."""
        t = time.process_time()
        out = call(*args)
        self.assertLess(time.process_time() - t, limit)
        return out

    def test_a_command_too_long_to_check_is_denied_when_it_names_the_data_folder(self):
        pad = "dd " * (guard.MAX_COMMAND // 3 + 1)
        self.assertTrue(denied(self.fast(guard.decide, bash(f"{pad} > {DATA}/config.yaml"), WORKER)))
        self.assertIsNone(self.fast(guard.decide, bash(f"{pad} > /tmp/x"), WORKER))
        self.assertTrue(denied(self.fast(guard.decide, bash(f'{pad} > "$AP2_"DATA/config.yaml'), WORKER)))
        # Just under the cap, the slowest shape the write pattern has still answers well inside the 5 s timeout.
        for shape in ("dd ", "open(", "sed -a "):
            body = (shape * (guard.MAX_COMMAND // len(shape)))[:guard.MAX_COMMAND - 40]
            with self.subTest(shape):
                self.fast(guard.decide, bash(f"{body} {DATA}/x"), WORKER)

    def test_a_payload_past_the_read_cap_is_denied_when_it_is_a_bash_call(self):
        """`main` reads MAX_STDIN + 1 bytes, so a write past them is never seen: a v2 worker's Bash call that long is
        denied outright (no real command is 1 MiB)."""
        raw = (b'{"tool_name": "Bash", "tool_input": {"command": "' + b"a " * (guard.MAX_STDIN // 2)
               + b'> \\"$AP2_\\"DATA/config.yaml"}}')
        self.assertLessEqual(guard.MAX_STDIN, 1 << 20)
        read = raw[:guard.MAX_STDIN + 1]  # exactly what main reads
        self.assertNotIn(b"AP2_", read)
        self.assertTrue(denied(guard._judge(read, WORKER)))
        self.assertIsNone(guard._judge(read.replace(b'"Bash"', b'"Grep"'), WORKER))
        self.assertIsNone(guard._judge(read, dict(WORKER, AP2_ROLE="")))
        # The slowest fallback (a payload too deep to parse is judged by it): a quote-split spelling at its end.
        self.assertTrue(denied(self.fast(guard._fallback, raw, WORKER)))
        self.assertIsNone(self.fast(guard._fallback, raw.replace(b"$AP2_", b"$NOPE_"), WORKER))
        # And the largest command `decide` sees after a parse, with the same spelling (the text search finds it), and
        # with none (it reads 1 MiB as words, about 0.4 s here): a wider bound than the 64 KiB shapes', still well
        # inside the 5 s timeout.
        self.assertTrue(denied(self.fast(guard.decide, bash("a " * (guard.MAX_STDIN // 2 - 40) + '> "$AP2_"DATA/c'),
                                         WORKER, limit=3.0)))
        self.assertIsNone(self.fast(guard.decide, bash("a " * (guard.MAX_STDIN // 2 - 40) + '> "$NOPE_"DATA/c'),
                                    WORKER, limit=3.0))
        # 1 MiB of backticks with that spelling at the end, or with none (then read as words): each in well under a
        # second, and the fallback (which runs with no timer) reads no words.
        for unit in ("`}` ", "`x` ", "`` ", "`}`", "``"):
            cmd = unit * ((guard.MAX_STDIN - 200) // len(unit)) + '> "$AP2_"DATA/items/C1.yaml'
            with self.subTest(unit=unit):
                # 1.5 s: measured 0.22-0.52 s CPU on a loaded machine, so a slowdown of about three times is seen.
                self.assertTrue(self.fast(guard._Marks(WORKER).names, cmd, limit=1.5))
                self.assertFalse(self.fast(guard._Marks(WORKER).names, cmd.replace("$AP2_", "$NOPE_"), limit=1.5))
                raw = json.dumps(bash(cmd)).encode()
                self.assertTrue(denied(self.fast(guard._fallback, raw, WORKER, limit=0.2)))
                self.assertIsNone(self.fast(guard._fallback, raw.replace(b"$AP2_", b"$NOPE_"), WORKER, limit=0.2))

    def test_each_parser_shape_at_the_cap_answers_fast(self):
        """The shapes that were slow (a digit run before a redirect, a long `sed -x -x ...`, a word with the run
        directory many times) and a long run of each construct the parser tracks, each just under MAX_COMMAND."""
        size, tail = guard.MAX_COMMAND - 40, " > $AP2_DATA/cards.yaml"

        def fill(unit, head=""):
            return head + (unit * (size // len(unit)))[:size - len(head)] + tail

        def pad(head):  # `head`, then `a` up to MAX_COMMAND
            return head + "a" * (guard.MAX_COMMAND - len(head))

        shapes = {"digits": fill("1", "echo "), "sed": fill("-sed ", "sed "),
                  "run dir": "cp x " + (RUN + "/a/") * ((size - 5) // (len(RUN) + 3)) + tail,
                  "$(": fill("$(echo "), '"$(': fill('"$(a '), "$'": fill("$'a\\'b' "), "${": fill("${x:-"),
                  "backticks": fill("`a #` "), "&": fill("a & "), "((": fill("((a) ) "), "$((": fill("$((a<<b)) "),
                  "#": fill("a\\ #"), ">|": fill(">|a "), "$[": fill("$[a<<b] "), "$[[": fill("[", "echo $["),
                  '"`': fill('"`a #`" '), "\\newline": fill("a\\\n"), "$$'": fill("$$'a' "),
                  "${;": fill("${x:-;"), "((|": fill("((a|b)) "), "heredoc \\": fill("a\\\n", "cat <<E\n"),
                  "ap2 >": fill("> $AP2_DATA ", "ap2 "), "[": "cat $AP2_RUN_DIR/" + "[" * (size - 20) + "/x",
                  "*": "cat $AP2_RUN_DIR/" + "*[" * ((size - 20) // 2),
                  '"${': fill('"${x:-'), '"${"': fill('"${x:-"'), '"${\'': fill("\"${x:-'}' "),
                  '"$$(': fill('"$$(a '), '"$((': fill('"$((a '), '"$(() )': fill('"$((a) ) ', "ap2 "),
                  "$( cut": fill('"$(a; '), "$( cut ap2": fill('"$(ap2 x; '), "${ $( cut": fill('"${x:-$(a; '),
                  "keywords": fill("do "), "keywords ap2": "do " * ((size - 10) // 3) + "ap2 x" + tail,
                  "xargs": fill("-n ", "xargs "), "$( ap2": fill(f'"$({PKG}/bin/ap2 '),
                  "$( ;": fill("$(a; b) "), "` ;": fill("`a; b` "), '"$(( ;': fill('"$((a; b) ) '),
                  "(( ;": fill("((a; b) ) "), '"$(( open': fill('"$((a) "'),
                  "(( nested": "(( $( " * (size // 12) + "x" + ") ) )" * (size // 12) + tail,
                  "heredoc $(": fill("cat <<E $(\n"), "runners": fill("env A=1 nohup nice -n 1 command "),
                  "case )": fill("(a) ", "ap2 "), ")": fill(") ", "ap2 "), ") >": fill(") 2>a <b ", "ap2 "),
                  ") >>": "ap2 ) " + "2>a " * ((size - 10) // 4) + tail, "run dir.": fill("$AP2_RUN_DIR."),
                  "{run dir": fill("{$AP2_RUN_DIR/,"), "` open": fill("`", "cat "),
                  "{ quoted run dir": fill("'{' $AP2_RUN_DIR/a,b}"), "{ run dir,": fill("{a $AP2_RUN_DIR/a,b "),
                  "extglob": fill("a@(b "), "extglob )": fill("a@(b)"), "extglob run dir": fill("$AP2_RUN_DIR/@(a)"),
                  "extglob on": fill("$AP2_RUN_DIR/a ", "shopt -s ext\\glob\n"),
                  "heredoc $'": fill("cat <<$'\\x4f\\105\\cA' x\n"), 'heredoc $"': fill('cat <<$"E" x\n'),
                  "heredoc $' open": fill("\\'", "cat <<$'"), "func $'": fill("$'a'$$'b'", "ap2 "),
                  "extglob nested": fill("@("), "extglob $(": fill("@($(a) "),
                  "(( deep": ("(( " * (size // 9) + "x" + " )" * (2 * (size // 9)))[:size] + tail,
                  "(( $$' delim": pad('"$$(x)" ' + "((" * 180 + "$(<<$$'x\\' )" + " )" * 360 + " "),
                  "${ quoted {": fill('"${x:-"{'), "${ {": fill('"${x:-{'),
                  "rebind": fill("f() { x; }; ap2 $AP2_DATA "),
                  "alias": fill("alias "), "=(": fill("=(a ", "ap2 "), ">!": fill(">! a ", "ap2 "),
                  "always": fill("always ", "ap2 "), "zsh group": fill("$AP2_RUN_DIR/(a) "),
                  "run dir twice": fill('"$AP2_RUN_DIR/a $AP2_RUN_DIR/b '), "( mid": fill("a("),
                  "()": fill("() "), "bash -c {": fill("{a,b} ", "bash -c '"), "[ top": fill("$AP2_RUN_DIR/[a "),
                  "extglob @ seen": fill("a@(b) "), "shells": fill("bash -c "), "eval": fill("eval $AP2_DATA "),
                  "shells quoted": fill("'", "bash -c " + "'bash -c " * 50),
                  # A shell's script read again, in both `$$(` readings, with many mentions in it.
                  "sh $$( words": pad('sh sh "$$(x)" ' + "$$(x)$AP2_RUN_DIR " * ((size - 20) // 18)),
                  "sh $$( quoted": pad("sh sh '" + "$$(x)$AP2_RUN_DIR " * ((size - 20) // 18) + "\"$$(x)\"'"),
                  "sh $$( run dir": pad('sh sh "$$(x)" ' + '$AP2_RUN_DIR/a"$$(x)" ' * ((size - 20) // 22)),
                  "soft ${'": fill("'$(a) ", '"${x:-'), "soft ((": fill("'$(a)' ", "(( "),
                  "soft $[": fill("'$(a)' ", "ap2 $[ "), "zsh ${'": fill("\"${x:-'}\" $AP2_RUN_DIR/a "),
                  "${(": fill('"${(e)x:-a} ', "ap2 "), "setopt": fill("setopt x; set -o y; "),
                  "set -o": fill("-euo pipefail ", "set "), ">&|": fill(">&| a ", "ap2 "),
                  "| sh": fill("echo 'tee $AP2_RUN_DIR/a' | sh | "), "| into sh": fill("echo 'a' | ") + "| sh",
                  "quoted globs": fill("[x]{a,b}*?(y) $AP2_RUN_DIR/", "echo '"),
                  "${ ` \\": fill("`a \\* b` ", '"${x:-'), "rebound run dir $": fill("ls $AP2_RUN_DIR/$f ", "source x; "),
                  "all readings": fill("\"${x:-'$(a)'}\" $$(x)$AP2_RUN_DIR ", 'sh "$$(x)" "${x:-\'}" '),
                  # Many tiny segments, read in all three readings, a backtick read again with a level taken out.
                  "all readings tiny": ((('sh "$$(x)" "${x:-\'}" ' + '"${x:-`sh '
                                          + ('$(a|)' * size)[:int(0.66 * size)] + '\\x`}" ') + "$(a)" * size)
                                        [:guard.MAX_COMMAND]),
                  "c\\p": fill("c\\p '' "), "sed -e": fill("-e x ", "sed "), "sed sed": fill("sed "),
                  "} cmd": fill("} 2>a} ", "ap2 { "), "}}": fill("a} ", "ap2 "),
                  "rebind words": fill("hash x "), "${HOME:": fill("${HOME:-${HOME:?"),
                  "${HOME: rel": fill("${HOME:?}/Documents/Assistant/ "), "set -E": fill("-Eeua ", "set "),
                  "zsh (": fill("(a) ", "zsh -c '"), "${( quoted": fill("'${(a)' "),
                  "heredoc ${(": fill("${(a)\n", "cat <<'E'\n"), "bash -c $?": fill("echo $? ", "bash -c '$AP2_RUN_DIR "),
                  "| sh $?": fill("echo $AP2_RUN_DIR/a $? ", "echo '") + "' | sh",
                  "sh -c args": fill('"$AP2_RUN_DIR/a" ', "sh -c 'cp \"$@\"; echo ok' _ "),
                  "sh -c flags": fill("-o x ", "bash -e ") + " -c 'cp a b'",
                  "| sh fed": fill("x ", "touch a ';' ") + " | sh", "sh}": fill("a | sh} "),
                  "open( quoted": fill("$AP2_RUN_DIR/a', 'w'),(", "python3 -c \"open('") + '"',
                  "rebind loose": fill("x) hash ", "case x in "), "rebind $": fill("$e hash "),
                  # Process substitutions, redirect words, assignments, glued words, `{}` pairs, `maybe` words.
                  "<(": fill("<(a "), ">(": fill(">(a "), "=( word": fill("=(a "), "<( ap2": fill("<(ap2 x ", "cat "),
                  "ops": fill(">a 2>&1 <b <<<c "), "ops hash": fill(">a hash "), "assign+": fill("x+=1 a[1]=1 "),
                  "glued": fill("x=$(a)/b "), "glued tick": fill("x=`a`/b "), "{}": fill("{} -I{} a/{} "),
                  "maybe": fill("$(a) b "), "repeat": fill("repeat 1+1 "), "command --": fill("command -- "),
                  # The ap2 trust check: many ap2 segments, `ap2` names, name tables, rebinding words.
                  "ap2 segs": fill("ap2 x --data $AP2_DATA; "), "ap2 names": fill("ap2 x ap2 ap2=x "),
                  "ap2 hash": fill("ap2 x; hash "), "tables": fill(": ${commands[a]::=b} ${PATH::=c} "),
                  "ap2 dot": fill("x=1 . a; ap2 "), "ap2 heredoc": fill("${commands[a]}\n", "ap2 x <<E\n"),
                  # A fed printer, and quoted closers ending a file name in code.
                  "fed printer": fill("echo a } always | ") + "| sh",
                  "perl q{": fill("q{$AP2_RUN_DIR/a} ", 'perl -e "') + '"',
                  # Backticks: empty ones in a run, one-word ones, inside a shell's quoted script in every reading.
                  "`` run": fill("``"), "`x`": fill("`x`"), "`}` ": fill("`}` "),
                  "`` in sh -c": pad('sh "$$(x)" "${x:-\'}" bash -c "sh -c \'' + "``" * ((size - 60) // 2)
                                    + '\' $AP2_DATA/y" $AP2_DATA/x'),
                  # Heredocs a shell reads, unquoted bodies with substitutions, and both at once.
                  "heredoc fed": fill("cp a $AP2_RUN_DIR/x\n", "bash <<'E'\n"),
                  "heredoc fed f()": fill("f() { cp \"$1\" $AP2_RUN_DIR/x; }\n", "cat <<'E' | bash -s\n"),
                  "heredoc body $(": fill("$(a)\n", "cat <<E\n"), "heredoc body `": fill("`a` it's\n", "cat <<E\n"),
                  "heredoc body $( open": fill("$(\n", "cat <<E\n"),
                  "heredoc body nested": fill("$(cat <<F\nx $(a)\nF\n)\n", "cat <<E\n"),
                  "heredoc fed $(": fill("$(a) $AP2_RUN_DIR/x\n", "bash <<E\n"),
                  "heredocs fed": fill("bash <<E\nx\nE\n"), "heredocs on a line": fill("<<E ", "bash ") + "\nx\nE",
                  # Decoded quotes, zsh flags, a quoted end after the run directory, search-path words.
                  "$' decoded": fill("$'\\x63p' "), "$' raw": fill("$'\\u0063' "), '$"': fill('c$"p" '),
                  "${=": fill("${=AP2_DATA} "), "run dir '": fill("'$AP2_RUN_DIR' "),
                  "path=": fill('"path=" path ', "rg "), "path setter": fill("path PATH= ", "read "),
                  # Unsure heredoc delimiters, a shell's options, brace-less zsh flags, text before the run
                  # directory, NUL and `\\u` quotes, setters as arguments, a fed body that rebinds.
                  "<<$(": fill("cat <<$(x) a\n"), "<<${": fill("<<${x:-a b} "), "<<`": fill("<<`a b` "),
                  "sh options": fill("-o x -e ", "bash ") + " <<E\nx\nE", "sh -s --": fill("-c ", "bash -s -- "),
                  "$=": fill("$=AP2_DATA $~HOME/x "), "prose run dir": fill('"in $AP2_RUN_DIR now, ', "echo "),
                  "/.. run dir": fill("/..$AP2_RUN_DIR. x=$AP2_RUN_DIR' "),
                  "/./ run": "echo x" + "/." * ((size - 40) // 2) + "$AP2_RUN_DIR' " + tail,
                  "$'\\0": fill("$'\\0a\\'b' "), "$'\\u": fill("$'\\u2014\\U0001F600' "),
                  "setter args": fill("set export path ", "git grep "),
                  "fed rebind": fill("source x; f() { :; }\n", "bash <<'E'\n"),
                  "$x verb": fill("$x'c'p $=y$'\\x63' "),
                  # `$'...'` escapes the two shells read differently, and a `$"..."` heredoc delimiter.
                  "$' unshared": fill("$'a\\d' $'\\C-@x' x$'\\0' "), "$' alone NUL": fill("$'\\0' "),
                  '<<$"': fill('cat <<$"E" x\n'),
                  "$' zsh prefixes": "cp a $'" + "\\C-\\M-" * ((size - 20) // 6) + "'" + tail,
                  "$' readings": fill("$'\\d' $'\\x41' ")}
        # The slowest shapes (a shell's script read again in both `$$(` readings, about 0.3 s here) get a bound a
        # few times their time (1 s), so a slowdown of that much is seen well before the 2 s deadline; the rest
        # keep 2 s.
        limits = {"sh $$( words": 1.0, "sh $$( run dir": 1.0, "sh $$( quoted": 1.0}
        for name, cmd in shapes.items():
            with self.subTest(name):
                self.assertLessEqual(len(cmd), guard.MAX_COMMAND)
                self.fast(guard.decide, bash(cmd), WORKER, limit=limits.get(name, 2.0))

    def test_a_judgement_past_the_deadline_is_a_deny_when_it_names_a_guarded_path(self):
        if not hasattr(guard.signal, "setitimer"):
            self.skipTest("no setitimer")

        def forever(payload, env):
            while True:
                pass

        with mock.patch.object(guard, "decide", forever), mock.patch.object(guard, "DEADLINE", 0.2):
            # Each call on its own, under the 2 s default: it fails if the patched deadline were not the one used.
            t = time.monotonic()
            self.assertTrue(denied(guard._judge(json.dumps(bash(f"echo x > {DATA}/config.yaml")).encode(),
                                                WORKER)))
            self.assertLess(time.monotonic() - t, 1.9)
            t = time.monotonic()
            self.assertIsNone(guard._judge(json.dumps(bash("echo x > /tmp/x")).encode(), WORKER))
            self.assertLess(time.monotonic() - t, 1.9)
        self.assertEqual(guard.signal.getitimer(guard.signal.ITIMER_REAL), (0.0, 0.0))  # the timer is off again
        # A judgement that ends in time (the real `decide`) also turns the timer off and puts the old handler back.
        def mine(signum, frame):
            pass

        self.addCleanup(guard.signal.signal, guard.signal.SIGALRM, guard.signal.signal(guard.signal.SIGALRM, mine))
        self.assertIsNone(guard._judge(json.dumps(bash("echo x > /tmp/x")).encode(), WORKER))
        self.assertEqual(guard.signal.getitimer(guard.signal.ITIMER_REAL), (0.0, 0.0))
        self.assertIs(guard.signal.getsignal(guard.signal.SIGALRM), mine)

    def test_the_harmless_redirects_match_v1(self):
        v1 = re.compile(r"\d*>&\d+|&>>?\s*/dev/null|\d*>>?\s*/dev/null")
        rnd = random.Random(7)
        texts = ["2>&1>/dev/null", "12>&3", "x2>/dev/null", "&>>/dev/null", "1>&x", "2>&-", ">& 2", "9" * 50]
        texts += ["".join(rnd.choice(["1", "2", ">", "&", " ", "/dev/null", "x", ">>"]) for _ in range(12))
                  for _ in range(3000)]
        for text in texts:
            with self.subTest(text=text):
                self.assertEqual(guard._harmless(text), v1.sub(" ", text))

    def test_the_write_forms_match_v1(self):
        """`_writes` finds what v1's one `_WRITE` regex finds, for the forms it splits out and the near misses. The
        sed form is a superset of v1's: v1 stops at the first word after `sed` that is no `-x` flag (`sed -e x -i f`
        is no match there), while `_writes` finds an in-place flag anywhere after the `sed` in its segment, so for
        sed only v1's matches are pinned (each one is found), plus the forms only `_writes` finds."""
        v1 = re.compile(r"(>>?|\btee\b|\bsed\s+(-[a-zA-Z]*\s+)*-i|\bperl\s+-[a-zA-Z]*i|\bcp\b|\bmv\b|\brm\b|"
                        r"\btruncate\b|\bln\b|\binstall\b|\bdd\b[^|;&]*\bof=|\bopen\([^)]*,\s*['\"][wax+]|\.write\(|"
                        r"write_text|\bchmod\b|\btouch\b)")
        for text in ("dd if=a of=b", "dd if=a & of=b", "of=b dd", "ddx of=b", "dd dd of=b", "x dd y of=z",
                     "open('f', 'w')", "open('f')", "open(f) , 'w'", "open(f, \"a\")", "open(f,  '+')",
                     "open(open(f), 'w')", "f.write(x)", "sed -n -i s/a/b/ f", "perl -pi -e x f", "cat f", "",
                     "sed -in f", "sed  -n\t-i f", "xsed -i f", "sed -sed -i", "a-sed -i", "sed -n f"):
            with self.subTest(text=text):
                self.assertEqual(guard._writes(text), bool(v1.search(text)))
        for text in ("sed -n f -i", "sed -1 -i", "sed -e x -i f", "sed -ni f", "sed --in-place f", "sed -E -e x -i.b f",
                     "sed -I '' f"):
            with self.subTest(text=text):
                self.assertTrue(guard._writes(text))
        for text in ("sed --expression=s/i/x/ f", "sed -e -x f", "sed", "sed-i f", "cat -i f"):
            with self.subTest(text=text):
                self.assertFalse(guard._writes(text))
        # The sed form, which `_writes` checks token by token, on random text: every v1 match is found.
        sed = re.compile(r"\bsed\s+(-[a-zA-Z]*\s+)*-i")
        rnd = random.Random(11)
        for _ in range(5000):
            text = "".join(rnd.choice(["sed", " ", "-", "i", "n", "x", "\t", "-i", "_", "1"]) for _ in range(10))
            with self.subTest(text=text):
                if v1.search(text):
                    self.assertTrue(guard._writes(text), sed.search(text))

    def test_the_package_root_is_the_one_workers_uses(self):
        self.assertEqual(guard.package_root(), workers_mod.package_root())


class CommandTest(unittest.TestCase):
    """The real `bin/ap2 hook-guard`, with an empty temp HOME."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-guard-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.data = self.tmp / "data"

    def env(self, **extra) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AUTOPILOT_", "AP2_"))}
        env.update(HOME=str(self.home), **extra)
        return env

    def worker(self) -> dict:
        return self.env(AP2_ROLE="implement", AP2_CARD="C1", AP2_DATA=str(self.data),
                        AP2_RUN_DIR=str(self.data / "sessions" / "C1"))

    def run_hook(self, stdin: bytes, env=None, args=(), **kw):
        return subprocess.run([sys.executable, AP2, "hook-guard", *args], capture_output=True, input=stdin,
                              env=env if env is not None else self.worker(), timeout=60, **kw)

    def test_deny_and_allow(self):
        r = self.run_hook(json.dumps(bash(f"echo x > {self.data}/config.yaml")).encode())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(denied(json.loads(r.stdout)))
        for cmd in (f"cat {self.data}/config.yaml", "echo x > /tmp/y", f"echo x > {self.data}/sessions/C1/q.json"):
            with self.subTest(cmd=cmd):
                r = self.run_hook(json.dumps(bash(cmd)).encode())
                self.assertEqual((r.returncode, r.stdout), (0, b""), r.stderr)
        r = self.run_hook(json.dumps(bash(f"echo x > {self.data}/config.yaml")).encode(), env=self.env())
        self.assertEqual((r.returncode, r.stdout), (0, b""))  # not a v2 worker

    def test_errors_never_break_the_session(self):
        for name, stdin in {"invalid JSON": b"{not json", "a JSON list": b"[1, 2]", "empty stdin": b"",
                            "not UTF-8": b"\xff\xfe{", "a string": b'"x"'}.items():
            with self.subTest(name):
                r = self.run_hook(stdin)
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""))

    def test_a_payload_too_deep_to_parse_that_names_the_data_folder_is_denied(self):
        deep = ('{"tool_name": "Bash", "tool_input": {"command": "echo x > ' + str(self.data) + '/config.yaml", '
                '"pad": ' + "[" * 100000 + "]" * 100000 + "}}").encode()
        r = self.run_hook(deep)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(denied(json.loads(r.stdout)))
        r = self.run_hook(deep.replace(str(self.data).encode(), b"/tmp/elsewhere"))
        self.assertEqual((r.returncode, r.stdout), (0, b""))
        # A data folder spelled with a `$"..."` around its last letter: found once the `$` before a quote is taken out.
        data = str(self.data)
        cmd = json.dumps(f'echo x > {data[:-1]}$"{data[-1]}"/config.yaml')
        split = ('{"tool_name": "Bash", "tool_input": {"command": ' + cmd + ', "pad": ' + "[" * 100000
                 + "]" * 100000 + "}}").encode()
        r = self.run_hook(split)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(denied(json.loads(r.stdout)))

    def test_a_bash_payload_past_the_read_cap_is_denied(self):
        """The write sits past the bytes `main` reads, so it is never seen: the length alone denies the call."""
        cmd = "echo " + "a " * (guard.MAX_STDIN // 2) + f"> {self.data}/config.yaml"
        r = self.run_hook(json.dumps(bash(cmd)).encode())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(denied(json.loads(r.stdout)))
        r = self.run_hook(json.dumps({"tool_name": "Grep", "tool_input": {"pattern": cmd}}).encode())
        self.assertEqual((r.returncode, r.stdout), (0, b""))

    def test_a_mebibyte_of_backticks_answers_well_inside_the_timeout(self):
        """The real hook on a 1 MiB payload of backticks with a quote-split data-folder spelling at its end (denied),
        or with a spelling of nothing guarded (allowed, after reading all of it as words): each in under 2.5 s of CPU
        (best of three; measured 0.49-0.56 s, interpreter start included, on a loaded machine), inside the 5 s
        timeout."""
        for unit in ("`}` ", "`x` ", "`` ", "`}`"):
            for name, deny in ((f'"{str(self.data)[:-1]}"{str(self.data)[-1]}', True), ('"/tmp/no"where', False)):
                cmd = unit * ((guard.MAX_STDIN - 400) // len(unit)) + f"> {name}/x"
                payload = json.dumps(bash(cmd)).encode()
                self.assertLessEqual(len(payload), guard.MAX_STDIN)
                best = None
                for _ in range(3):
                    before = resource.getrusage(resource.RUSAGE_CHILDREN)
                    r = self.run_hook(payload)
                    after = resource.getrusage(resource.RUSAGE_CHILDREN)
                    cpu = after.ru_utime - before.ru_utime + after.ru_stime - before.ru_stime
                    best = cpu if best is None else min(best, cpu)
                with self.subTest(unit=unit, deny=deny):
                    self.assertEqual(denied(json.loads(r.stdout)) if r.stdout else False, deny, r.stderr)
                    self.assertLess(best, 2.5)

    def test_a_closed_stdout_still_exits_0(self):
        read_end, write_end = os.pipe()
        os.close(read_end)
        try:
            r = subprocess.run([sys.executable, AP2, "hook-guard"], stdout=write_end, stderr=subprocess.PIPE,
                               input=json.dumps(bash(f"echo x > {self.data}/config.yaml")).encode(),
                               env=self.worker(), timeout=60)
        finally:
            os.close(write_end)
        self.assertEqual((r.returncode, r.stderr), (0, b""))

    def test_it_writes_nothing(self):
        r = self.run_hook(json.dumps(bash(f"echo x > {self.data}/config.yaml")).encode())
        self.assertTrue(denied(json.loads(r.stdout)))
        self.assertFalse(self.data.exists())
        self.assertEqual(list(self.home.iterdir()), [])

    def test_it_stays_light(self):
        code = ("import json, sys; sys.path.insert(0, sys.argv[1]); from assistant import guard; "
                "guard.main([], sys.stdin); "
                "print(json.dumps(sorted(m for m in sys.modules if m.startswith(('assistant', 'autopilot')))), "
                "file=sys.stderr)")
        r = subprocess.run([sys.executable, "-c", code, str(LIB)], capture_output=True,
                           input=json.dumps(bash(f"echo x > {self.data}/config.yaml")).encode(), env=self.worker(),
                           timeout=60)
        self.assertTrue(denied(json.loads(r.stdout)), r.stderr)
        self.assertEqual(json.loads(r.stderr), ["assistant", "assistant.guard"])

    def test_the_real_command_stays_light(self):
        """`bin/ap2 hook-guard` routes before `bin/ap2` imports `cli`, so none of the heavy modules load on a
        worker's Bash call."""
        r = subprocess.run([sys.executable, "-X", "importtime", AP2, "hook-guard"], capture_output=True,
                           input=json.dumps(bash(f"echo x > {self.data}/config.yaml")).encode(), env=self.worker(),
                           timeout=60)
        self.assertEqual(r.returncode, 0)
        self.assertTrue(denied(json.loads(r.stdout)))
        imported = {line.rsplit("|", 1)[-1].strip() for line in r.stderr.decode().splitlines()
                    if line.startswith("import time:")}
        self.assertIn("assistant.guard", imported)  # the -X importtime list was read
        for name in ("assistant.cli", "assistant.workers", "assistant.store", "assistant.config", "autopilot.guard",
                     "autopilot.cli"):
            with self.subTest(name):
                self.assertNotIn(name, imported)

    def test_the_ap2_command_routes_before_the_full_parser(self):
        r = self.run_hook(json.dumps(bash(f"echo x > {self.data}/config.yaml")).encode(), args=["--whatever"])
        self.assertEqual((r.returncode, r.stdout), (0, b""))  # an unknown argument: no opinion, still exit 0
        r = subprocess.run([sys.executable, AP2], capture_output=True, env=self.env(), timeout=60)
        self.assertIn("hook-guard", r.stderr.decode())

    def test_cli_main_routes_it_too(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); from assistant import cli; "
                "sys.exit(cli.main(['hook-guard']))")
        r = subprocess.run([sys.executable, "-c", code, str(LIB)], capture_output=True,
                           input=json.dumps(bash(f"echo x > {self.data}/config.yaml")).encode(), env=self.worker(),
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(denied(json.loads(r.stdout)))


if __name__ == "__main__":
    unittest.main()
