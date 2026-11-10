"""System access: the logins and connections every pass depends on.

Checks, in this order: GitHub (`gh auth status`), the page host's login (`claude auth status`), each MCP
server the passes use (`claude mcp get <name>`), and any check an autopilot declares in its config
(`health_checks`: a shell command that exits 0 when access works). The gate runs one check before each
pass, the one checked longest ago, with a short timeout; no check ever starts a login. A broken access
opens one `system` card per check, and a later check that sees it working again resolves the card.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

from autopilot import cli, config, events, ops, paths, state, text, workers

TIMEOUT_S = 20
NAMES = {"gh": "GitHub (gh)", "page": "claude.ai login", "artifact": "Artifact tools",
         "page_refresh": "Page refresh"}


def _file(ap: str):
    return paths.ap_dir(ap) / "access.json"


def _load(ap: str) -> dict:
    try:
        data = json.loads(_file(ap).read_text())
    except (OSError, ValueError):
        data = {}
    data.setdefault("checks", {})
    data.setdefault("servers", [])
    return data


def _save(ap: str, data: dict) -> None:
    tmp = _file(ap).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    os.replace(tmp, _file(ap))


def record_servers(ap: str, names: list) -> None:
    """The MCP servers the passes use, found by `ap perms write`; kept when a later discovery misses one."""
    data = _load(ap)
    data["servers"] = sorted(set(data["servers"]) | set(names))
    _save(ap, data)


def servers(ap: str) -> list:
    return _load(ap)["servers"]


def _gh(gcfg: dict) -> str:
    return gcfg["tools"].get("gh") or "gh"


def checks(ap: str) -> list:
    gcfg, cfg = config.load_global(), config.load_ap(ap)
    claude, repo = workers.claude_bin(gcfg), cfg.get("repo")
    out = [{"id": "gh", "name": NAMES["gh"], "argv": [_gh(gcfg), "auth", "status"], "cwd": repo, "judge": "exit",
            "fix": "gh auth login", "blocks": {"key": "blocks_gh"}}]
    if gcfg.get("page_host") == "claude.ai":
        out.append({"id": "page", "name": NAMES["page"], "argv": [claude, "auth", "status", "--json"], "cwd": repo,
                    "judge": "claude_auth", "fix": "claude auth login", "blocks": {"key": "blocks_page"}})
    names = servers(ap) + [s for s in cfg.get("mcp_servers") or [] if isinstance(s, str)]
    for name in dict.fromkeys(names):
        out.append({"id": f"mcp:{name}", "name": f"MCP {name}", "argv": [claude, "mcp", "get", name], "cwd": repo,
                    "judge": "mcp", "fix": f"/mcp → {name} → Authenticate",
                    "blocks": {"key": "blocks_mcp", "args": {"name": name}}})
    for c in cfg.get("health_checks") or []:
        if not (isinstance(c, dict) and isinstance(c.get("name"), str) and isinstance(c.get("command"), str)):
            continue
        out.append({"id": f"check:{c['name']}", "name": c["name"], "argv": ["/bin/sh", "-c", c["command"]],
                    "cwd": repo, "judge": "exit", "fix": c.get("fix") or c["command"],
                    "blocks": c.get("blocks") or {"key": "blocks_check", "args": {"name": c["name"]}}})
    return out


def _tail(s: str, n: int = 300) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s[-n:]


def judge(c: dict, out: subprocess.CompletedProcess) -> tuple:
    """(ok, error, fix) for one finished check."""
    if c["judge"] == "mcp":
        line = next((l.split(":", 1)[1].strip() for l in (out.stdout or "").splitlines()
                     if l.strip().startswith("Status:")), "")
        if out.returncode == 0 and "Connected" in line and not line.startswith("!"):
            return True, "", ""
        err = line or _tail(out.stderr or out.stdout) or f"exit {out.returncode}"
        if "authentication" in err.lower():
            return False, err, c["fix"]
        return False, err, f"claude mcp get {c['argv'][-1]}"
    if c["judge"] == "claude_auth":
        try:
            st = json.loads(out.stdout)
        except ValueError:
            return False, _tail(out.stderr or out.stdout) or f"exit {out.returncode}", c["fix"]
        if isinstance(st, dict) and st.get("loggedIn") is True and st.get("authMethod") == "claude.ai":
            return True, "", ""
        got = st if isinstance(st, dict) else {}
        return False, f"loggedIn={got.get('loggedIn')}, authMethod={got.get('authMethod')}", c["fix"]
    if out.returncode == 0:
        return True, "", ""
    return False, _tail(out.stderr or out.stdout) or f"exit {out.returncode}", c["fix"]


def run_check(c: dict) -> tuple:
    try:
        out = subprocess.run(c["argv"], cwd=c.get("cwd") or None, capture_output=True, text=True,
                             timeout=TIMEOUT_S, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, f"timed out after {TIMEOUT_S}s", c["fix"]
    except OSError as e:
        return False, str(e), c["fix"]
    ok, err, fix = judge(c, out)
    if not ok and c["judge"] == "exit" and not err.endswith(f"exit {out.returncode}"):
        err = f"{err} (exit {out.returncode})"
    return ok, err, fix


def run_due(aps: list):
    """Run the one check, across these autopilots, that was checked longest ago (never-run first)."""
    best = None
    for ap in aps:
        try:
            done_at = _load(ap)["checks"]
            cs = checks(ap)
        except (OSError, ValueError, FileNotFoundError, state.StateError):
            continue
        for c in cs:
            at = (done_at.get(c["id"]) or {}).get("at") or ""
            if best is None or at < best[0]:
                best = (at, ap, c)
    if best is None:
        return None
    _, ap, c = best
    ok, err, fix = run_check(c)
    record(ap, c["id"], ok, err, fix)
    return {"ap": ap, "check": c["id"], "ok": ok}


def _notify(msg: str) -> None:
    from autopilot import gate
    gate.notify(msg)


def _texts(t, langs: set) -> dict:
    if isinstance(t, str):
        return {lang: t for lang in langs}
    return t


def _bad_texts(info) -> list:
    """Errors for `ap access fail`: error and fix are a string or text in every reader language; blocks may
    also be a label reference, as its default is."""
    if not isinstance(info, dict):
        return ["the argument must be a JSON object"]
    out = []
    for f in ("error", "fix", "blocks"):
        v = info.get(f)
        if v is None or isinstance(v, str):
            continue
        if not isinstance(v, dict):
            out.append(f"{f}: must be a string or text in each language, got {type(v).__name__}")
        elif "key" in v and f != "blocks":
            out.append(f"{f}: a label reference is not accepted here")
        else:
            out += text.check(v, state.langs(), f)
    return out


def _info(ap: str, check: str) -> dict:
    try:
        return next((c for c in checks(ap) if c["id"] == check), {})
    except (OSError, ValueError, FileNotFoundError):
        return {}


def record(ap: str, check: str, ok: bool, error: str = "", fix: str = "", blocks=None) -> None:
    """Record one result; open, update or resolve the check's system card."""
    now = events.now_iso()
    data = _load(ap)
    prev = data["checks"].get(check) or {}
    if ok:
        data["checks"][check] = {"ok": True, "at": now}
    else:
        since = prev.get("since") if prev.get("ok") is False else now
        data["checks"][check] = {"ok": False, "at": now, "since": since or now, "error": error}
    _save(ap, data)
    card = next((d for d in state.load(ap)["decisions"] if d["kind"] == "system" and d["status"] == "open"
                 and (d.get("access") or {}).get("check") == check), None)
    if ok:
        if card is None:
            return
        with state.mutate(ap) as st:
            for d in st["decisions"]:
                if d["id"] == card["id"]:
                    d.update(status="resolved", answer="restored", answered_at=now)
                    d["access"]["restored_at"] = now
        events.append(ap, "access_restored", {"check": check, "card": card["id"], "since": card["access"]["since"],
                                              "restored_at": now})
        return
    body = {"key": "access_body", "args": {"error": error or "-"}}
    if card is not None:
        with state.mutate(ap) as st:
            for d in st["decisions"]:
                if d["id"] == card["id"]:
                    d["body"] = body
                    d["access"].update(error=error, last_checked=now, fix=fix or d["access"]["fix"])
        return
    info = _info(ap, check)
    name = info.get("name") or NAMES.get(check) or check.split(":", 1)[-1]
    did = ops.decision_add(ap, {
        "kind": "system", "title": {"key": "access_broken", "args": {"name": name}}, "body": body,
        "options": [], "links": [],
        "access": {"check": check, "since": data["checks"][check]["since"], "last_checked": now, "error": error,
                   "fix": fix or info.get("fix") or "-",
                   "blocks": _texts(blocks or info.get("blocks") or {"key": "blocks_check", "args": {"name": name}},
                                    state.langs())}})
    events.append(ap, "access_broken", {"check": check, "card": did, "error": error, "fix": fix})
    _notify(f"{name} is not working for autopilot {ap}. See the page.")


@cli.command("access")
def _cmd(args: list) -> int:
    if args[:1] == ["check"] and len(args) == 2:
        print(json.dumps(run_due([args[1]])))
        return 0
    if args[:1] == ["check"] and len(args) == 3:
        c = next((c for c in checks(args[1]) if c["id"] == args[2]), None)
        if c is None:
            print(f"ap access: {args[1]} has no check {args[2]!r} (see ap access list {args[1]})", file=sys.stderr)
            return 2
        ok, err, fix = run_check(c)
        record(args[1], c["id"], ok, err, fix)
        print(json.dumps({"ap": args[1], "check": c["id"], "ok": ok, "error": err, "fix": fix}, ensure_ascii=False))
        return 0 if ok else 1
    if args[:1] == ["fail"] and len(args) == 4:
        try:
            info = json.loads(args[3])
        except ValueError as e:
            info, bad = None, [f"the argument is not JSON ({e})"]
        else:
            bad = _bad_texts(info)
        if bad:
            print(f"ap access fail: {'; '.join(bad)} (each of error, fix and blocks is a string or "
                  "{\"en\": ..., \"<lang>\": ...})", file=sys.stderr)
            return 2
        events.append(args[1], "access_failure", dict(info, check=args[2]))
        record(args[1], args[2], False, info.get("error") or "", info.get("fix") or "", info.get("blocks"))
        return 0
    if args[:1] == ["ok"] and len(args) == 3:
        record(args[1], args[2], True)
        return 0
    if args[:1] == ["list"] and len(args) == 2:
        print(json.dumps([{k: c[k] for k in ("id", "name", "fix")} for c in checks(args[1])], ensure_ascii=False))
        return 0
    print("usage: ap access check <ap> [<check>] | ap access fail <ap> <check> '{\"error\": ..., \"fix\": ...}'"
          " | ap access ok <ap> <check> | ap access list <ap>", file=sys.stderr)
    return 2
