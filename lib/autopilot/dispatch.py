from __future__ import annotations

import json
import subprocess
import time

from autopilot import config, lock, ops, paths, sessions, workers

NAME = "autopilot · dispatch"


def groups(active: list) -> dict:
    out = {}
    for n in active:
        out.setdefault(config.load_ap(n)["repo"], []).append(n)
    return {k: sorted(v) for k, v in sorted(out.items())}


def build_command(gcfg: dict, repo: str, aps: list) -> tuple:
    runbook = paths.package_root() / "RUNBOOK.md"
    prompt = (f"Run one autopilot dispatcher pass for: {', '.join(aps)}. "
              f"Read {runbook} first and follow it exactly.")
    settings = paths.data_root() / "settings-dispatcher.json"
    if not settings.exists():
        settings = paths.package_root() / "settings" / "dispatcher.json"
    argv = [workers.claude_bin(gcfg), "--bg", "-n", NAME, "--permission-mode", gcfg["permission_mode"],
            "--settings", str(settings)]
    if gcfg["models"].get("dispatcher"):
        argv += ["--model", gcfg["models"]["dispatcher"]]
    argv.append(prompt)
    env = workers._env("dispatcher")
    return argv, env


def _last_pass() -> dict:
    try:
        return json.loads((paths.data_root() / "last_pass.json").read_text())
    except (OSError, ValueError):
        return {}


def _cleanup(claude: str, short: str) -> None:
    for sub in ("stop", "rm"):
        subprocess.run([claude, sub, short], capture_output=True, timeout=60)


def run(gcfg: dict, active: list, poll_s: float = 10, timeout_s: float = None) -> list:
    timeout_s = gcfg["dispatch_timeout_s"] if timeout_s is None else timeout_s
    claude = workers.claude_bin(gcfg)
    results = []
    for repo, aps in groups(active).items():
        lk = paths.data_root() / "lock"
        if not lock.acquire(lk, f"dispatch {','.join(aps)}"):
            results.append({"skipped": "locked"})
            continue
        try:
            ops.pass_begin()
            argv, env = build_command(gcfg, repo, aps)
            short = workers._launch(argv, repo, env)
            began = time.monotonic()
            deadline = began + timeout_s
            timed_out, seen_busy = True, False
            while True:
                s = sessions.find(NAME, sessions.list_all(claude))
                if s is not None and s.get("status") == "busy":
                    seen_busy = True
                finished = s is not None and (s.get("state") == "done" or s.get("status") == "idle")
                # A fresh session can report idle before it picks up its prompt: wait for busy or 3 polls.
                if finished and (seen_busy or time.monotonic() - began >= 3 * poll_s):
                    timed_out = False
                    break
                if time.monotonic() >= deadline:
                    break
                time.sleep(poll_s)
            _cleanup(claude, (s or {}).get("id") or short)
            res = _last_pass()
            res["repo"] = repo
            res["aps"] = aps
            if timed_out:
                res["timeout"] = True
            results.append(res)
        finally:
            lock.release(lk)
    return results
