from __future__ import annotations

import json
import os
import re
import sys

from autopilot import cli, paths

# Shell forms that write a file. Three forms that scan ahead, `sed ... -i`, `dd ... of=` and `open(..., 'w')`, are
# not in this one regex but in `_writes`: as one regex they are quadratic on a long run of `dd ` or `open(`.
_WRITE = re.compile(r"(>>?|\btee\b|\bperl\s+-[a-zA-Z]*i|\bcp\b|\bmv\b|\brm\b|\btruncate\b|"
                    r"\bln\b|\binstall\b|\.write\(|write_text|\bchmod\b|\btouch\b)")
_SED = re.compile(r"\bsed\s")
_IN_PLACE = re.compile(r"-[a-zA-Z]*[iI]|--in-place")  # a sed word that edits its files in place
_DD, _OF = re.compile(r"\bdd\b"), re.compile(r"\bof=")
_OPEN, _MODE = re.compile(r"\bopen\("), re.compile(r",\s*['\"][wax+]")
_SPLIT = re.compile(r"\n|;|&&|\|\||\|")
# Redirects that write nowhere: a descriptor copy (2>&1) or output thrown away (>/dev/null, &>/dev/null). A digit
# run is read once, whole, and kept when no redirect follows it (`_harmless`), so a long run of digits stays linear.
_HARMLESS = re.compile(r"\d+(?:>&\d+|>>?\s*/dev/null)?|>&\d+|&>>?\s*/dev/null|>>?\s*/dev/null")
# What a second reading of each segment takes out: a line continuation, a `$` before a quote (`$"..."`, `$'...'`),
# then any quote or backslash, so `c""p`, `'rm'` and `~/.claude/"autopilot-data"` read as bash runs them.
_STRIP = re.compile(r"\\\n|\$(?=[\"'])|[\"'\\]")
_ESCAPE = re.compile(r"\\(.)", re.S)  # a backslash and the one character it escapes
# A command past this is not split and matched (a hook past its timeout lets the call through): it is denied when
# it names a guarded path anywhere, as written or with `_STRIP` applied.
MAX_COMMAND = 64 << 10
REASON = ("autopilot data and code change only through `ap` commands, or through a talk session after the "
          "user says yes")


def _markers() -> list:
    return [str(paths.data_root()), str(paths.package_root()), "autopilot-data/", "$AUTOPILOT_DATA",
            "${AUTOPILOT_DATA}", "~/.claude/skills/autopilot"]


def _is_ap(segment: str) -> bool:
    first = segment.strip().split(" ", 1)[0].strip("\"'")
    return first == "ap" or first.endswith("/bin/ap")


def _after_first(piece: str, first, then) -> bool:
    m = first.search(piece)
    return bool(m) and bool(then.search(piece, m.end()))


def _sed_in_place(text: str) -> bool:
    """A `sed` with an in-place flag (`_IN_PLACE`: `-i`, `-i.bak`, `-ni`, `--in-place`) as any blank-separated word
    after it in the segment (fail-closed: a quoted word that only looks like a flag counts)."""
    m = _SED.search(text)
    return bool(m) and any(_IN_PLACE.match(t) for t in text[m.end():].split())


def _opens_to_write(text: str) -> bool:
    """An `open(` with a `, 'w'` (or a, x, +) mode after it and no `)` between, found piece by piece."""
    return any(_after_first(p, _OPEN, _MODE) for p in text.split(")"))


def _writes(text: str) -> bool:
    """Whether a segment writes a file, in linear time: `_WRITE`, then `sed` with an in-place flag after it, `dd`
    with an `of=` after it and no `|`, `;` or `&` between, and `_opens_to_write`, each found piece by piece."""
    return bool(_WRITE.search(text) or _sed_in_place(text)
                or any(_after_first(p, _DD, _OF) for p in re.split(r"[|;&]", text))
                or _opens_to_write(text))


def _join(cmd: str) -> str:
    """`cmd` with each line continuation taken out: a `\\` + newline whose backslash is not itself escaped, read
    pair by pair from the left (`\\\\` + newline is a literal backslash, and the newline still ends the line)."""
    return _ESCAPE.sub(lambda m: "" if m.group(1) == "\n" else m.group(0), cmd)


def _harmless(text: str) -> str:
    """`text` with each `_HARMLESS` redirect replaced by a space (a digit run with no redirect after it stays)."""
    return _HARMLESS.sub(lambda m: m.group(0) if m.group(0).isdigit() else " ", text)


def _denies(text: str, marks: list) -> bool:
    return any(m in text for m in marks) and _writes(_harmless(text))


def decide(payload: dict, env) -> dict:
    """Deny shell writes into the data folder or the package from dispatcher, worker and one-off sessions.
    Each segment is read as written, and again with its quotes and backslashes taken out (`_STRIP`, on the lines
    both as split and with line continuations joined; an `open(..., 'w')` mode is still matched as written), so a
    guarded path or a write verb split or escaped by them (`"$AUTOPILOT_"DATA`, `~/.claude/autopilot\\-data`,
    `c""p`, `'rm'`) is still found. A command longer than `MAX_COMMAND` is not split: it is denied when it names
    a guarded path anywhere.
    Best effort: it reads the command text, so a path built at run time (`$(...)`, a variable set earlier in the
    command, a script given to an interpreter) can get past it; the Edit/Write deny rules are the main guard."""
    if not env.get("AUTOPILOT_ROLE") or payload.get("tool_name") != "Bash":
        return None
    cmd = (payload.get("tool_input") or {}).get("command") or ""
    marks = _markers()
    plain = [_STRIP.sub("", m) for m in marks]
    if len(cmd) > MAX_COMMAND:
        stripped = _STRIP.sub("", _join(cmd))
        named = any(m in cmd for m in marks) or any(m in stripped for m in plain)
        return _deny() if named else None
    for seg in _SPLIT.split(cmd):  # as written: v1's reading before quotes were taken out
        if seg.strip() and not _is_ap(seg) and _denies(seg, marks):
            return _deny()
    # bash may or may not join a `\` + newline (not after a `#`), so the lines are read both split and joined
    for seg in _SPLIT.split(cmd) + _SPLIT.split(_join(cmd)):
        if not seg.strip() or _is_ap(seg):
            continue
        stripped = _STRIP.sub("", seg)
        # `_STRIP` takes out the quotes an open mode needs, so that one form is matched on the segment as written
        if any(m in stripped for m in plain) and (_writes(_harmless(stripped)) or _opens_to_write(seg)):
            return _deny()
    return None


def _deny() -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": REASON}}


@cli.command("hook-guard")
def _hook(args: list) -> int:
    try:
        out = decide(json.loads(sys.stdin.read()), os.environ)
        if out:
            print(json.dumps(out))
    except Exception:  # a hook must never break the session
        pass
    return 0
