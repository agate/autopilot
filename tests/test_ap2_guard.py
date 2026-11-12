"""`ap2 hook-guard` (design 12, workers): the PreToolUse hook on Bash that denies a v2 worker's shell write into the
data folder or the package. Unit tests call `guard.decide` over a table of commands; the command tests run the real
`bin/ap2 hook-guard` with a payload on stdin and a temp HOME."""
from __future__ import annotations

import json
import os
import random
import re
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
        """No heredoc inside `${...}` or `$[...]`; the delimiter keeps its backticks; a body line that ends in an odd
        number of backslashes joins the next one, as bash joins them; inside `$(...)` a `EOF)` line ends the body."""
        self.check(['echo ${x/<</y}\nrm -rf \\\n"$AP2_DATA/victim"',
                    'echo $[1<<2]\nrm -rf \\\n"$AP2_DATA/victim"',
                    'cat <<a`b`\nhi\na`b`\nrm -rf \\\n"$AP2_DATA/victim"',
                    'cat <<EOF\nrm -rf \\\n"$AP2_DATA/victim"\nEOF',
                    'bash <<EOF\nrm -rf \\\n\\\n$AP2_DATA/victim\nEOF',
                    'x=$(cat <<EOF\nhi\nEOF)\ncp a $AP2_DATA/config.yaml'], deny=True)
        self.check(['cat <<EOF\nls \\\\\n$AP2_DATA/x\nEOF',
                    'x=$(cat <<EOF\nhi\nEOF)\nls $AP2_DATA; echo "$x" > /tmp/y',
                    'x=$(cat <<-EOF\nhi\n\tEOF)\nls $AP2_DATA; echo "$x" > /tmp/y'], deny=False)
        self.assertEqual(guard._segments("cat <<a`b`\nhi\na`b`\nz"), ["cat <<a`b`", "hi", "a`b`", "z"])
        self.assertEqual(guard._segments('cat <<"a\\$b"\nhi\na$b\nz'), ['cat <<"a\\$b"', "hi", "a$b", "z"])
        self.assertEqual(guard._segments("x=$(cat <<E\nhi\nE)\nz"), ["x=$(cat <<E", "hi", "E", ")", "z"])
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

    def test_a_backtick_inside_double_quotes_reads_its_own_quotes(self):
        self.check(['echo "`echo " #"`"; rm -rf $AP2_DATA/items',
                    'git commit -m "`printf "%s #%s" fix 12`"; rm -rf $AP2_DATA/items',
                    'cp "`echo "x;y"`" $AP2_DATA/config.yaml',
                    'echo "`cp a $AP2_DATA/config.yaml`"',
                    'ap2 card note C1 --text "`echo ok`"; cp a $AP2_DATA/config.yaml'], deny=True)
        self.check(['echo "`date +"%F #"`" > /tmp/x; ls $AP2_DATA'], deny=False)
        self.assertEqual(guard._segments('a "`b; c`" d; e'), ["b", " c", 'a "`b; c`" d', " e"])
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
                    f"ap2 status $$[1| cp a {DATA}/config.yaml"],
                   deny=True)
        self.check([f"x=$((a<<b)); cat <<EOF\nit's\nEOF\nls {DATA}/items",
                    f"echo $(( (1+2) << 1 )) $(((1+2)))\ncat <<EOF\nit's\nEOF\nls {DATA}"], deny=False)

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
                    "ap2 card note C1 --data $AP2_DATA --text \"rm step $((n+1)) failed\""], deny=False)
        self.check(["ap2 card note C1 --data $AP2_DATA --text \"$(cp a b)\""], deny=True)

    def test_a_parameter_expansion_of_the_data_folder_is_a_mention(self):
        self.check(["rm -rf \"${AP2_DATA:?}/items\"", "rm -rf \"${AP2_DATA:?}\"/items",
                    "cp a \"${AP2_DATA%/}/config.yaml\"", "echo x > \"${AP2_RUN_DIR%/*}/C2/x\"",
                    # Accepted false deny: a brace after the run directory is never trusted.
                    "echo x > ${AP2_RUN_DIR:?}/x", "echo x > ${AP2_RUN_DIR[0]}/x",
                    'rm -rf "${AP2_DATA?}/items"', 'rm -rf "${AP2_DATA-}/items"', 'cp a "${AP2_DATA#}/config.yaml"',
                    'rm -rf "${AP2_DATA//x/x}/items"',
                    # An array subscript of the variable is the variable too.
                    'rm -rf "${AP2_DATA[0]}/victim"', "rm -rf ${AP2_DATA[@]}", 'rm -rf "${AP2_DATA[*]}/x"',
                    'echo pwned > "${AP2_RUN_DIR[0]}/../../victim/f"'], deny=True)
        self.check(["echo ${AP2_DATA_OLD:-x} > /tmp/x"], deny=False)

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
                    f"echo x > {RUN}"], deny=False)

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
                    "rm $AP2_RUN_DIR//*", "rm $AP2_RUN_DIR/./settings*"], deny=True)
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

    def fast(self, call, *args):
        """`call(*args)`, asserted to take well under the hook's 5 s timeout. CPU time, not wall time: the point is
        that each step is linear (the quadratic shapes took 4-14 s), and a loaded machine does not change CPU time."""
        t = time.process_time()
        out = call(*args)
        self.assertLess(time.process_time() - t, 2.0)
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
        # And the largest command `decide` sees after a parse, with the same spelling.
        self.assertTrue(denied(self.fast(guard.decide, bash("a " * (guard.MAX_STDIN // 2 - 40) + '> "$AP2_"DATA/c'),
                                         WORKER)))

    def test_each_parser_shape_at_the_cap_answers_fast(self):
        """The shapes that were slow (a digit run before a redirect, a long `sed -x -x ...`, a word with the run
        directory many times) and a long run of each construct the parser tracks, each just under MAX_COMMAND."""
        size, tail = guard.MAX_COMMAND - 40, " > $AP2_DATA/cards.yaml"

        def fill(unit, head=""):
            return head + (unit * (size // len(unit)))[:size - len(head)] + tail

        shapes = {"digits": fill("1", "echo "), "sed": fill("-sed ", "sed "),
                  "run dir": "cp x " + (RUN + "/a/") * ((size - 5) // (len(RUN) + 3)) + tail,
                  "$(": fill("$(echo "), '"$(': fill('"$(a '), "$'": fill("$'a\\'b' "), "${": fill("${x:-"),
                  "backticks": fill("`a #` "), "&": fill("a & "), "((": fill("((a) ) "), "$((": fill("$((a<<b)) "),
                  "#": fill("a\\ #"), ">|": fill(">|a "), "$[": fill("$[a<<b] "), "$[[": fill("[", "echo $["),
                  '"`': fill('"`a #`" '), "\\newline": fill("a\\\n"), "$$'": fill("$$'a' "),
                  "${;": fill("${x:-;"), "((|": fill("((a|b)) "), "heredoc \\": fill("a\\\n", "cat <<E\n"),
                  "ap2 >": fill("> $AP2_DATA ", "ap2 "), "[": "cat $AP2_RUN_DIR/" + "[" * (size - 20) + "/x",
                  "*": "cat $AP2_RUN_DIR/" + "*[" * ((size - 20) // 2)}
        for name, cmd in shapes.items():
            with self.subTest(name):
                self.assertLessEqual(len(cmd), guard.MAX_COMMAND)
                self.fast(guard.decide, bash(cmd), WORKER)

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
        """`_writes` finds what v1's one `_WRITE` regex finds, for the forms it splits out and the near misses."""
        v1 = re.compile(r"(>>?|\btee\b|\bsed\s+(-[a-zA-Z]*\s+)*-i|\bperl\s+-[a-zA-Z]*i|\bcp\b|\bmv\b|\brm\b|"
                        r"\btruncate\b|\bln\b|\binstall\b|\bdd\b[^|;&]*\bof=|\bopen\([^)]*,\s*['\"][wax+]|\.write\(|"
                        r"write_text|\bchmod\b|\btouch\b)")
        for text in ("dd if=a of=b", "dd if=a & of=b", "of=b dd", "ddx of=b", "dd dd of=b", "x dd y of=z",
                     "open('f', 'w')", "open('f')", "open(f) , 'w'", "open(f, \"a\")", "open(f,  '+')",
                     "open(open(f), 'w')", "f.write(x)", "sed -n -i s/a/b/ f", "perl -pi -e x f", "cat f", "",
                     "sed -in f", "sed  -n\t-i f", "sed -n f -i", "xsed -i f", "sed -sed -i", "a-sed -i", "sed -1 -i"):
            with self.subTest(text=text):
                self.assertEqual(guard._writes(text), bool(v1.search(text)))
        # The sed form, which `_writes` checks token by token, on random text.
        sed = re.compile(r"\bsed\s+(-[a-zA-Z]*\s+)*-i")
        rnd = random.Random(11)
        for _ in range(5000):
            text = "".join(rnd.choice(["sed", " ", "-", "i", "n", "x", "\t", "-i", "_", "1"]) for _ in range(10))
            with self.subTest(text=text):
                self.assertEqual(guard._writes(text), bool(v1.search(text)), sed.search(text))

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

    def test_a_bash_payload_past_the_read_cap_is_denied(self):
        """The write sits past the bytes `main` reads, so it is never seen: the length alone denies the call."""
        cmd = "echo " + "a " * (guard.MAX_STDIN // 2) + f"> {self.data}/config.yaml"
        r = self.run_hook(json.dumps(bash(cmd)).encode())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(denied(json.loads(r.stdout)))
        r = self.run_hook(json.dumps({"tool_name": "Grep", "tool_input": {"pattern": cmd}}).encode())
        self.assertEqual((r.returncode, r.stdout), (0, b""))

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
