from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import datetime

from autopilot import cli, config, events, paths, profile as profiles, spend, state, workers

# Outward actions, matched case-insensitively on the tool's own name (after mcp__<server>__).
DENY_TOOL_PATTERNS = [re.compile(p, re.I) for p in (
    r"create[_-]?(jira[_-]?)?issue", r"add[_-]?(issue[_-]?)?comment|addcomment|create[_-]?comment|post[_-]?comment",
    r"(^|[_-])reply", r"send[_-]?(message|email|mail)|post[_-]?message", r"schedule[_-]?message", r"canvas",
    r"create[_-]?(confluence[_-]?)?pages?\b|createconfluencepage|(^|[_-])create-pages|update[_-]?(confluence[_-]?)?page",
    r"(^|[_-])(add|edit|create|post)[a-z_-]*comment", r"(create|update)[_-]?confluence[_-]?content",
    # Generic tools that send any API request: the write and delete ones can post, edit or remove anything.
    r"^execute[_-]?(write|destructive)$",
)]
PROTECTED = ("RULES.md", "config.json", "settings*.json", "oneoff-*.json", "state.json", "page.json", "gate.json",
             "tick.json", "lock", "state.lock", "access.json")
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
    a close, a ready-for-review, a merge or a stop as unrequested. Workers get none: they never take these
    steps. `source_status` and `open_prs` are on unless set to false; the merge rule needs `merge: true`."""
    if role == "worker":
        # The park command is the one step a parking worker takes; the auto-mode classifier must not judge it.
        return [f"Bash({paths.package_root() / 'bin' / 'ap'} worker park:*)"]
    if role != "dispatcher":
        return []
    out = status_tools(tools) if authority.get("source_status", True) else []
    if authority.get("open_prs", True):
        out.append("Bash(gh pr ready:*)")
    if authority.get("merge") is True:
        # The pass merges only after `ap merge-check` passes; the classifier sees neither that nor this authority.
        # The prefix makes every allowed merge carry the head-SHA guard, so it merges only the checked head.
        out.append("Bash(gh pr merge --match-head-commit:*)")
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


def park_hook() -> dict:
    """Workers park themselves at the usage line. A hook, not a request: it runs before every tool call, in the
    worker and in its subagents, and denies all but the one `ap worker park` command while usage is high."""
    return {"PreToolUse": [{"matcher": "*", "hooks": [
        {"type": "command", "command": f"\"{paths.package_root() / 'bin' / 'ap'}\" hook-park", "timeout": 5}]}]}


def generate(tools: list, exclude: set = frozenset(), role: str = "worker", authority: dict = None,
             profile: dict = None) -> dict:
    deny = [t for t in denied_tools(tools) if t not in exclude]
    deny += deny_rules("/" + str(paths.data_root()), "/" + str(paths.package_root()))
    perms = {"deny": deny}
    allow = [a for a in allow_rules(tools, authority or {}, role) if a not in deny]
    if allow:
        perms["allow"] = allow
    out = {"permissions": perms,
           "env": {"AUTOPILOT_ROLE": role, "AUTOPILOT_DATA": str(paths.data_root())}}
    if role == "worker":
        out["hooks"] = park_hook()
    if profile is not None:
        out = profiles.merge(out, profiles.overlay(profile))
    return out


def shared_authority() -> dict:
    """The dispatcher floor is shared by every autopilot, so a key one autopilot turns off is off for all.
    Merging is granted only when every autopilot sets `merge: true`."""
    out = {}
    for n in config.list_aps():
        try:
            auth = config.load_ap(n).get("authority") or {}
        except Exception:  # noqa: BLE001 - a broken config grants nothing
            auth = {"source_status": False, "open_prs": False, "merge": False}
        for k in ("source_status", "open_prs"):
            out[k] = out.get(k, True) and auth.get(k, True) is not False
        out["merge"] = out.get("merge", True) and auth.get("merge") is True
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


def servers_from_stream(stream: str) -> list:
    for line in stream.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") == "system" and d.get("subtype") == "init":
            return [s.get("name") for s in d.get("mcp_servers") or [] if isinstance(s, dict) and s.get("name")]
    return []


def session_from_stream(stream: str) -> str:
    for line in stream.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("type") == "system" and d.get("subtype") == "init":
            return str(d.get("session_id") or "")
    return ""


def discover(claude_bin: str, cwd: str, settings, ap=None, profile=None) -> tuple:
    """The tools and the MCP server names a session in `cwd` starts with, under the profile in `settings`.
    A `-p` run has no short id: its spend is recorded by session id (`profile` is the profile it ran under)."""
    out = subprocess.run([claude_bin, "-p", "Reply with the single word OK.", "--output-format", "stream-json",
                          "--verbose", "--settings", str(settings)], cwd=cwd, capture_output=True, text=True, timeout=180,
                         stdin=subprocess.DEVNULL)
    if profile is not None:
        try:
            spend.record_session(session_from_stream(out.stdout), "discover", ap, profile)
        except Exception:  # noqa: BLE001 - spend bookkeeping must not fail discovery
            pass
    return parse_discovery(out.stdout), servers_from_stream(out.stdout)


def discover_tools(claude_bin: str, cwd: str, settings, ap=None, profile=None) -> list:
    return discover(claude_bin, cwd, settings, ap, profile)[0]


def tool_prefix(server: str) -> str:
    """The `mcp__<prefix>__` Claude Code gives a server's tools."""
    return "mcp__" + re.sub(r"[^A-Za-z0-9_-]", "_", server) + "__"


def _write(path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + "\n")


def write_for(ap: str) -> list:
    """Discover the tools a session in this autopilot's repo has and write its permission floors."""
    gcfg, apcfg = config.load_global(), config.load_ap(ap)
    # Discovery runs under the profile a worker will run under (a gateway has no claude.ai tools), but without
    # the floor's deny list: a denied tool must still show up, or the next write would drop its deny rule.
    act, page = profiles.active(), profiles.page_profile()
    probe = paths.ap_dir(ap) / "settings-discover.json"
    _write(probe, profiles.overlay(act))
    tools, found = discover(workers.claude_bin(gcfg), apcfg["repo"], probe, ap, act)
    # The dispatcher runs on the page profile (the subscription), so its floor comes from what that profile sees:
    # under a gateway the first probe has no claude.ai connector tools, and they must still be denied.
    ptools, pfound = tools, found
    if act["name"] != page["name"]:
        pprobe = paths.ap_dir(ap) / "settings-discover-page.json"
        _write(pprobe, profiles.overlay(page))
        ptools, pfound = discover(workers.claude_bin(gcfg), apcfg["repo"], pprobe, ap, page)
    _write(paths.ap_dir(ap) / "settings.json", generate(tools, role="worker", profile=profiles.active()))
    disp = paths.data_root() / "settings-dispatcher.json"
    known = set()
    if disp.exists():
        cur = json.loads(disp.read_text())["permissions"]
        known = {t for t in cur["deny"] + cur.get("allow", []) if t.startswith("mcp__")}
    floor = generate(sorted(known | set(denied_tools(ptools)) | set(status_tools(ptools))), role="dispatcher",
                     authority=shared_authority(), profile=profiles.page_profile())
    _write(disp, floor)
    # The servers whose tools the dispatcher floor allows are the ones a pass needs: their access is checked.
    from autopilot import access
    used = [s for s in pfound if any(a.startswith(tool_prefix(s)) for a in status_tools(ptools))]
    access.record_servers(ap, used)
    return list(dict.fromkeys(denied_tools(tools) + denied_tools(ptools)))


def one_off_settings(ap: str, tool: str):
    cur = json.loads((paths.ap_dir(ap) / "settings.json").read_text()).get("permissions", {}).get("deny", [])
    path = paths.ap_dir(ap) / f"oneoff-{re.sub(r'[^A-Za-z0-9_-]', '_', tool)}.json"
    deny = [t for t in cur if t != tool] or generate([], role="oneoff")["permissions"]["deny"]
    floor = {"permissions": {"deny": deny}, "env": {"AUTOPILOT_ROLE": "oneoff",
                                                      "AUTOPILOT_DATA": str(paths.data_root())}}
    _write(path, profiles.merge(floor, profiles.overlay(profiles.active())))
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
            "--output-format", "json", f"--allowedTools={action['tool']}", "--", action["prompt"]]
    try:
        out = subprocess.run(argv, cwd=apcfg["repo"], capture_output=True, text=True, timeout=ONEOFF_TIMEOUT_S,
                             stdin=subprocess.DEVNULL, env=workers._env("oneoff"))
        code, stdout, stderr = out.returncode, out.stdout, out.stderr
        stdout, sid = _json_result(stdout)
    except subprocess.TimeoutExpired as e:
        code, stdout, stderr = -1, _text(e.stdout), _text(e.stderr) + f"\ntimed out after {ONEOFF_TIMEOUT_S}s"
        sid = ""
    try:
        spend.record_session(sid, "oneoff", ap, profiles.active())
    except Exception:  # noqa: BLE001 - spend bookkeeping must not fail an approved action
        pass
    events.append(ap, "oneoff_done", {"decision": decision_id, "tool": action["tool"], "exit": code,
                                      "output": stdout[-1500:], "error": stderr[-1500:]})
    if code != 0:
        # Not done: the card stays answered, so the next pass (or a talk session) can run it again.
        _set_action(ap, decision_id, running_since=None, failed_at=events.now_iso())
        raise RuntimeError(f"{decision_id}: claude exited {code}: {(stderr or stdout).strip()[-500:]}")
    _set_action(ap, decision_id, running_since=None, done_at=events.now_iso())
    return stdout


def _json_result(stdout: str) -> tuple:
    """(the reply text, the session id) of a `--output-format json` run; the raw text and "" when it is not JSON."""
    try:
        d = json.loads(stdout)
    except ValueError:
        return stdout, ""
    if not isinstance(d, dict):
        return stdout, ""
    sid = str(d.get("session_id") or "")
    if d.get("result"):
        return str(d["result"]), sid
    # An error run (error_max_turns, error_during_execution) has no result: keep what it did say.
    return f"{d.get('subtype') or 'no result'}: {stdout.strip()}", sid


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
