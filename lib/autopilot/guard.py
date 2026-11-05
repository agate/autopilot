from __future__ import annotations

import json
import os
import re
import sys

from autopilot import cli, paths

_WRITE = re.compile(r"(>>?|\btee\b|\bsed\s+(-[a-zA-Z]*\s+)*-i|\bperl\s+-[a-zA-Z]*i|\bcp\b|\bmv\b|\brm\b|\btruncate\b|"
                    r"\bln\b|\binstall\b|\bdd\b[^|;&]*\bof=|\bopen\([^)]*,\s*['\"][wax+]|\.write\(|write_text|"
                    r"\bchmod\b|\btouch\b)")
_SPLIT = re.compile(r"\n|;|&&|\|\||\|")
# Redirects that write nowhere: a descriptor copy (2>&1) or output thrown away (>/dev/null, &>/dev/null).
_HARMLESS = re.compile(r"\d*>&\d+|&>>?\s*/dev/null|\d*>>?\s*/dev/null")
REASON = ("autopilot data and code change only through `ap` commands, or through a talk session after the "
          "user says yes")


def _markers() -> list:
    return [str(paths.data_root()), str(paths.package_root()), "autopilot-data/", "$AUTOPILOT_DATA",
            "${AUTOPILOT_DATA}", "~/.claude/skills/autopilot"]


def _is_ap(segment: str) -> bool:
    first = segment.strip().split(" ", 1)[0].strip("\"'")
    return first == "ap" or first.endswith("/bin/ap")


def decide(payload: dict, env) -> dict:
    """Deny shell writes into the data folder or the package from dispatcher, worker and one-off sessions.
    Best effort: it reads the command text, so a path built at run time can get past it; the Edit/Write
    deny rules are the main guard."""
    if not env.get("AUTOPILOT_ROLE") or payload.get("tool_name") != "Bash":
        return None
    cmd = (payload.get("tool_input") or {}).get("command") or ""
    marks = _markers()
    for seg in _SPLIT.split(cmd):
        if not seg.strip() or _is_ap(seg):
            continue
        if any(m in seg for m in marks) and _WRITE.search(_HARMLESS.sub(" ", seg)):
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": REASON}}
    return None


@cli.command("hook-guard")
def _hook(args: list) -> int:
    try:
        out = decide(json.loads(sys.stdin.read()), os.environ)
        if out:
            print(json.dumps(out))
    except Exception:  # a hook must never break the session
        pass
    return 0
