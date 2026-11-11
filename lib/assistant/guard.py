"""`ap2 hook-guard`: the PreToolUse hook on Bash that keeps a v2 worker's shell out of the data folder and the
package (design 8.1, section 12 workers).

`workers.floor()` wires it into every worker's settings after the park hook. For a Bash call from a v2 worker
(`AP2_ROLE` set) it splits the command into segments (newlines, `;`, `&&`, `||`, `|`) and denies the call when a
segment names the data folder (`AP2_DATA`, its `~` or `$HOME` form, `$AP2_DATA`, `${AP2_DATA}`, `$AP2_RUN_DIR`) or
the package and looks like a write (v1's `_WRITE`, with the redirects that write nowhere removed first). A segment
that runs `ap2` may name them (`ap2` writes the data folder itself); only a redirect in it counts as a write. A
segment whose every data-folder mention is a path under the worker's own run directory (`AP2_RUN_DIR`) with no `..`,
no `$` and no `settings-*.json` (the floor itself, Edit-denied too) after it is allowed: the worker writes its
results there.

Best effort: it reads the command text, so a path built at run time (a variable, `$(...)`, a `cd` first and a
relative path after) can get past it. The `Edit` deny rules of the floor stay the main guard.

A copy of v1's `autopilot/guard.py`, adapted, not imported (design 3.2: v2 copies v1 code paths it may change). It
runs before every Bash call of a worker, so it imports only the standard library. It writes nothing and never
breaks a session: any error is no output and exit 0.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
from pathlib import Path

# v1 guard._WRITE (shell forms that write a file), with its two forms that scan ahead, `dd ... of=` and
# `open(..., 'w')`, taken out into `_writes`: as one regex they are quadratic on a long run of `dd ` or `open(`.
_WRITE = re.compile(r"(>>?|\btee\b|\bsed\s+(-[a-zA-Z]*\s+)*-i|\bperl\s+-[a-zA-Z]*i|\bcp\b|\bmv\b|\brm\b|\btruncate\b|"
                    r"\bln\b|\binstall\b|\.write\(|write_text|\bchmod\b|\btouch\b)")
_DD, _OF = re.compile(r"\bdd\b"), re.compile(r"\bof=")
_OPEN, _MODE = re.compile(r"\bopen\("), re.compile(r",\s*['\"][wax+]")
# == v1 guard._HARMLESS: redirects that write nowhere, a descriptor copy (2>&1) or output thrown away (>/dev/null).
_HARMLESS = re.compile(r"\d*>&\d+|&>>?\s*/dev/null|\d*>>?\s*/dev/null")
_BREAK = set(" \t\r\n;|&<>()`")  # outside quotes, these end a shell word
# A mention ends where the path part ends: `$AP2_DATA_OLD` or `<data>2/` is not the data folder.
_END = r"(?![A-Za-z0-9_.-])"
MAX_STDIN = 1 << 20  # a payload read past this is judged from its text (`_fallback`), well inside the timeout
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
        self.any = re.compile("|".join(re.escape(n) + _END for n in names), re.IGNORECASE)

    def _own_run(self, word: str) -> bool:
        w = word.casefold()
        for form in self.run:
            if w == form or w.startswith(form + "/"):
                rest = w[len(form):]
                parts = rest.split("/")[1:]  # rest is empty or starts with `/`
                return "$" not in rest and "{" not in rest and not any(
                    _bad_part(p, k == 0) for k, p in enumerate(parts))
        return False

    def names(self, text: str) -> bool:
        """Whether `text` names a guarded path anywhere, as written or with its quotes removed."""
        return bool(self.any.search(text)) or any(self.any.search(w) for w in _words(text))

    def unsafe(self, seg: str) -> bool:
        """Whether `seg` names the data folder or the package anywhere but under the worker's own run directory."""
        for word in _words(seg):
            word = re.sub(r"/(?:\./)+", "/", re.sub(r"/{2,}", "/", word))  # `a//b` and `a/./b` are `a/b`
            for m in self.any.finditer(word):
                if m.group(0).casefold() in self.package or not self._own_run(word[m.start():]):
                    return True
        return False


class _Body(str):
    """A heredoc body line as a segment: data for the command above it, never a command of its own."""


# The delimiter word after `<<`: unquoted characters that do not end a word, escapes, and quoted runs.
_DELIM = re.compile(r"""(?:[^\s;|&<>()'"\\]|\\.|'[^']*'|"(?:[^"\\]|\\.)*")+""", re.S)


def _segments(cmd: str) -> list:
    """`cmd` cut at the separators v1 splits on (newline, `;`, `&&`, `||`, `|`), only where bash reads them as
    separators: outside quotes and not after a backslash. `sed -i 's|a|b|' f` is one segment. An unterminated quote
    keeps the rest as one segment. The segments keep their text as written (quotes and backslashes too).
    Bash reads no quotes in a comment or a heredoc body, and neither does this: an unquoted `#` at the start of a
    word drops the rest of its line, and after an unquoted `<<` or `<<-` the lines from the next newline on, up to and
    including the delimiter line (leading tabs stripped for `<<-`), are each one segment (`_Body`), with no scan. A
    here-string `<<<` and a shift inside `((...))` or `$((...))` open no heredoc."""
    segs, start, quote, i, n = [], 0, None, 0, len(cmd)
    cut = None  # where a comment ended the current segment
    pending = []  # the heredocs opened on this line: (delimiter, strip leading tabs)
    arith = 0  # depth of `((` / `$((`: a `<<` in there is a shift, not a heredoc
    while i < n:
        c = cmd[i]
        if quote:
            if c == quote:
                quote = None
            elif c == "\\" and quote == '"':
                i += 1
        elif c in ("'", '"'):
            quote = c
        elif c == "\\":
            i += 1
        elif cmd.startswith("((", i):
            arith += 1
            i += 2
            continue
        elif arith and cmd.startswith("))", i):
            arith -= 1
            i += 2
            continue
        elif c == "#" and (i == 0 or cmd[i - 1] in " \t\n;|&("):  # after `)` bash keeps `#` in the word
            cut = i
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
        elif c in "\n;" or (c in "&|" and cmd[i + 1:i + 2] == c) or c == "|":
            segs.append(cmd[start:cut if cut is not None else i])
            cut = None
            i += 2 if (c in "&|" and cmd[i + 1:i + 2] == c) else 1
            if c == "\n":
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


def _words(seg: str) -> list:
    """The shell words of `seg` as bash reads quotes and backslashes, with the quotes removed: `"$AP2_RUN_DIR"/../x`
    is one word, `$AP2_RUN_DIR/../x`, and `"$AP2_"DATA` is `$AP2_DATA`."""
    words, text, quote, i = [], [], None, 0
    while i < len(seg):
        c = seg[i]
        if quote:
            if c == quote:
                quote = None
            elif c == "\\" and quote == '"' and i + 1 < len(seg):
                i += 1
                text.append(seg[i])
            else:
                text.append(c)
        elif c in ("'", '"'):
            quote = c
        elif c == "\\" and i + 1 < len(seg):
            i += 1
            text.append(seg[i])
        elif c in _BREAK:
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


def _after_first(piece: str, first, then) -> bool:
    m = first.search(piece)
    return bool(m) and bool(then.search(piece, m.end()))


def _writes(text: str) -> bool:
    """v1's `_WRITE.search(text)`, in linear time: `dd` with an `of=` after it and no `|`, `;` or `&` between, and
    `open(` with a `, 'w'` (or a, x, +) mode after it and no `)` between, are found piece by piece."""
    return bool(_WRITE.search(text)
                or any(_after_first(p, _DD, _OF) for p in re.split(r"[|;&]", text))
                or any(_after_first(p, _OPEN, _MODE) for p in text.split(")")))


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
        rest = _HARMLESS.sub(" ", seg)
        if (">" in rest) if (_runs_ap2(seg) and not isinstance(seg, _Body)) else _writes(rest):
            return _deny()
    return None


def _fallback(raw: bytes, env):
    """A payload too big or too deep to parse: denied when it is a v2 worker's Bash call naming a guarded path."""
    if not env.get("AP2_ROLE") or b'"Bash"' not in raw:
        return None
    # The text is JSON: undo its `\\` and `\"` escapes, so a quote-split `"$AP2_"DATA` reads as the shell's.
    text = raw.decode("utf-8", "replace").replace("\\\\", "\0").replace('\\"', '"').replace("\0", "\\")
    return _deny() if _Marks(env).names(text) else None


def main(argv: list, stdin=None) -> int:
    """`ap2 hook-guard`: the payload on stdin, the deny JSON (or nothing) on stdout, exit 0 always. Any argument is
    no opinion."""
    try:
        if argv or not os.environ.get("AP2_ROLE") or stdin is None:
            return 0
        raw = getattr(stdin, "buffer", stdin).read(MAX_STDIN + 1)
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if len(raw) > MAX_STDIN:
            out = _fallback(raw, os.environ)
        else:
            try:
                payload = json.loads(raw.decode("utf-8"))
            except RecursionError:
                out = _fallback(raw, os.environ)
            else:
                out = decide(payload, os.environ) if isinstance(payload, dict) else None
        if out:  # unbuffered: a closed stdout raises here, not again at interpreter exit
            os.write(1, (json.dumps(out) + "\n").encode())
    except Exception:  # noqa: BLE001 - a hook must never break the session
        pass
    return 0
