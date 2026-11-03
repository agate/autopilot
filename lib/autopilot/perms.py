from __future__ import annotations

import json
import re
import subprocess
import sys

from autopilot import cli, config, events, paths, state, workers

DENY_TOOL_PATTERNS = [re.compile(p) for p in (
    r"createJiraIssue", r"addCommentToJiraIssue", r"(^|_)send_message", r"schedule_message",
    r"create_canvas", r"update_canvas",
)]
DENY_RULES = [
    "Edit(**/autopilot-data/**/RULES.md)", "Write(**/autopilot-data/**/RULES.md)",
    "Edit(**/autopilot-data/RULES.md)", "Write(**/autopilot-data/RULES.md)",
    "Bash(gh pr comment:*)", "Bash(gh pr review:*)", "Bash(gh issue comment:*)",
    "Bash(gh pr edit --add-reviewer:*)",
]


def denied_tools(tools: list) -> list:
    return [t for t in tools if t.startswith("mcp__") and any(p.search(t) for p in DENY_TOOL_PATTERNS)]


def generate(tools: list, exclude: set = frozenset()) -> dict:
    deny = [t for t in denied_tools(tools) if t not in exclude] + DENY_RULES
    return {"permissions": {"deny": deny}}


def tools_from_stream(stream: str) -> list:
    for line in stream.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") == "system" and d.get("subtype") == "init":
            return d.get("tools") or []
    return []


def discover_tools(claude_bin: str, cwd: str) -> list:
    out = subprocess.run([claude_bin, "-p", "Reply with the single word OK.", "--output-format", "stream-json",
                          "--verbose"], cwd=cwd, capture_output=True, text=True, timeout=180,
                         stdin=subprocess.DEVNULL)
    return tools_from_stream(out.stdout)


def _write(path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + "\n")


def write_for(ap: str) -> list:
    """Discover the tools a session in this autopilot's repo has and write its permission floors."""
    gcfg, apcfg = config.load_global(), config.load_ap(ap)
    tools = discover_tools(workers.claude_bin(gcfg), apcfg["repo"])
    _write(paths.ap_dir(ap) / "settings.json", generate(tools))
    disp = paths.data_root() / "settings-dispatcher.json"
    known = set()
    if disp.exists():
        known = {t for t in json.loads(disp.read_text())["permissions"]["deny"] if t.startswith("mcp__")}
    _write(disp, generate(sorted(known | set(denied_tools(tools)))))
    return denied_tools(tools)


def one_off_settings(ap: str, tool: str):
    cur = json.loads((paths.ap_dir(ap) / "settings.json").read_text())["permissions"]["deny"]
    path = paths.ap_dir(ap) / f"oneoff-{re.sub(r'[^A-Za-z0-9_-]', '_', tool)}.json"
    _write(path, {"permissions": {"deny": [t for t in cur if t != tool]}})
    return path


def run_one_off(ap: str, decision_id: str) -> str:
    """Carry out exactly one approved outward action, with only that one tool unlocked."""
    st = state.load(ap)
    d = next(d for d in st["decisions"] if d["id"] == decision_id)
    action = d.get("action") or {}
    if d["status"] != "answered" or not action.get("tool") or not action.get("prompt"):
        raise ValueError(f"{decision_id} is not an answered decision with an action")
    gcfg, apcfg = config.load_global(), config.load_ap(ap)
    settings = one_off_settings(ap, action["tool"])
    out = subprocess.run([workers.claude_bin(gcfg), "-p", "--permission-mode", gcfg["permission_mode"],
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
