"""`ap2 hook-park`: the PreToolUse hook that parks a v2 worker at the usage line (design 8.1, section 12 workers).

`workers.floor()` wires it into every worker's settings, so it runs before each tool call of the worker and of its
subagents. While a usage window is at the park line (v1's `budget.parking` with `PARK_RULES`) it denies every tool
call except the worker's own `ap2 worker park <card> <role> [--note TEXT]`, and the deny reason names that command.
It acts only on a v2 worker (`AP2_ROLE` one of `ROLES`, `AP2_CARD` set): v1 workers, dispatchers and the user's own
sessions get no opinion, and neither does a worker whose own profile (`AP2_PROFILE`) is a gateway profile: the
gateway's daily cap (`cap.py`) holds it instead. It looks only at the 5-hour and weekly windows, not at extra
credits. It writes nothing and never breaks a session: any error is no output and exit 0.

It runs before every tool call, so it imports only what it needs; `workers.py`, `store.py` and `config.py` are too
heavy. `PARK_RULES` and `ROLES` are copies of `workers.PARK_RULES` and `workers.ROLES`, kept equal by a test.
"""
from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

from autopilot import budget as v1_budget

ROLES = ("implement", "review", "pr")  # == workers.ROLES
PARK_RULES = {"window_pct": 85, "stale_min": 60, "credit_pct": 90, "park_pct": 90,
              "resume_after_min": 5}  # == workers.PARK_RULES (v1's default budget rules)
MAX_STDIN = 1 << 20  # a payload past 1 MiB is not read: no opinion
PUNCT = set("();<>|&")  # == v1 park.PUNCT: a token made of these chains or redirects
MAX_COMMAND = 4096  # the park command is the package path, `worker park <card> <role>` and a one-line note


def ap2_bin() -> str:
    return str(Path(__file__).resolve().parent.parent.parent / "bin" / "ap2")


def _worker(env) -> tuple:
    """(card, role) of a v2 worker, else None."""
    role, card = env.get("AP2_ROLE"), env.get("AP2_CARD")
    if role not in ROLES or not card:
        return None
    return card, role


def _gateway(env) -> bool:
    """The worker's own profile (`AP2_PROFILE` in the data folder `AP2_DATA`) loads as a gateway profile. Read only
    at the park line, so the import stays off the common path. A profile that cannot be read is not a gateway one:
    the subscription's rule stays (parking stops work, it never spends more)."""
    data, name = env.get("AP2_DATA"), env.get("AP2_PROFILE")
    if not data or not name:
        return False
    try:
        from assistant import profiles
        return profiles.load(data, name).get("mode") == "gateway"
    except Exception:  # noqa: BLE001 - any error keeps the subscription's rule
        return False


def is_park_command(cmd: str, card: str, role: str) -> bool:
    """Exactly `<this package>/bin/ap2 worker park <card> <role> [--note TEXT]` on one line, the program quoted or
    not: the one command a parked worker may run. Anything chained to it, any `$` (command, parameter or arithmetic
    expansion), any `{` (brace expansion would turn one token into several words, such as `{x,--usage,/tmp/f}`),
    any other flag, or another card or role is not it (v1's `park.is_park_command` tokenizer rules). A `#` is a
    plain character, as bash reads one inside a word, so it cannot hide the rest of the line. A command past
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
    if toks[:5] != [ap2_bin(), "worker", "park", card, role]:
        return False
    rest = toks[5:]
    return rest == [] or (len(rest) == 2 and rest[0] == "--note")


def reason(p: dict, card: str, role: str, subagent: bool) -> str:
    where = f"({p.get('kind') or 'a'}{'/' + p['model'] if p.get('model') else ''} window) is at {p.get('percent')}%"
    if subagent:
        return (f"Usage {where}, so autopilot workers are parked. Stop now: make no more tool calls. Reply to your "
                "parent with exactly 'parked for quota' and one line on what you finished.")
    return (f"Usage {where}, so this autopilot worker must park until it resets. Run this one command: {ap2_bin()} "
            f"worker park {card} {role} --note \"<one line: where you are and the next step>\". Then end your turn "
            "with no further tool call. Do not work around this and do not use another tool.")


def decide(payload: dict, env, usage: dict, rules: dict = None):
    """The hook's verdict for one tool call: a PreToolUse deny, or None (no opinion). `usage` is what
    `autopilot.budget.read` returns; `rules` the budget rules (default `PARK_RULES`)."""
    who = _worker(env)
    if who is None:
        return None
    card, role = who
    p = v1_budget.parking(usage, {"budget": rules or PARK_RULES})
    if p is None:
        return None
    if _gateway(env):  # the subscription's windows never park a gateway worker (its cap is the daily cap's job)
        return None
    if payload.get("tool_name") == "Bash" and is_park_command((payload.get("tool_input") or {}).get("command") or "",
                                                              card, role):
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason(p, card, role, bool(payload.get("agent_id")))}}


def _read(stdin):
    """The payload on stdin, or None when stdin is closed, past MAX_STDIN, or not a JSON object. JSON nested too deep
    to parse is `{}`, not None, so it cannot fail open at the line."""
    if stdin is None:
        return None
    src = getattr(stdin, "buffer", stdin)
    raw = src.read(MAX_STDIN + 1)
    if len(raw) > MAX_STDIN:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        payload = json.loads(raw)
    except RecursionError:  # too deep to be the flat Bash park call; with no `tool_name` it is denied at the line
        return {}  # and still gets no opinion under it
    return payload if isinstance(payload, dict) else None


def main(argv: list, stdin=None) -> int:
    """`ap2 hook-park [--usage PATH]`: the payload on stdin, the deny JSON (or nothing) on stdout, exit 0 always.
    `--usage` is for tests (default `~/.claude.json`); any other argument is no opinion."""
    try:
        if _worker(os.environ) is None:
            return 0
        if argv == []:
            usage_path = Path.home() / ".claude.json"
        elif len(argv) == 2 and argv[0] == "--usage":
            usage_path = Path(argv[1])
        else:
            return 0
        payload = _read(stdin)
        if payload is None:
            return 0
        out = decide(payload, os.environ, v1_budget.read(usage_path, stale_min=PARK_RULES["stale_min"]), PARK_RULES)
        if out:  # unbuffered: a closed stdout raises here, not again at interpreter exit
            os.write(1, (json.dumps(out) + "\n").encode())
    except Exception:  # noqa: BLE001 - a hook must never break the session
        pass
    return 0
