from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import datetime

from autopilot import cli, config, events, paths, state, workers

# Outward actions, matched case-insensitively on the tool's own name (after mcp__<server>__).
DENY_TOOL_PATTERNS = [re.compile(p, re.I) for p in (
    r"create[_-]?(jira[_-]?)?issue", r"add[_-]?(issue[_-]?)?comment|addcomment|create[_-]?comment|post[_-]?comment",
    r"(^|[_-])reply", r"send[_-]?(message|email|mail)|post[_-]?message", r"schedule[_-]?message", r"canvas",
    r"create[_-]?(confluence[_-]?)?pages?\b|createconfluencepage|(^|[_-])create-pages|update[_-]?(confluence[_-]?)?page",
)]
PROTECTED = ("RULES.md", "config.json", "settings*.json", "oneoff-*.json", "state.json", "page.json", "gate.json",
             "tick.json", "lock", "state.lock")
ONEOFF_TIMEOUT_S = 600
BASH_DENY = ["Bash(gh pr comment:*)", "Bash(gh pr review:*)", "Bash(gh issue comment:*)", "Bash(gh issue create:*)",
             "Bash(gh pr edit --add-reviewer:*)"]
# The ticket-status tool, matched on its own name: a dispatcher moves its tickets (start, review, done).
STATUS_TOOL_PATTERNS = [re.compile(r"(^|[_-])transition[_-]?(jira[_-]?)?issues?$", re.I)]


def denied_tools(tools: list) -> list:
    return [t for t in tools if t.startswith("mcp__") and any(p.search(t.split("__")[-1]) for p in DENY_TOOL_PATTERNS)]


def status_tools(tools: list) -> list:
    return [t for t in tools
            if t.startswith("mcp__") and any(p.search(t.split("__")[-1]) for p in STATUS_TOOL_PATTERNS)]


def allow_rules(tools: list, authority: dict, role: str) -> list:
    """Steps the dispatcher takes on every pass, allowed by rule so the auto-mode classifier never judges
    them: a pass is a fresh session and its prompt names no ticket, PR or worker, so the classifier blocks
    a close, a ready-for-review or a stop as unrequested. Workers get none: they never take these steps.
    Each `authority` key can only take a rule away."""
    if role != "dispatcher":
        return []
    out = status_tools(tools) if authority.get("source_status", True) else []
    if authority.get("open_prs", True):
        out.append("Bash(gh pr ready:*)")
    # Stops only the sessions this autopilot's state records; never a raw `claude stop`.
    out.append(f"Bash({paths.package_root() / 'bin' / 'ap'} worker end:*)")
    return out


def deny_rules(data_root: str, package_root: str) -> list:
    """File rules use `//` (absolute) or `~/` (home) paths: a bare path would be read relative to the
    session's working folder, which is a repo or worktree, never the data folder. Only Edit(path) rules:
    Claude Code applies them to every file-editing tool (Write included) and follows symlinks both ways."""
    out = []
    for name in PROTECTED:
        out += [f"Edit({data_root}/**/{name})", f"Edit({data_root}/{name})"]
    out += [f"Edit({package_root}/**)"]
    return out + BASH_DENY


def generate(tools: list, exclude: set = frozenset(), role: str = "worker", authority: dict = None) -> dict:
    deny = [t for t in denied_tools(tools) if t not in exclude]
    deny += deny_rules("/" + str(paths.data_root()), "/" + str(paths.package_root()))
    perms = {"deny": deny}
    allow = [a for a in allow_rules(tools, authority or {}, role) if a not in deny]
    if allow:
        perms["allow"] = allow
    return {"permissions": perms,
            "env": {"AUTOPILOT_ROLE": role, "AUTOPILOT_DATA": str(paths.data_root())}}


def shared_authority() -> dict:
    """The dispatcher floor is shared by every autopilot, so a key one autopilot turns off is off for all."""
    out = {}
    for n in config.list_aps():
        try:
            auth = config.load_ap(n).get("authority") or {}
        except Exception:  # noqa: BLE001 - a broken config grants nothing
            auth = {"source_status": False, "open_prs": False}
        for k in ("source_status", "open_prs"):
            out[k] = out.get(k, True) and auth.get(k, True) is not False
    return out


def static_settings(role: str) -> dict:
    """The fallback shipped in the package: home-relative paths, no user-specific values."""
    return {"permissions": {"deny": deny_rules("~/.claude/autopilot-data", "~/.claude/skills/autopilot")},
            "env": {"AUTOPILOT_ROLE": role}}


def parse_discovery(stream: str) -> list:
    for line in stream.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") == "system" and d.get("subtype") == "init":
            return d.get("tools") or []
    raise RuntimeError("tool discovery got no init event (is claude logged in?)")


tools_from_stream = parse_discovery


def discover_tools(claude_bin: str, cwd: str) -> list:
    out = subprocess.run([claude_bin, "-p", "Reply with the single word OK.", "--output-format", "stream-json",
                          "--verbose"], cwd=cwd, capture_output=True, text=True, timeout=180,
                         stdin=subprocess.DEVNULL)
    return parse_discovery(out.stdout)


def _write(path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + "\n")


def write_for(ap: str) -> list:
    """Discover the tools a session in this autopilot's repo has and write its permission floors."""
    gcfg, apcfg = config.load_global(), config.load_ap(ap)
    tools = discover_tools(workers.claude_bin(gcfg), apcfg["repo"])
    _write(paths.ap_dir(ap) / "settings.json", generate(tools, role="worker"))
    disp = paths.data_root() / "settings-dispatcher.json"
    known = set()
    if disp.exists():
        cur = json.loads(disp.read_text())["permissions"]
        known = {t for t in cur["deny"] + cur.get("allow", []) if t.startswith("mcp__")}
    _write(disp, generate(sorted(known | set(denied_tools(tools)) | set(status_tools(tools))), role="dispatcher",
                          authority=shared_authority()))
    return denied_tools(tools)


def one_off_settings(ap: str, tool: str):
    cur = json.loads((paths.ap_dir(ap) / "settings.json").read_text()).get("permissions", {}).get("deny", [])
    path = paths.ap_dir(ap) / f"oneoff-{re.sub(r'[^A-Za-z0-9_-]', '_', tool)}.json"
    deny = [t for t in cur if t != tool] or generate([], role="oneoff")["permissions"]["deny"]
    _write(path, {"permissions": {"deny": deny}, "env": {"AUTOPILOT_ROLE": "oneoff",
                                                           "AUTOPILOT_DATA": str(paths.data_root())}})
    return path


def run_one_off(ap: str, decision_id: str) -> str:
    """Carry out exactly one approved outward action, once, with only that one tool allowed."""
    st = state.load(ap)
    d = next((d for d in st["decisions"] if d["id"] == decision_id), None)
    action = (d or {}).get("action") or {}
    if d is None or not action.get("tool") or not action.get("prompt") or not action.get("option"):
        raise ValueError(f"{decision_id} has no complete action (tool, option, prompt)")
    if d["status"] != "answered" or d.get("answer") != action["option"]:
        raise ValueError(f"{decision_id} was not answered with {action['option']!r}")
    if action.get("done_at"):
        raise ValueError(f"{decision_id} already ran at {action['done_at']}")
    since = _epoch(action.get("running_since"))
    if since and time.time() - since < ONEOFF_TIMEOUT_S + 60:
        raise ValueError(f"{decision_id} is running since {action['running_since']}")
    gcfg, apcfg = config.load_global(), config.load_ap(ap)
    settings = one_off_settings(ap, action["tool"])
    _set_action(ap, decision_id, running_since=events.now_iso())
    # `--allowedTools` takes a list: give it one value with `=` and end the options with `--`, or it
    # reads the prompt as a second tool and claude exits asking for a prompt.
    argv = [workers.claude_bin(gcfg), "-p", "--permission-mode", "dontAsk", "--settings", str(settings),
            f"--allowedTools={action['tool']}", "--", action["prompt"]]
    try:
        out = subprocess.run(argv, cwd=apcfg["repo"], capture_output=True, text=True, timeout=ONEOFF_TIMEOUT_S,
                             stdin=subprocess.DEVNULL, env=workers._env("oneoff"))
        code, stdout, stderr = out.returncode, out.stdout, out.stderr
    except subprocess.TimeoutExpired as e:
        code, stdout, stderr = -1, _text(e.stdout), _text(e.stderr) + f"\ntimed out after {ONEOFF_TIMEOUT_S}s"
    events.append(ap, "oneoff_done", {"decision": decision_id, "tool": action["tool"], "exit": code,
                                      "output": stdout[-1500:], "error": stderr[-1500:]})
    if code != 0:
        # Not done: the card stays answered, so the next pass (or a talk session) can run it again.
        _set_action(ap, decision_id, running_since=None, failed_at=events.now_iso())
        raise RuntimeError(f"{decision_id}: claude exited {code}: {(stderr or stdout).strip()[-500:]}")
    _set_action(ap, decision_id, running_since=None, done_at=events.now_iso())
    return stdout


def _set_action(ap: str, decision_id: str, **fields) -> None:
    with state.mutate(ap) as cur:
        for x in cur["decisions"]:
            if x["id"] == decision_id:
                for k, v in fields.items():
                    if v is None:
                        x["action"].pop(k, None)
                    else:
                        x["action"][k] = v


def _text(b) -> str:
    return b.decode(errors="replace") if isinstance(b, bytes) else (b or "")


def _epoch(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


@cli.command("perms")
def _perms(args: list) -> int:
    if args[:1] == ["write"] and len(args) == 2:
        print("\n".join(write_for(args[1])) or "(no outward tools found)")
    elif args[:1] == ["one-off"] and len(args) == 3:
        print(one_off_settings(args[1], args[2]))
    else:
        print("usage: ap perms write <ap> | ap perms one-off <ap> <tool>", file=sys.stderr)
        return 2
    return 0


@cli.command("oneoff")
def _oneoff(args: list) -> int:
    try:
        print(run_one_off(args[0], args[1]))
    except (ValueError, RuntimeError) as e:
        print(f"ap oneoff: {e}", file=sys.stderr)
        return 1
    return 0
