from __future__ import annotations

import json
import os
import re
import sys

from autopilot import cli

_WRITE = re.compile(r"(>>?|\btee\b|\bsed\s+(-[a-zA-Z]*\s+)*-i|\bperl\s+-[a-zA-Z]*i|\bcp\b|\bmv\b|\brm\b|\btruncate\b|"
                    r"\bln\b|\binstall\b)[^;&|]*RULES\.md")
REASON = "RULES.md changes only through a talk session after the user says yes"


def decide(payload: dict, env) -> dict:
    if not env.get("AUTOPILOT_ROLE") or payload.get("tool_name") != "Bash":
        return None
    cmd = (payload.get("tool_input") or {}).get("command") or ""
    if not _WRITE.search(cmd):
        return None
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
