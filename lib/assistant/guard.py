"""`ap2 hook-guard`: the PreToolUse hook on Bash that keeps a v2 worker's shell out of the data folder and the
package (design 8.1, section 12 workers).

`workers.floor()` wires it into every worker's settings after the park hook. For a Bash call from a v2 worker
(`AP2_ROLE` set) it splits the command into segments (newlines, `;`, `&&`, `||`, `|`, `|&`, and a background `&`; not
the `&` of `2>&1`, `&>` or `<&`, nor the `|` of `>|` or zsh's `>&|`) and denies the call when a segment names the
data folder (`AP2_DATA`, its `~` or `$HOME` form, `$AP2_DATA`, `${AP2_DATA}`, `${AP2_DATA:?}`- and
`${AP2_DATA[0]}`-style expansions, zsh's `${=AP2_DATA}`, `${~...}` and `${^...}` and their brace-less `$=AP2_DATA`
forms, a `${HOME:?}`-style one before its path under HOME, `$AP2_RUN_DIR`) or the package and looks like a write
(v1's `_WRITE`, with the redirects that write nowhere removed first, in the text as written and in its words with
quotes removed, so `c\\p` is `cp`, `c$"p"` and `$'\\x63p'` too, a `$'...'` decoded only where bash 3.2 and zsh
decode it alike; a `sed` with `-i` anywhere after it). The commands
inside a `$(...)` or backticks are segments of their own, and the command around them is one segment with each such text
replaced by `_`, so its verb keeps all its words. A segment that runs `ap2` (its first command after keywords such as
`do` or `time`, `NAME=value` words and runners such as `env` or `nohup`, by name or by its full path) may name them
(`ap2` writes the data folder itself); only a redirect into one of them counts as a write (`>!` and `>&|` too),
unless it also runs other shell code (`$(...)`, a backtick, `<(...)`, zsh's `=(...)`, `${(...)` and `always`, `((`,
`$[`, `"$((cmd) )"`, a function named `ap2`, a case pattern `ap2) <cmd>`, a `}` that ends a word and then a command:
zsh's `while { ap2 x } { <cmd> }`). Inside `"${...}"` and arithmetic a `'` hides no `$(...)` or backtick (bash keeps
it only to find the end), and zsh's reading, where such a `'` is a plain character, is judged too. The package's
`bin/ap2` where bash runs it as a command (after keywords, assignments and runners, inside `$(...)` or backticks,
after `xargs`) is no package mention, unless a redirect writes it. A segment whose every data-folder mention (each
one judged up to the next one in its word) is a path under the worker's own run directory (`AP2_RUN_DIR`) with no
`..`, no `$`, no backtick, no `settings-*.json` (the floor itself, Edit-denied too; also in the part up to a quote,
comma, paren, `}`, `]` or `>` a quote made text, as in `python3 -c "open('<run>/x', 'w')"` or Perl's
`q{<run>/x}`; inside a word of prose, JSON or code, not after an `=` or a `..` part, the run directory itself may be
followed by any character that does not go on with its name, `Path('<run>')`, `"results in <run> now"`) and, unquoted
(bash and zsh glob
and brace-expand no quoted character, so JSON or code in quotes is text), no `{`, `}` or `,` when the segment has an
unquoted `{` (not that of `${`: bash brace-expands no other) or a `{` inside a `"${...}"`, no `(` when the segment
has an unquoted `(` inside a word (an `@(`-style extglob, a zsh glob group), no dot glob, and at the top level no `[`
and no glob that matches a floor file (or longer than 64 characters) after it, also with an unquoted `}` at its end
dropped (zsh closes a brace there), is allowed: the worker writes its results there. A command that may turn extglob
on (it holds `extglob`), may change a zsh option (`setopt`, `unsetopt`, `emulate`, `options[...]`, a `set -o` other
than `pipefail` and the like), holds zsh's `${(` (outside single quotes and quoted-delimiter heredoc bodies) or an
unquoted extglob opener outside arithmetic, is denied when it names a guarded path anywhere. `ap2` keeps its
exemption only when nothing in the command, in any position and in every reading, can make the name run something
else (`_ap2_trusted`: a word `alias`, `hash`, `source`, `eval`, `function`, ..., a `.` that may run, `ap2` as a name
being bound outside an `ap2` call's own words (the word `ap2`, `ap2=`, `[ap2]`, ...; not `ap2` in a commit message), a
name table such as `${commands[ap2]::=...}`, an unquoted `PATH=...`, the search path's name in a setter such as
`read` or `export`, the name a setter binds when it is a zsh name table (`read -A commands`, `for aliases in ...`;
not `pytest -k commands` or `for d in commands`), a function definition, a `$'...'` it cannot decode); else an `ap2`
segment that names a guarded path is denied. A command that may make any
name run something else (a function definition, `alias`, `hash`, `source` or `autoload` where it may run as a
command, after redirects, assignments, `command --`, a substitution that may expand to nothing or inside `<(...)`
too, or as any word after an unquoted `)`, `]]` or `}` of its segment, `PATH=...`, `fpath=...`, zsh's
`functions[...]`, ...) loses every `ap2` exemption, and a segment of it that names the data folder or the package
outside the own run directory at all is denied (one whose run-directory paths only have a `$` after them is judged
by its verb). The script a shell or `eval` runs (`bash -c '...'`), and the words piped into a shell with no `-c`
among its options (`echo '...' | sh`; `bash -s -- -c` has none) or with a `-c` script that is missing or a
substitution (`sh -c "$(cat)"`), `csh` and `tcsh` too, are read as a command of their own; when that
writes nothing, the outer segment no longer reads the `-c` script word (the words after it, the script's `$1`...,
stay, judged with the script's verb too) nor the words of a piped plain `echo` or `printf` (one command word, no
`always` or closer; its redirects still count). A shell's name, or its script, before a word-final `}` counts as
well (zsh's `{ ... | sh}`). What one command reads from another can be a write too (`_fed_writes`): a segment that
runs `xargs` (any of its words, by name or full path, after any runner) with a write verb outside its redirects (or
a `dd` or `sed` that may take its `of=` or `-i` from the words it reads) is a write when what it reads (its
pipeline back to the start, the compound it starts a pipeline in, a heredoc body it holds, the input an earlier
`exec <...` gives the shell (an `exec N<` only to a command that moves fd N onto its input, `<&N`), and on the
way each `{ ...; }`, loop, `if`, `case` or `(...)` that ends before a `|` and each command substitution's output)
names a guarded path outside the own run directory (`ls <data>/items |
xargs touch`, `{ ls <data>; } | xargs rm`; `| xargs wc -l` and `find $AP2_RUN_DIR ... | xargs rm` stay allowed);
so is a segment that runs such a writer outside its own command substitutions when they read one (`rm $(find
<data> ...)`; not `wc -l $(ls <data>) > <run>/n`, nor a verb in quoted prose, unless a shell or interpreter word
there makes that word code: `sh -c "rm $(ls <data>)"`), and a pipeline into a shell that reads its script from its
input (or from a later `exec <`, or into an `xargs` that runs a shell reading it as code) where a segment names a
guarded path and one holds a write verb, counting for the shell only text made at run time, not the words of a
printer or a heredoc body `cat` passes on straight into a POSIX shell with no `$` but the guard's own names, no
`cd`, assignment, loop, `read` or `%`, which the shell's own reading judged (`find <data> | sed 's/^/rm /' |
sh`). A zsh script with a `(` that names a guarded path is a write (a fresh zsh has bare glob
qualifiers on). Heredoc body lines that end in a backslash are joined, as bash joins them (an
even run of backslashes too, fail-closed); a `$'...'` delimiter is decoded as bash 3.2 and zsh both decode it, and
a `$"..."` one (zsh keeps its `$`), one with a `$(`, `${`, `$[` or backtick in it, a `(` after it, or one it cannot
decode opens no body (its lines are code) and makes a command that names a guarded path a write. So does a `$'...'`
word with an escape the two shells read differently (`\\d`, `\\cX`, `\\C-X`, `\\M-X`, a NUL in a longer word): it
keeps its `$` and may rebind any name, and the guarded paths are also looked for with each `$'...'` read as each
shell reads it (`_Marks.names_decoded`). A heredoc body a shell on its line reads (`bash <<'EOF'`,
`bash -s <<'EOF'`, `cat <<'EOF' | sh`) is also read whole as that shell's script, one level deeper, where its own
rebinding is judged (a child shell rebinds nothing in the outer command); an unquoted body with a
`$(...)` or a backtick is also read as a `"..."` word, so a substitution across lines is a segment of its own. Whether
a text names a guarded path at all (`_Marks.names`) is also looked for with every quote, backslash and line
continuation taken out and its escapes decoded, so a spelling quoted or escaped for a deeper shell counts.

The reader follows bash 3.2, plus the zsh forms that write (Claude Code runs a worker's Bash call in zsh on this
Mac): `=(...)`, `>!`, `>&|`, `{ ... } always { ... }`, glob groups, a word-final `}`, `'` inside `"${...}"`,
`${(...)` flags and option changes, short loops (`while { a } { b }`), the precommand modifiers (`noglob`,
`nocorrect`, `-`, `repeat N`) and `autoload`. Other zsh syntax is best effort. Best effort too: it reads the command
text, so a path built at run time (a variable, `$(...)`, a `cd` first and a relative path after, a symlink) can get
past it. The `Edit` deny rules of the floor stay the main guard.

A copy of v1's `autopilot/guard.py`, adapted, not imported (design 3.2: v2 copies v1 code paths it may change). It
runs before every Bash call of a worker, so it imports only the standard library. It writes nothing and never
breaks a session: any error is no output and exit 0. Every step is linear in the command's length; all the text
one judgement reads again (a second reading, a shell's script, a `((` that was two subshells, each segment of a
backtick read again with a backslash level taken out) shares one `_Budget`, past which the command is judged as one
too long to check; a later reading takes the segments every reading reads alike from the earlier one; and `main`
stops a judgement at a deadline (`DEADLINE`), well inside the hook's 5 s timeout, after which `_fallback` (with no
timer) only searches the text.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import signal
from pathlib import Path

# v1 guard._WRITE (shell forms that write a file), with its three forms that scan ahead, `sed -x ... -i` (widened
# to an in-place flag anywhere after the `sed`, `_sed_in_place`), `dd ... of=` and `open(..., 'w')`, taken out into
# `_writes`: as one regex they are quadratic on a long run of `-sed `, `dd ` or `open(`.
_WRITE = re.compile(r"(>>?|\btee\b|\bperl\s+-[a-zA-Z]*i|\bcp\b|\bmv\b|\brm\b|\btruncate\b|"
                    r"\bln\b|\binstall\b|\.write\(|write_text|\bchmod\b|\btouch\b)")
_SED = re.compile(r"\bsed\s")
_IN_PLACE = re.compile(r"-[a-zA-Z]*[iI]|--in-place")  # a sed word that edits its files in place
_DD, _OF = re.compile(r"\bdd\b"), re.compile(r"\bof=")
_OPEN, _MODE = re.compile(r"\bopen\("), re.compile(r",\s*['\"][wax+]")
# v1 guard._HARMLESS (`\d*>&\d+|&>>?\s*/dev/null|\d*>>?\s*/dev/null`: redirects that write nowhere, a descriptor
# copy (2>&1) or output thrown away (>/dev/null)), with the same `.sub` result in linear time: v1's leading `\d*`
# rescans a digit run from each of its digits. Here a digit run is read once, whole, and kept when no redirect
# follows it (`_harmless`).
_HARMLESS = re.compile(r"\d+(?:>&\d+|>>?\s*/dev/null)?|>&\d+|&>>?\s*/dev/null|>>?\s*/dev/null")
# `${AP2_DATA:?}`, `${AP2_RUN_DIR%/*}`, ...: an expansion of the variable, never the worker's own run directory; so
# is one with zsh's `=`, `~` or `^` flags before the name (`${=AP2_DATA}`, `${^~AP2_DATA:-x}`: split, glob,
# rc-expand), then its `}` or an operator, and zsh's brace-less form of those flags (`$=AP2_DATA`, `$~AP2_RUN_DIR`),
# which ends where bash ends a name.
_EXPANSION = (r"\$\{(?:[=~^]+AP2_(?:DATA|RUN_DIR)[}:%#/^,+=?\[-]|AP2_(?:DATA|RUN_DIR)[:%#/^,+=?\[-])"
              r"|\$[=~^]+AP2_(?:DATA|RUN_DIR)(?![A-Za-z0-9_])")
_BREAK = set(" \t\r\n;|&<>()`")  # outside quotes, these end a shell word
_EMPTY_PARENS = re.compile(r"\([ \t]*\)")  # the `()` of a function definition
# A path mention ends where the path part ends: `<data>2/` or `<data>-old` is not the data folder. A `$NAME`
# ends where bash ends it, at the first character that is not a name character (`$AP2_DATA_OLD` is another
# variable, `$AP2_RUN_DIR.bak` the run directory and then `.bak`), and a `${NAME}` at its `}`.
_END, _VAR_END = r"(?![A-Za-z0-9_.-])", r"(?![A-Za-z0-9_])"
MAX_STDIN = 1 << 20  # a v2 worker's Bash payload past this is denied (`main` reads no further; no command is 1 MiB)
DEADLINE = 2.0  # seconds `main` gives one judgement before it falls back to `_fallback` (the hook's timeout is 5 s)
# == workers.ROLES: the run directory's floor files are settings-<role>.json
ROLES = ("implement", "review", "pr", "pr-review")
# A command past this is not split and matched (a long command of repeated `dd ` makes `_WRITE` slow, and a hook
# past its 5 s timeout lets the call through): it is denied when it names the data folder or the package anywhere.
MAX_COMMAND = 64 << 10
REASON = ("The assistant's data folder and the ap2 package change only through `ap2` commands; this shell "
          "command writes into one of them. Your run directory ($AP2_RUN_DIR) stays writable: write your results "
          "there, by that path, or with the Write tool.")


def package_root() -> Path:  # == workers.package_root
    return Path(__file__).resolve().parent.parent.parent


def _clean(path: str) -> str:
    path = path.rstrip("/")
    return "" if path in ("", ".") else path


def _forms(path: str, home: str) -> list:
    """`path` as a command can spell it: as is, and as `~/...`, `$HOME/...`, `${HOME}/...` when it is under HOME."""
    if not path:
        return []
    out = [path]
    if home and path.startswith(home + "/"):
        rel = path[len(home) + 1:]
        out += [f"~/{rel}", f"$HOME/{rel}", f"${{HOME}}/{rel}"]
    return out


def _home_expansions(paths, home: str) -> list:
    """For each of `paths` under HOME, a pattern for its `${HOME` spelling with an operator after the name
    (`${HOME:?}/<rel>`, `${HOME%/}/<rel>`, `${HOME:-/x}/<rel>`), or with zsh's `=`, `~` or `^` flags before it
    (`${=HOME}/<rel>`, and the brace-less `$=HOME/<rel>`): a mention that is never the own run directory, as
    `_EXPANSION` is. The operator's text runs
    up to the `}`, past no `{` or `}` (a `${` in there is not followed), so the scans from two `${HOME` never
    overlap."""
    out = []
    op = r"[:%#/^,+=?\[-](?:[^{}$]|\$(?!\{))*"
    for path in paths:
        if home and path and path.startswith(home + "/"):
            rel = path[len(home) + 1:]
            out.append(r"(?:\$\{(?:[=~^]+HOME(?:" + op + r")?|HOME" + op + r")\}|\$[=~^]+HOME)/" + re.escape(rel)
                       + _END)
    return out


def _text_end(rest: str, mask: str):
    """The index of the first quote, comma, paren, `}`, `]` or `>` in `rest` that a quote made text (`_HIDE` in
    `mask`), or None: in code inside quotes a file name ends there (`python3 -c "open('<run>/x', 'w')"`, Perl's
    `q{<run>/x}` and `q[...]`, Ruby's `%q{...}`)."""
    k = mask.find(_HIDE)
    while k >= 0:
        if rest[k] in _TEXT_END:
            return k
        k = mask.find(_HIDE, k + 1)
    return None


_TEXT_END = frozenset("'\",()}]>")
# After the run directory inside a word of prose, JSON or code, a character that goes on with its name (`<run>-old`,
# `<run>x`: another folder), or a `/` (a path under it, judged part by part). `w` is case-folded.
_NAME_GOES_ON = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-/")


def _bad_part(part: str, top: bool, mask=None) -> bool:
    """A path part under the run directory that leaves it or names the floor: `..`, a glob that can match `..` (bash
    3.2 matches `.*` to it), a `settings-*.json`, or, at the top level (`top`), where the floor's
    `settings-<role>.json` files live, a part with a `[` (fnmatch reads `[^x]` and `[[:lower:]]` otherwise than
    bash; no floor-file write needs one) or a glob that matches one (`*`, `settings*`). `mask` is `part` with each
    quoted glob or bracket character as `_HIDE` (`_Word`): only an unquoted one globs, while `..` and a
    `settings-*.json` name count however they are quoted."""
    mask = part if mask is None else mask
    if part == ".." or (part.startswith(".") and any(c in mask for c in "*?[")):
        return True
    if fnmatch.fnmatchcase(part, "settings-*.json") or (top and "[" in mask):
        return True
    if top and len(mask) > 64 and any(c in mask for c in "*?["):
        return True  # fnmatch reads a pattern in quadratic time (many `[`); no floor file name is that long
    return top and any(fnmatch.fnmatchcase(f"settings-{role}.json", mask) for role in ROLES)


class _Marks:
    """The spellings of the data folder, the package and the worker's run directory, from the worker's env."""

    def __init__(self, env):
        home = _clean(str(env.get("HOME") or ""))
        data = _clean(str(env.get("AP2_DATA") or ""))
        run = _clean(str(env.get("AP2_RUN_DIR") or ""))
        self.package = [f.casefold() for f in _forms(str(package_root()), home)]
        self.ap2 = {f"{f}/bin/ap2" for f in self.package}  # the spellings of the package's own `bin/ap2`
        data_forms = _forms(data, home) + ["$AP2_DATA", "${AP2_DATA}"]
        run_vars = ["$AP2_RUN_DIR", "${AP2_RUN_DIR}"]
        self.run = []
        if run:
            self.run = _forms(run, home) + run_vars
            if data and run.startswith(data + "/"):
                rel = run[len(data) + 1:]
                self.run += [f"{d}/{rel}" for d in data_forms]
        self.run = [f.casefold() for f in self.run]
        # Longest first, so at one place the longest spelling is the one matched. Case-folded: macOS folders are
        # case-insensitive.
        names = sorted(set(data_forms + run_vars + _forms(str(package_root()), home)), key=len, reverse=True)
        # An expansion form (`_EXPANSION`, `_home_expansions`) matches no run-directory spelling, so `unsafe` always
        # counts it.
        ends = {"$AP2_DATA": _VAR_END, "$AP2_RUN_DIR": _VAR_END, "${AP2_DATA}": "", "${AP2_RUN_DIR}": ""}
        self.any = re.compile("|".join([re.escape(n) + ends.get(n, _END) for n in names] + [_EXPANSION]
                                       + _home_expansions((data, str(package_root())), home)), re.IGNORECASE)
        # For each spelling `any` matches, the run-directory spellings that start with it (a mention can only be one
        # of those).
        self.forms = {n.casefold(): [f for f in self.run if f.startswith(n.casefold())] for n in names}

    def _own_run(self, w: str, braces: bool = True, eglob: bool = True, mask=None) -> bool:
        """Whether the case-folded text `w` (one mention, up to the next one in its word) is a path under the own
        run directory that stays inside it (`_level` is 0)."""
        return self._level(w, braces, eglob, mask) == 0

    def _level(self, w: str, braces: bool = True, eglob: bool = True, mask=None, forms=None, inside=False) -> int:
        """How far the case-folded text `w` (one mention, up to the next one in its word; `mask` the same text with each
        quoted glob, bracket, brace, comma, paren, quote or `>` character as `_HIDE`, `_Word`) is from a path under the
        own run directory that stays inside it: 0 when it is one; 1 when it is one but for a `$` or backtick after it (a
        value bash fills in at run time); 2 when it is no such path. The run directory is followed by `/`, nothing, or,
        when the mention is inside its word (`inside`: after text that cannot expand to nothing, prose, JSON or code
        around it, never a path a command is given), any character that is no name character and no `/` (`'{"run_dir":
        "<run>"}'`, `Path('<run>')`, `"results in <run> now"`, `"see <run>."`; `touch "<run>'"` makes a file next to the
        run directory, `"in <run>-old"` names another folder). After the run directory it refuses, unquoted, a `{`, `}`
        or `,` when its segment has an unquoted `{` (`braces`: a brace expansion, also one that opens before the
        mention, as in `{$AP2_RUN_DIR/..,x}`), a `(` when its segment has an unquoted `(` inside a word (`eglob`: an
        extglob such as `@(`, or a zsh glob group; one can match `..` or a floor file), and a bad part (`_bad_part`),
        also once any unquoted `}` at its end is dropped (zsh reads a `}` that ends a word as a closing brace: `{ echo x
        > <run>/settings-implement.json}` writes the floor file). Bash and zsh glob and brace-expand no quoted
        character, so a quoted one is text (JSON or code in quotes). `forms`: the run-directory spellings to try (all of
        them by default)."""
        mask = w if mask is None else mask
        for form in self.run if forms is None else forms:
            # The run directory itself, then `/`, nothing, or, inside a word, any character that does not go on
            # with its name (`'<run>'`, `"<run>"}`, `[<run>]`, `<run>, see`, `<run> now`, `<run>.`): the parts
            # after the next `/` are judged as usual.
            if w == form or w.startswith(form + "/") or (
                    inside and w.startswith(form) and w[len(form)] not in _NAME_GOES_ON):
                rest, mrest = w[len(form):], mask[len(form):]
                if (braces and any(c in mrest for c in "{},")) or (eglob and "(" in mrest):
                    return 2
                cut = len(mrest) - len(mrest.rstrip("}"))
                pairs = {(rest, mrest), (rest[:len(rest) - cut], mrest[:len(mrest) - cut])}
                text = _text_end(rest, mrest)
                if text is not None:  # Python's `open('<run>/settings-implement.json', 'w')`: the name ends there
                    pairs.add((rest[:text], mrest[:text]))
                # A rest is empty or starts with `/` or a quoted end (whose text up to a `/` is code, not a part).
                if any(_bad_part(p, k == 0, m) for r, mr in pairs
                       for k, (p, m) in enumerate(zip(r.split("/")[1:], mr.split("/")[1:]))):
                    return 2
                return 1 if ("$" in rest or "`" in rest) else 0
        return 2

    def names(self, text: str) -> bool:
        """Whether `text` names a guarded path anywhere: in the text (`in_text`), or in its words with their quotes
        removed (`_words`: `$'\\x41'` decoded too)."""
        return self.in_text(text) or any(self.any.search(w) for w in _words(text))

    def in_text(self, text: str) -> bool:
        """Whether the text `text` names a guarded path, searched as written, with every quote, backslash, `$` before
        a quote and line continuation taken out (`_STRIP`), and so again once every `\\xHH`, `\\NNN` and `\\uHHHH`
        escape in it is decoded (`_decoded`): a spelling split by quotes or escaped for a shell at any depth (`echo
        '... $AP2_"DATA" ...' | sh`, `$"..."` or `$'\\x74'` inside a quoted script). Two regex passes and three
        searches, linear: it reads no words."""
        stripped = _STRIP.sub("", text)
        return bool(self.any.search(text) or self.any.search(stripped)
                    or ("\\" in text and self.any.search(_STRIP.sub("", _decoded(text)))))

    def names_decoded(self, text: str) -> bool:
        """Whether `text` names a guarded path once each `$'...'` in it is read as bash 3.2 reads it, or as zsh does
        (`_shell_readings`), then searched with quotes, backslashes and line continuations taken out and `a//b` and
        `a/./b` read as `a/b`: for a command with a `$'...'` the two shells read differently, where the word keeps
        its `$` and its text may spell a guarded path only in one of them (`~/Documents/Assis$'t\\0'ant`)."""
        for reading in _shell_readings(text):
            if self.any.search(_norm(_STRIP.sub("", reading))):
                return True
        return False

    def is_ap2(self, word: str) -> bool:
        """Whether the shell word `word` is exactly a spelling of the package's `bin/ap2`."""
        return _norm(word) in self.ap2

    def unsafe(self, words, braces: bool = True, eglob: bool = True) -> bool:
        """Whether one of the shell words `words` names the data folder or the package anywhere but under the
        worker's own run directory (`braces` and `eglob` as in `_level`: whether the segment has a `{` that can
        brace-expand, and an unquoted `(` inside a word)."""
        return self.level(words, braces, eglob) > 0

    def level(self, words, braces: bool = True, eglob: bool = True) -> int:
        """The highest `_level` of the guarded-path mentions in the shell words `words` (2 for a package
        mention), 0 when there is none."""
        top = 0
        for word in words:
            w = _norm(word)
            mw = _norm(word.mask) if getattr(word, "mask", None) is not None else w  # aligned with `w`
            # Each mention is judged on its own, up to the next one (`"see $AP2_RUN_DIR/a.png and
            # $AP2_RUN_DIR/b.png"`): every run-directory spelling starts a new path part (`/`, `$`, or a `~` that
            # stays text mid-word), so no `..` or floor glob spans two of them.
            found = list(self.any.finditer(w))
            # Whether the text before the mention, the mentions aside, cannot expand to nothing (no `$`, backtick or
            # unquoted `{`) and holds a character other than `/` and `.` (`/<run>` and `./<run>` can be the run
            # directory's own path): then the mention is inside prose, JSON or code, not a path a command is given.
            # Not when that text ends in an `=` or a `..` part, then any `/` and `.` (`_path_before`).
            plain, other, last = True, False, 0
            for k, m in enumerate(found):
                if m.group(0).casefold() in self.package:
                    return 2
                before, last_end = w[last:m.start()], last
                plain = plain and not any(c in before for c in "$`") and "{" not in mw[last:m.start()]
                other = other or bool(before.strip("/."))
                last = m.end()
                end = found[k + 1].start() if k + 1 < len(found) else len(w)
                inside = plain and other and not _path_before(w, last_end, m.start())
                top = max(top, self._level(w[m.start():end], braces, eglob, mw[m.start():end],
                                           self.forms.get(m.group(0).casefold(), ()), inside))
                if top == 2:
                    return 2
        return top


def _path_before(w: str, last: int, start: int) -> bool:
    """Whether the text of `w` before the mention at `start` ends so that the mention is a path a command is given
    after all: after the run of `/` and `.` right before it (read back to `last`, the end of the mention before, so
    the scans of a word's mentions do not overlap) comes an `=` (`of=<run>'`, `--out=/<run>.bak`: a value a command
    takes as a path), or that run holds a `..` part (`/usr/..<run>.bak` is `/<run>.bak`)."""
    j = start
    while j > last and w[j - 1] in "/.":
        j -= 1
    parts = w[j:start].split("/")
    if j > 0:  # the run's first piece ends the part before it (`usr` of `/usr/..`, `x..`)
        parts = parts[1:]
    return (j > 0 and w[j - 1] == "=") or ".." in parts


def _norm(word: str) -> str:
    """`word` with `a//b` and `a/./b` read as `a/b`, case-folded (macOS folders are case-insensitive)."""
    if "//" in word or "/./" in word:
        word = re.sub(r"/(?:\./)+", "/", re.sub(r"/{2,}", "/", word))
    return word.casefold()


class _Body(str):
    """A heredoc body line as a segment: data for the command above it, never a command of its own (a body a shell
    reads is also one `_Script`, judged as that shell's script). `quoted`: its delimiter had a quote or a backslash,
    so the shell expands nothing in it (`<<'EOF'`, `<<\\EOF`)."""
    quoted = False


def _body(text: str, quoted: bool) -> _Body:
    line = _Body(text)
    line.quoted = quoted
    return line


class _Script(str):
    """A whole heredoc body that a shell on its line reads as its script (lines joined with newlines; an unquoted
    body as written, before the outer shell expands it): `_command_writes` reads it as a script of its own
    (`_scripts_write`), so a function, a rebinding or a quoted word across lines counts. `zsh`: that shell may be
    zsh."""
    zsh = False


class _Piped(str):
    """A segment that may read what another one writes (`_fed_writes`): `prev` is the segment piped into it (a
    `_Pipe`, whose own `prev` goes on back to the start of the pipeline), or None; `inherit` the `_Pipe` piped into a
    compound it starts a pipeline in (`ls | if true; then xargs rm; fi`); `bodies` the heredoc bodies it may read
    (`_heredocs`), each a shared tuple of `_Body` lines; `subs` the `_Group` of its command substitutions' segments,
    whose output becomes its words; `group` the innermost compound (`_Group`) it sits in, and `closed` the compounds
    it ends (`{ ...; } | xargs rm`: the `}` segment ends its group). A segment with none of these is a plain `str`."""
    prev = inherit = subs = group = None
    bodies = closed = ()


class _Pipe(_Piped):
    """A segment that a `|` or `|&` ends: its output is the next command's input."""


class _Group:
    """A compound whose segments' output goes out as one: a `{ ...; }` group, a loop or `if` or `case` up to its
    closing word, a `(...)` (`_segments`), or the command substitutions of one segment (a group of its own, `up`
    None). `up` is the compound around it, `input` the `_Pipe` piped into it (or into one around it)."""
    __slots__ = ("up", "input")

    def __init__(self, up, input):
        self.up, self.input = up, input


# The words a segment starts with that close compounds (`}`, `done`, `fi`, `esac`), then the ones that open them
# (`{`, `if`, `while`, `until`; after `do`, `then`, `else`, `elif`, `!` or `time`; and last `for`, `select` or `case`,
# whose own words follow), read by `_link`.
_CLOSE_WORD = r"(?:\}|done|fi|esac)(?=[\s<>;|&)]|$)"
_LEAD = re.compile(r"\s*((?:" + _CLOSE_WORD + r"\s*)*)((?:(?:\{|if|while|until|do|then|else|elif|!|time)(?=\s|$)\s*)*)"
                   r"((?:for|select|case|foreach)(?=\s|$))?")
_OPENS = re.compile(r"\{|if|while|until")
# A segment that starts with a closer (`_ends_compound`: `{ ...; xargs rm; } <<EOF` gives its body to the whole line).
_CLOSER = re.compile(r"\s*(?:\)|" + _CLOSE_WORD + ")")


def _link(text: str, pipe: bool, prev, top=None, base=None, closed=(), subs=None, inherit=None):
    """The segment `text`, a `_Pipe` when a `|` ends it, with `prev`, the `_Pipe` before it in its pipeline (None
    at the start of one), and the compound state of `_segments`: `top` the innermost compound open at its end
    (`base`, that of its context, is never closed), `closed` the compounds that ended inside it, `subs` its
    substitutions' group and `inherit` the input of the compound it started in. The words it starts with close
    and open compounds as `_LEAD` reads them; returns the segment and the new `top`."""
    m = _LEAD.match(text)
    if m.group(1):
        closed = list(closed)
        for _ in m.group(1).split():
            if top is not None and top is not base:
                closed.append(top)
                top = top.up
    for _ in range(len(_OPENS.findall(m.group(2))) + bool(m.group(3))):
        top = _Group(top, prev if prev is not None else top.input if top is not None else inherit)
    if prev is None and inherit is None and top is None and not closed and subs is None:
        return (_Pipe(text) if pipe else text), top
    seg = _Pipe(text) if pipe else _Piped(text)
    seg.prev, seg.group, seg.subs = prev, top, subs
    seg.inherit = inherit if prev is None else None
    if closed:
        seg.closed = tuple(closed)
    return seg, top


class _Deep(Exception):
    """The text read again (a `((` found to be two subshells, a shell's script, a second reading) went past its
    `_Budget`: `decide` then judges the command as one too long to check."""


class _Budget:
    """How many more characters one judgement may read again (`left`), shared by every step of it, and the
    scripts (text, depth) already judged to write nothing (`seen`), so another reading does not read them again.
    Also the segments one reading parsed (`parsed`) and judged to write nothing (`clean`) whose text holds neither a
    `$$(` nor a `'`: every reading reads those alike, so a later reading takes them from here."""

    def __init__(self, size: int):
        self.left, self.seen, self.parsed, self.clean = size, set(), {}, set()

    def spend(self, n: int):
        self.left -= n
        if self.left < 0:
            raise _Deep


# The delimiter word after `<<`: unquoted characters that do not end a word, escapes, and quoted runs, an ANSI-C
# `$'...'` (where `\'` stays inside) among them. A run of `$` is read two at a time (`$$` is the PID), as `_expands`
# reads it, so only an odd run's last `$` starts a `$'...'` (`$$'x\'` is the PID and a plain `'x\'`). The
# alternatives never overlap (each `$` one by the character after it), so `_FUNC`, which backtracks over this
# pattern, stays linear, and a `$$'` never scans the rest of the command for a closing quote that bash never needs.
_DELIM = re.compile(r"""(?:\$'(?:[^'\\]|\\.)*'|\$\$|\$(?![$'])|[^\s;|&<>()'"\\$]|\\.|'[^']*'|"(?:[^"\\]|\\.)*")+""",
                    re.S)
# A delimiter word whose end bash and zsh may find elsewhere than `_DELIM` does: one with a `$(`, `${`, `$[` or a
# backtick in it (bash reads such a construct whole, blanks and parens inside too).
_UNSURE_DELIM = re.compile(r"\$[({\[]|`")


# Outside quotes, the characters `_segments` acts on; any other one it steps over (a run of them at once).
_SPECIAL = frozenset("\\$'\"`(){}[]#<>\n;|&")
_SEG_ORDINARY = re.compile(r"[^\\$'\"`(){}\[\]#<>\n;|&]*")
# A backtick command substitution's text: bash ends it at the first unescaped backtick, whatever quotes lie between.
_TICK_BODY = re.compile(r"[^\\`]*(?:\\.[^\\`]*)*", re.S)
# One level of backslashes: `\x` is `x` (a backtick's text inside `"${...}"`, as bash 3.2 can read it).
_UNESCAPE = re.compile(r"\\(.)", re.S)


def _tick_end(s: str, j: int) -> int:
    """The index of the first unescaped backtick in `s` from `j` on, or len(s) when there is none."""
    k = _TICK_BODY.match(s, j).end()
    return k if k < len(s) and s[k] == "`" else len(s)


def _expands(s: str, i: int, esc: int) -> bool:
    """Whether the `$` at `i` starts an expansion (`$'`, `${`, `$[`, `$(`): the run of unescaped `$` that ends at it
    is odd. `$$` is the shell's PID, so in `$$'`, `$${` or `$$[` the quote or bracket is a plain one. `esc` is the
    index of the last escaped character. Linear overall: only the last `$` of a run has another character next."""
    k = i
    while k >= 0 and s[k] == "$" and k != esc:
        k -= 1
    return (i - k) % 2 == 1


def _opens(s: str, i: int, esc: int, quote, pid: bool) -> str:
    """The context the `$` at `i` opens, where quotes nest afresh up to its end: `(` for a `$(` (not `$((`) that
    expands, and inside `"..."` (`quote`) `{` for a `${` that expands and, in the `pid` reading, `$` for a `$$(` (the
    PID, then a `(...)` that bash 3.2 reads with its own quotes); "" for none."""
    if s.startswith("$(", i):
        if _expands(s, i, esc):
            return "" if s.startswith("$((", i) else "("
        return "$" if quote == '"' and pid else ""
    return "{" if quote == '"' and s.startswith("${", i) and _expands(s, i, esc) else ""


# `$'...'` escapes bash 3.2 and zsh decode alike, each to one character (`\\NNN` octal, `\\xHH` hex, `\\uHHHH` and
# `\\UHHHHHHHH` are read in `_ansi_c`).
_ANSI_C = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v",
           "\\": "\\", "'": "'", '"': '"', "?": "?"}
_OCT, _HEX = frozenset("01234567"), frozenset("0123456789abcdefABCDEF")


def _ansi_c(word: str, i: int, out: list, strict: bool = True, flags=None, alone: bool = False):
    """Decodes the text of a `$'...'` from `i` (just past its opening quote) into `out`, as bash 3.2 and zsh both
    decode it. Only the escapes the two shells read alike are decoded: the `_ANSI_C` ones, `\\NNN` (1-3 octal
    digits, the value kept to a byte, as both keep it: `\\543` is `c`), `\\xHH` (1-2 hex digits), and a `\\u` (up to
    4 hex digits) or `\\U` (up to 8) with at least one digit. Returns the index of the closing quote, or None for a
    text it cannot decode the way both shells would: any other escape (zsh drops the backslash of an unknown one,
    `\\d` is `d` there and `\\d` in bash; it reads `\\C-X`, `\\CX` and `\\M-X` as control and meta characters,
    and `\\cX` as `c` and X, where bash reads a control character), a NUL (zsh makes one of `\\x`, `\\u` and `\\U`
    with no digit and of `\\u0000` too, and an external command's path or a redirect's file ends there, while bash
    ends the quote's text there and goes on with the word), or no closing quote. Only a quote that is a whole word
    (`alone`: it starts its word and a blank or an operator follows it) and holds nothing but `\\0`-style octal and
    `\\x0`-style hex NULs passes (`read -d $'\\0'`, `tr -d $'\\0'`: an empty word, which names nothing). When `strict`
    (a heredoc delimiter, which must match a line exactly) a NUL, a non-ASCII code and any `\\u` or `\\U` are
    undecodable too. Not `strict` (a word), a code past 0x7F is kept as it is (no name, nor a path part the guard
    judges, holds one), and so is a `\\u` or `\\U` past 0x7F, as the text bash 3.2 keeps in some locales (bash 3.2 may
    or may not decode `\\u`: it depends on the locale; a code above 0x7F spells no ASCII name either way); one whose
    code point is an ASCII character is read as decoded (`$'\\u0063p'` is `cp`, `$'\\u002e\\u002e'` is `..`) and adds
    `$'` to `flags`: bash 3.2 may read another name."""
    n, cut, first = len(word), False, len(out)  # cut: a NUL was read
    while i < n and word[i] != "'":
        if word[i] != "\\" or i + 1 >= n:
            if cut:
                return None  # a NUL with other text in the quote
            out.append(word[i])
            i += 1
            continue
        e, i = word[i + 1], i + 2
        if e in _ANSI_C:
            if cut:
                return None
            out.append(_ANSI_C[e])
            continue
        if e in _OCT or e == "x":
            digits, k = (_OCT, i - 1) if e in _OCT else (_HEX, i)
            stop = k + (3 if e in _OCT else 2)
            j = k
            while j < min(n, stop) and word[j] in digits:
                j += 1
            if j == k:  # `\\x` with no hex digit: zsh makes a NUL of it, bash keeps it as text
                return None
            code, i = int(word[k:j], 8 if e in _OCT else 16) & 0xFF, j
        elif e in "uU":
            j = i
            while j < min(n, i + (4 if e == "u" else 8)) and word[j] in _HEX:
                j += 1
            if strict or j == i or cut or not int(word[i:j], 16):
                return None  # no digit or code 0: zsh's NUL
            if int(word[i:j], 16) < 0x80:  # an ASCII character, which may spell a name
                out.append(chr(int(word[i:j], 16)))
                i = j
                if flags is not None:
                    flags.add("$'")
                continue
            out.append("\\" + e)  # past 0x7F: kept as bash 3.2's text, its digits read on as plain characters
            continue
        else:
            return None  # `\\c`, `\\C`, `\\M` or an escape outside the shared set
        if strict and not 0 < code < 0x80:
            return None
        if code:
            if cut:
                return None
            out.append(chr(code))
        elif len(out) > first or not alone:
            return None  # a NUL after other text, or in a quote that is not a whole word
        else:
            cut = True
    if i >= n or (cut and not (i + 1 >= n or word[i + 1] in _BREAK)):
        return None  # no closing quote, or a NUL-only quote that the word goes on after
    return i


def _unquote(word: str):
    """`word` after quote removal only, as bash reads a heredoc delimiter: quotes and the backslashes that escape go,
    every other character stays (``a\\`b`` is ``a`b``, `"a\\$b"` is `a$b`, `"a\\b"` is `a\\b`). `$'...'` is decoded as
    bash 3.2 and zsh both decode it (`_ansi_c`; `$'E\\x4f'` is `EO`); None when it cannot be, and for a `$"..."` (bash
    reads it as `"..."`, zsh keeps its `$`: `$"EOF"` is `$EOF` there), so `_segments` opens no heredoc for it and its
    lines are read as code."""
    out, i, n, esc = [], 0, len(word), -1
    while i < n:
        c = word[i]
        if c in "'\"" and word[i - 1:i] == "$" and _expands(word, i - 1, esc):
            out.pop()  # the `$` of a `$'` or `$"`
            if c == '"':
                return None  # zsh keeps the `$` of a `$"..."` (`$"EOF"` is `$EOF` there), bash drops it
            if c == "'":
                i = _ansi_c(word, i + 1, out)
                if i is None:
                    return None
                i += 1
                continue
        if c == "\\" and i + 1 < n:
            i += 1
            esc = i
            if word[i] != "\n":  # a line continuation: both characters go
                out.append(word[i])
        elif c == "'":
            i += 1
            while i < n and word[i] != "'":
                out.append(word[i])
                i += 1
        elif c == '"':
            i += 1
            while i < n and word[i] != '"':
                if word[i] == "\\" and word[i + 1:i + 2] in ("$", "`", '"', "\\", "\n"):
                    i += 1
                    if word[i] == "\n":
                        i += 1
                        continue
                out.append(word[i])
                i += 1
        else:
            out.append(c)
        i += 1
    return "".join(out)


# Words bash reads before a command name: `do <cmd>`, `then <cmd>`, `time <cmd>`, ...
_KEYWORDS = frozenset(("do", "then", "else", "elif", "if", "while", "until", "!", "{", "time"))
# The first word of a segment after the keywords (`_KEYWORDS`), then `(`: `ap2(){ ...; }`, `ap2 () ...` or
# `do ap2(){ ...; }` defines a function, it does not run ap2.
_FUNC = re.compile(r"\s*(?:(?:" + "|".join(re.escape(k) for k in sorted(_KEYWORDS)) + r")\s+)*(?:"
                   + _DELIM.pattern + r")[ \t]*\(", re.S)


def _segments(cmd: str, pid: bool = True, budget=None, seen=None, sq: bool = True, body: bool = False,
              owner=None) -> list:
    """`cmd` cut at the separators v1 splits on (newline, `;`, `&&`, `||`, `|`), and at `|&` and a background `&`,
    only where bash reads them as separators: outside quotes and not after a backslash. `sed -i 's|a|b|' f` is one
    segment. The `&` of `2>&1`, `>&2`, `<&3`, `&>` and `&>>` and the `|` of `>|` are part of a redirect, not
    separators, and so is the `|` of zsh's `>&|` and `>>&|`. An unterminated quote keeps the rest as one segment.
    The segments keep their text as written (quotes and backslashes too); one that a `|` or `|&` ends is a `_Pipe`.
    A command substitution is read as bash reads it: the text of a `$(...)` (even within `"..."`, where quoting
    starts afresh, `"$(cmd "a b")"`, up to the `)` that closes it) or of backticks (up to the next unescaped
    backtick, whatever quotes lie between, and read with its own quotes) is cut into segments of its own, and in
    the segment around it that text is `_` (`cp a "$(b; c)" d` is `b`, ` c` and `cp a "$(_)" d`), so the outer
    verb keeps all its words. Inside `"..."` a `${...}` reads quotes afresh too (`"${x:-"a; b"}"`, `"${x:-'}'}"`),
    up to the `}` that closes it, but a `'` there (and inside arithmetic, `$((...))`, `((...))` and `$[...]`)
    only hides that end, a separator or a comment: bash keeps it only to find the end, so a `$(...)` or backtick
    inside it runs and is cut out as above (`"${x:-'$(cmd)'}"` runs `cmd`); a backslash there escapes only a `$`,
    a backtick or a backslash. With `sq` False a `'` inside `"${...}"` is a plain character, as zsh reads it (zsh
    closes `"${x:-'}"` at that `}`). The `(...)` of a `$$(` reads quotes afresh too (bash 3.2 reads the PID and
    then a `(...)` with its own quotes), and nothing splits there; with `pid` False, the `$$(` is plain text, as bash 5.3 reads it at
    times (`"$$("<newline><cmd>` runs `<cmd>` there). `$'...'` is an ANSI-C quote, where `\\'` stays inside, but in
    `$$'` the `$$` is the PID and the quote a plain one (likewise `$${` and `$$[`). Inside `${...}` no separator
    splits (`${x:-;}`), and inside arithmetic (`((...))`, `$((...))`, `$[...]`) no newline, `|` or `&` does: bash
    keeps arithmetic open across lines (a `$((` inside `"..."` reads its quotes afresh and splits nothing).
    A `((` or `$((` closed by a single `)` first (`((cd x; ls) )`) was two subshells, not arithmetic, as bash finds
    out: its text is then read again from the `((`, as `(` `(` (a `$((` as `$(` `(`), so the comments, heredocs
    and separators in there count (`budget`, a `_Budget` shared with the backtick texts and, from `decide`, with
    every other step that reads text again, caps how much is read again: past it, `_Deep`; four times the
    command's length when the call makes its own).
    Bash reads no quotes in a comment or a heredoc body, and neither does this: an unquoted `#` at the start of a
    word (not after an escaped blank, not inside `${...}` or arithmetic) drops the rest of its line, and after an
    unquoted `<<` or `<<-` (not inside `${...}` or arithmetic) the lines from the next newline at the same depth
    on (a `$(` opened after the `<<` is read whole first), up to and including the delimiter line (its word after
    quote removal only, `_unquote`; leading tabs stripped for `<<-`), are each one segment (`_Body`), with no scan;
    a delimiter `_unquote` cannot decode, or one with a `$(`, `${`, `$[` or backtick in it or a `(` right after it
    (`_UNSURE_DELIM`: the shell may end it elsewhere), opens no heredoc and adds `<<` to `seen`. Inside `$(...)` a line
    of the delimiter and then `)` ends the body too (bash 3.2), and the scan goes on at that `)`; a heredoc whose
    `$(...)` closes on its own line has no body (bash runs the next lines). A here-string `<<<` opens no heredoc. A body
    a shell on its line reads (`bash <<'EOF'`, `cat <<'EOF' | sh`) is also one `_Script`, and an unquoted body with a
    `$(` or a backtick is also read as a `"..."` word, its substitutions cut into segments of their own across lines
    (`_heredocs`; `body` True is that reading: the text starts inside a `"` that only its end closes, and the text
    around the substitutions is no segment). Empty backticks run nothing: no segment, and they stay empty (no `_`).
    When `seen` is a set, `@` is added to it for an extglob opener read as code: an unescaped `@`, `+`, `!`, `?` or
    `*` and then `(` (or `((`) outside quotes, `"${...}"`, `"$$(...)"` and arithmetic, as in `cp @(a|b) <data>/c`,
    where bash with extglob on reads no comment and no pipe, and `<<` for a heredoc it opened no body for (above).
    Each segment also gets what `_fed_writes` follows (`_Piped`, `_link`): the compounds it sits in and ends (a
    `(` opens one and its `)` ends it; `{`, `if`, `while`, `until`, `for`, `select` and `case` at the start of a
    segment open one, and `}`, `done`, `fi` and `esac` there end it), the group of its command substitutions'
    segments (`owner`: that group, for a backtick's text read on its own), the input of a piped compound it starts
    a pipeline in, and the heredoc bodies of the `<<` it holds."""
    budget = _Budget(4 * len(cmd)) if budget is None else budget
    segs, n, subs = [], len(cmd), set()  # subs: the `((` found to be two subshells
    quote, i, esc = None, 0, -1  # esc: the index of the last character a backslash escaped
    # The quote state of the innermost context: the quote (None, `'`, `"`, or `$'`), open `(` (`paren`), open `${`
    # (`brace`), the open arithmetic span if any (`arith`: `["(", open single (, index of its `((`, the state to
    # read it again from]` for `((` and `$((`, `["[", open [, None, None]` for `$[`), and the context's `kind`:
    # None at the top, `(` for `$(`, `{` for `"${`, `$` for `"$$(`, `a` for `"$((`. `stack` holds the saved
    # states of the outer contexts, each with, for a `(`, the outer segment's (`parts`, `pending`, `chain`).
    # The current segment is `parts` and then `cmd[start:]` (up to `cut`, where a comment ended it), and
    # `pending` the heredocs opened on its line: (delimiter, strip leading tabs).
    paren, brace, arith, kind, stack = 0, 0, None, None, []
    start, parts, cut, pending = 0, [], None, []
    chain = None  # the `_Pipe` before the current segment in its pipeline, in the current context (`_link`)
    line = 0  # the index in `segs` of the first segment of the current line (`_heredocs`)
    # The compounds (`_Group`, `_link`): `top` the innermost one open, `base` the context's own (a substitution's
    # group, `owner` for a backtick's text; never closed), `closed` those the current segment ended, `subtok` the
    # group of its substitutions, `inherit` the input of the compound it started in, `holder` whether it holds a
    # `<<`, `holders` the indexes in `segs` of those on the current line, and `undo` each `bodies` `_heredocs` set,
    # with the value before, so a `((` read again takes them back.
    top = base = owner
    closed, subtok, inherit, holder, holders, undo = [], None, None, False, [], []
    if body:  # an unquoted heredoc body: read as a `"..."` word whose `"` is a plain character
        quote = '"'
    while i < n:
        c = cmd[i]
        if quote is None and c not in _SPECIAL:  # a run of ordinary characters, stepped over at once
            i = _SEG_ORDINARY.match(cmd, i).end()
            continue
        elif quote == "'":
            j = cmd.find("'", i)
            i, quote = (n, quote) if j < 0 else (j + 1, None)
            continue
        elif quote == "$'":
            if c == "\\":
                i += 1
            elif c == "'":
                quote = None
        elif quote == "s" and (c == "'" or (c == "\\" and cmd[i + 1:i + 2] not in ("$", "`", "\\"))):
            if c == "'":
                quote = None  # bash closes it at the next `'`, a backslash before it or not
        elif c == "\\":
            i += 1
            esc = i
        elif c == "`":
            end = _tick_end(cmd, i + 1)
            if end == i + 1 < n:  # empty ones (a run of them read at once) run nothing, and stay empty in the outer text
                end = _EMPTY_TICKS.match(cmd, i).end() - 1
                parts.append(cmd[start:end])
                start, i = end, end + 1
                continue
            subtok = subtok or _Group(None, None)
            segs.extend(_segments(cmd[i + 1:end], pid, budget, seen, sq, owner=subtok))
            if kind == "{" and "\\" in cmd[i + 1:end]:  # bash 3.2 can drop a backslash level there: read it so too
                budget.spend(end - i)
                again = _segments(_UNESCAPE.sub(r"\1", cmd[i + 1:end]), pid, budget, seen, sq, owner=subtok)
                budget.spend(SEGMENT_COST * len(again))  # each segment is judged on its own later: charged too
                segs.extend(again)
            parts += (cmd[start:i + 1], "_")
            start, i = end, end + 1  # the closing backtick stays in the outer text
            continue
        elif c == "$" and arith is None and quote != "s" and i + 1 not in subs and cmd.startswith("$((", i) and (
                _expands(cmd, i, esc)):
            again = (i, len(segs), start, parts, len(parts), cut, pending, esc, quote, paren, brace, kind, len(stack),
                     line, chain, closed, len(closed), subtok, inherit, holder, holders, len(holders), top, base,
                     len(undo))
            if quote == '"':
                stack.append((quote, paren, brace, arith, kind, None))
                quote, paren, brace, kind = None, 0, 0, "a"
            arith = ["(", 0, i + 1, again]
            i += 3
            continue
        elif c == "$" and ((new := _opens(cmd, i, esc, quote, pid)) or i + 1 in subs and cmd.startswith("$((", i)):
            new = new or "("  # a `$((` found to be two subshells is a `$(`
            if new == "(":
                parts += (cmd[start:i + 2], "_")
                subtok = subtok or _Group(None, None)
                stack.append((quote, paren, brace, arith, kind,
                              (parts, pending, chain, closed, subtok, inherit, holder, holders, top, base)))
                start, parts, cut, pending, chain = i + 2, [], None, [], None
                top = base = subtok
                closed, subtok, inherit, holder, holders = [], None, None, False, []
            else:
                stack.append((quote, paren, brace, arith, kind, None))
            quote, paren, brace, arith, kind = None, 0, int(new == "{"), None, new
            i += 2
            continue
        elif quote == '"':
            if c == '"' and not (body and not stack):  # in a heredoc body's own text a `"` is a plain character
                quote = None
        elif quote == "s":
            pass  # inside `"${...}"` or arithmetic a `'` hides only the end, a separator or a comment
        elif c == "'":
            if cmd[i - 1:i] == "$" and _expands(cmd, i - 1, esc):
                quote = "$'"
            elif kind == "{" and not sq:
                pass  # zsh: a plain character in `"${...}"`, so `"${x:-'}"` closes at that `}`
            else:  # in `"${...}"` and arithmetic bash keeps a `'` only to find the end: a `$(` in there runs
                quote = "s" if kind == "{" or arith is not None else "'"
        elif c == '"':
            quote = c
        elif c == "$" and cmd.startswith("${", i) and _expands(cmd, i, esc):
            brace += 1
            i += 2
            continue
        elif c == "$" and arith is None and cmd.startswith("$[", i) and _expands(cmd, i, esc):
            arith = ["[", 0, None, None]
            i += 2
            continue
        elif c == "}" and brace:
            brace -= 1
            if not brace and kind == "{":  # the `}` that closes a `"${`: back inside the `"..."`
                quote, paren, brace, arith, kind = stack.pop()[:5]
        elif c in "[]":
            if arith and arith[0] == "[":
                if c == "[":
                    arith[1] += 1
                elif arith[1]:
                    arith[1] -= 1
                else:
                    arith = None
        elif arith is None and i not in subs and cmd.startswith("((", i):
            if seen is not None and _opener(cmd, i, esc, kind):
                seen.add("@")
            again = (i, len(segs), start, parts, len(parts), cut, pending, esc, quote, paren, brace, kind, len(stack),
                     line, chain, closed, len(closed), subtok, inherit, holder, holders, len(holders), top, base,
                     len(undo))
            arith = ["(", 0, i, again]
            i += 2
            continue
        elif c == "(":
            if arith is None:
                paren += 1
                top = _Group(top, chain if chain is not None else top.input if top is not None else inherit)
                if seen is not None and _opener(cmd, i, esc, kind):
                    seen.add("@")
            elif arith[0] == "(":
                arith[1] += 1  # inside arithmetic a `((` or `$((` is parentheses too
        elif c == ")":
            if arith and arith[0] == "[":
                pass  # `$[...]` counts brackets only
            elif arith and arith[1]:
                arith[1] -= 1
            elif arith and cmd.startswith("))", i):
                if kind == "a":  # the `))` that closes a `"$((`: back inside the `"..."`
                    quote, paren, brace, arith, kind = stack.pop()[:5]
                else:
                    arith = None
                i += 2
                continue
            elif arith:  # the `((` was two subshells: read its text again as code, from the `((`
                budget.spend(i - arith[3][0])
                subs.add(arith[2])
                (i, size, start, parts, size_parts, cut, pending, esc, quote, paren, brace, kind, depth, line,
                 chain, closed, size_closed, subtok, inherit, holder, holders, size_holders, top, base,
                 size_undo) = arith[3]
                del segs[size:], parts[size_parts:], stack[depth:], closed[size_closed:], holders[size_holders:]
                while len(undo) > size_undo:  # the bodies `_heredocs` gave earlier segments since the `((`
                    seg, bodies = undo.pop()
                    seg.bodies = bodies
                arith = None  # as it was at the `((`
                continue
            elif paren:
                paren -= 1
                if top is not None and top is not base:
                    closed.append(top)
                    top = top.up
            elif stack and kind != "{":  # the `)` that closes a `$(` or `"$$(`
                if kind == "(":
                    text = "".join(parts) + cmd[start:cut if cut is not None else i]
                    if text:
                        seg, top = _link(text, False, chain, top, base, closed, subtok, inherit)
                        segs.append(seg)
                        if holder:
                            holders.append(len(segs) - 1)
                    # A heredoc opened in there and still open has no body.
                    parts, pending, chain, closed, subtok, inherit, holder, holders, top, base = stack[-1][5]
                    start, cut = i, None
                quote, paren, brace, arith, kind = stack.pop()[:5]
        elif kind in ("$", "a"):
            pass  # inside `"$$(...)"` or `"$((...))"` nothing runs: no comment, heredoc or separator
        elif c == "#" and not brace and not arith and (i == 0 or (cmd[i - 1] in " \t\n;|&(" and esc != i - 1)):
            cut = i  # after `)` bash keeps `#` in the word
            j = cmd.find("\n", i)
            i = n if j < 0 else j  # the newline still ends the segment, below
            continue
        elif cmd.startswith("<<<", i):  # a here-string: one word of input, no body lines
            i += 3
            continue
        elif cmd.startswith("<<", i) and not arith and not brace:
            j = i + 2
            strip = cmd[j:j + 1] == "-"
            j += strip
            while j < n and cmd[j] in " \t":
                j += 1
            m = _DELIM.match(cmd, j)
            if m:
                word = m.group(0)
                # A word with a substitution or an expansion in it, or a `(` after it, can end where bash and zsh
                # end it and `_DELIM` does not (`<<$(x)`, `<<${x:-a b}`); one it cannot decode ends nowhere it can
                # tell: no heredoc is opened (its lines are read as code), and the command is marked (`<<`).
                delim = None if _UNSURE_DELIM.search(word) or cmd.startswith("(", m.end()) else _unquote(word)
                if delim is not None:
                    pending.append((delim, strip, any(q in word for q in "'\"\\")))
                    holder = True
                elif seen is not None:
                    seen.add("<<")
                i = m.end()
            else:
                i = j
            continue
        elif c in "\n;|&" and not brace and not (arith and c != ";") and not _in_redirect(cmd, i, esc):
            text = "".join(parts) + cmd[start:cut if cut is not None else i]
            nxt = cmd[i + 1:i + 2]
            pipe = c == "|" and nxt != "|"  # `|` and `|&` pipe, `||` does not
            seg, top = _link(text, pipe, chain, top, base, closed, subtok, inherit)
            segs.append(seg)
            if holder:
                holders.append(len(segs) - 1)
            closed, subtok, holder = [], None, False
            inherit = top.input if top is not None else None
            # A pipeline goes on after a `|` and a newline (`ls |<newline>xargs ...`); any other separator ends it.
            if pipe:
                chain = segs[-1]
            elif not (c == "\n" and chain is not None and not text.strip()):
                chain = None
            parts, cut = [], None
            i += 2 if (c in "&|" and nxt == c) or (c == "|" and nxt == "&") else 1
            if c == "\n":
                if pending:
                    i = _heredocs(cmd, i, segs, line, pending, kind == "(", pid, budget, seen, sq, holders, undo)
                    pending = []
                line, holders = len(segs), []
            start = i
            continue
        i += 1
    while True:  # the end: the segments still open, innermost first
        while stack and not stack[-1][5]:
            stack.pop()  # a `"${`, `"$$(` or `"$((`: still the same segment
        text = "".join(parts) + cmd[start:cut if cut is not None else n]
        if not stack:
            if (text or not segs) and not body:  # a heredoc body's own text is data: only its substitutions run
                segs.append(_link(text, False, chain, top, base, closed, subtok, inherit)[0])
            return segs
        if text:
            segs.append(_link(text, False, chain, top, base, closed, subtok, inherit)[0])
        parts, _, chain, closed, subtok, inherit, holder, holders, top, base = stack.pop()[5]
        start, cut = n, None


def _heredocs(cmd: str, i: int, segs: list, line: int, pending: list, sub: bool, pid: bool, budget, seen,
              sq: bool, holders=(), undo=None) -> int:
    """Reads the bodies of the heredocs opened on the line that ends just before `i` (`pending`: (delimiter, strip
    leading tabs, quoted)) into `segs`, as `_segments` describes, and returns the index after them. Each body line is
    one `_Body`. `sub`: the line is inside `$(...)`, where a line of the delimiter and then `)` ends the body too
    (bash 3.2), the scan going on at that `)`. When a segment of the line (`segs[line:]`) runs a shell that reads its
    script from its input (`_stdin_shell`: `bash <<'EOF'`, `bash -s <<'EOF'`, `cat <<'EOF' | sh`; fail-closed, any
    such shell on the line), the whole body (leading tabs stripped for `<<-`) is also a `_Script`. An unquoted body
    with a `$(` or a backtick is also read as a `"..."` word is (`_segments` with `body`: its substitutions, which
    can run across lines, are segments of their own; a `'` or `"` outside them is a plain character), its length
    spent from `budget`. The body lines, one shared `_Chunk` (`scripted` when a shell on the line reads them), are
    also added to the `bodies` (`_Piped`) of each segment that holds a `<<` (`holders`, indexes in `segs`: `xargs
    touch <<'EOF'`; its pipe successors read them through `prev`, `cat <<'EOF' | xargs touch`), or of every segment
    of the line when any holder ends a compound (`_ends_compound`: `{ ...; xargs rm; } <<'EOF'`, `(xargs rm; true)
    <<'EOF'` feed the group), that target set found once per line; each one set goes to `undo` with the value
    before."""
    n, closed, first = len(cmd), False, len(segs)
    shells = [w for s in segs[line:] if not isinstance(s, _Body)
              and (w := _stdin_shell(_words(s, None, None, pid, None, sq)))]
    fed, zsh = bool(shells), any(_ZSH.search(w) for w in shells)  # fed: a shell reads the bodies
    for delim, strip, quoted in pending:
        close, lines = delim + ")", []
        while i < n and not closed:
            j = cmd.find("\n", i)
            end = n if j < 0 else j
            text = cmd[i:end]
            body = text.lstrip("\t") if strip else text
            if sub and body.startswith(close):  # bash 3.2 ends it in `$(...)` at `EOF)` too
                segs.append(_body(delim, quoted))
                i = end - len(body) + len(delim)  # the scan goes on at that `)`
                closed = True  # it closes the `$(`: any other heredoc of that line has no body
                break
            segs.append(_body(text, quoted))
            i = end + 1
            if body == delim:
                break
            lines.append(body)
        script = "".join(t + "\n" for t in lines)
        if fed and script:
            segs.append(_Script(script))
            segs[-1].zsh = zsh
        if not quoted and ("$(" in script or "`" in script):
            budget.spend(len(script))
            segs.extend(_segments(script, pid, budget, seen, sq, True))
    bodies = _Chunk(s for s in segs[first:] if isinstance(s, _Body))
    if bodies:
        bodies.scripted = fed
        # One target set for the line: every segment of it when a holder ends a compound, else the holders.
        for k in range(line, first) if any(_ends_compound(segs[h], pid, sq) for h in holders) else holders:
            seg = segs[k]
            if isinstance(seg, (_Body, _Script)) or (isinstance(seg, _Piped) and any(b is bodies for b in seg.bodies)):
                continue
            if not isinstance(seg, _Piped):
                seg = segs[k] = _Piped(seg)
            if undo is not None:
                undo.append((seg, seg.bodies))
            seg.bodies = seg.bodies + (bodies,)
    return min(i, n)


class _Chunk(tuple):
    """The body lines of the heredocs of one line (`_heredocs`), shared by every segment that may read them.
    `scripted`: a shell on the line reads them as its script too (a `_Script`), so a plain `cat` that passes them on
    straight to that shell carries no text the shell's own reading has not judged, when the lines are exact
    (`_exact_script`, `_fed_writes`)."""
    scripted = False


def _ends_compound(seg: str, pid: bool, sq: bool) -> bool:
    """Whether the heredoc holder `seg` ends a compound: it starts with a closer (`_CLOSER`), a `)` or a closing
    word ends one inside it (`_Piped.closed`: `(xargs rm; true) <<'EOF'`), or one of its words ends in an unquoted
    `}` that closes no `${` (zsh closes a group there: `{ xargs rm; true } <<'EOF'`)."""
    if _CLOSER.match(seg) or getattr(seg, "closed", ()):
        return True
    if "}" not in seg:
        return False
    for w in _words(seg, None, None, pid, None, sq):
        if _unbrace(w) != w:
            o = w.rfind("{")
            if o < 1 or w[o - 1] != "$":
                return True
    return False


def _opener(cmd: str, i: int, esc: int, kind) -> bool:
    """Whether the unquoted `(` at `i`, read as code (not inside `"${...}"` or `"$$(...)"`: `kind`), ends an extglob
    opener: an unescaped `@`, `+`, `!`, `?` or `*` right before it."""
    return kind not in ("{", "$") and i > 0 and cmd[i - 1] in "@+!?*" and esc != i - 1


def _in_redirect(cmd: str, i: int, esc: int) -> bool:
    """Whether the `|` or `&` at `i` belongs to a redirect: `>|`, `>&`, `<&` (after an unescaped `>` or `<`),
    `&>` / `&>>`, or zsh's `>&|` and `>>&|` (a `|` after an unescaped `>&`)."""
    c = cmd[i]
    after = i > 0 and esc != i - 1 and cmd[i - 1] in (">" if c == "|" else "<>")
    zsh = c == "|" and i > 1 and cmd[i - 2:i] == ">&" and esc not in (i - 1, i - 2)
    return c in "|&" and (after or zsh or (c == "&" and cmd[i + 1:i + 2] == ">"))


def _words(seg: str, targets=None, events=None, pid: bool = True, flags=None, sq: bool = True, extra=None) -> list:
    """The shell words of `seg` as bash reads quotes and backslashes, with the quotes removed: `"$AP2_RUN_DIR"/../x`
    is one word, `$AP2_RUN_DIR/../x`, and `"$AP2_"DATA` is `$AP2_DATA`; a backslash-newline outside single quotes is
    no character at all, and a lone backslash at the very end is none either (bash drops it). Inside `$(...)`, even
    within `"..."`, quoting starts afresh, as in `_segments`, and a
    backtick runs up to the next unescaped backtick (whatever quotes lie between), read as words of its own (a run of
    empty ones, ```` `` `` ````, is one; a text that is one plain word is that word, read with no recursion). Inside
    `"..."` a `${...}` and the `(...)` of a `$$(` read quotes afresh too, and stay part of the word. A word that a
    `$(`, a `$$(` or an opening backtick ends keeps that `$` or backtick (`$AP2_RUN_DIR$`), so no command's output
    passes for a plain path, and, when `events` is a list, a backtick that opens a word is a word of its own
    (`` ` ``), as `$(`'s `$` is; the closing `)` or backtick is no part of a word. A `$"..."` is `"..."` (its `$`
    goes), and a `$'...'` is decoded as bash 3.2 and zsh both pass it (`_ansi_c`: `$'\\x63p'` is `cp`, a whole word
    of NULs is no word, `\\u2014` stays text; a `\\u0063` is read as decoded, `c`, and adds `$'` to `flags`:
    `_rebinds`, `_ap2_trusted`, bash 3.2 may read another name); one `_ansi_c` cannot decode (an escape the two shells
    read differently, `\\d`, `\\cX`, `\\C-X`, `\\M-X`, a NUL in a longer word, no closing quote) keeps its `$` (a value
    filled in at run time), a backslash in it keeping the next character, and adds `$'` and `$'?` to `flags`
    (`_command_writes` then denies a command that names a guarded path anywhere). `pid` is the
    reading of `"$$(`, and `sq` that of a `'` inside `"${...}"`, as in `_segments`. Each word is a `_Word`: its `mask`
    hides each glob, bracket, brace, comma, paren, quote, `>` or `=` that a quote or a backslash made text (outside
    `"${...}"`, where bash 3.2 ends the quote at an inner `"`).
    When `targets` is a list, the word after each unquoted `>` (in `>`, `>>`, `>|`, `>&`, `&>`, `<>`, and zsh's
    `>!`, `>>!`, `>&|`, `>&!`, `>>&|` and `>>&!`) is added to it too: the file that redirect writes. When `events`
    is a list, each `$(` or opening backtick, and each `<(`, `>(` and zsh's `=(` (a `=(` that starts a word), adds
    `(index, True)` to it and each `)` or backtick that closes one `(index, False)`, `index` being that of the next
    word: where a command starts and where it ends, for `_commands`.
    An unquoted `@(`, `+(`, `!(`, `?(` or `*(` (the `(` right after one of those characters, unescaped, in a word)
    is an extglob, as bash reads it with `shopt -s extglob` (and as a syntax error without it): its `(...)` stays in
    the word, with no word break inside (`$X/.@(. |x)/c` is one word). Any other unquoted `(` inside a word (zsh
    reads `$X/(a)` as a glob group, bash as a syntax error) stays at the end of the word it ends (`$X/(`). When
    `flags` is a set, `{` is added to it for an unquoted `{` that is not the `{` of `${` (bash brace-expands only
    such a `{`: `echo "{"a,b}` prints `{a,b}`) and for any `{` inside a `"${...}"` (bash 3.2 ends the quote at an
    inner `"`, so `x=; echo "${x:-"{a,b}"}"` prints `a b`), `(` for a `(` inside a word (what `_level` refuses
    after the run directory), and `()` for an unquoted `(` with only blanks before its `)` (a function
    definition, `ap2() {`), and `)`, `]]` and `}` for an unquoted one (a `)` that closes no substitution, a `}` not
    that of a `${`: `_rebinds`). When `extra` is a dict of lists, `extra["ops"]` gets the index of each word that
    is part of a redirect (the word after an unquoted `<` or `>` operator, and a digit run right before one, as
    the `2` of `2>&1`), `extra["glued"]` that of each word that goes on, with no blank, after a substitution closed
    (the `/x` of `f=$(pwd)/x`: one shell word with the word before the `$(`), and `extra["loose"]` the index of the
    first word after each unquoted closer (`)`, `]]`, or a `}` that is no `${`'s and does not close an empty `{}`
    pair, as in `-exec ... {} +` or `xargs -I{}`)."""
    words, text, hid, i, n = [], [], [], 0, len(seg)  # hid: the indexes in `text` of the characters quoted text
    esc = -1  # the last escaped index
    # The quote state, as in `_segments` (`s`: a `'` inside `"${...}"`, which hides no `$(` or backtick): quote,
    # open `(`, open `${`, the context's kind, and the open `(` of an extglob (`eglob`); `stack` holds the outer
    # contexts' states.
    stack, quote, paren, brace, kind, eglob = [], None, 0, 0, None, 0
    redir = 0  # whether the next word is a redirect's target (1) or operand (2)
    glue = False  # whether a substitution just closed, with no blank after it yet
    dq = 0  # how many `"${` contexts are open
    ops, glued, loose = (extra["ops"], extra["glued"], extra["loose"]) if extra is not None else ([], [], [])
    while i < n:
        c = seg[i]
        # A run of characters that only go into the word, read at once: outside quotes (not inside `"${...}"`,
        # `"$$(...)"` or an extglob), and inside `"..."`.
        if quote is None and c not in _WORD_SPECIAL and (kind is None or kind == "(") and not eglob:
            j = _ORDINARY.match(seg, i).end()
            text.extend(seg[i:j])
            i = j
            continue
        if quote == '"' and c not in _DQ_SPECIAL:
            j = _DQ_ORDINARY.match(seg, i).end()
            text.extend(seg[i:j])
            i = j
            continue
        if dq and c == "{" and flags is not None:
            flags.add("{")  # inside `"${...}"` bash 3.2 ends the quote at an inner `"`: `"${x:-"{a,b}"}"` is `a b`
        if quote == "'":
            if c == "'":
                quote = None
            else:
                if c in _GLOBS and not dq:
                    hid.append(len(text))
                text.append(c)
        elif quote == "$'":
            if c == "'":
                quote = None
            else:
                if c == "\\" and i + 1 < n:
                    i += 1
                    c = seg[i]
                if c in _GLOBS and not dq:
                    hid.append(len(text))
                text.append(c)
        elif quote == "s" and (c == "'" or (c == "\\" and seg[i + 1:i + 2] not in ("$", "`", "\\"))):
            if c == "'":
                quote = None  # bash closes it at the next `'`, a backslash before it or not
            else:
                text.append(c)
        elif c == "\\" and i + 1 < n:
            i += 1
            esc = i
            if seg[i] != "\n":  # a line continuation: both characters go
                if seg[i] in _GLOBS and not dq:
                    hid.append(len(text))
                text.append(seg[i])
        elif c == "\\":
            pass  # a lone backslash at the very end: bash drops it
        elif c == "$" and (new := _opens(seg, i, esc, quote, pid)):
            stack.append((quote, paren, brace, kind, eglob))
            quote, paren, brace, kind, eglob = None, 0, int(new == "{"), new, 0
            if new == "{":
                dq += 1
                text.append("${")  # still the `"..."` word
            else:  # a `$(` or `$$(` ends the word, keeping its `$`
                text.append(c)
                redir = _flush_word(words, text, hid, targets, redir, ops, glued, glue)
                glue = False
                if new == "(" and events is not None:
                    events.append((len(words), True))
            i += 2
            continue
        elif c == "`":
            end = _tick_end(seg, i + 1)
            if end == i + 1 < n:  # a run of empty ones is one empty substitution, as bash expands it
                end = _EMPTY_TICKS.match(seg, i).end() - 1
                i = end - 1
            if text or events is not None:  # the word it ends keeps it; at a word's start it is a word of its own
                text.append(c)
                redir = _flush_word(words, text, hid, targets, redir, ops, glued, glue)
            glue = False
            base = len(words)
            if end == i + 1:  # an empty text: no words
                if events is not None:
                    events += ((base, True), (base, False))
            elif (_PLAIN_WORD if flags is not None or extra is not None else _PLAIN_TEXT).fullmatch(seg, i + 1, end):
                # One plain word (the `_` of a cut-out text; with no flags or lists kept, a brace or bracket too).
                words.append(_Word(seg[i + 1:end]))
                if events is not None:
                    events += ((base, True), (base + 1, False))
            elif events is not None or extra is not None:
                inner, more = [], {"ops": [], "glued": [], "loose": []}
                sub = _words(seg[i + 1:end], targets, inner, pid, flags, sq, more)
                if events is not None:
                    events.append((base, True))
                    events.extend((base + k, opens) for k, opens in inner)
                    events.append((base + len(sub), False))
                for name, out in (("ops", ops), ("glued", glued), ("loose", loose)):
                    if more[name]:
                        out.extend(base + k for k in more[name])
                words += sub
            else:  # only the words (`names`): no lists to keep
                words += _words(seg[i + 1:end], targets, None, pid, flags, sq)
            glue = True
            i = end + 1
            continue
        elif quote == '"':
            if c == '"':
                quote = None
            else:
                if c in _GLOBS and not dq:
                    hid.append(len(text))
                text.append(c)
        elif quote == "s":
            text.append(c)  # inside `"${...}"`: still the `"..."` word
        elif c == "'":
            if seg[i - 1:i] == "$" and _expands(seg, i - 1, esc):  # `$'...'`: decoded, as the shell passes it
                decoded = []
                close = _ansi_c(seg, i + 1, decoded, False, flags, len(text) == 1)
                if close is not None:
                    text.pop()  # the `$` is not part of the word
                    for ch in decoded:
                        if ch in _GLOBS and not dq:
                            hid.append(len(text))
                        text.append(ch)
                    i = close + 1
                    continue
                # One `_ansi_c` cannot decode as both shells would (`\d`, `\C-@`, a NUL in a word, no closing
                # quote): its `$` stays (a value filled in at run time), the segment may run any name (`_rebinds`,
                # `_ap2_trusted`), and the command is denied when it names a guarded path anywhere (`$'?`).
                if flags is not None:
                    flags.update(("$'", "$'?"))
                quote = "$'"
            elif kind == "{":
                if sq:
                    quote = "s"
                else:
                    text.append(c)  # zsh: a plain character in `"${...}"`, so `"${x:-'}"` closes at that `}`
            else:
                quote = c
        elif c == '"':
            if text and text[-1] == "$" and seg[i - 1] == "$" and _expands(seg, i - 1, esc):
                text.pop()  # `$"..."` (a locale string) is `"..."`: the `$` is not part of the word
            quote = c
        elif c == "$" and seg.startswith("${", i) and _expands(seg, i, esc):
            brace += 1
            text.append("${")
            i += 2
            continue
        elif c == "}" and brace:
            brace -= 1
            text.append(c)
            if not brace and kind == "{":
                dq -= 1
                quote, paren, brace, kind, eglob = stack.pop()
        elif kind in ("{", "$"):  # inside `"${...}"` or `"$$(...)"`: still the `"..."` word, no break here
            if c == "(" and kind == "$":
                paren += 1
            elif c == ")" and kind == "$":
                if paren:
                    paren -= 1
                else:
                    quote, paren, brace, kind, eglob = stack.pop()
            text.append(c)
        elif eglob and c in _BREAK:  # inside an extglob's `(...)`: no word break
            eglob += (c == "(") - (c == ")")
            text.append(c)
        elif c == "(" and text and seg[i - 1] in "@+!?*" and esc != i - 1:  # an extglob: it stays in the word
            eglob = 1
            text.append(c)
            if flags is not None:
                flags.add("(")
        elif c in _BREAK:
            opener = False  # a `<(`, `>(` or zsh's `=(`: a command runs in there
            if c == "(":
                opener = (not text and i and seg[i - 1] in "<>" and esc != i - 1) or text == ["="]
                if text:  # `x(`: a zsh glob group (`$AP2_RUN_DIR/(a)`), a `=(`, a function name; bash: an error
                    text.append(c)  # the `(` stays in the word it ends
                    if flags is not None:
                        flags.add("(")
                if flags is not None and _EMPTY_PARENS.match(seg, i):
                    flags.add("()")  # `name()`, `name ()`: a function definition
            elif c in "<>" and text and "".join(text).isdigit():
                redir |= 2  # the `2` of `2>&1`: part of the redirect
            redir = _flush_word(words, text, hid, targets, redir, ops, glued, glue)
            glue = False
            if opener:
                stack.append((quote, paren, brace, kind, eglob))
                quote, paren, brace, kind, eglob, redir = None, 0, 0, "(", 0, 0
                if events is not None:
                    events.append((len(words), True))
            elif c == "(":
                paren += 1
            elif c == ")":
                if paren or not stack:
                    paren -= bool(paren)
                    if flags is not None:
                        flags.add(")")  # no `)` that closes a substitution
                    loose.append(len(words))
                else:
                    if events is not None:
                        events.append((len(words), False))
                    quote, paren, brace, kind, eglob = stack.pop()
                    glue = True
            elif c == "<":
                redir |= 2
            elif c == ">":
                redir = 3
                if seg.startswith("!", i + 1):
                    i += 1  # zsh's `>!` (noclobber override, as `>|`): the `!` is part of the operator
                elif seg.startswith(("&!", "&|"), i + 1):
                    i += 2  # zsh's `>&!` and `>&|` (`>>&!`, `>>&|`): the `&` and the `!` or `|` too
        else:
            if flags is not None and (c in "{}" or seg.startswith("]]", i)):
                flags.add("]]" if c == "]" else c)
            if seg.startswith("]]", i) or (c == "}" and not (
                    text and text[-1] == "{" and not (hid and hid[-1] == len(text) - 1))):
                loose.append(len(words) + 1)  # a closer inside the word being read: the words after it
            text.append(c)
        i += 1
    _flush_word(words, text, hid, targets, redir, ops, glued, glue)
    return words


def _flush_word(words: list, text: list, hid: list, targets, redir: int, ops: list, glued: list, glue: bool) -> int:
    """`_flush`, noting the word's index in `ops` (it is part of a redirect, `redir` & 2) and in `glued` (`glue`: it
    goes on right after a substitution closed)."""
    if text:
        if redir & 2:
            ops.append(len(words))
        if glue:
            glued.append(len(words))
    return _flush(words, text, hid, targets, redir)


# A run of empty backtick substitutions (`` ```` ``): read at once, as one.
_EMPTY_TICKS = re.compile(r"(?:``)+")
# A backtick text that is one plain word (no blank, quote, backslash, `$`, operator, paren, brace or bracket), as
# the `_` that stands for a cut-out text: `_words` reads it as that word, with no recursion.
_PLAIN_WORD = re.compile(r"[^\s\\$'\"`()<>;|&{}\[\]]+")
_PLAIN_TEXT = re.compile(r"[^\s\\$'\"`()<>;|&]+")
# The characters a quote or a backslash makes text for globbing and brace expansion (`_Word.mask` hides them), the
# quotes and `>`, which then end a file name in code inside quotes (`_text_end`), and `=`, which then makes no
# assignment (`_sets_path`: `rg "path=" lib`).
_GLOBS = frozenset("*?[]{},()'\">=")
_HIDE = "\x00"  # what `_Word.mask` puts for such a quoted character: it matches no glob, part or name
# The characters `_words` acts on outside quotes and inside `"..."`: a run of any others goes into the word at once.
_WORD_SPECIAL = _BREAK | frozenset("\\$'\"`{}]")
_ORDINARY = re.compile(r"[^\\$'\"`{}\] \t\r\n;|&<>()]*")
_DQ_SPECIAL = _GLOBS | frozenset("\"\\$`")
_DQ_ORDINARY = re.compile(r"[^\"\\$`*?\[\]{},()'>=]*")


class _Word(str):
    """A shell word, with its quotes removed. `mask` is the same text with each character in `_GLOBS` that a quote
    or a backslash made text as `_HIDE` (one character for one, so its indexes stay those of the word), or None
    when there is none: bash and zsh glob and brace-expand only unquoted ones."""
    mask = None


def _flush(words: list, text: list, hid: list, targets, redir: int) -> int:
    """Ends the word in `text` (`hid`: the indexes of its quoted `_GLOBS` characters), if any, adding it to
    `targets` too when it is a redirect's target (`redir` & 1), and empties `text` and `hid`; returns `redir` for
    the next word: unchanged when there was no word, else 0."""
    if not text:
        return redir
    word = _Word("".join(text))
    if hid:
        hidden = set(hid)
        word.mask = "".join(_HIDE if k in hidden else t for k, t in enumerate(text))
    text.clear()
    hid.clear()
    words.append(word)
    if redir & 1 and targets is not None:
        targets.append(word)
    return 0


_NEVER = re.compile(r"(?!)")  # matches no word
# xargs flags that take the next word as their value (`xargs -n 1 cmd`).
_XARGS_VALUE = frozenset(("-E", "-I", "-J", "-L", "-P", "-R", "-S", "-a", "-d", "-n", "-s"))
# Commands that run the command named after their flags (`env FOO=1 cmd`, `nice -n 5 cmd`, zsh's `noglob cmd`):
# name -> (the flags that take no value, as a regex; the flags that take the next word). Any other flag (`env -S`,
# `command -v`) leaves the next word an argument.
_RUNNERS = {"xargs": (re.compile(r"-.*", re.S), _XARGS_VALUE), "env": (re.compile(r"-[iv]*"), frozenset(("-u",))),
            "nohup": (re.compile(r"--"), frozenset()), "command": (re.compile(r"-p|--"), frozenset()),
            "nice": (re.compile(r"-\d+"), frozenset(("-n",))),
            # zsh's precommand modifiers, and `repeat N` (a count, then the command; any other count word leaves
            # the next word an argument, fail-closed: an `ap2` there is no ap2 call).
            "noglob": (_NEVER, frozenset()), "nocorrect": (_NEVER, frozenset()), "-": (_NEVER, frozenset()),
            "repeat": (re.compile(r"\d+"), frozenset())}
# A `NAME=value` word (also `NAME+=value` and `NAME[i]=value`): before a command name, or after `env`, it sets a
# variable.
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=")
# `time -p` (POSIX output) still runs the next word. A module constant, not a literal in `_commands`: a function
# holding the "-p" literal reads as a session launch to tests/test_ap2_profile_launch.py.
_TIME_POSIX = "-p"
# Words that can make a name run something else in the same shell: an alias, a `hash` entry, a builtin turned
# off, a function (`function name`, zsh's `autoload` of one found on `fpath`), and the ways to run text that does
# one of those (`eval`, `source`, `trap`, whose action text runs in this shell on a signal, `DEBUG`, `ERR`,
# `EXIT` or `RETURN`; `.` too, but only as a command, `_rebinds`).
_REBIND = frozenset(("alias", "unalias", "hash", "enable", "builtin", "function", "functions", "eval", "source",
                     "autoload", "trap"))
# A word that runs a quoted script as code (`bash -c '...'`, `eval '...'`): a shell's name or `eval`, alone or after
# a character no name holds (`/bin/sh`, zsh's `=sh`, `$@eval` with `$@` empty), so `ssh` and `run.sh` are none.
_SHELL = re.compile(r"(?<![A-Za-z0-9_.])(?:sh|bash|zsh|ksh|dash|eval)\Z")
# zsh among those: a fresh zsh has bare glob qualifiers on, so `/(e:'cmd':)` in its script runs `cmd`.
_ZSH = re.compile(r"(?<![A-Za-z0-9_.])zsh\Z")


def _unbrace(word: str) -> str:
    """`word` with an unquoted `}` at its end dropped: zsh closes a brace list there, so `{ ... | sh}` runs `sh`."""
    if word.endswith("}") and (getattr(word, "mask", None) is None or word.mask[-1] != _HIDE):
        return word[:-1]
    return word


# zsh's `always` word (`{ a } always { b }`), up to a character that ends a word.
_ALWAYS = re.compile(r"always(?![^\s;&|(){}<>])")


def _commands(words: list, events: list, ops=frozenset(), glued=frozenset()):
    """The indexes of the words bash runs as a command name: the first word after the keywords, the words of a
    redirect (`ops`, from `_words`: `>/dev/null hash ...`, `2>&1 hash ...`) and `NAME=value` words (`x+=1`,
    `a[1]=1`, `x=$(...)`; and a `-p` after `time`) from the segment's start and from each `$(...)`, backticks,
    `<(...)`, `>(...)` or `=(...)` that opens (`events`, from `_words`; one that closes first, as in `$(time)`,
    names none), and the word a runner (`_RUNNERS`: `xargs`, `env`, `nohup`, `command` (also `command --`), `nice`,
    zsh's `noglob`, `nocorrect`, `-` and `repeat N`) runs after its flags. When a substitution closes, the reading
    goes on as it was where it opened, and a word glued to it (`glued`: the `/x` of `f=$(pwd)/x`) is part of the
    word before it. Also the segment's own command (`lead`): the last of that chain from the segment's start
    (`nohup env A=1 <cmd>` is `<cmd>`), or None; and the words that may run as a command after all (`maybe`): after
    a command word that holds a `$` or a backtick (`$e hash ...`, `$(true) hash ...`: it may expand to nothing) or
    that is the count of zsh's `repeat` and no plain number (`repeat 1+1 hash ...`), the reading goes on as if that
    word were not there, its command words going to `maybe` (and on again after such a word: `$(true) $(true) .
    f`, `` `true` command -- . f ``)."""
    out, lead, want, top, run, value, e = [], None, True, True, None, False, 0
    maybe, soft, stack, chosen = set(), False, [], None  # soft: a command word found now only may run (`maybe`)
    for k, w in enumerate(words):
        while e < len(events) and events[e][0] <= k:
            if events[e][1]:
                stack.append((want, top, run, value, soft))
                want, top, run, value, soft = True, False, None, False, False
            elif stack:
                want, top, run, value, soft = stack.pop()
            else:
                want, top, run, value, soft = False, False, None, False, False
            e += 1
        if k in glued or k in ops:
            continue
        if run is not None:
            plain, values = _RUNNERS[run]
            if value:
                value = False
                continue
            if w in values:
                value = True
                continue
            if plain.fullmatch(w) or (run == "env" and _ASSIGN.match(w)):
                continue
            # An unknown flag: no command after it; zsh's `-` modifier is a command word (`nocorrect - cmd`).
            want, run = not w.startswith("-") or w == "-", None
        if want and w not in _KEYWORDS and not _ASSIGN.match(w) and not (
                w == _TIME_POSIX and k and words[k - 1] == "time"):
            if soft:
                maybe.add(k)
            else:
                out.append(k)
                if top:
                    lead = k
            name = w.rsplit("/", 1)[-1]
            empty = "$" in w or "`" in w or (k > 0 and words[k - 1] == "repeat" and chosen == k - 1)
            chosen = k
            want, run, soft = empty, name if name in _RUNNERS else None, empty
    return out, lead, maybe


def _runs_ap2(words: list, lead) -> bool:
    """Whether the segment of `words` runs `ap2`: its own command (`lead`, from `_commands`) is `ap2` or ends in
    `/bin/ap2`."""
    return lead is not None and (words[lead] == "ap2" or words[lead].endswith("/bin/ap2"))


def _command_after(s: str, j: int) -> bool:
    """Whether a command word follows, from `j` on, a `)` that closes a subshell (or nothing) at an `ap2` segment's
    top level: after it bash takes only redirects (`) 2>/dev/null`) and then a separator, another `)` or the end,
    so any other word is a command after a case pattern (`ap2) cp ...`, `ap2) 2>/dev/null cp ...`). A redirect
    target is read up to a blank or an operator character, and a quote in it can make its rest a word (a lost
    exemption, fail-closed). The scan stops at the next `)` or separator, so the scans of a segment's `)`s do not
    overlap."""
    return _scan_after(s, j)[0]


def _scan_after(s: str, j: int):
    """`_command_after(s, j)`, and the index where its scan stopped."""
    n = len(s)
    while True:
        while j < n and s[j] in " \t":
            j += 1
        k = j
        while k < n and s[k].isdigit():
            k += 1
        if k < n and (s[k] in "<>" or s.startswith("&>", k)):  # a redirect: its operator, then its target word
            k += 1
            while k < n and s[k] in "<>&|":
                k += 1
            while k < n and s[k] in " \t":
                k += 1
            while k < n and s[k] not in " \t\n;|&<>()":
                k += 1
            j = k
            continue
        return j < n and s[j] not in ";|&)\n", j


def _runs_more(segment: str, pid: bool = True, sq: bool = True) -> bool:
    """Whether an `ap2` segment also runs other shell code, so its exemption ends: outside single quotes a `$(`
    (not `$((`), a backtick, `<(` or `>(`, or zsh's `${(` (a flag such as `(e)` runs the value as code); outside
    any quotes a `((` (`((ap2 x | cp ...) )` is two subshells, and an unquoted `$((cmd) )` runs `cmd`), a `$[`
    (arithmetic, where a `'` hides no `$(`), and zsh's `=(` (it runs a command, as `<(` does) or an `always` word
    (`{ ap2 x } always { cmd }` runs `cmd`); inside `"..."` a `$((` that closes with `) )` (`"$((cmd) )"` runs `cmd`
    too); its first word after the keywords followed by `(` (a function named `ap2`); at its top level a `)` with a
    command after it, past any redirects (`_command_after`: `case x in ap2) cp ...`, `ap2` was a case pattern), and
    likewise an unquoted `}` that ends a word, not that of a `${` (zsh ends a `{ ... }` list there and runs the next
    `{ ... }` of a short loop: `while { ap2 x } { cmd }`); or, at its end, a quote, a `"${`, a `"$$(` or a `"$((`
    still open (a reading that went wrong keeps no exemption). Quotes nest as in `_words`
    (`"${x:-'}'}"`, `"$$(echo "'")"` in the `pid` reading); a `'` inside `"${...}"` hides no `$(` or backtick, and
    with `sq` False (zsh) it is a plain character there."""
    if _FUNC.match(segment):
        return True
    i, n, esc = 0, len(segment), -1
    done = 0  # where the last `}` scan stopped: a `}` before it ends a redirect target that scan read
    # As in `_words`; `arith` holds the open single `(` of each `$((` inside `"..."` (its own extra, per context).
    stack, quote, paren, brace, kind, arith = [], None, 0, 0, None, []
    while i < n:
        c = segment[i]
        word = quote == '"' or (quote is None and kind in ("{", "$"))  # inside a `"..."` word
        if quote == "'":
            if c == "'":
                quote = None
        elif quote == "$'":
            if c == "\\":
                i += 1
            elif c == "'":
                quote = None
        elif quote == "s" and (c == "'" or (c == "\\" and segment[i + 1:i + 2] not in ("$", "`", "\\"))):
            if c == "'":
                quote = None
        elif c == "\\":
            i += 1
            esc = i
        elif c == "`" or segment.startswith(("<(", ">("), i) or (
                quote is None and kind not in ("{", "$") and segment.startswith("((", i)) or (
                segment.startswith(("$(", "${("), i) and not segment.startswith("$((", i)
                and _expands(segment, i, esc)) or (
                quote is None and segment.startswith("$[", i) and _expands(segment, i, esc)):
            return True
        elif quote is None and kind not in ("{", "$") and (segment.startswith("=(", i) or (
                c == "a" and _ALWAYS.match(segment, i) and (i == 0 or segment[i - 1] in " \t\n;&|(){}"))):
            return True  # zsh: `=(cmd)` runs `cmd`, `{ ... } always { cmd }` runs `cmd`
        elif c == "$" and word and segment.startswith("$((", i) and _expands(segment, i, esc):
            arith.append(0)
            i += 3
            continue
        elif c == "$" and (new := _opens(segment, i, esc, quote, pid)):  # `"${` or `"$$(`: a `$(` returned above
            stack.append((quote, paren, brace, kind, arith))
            quote, paren, brace, kind, arith = None, 0, int(new == "{"), new, []
            i += 2
            continue
        elif quote == "s":
            pass  # inside `"${...}"`: a `'` hides the end only
        elif c in "()" and arith:
            if c == "(":
                arith[-1] += 1
            elif arith[-1]:
                arith[-1] -= 1
            elif segment.startswith("))", i):
                arith.pop()
                i += 2
                continue
            else:  # `$((cmd) )`: two subshells, so `cmd` runs
                return True
        elif c == '"':
            quote = None if quote else c
        elif c == "'" and not quote:
            if segment[i - 1:i] == "$" and _expands(segment, i - 1, esc):
                quote = "$'"
            elif kind == "{":
                quote = "s" if sq else None  # zsh: a plain character in `"${...}"`
            else:
                quote = "'"
        elif quote is None and c == "$" and segment.startswith("${", i) and _expands(segment, i, esc):
            brace += 1
            i += 2
            continue
        elif quote is None and c == "}" and brace:
            brace -= 1
            if not brace and kind == "{":
                quote, paren, brace, kind, arith = stack.pop()
        elif quote is None and c == "}" and not paren and not stack and i >= done and (
                i + 1 == n or segment[i + 1] in " \t\n;|&()<>"):
            # zsh: a `}` that ends a word ends a `{ ... }` list, and the next `{ ... }` of a short loop runs
            # (`while { ap2 x } { cmd }`): a command after it, past any redirects, ends the exemption.
            more, done = _scan_after(segment, i + 1)
            if more:
                return True
        elif quote is None and c == "(" and kind != "{":
            paren += 1
        elif quote is None and c == ")" and kind != "{":
            if paren:
                paren -= 1
            elif stack:
                quote, paren, brace, kind, arith = stack.pop()
                i += 1
                continue
            if not paren and not stack:
                more, done = _scan_after(segment, i + 1)
                if more:
                    return True
        i += 1
    return quote is not None or bool(stack) or bool(arith)


def _after_first(piece: str, first, then) -> bool:
    m = first.search(piece)
    return bool(m) and bool(then.search(piece, m.end()))


def _sed_in_place(text: str) -> bool:
    """v1's `\\bsed\\s+(-[a-zA-Z]*\\s+)*-i`, widened: a `sed` with an in-place flag (`_IN_PLACE`: `-i`, a flag cluster
    with an `i` or `I` such as `-ni` or `-i.bak`, `--in-place`) as any blank-separated word after it, so
    `sed -e 's/a/b/' -i '' f` is found too. `text` is one segment (its separators are quoted text), so the scan
    runs to its end, once, from its first `sed` (fail-closed: a quoted word that only looks like a flag counts)."""
    m = _SED.search(text)
    return bool(m) and any(_IN_PLACE.match(t) for t in text[m.end():].split())


def _writes(text: str) -> bool:
    """v1's `_WRITE.search(text)`, in linear time (its sed form widened): `sed` with an in-place flag after it
    (`_sed_in_place`), `dd` with an `of=` after it
    and no `|`, `;` or `&` between, and `open(` with a `, 'w'` (or a, x, +) mode after it and no `)` between, are
    found piece by piece. A text with a backslash is searched again with each escape that `printf` or `echo` turn
    into a blank or a newline (`_BLANK_ESCAPE`: `\\n`, `\\t`, `\\012`, `\\x0a`, ...) read as a blank, so the verb
    after it is a word of its own (`printf 'echo hi\\nrm <data>/x' | sh`)."""
    if _writes_as_is(text):
        return True
    blanked = _BLANK_ESCAPE.sub(" ", text) if "\\" in text else text
    return blanked != text and _writes_as_is(blanked)


def _writes_as_is(text: str) -> bool:
    return bool(_WRITE.search(text) or _sed_in_place(text)
                or any(_after_first(p, _DD, _OF) for p in re.split(r"[|;&]", text))
                or any(_after_first(p, _OPEN, _MODE) for p in text.split(")")))


# The backslash escapes `printf` and `echo` (`echo -e`, zsh's `echo`) print as a blank or a line break: `\n`, `\t`,
# `\v`, `\f`, the octal ones (`\11`, `\012`, `\0012`), the hex ones (`\x9`, `\x0a`) and `\u`/`\U` ones.
_BLANK_ESCAPE = re.compile(r"\\+(?:[ntvf]|0{0,2}1[123]|x0?[9aAbBcC]|[uU]0{0,7}[9aAbBcC])")


def _harmless(text: str) -> str:
    """`text` with v1's `_HARMLESS` redirects replaced by a space (a digit run with no redirect after it stays)."""
    return _HARMLESS.sub(lambda m: m.group(0) if m.group(0).isdigit() else " ", text)


def _joined(segs: list) -> list:
    """`segs` with each heredoc body line that ends in a backslash joined to the body line after it (that last
    backslash and the newline dropped), as bash joins an odd run of them in an unquoted heredoc and as a shell fed
    the body (`bash <<'EOF'`) does. An even run is joined too, fail-closed: an unquoted heredoc turns `\\\\` into
    `\\`, so `bash <<EOF` joins that line as well. A joined line is `quoted` when each of its lines is."""
    out, parts, quoted = [], [], False
    for seg in segs:
        if isinstance(seg, _Body):
            if seg.endswith("\\"):
                quoted = (quoted or not parts) and seg.quoted
                parts.append(seg[:-1])
                continue
            if parts:
                parts.append(seg)
                seg = _body("".join(parts), quoted and seg.quoted)
                parts = []
        elif parts:
            out.append(_body("".join(parts), quoted))
            parts = []
        out.append(seg)
    if parts:
        out.append(_body("".join(parts), quoted))
    return out


def _deny() -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": REASON}}


def decide(payload: dict, env):
    """The hook's verdict for one tool call: a PreToolUse deny, or None (no opinion). None for any tool but Bash and
    outside a v2 worker (no `AP2_ROLE`)."""
    if not env.get("AP2_ROLE") or not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return None
    tool_input = payload.get("tool_input")
    cmd = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(cmd, str) or not cmd:
        return None
    marks = _Marks(env)
    if len(cmd) > MAX_COMMAND:
        return _deny() if marks.names(cmd) else None
    try:
        return _deny() if _command_writes(cmd, marks, 0, _Budget(BUDGET * len(cmd) + BUDGET_EXTRA)) else None
    except _Deep:  # read again too much: judged like a command too long to check
        return _deny() if marks.names(cmd) else None


# How deep `_command_writes` reads a script given to a shell inside a script given to a shell (`bash -c "sh -c
# '...'"`); past it, a script that names a guarded path is a write.
MAX_DEPTH = 2
# How much one judgement may read again (`_Budget`), in characters: BUDGET times the command's length, plus
# BUDGET_EXTRA, so a command up to about 16 KiB with a shell's script in it and a second reading stays inside.
BUDGET, BUDGET_EXTRA = 4, 16 << 10
# What one segment read again costs on top of its characters (`_Budget`): a fan-out of tiny segments (`$(a|)` many
# times over) is judged segment by segment, so the characters alone undercount the work.
SEGMENT_COST = 16


def _command_writes(cmd: str, marks: _Marks, depth: int, budget: _Budget) -> bool:
    """Whether the command text `cmd` writes into a guarded path; `depth` is how many shells' scripts it sits in,
    and `budget` how much more the judgement may read again (each reading past the first spends `cmd`'s length;
    a segment every reading reads alike is parsed and judged once, `_alike`)."""
    # Options that change how words read (extglob, zsh's `setopt`; zsh's `${(` is `_flag_group`, below): this
    # reading does not follow them, so a command with one that names a guarded path is a write.
    if (_extglob(cmd) or _options(cmd)) and marks.names(cmd):
        return True
    # bash 3.2 reads `"$$(...)"` with its own quotes, bash 5.3 at times as plain text, and zsh reads it as text and
    # a `'` inside `"${...}"` as a plain character: a command with a `$$(`, or with a `'` and a `${`, is judged in
    # each of those readings, and denied when one of them writes.
    readings = [(True, True)] + [(False, True)] * ("$$(" in cmd) + [(False, False)] * ("'" in cmd and "${" in cmd)
    done = []  # each reading: (pid, sq, segments, `_Parsed` segments)
    for k, (pid, sq) in enumerate(readings):
        if k:
            budget.spend(len(cmd))
        seen = set()
        segs = _segments(cmd, pid, budget, seen, sq)
        # A heredoc body a shell reads is a script of its own (`bash <<'EOF'`): read whole, one level deeper.
        if any(_scripts_write([s], marks, depth, budget, s.zsh) for s in segs if isinstance(s, _Script)):
            return True
        segs = _joined([s for s in segs if not isinstance(s, _Script)])
        # Extglob may be on (an option name built at run time): `@(...)` is then one word with no comment or pipe
        # inside, which this reading does not follow. A heredoc it opened no body for (`<<`, `_segments`) may have
        # one in the shell, whose lines this reading takes for code: a quote there can hide the next command.
        if seen & {"@", "<<"} and marks.names(cmd):
            return True
        parsed = [_parse(seg, pid, sq, budget) for seg in segs]
        # zsh's `${(` flag group (`"${(e)x:-\$(cmd)}"` runs `cmd`), which this reading does not follow; and a
        # `$'...'` word bash 3.2 and zsh decode differently (`_ansi_c`: `~/<rel>/$'\A'ssistant` is the data folder in
        # zsh), which this reading keeps with its `$`: its text is also searched as each shell decodes it.
        if any(_flag_group(p) for p in parsed) and marks.names(cmd):
            return True
        if any(not p.body and "$'?" in p.flags for p in parsed) and (marks.names(cmd) or marks.names_decoded(cmd)):
            return True
        done.append((pid, sq, segs, parsed))
    # Whether `ap2` keeps its exemption: judged once, on every reading, before any segment is.
    trusted = _ap2_trusted(done)
    for pid, sq, segs, parsed in done:
        rebound = _rebinds(parsed)
        # A shell that reads its script from its input (`... | sh`, no `-c`): the words of each segment piped
        # before it are read as a script too (`echo 'tee <run>/settings-implement.json < f' | sh`), and those
        # segments are `fed` (`_writes_guarded`).
        fed, shells = set(), []
        if any(isinstance(seg, _Pipe) for seg in segs):
            shells = [(j, w) for j, p in enumerate(parsed) if not p.body and (w := _stdin_shell(p.words))]
            last, zsh = (shells[-1][0] if shells else 0), any(_ZSH.search(w) for _, w in shells)
            for j in range(last):
                if isinstance(segs[j], _Pipe):
                    if _scripts_write(parsed[j].words, marks, depth, budget, zsh):
                        return True
                    fed.add(j)
        # `... | xargs <writer>`, `<writer> $(<lister>)`, `... | sed 's/^/rm /' | sh`: what a command reads becomes
        # its words or its script.
        if _fed_writes(segs, parsed, marks, pid, sq, budget, dict(shells)):
            return True
        for j, (seg, p) in enumerate(zip(segs, parsed)):
            key = _alike(seg) and (seg, p.body, p.quoted, rebound, trusted, depth, j in fed)
            if key in budget.clean:
                continue  # judged in an earlier reading, which read it alike
            if _writes_guarded(seg, marks, pid, rebound, depth, budget, sq, p, j in fed, trusted):
                return True
            if key:
                budget.clean.add(key)
    return False


# A sed in-place flag among the words a fed command reads (`_fed_writes`): `-i`, `-i.bak`, a cluster of sed's own
# letters with an `i` or `I` (`-ni`, `-Ei`, macOS's `-I`; not `-print0`), `--in-place`.
_FED_IN_PLACE = re.compile(r"-[EIabelnrsuz]*[iI]|--in-place")
# What a segment's output may carry (`_fed_writes`): (the highest `_Marks.level` of its guarded-path mentions, an
# `of=`, a sed in-place flag, a write verb).
_NOTHING = (0, False, False, False)


def _either(a, b):
    return max(a[0], b[0]), a[1] or b[1], a[2] or b[2], a[3] or b[3]


# Every form `_writes` finds holds a letter or a `>`: a text with neither is no write (`$ _`, a substitution alone).
_VERBISH = re.compile(r"[A-Za-z>]")


def _fed_verb(seg: str, p, pid: bool, sq: bool) -> bool:
    """Whether the segment `seg` (`p` its `_Parsed`) holds a write verb in its words, its redirects aside (`| xargs
    -n1 basename > <run>/rm-list.txt` writes only the run directory, which the segment's own reading judges); also
    once each `$NAME` is taken out, as `_verb` does, when that changes the text, its redirect words again aside."""
    text = " ".join(w for k, w in enumerate(p.words) if k not in p.ops) + " "
    if _VERBISH.search(text) and _writes(_harmless(text)):
        return True
    bare = _VAR_REF.sub("", seg) if "$" in seg else seg
    if bare == seg:
        return False
    extra = {"ops": [], "glued": [], "loose": []}
    words = _words(bare, None, None, pid, None, sq, extra)
    ops = set(extra["ops"])
    return _writes(_harmless(" ".join(w for k, w in enumerate(words) if k not in ops) + " "))


def _fed_writes(segs: list, parsed: list, marks: _Marks, pid: bool, sq: bool, budget: _Budget, shells: dict) -> bool:
    """Whether a command of `segs` (`parsed` their `_Parsed`) writes a guarded path it reads from another one. What a
    segment's output may carry is its own words (a guarded path outside the own run directory, `_Marks.unsafe`, the
    package's `bin/ap2` run as a command aside, as in `_writes_guarded`; an `of=`; a sed in-place flag
    (`_FED_IN_PLACE`); a write verb), its heredoc bodies (`_Piped.bodies`), its command substitutions' output
    (`_Piped.subs`), the compounds it ends (`_Piped.closed`: all of their segments, `{ ls <data>; } | xargs rm`, `for
    ... done | xargs rm`) and what is piped into it (`_Piped.prev`, back to the start of the pipeline). It is a write:
    - when a segment that runs `xargs` (any of its words, quotes removed, by name or by its full path, after any
      runner: `timeout 5 xargs`, `${SUDO} xargs`) reads a guarded path (piped into it, or into a compound it starts
      a pipeline in, `_Piped.inherit`: `| if true; then xargs rm; fi`; a heredoc body it holds; or the input an
      earlier `exec` segment's input redirect gives the shell (`_input_fds`: fd 0 to every later command, fd N
      only to one that moves it onto its input with `<&N`), when that segment's words or process substitution,
      its whole pipeline to the closing `)`, name one: `exec < <(ls <data>); xargs rm`) and holds a write verb
      (`_fed_verb`), or a `dd` or a `sed` that may take an `of=` or an in-place flag from what it reads (`echo
      "of=<data>/x" | xargs dd`); or when such an `xargs` runs a shell that reads what it reads as code
      (`_xargs_code`: `xargs -I{} sh -c {}`, `xargs bash -c '$0'`; not `xargs -n1 bash -c 'echo "$0"'`) and that
      carries both a guarded path and a write verb, as for a shell below;
    - when a segment's command substitutions read a guarded path and a word the segment runs is such a writer
      (`_runs_writer`: `rm $(find <data> ...)`, `` chmod 600 `ls <data>/x` ``, `sudo rm $(...)`; not `wc -l $(ls
      <data>) > <run>/n`, a plain printer, nor a verb in quoted prose: `echo "next: rm $(ls <data> | head -1)"`);
    - when what is piped into a shell that reads its script from its input (`shells`, `_stdin_shell`; the input
      an `exec` gives it too) carries both a guarded path and a write verb (`find <data> | sed 's/^/rm /' | sh`);
      here only a path that is no run-directory path at all counts (`_Marks.level` 2): a run-directory path with a
      `$` after it is a script word the shell's
      own reading judges (`echo 'make > "$AP2_RUN_DIR/m.log"; echo "exit $?"' | sh`). Nor does text that reading
      already judged as the shell's exact script count (`gen`, kept beside what a segment carries): the words of a
      plain printer (`_printer`: `echo '...' | sh`) and a heredoc body that a plain `cat` passes on to a shell that
      reads it as its script (`_Chunk.scripted`: `cat <<'EOF' | bash`), each only when the printer or `cat` is the
      stage piped straight into a POSIX shell (`_SHELL`), outside any compound, and its text is exact
      (`_exact_script`); only text a command makes at run time does (a `sed`, `awk` or `find` and all it reads, a
      substitution's output).
    One pass in order: a segment comes after what it reads (its substitutions', its pipe's and its compounds'
    segments), and each compound keeps what its segments carry, passed on to the compounds around it until one
    already holds it, so the pass stays linear."""
    xargs, xshells = set(), set()
    for j, p in enumerate(parsed):
        if p.body:
            continue
        k = next((k for k, w in enumerate(p.words) if w.rsplit("/", 1)[-1] == "xargs"), None)
        if k is not None:
            xargs.add(j)
            if _xargs_code(p.words, k):
                xshells.add(j)
    execs = any(not p.body and _exec_input(seg, p) for seg, p in zip(segs, parsed))
    need = bool(shells or xshells or execs)  # the write-verb bit counts only for a shell's input
    index = {id(seg): j for j, seg in enumerate(segs)}
    # id of a `_Group`, a segment, a body chunk -> what it carries; `ggroups` and `gout`: what a shell's own
    # reading has not judged of it (`gen`)
    groups, out, chunks, verbs, ggroups, gout = {}, {}, {}, {}, {}, {}

    def verb_of(j):
        if j not in verbs:
            verbs[j] = _fed_verb(segs[j], parsed[j], pid, sq)
        return verbs[j]

    def writer(j, state=(0, True, True, True)):  # with no `state`: whether it may be one at all
        text = " ".join(w for k, w in enumerate(parsed[j].words) if k not in parsed[j].ops) + " "
        return (verb_of(j) or (state[1] and bool(_DD.search(text)))
                or (state[2] and bool(_SED.search(text))))

    if not (xargs or shells or execs or any(getattr(seg, "subs", None) is not None and not p.body
                                   and _runs_writer(seg, p, (0, True, True, True), pid, sq)
                                   for seg, p in zip(segs, parsed))):
        return False

    def carried(words, p=None, seg=None):
        if p is not None:
            written = set(p.targets)
            run = {k for k in p.commands if words[k] not in written and marks.is_ap2(words[k])}
            words = [w for k, w in enumerate(words) if k not in run]
            unsafe = marks.level(words, "{" in p.flags, "(" in p.flags)
        else:
            unsafe = marks.level(words)
        text = " ".join(words)
        verb = need and (verb_of(index[id(seg)]) if p is not None and not p.body else _writes(_harmless(text)))
        return (unsafe, "of=" in text and bool(_OF.search(text)),
                "-" in text and any(_FED_IN_PLACE.match(t) for t in text.split()), verb)

    def read(chunk):  # what the body lines of one heredoc chunk carry
        key = id(chunk)
        if key not in chunks:
            state = _NOTHING
            for b in chunk:
                j = index.get(id(b))
                if j is None:
                    budget.spend(len(b))
                    state = _either(state, carried(_words(b, None, None, pid, None, sq)))
                else:
                    state = _either(state, carried(parsed[j].words, parsed[j], b))
            chunks[key] = state
        return chunks[key]

    def source(seg, table):  # what the segment `seg` sends on (a pipe or a compound's input)
        state = table.get(id(seg))
        return state if state is not None else (2 * marks.names(seg), True, True, True)

    def keep(group, state, table):  # `group` and the compounds around it carry `state` too
        while group is not None:
            old = table.get(id(group), _NOTHING)
            new = _either(old, state)
            if new == old:
                return  # one around it already holds it as well
            table[id(group)] = new
            group = group.up

    # fd -> what an `exec` input redirect on it gives every later command of the shell (0: their input), and the
    # `(` group an `exec` segment opens (`exec < <(a | b)`) -> the fds that group's whole output goes to
    fds, pending = {}, {}

    def give(seg, state):  # the `exec` segment `seg` redirects its fds' input from what carries `state`
        targets = []
        for fd, dup in _input_fds(seg):
            fds[fd] = _either(_either(fds.get(fd, _NOTHING), state), fds.get(dup, _NOTHING))
            targets.append(fd)
        return targets

    def input_of(seg):  # what the segment `seg` reads from the shell's input: fd 0 and each fd it moves onto it
        state = fds.get(0, _NOTHING)
        for fd, dup in _input_fds(seg) if fds and "<&" in seg else ():
            if fd == 0 and dup is not None:
                state = _either(state, fds.get(dup, _NOTHING))
        return state

    def shell_reads(j, state):  # the segment j runs a shell that reads `state` as its script
        return state[0] == 2 and state[3] and (j in shells or _stdin_shell(parsed[j].words) is not None)

    for j, (seg, p) in enumerate(zip(segs, parsed)):
        if p.body:
            continue
        lone = not isinstance(seg, _Piped)
        if lone or not (p.words or seg.closed or seg.bodies or seg.prev):
            if p.words and _exec_input(seg, p):
                give(seg, carried(p.words, p, seg))
            stdin = input_of(seg) if fds else _NOTHING
            if stdin[0] and ((j in xargs and (writer(j, stdin) or (j in xshells and stdin[0] == 2 and stdin[3])))
                             or (p.words and shell_reads(j, stdin))):
                return True
            continue  # a blank segment (a newline inside `$(`) carries nothing
        state = carried(p.words, p, seg)
        bodies = gbodies = _NOTHING
        # Only a stage piped straight into a POSIX shell, outside any compound, passes on the shell's exact script.
        exact = (isinstance(seg, _Pipe) and seg.group is None and not seg.closed and j + 1 < len(segs)
                 and getattr(segs[j + 1], "prev", None) is seg and bool(_SHELL.search(shells.get(j + 1) or "")))
        plain_cat = exact and [w for k, w in enumerate(p.words) if k not in p.ops] in (["cat"], ["cat", "-"])
        for chunk in seg.bodies:
            got = read(chunk)
            bodies = _either(bodies, got)
            if not (plain_cat and getattr(chunk, "scripted", False) and _exact_script("\n".join(chunk))):
                gbodies = _either(gbodies, got)
        subs = groups.get(id(seg.subs), _NOTHING) if seg.subs is not None else _NOTHING
        feed = seg.prev if seg.prev is not None else seg.inherit
        piped = source(feed, out) if feed is not None else _NOTHING
        gpiped = source(feed, gout) if feed is not None else _NOTHING
        if _exec_input(seg, p):
            targets = give(seg, _either(state, subs))
            if seg.group is not None and targets:
                pending.setdefault(id(seg.group), set()).update(targets)
        if subs[0] and _runs_writer(seg, p, subs, pid, sq):
            return True
        stdin = input_of(seg) if fds else _NOTHING
        fed = _either(_either(piped, bodies), stdin)
        if j in xargs and fed[0] and writer(j, fed):
            return True
        if j in xshells and fed[0] == 2 and fed[3]:
            return True
        if (j in shells and gpiped[0] == 2 and gpiped[3]) or (stdin[0] and shell_reads(j, stdin)):
            return True
        plain = (exact and _printer(p) and _exact_script(seg, p.words[p.lead] == "printf")
                 and _exact_script(_printed(p)))
        gen = _either(_either(_NOTHING if plain else state, gbodies), subs)
        state = _either(_either(state, bodies), subs)
        state = _either(state, stdin)  # what a stage reads from the shell's input is in what it sends on,
        gen = _either(gen, stdin)  # made at run time
        if seg.prev is not None:
            state = _either(state, piped)
            gen = _either(gen, piped)  # a command's output from what it reads is made at run time
        for group in seg.closed:
            keep(group, state, groups)
            state = _either(state, groups.get(id(group), _NOTHING))
            keep(group, gen, ggroups)
            gen = _either(gen, ggroups.get(id(group), _NOTHING))
            for fd in pending.pop(id(group), ()):  # `exec < <(a | b)`: the whole pipeline's output is the input
                fds[fd] = _either(fds[fd], state)
        out[id(seg)], gout[id(seg)] = state, gen
        keep(seg.group, state, groups)
        keep(seg.group, gen, ggroups)
    return False


def _xargs_code(words: list, k: int) -> bool:
    """Whether the `xargs` at `words[k]` runs a shell that may read what `xargs` reads as code: a shell
    (`_STDIN_SHELL`) after it that reads its script from its input or whose `-c` script word is missing
    (`_stdin_shell`: `xargs -0 sh -c`), one whose words hold the replace string of an `-I`, `-J`, `-i` or `--replace`
    flag of the `xargs` (`xargs -I{} sh -c {}`), or one whose `-c` script runs a positional parameter (`$0`, `$1`,
    ..., `$@`, `$*`) as a command of its own or holds one beside an `eval`, a shell, `source`, `.` or an interpreter
    word (`_POSITIONAL_RUN`, `_RUNS_CODE`: `xargs bash -c '$0'`, `xargs sh -c 'eval "$0"'`, `xargs sh -c '"$@"' _`;
    not `xargs -n1 bash -c 'echo "$0"'`, whose input is only printed). The flags with a value (`-n 1`, `-L`, `-P`,
    ...) are skipped; any other word before the shell is read as a flag cluster or passed over."""
    rest = words[k + 1:]
    s = next((s for s, w in enumerate(rest) if _STDIN_SHELL.search(_unbrace(w))), None)
    if s is None:
        return False
    if _stdin_shell(rest) is not None:
        return True
    replace, n = None, 0
    while n < s and replace is None:
        w = rest[n]
        n += 1
        if w in ("--replace", "-i") or w.startswith("--replace="):
            replace = w.partition("=")[2] or "{}"
        elif w[:1] == "-" and w[1:2] != "-":
            for c, letter in enumerate(w[1:], 2):
                if letter in "IJi":
                    replace = w[c:] or (rest[n] if letter != "i" and n < len(rest) else "{}")
                    break
                if letter in "ELnPRSsad":
                    n += not w[c:]  # this flag's value is the next word
                    break
    if replace is not None and (not replace or any(replace in w for w in rest[s:])):
        return True
    script = _script_word(rest, s)
    if script is None or not _POSITIONAL.search(rest[script]):
        return False
    text = rest[script]
    return bool(_POSITIONAL_RUN.search(text)) or any(
        _RUNS_CODE.fullmatch(t) or t in ("source", ".") for t in _CODE_TOKEN.findall(text))


# A positional parameter in a `-c` script (`_xargs_code`): `$0`, `${1}`, `$@`, `$*`; one at the start of a command
# (the script's start, after `;`, `&`, `|`, `(`, `{`, a newline or a keyword) runs it; so may one in a script that
# holds a shell, `eval`, `source`, `.` or an interpreter word (`eval "$0"`, `echo "$0" | sh`, `python3 -c "$0"`).
_POSITIONAL = re.compile(r"\$\{?[0-9@*]")
_POSITIONAL_RUN = re.compile(r"(?:^|[;&|({\n]|\b(?:then|do|else|exec|command|builtin|env|time|nohup)\s)[ \t]*"
                             r"[\"']?\$\{?[0-9@*]")
_CODE_TOKEN = re.compile(r"[\w.+-]+")


def _exact_script(text: str, fmt: bool = False) -> bool:
    """Whether the text `text` (a printer's segment and the text it prints, `_printed`, or the lines of a heredoc
    body `cat` passes on) is a shell's script as the guard's own reading of it reads it (`_fed_writes`): once its
    `#` comments are dropped (`_uncommented`), no `$` but the guard's own names (`_GUARD_NAMES`), no backtick, no
    `%` when the printer is `printf` (`fmt`: a format; `date +%F` is none), no word that binds a name (`f=<data>/x`,
    `a[i]=x`; a subscript past 256 characters counts as one, `_BINDS`), and no `cd`, `chdir` or `pushd` (a relative
    path then lands elsewhere) and no `for`, `while`, `until`, `read`, `select` or `set` where a command starts
    (`_KEYWORD_AT`; quoted or escaped too, `c''d`; not in quoted prose), `set` with only option flags aside (`set
    -Eeuo pipefail`, `_SET_FLAGS`): each may make the script's words other text at run time (`f=<data>/x; rm $f`),
    which that reading does not follow."""
    if "#" in text:
        text = _uncommented(text)
    if fmt and "%" in text:
        return False
    if "set" in text:
        text = _SET_FLAGS.sub(" ", text)
    if "$" in text:
        text = _GUARD_NAMES.sub("", text)
    return not ("$" in text or "`" in text or _BINDS.search(text) or _KEYWORD_AT.search(text))


def _printed(p) -> str:
    """The text the plain printer `p` (a `_Parsed`) prints, as the shell that reads it gets it: its words after the
    printer, quotes removed, its redirects and leading `-` words (`echo -n`) aside, joined by blanks."""
    words = [w for k, w in enumerate(p.words) if k > p.lead and k not in p.ops]
    start = next((k for k, w in enumerate(words) if w[:1] != "-"), len(words))
    return " ".join(words[start:])


def _uncommented(text: str) -> str:
    """`text` with each `#` comment dropped, to the end of its line: a `#` outside quotes, at the start of the text
    or a line or after a blank that no backslash escapes (`_COMMENT`). Fail-closed, a text with a `<<`, `((` or `[[`
    keeps its comments (a heredoc body line or an arithmetic or test word may hold a `#` that starts none)."""
    if "<<" in text or "((" in text or "[[" in text:
        return text
    return _COMMENT.sub(lambda m: "" if m.group(0)[0] == "#" else m.group(0), text)


# The names `_exact_script` lets a script expand (the guard reads them itself), the words it does not let one hold,
# and a `set` that only turns options on or off (`set -e`, `set -o pipefail`; no `--` and no positional words).
_GUARD_NAMES = re.compile(r"\$(?:\{(?:AP2_RUN_DIR|AP2_DATA|HOME)\}|(?:AP2_RUN_DIR|AP2_DATA|HOME)(?![A-Za-z0-9_]))")
# A word that binds a name (`f=x`, `f+=x`, `a[i]=x`); a subscript longer than 256 characters counts as one too, so the
# scan stays linear (fail-closed).
_BINDS = re.compile(r"(?<![\w.\-/=])[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]{0,256}\]\+?=|\[[^\]]{256}|\+?=)")
# A word that moves the shell or makes other text at run time, where a command starts: at the start, after a newline,
# `;`, `&`, `|`, `(`, `)`, `{`, `}`, `]` or `!`, there or after a keyword or a runner there that runs the next word
# in this shell and its flags (`then`, `do`, `if`, `eval`, `command -p`, `builtin`, `time`, zsh's `noglob`, `-` and
# `repeat N`, ...; `if x; then cd y`); blanks, quotes and backslashes may come before each (`'cd'`, `\cd`) and
# between its letters (`c''d`, `c\d`). Quoted prose (`echo 'select the next card'`, `echo 'then read it'`) is no
# command start; a printer's own script is read from its printed words (`_printed`), where it starts.
_KEYWORD_AT = re.compile(
    r"(?:^|[\n;&|(){}\]!])[ \t'\"\\]*(?:(?:"
    + "|".join("[\\\\'\"]*".join(w) for w in ("then", "do", "else", "elif", "if", "while", "until", "time", "command",
                                              "builtin", "eval", "exec", "noglob", "nocorrect", "nohup", "env",
                                              "coproc", "-"))
    + r")[\\'\"]*(?:[ \t]+[-+][\w-]*)*[ \t][ \t'\"\\]*|repeat[ \t]+\w+[ \t][ \t'\"\\]*)*(?:"
    + "|".join("[\\\\'\"]*".join(w) for w in ("cd", "chdir", "pushd", "for", "while", "until", "read", "select", "set"))
    + r")(?![\w.\-/])")
_SET_FLAGS = re.compile(r"(?<![\w.\-/])set(?:[ \t]+[-+][eEuxv]*(?:o[ \t]+(?:pipefail|errexit|nounset|xtrace|verbose"
                        r"|errtrace))?(?![\w.\-/]))+(?=[ \t]*(?:$|[\n;&|'\"]))", re.M)
# What `_uncommented` keeps whole (a quoted text, an escaped character) and the comments it drops.
_COMMENT = re.compile(r"'[^']*'?|\"(?:[^\"\\]|\\.)*\"?|\\.|(?<![^ \t\n])(?<!\\[ \t\n])#[^\n]*", re.S)


def _exec_input(seg: str, p) -> bool:
    """Whether the segment `seg` (`p` its `_Parsed`) runs `exec` with a `<` in its text: an input redirect (or a
    process substitution) that later commands of the shell read (`exec < <(ls <data>); xargs rm`)."""
    return "<" in seg and p.lead is not None and p.words[p.lead] == "exec"


def _input_fds(seg: str) -> list:
    """The input redirects in the text of the segment `seg`, as (fd, dup) pairs: each `<` (`<<`, `<<<` and `<>`
    too) that opens no process substitution, with the fd right before it (`3<`; none, or a `{name}` one, is 0,
    fail-closed: `exec {fd}< <data>/x` feeds every later input) and the fd a `<&N` moves onto it (`<&3`, `0<&3`),
    or None. A `<` inside quotes counts as well (fail-closed)."""
    out, i = [], seg.find("<")
    while i >= 0:
        if seg[i + 1:i + 2] != "(" and seg[i - 1:i] not in ("<", ">"):
            k = i
            while k and seg[k - 1].isdigit():
                k -= 1  # back over the fd's digits (a run up to the `<` before, so the scans stay linear)
            fd = int(seg[k:i]) if k < i and not (k and (seg[k - 1].isalnum() or seg[k - 1] in "_$}")) else 0
            dup = _FD_AFTER.match(seg, i + 2) if seg[i + 1:i + 2] == "&" else None
            out.append((fd, int(dup.group(1)) if dup else None))
        i = seg.find("<", i + 1)
    return out


# The fd a `<&` moves onto the input it redirects (`_input_fds`).
_FD_AFTER = re.compile(r"[ \t]*(\d+)")


def _depths(p) -> list:
    """For each word of the `_Parsed` segment `p`, how many substitutions it sits inside (its `events`)."""
    out, depth, e, events = [], 0, 0, p.events
    for k in range(len(p.words)):
        while e < len(events) and events[e][0] <= k:
            depth = depth + 1 if events[e][1] else max(depth - 1, 0)
            e += 1
        out.append(depth)
    return out


def _runs_writer(seg: str, p, state, pid: bool, sq: bool) -> bool:
    """Whether the segment `seg` (`p` its `_Parsed`), given its command substitutions' output (`state`, as
    `_fed_writes` reads it), runs a writer of what they print: a write verb among the words it runs outside them
    (`_run_text`), also in those words once each `$NAME` of the segment is taken out, as `_verb` does (`r$x'm'
    $(...)` runs `rm`), or a `dd` or `sed` there that may take an `of=` or an in-place flag from the output. A
    plain printer outside them (`echo`, `printf`) runs none."""
    text = _run_text(p)
    if text is None:
        return False
    if _writes(_harmless(text)) or (state[1] and bool(_DD.search(text))) or (state[2] and bool(_SED.search(text))):
        return True
    bare = _VAR_REF.sub("", seg) if "$" in seg else seg
    if bare == seg:
        return False
    text = _run_text(_Parsed(bare, pid, sq))
    return text is not None and _writes(_harmless(text))


def _run_text(p):
    """The words the `_Parsed` segment `p` runs outside its command substitutions, joined by blanks: its command
    words there (`_commands`) and each other word there with no blank in it (so a runner `_commands` does not know
    still counts: `sudo rm $(...)`; quoted prose such as `"next: rm $(...)"` does not), redirects aside; every word
    there when one of them is a shell, `eval` or an interpreter that runs a code word (`_RUNS_CODE`: `sh -c "rm
    $(...)"`, `python3 -c "open('$(...)', 'w')"`, `awk '... > ARGV[1]' $(...)`: that word is code, not prose);
    None for a plain printer there (`_printer` once the substitutions' own words are aside: `echo "rm: $(...)"`)."""
    depths = _depths(p)
    outer = [k for k in p.commands if not depths[k]]
    if (len(outer) == 1 and outer[0] == p.lead and p.words[p.lead] in _PRINTERS and not p.flags & _LOOSE
            and "always" not in p.words):
        return None
    words = [k for k in range(len(p.words)) if k not in p.ops and not depths[k]]
    code = any(_RUNS_CODE.fullmatch(_unbrace(p.words[k]).rsplit("/", 1)[-1]) for k in words)
    return " ".join(p.words[k] for k in words
                    if code or k in p.commands_set or not any(c.isspace() for c in p.words[k])) + " "


# The commands whose word may be code they run (`_run_text`), by basename: the shells and `eval`, and the
# interpreters that take a code word (`python3 -c`, `perl -e`, `awk '...'`, `node -e`, `osascript -e`, ...).
_RUNS_CODE = re.compile(r"sh|bash|zsh|ksh|dash|csh|tcsh|eval|python[0-9.]*|perl[0-9.]*|ruby|node|awk|gawk|nawk"
                        r"|php|lua|osascript|Rscript|deno|bun")


def _alike(seg: str) -> bool:
    """Whether every reading of `_command_writes` reads the segment `seg` alike: it holds neither a `$$(` (the
    `pid` readings) nor a `'` (the `sq` ones)."""
    return "$$(" not in seg and "'" not in seg


def _parse(seg: str, pid: bool, sq: bool, budget: _Budget):
    """`_Parsed(seg, pid, sq)`, taken from `budget.parsed` when an earlier reading parsed the same segment and every
    reading reads it alike (`_alike`)."""
    if not _alike(seg):
        return _Parsed(seg, pid, sq)
    key = (seg, isinstance(seg, _Body), getattr(seg, "quoted", False))
    p = budget.parsed.get(key)
    if p is None:
        p = budget.parsed[key] = _Parsed(seg, pid, sq)
    return p


def _flag_group(p) -> bool:
    """Whether the `_Parsed` segment `p` holds zsh's `${(` (a flag group) outside single quotes and backslashes: in a
    word whose `(` there is no quoted text (`_Word.mask`; inside `"${...}"` nothing is), and not in a body line of a
    heredoc with a quoted delimiter, where zsh expands nothing (JavaScript's `` `${(ms/1000).toFixed(2)}` `` in
    `node -e '...'` or in a `<<'EOF'` body is text). A shell's or `eval`'s script is read again as a command of
    its own, where its `${(` is unquoted."""
    if p.body and p.quoted:
        return False
    for w in p.words:
        i = w.find("${(")
        while i >= 0:
            if w.mask is None or w.mask[i + 2] != _HIDE:
                return True
            i = w.find("${(", i + 1)
    return False


class _Parsed:
    """One segment read as words (`_words`, with `pid` and `sq`): its `words`, redirect `targets`, substitution
    `events`, `flags`, the index of the first word after an unquoted closer (`loose`, len(words) for none), the
    indexes of its command words, its own command and the words that may run after all (`commands`, `lead`,
    `maybe`, from `_commands`), whether it is a heredoc body line (`body`), one of a heredoc with a quoted
    delimiter (`quoted`), the indexes of its redirect words (`ops`), whether a word is one of `_PATH_SETTERS` where
    it runs (`setter`), and whether such a setter takes one of zsh's name tables as a name it binds (`table`,
    `_names_table`)."""

    def __init__(self, seg: str, pid: bool, sq: bool):
        self.targets, self.events, self.flags, self.body = [], [], set(), isinstance(seg, _Body)
        self.quoted = self.body and seg.quoted
        extra = {"ops": [], "glued": [], "loose": []}
        self.words = _words(seg, self.targets, self.events, pid, self.flags, sq, extra)
        self.loose = min(extra["loose"], default=len(self.words))
        self.ops = set(extra["ops"])  # the indexes of the words of its redirects
        self.commands, self.lead, self.maybe = _commands(self.words, self.events, self.ops, set(extra["glued"]))
        self.commands_set = set(self.commands)
        # A setter counts where it runs (`_command_like`, or after an unquoted closer: `case x in x) read PATH`),
        # not as an argument (`git grep -n set -- path`, `rg -e for -e path`).
        setters = [k for k, w in enumerate(self.words)
                   if w in _PATH_SETTERS and (k >= self.loose or _command_like(self, k))]
        self.setter = bool(setters)
        self.table = bool(setters) and _names_table(self, setters)


def _stdin_shell(words: list):
    """The shell word (`_unbrace`d), when a segment of the shell words `words` runs a shell (`_STDIN_SHELL`: `_SHELL`'s
    shells, not `eval`, and `csh` or `tcsh`, also before a word-final `}`) that may read its script from its input:
    with no `-c` among its options, or with a `-c` whose script word is missing or comes from a substitution (it
    holds a backtick or a `$(`, which `_words` leaves as a word ending in `$`: `sh -c "$(cat)"`; fail-closed, a word
    ending in a plain `$` too); else None. Only the option words right after the shell count (as in `_script_word`:
    up to the first `--`, `-` or other word, an `-o` or `-O` value skipped): a `-c` after them is a positional
    argument (`bash -s -- -c`), and a cluster with an `s` reads the input whatever else it holds (fail-closed)."""
    k = next((k for k, w in enumerate(words) if _STDIN_SHELL.search(_unbrace(w))), None)
    if k is None:
        return None
    values, c = 0, False
    for w in words[k + 1:]:
        if values:
            values -= 1
        elif w[:1] in "-+" and w not in ("-", "--"):
            if w[:1] == "-" and w[1:2] != "-":
                if "s" in w:
                    return _unbrace(words[k])
                c = c or "c" in w
            values = 0 if w[1:2] == "-" else w.count("o") + w.count("O")
        else:
            break
    if c:
        script = _script_word(words, k)
        if script is not None and not (words[script].endswith("$") or "`" in words[script] or "$(" in words[script]):
            return None
    return _unbrace(words[k])


# The shells `_stdin_shell` finds: `_SHELL`'s, `eval` aside, and `csh` and `tcsh`, which read a script from their
# input too (a `-c` script is still read as POSIX shell code only for `_SHELL`'s shells).
_STDIN_SHELL = re.compile(r"(?<![A-Za-z0-9_.])(?:sh|bash|zsh|ksh|dash|csh|tcsh)\Z")


def _script_word(words: list, shell: int):
    """The index of the script a `-c` shell runs (`bash -e -o pipefail -c '<script>' arg0 arg1`): the first word
    after a `-c` flag (a `-` cluster with a `c`) that is no flag (`-x`, `+O`, `--`, `-`) and no option's value (one
    word for each `o` or `O` in a cluster: `-o pipefail`; bash and zsh exit on an option name that is none); None
    when a word that is neither comes first (`bash run.sh`: a script file, its arguments after it) or there is no
    `-c`. The words after the script are its `$0`, `$1`, ..."""
    seen, values, end = False, 0, False
    for k in range(shell + 1, len(words)):
        w = words[k]
        if values:
            values -= 1
        elif not end and w[:1] in "-+" and w not in ("-", "--"):
            seen = seen or (w[:1] == "-" and w[1:2] != "-" and "c" in w)
            values = 0 if w[1:2] == "-" else w.count("o") + w.count("O")
        elif not end and w in ("-", "--"):
            end = True  # the end of the options: the next word is the script
        else:
            return k if seen else None
    return None


def _scripts_write(words: list, marks: _Marks, depth: int, budget: _Budget, zsh: bool = False) -> bool:
    """Whether the shell words `words` (a shell's or `eval`'s, a segment piped into a shell, or a heredoc body a
    shell reads, `_Script`, as one word) write into a guarded
    path when read as a script: each one that names a guarded path, and all of them joined by blanks (as `eval`
    joins them), read as a command of its own, one level deeper. Below the top level only the joined text is read
    (it holds each word). At `MAX_DEPTH` a script that names a guarded path is a write. Each text read spends its
    length from `budget`, once (a text already judged at that depth, or found to name nothing, is not read again).
    A last word that ends in an unquoted `}` is read with that `}` dropped too (zsh closes a brace list there:
    `{ sh -c '...'}`). For `zsh` (the shell is zsh) a script with a `(` anywhere that names a guarded path is a write:
    a fresh zsh has bare glob qualifiers on (`zsh -c "ap2 card list /(e:'cmd':)"` runs `cmd`), which this reading
    does not follow."""
    joined = " ".join(words)
    if zsh and "(" in joined and marks.names(joined):
        return True
    texts = [joined] if depth or len(words) < 2 else list(words) + [joined]
    texts += [t for t in map(_unbrace, words[-1:]) if t != words[-1]]  # `{ sh -c '...'}`: zsh drops that `}`
    for text in texts:
        key = (text, depth + 1)
        if key in budget.seen:
            continue
        if not marks.names(text):
            budget.seen.add(key)  # names nothing: no other reading needs to look again
            continue
        if depth >= MAX_DEPTH:
            return True
        budget.spend(len(text))
        if _command_writes(text, marks, depth + 1, budget):
            return True
        budget.seen.add(key)
    return False


def _rebinds(parsed: list) -> bool:
    """Whether a segment of the command (not a heredoc body line: a body a shell reads is that shell's script, whose
    rebinding the `_Script` reading judges one level deeper, and a child shell rebinds nothing here) may make a name
    run something else: it defines a function (`name()`, `name ()`, `function name`; bash's
    `command_not_found_handle` and zsh's `command_not_found_handler`, which run for a name that is not found, are
    functions too), holds a `$'...'` this reading cannot decode (`_words`: it may spell any name), holds one of
    `_REBIND` or `.` where it may run as a command (`_command_like`: a command word to `_commands`, so also after
    redirects, assignments, a runner, a zsh precommand modifier or a backslash, `command --`, inside `<(...)`,
    `>(...)` or `=(...)`; or a word `_commands` finds may run after all, after a command word with a `$` or backtick
    or after `repeat N`), or as any word after an unquoted `)`, `]]` or `}` of its segment (`_Parsed.loose`: `case
    x in x) hash ...`, `for x (1) hash ...`, `if [[ -n x ]] hash ...`; fail-closed there, `grep -c 'x' f) hash`
    counts too, while the `}` of an empty `{}`, as in `-exec grep -l hash {} +`, is none), or holds a word that
    sets a zsh name table (`_TABLE_ANY`: `functions[ap2]=...`, `: ${commands[ap2]::=/bin/cp}`, `${PATH::=...}`; or
    the name a setter binds, `_Parsed.table`: `read -A commands`) or the search path or zsh's function path
    (`_sets_path`: `PATH=/tmp:$PATH`, `fpath=(/tmp/fn)`). A rebinding name
    as an argument (`<pkg>/bin/ap2 source list`, `grep -i hash <data>/items/C1.yaml`, `find . -name x`, `rg
    "path=" lib`) rebinds nothing."""
    for p in parsed:  # `_Parsed` segments
        if p.body:
            continue
        if "()" in p.flags or "$'" in p.flags or p.table:
            return True
        if any(_TABLE_ANY.search(w) or _sets_path(p, w) for w in p.words):
            return True
        if any((w in _REBIND or w == ".") and (k >= p.loose or _command_like(p, k)) for k, w in enumerate(p.words)):
            return True
    return False


def _command_like(p, k: int) -> bool:
    """Whether the word at `k` of the `_Parsed` segment `p` may run as a command: a command word (`commands`), or a
    word that may run after all (`maybe`)."""
    return k in p.commands_set or k in p.maybe


# The `_words` flags of an unquoted `)` (not one that closes a substitution), `]]` and `}` (not that of a `${`):
# after one of them (a case pattern, `x) cmd`, a zsh short loop, `for x (1) cmd` and `while { a } { cmd }`, or a zsh
# condition, `if [[ -n x ]] cmd`) a word that is no command word to `_commands` can run (`_printer`; `_rebinds`
# reads where the first one is, `_Parsed.loose`).
_LOOSE = frozenset((")", "]]", "}"))
# zsh's name tables, set by a subscript or an assignment anywhere in a word (not after a name character, `-` or `.`:
# `my_commands=x` and `--aliases=x` are none: `functions[ap2]=...`, `commands+=(ap2 /bin/cp)`), and zsh's
# `${PATH::=...}`, which sets the search path in an expansion (also inside `"..."`, where it still expands).
_TABLE_NAMES = r"(?:functions|aliases|galiases|saliases|commands)"
_TABLE_ANY = re.compile(r"(?<![A-Za-z0-9_.-])" + _TABLE_NAMES + r"(?:\[|\+?=)|\$\{(?:PATH|path|FPATH|fpath)::?=")
# The search path's or zsh's function path's name, assigned (`PATH=...`, `path+=(...)`, `path[1]=...`; not after a
# name character, `-` or `.`: `PYTHONPATH=src`, `--path=x` and `sys.path=` are none), and as a plain word.
_PATH_SET = re.compile(r"(?<![A-Za-z0-9_.-])(?:PATH|path|FPATH|fpath)(?:\[|\+?=)")
_PATH_WORDS = frozenset(("PATH", "path", "FPATH", "fpath"))
# zsh's name tables as the name a setter binds (`_names_table`): `read -A commands <<< 'ap2 /bin/cp'` fills the
# command hash table (`commands[ap2]` is then `/bin/cp`), and so may `typeset -A functions ...` or `for aliases in
# ...`; as any other word (`pytest -k commands`, `for d in commands skills`, `set -- commands`) it binds nothing.
_TABLE_WORDS = frozenset(("functions", "aliases", "galiases", "saliases", "commands"))
# The setters whose every plain word (no flag, no redirect word) may be a name they bind (`read -rA NAME`,
# `typeset -A NAME`, `getopts OPTS NAME`); `for` and `select` bind the words before their `in`, and `printf` and
# `print` the word after a `-v`, `set` (and `print`) the word after a `-A` (`set -A NAME ...`). `env`, `let`,
# `trap`, `set --` and a plain `printf` or `print` bind none.
_NAME_SETTERS = frozenset(("read", "mapfile", "readarray", "declare", "typeset", "local", "export", "readonly",
                           "integer", "float", "private", "vared", "getopts"))
_NAME_FLAGS = {"printf": "v", "print": "vA", "set": "A"}
# The commands that set a variable named by a word (`for PATH in ./b`, `read PATH`, `printf -v PATH ...`, `export
# "PATH=..."`, `env "PATH=..." cmd`, zsh's `set -A path ...`), or run a quoted text that can (`trap '...' DEBUG`).
_PATH_SETTERS = frozenset(("for", "select", "read", "declare", "typeset", "export", "local", "readonly", "integer",
                           "float", "private", "printf", "print", "getopts", "vared", "mapfile", "readarray", "let",
                           "env", "set", "trap"))


def _sets_path(p, w) -> bool:
    """Whether the word `w` of the `_Parsed` segment `p` may set the search path or zsh's function path: an
    assignment to one whose `=` or `[` no quote made text (`_Word.mask`; a quoted `"path="` is a plain argument, as
    in `rg "path=" lib`), or, in a segment where one of `_PATH_SETTERS` runs (`_Parsed.setter`), such a name as a
    plain word, or an assignment quoted or not (`git log -- path` names a file, and so does `git grep set --
    path`). A name table a setter binds is `_Parsed.table`."""
    for m in _PATH_SET.finditer(w):
        if w.mask is None or w.mask[m.end() - 1] != _HIDE:
            return True
    return p.setter and (w in _PATH_WORDS or bool(_PATH_SET.search(w)))


def _names_table(p, setters: list) -> bool:
    """Whether a setter of the `_Parsed` segment `p` (at the indexes `setters`, each one where it runs) takes one of
    zsh's name tables (`_TABLE_WORDS`) as the name it binds: a word before the `in` (or `do`) of a `for` or
    `select`; a plain word (no `-` or `+` flag, no redirect word) of one of `_NAME_SETTERS` (`read -A commands`,
    `echo x | read -rA commands`, `vared commands`); the word after a flag with a `v` of `printf` or `print`, or with
    an `A` of `set` or `print`, before any `--`. Each setter reads up to the next one, so the scan is linear. A name
    table as any other word (`pytest -k commands`, `for d in commands`, `set -- commands`, `printf '%s' commands`,
    `read -r x <<< commands`, `env`, `let`, `trap`) binds nothing."""
    words = p.words
    for n, k in enumerate(setters):
        end = setters[n + 1] if n + 1 < len(setters) else len(words)
        setter = words[k]
        if setter in ("for", "select"):
            for w in words[k + 1:end]:
                if w in ("in", "do"):
                    break
                if w in _TABLE_WORDS:
                    return True
        elif setter in _NAME_SETTERS:
            if any(words[i] in _TABLE_WORDS for i in range(k + 1, end) if i not in p.ops):
                return True
        elif setter in _NAME_FLAGS:
            letters = _NAME_FLAGS[setter]
            for i in range(k + 1, end - 1):
                w = words[i]
                if w == "--" or w[:1] not in "-+":
                    break
                if any(c in letters for c in w[1:]) and words[i + 1] in _TABLE_WORDS:
                    return True
    return False


# `ap2` as a name being bound, in a word with its quotes removed: the word `ap2` (`hash -p /bin/cp ap2`, `autoload
# -U ap2`, `typeset -fu ap2`), a path to a file named `ap2` (`/tmp/b/ap2`), or `ap2=`, `ap2+=`, `ap2(`, `ap2 ()`
# (blanks before a `()`, blanks inside it too: `ap2 () { ... }` in a quoted action) and `[ap2]` inside a word
# (`alias ap2=cp`, `hash ap2=/bin/cp`, `commands[ap2]`); not `ap2` inside prose (`'fix: ap2 guard'`, `'teach ap2
# (the CLI) a flag'`, `"ap2 (v2)"`), `AP2_DATA`, `test_ap2_x` or `ap2x`.
_AP2_BOUND = re.compile(r"(?<![A-Za-z0-9_])ap2(?:\+?=|\(|[ \t]+\([ \t]*\))|\[ap2\]")


def _ap2_bound(w: str) -> bool:
    return w == "ap2" or w.endswith("/ap2") or bool(_AP2_BOUND.search(w))


def _ap2_trusted(readings) -> bool:
    """Whether `ap2` in the command is the package's `ap2` for sure, so an `ap2` segment keeps its exemption:
    nothing in any reading (`readings`: (pid, sq, segments, `_Parsed` segments) of every reading of the command;
    each `$(...)`, backtick and process substitution's text is in there, and a shell's script is judged as a command
    of its own) can make the name run something else, whatever its position (fail-closed: as an argument too). It
    is not when any word is one of `_REBIND`, a `.` that may run (`_dot_runs`), `ap2` as a name being bound
    (`_ap2_bound`: `hash ap2=...`, `commands[ap2]`, `autoload -U ap2`; not the command word of an `ap2` segment,
    nor its arguments after it when it runs nothing else: `ap2 card note C1 --text "ap2"`; not `ap2` inside a longer
    word, as in a commit message), a name table (`_TABLE_ANY`, or as the name a setter binds: `_Parsed.table`) or a
    search-path setting (`_sets_path`), or when a
    segment defines a function (a `()`) or holds a `$'...'` this reading cannot decode. A heredoc body line is
    data: only a name table there counts (an unquoted body expands `${commands[ap2]::=...}`); a body a shell reads is
    that shell's script, judged on its own one level deeper (a child shell rebinds nothing in this one)."""
    for pid, sq, segs, parsed in readings:
        for seg, p in zip(segs, parsed):
            if p.body:
                if not p.quoted and any(_TABLE_ANY.search(w) for w in p.words):
                    return False
                continue
            if "()" in p.flags or "$'" in p.flags or p.table:
                return False
            lead = p.lead if _runs_ap2(p.words, p.lead) else None  # its arguments too, when it runs nothing else
            args = lead is not None and not _runs_more(seg, pid, sq)
            for k, w in enumerate(p.words):
                if w in _REBIND or _TABLE_ANY.search(w) or _sets_path(p, w) or (w == "." and _dot_runs(p, k)):
                    return False
                if k != lead and not (args and k > lead) and _ap2_bound(w):
                    return False
    return True


def _dot_runs(p, k: int) -> bool:
    """Whether the `.` at `k` of the `_Parsed` segment `p` may run a file: it is the segment's first word, may run
    as a command (`_command_like`), comes after an unquoted closer (`loose`) or after a keyword."""
    return k == 0 or _command_like(p, k) or k >= p.loose or p.words[k - 1] in _KEYWORDS


def _extglob(cmd: str) -> bool:
    """Whether `cmd` may turn extglob on (`shopt -s extglob`, `bash -O extglob`): it holds `extglob` once line
    continuations, backslashes, quotes and `$` are taken out (`ext\\glob`, `ext'gl'ob`, `ext$'g'lob`). Extglob is
    off in a non-interactive bash; with it on, `@(...)` and the like are words `_segments` does not read as bash
    does (no comment, split or word break inside, `cp @(a|b) <data>/c` is one command)."""
    return "extglob" in _bare(cmd)


def _bare(cmd: str) -> str:
    """`cmd` with line continuations, backslashes, quotes and `$` taken out: a name spelled in pieces reads whole."""
    return re.sub(r"\\\n|[\\'\"$]", "", cmd)


# zsh's words that set options (`setopt bareglobqual` makes `/(e:'cmd':)` run `cmd`; `emulate ksh` turns on ksh
# globs) or its `options` table (`options[extendedglob]=on`).
_OPTION_WORDS = re.compile(r"(?<![\w.-])(?:setopt|unsetopt|emulate)(?![\w.-])|(?<![\w.-])options(?:\[|\+?=)")
_SET = re.compile(r"(?<![\w.-])set(?![\w.-])")
_SET_TOKEN = re.compile(r"[^\s;&|()]+")
# What `set` may turn on with no effect on how words read: `set -Eeuo pipefail`, `set -x`, `set +e`, `set -C`.
_SET_LETTERS = frozenset("euxvoEaCT")
_SET_NAMES = frozenset(("pipefail", "errexit", "nounset", "xtrace", "verbose", "errtrace", "noclobber", "allexport",
                        "functrace"))


def _options(cmd: str) -> bool:
    """Whether `cmd` may change an option that changes how words read (as `_extglob`, once continuations, quotes
    and `$` are taken out): `setopt`, `unsetopt`, `emulate` or zsh's `options` table, or a `set` with a flag other
    than `-e`, `-u`, `-x`, `-v`, `-E`, `-a`, `-C`, `-T` and `-o`/`+o` with an option other than `pipefail`,
    `errexit`, `nounset`, `xtrace`, `verbose`, `errtrace`, `noclobber`, `allexport` or `functrace` (`set -o
    extendedglob`; `set -Eeuo pipefail` and `set -o noclobber` change no word's reading in bash or zsh). zsh's
    `${(` flag group is `_flag_group`, read per segment."""
    text = _bare(cmd)
    if _OPTION_WORDS.search(text):
        return True
    for m in _SET.finditer(text):
        name = False  # whether the next word is the option name of a `-o`
        for tok in _SET_TOKEN.finditer(text, m.end()):
            word = tok.group(0)
            if name:
                if word.lower() not in _SET_NAMES:
                    return True
                name = False
            elif word[:1] in "-+" and word not in ("-", "--"):
                if not set(word[1:]) <= _SET_LETTERS:
                    return True
                name = "o" in word
            else:
                break
    return False


def _writes_guarded(seg: str, marks: _Marks, pid: bool, rebound: bool = False, depth: int = 0, budget=None,
                    sq: bool = True, parsed=None, fed: bool = False, trusted: bool = True) -> bool:
    """Whether the segment `seg` (read with `pid` and `sq`, as in `_segments`; `parsed` its `_Parsed`, when the caller
    has it) writes into a guarded path. With
    `rebound` (`_rebinds`: a name may run anything) no segment keeps the `ap2` exemption, and one that names the data
    folder or the package (`ap2` and `<pkg>/bin/ap2` too) outside the own run directory at all is a write; one whose
    only such mentions are run-directory paths with a `$` or backtick after them (`_level` 1) is judged by its verb,
    as any other. A segment with a shell or `eval` in it (`_SHELL`) also writes when the words after it write as a
    script (`_scripts_write`: `bash -c 'tee $AP2_RUN_DIR/settings-implement.json < f'`, whose quotes hide the
    script's words, braces and line continuations from this segment's reading). When that shell is one of the
    segment's command words, runs a `-c` script (`_script_word`) and the scripts write nothing, only the script word
    is left out of this segment's own reading (it was judged as a script; the words after it are the script's
    `$0`, `$1`, ..., judged here with the segment's verb and the script's own, quotes removed: `sh -c 'c""p "$1"
    "$2"; echo ok' _ a <data>/x` writes), and so are all the words of a segment piped into a shell (`fed`, its words
    already read as that shell's script) whose own command is `echo` or `printf` (`_PRINTERS`: any other command
    runs before the shell reads, `touch a '#' <data>/x | sh`); only at `depth` 0, where each word was also read on its
    own (or, for `fed`, a single word); its redirect targets still count. With `trusted` False (`_ap2_trusted`), an
    `ap2` segment that names a guarded path is a write. The verb is looked for in the text as written and in its
    words with quotes and backslashes removed (`c\\p` and `c''p` are `cp`)."""
    if not seg.strip():
        return False
    budget = _Budget(BUDGET * len(seg) + BUDGET_EXTRA) if budget is None else budget
    p = _Parsed(seg, pid, sq) if parsed is None else parsed
    words, targets, flags = p.words, p.targets, p.flags
    braces, eglob = "{" in flags, "(" in flags  # an unquoted `{` or `(` in a word in the segment (`_level`)
    shell = next((k for k, w in enumerate(words) if _SHELL.search(_unbrace(w))), None)
    out = set()  # the indexes of the words judged as a script, left out of this reading (redirect targets aside)
    script = None  # a `-c` shell's script word, judged as a script that writes nothing
    if shell is not None:
        if _scripts_write(words[shell + 1:], marks, depth, budget, bool(_ZSH.search(_unbrace(words[shell])))):
            return True
        script = _script_word(words, shell) if depth == 0 and shell in p.commands else None
        if script is not None:
            out.add(script)
    if fed and (depth == 0 or len(words) < 2) and _printer(p):
        out.update(range(len(words)))
    commands, lead = p.commands, p.lead
    ap2 = not p.body and _runs_ap2(words, lead)
    if rebound:
        level = marks.level(words, braces, eglob)
        return level == 2 or (level == 1 and (ap2 or _verb(seg, words)))
    # `<pkg>/bin/ap2` where bash runs it as a command is no write into the package (unless a redirect names it).
    written = set(targets)
    run = {k for k in commands if words[k] not in written and marks.is_ap2(words[k])}
    kept = {id(t) for t in targets}  # a redirect's target stays in, script word or not
    if not marks.unsafe((w for k, w in enumerate(words) if k not in run and (k not in out or id(w) in kept)),
                        braces, eglob):
        return False
    if ap2 and not trusted:
        return True  # `ap2` may run anything here (`_ap2_trusted`): a guarded path it names is a write
    if ap2 and not _runs_more(seg, pid, sq):
        return marks.unsafe(targets, braces, eglob)  # a redirect's file is the shell writing, not ap2
    if script is not None:  # the script's own verb, with its quotes removed, acts on the arguments after it
        budget.spend(len(words[script]))
        if _verb(words[script], _words(words[script])):
            return True
    return _verb(seg, words)


# The commands that print their words and write nothing (their redirects aside): piped into a shell, their words are
# that shell's script, and nothing else (`_writes_guarded`).
_PRINTERS = frozenset(("echo", "printf"))


def _printer(p) -> bool:
    """Whether the `_Parsed` segment `p` is a plain printer command: its own command is one of `_PRINTERS`, it is its
    only command word (no substitution runs one), and it has no unquoted `)`, `]]` or `}` (`_LOOSE`) and no
    `always` word, after which zsh runs another command (`{ echo x } always { cmd }`)."""
    return (p.lead is not None and p.words[p.lead] in _PRINTERS and len(p.commands) == 1
            and not p.flags & _LOOSE and "always" not in p.words)


def _verb(seg: str, words: list) -> bool:
    """Whether the segment `seg` (its shell words `words`) holds a write verb (`_writes`, after `_harmless`): in its
    text as written, or in its words joined by blanks, with quotes and backslashes removed as the shell removes them
    before it runs a word (`c\\p`, `c''p`, `c\\<newline>p` and `sed '-i'` are `cp` and `sed -i`), and also in the
    words of the text with each `$NAME` (zsh's `$=NAME` too) taken out: it may expand to nothing, and in a word it
    hides the start of the next quoted text (`$x'c'p` and `$x$'\\x63p'` run `cp`; their words read `$xcp`)."""
    if _writes(_harmless(seg)) or _writes(_harmless(" ".join(words))):
        return True
    bare = _VAR_REF.sub("", seg) if "$" in seg else seg
    return bare != seg and _writes(_harmless(" ".join(_words(bare))))


# A `$NAME` reference (with zsh's `=`, `~` or `^` flags), or a special parameter: `_verb` takes it out.
_VAR_REF = re.compile(r"\$[=~^]*(?:[A-Za-z_][A-Za-z0-9_]*|[0-9#?@*!-])")


def _fallback(raw: bytes, env):
    """A payload too deep to parse, or one judged past the deadline: denied when it is a v2 worker's Bash call
    naming a guarded path. It runs with no timer, so it is linear and cheap: `_Marks.in_text`, no shell reading."""
    if not env.get("AP2_ROLE") or b'"Bash"' not in raw:
        return None
    # The text is JSON: undo its escapes, so a quote-split `"$AP2_"DATA` or a line continuation reads as the shell's.
    text = _JSON_ESC.sub(_unescape, raw.decode("utf-8", "replace"))
    return _deny() if _Marks(env).in_text(text) else None


# What `_Marks.in_text` takes out before a second search: a line continuation, a `$` before a quote (`$"..."`,
# `$'...'`), then any quote or backslash.
_STRIP = re.compile(r"\\\n|\$(?=[\"'])|[\"'\\]")
# An ANSI-C escape a shell may decode (`$'\x74'`, `$'\164'`, zsh's `$'\u0074'`), after any run of backslashes (a
# quoted script's own escaping).
_ESCAPE = re.compile(r"(?<!\\)\\++(?:x([0-9a-fA-F]{1,2})|([0-7]{1,3})|u([0-9a-fA-F]{1,4})|U([0-9a-fA-F]{1,8}))")


def _decoded(text: str) -> str:
    """`text` with each `_ESCAPE` that stands for a printable ASCII character replaced by it, wherever it is
    (fail-closed: also outside a `$'...'`)."""
    def one(m):
        code = int(m.group(2), 8) if m.group(2) else int(m.group(1) or m.group(3) or m.group(4), 16)
        return chr(code) if 0x20 <= code < 0x7F else m.group(0)
    return _ESCAPE.sub(one, text)


# A `$'...'` quote anywhere in a text (fail-closed: also one a quote around it made text), its body in group 1.
_ANSI_QUOTE = re.compile(r"\$'((?:[^'\\]|\\.)*)'", re.S)


def _shell_readings(text: str) -> list:
    """`text` twice, with each `$'...'` in it (`_ANSI_QUOTE`) replaced by its text as bash 3.2 decodes it and as zsh
    decodes it (`_bash_ansi`, `_zsh_ansi`); a character no name holds stands for each non-ASCII or control result."""
    return [_ANSI_QUOTE.sub(lambda m: decode(m.group(1)), text) for decode in (_bash_ansi, _zsh_ansi)]


def _ansi_digits(body: str, i: int, digits, most: int) -> int:
    """The index after the run of at most `most` characters of `digits` in `body` from `i`."""
    j = i
    while j < min(len(body), i + most) and body[j] in digits:
        j += 1
    return j


def _bash_ansi(body: str) -> str:
    """The text of a `$'...'` (`body`, between its quotes) as bash 3.2 decodes it: the `_ANSI_C` escapes, `\\NNN`,
    `\\xHH` (`\\x` with no digit stays), `\\cX` (X's low five bits), `\\u` and `\\U` decoded (as some locales do;
    as text they spell no name); any other escape keeps its backslash; a NUL ends the text."""
    out, i, n = [], 0, len(body)
    while i < n:
        if body[i] != "\\" or i + 1 >= n:
            out.append(body[i])
            i += 1
            continue
        e, i = body[i + 1], i + 2
        if e in _ANSI_C:
            out.append(_ANSI_C[e])
            continue
        if e in _OCT:
            j = _ansi_digits(body, i - 1, _OCT, 3)
            code, i = int(body[i - 1:j], 8) & 0xFF, j
        elif e in "xuU":
            j = _ansi_digits(body, i, _HEX, {"x": 2, "u": 4, "U": 8}[e])
            if j == i:
                out.append("\\" + e)
                continue
            code, i = int(body[i:j], 16), j
        elif e == "c" and i < n:
            code, i = ord(body[i]) & 0x1F, i + 1
        else:
            out.append("\\" + e)
            continue
        if not code:
            break
        out.append(chr(code) if code < 0x80 else "\x01")
    return "".join(out)


def _zsh_ansi(body: str) -> str:
    """The text of a `$'...'` (`body`) as zsh decodes it: the `_ANSI_C` escapes, `\\NNN`, `\\xHH`, `\\uHHHH` and
    `\\UHHHHHHHH` (no digit is a NUL), `\\C-X`, `\\CX` and `\\M-X` (one control or meta character for the
    character or escape after them, nothing at the text's end), and any other escape as the character after its
    backslash (`\\c` is `c`). A NUL stays: an external command's path ends there."""
    out, i, n = [], 0, len(body)
    while i < n:
        c, i = _zsh_unit(body, i)
        out.append(c)
    return "".join(out)


def _zsh_unit(body: str, i: int):
    """One character of `_zsh_ansi` from `i`, and the index after it."""
    n = len(body)
    if body[i] != "\\" or i + 1 >= n:
        return body[i], i + 1
    e, i = body[i + 1], i + 2
    if e in _ANSI_C:
        return _ANSI_C[e], i
    if e in _OCT:
        j = _ansi_digits(body, i - 1, _OCT, 3)
        code = int(body[i - 1:j], 8) & 0xFF
    elif e in "xuU":
        j = _ansi_digits(body, i, _HEX, {"x": 2, "u": 4, "U": 8}[e])
        code = int(body[i:j], 16) if j > i else 0
    elif e in "CM":
        # A control or meta character (no name holds one) for the next character or escape; a run of such prefixes
        # (`\\C-\\M-x`) is read in a loop, not by recursion.
        while True:
            i += body[i:i + 1] == "-"
            if i >= n:
                return "", i
            if body.startswith(("\\C", "\\M"), i):
                i += 2
                continue
            return "\x01", _zsh_unit(body, i)[1]
    else:
        return e, i
    return (chr(code) if code < 0x80 else "\x01"), j


_JSON_ESC = re.compile(r"\\(?:u([0-9a-fA-F]{4})|(.))", re.S)
_JSON_CHARS = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}


def _unescape(m) -> str:
    return chr(int(m.group(1), 16)) if m.group(1) else _JSON_CHARS.get(m.group(2), m.group(2))


class _Late(Exception):
    """The judgement ran past `DEADLINE`."""


def _late(signum, frame):
    raise _Late


def _judge(raw: bytes, env):
    """The verdict on the bytes `main` read (at most MAX_STDIN + 1). Past MAX_STDIN, a v2 worker's Bash call is
    denied outright: the rest was never read. A judgement that runs past `DEADLINE` (a timer signal, which also
    stops a slow regex match) falls back to `_fallback`. The timer is one-shot and turned off inside the block that
    catches it, so a signal that lands after the judgement returned and before the timer is off is caught there too
    (`_fallback` then gives the verdict); the old handler is put back last."""
    if len(raw) > MAX_STDIN:
        return _deny() if env.get("AP2_ROLE") and b'"Bash"' in raw else None
    armed, before = False, None
    if hasattr(signal, "setitimer"):
        try:
            before = signal.signal(signal.SIGALRM, _late)
            signal.setitimer(signal.ITIMER_REAL, DEADLINE)
            armed = True
        except ValueError:  # not the main thread: no timer
            pass
    try:
        try:
            try:
                payload = json.loads(raw.decode("utf-8"))
            except RecursionError:
                return _fallback(raw, env)
            return decide(payload, env) if isinstance(payload, dict) else None
        finally:
            if armed:
                signal.setitimer(signal.ITIMER_REAL, 0)
    except _Late:
        return _fallback(raw, env)
    finally:
        if armed:
            signal.signal(signal.SIGALRM, before if before is not None else signal.SIG_DFL)


def main(argv: list, stdin=None) -> int:
    """`ap2 hook-guard`: the payload on stdin, the deny JSON (or nothing) on stdout, exit 0 always. Any argument is
    no opinion. A `_Late` that gets out of `_judge` (the timer fired as it was set) is judged by `_fallback`."""
    try:
        if argv or not os.environ.get("AP2_ROLE") or stdin is None:
            return 0
        raw = getattr(stdin, "buffer", stdin).read(MAX_STDIN + 1)
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        try:
            out = _judge(raw, os.environ)
        except _Late:  # the timer fired before `_judge` could catch it: judged with no timer, never no opinion
            out = _fallback(raw, os.environ)
        if out:  # unbuffered: a closed stdout raises here, not again at interpreter exit
            os.write(1, (json.dumps(out) + "\n").encode())
    except Exception:  # noqa: BLE001 - a hook must never break the session
        pass
    return 0
