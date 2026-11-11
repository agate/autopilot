"""`ap2 hook-guard`: the PreToolUse hook on Bash that keeps a v2 worker's shell out of the data folder and the
package (design 8.1, section 12 workers).

`workers.floor()` wires it into every worker's settings after the park hook. For a Bash call from a v2 worker
(`AP2_ROLE` set) it splits the command into segments (newlines, `;`, `&&`, `||`, `|`, `|&`, and a background `&`;
not the `&` of `2>&1`, `&>` or `<&`, nor the `|` of `>|`) and denies the call when a segment names the data folder
(`AP2_DATA`, its `~` or `$HOME` form, `$AP2_DATA`, `${AP2_DATA}`, `${AP2_DATA:?}`-style expansions,
`$AP2_RUN_DIR`) or the package and looks like a write (v1's `_WRITE`, with the redirects that write nowhere removed
first). A segment that runs `ap2` may name them (`ap2` writes the data folder itself); only a redirect in it counts
as a write, unless it also runs other shell code (`$(...)`, a backtick, `<(...)`, a function named `ap2`). A
segment whose every data-folder mention is a path under the worker's own run directory (`AP2_RUN_DIR`) with no
`..`, no `$` and no `settings-*.json` (the floor itself, Edit-denied too) after it is allowed: the worker writes
its results there.

Best effort: it reads the command text, so a path built at run time (a variable, `$(...)`, a `cd` first and a
relative path after, a symlink) can get past it. The `Edit` deny rules of the floor stay the main guard.

A copy of v1's `autopilot/guard.py`, adapted, not imported (design 3.2: v2 copies v1 code paths it may change). It
runs before every Bash call of a worker, so it imports only the standard library. It writes nothing and never
breaks a session: any error is no output and exit 0. Every step is linear in the command's length, and `main`
stops a judgement at a deadline (`DEADLINE`), well inside the hook's 5 s timeout.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import signal
from pathlib import Path

# v1 guard._WRITE (shell forms that write a file), with its three forms that scan ahead, `sed -x ... -i`,
# `dd ... of=` and `open(..., 'w')`, taken out into `_writes`: as one regex they are quadratic on a long run of
# `-sed `, `dd ` or `open(`.
_WRITE = re.compile(r"(>>?|\btee\b|\bperl\s+-[a-zA-Z]*i|\bcp\b|\bmv\b|\brm\b|\btruncate\b|"
                    r"\bln\b|\binstall\b|\.write\(|write_text|\bchmod\b|\btouch\b)")
_SED, _FLAG = re.compile(r"\bsed\s+"), re.compile(r"-[a-zA-Z]*\s+")
_DD, _OF = re.compile(r"\bdd\b"), re.compile(r"\bof=")
_OPEN, _MODE = re.compile(r"\bopen\("), re.compile(r",\s*['\"][wax+]")
# v1 guard._HARMLESS (`\d*>&\d+|&>>?\s*/dev/null|\d*>>?\s*/dev/null`: redirects that write nowhere, a descriptor
# copy (2>&1) or output thrown away (>/dev/null)), with the same `.sub` result in linear time: v1's leading `\d*`
# rescans a digit run from each of its digits. Here a digit run is read once, whole, and kept when no redirect
# follows it (`_harmless`).
_HARMLESS = re.compile(r"\d+(?:>&\d+|>>?\s*/dev/null)?|>&\d+|&>>?\s*/dev/null|>>?\s*/dev/null")
# `${AP2_DATA:?}`, `${AP2_RUN_DIR%/*}`, ...: an expansion of the variable, never the worker's own run directory.
_EXPANSION = r"\$\{AP2_(?:DATA|RUN_DIR)[:%#/^,+=?-]"
_BREAK = set(" \t\r\n;|&<>()`")  # outside quotes, these end a shell word
# A mention ends where the path part ends: `$AP2_DATA_OLD` or `<data>2/` is not the data folder.
_END = r"(?![A-Za-z0-9_.-])"
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


def _bad_part(part: str, top: bool) -> bool:
    """A path part under the run directory that leaves it or names the floor: `..`, a glob that can match `..` (bash
    3.2 matches `.*` to it), a `settings-*.json`, or, at the top level (`top`), where the floor's
    `settings-<role>.json` files live, a glob that matches one (`*`, `settings*`)."""
    if part == ".." or (part.startswith(".") and any(c in part for c in "*?[")):
        return True
    if fnmatch.fnmatchcase(part, "settings-*.json"):
        return True
    return top and any(fnmatch.fnmatchcase(f"settings-{role}.json", part) for role in ROLES)


class _Marks:
    """The spellings of the data folder, the package and the worker's run directory, from the worker's env."""

    def __init__(self, env):
        home = _clean(str(env.get("HOME") or ""))
        data = _clean(str(env.get("AP2_DATA") or ""))
        run = _clean(str(env.get("AP2_RUN_DIR") or ""))
        self.package = [f.casefold() for f in _forms(str(package_root()), home)]
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
        # An expansion form (`_EXPANSION`) matches no run-directory spelling, so `unsafe` always counts it.
        self.any = re.compile("|".join([re.escape(n) + _END for n in names] + [_EXPANSION]), re.IGNORECASE)

    def _own_run(self, w: str) -> bool:
        """Whether the case-folded word `w` is a path under the own run directory that stays inside it."""
        for form in self.run:
            if w == form or w.startswith(form + "/"):
                rest = w[len(form):]
                parts = rest.split("/")[1:]  # rest is empty or starts with `/`
                return "$" not in rest and "{" not in rest and not any(
                    _bad_part(p, k == 0) for k, p in enumerate(parts))
        return False

    def _own_run_at(self, w: str, at: int) -> bool:
        """`_own_run(w[at:])` for a mention after one that passed it: that one checked the whole rest of `w` (no
        `$`, no `{`, no bad part below the top level), so only the form and the one part right after it are new."""
        for form in self.run:
            end = at + len(form)
            if w.startswith(form, at) and (end == len(w) or w[end] == "/"):
                if end == len(w):
                    return True
                stop = w.find("/", end + 1)
                return not _bad_part(w[end + 1:stop if stop >= 0 else len(w)], True)
        return False

    def names(self, text: str) -> bool:
        """Whether `text` names a guarded path anywhere, as written or with its quotes removed."""
        return bool(self.any.search(text)) or any(self.any.search(w) for w in _words(text))

    def unsafe(self, seg: str) -> bool:
        """Whether `seg` names the data folder or the package anywhere but under the worker's own run directory."""
        for word in _words(seg):
            # `a//b` and `a/./b` are `a/b`; case-folded once (macOS folders are case-insensitive).
            w = re.sub(r"/(?:\./)+", "/", re.sub(r"/{2,}", "/", word)).casefold()
            checked = False  # whether a mention in `w` passed `_own_run` (each later one is then checked cheaply)
            for m in self.any.finditer(w):
                if m.group(0).casefold() in self.package:
                    return True
                if not (self._own_run_at(w, m.start()) if checked else self._own_run(w[m.start():])):
                    return True
                checked = True
        return False


class _Body(str):
    """A heredoc body line as a segment: data for the command above it, never a command of its own."""


# The delimiter word after `<<`: unquoted characters that do not end a word, escapes, and quoted runs.
_DELIM = re.compile(r"""(?:[^\s;|&<>()'"\\]|\\.|'[^']*'|"(?:[^"\\]|\\.)*")+""", re.S)


# Outside quotes, the characters `_segments` acts on; any other one it steps over.
_SPECIAL = frozenset("\\$'\"`(){}#<>\n;|&")
# The first word of a segment, then `(`: `ap2(){ ...; }` or `ap2 () ...` defines a function, it does not run ap2.
_FUNC = re.compile(r"\s*(?:" + _DELIM.pattern + r")[ \t]*\(", re.S)


def _segments(cmd: str) -> list:
    """`cmd` cut at the separators v1 splits on (newline, `;`, `&&`, `||`, `|`), and at `|&` and a background `&`,
    only where bash reads them as separators: outside quotes and not after a backslash. `sed -i 's|a|b|' f` is one
    segment. The `&` of `2>&1`, `>&2`, `<&3`, `&>` and `&>>` and the `|` of `>|` are part of a redirect, not
    separators. An unterminated quote keeps the rest as one segment. The segments keep their text as written (quotes
    and backslashes too).
    Quotes nest as in bash: inside `$(...)`, even within `"..."`, quoting starts afresh (`"$(cmd "a b")"`), up to
    the `)` that closes it; separators in there split too, since bash runs them. `$'...'` is an ANSI-C quote, where
    `\\'` stays inside.
    Bash reads no quotes in a comment or a heredoc body, and neither does this: an unquoted `#` at the start of a
    word (not after an escaped blank, not inside `${...}` or backticks) drops the rest of its line, and after an
    unquoted `<<` or `<<-` the lines from the next newline on, up to and including the delimiter line (leading tabs
    stripped for `<<-`), are each one segment (`_Body`), with no scan. A here-string `<<<` and a shift inside
    `((...))` or `$((...))` open no heredoc. A `((` closed by a single `)` first (`((cd x; ls) )`) was two
    subshells, not arithmetic; a newline ends any arithmetic span."""
    segs, start, quote, i, n = [], 0, None, 0, len(cmd)
    cut = None  # where a comment ended the current segment
    pending = []  # the heredocs opened on this line: (delimiter, strip leading tabs)
    esc = -1  # the index of the last character a backslash escaped
    # The quote state, one context per open `$(`: the quote (None, `'`, `"`, or `$'`), open `(` (`paren`), open
    # `${` (`brace`), inside backticks (`tick`), and per open `((` / `$((` its own open single `(` (`arith`).
    paren, brace, tick, arith, stack = 0, 0, False, [], []
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
        elif c == "\\":
            i += 1
            esc = i
        elif c == "$" and cmd.startswith("$(", i) and not cmd.startswith("$((", i):
            stack.append((quote, paren, brace, tick, arith))
            quote, paren, brace, tick, arith = None, 0, 0, False, []
            i += 2
            continue
        elif quote == '"':
            if c == '"':
                quote = None
        elif c == "'":
            quote = "$'" if cmd[i - 1:i] == "$" and esc != i - 1 else "'"
        elif c == '"':
            quote = c
        elif c == "$" and cmd.startswith("${", i):
            brace += 1
            i += 2
            continue
        elif c == "}" and brace:
            brace -= 1
        elif c == "`":
            tick = not tick
        elif cmd.startswith("((", i):
            arith.append(0)
            i += 2
            continue
        elif c == "(":
            if arith:
                arith[-1] += 1
            else:
                paren += 1
        elif c == ")":
            if arith and arith[-1]:
                arith[-1] -= 1
            elif arith and cmd.startswith("))", i):
                arith.pop()
                i += 2
                continue
            elif arith:  # the `((` was two subshells: this `)` closes the inner one, the outer one is still open
                arith.pop()
                paren += 1
            elif paren:
                paren -= 1
            elif stack:
                quote, paren, brace, tick, arith = stack.pop()
        elif c == "#" and not brace and not tick and (i == 0 or (cmd[i - 1] in " \t\n;|&(" and esc != i - 1)):
            cut = i  # after `)` bash keeps `#` in the word
            j = cmd.find("\n", i)
            i = n if j < 0 else j  # the newline still ends the segment, below
            continue
        elif cmd.startswith("<<<", i):  # a here-string: one word of input, no body lines
            i += 3
            continue
        elif cmd.startswith("<<", i) and not arith:
            j = i + 2
            strip = cmd[j:j + 1] == "-"
            j += strip
            while j < n and cmd[j] in " \t":
                j += 1
            m = _DELIM.match(cmd, j)
            if m:
                pending.append(("".join(_words(m.group(0))), strip))
                i = m.end()
            else:
                i = j
            continue
        elif c in "\n;|&" and not _in_redirect(cmd, i, esc):
            segs.append(cmd[start:cut if cut is not None else i])
            cut = None
            nxt = cmd[i + 1:i + 2]
            i += 2 if (c in "&|" and nxt == c) or (c == "|" and nxt == "&") else 1
            if c == "\n":
                arith = []
                for delim, strip in pending:
                    while i < n:
                        j = cmd.find("\n", i)
                        end = n if j < 0 else j
                        line = cmd[i:end]
                        segs.append(_Body(line))
                        i = end + 1
                        if (line.lstrip("\t") if strip else line) == delim:
                            break
                pending = []
                i = min(i, n)
            start = i
            continue
        i += 1
    if start < n or not segs:
        segs.append(cmd[start:cut if cut is not None else n])
    return segs


def _in_redirect(cmd: str, i: int, esc: int) -> bool:
    """Whether the `|` or `&` at `i` belongs to a redirect: `>|`, `>&`, `<&` (after an unescaped `>` or `<`) or
    `&>` / `&>>`."""
    c = cmd[i]
    after = i > 0 and esc != i - 1 and cmd[i - 1] in (">" if c == "|" else "<>")
    return c in "|&" and (after or (c == "&" and cmd[i + 1:i + 2] == ">"))


def _words(seg: str) -> list:
    """The shell words of `seg` as bash reads quotes and backslashes, with the quotes removed: `"$AP2_RUN_DIR"/../x`
    is one word, `$AP2_RUN_DIR/../x`, and `"$AP2_"DATA` is `$AP2_DATA`. Inside `$(...)`, even within `"..."`,
    quoting starts afresh, as in `_segments`; in `$'...'` a backslash keeps the next character as it is."""
    words, text, quote, i, n = [], [], None, 0, len(seg)
    esc, paren, stack = -1, 0, []  # the last escaped index, open `(`, and the state saved at each open `$(`
    while i < n:
        c = seg[i]
        if quote == "'":
            if c == "'":
                quote = None
            else:
                text.append(c)
        elif quote == "$'":
            if c == "'":
                quote = None
            elif c == "\\" and i + 1 < n:
                i += 1
                text.append(seg[i])
            else:
                text.append(c)
        elif c == "\\" and i + 1 < n:
            i += 1
            esc = i
            text.append(seg[i])
        elif c == "$" and seg.startswith("$(", i) and not seg.startswith("$((", i):
            stack.append((quote, paren))
            quote, paren = None, 0
            text.append(c)
            words.append("".join(text))
            text = []
            i += 2
            continue
        elif quote == '"':
            if c == '"':
                quote = None
            else:
                text.append(c)
        elif c == "'":
            if seg[i - 1:i] == "$" and esc != i - 1:  # `$'...'`: the `$` is not part of the word
                text.pop()
                quote = "$'"
            else:
                quote = c
        elif c == '"':
            quote = c
        elif c in _BREAK:
            if c == "(":
                paren += 1
            elif c == ")":
                if paren:
                    paren -= 1
                elif stack:
                    quote, paren = stack.pop()
            if text:
                words.append("".join(text))
            text = []
        else:
            text.append(c)
        i += 1
    if text:
        words.append("".join(text))
    return words


def _runs_ap2(segment: str) -> bool:
    words = _words(segment)
    return bool(words) and (words[0] == "ap2" or words[0].endswith("/bin/ap2"))


def _runs_more(segment: str) -> bool:
    """Whether an `ap2` segment also runs other shell code, so its exemption ends: outside single quotes a `$(`
    (not `$((`), a backtick, `<(` or `>(`; a first word followed by `(` (a function named `ap2`); or a quote still
    open at its end."""
    if _FUNC.match(segment):
        return True
    quote, i, n = None, 0, len(segment)
    while i < n:
        c = segment[i]
        if quote == "'":
            if c == "'":
                quote = None
        elif quote == "$'":
            if c == "\\":
                i += 1
            elif c == "'":
                quote = None
        elif c == "\\":
            i += 1
        elif c == "`" or segment.startswith(("<(", ">("), i) or (
                segment.startswith("$(", i) and not segment.startswith("$((", i)):
            return True
        elif c == '"':
            quote = None if quote else c
        elif c == "'" and not quote:
            quote = "$'" if segment[i - 1:i] == "$" and (i < 2 or segment[i - 2] != "\\") else "'"
        i += 1
    return quote is not None


def _after_first(piece: str, first, then) -> bool:
    m = first.search(piece)
    return bool(m) and bool(then.search(piece, m.end()))


def _sed_in_place(text: str) -> bool:
    """v1's `\\bsed\\s+(-[a-zA-Z]*\\s+)*-i`, read token by token: from each `sed`, its `-x` flags up to a `-i`. A
    `sed` inside a flag run already read (`-sed `) starts the same run's tail, so the search goes on after it."""
    pos = 0
    while True:
        m = _SED.search(text, pos)
        if not m:
            return False
        j = m.end()
        while not text.startswith("-i", j):
            k = _FLAG.match(text, j)
            if not k:
                break
            j = k.end()
        if text.startswith("-i", j):
            return True
        pos = max(j, m.start() + 1)


def _writes(text: str) -> bool:
    """v1's `_WRITE.search(text)`, in linear time: `sed` with `-x` flags up to a `-i`, `dd` with an `of=` after it
    and no `|`, `;` or `&` between, and `open(` with a `, 'w'` (or a, x, +) mode after it and no `)` between, are
    found piece by piece."""
    return bool(_WRITE.search(text) or _sed_in_place(text)
                or any(_after_first(p, _DD, _OF) for p in re.split(r"[|;&]", text))
                or any(_after_first(p, _OPEN, _MODE) for p in text.split(")")))


def _harmless(text: str) -> str:
    """`text` with v1's `_HARMLESS` redirects replaced by a space (a digit run with no redirect after it stays)."""
    return _HARMLESS.sub(lambda m: m.group(0) if m.group(0).isdigit() else " ", text)


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
    for seg in _segments(cmd):
        if not seg.strip() or not marks.unsafe(seg):
            continue
        rest = _harmless(seg)
        ap2 = not isinstance(seg, _Body) and _runs_ap2(seg) and not _runs_more(seg)
        if (">" in rest) if ap2 else _writes(rest):
            return _deny()
    return None


def _fallback(raw: bytes, env):
    """A payload too deep to parse, or one judged past the deadline: denied when it is a v2 worker's Bash call
    naming a guarded path."""
    if not env.get("AP2_ROLE") or b'"Bash"' not in raw:
        return None
    # The text is JSON: undo its `\\` and `\"` escapes, so a quote-split `"$AP2_"DATA` reads as the shell's.
    text = raw.decode("utf-8", "replace").replace("\\\\", "\0").replace('\\"', '"').replace("\0", "\\")
    return _deny() if _Marks(env).names(text) else None


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
