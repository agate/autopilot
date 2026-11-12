"""`ap2 hook-guard`: the PreToolUse hook on Bash that keeps a v2 worker's shell out of the data folder and the
package (design 8.1, section 12 workers).

`workers.floor()` wires it into every worker's settings after the park hook. For a Bash call from a v2 worker
(`AP2_ROLE` set) it splits the command into segments (newlines, `;`, `&&`, `||`, `|`, `|&`, and a background `&`; not
the `&` of `2>&1`, `&>` or `<&`, nor the `|` of `>|` or zsh's `>&|`) and denies the call when a segment names the
data folder (`AP2_DATA`, its `~` or `$HOME` form, `$AP2_DATA`, `${AP2_DATA}`, `${AP2_DATA:?}`- and
`${AP2_DATA[0]}`-style expansions, a `${HOME:?}`-style one before its path under HOME, `$AP2_RUN_DIR`) or the package
and looks like a write (v1's `_WRITE`, with the redirects that write nowhere removed first, in the text as written
and in its words with quotes removed, so `c\\p` is `cp`; a `sed` with `-i` anywhere after it). The commands inside a
`$(...)` or backticks are segments of their own, and the command around them is one segment with each such text
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
`..`, no `$`, no backtick, no `settings-*.json` (the floor itself, Edit-denied too) and, unquoted (bash and zsh glob
and brace-expand no quoted character, so JSON or code in quotes is text), no `{`, `}` or `,` when the segment has an
unquoted `{` (not that of `${`: bash brace-expands no other) or a `{` inside a `"${...}"`, no `(` when the segment
has an unquoted `(` inside a word (an `@(`-style extglob, a zsh glob group), no dot glob, and at the top level no `[`
and no glob that matches a floor file (or longer than 64 characters) after it, also with an unquoted `}` at its end
dropped (zsh closes a brace there), is allowed: the worker writes its results there. A command that may turn extglob
on (it holds `extglob`), may change a zsh option (`setopt`, `unsetopt`, `emulate`, `options[...]`, a `set -o` other
than `pipefail` and the like), holds zsh's `${(` (outside single quotes and quoted-delimiter heredoc bodies) or an
unquoted extglob opener outside arithmetic, is denied when it names a guarded path anywhere. A command that may make
a name run something else (a function definition, `alias`, `hash` or `autoload` as any word, `source`, `PATH=...`,
`fpath=...`, zsh's `functions[...]`, ...) loses every `ap2` exemption, and a segment of it that names the data folder
or the package outside the own run directory at all is denied (one whose run-directory paths only have a `$` after
them is judged by its verb). The script a shell or `eval` runs (`bash -c '...'`), and the words piped into a shell
with no `-c` (`echo '...' | sh`), are read as a command of their own; when that writes nothing, the outer segment no
longer reads those words (its redirects still count). A zsh script with a `(` that names a guarded path is a write (a
fresh zsh has bare glob qualifiers on). Heredoc body lines that end in a backslash are joined, as bash joins them (an
even run of backslashes too, fail-closed); a `$'...'` or `$"..."` delimiter is decoded as bash decodes it.

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
stops a judgement at a deadline (`DEADLINE`), well inside the hook's 5 s timeout.
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
# `${AP2_DATA:?}`, `${AP2_RUN_DIR%/*}`, ...: an expansion of the variable, never the worker's own run directory.
_EXPANSION = r"\$\{AP2_(?:DATA|RUN_DIR)[:%#/^,+=?\[-]"
_BREAK = set(" \t\r\n;|&<>()`")  # outside quotes, these end a shell word
_EMPTY_PARENS = re.compile(r"\([ \t]*\)")  # the `()` of a function definition
# A path mention ends where the path part ends: `<data>2/` or `<data>-old` is not the data folder. A `$NAME`
# ends where bash ends it, at the first character that is not a name character (`$AP2_DATA_OLD` is another
# variable, `$AP2_RUN_DIR.bak` the run directory and then `.bak`), and a `${NAME}` at its `}`.
_END, _VAR_END = r"(?![A-Za-z0-9_.-])", r"(?![A-Za-z0-9_])"
MAX_STDIN = 1 << 20  # a v2 worker's Bash payload past this is denied (`main` reads no further; no command is 1 MiB)
DEADLINE = 2.0  # seconds `main` gives one judgement before it falls back to `_fallback` (the hook's timeout is 5 s)
ROLES = ("implement", "review", "pr")  # == workers.ROLES: the run directory's floor files are settings-<role>.json
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
    (`${HOME:?}/<rel>`, `${HOME%/}/<rel>`, `${HOME:-/x}/<rel>`): a mention that is never the own run directory, as
    `_EXPANSION` is. The operator's text runs up to the `}`, past no `{` or `}` (a `${` in there is not followed),
    so the scans from two `${HOME` never overlap."""
    out = []
    for path in paths:
        if home and path and path.startswith(home + "/"):
            rel = path[len(home) + 1:]
            out.append(r"\$\{HOME[:%#/^,+=?\[-](?:[^{}$]|\$(?!\{))*\}/" + re.escape(rel) + _END)
    return out


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

    def _level(self, w: str, braces: bool = True, eglob: bool = True, mask=None, forms=None) -> int:
        """How far the case-folded text `w` (one mention, up to the next one in its word; `mask` the same text with
        each quoted glob, bracket, brace, comma or paren character as `_HIDE`, `_Word`) is from a path under the own
        run directory that stays inside it: 0 when it is one; 1 when it is one but for a `$` or backtick after it (a
        value bash fills in at run time); 2 when it is no such path. After the run directory it refuses, unquoted,
        a `{`, `}` or `,` when its segment has an unquoted `{` (`braces`: a brace expansion, also one that opens
        before the mention, as in `{$AP2_RUN_DIR/..,x}`), a `(` when its segment has an unquoted `(` inside a word
        (`eglob`: an extglob such as `@(`, or a zsh glob group; one can match `..` or a floor file), and a bad part
        (`_bad_part`), also once any unquoted `}` at its end is dropped (zsh reads a `}` that ends a word as a
        closing brace: `{ echo x > <run>/settings-implement.json}` writes the floor file). Bash and zsh glob and
        brace-expand no quoted character, so a quoted one is text (JSON or code in quotes). `forms`: the
        run-directory spellings to try (all of them by default)."""
        mask = w if mask is None else mask
        for form in self.run if forms is None else forms:
            if w == form or w.startswith(form + "/"):
                rest, mrest = w[len(form):], mask[len(form):]
                if (braces and any(c in mrest for c in "{},")) or (eglob and "(" in mrest):
                    return 2
                cut = len(mrest) - len(mrest.rstrip("}"))
                pairs = {(rest, mrest), (rest[:len(rest) - cut], mrest[:len(mrest) - cut])}
                if any(_bad_part(p, k == 0, m) for r, mr in pairs  # a rest is empty or starts with `/`
                       for k, (p, m) in enumerate(zip(r.split("/")[1:], mr.split("/")[1:]))):
                    return 2
                return 1 if ("$" in rest or "`" in rest) else 0
        return 2

    def names(self, text: str) -> bool:
        """Whether `text` names a guarded path anywhere, as written or with its quotes removed."""
        return bool(self.any.search(text)) or any(self.any.search(w) for w in _words(text))

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
            for k, m in enumerate(found):
                if m.group(0).casefold() in self.package:
                    return 2
                end = found[k + 1].start() if k + 1 < len(found) else len(w)
                top = max(top, self._level(w[m.start():end], braces, eglob, mw[m.start():end],
                                           self.forms.get(m.group(0).casefold(), ())))
                if top == 2:
                    return 2
        return top


def _norm(word: str) -> str:
    """`word` with `a//b` and `a/./b` read as `a/b`, case-folded (macOS folders are case-insensitive)."""
    if "//" in word or "/./" in word:
        word = re.sub(r"/(?:\./)+", "/", re.sub(r"/{2,}", "/", word))
    return word.casefold()


class _Body(str):
    """A heredoc body line as a segment: data for the command above it, never a command of its own. `quoted`: its
    delimiter had a quote or a backslash, so the shell expands nothing in it (`<<'EOF'`, `<<\\EOF`)."""
    quoted = False


def _body(text: str, quoted: bool) -> _Body:
    line = _Body(text)
    line.quoted = quoted
    return line


class _Pipe(str):
    """A segment that a `|` or `|&` ends: its output is the next command's input."""


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


# Outside quotes, the characters `_segments` acts on; any other one it steps over.
_SPECIAL = frozenset("\\$'\"`(){}[]#<>\n;|&")
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


# `$'...'` escapes bash 3.2 decodes to one character (`\NNN` octal, `\xHH` hex and `\cX` are read in `_ansi_c`).
_ANSI_C = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v",
           "\\": "\\", "'": "'", '"': '"', "?": "?"}
_OCT, _HEX = frozenset("01234567"), frozenset("0123456789abcdefABCDEF")


def _ansi_c(word: str, i: int, out: list):
    """Decodes the text of a `$'...'` from `i` (just past its opening quote) into `out`, as bash 3.2 does: the
    `_ANSI_C` escapes, `\\NNN` (1-3 octal digits), `\\xHH` (1-2 hex digits), `\\cX` (X's low five bits); any other
    escape keeps its backslash (`\\q`). Returns the index of the closing quote, or None for what it cannot decode
    the way every bash would: a NUL or a non-ASCII code, `\\c` with no letter or digit after it, `\\u` and `\\U`
    (bash 4.2 decodes them), or no closing quote."""
    n = len(word)
    while i < n and word[i] != "'":
        if word[i] != "\\" or i + 1 >= n:
            out.append(word[i])
            i += 1
            continue
        e, i = word[i + 1], i + 2
        if e in _ANSI_C:
            out.append(_ANSI_C[e])
            continue
        if e in _OCT or e == "x":
            digits, k = (_OCT, i - 1) if e in _OCT else (_HEX, i)
            stop = k + (3 if e in _OCT else 2)
            j = k
            while j < min(n, stop) and word[j] in digits:
                j += 1
            if j == k:  # `\\x` with no hex digit: kept as it is
                out.append("\\x")
                continue
            code, i = int(word[k:j], 8 if e in _OCT else 16), j
        elif e == "c":
            if i >= n or not word[i].isascii() or not word[i].isalnum():
                return None
            code, i = ord(word[i]) & 0x1F, i + 1
        elif e in "uU":
            return None
        else:
            out.append("\\" + e)
            continue
        if not 0 < code < 0x80:
            return None
        out.append(chr(code))
    return i if i < n else None


def _unquote(word: str):
    """`word` after quote removal only, as bash reads a heredoc delimiter: quotes and the backslashes that escape go,
    every other character stays (``a\\`b`` is ``a`b``, `"a\\$b"` is `a$b`, `"a\\b"` is `a\\b`). `$"..."` reads as
    `"..."`, and `$'...'` is decoded as bash decodes it (`_ansi_c`; `$'E\\x4f'` is `EO`); None when it cannot be
    (`_segments` then opens no heredoc for it, so its lines are read as code)."""
    out, i, n, esc = [], 0, len(word), -1
    while i < n:
        c = word[i]
        if c in "'\"" and word[i - 1:i] == "$" and _expands(word, i - 1, esc):
            out.pop()  # the `$` of a `$'` or `$"`
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


def _segments(cmd: str, pid: bool = True, budget=None, seen=None, sq: bool = True) -> list:
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
    a delimiter `_unquote` cannot decode opens no heredoc. Inside `$(...)` a line of the delimiter and then `)` ends
    the body too (bash 3.2), and the scan goes on at that `)`; a heredoc whose `$(...)` closes on its own line has
    no body (bash runs the next lines). A here-string `<<<` opens no heredoc.
    When `seen` is a set, `@` is added to it for an extglob opener read as code: an unescaped `@`, `+`, `!`, `?` or
    `*` and then `(` (or `((`) outside quotes, `"${...}"`, `"$$(...)"` and arithmetic, as in `cp @(a|b) <data>/c`,
    where bash with extglob on reads no comment and no pipe."""
    budget = _Budget(4 * len(cmd)) if budget is None else budget
    segs, n, subs = [], len(cmd), set()  # subs: the `((` found to be two subshells
    quote, i, esc = None, 0, -1  # esc: the index of the last character a backslash escaped
    # The quote state of the innermost context: the quote (None, `'`, `"`, or `$'`), open `(` (`paren`), open `${`
    # (`brace`), the open arithmetic span if any (`arith`: `["(", open single (, index of its `((`, the state to
    # read it again from]` for `((` and `$((`, `["[", open [, None, None]` for `$[`), and the context's `kind`:
    # None at the top, `(` for `$(`, `{` for `"${`, `$` for `"$$(`, `a` for `"$((`. `stack` holds the saved
    # states of the outer contexts, each with, for a `(`, the outer segment's (`parts`, `pending`).
    # The current segment is `parts` and then `cmd[start:]` (up to `cut`, where a comment ended it), and
    # `pending` the heredocs opened on its line: (delimiter, strip leading tabs).
    paren, brace, arith, kind, stack = 0, 0, None, None, []
    start, parts, cut, pending = 0, [], None, []
    while i < n:
        c = cmd[i]
        if quote is None and c not in _SPECIAL:
            pass  # an ordinary character
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
            segs.extend(_segments(cmd[i + 1:end], pid, budget, seen, sq))
            if kind == "{" and "\\" in cmd[i + 1:end]:  # bash 3.2 can drop a backslash level there: read it so too
                budget.spend(end - i)
                again = _segments(_UNESCAPE.sub(r"\1", cmd[i + 1:end]), pid, budget, seen, sq)
                budget.spend(SEGMENT_COST * len(again))  # each segment is judged on its own later: charged too
                segs.extend(again)
            parts += (cmd[start:i + 1], "_")
            start, i = end, end + 1  # the closing backtick stays in the outer text
            continue
        elif c == "$" and arith is None and quote != "s" and i + 1 not in subs and cmd.startswith("$((", i) and (
                _expands(cmd, i, esc)):
            again = (i, len(segs), start, parts, len(parts), cut, pending, esc, quote, paren, brace, kind, len(stack))
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
                stack.append((quote, paren, brace, arith, kind, (parts, pending)))
                start, parts, cut, pending = i + 2, [], None, []
            else:
                stack.append((quote, paren, brace, arith, kind, None))
            quote, paren, brace, arith, kind = None, 0, int(new == "{"), None, new
            i += 2
            continue
        elif quote == '"':
            if c == '"':
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
            again = (i, len(segs), start, parts, len(parts), cut, pending, esc, quote, paren, brace, kind, len(stack))
            arith = ["(", 0, i, again]
            i += 2
            continue
        elif c == "(":
            if arith is None:
                paren += 1
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
                i, size, start, parts, size_parts, cut, pending, esc, quote, paren, brace, kind, depth = arith[3]
                del segs[size:], parts[size_parts:], stack[depth:]
                arith = None  # as it was at the `((`
                continue
            elif paren:
                paren -= 1
            elif stack and kind != "{":  # the `)` that closes a `$(` or `"$$(`
                if kind == "(":
                    text = "".join(parts) + cmd[start:cut if cut is not None else i]
                    if text:
                        segs.append(text)
                    parts, pending = stack[-1][5]  # a heredoc opened in there and still open has no body
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
                delim = _unquote(m.group(0))
                if delim is not None:  # one it cannot decode opens no heredoc: its lines are read as code
                    pending.append((delim, strip, any(q in m.group(0) for q in "'\"\\")))
                i = m.end()
            else:
                i = j
            continue
        elif c in "\n;|&" and not brace and not (arith and c != ";") and not _in_redirect(cmd, i, esc):
            text = "".join(parts) + cmd[start:cut if cut is not None else i]
            nxt = cmd[i + 1:i + 2]
            segs.append(_Pipe(text) if c == "|" and nxt != "|" else text)  # `|` and `|&` pipe, `||` does not
            parts, cut = [], None
            i += 2 if (c in "&|" and nxt == c) or (c == "|" and nxt == "&") else 1
            if c == "\n":
                closed = False
                for delim, strip, quoted in pending:
                    close = delim + ")"
                    while i < n and not closed:
                        j = cmd.find("\n", i)
                        end = n if j < 0 else j
                        line = cmd[i:end]
                        body = line.lstrip("\t") if strip else line
                        if kind == "(" and body.startswith(close):  # bash 3.2 ends it in `$(...)` at `EOF)` too
                            segs.append(_body(delim, quoted))
                            i = end - len(body) + len(delim)  # the scan goes on at that `)`
                            closed = True  # it closes the `$(`: any other heredoc of that line has no body
                            break
                        segs.append(_body(line, quoted))
                        i = end + 1
                        if body == delim:
                            break
                pending = []
                i = min(i, n)
            start = i
            continue
        i += 1
    while True:  # the end: the segments still open, innermost first
        while stack and not stack[-1][5]:
            stack.pop()  # a `"${`, `"$$(` or `"$((`: still the same segment
        text = "".join(parts) + cmd[start:cut if cut is not None else n]
        if not stack:
            if text or not segs:
                segs.append(text)
            return segs
        if text:
            segs.append(text)
        parts, start, cut = stack.pop()[5][0], n, None


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


def _words(seg: str, targets=None, events=None, pid: bool = True, flags=None, sq: bool = True) -> list:
    """The shell words of `seg` as bash reads quotes and backslashes, with the quotes removed: `"$AP2_RUN_DIR"/../x`
    is one word, `$AP2_RUN_DIR/../x`, and `"$AP2_"DATA` is `$AP2_DATA`; a backslash-newline outside single quotes is
    no character at all, and a lone backslash at the very end is none either (bash drops it). Inside `$(...)`, even
    within `"..."`, quoting starts afresh, as in `_segments`, and a
    backtick runs up to the next unescaped backtick (whatever quotes lie between), read as words of its own. Inside
    `"..."` a `${...}` and the `(...)` of a `$$(` read quotes afresh too, and stay part of the word. A word that a
    `$(`, a `$$(` or an opening backtick ends keeps that `$` or backtick (`$AP2_RUN_DIR$`), so no command's output
    passes for a plain path; the closing `)` or backtick is no part of a word. In `$'...'` a backslash keeps the
    next character as it is. `pid` is the reading of `"$$(`, and `sq` that of a `'` inside `"${...}"`, as in
    `_segments`. Each word is a `_Word`: its `mask` hides each glob, bracket, brace, comma or paren character that
    a quote or a backslash made text (outside `"${...}"`, where bash 3.2 ends the quote at an inner `"`).
    When `targets` is a list, the word after each unquoted `>` (in `>`, `>>`, `>|`, `>&`, `&>`, `<>`, and zsh's
    `>!`, `>>!`, `>&|`, `>&!`, `>>&|` and `>>&!`) is added to it too: the file that redirect writes. When `events`
    is a list, each `$(` or opening backtick adds `(index, True)` to it and each `)` or backtick that closes one
    `(index, False)`, `index` being that of the next word: where a command starts and where it ends, for `_commands`.
    An unquoted `@(`, `+(`, `!(`, `?(` or `*(` (the `(` right after one of those characters, unescaped, in a word)
    is an extglob, as bash reads it with `shopt -s extglob` (and as a syntax error without it): its `(...)` stays in
    the word, with no word break inside (`$X/.@(. |x)/c` is one word). Any other unquoted `(` inside a word (zsh
    reads `$X/(a)` as a glob group, bash as a syntax error) stays at the end of the word it ends (`$X/(`). When
    `flags` is a set, `{` is added to it for an unquoted `{` that is not the `{` of `${` (bash brace-expands only
    such a `{`: `echo "{"a,b}` prints `{a,b}`) and for any `{` inside a `"${...}"` (bash 3.2 ends the quote at an
    inner `"`, so `x=; echo "${x:-"{a,b}"}"` prints `a b`), `(` for a `(` inside a word (what `_level` refuses
    after the run directory), and `()` for an unquoted `(` with only blanks before its `)` (a function
    definition, `ap2() {`)."""
    words, text, hid, i, n = [], [], [], 0, len(seg)  # hid: the indexes in `text` of the characters quoted text
    esc = -1  # the last escaped index
    # The quote state, as in `_segments` (`s`: a `'` inside `"${...}"`, which hides no `$(` or backtick): quote,
    # open `(`, open `${`, the context's kind, and the open `(` of an extglob (`eglob`); `stack` holds the outer
    # contexts' states.
    stack, quote, paren, brace, kind, eglob = [], None, 0, 0, None, 0
    redir = False  # whether the next word is a redirect's target
    dq = 0  # how many `"${` contexts are open
    while i < n:
        c = seg[i]
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
                redir = _flush(words, text, hid, targets, redir)
                if new == "(" and events is not None:
                    events.append((len(words), True))
            i += 2
            continue
        elif c == "`":
            end = _tick_end(seg, i + 1)
            if text:
                text.append(c)
            redir = _flush(words, text, hid, targets, redir)
            inner = []
            sub = _words(seg[i + 1:end], targets, inner, pid, flags, sq)
            if events is not None:
                events.append((len(words), True))
                events.extend((len(words) + k, opens) for k, opens in inner)
                events.append((len(words) + len(sub), False))
            words += sub
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
            if seg[i - 1:i] == "$" and _expands(seg, i - 1, esc):  # `$'...'`: the `$` is not part of the word
                text.pop()
                quote = "$'"
            elif kind == "{":
                if sq:
                    quote = "s"
                else:
                    text.append(c)  # zsh: a plain character in `"${...}"`, so `"${x:-'}"` closes at that `}`
            else:
                quote = c
        elif c == '"':
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
            if c == "(":
                if text:  # `x(`: a zsh glob group (`$AP2_RUN_DIR/(a)`), a `=(`, a function name; bash: an error
                    text.append(c)  # the `(` stays in the word it ends
                    if flags is not None:
                        flags.add("(")
                if flags is not None and _EMPTY_PARENS.match(seg, i):
                    flags.add("()")  # `name()`, `name ()`: a function definition
            redir = _flush(words, text, hid, targets, redir)
            if c == "(":
                paren += 1
            elif c == ")":
                if paren:
                    paren -= 1
                elif stack:
                    if events is not None:
                        events.append((len(words), False))
                    quote, paren, brace, kind, eglob = stack.pop()
            elif c == ">":
                redir = True
                if seg.startswith("!", i + 1):
                    i += 1  # zsh's `>!` (noclobber override, as `>|`): the `!` is part of the operator
                elif seg.startswith(("&!", "&|"), i + 1):
                    i += 2  # zsh's `>&!` and `>&|` (`>>&!`, `>>&|`): the `&` and the `!` or `|` too
        else:
            if c == "{" and flags is not None:
                flags.add("{")
            text.append(c)
        i += 1
    _flush(words, text, hid, targets, redir)
    return words


# The characters a quote or a backslash makes text for globbing and brace expansion (`_Word.mask` hides them).
_GLOBS = frozenset("*?[]{},()")
_HIDE = "\x00"  # what `_Word.mask` puts for such a quoted character: it matches no glob, part or name


class _Word(str):
    """A shell word, with its quotes removed. `mask` is the same text with each character in `_GLOBS` that a quote
    or a backslash made text as `_HIDE` (one character for one, so its indexes stay those of the word), or None
    when there is none: bash and zsh glob and brace-expand only unquoted ones."""
    mask = None


def _flush(words: list, text: list, hid: list, targets, redir: bool) -> bool:
    """Ends the word in `text` (`hid`: the indexes of its quoted `_GLOBS` characters), if any, adding it to
    `targets` too when it is a redirect's target, and empties `text` and `hid`; returns whether the next word
    still is one."""
    if not text:
        return redir
    word = _Word("".join(text))
    if hid:
        hidden = set(hid)
        word.mask = "".join(_HIDE if k in hidden else t for k, t in enumerate(text))
    text.clear()
    hid.clear()
    words.append(word)
    if redir and targets is not None:
        targets.append(word)
    return False


_NEVER = re.compile(r"(?!)")  # matches no word
# xargs flags that take the next word as their value (`xargs -n 1 cmd`).
_XARGS_VALUE = frozenset(("-E", "-I", "-J", "-L", "-P", "-R", "-S", "-a", "-d", "-n", "-s"))
# Commands that run the command named after their flags (`env FOO=1 cmd`, `nice -n 5 cmd`, zsh's `noglob cmd`):
# name -> (the flags that take no value, as a regex; the flags that take the next word). Any other flag (`env -S`,
# `command -v`) leaves the next word an argument.
_RUNNERS = {"xargs": (re.compile(r"-.*", re.S), _XARGS_VALUE), "env": (re.compile(r"-[iv]*"), frozenset(("-u",))),
            "nohup": (re.compile(r"--"), frozenset()), "command": (re.compile(r"-p"), frozenset()),
            "nice": (re.compile(r"-\d+"), frozenset(("-n",))),
            # zsh's precommand modifiers, and `repeat N` (a count, then the command; any other count word leaves
            # the next word an argument, fail-closed: an `ap2` there is no ap2 call).
            "noglob": (_NEVER, frozenset()), "nocorrect": (_NEVER, frozenset()), "-": (_NEVER, frozenset()),
            "repeat": (re.compile(r"\d+"), frozenset())}
# A `NAME=value` word: before a command name, or after `env`, it sets a variable.
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
# `time -p` (POSIX output) still runs the next word. A module constant, not a literal in `_commands`: a function
# holding the "-p" literal reads as a session launch to tests/test_ap2_profile_launch.py.
_TIME_POSIX = "-p"
# Words that can make a name run something else in the same shell: an alias, a `hash` entry, a builtin turned
# off, a function (`function name`, zsh's `autoload` of one found on `fpath`), and the ways to run text that does
# one of those (`eval`, `source`; `.` too, but only as a command, `_rebinds`).
_REBIND = frozenset(("alias", "unalias", "hash", "enable", "builtin", "function", "functions", "eval", "source",
                     "autoload"))
# A word that sets the command search path, zsh's function path or one of zsh's name tables (`PATH=/tmp:$PATH`,
# `fpath=(/tmp/fn)`, `functions[ap2]=...`, `commands[ap2]=/bin/cp`): a name then runs another file or code.
_TABLES = re.compile(r"(?:PATH|path|FPATH|fpath|functions|aliases|galiases|saliases|commands)(?:\[|\+?=)")
# A word that runs a quoted script as code (`bash -c '...'`, `eval '...'`): a shell's name or `eval`, alone or after
# a character no name holds (`/bin/sh`, zsh's `=sh`, `$@eval` with `$@` empty), so `ssh` and `run.sh` are none.
_SHELL = re.compile(r"(?<![A-Za-z0-9_.])(?:sh|bash|zsh|ksh|dash|eval)\Z")
# zsh among those: a fresh zsh has bare glob qualifiers on, so `/(e:'cmd':)` in its script runs `cmd`.
_ZSH = re.compile(r"(?<![A-Za-z0-9_.])zsh\Z")
# zsh's `always` word (`{ a } always { b }`), up to a character that ends a word.
_ALWAYS = re.compile(r"always(?![^\s;&|(){}<>])")


def _commands(words: list, events: list):
    """The indexes of the words bash runs as a command name: the first word after the keywords and `NAME=value`
    words (and a `-p` after `time`) from the segment's start and from each `$(...)` or backticks that opens
    (`events`, from `_words`; one that closes first, as in `$(time)`, names none), and the word a runner
    (`_RUNNERS`: `xargs`, `env`, `nohup`, `command`, `nice`, zsh's `noglob`, `nocorrect`, `-` and `repeat N`) runs
    after its flags. Also the segment's own command
    (`lead`): the last of that chain from the segment's start (`nohup env A=1 <cmd>` is `<cmd>`), or None."""
    out, lead, want, top, run, value, e = [], None, True, True, None, False, 0
    for k, w in enumerate(words):
        while e < len(events) and events[e][0] <= k:
            want, top, run, value = events[e][1], False, None, False
            e += 1
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
            want, run = not w.startswith("-"), None  # an unknown flag: no command after it
        if want and w not in _KEYWORDS and not _ASSIGN.match(w) and not (
                w == _TIME_POSIX and k and words[k - 1] == "time"):
            out.append(k)
            if top:
                lead = k
            name = w.rsplit("/", 1)[-1]
            want, run = False, name if name in _RUNNERS else None
    return out, lead


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
    found piece by piece."""
    return bool(_WRITE.search(text) or _sed_in_place(text)
                or any(_after_first(p, _DD, _OF) for p in re.split(r"[|;&]", text))
                or any(_after_first(p, _OPEN, _MODE) for p in text.split(")")))


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
    for k, (pid, sq) in enumerate(readings):
        if k:
            budget.spend(len(cmd))
        seen = set()
        segs = _joined(_segments(cmd, pid, budget, seen, sq))
        # Extglob may be on (an option name built at run time): `@(...)` is then one word with no comment or pipe
        # inside, which this reading does not follow.
        if "@" in seen and marks.names(cmd):
            return True
        parsed = [_parse(seg, pid, sq, budget) for seg in segs]
        # zsh's `${(` flag group (`"${(e)x:-\$(cmd)}"` runs `cmd`), which this reading does not follow.
        if any(_flag_group(p) for p in parsed) and marks.names(cmd):
            return True
        rebound = _rebinds(parsed)
        # A shell that reads its script from its input (`... | sh`, no `-c`): the words of each segment piped
        # before it are read as a script too (`echo 'tee <run>/settings-implement.json < f' | sh`), and those
        # segments are `fed` (`_writes_guarded`).
        fed = set()
        if any(isinstance(seg, _Pipe) for seg in segs):
            shells = [(j, w) for j, p in enumerate(parsed) if not p.body and (w := _stdin_shell(p.words))]
            last, zsh = (shells[-1][0] if shells else 0), any(_ZSH.search(w) for _, w in shells)
            for j in range(last):
                if isinstance(segs[j], _Pipe):
                    if _scripts_write(parsed[j].words, marks, depth, budget, zsh):
                        return True
                    fed.add(j)
        for j, (seg, p) in enumerate(zip(segs, parsed)):
            key = _alike(seg) and (seg, p.body, p.quoted, rebound, depth, j in fed)
            if key in budget.clean:
                continue  # judged in an earlier reading, which read it alike
            if _writes_guarded(seg, marks, pid, rebound, depth, budget, sq, p, j in fed):
                return True
            if key:
                budget.clean.add(key)
    return False


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
    `events`, `flags`, the indexes of its command words and its own command (`commands`, `lead`, from
    `_commands`), whether it is a heredoc body line (`body`) and one of a heredoc with a quoted delimiter (`quoted`)."""

    def __init__(self, seg: str, pid: bool, sq: bool):
        self.targets, self.events, self.flags, self.body = [], [], set(), isinstance(seg, _Body)
        self.quoted = self.body and seg.quoted
        self.words = _words(seg, self.targets, self.events, pid, self.flags, sq)
        self.commands, self.lead = _commands(self.words, self.events)


def _stdin_shell(words: list):
    """The shell word, when a segment of the shell words `words` runs a shell (`_SHELL`, not `eval`) with no `-c` (no
    flag word with a `c` after it), so the shell may read its script from its input; else None."""
    k = next((k for k, w in enumerate(words) if _SHELL.search(w) and not w.endswith("eval")), None)
    if k is None or any(w[:1] == "-" and w[1:2] != "-" and "c" in w for w in words[k + 1:]):
        return None
    return words[k]


def _scripts_write(words: list, marks: _Marks, depth: int, budget: _Budget, zsh: bool = False) -> bool:
    """Whether the shell words `words` (a shell's or `eval`'s, or a segment piped into a shell) write into a guarded
    path when read as a script: each one that names a guarded path, and all of them joined by blanks (as `eval`
    joins them), read as a command of its own, one level deeper. Below the top level only the joined text is read
    (it holds each word). At `MAX_DEPTH` a script that names a guarded path is a write. Each text read spends its
    length from `budget`, once (a text already judged at that depth, or found to name nothing, is not read again).
    For `zsh` (the shell is zsh) a script with a `(` anywhere that names a guarded path is a write: a fresh zsh has
    bare glob qualifiers on (`zsh -c "ap2 card list /(e:'cmd':)"` runs `cmd`), which this reading does not
    follow."""
    joined = " ".join(words)
    if zsh and "(" in joined and marks.names(joined):
        return True
    texts = [joined] if depth or len(words) < 2 else list(words) + [joined]
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
    """Whether a segment of the command (not a heredoc body line) may make a name run something else: it defines a
    function (`name()`, `name ()`, `function name`; bash's `command_not_found_handle` and zsh's
    `command_not_found_handler`, which run for a name that is not found, are functions too), holds one of `_REBIND`
    as any word (`alias ap2=cp`, `hash -p /bin/cp ap2`, zsh's `hash ap2=/bin/cp`, also after a case pattern or a
    zsh precommand modifier: `case x in x) hash ...`, `noglob hash ...`; fail-closed, `grep -w hash` counts too)
    or runs `.` as a command (`find .` is no rebinding), or holds a word that sets the search path, zsh's function
    path or a zsh name table (`_TABLES`: `PATH=/tmp:$PATH`, `fpath=(/tmp/fn)`, `functions[ap2]=...`)."""
    for p in parsed:  # `_Parsed` segments
        if p.body:
            continue
        if "()" in p.flags or any(p.words[k] == "." for k in p.commands) or any(
                w in _REBIND or _TABLES.match(w) for w in p.words):
            return True
    return False


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
                    sq: bool = True, parsed=None, fed: bool = False) -> bool:
    """Whether the segment `seg` (read with `pid` and `sq`, as in `_segments`; `parsed` its `_Parsed`, when the caller
    has it) writes into a guarded path. With
    `rebound` (`_rebinds`: a name may run anything) no segment keeps the `ap2` exemption, and one that names the data
    folder or the package (`ap2` and `<pkg>/bin/ap2` too) outside the own run directory at all is a write; one whose
    only such mentions are run-directory paths with a `$` or backtick after them (`_level` 1) is judged by its verb,
    as any other. A segment with a shell or `eval` in it (`_SHELL`) also writes when the words after it write as a
    script (`_scripts_write`: `bash -c 'tee $AP2_RUN_DIR/settings-implement.json < f'`, whose quotes hide the
    script's words, braces and line continuations from this segment's reading). When that shell is one of the
    segment's command words and its script writes nothing, the words after it are left out of this segment's own
    reading (they were judged as the script; its redirect targets still count), and so are all the words of a
    segment piped into a shell (`fed`, its words already read as that shell's script); only where each word was also
    read on its own (`depth` 0, or a single word). The verb is looked for in the text as written and in its words
    with quotes and backslashes removed (`c\\p` and `c''p` are `cp`)."""
    if not seg.strip():
        return False
    budget = _Budget(BUDGET * len(seg) + BUDGET_EXTRA) if budget is None else budget
    p = _Parsed(seg, pid, sq) if parsed is None else parsed
    words, targets, flags = p.words, p.targets, p.flags
    braces, eglob = "{" in flags, "(" in flags  # an unquoted `{` or `(` in a word in the segment (`_level`)
    shell = next((k for k, w in enumerate(words) if _SHELL.search(w)), None)
    cut = len(words)  # the words from this index on (redirect targets aside) were judged as a script
    if shell is not None:
        if _scripts_write(words[shell + 1:], marks, depth, budget, bool(_ZSH.search(words[shell]))):
            return True
        if shell in p.commands and (depth == 0 or len(words) - shell - 1 < 2):
            cut = shell + 1
    if fed and (depth == 0 or len(words) < 2):
        cut = 0
    if rebound:
        level = marks.level(words, braces, eglob)
        return level == 2 or (level == 1 and _verb(seg, words))
    commands, lead = p.commands, p.lead
    ap2 = not p.body and _runs_ap2(words, lead)
    # `<pkg>/bin/ap2` where bash runs it as a command is no write into the package (unless a redirect names it).
    written = set(targets)
    run = {k for k in commands if words[k] not in written and marks.is_ap2(words[k])}
    kept = {id(t) for t in targets}  # a redirect's target stays in, script word or not
    if not marks.unsafe((w for k, w in enumerate(words) if k not in run and (k < cut or id(w) in kept)),
                        braces, eglob):
        return False
    if ap2 and not _runs_more(seg, pid, sq):
        return marks.unsafe(targets, braces, eglob)  # a redirect's file is the shell writing, not ap2
    return _verb(seg, words)


def _verb(seg: str, words: list) -> bool:
    """Whether the segment `seg` (its shell words `words`) holds a write verb (`_writes`, after `_harmless`): in its
    text as written, or in its words joined by blanks, with quotes and backslashes removed as the shell removes them
    before it runs a word (`c\\p`, `c''p`, `c\\<newline>p` and `sed '-i'` are `cp` and `sed -i`)."""
    return _writes(_harmless(seg)) or _writes(_harmless(" ".join(words)))


def _fallback(raw: bytes, env):
    """A payload too deep to parse, or one judged past the deadline: denied when it is a v2 worker's Bash call
    naming a guarded path."""
    if not env.get("AP2_ROLE") or b'"Bash"' not in raw:
        return None
    # The text is JSON: undo its escapes, so a quote-split `"$AP2_"DATA` or a line continuation reads as the shell's.
    text = _JSON_ESC.sub(_unescape, raw.decode("utf-8", "replace"))
    return _deny() if _Marks(env).names(text) else None


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
    stops a slow regex match) falls back to `_fallback`."""
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
            payload = json.loads(raw.decode("utf-8"))
        except RecursionError:
            return _fallback(raw, env)
        return decide(payload, env) if isinstance(payload, dict) else None
    except _Late:
        return _fallback(raw, env)
    finally:
        if armed:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, before if before is not None else signal.SIG_DFL)


def main(argv: list, stdin=None) -> int:
    """`ap2 hook-guard`: the payload on stdin, the deny JSON (or nothing) on stdout, exit 0 always. Any argument is
    no opinion."""
    try:
        if argv or not os.environ.get("AP2_ROLE") or stdin is None:
            return 0
        raw = getattr(stdin, "buffer", stdin).read(MAX_STDIN + 1)
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        out = _judge(raw, os.environ)
        if out:  # unbuffered: a closed stdout raises here, not again at interpreter exit
            os.write(1, (json.dumps(out) + "\n").encode())
    except Exception:  # noqa: BLE001 - a hook must never break the session
        pass
    return 0
