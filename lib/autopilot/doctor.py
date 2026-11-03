from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from autopilot import checkpkg, cli, config, paths

PLIST = Path.home() / "Library" / "LaunchAgents" / "com.autopilot.gate.plist"
LABEL = "com.autopilot.gate"


def _run(argv: list, timeout: int = 30) -> tuple:
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return out.returncode, (out.stdout + out.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


def _c(name, ok, detail="", fix="") -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail, "fix": fix}


def checks(gcfg: dict) -> list:
    out = []
    tools = gcfg.get("tools") or {}
    for t in ("claude", "gh", "git"):
        p = tools.get(t)
        ok = bool(p) and os.access(p, os.X_OK)
        out.append(_c(f"tool {t}", ok, p or "not recorded",
                      "" if ok else "run `ap install` from your own terminal so the gate knows where the tools are"))
    rc, msg = _run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"])
    out.append(_c("launchd job", PLIST.exists() and rc == 0, str(PLIST) if PLIST.exists() else "not installed",
                  "" if rc == 0 else "run the command that `ap install --print` shows, in your own terminal"))
    gate = {}
    try:
        gate = json.loads((paths.data_root() / "gate.json").read_text())
    except (OSError, ValueError):
        pass
    last = gate.get("last_run_at")
    fresh = False
    if last:
        try:
            fresh = datetime.now().astimezone() - datetime.fromisoformat(last) < timedelta(seconds=3 * gcfg["gate_interval_s"])
        except ValueError:
            fresh = False
    out.append(_c("gate ran recently", fresh, last or "never", "" if fresh else "check gate.log in the data folder"))
    for name, err in (gate.get("broken") or {}).items():
        out.append(_c(f"state {name}", False, err, f"fix or restore {paths.ap_dir(name) / 'state.json'}"))
    if tools.get("claude"):
        rc, msg = _run([tools["claude"], "auth", "status"])
        out.append(_c("claude login", rc == 0, msg.splitlines()[0] if msg else "", "" if rc == 0 else "run `claude auth login`"))
    if tools.get("gh"):
        rc, msg = _run([tools["gh"], "auth", "status"])
        out.append(_c("gh login", rc == 0, "", "" if rc == 0 else "run `gh auth login`"))
    files = [paths.package_root() / "settings" / "dispatcher.json", paths.package_root() / "settings" / "worker.json",
             paths.data_root() / "settings-dispatcher.json"] + [paths.ap_dir(n) / "settings.json" for n in config.list_aps()]
    bad = []
    for f in files:
        if f.exists():
            try:
                json.loads(f.read_text())
            except ValueError:
                bad.append(str(f))
    out.append(_c("settings files valid", not bad, ", ".join(bad),
                  "" if not bad else "headless sessions silently ignore an invalid settings file: fix the JSON"))
    active = len(config.active_aps())
    out.append(_c("worker cap", gcfg["max_workers"] >= active, f"cap {gcfg['max_workers']}, active {active}",
                  "" if gcfg["max_workers"] >= active else "raise max_workers or pause an autopilot"))
    sub = os.environ.get("CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS")
    out.append(_c("subagents per session", True, sub or "Claude Code default"))
    leaks = checkpkg.scan(paths.package_root(), checkpkg.identities())
    out.append(_c("package names nobody", not leaks, f"{len(leaks)} finding(s)",
                  "" if not leaks else "run `ap check-package` and remove what it lists"))
    return out


@cli.command("doctor")
def _cmd(args: list) -> int:
    res = checks(config.load_global())
    for c in res:
        mark = "ok  " if c["ok"] else "FAIL"
        print(f"{mark} {c['name']}: {c['detail']}" + (f"\n     fix: {c['fix']}" if c["fix"] else ""))
    return 0 if all(c["ok"] for c in res) else 1
