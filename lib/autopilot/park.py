from __future__ import annotations

import json
import os
import shlex
import sys
from datetime import datetime, timezone

from autopilot import budget, cli, config, paths

# A park hook runs before every tool call of every worker, so this module imports only what it needs.
PUNCT = set("();<>|&")
MAX_COMMAND = 4096  # the park command is the package path, `worker park <ap> <task> <role>` and a one-line note


def _ap_bin() -> str:
    return str(paths.package_root() / "bin" / "ap")


def is_park_command(cmd: str) -> bool:
    """Exactly `<ap> worker park <ap> <task> <role> [--note TEXT]` on one line: the one command a parking
    worker may run. Anything chained to it, any `$` (command, parameter or arithmetic expansion) and any `{`
    (brace expansion would turn one token into several words, such as `{implement,--data=x}`) is not it. A `#`
    is a plain character, as bash reads one inside a word, so it cannot hide the rest of the line. A command past
    `MAX_COMMAND` is refused before it is tokenized: `shlex` is quadratic on one long token, and a hook that runs
    past its timeout lets the call through."""
    if not cmd or len(cmd) > MAX_COMMAND or "\n" in cmd or "`" in cmd or "$" in cmd or "{" in cmd:
        return False
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        lex.commenters = ""
        toks = list(lex)
    except ValueError:
        return False
    if any(t and set(t) <= PUNCT for t in toks):
        return False
    return len(toks) >= 6 and toks[0] == _ap_bin() and toks[1:3] == ["worker", "park"]


def reason(p: dict, subagent: bool) -> str:
    where = f"({p['kind'] or 'a'}{'/' + p['model'] if p.get('model') else ''} window) is at {p['percent']}%"
    if subagent:
        return (f"Usage {where}, so autopilot workers are parked. Stop now: make no more tool calls. Reply to "
                "your parent with exactly 'parked for quota' and one line on what you finished.")
    return (f"Usage {where}, so this autopilot worker must park until it resets. Run this one command, with your "
            f"autopilot name, task id and role from your brief: {_ap_bin()} worker park <ap> <task> <role> "
            "--note \"<one line: where you are and the next step>\". Then end your turn with no further tool "
            "call. Do not work around this and do not use another tool.")


def decide(payload: dict, env, b: dict = None, cfg: dict = None):
    """Deny every tool call of a worker (and its subagents) while a usage window is at the park line, except
    the one `ap worker park` command. None = no opinion."""
    if env.get("AUTOPILOT_ROLE") != "worker":
        return None
    cfg = cfg or config.load_global()
    p = budget.parking(b if b is not None else budget.read_fast(stale_min=cfg["budget"]["stale_min"]), cfg)
    if p is None:
        return None
    if payload.get("tool_name") == "Bash" and is_park_command((payload.get("tool_input") or {}).get("command") or ""):
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason(p, bool(payload.get("agent_id")))}}


def resume_due(worker: dict, now: datetime, cfg: dict, b: dict) -> bool:
    """A parked worker can continue: its resume time has passed, no window is at the line, and the budget
    allows new work. `b` is `budget.read()`."""
    pk = worker.get("parked")
    if not pk:
        return False
    try:
        at = datetime.fromisoformat(pk["resume_at"])
    except (KeyError, TypeError, ValueError):
        at = None  # no usable resume time: wait for the numbers to drop
    if at is not None and (at.tzinfo is None or now < at):
        return False
    return budget.parking(b, cfg) is None and budget.allowance(b, cfg, now)["allow"] != 0


@cli.command("hook-park")
def _hook(args: list) -> int:
    try:
        out = decide(json.loads(sys.stdin.read()), os.environ)
        if out:
            print(json.dumps(out))
    except Exception:  # a hook must never break the session
        pass
    return 0
