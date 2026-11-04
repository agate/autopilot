from __future__ import annotations

import json
import re
import subprocess
import sys

from autopilot import cli, config, events, paths, state, workers

# Outward actions, matched case-insensitively on the tool's own name (after mcp__<server>__).
DENY_TOOL_PATTERNS = [re.compile(p, re.I) for p in (
    r"create[_-]?(jira[_-]?)?issue", r"add[_-]?(issue[_-]?)?comment|addcomment|create[_-]?comment|post[_-]?comment",
    r"(^|[_-])reply", r"send[_-]?(message|email|mail)|post[_-]?message", r"schedule[_-]?message", r"canvas",
    r"create[_-]?(confluence[_-]?)?pages?\b|createconfluencepage|(^|[_-])create-pages|update[_-]?(confluence[_-]?)?page",
)]
PROTECTED = ("RULES.md", "config.json", "settings*.json", "oneoff-*.json", "state.json", "page.json", "gate.json",
             "tick.json", "lock", "state.lock")
BASH_DENY = ["Bash(gh pr comment:*)", "Bash(gh pr review:*)", "Bash(gh issue comment:*)", "Bash(gh issue create:*)",
             "Bash(gh pr edit --add-reviewer:*)"]


def denied_tools(tools: list) -> list:
    return [t for t in tools if t.startswith("mcp__") and any(p.search(t.split("__")[-1]) for p in DENY_TOOL_PATTERNS)]


def deny_rules(data_root: str, package_root: str) -> list:
    """File rules use `//` (absolute) or `~/` (home) paths: a bare path would be read relative to the
    session's working folder, which is a repo or worktree, never the data folder. Only Edit(path) rules:
    Claude Code applies them to every file-editing tool (Write included) and follows symlinks both ways."""
    out = []
    for name in PROTECTED:
        out += [f"Edit({data_root}/**/{name})", f"Edit({data_root}/{name})"]
    out += [f"Edit({package_root}/**)"]
    return out + BASH_DENY


def generate(tools: list, exclude: set = frozenset(), role: str = "worker") -> dict:
    deny = [t for t in denied_tools(tools) if t not in exclude]
    deny += deny_rules("/" + str(paths.data_root()), "/" + str(paths.package_root()))
    return {"permissions": {"deny": deny},
            "env": {"AUTOPILOT_ROLE": role, "AUTOPILOT_DATA": str(paths.data_root())}}


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
        known = {t for t in json.loads(disp.read_text())["permissions"]["deny"] if t.startswith("mcp__")}
    _write(disp, generate(sorted(known | set(denied_tools(tools))), role="dispatcher"))
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
    gcfg, apcfg = config.load_global(), config.load_ap(ap)
    settings = one_off_settings(ap, action["tool"])
    with state.mutate(ap) as cur:
        for x in cur["decisions"]:
            if x["id"] == decision_id:
                x["action"]["done_at"] = events.now_iso()
    out = subprocess.run([workers.claude_bin(gcfg), "-p", "--permission-mode", "dontAsk",
                          "--settings", str(settings), "--allowedTools", action["tool"], action["prompt"]],
                         cwd=apcfg["repo"], capture_output=True, text=True, timeout=600,
                         stdin=subprocess.DEVNULL, env=workers._env("oneoff"))
    events.append(ap, "oneoff_done", {"decision": decision_id, "tool": action["tool"],
                                      "exit": out.returncode, "output": out.stdout[-1500:]})
    return out.stdout


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
    print(run_one_off(args[0], args[1]))
    return 0
