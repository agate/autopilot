from __future__ import annotations

import json
import subprocess
import time

from autopilot import access, config, lock, ops, paths, profile, sessions, spend, workers

NAME = "autopilot · dispatch"


def groups(active: list) -> dict:
    out = {}
    for n in active:
        try:
            repo = config.load_ap(n).get("repo")
        except Exception:  # noqa: BLE001 - the gate already marked it broken
            continue
        if repo:
            out.setdefault(repo, []).append(n)
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
    s = profile.page_profile()
    if s.get("dispatcher_model") or s.get("model"):
        argv += ["--model", s.get("dispatcher_model") or s["model"]]
    argv.append(prompt)
    env = workers._env("dispatcher")
    return argv, env


def _last_pass() -> dict:
    try:
        return json.loads((paths.data_root() / "last_pass.json").read_text())
    except (OSError, ValueError):
        return {}


def _cleanup(claude: str, short: str, remove: bool = True) -> None:
    for sub in (("stop", "rm") if remove else ("stop",)):
        if sub == "rm":
            workers._settle(short)
        subprocess.run([claude, sub, short], capture_output=True, timeout=60)


def _live_dispatcher(claude: str):
    """A dispatcher session that is still working, from this or any earlier pass."""
    s = sessions.find(NAME, sessions.list_all(claude) or [])
    if s and s.get("status") in ("busy", "waiting") and sessions.pid_alive(s.get("pid")):
        return s
    return None


def run(gcfg: dict, active: list, poll_s: float = 10, timeout_s: float = None) -> list:
    """One pass per repo, one after another, under the global lock. Never two dispatchers at once."""
    timeout_s = gcfg["dispatch_timeout_s"] if timeout_s is None else timeout_s
    claude = workers.claude_bin(gcfg)
    results = []
    for repo, aps in groups(active).items():
        lk = paths.data_root() / "lock"
        if not lock.acquire(lk, f"dispatch {','.join(aps)}"):
            results.append({"skipped": "locked", "aps": aps})
            continue
        short, blocked = None, False
        try:
            if _live_dispatcher(claude):
                results.append({"skipped": "dispatcher_running", "aps": aps})
                continue
            ops.pass_begin()
            try:
                access.run_due(aps)  # one system-access check per pass; its card is on the page this pass
            except Exception:  # noqa: BLE001 - a broken check must never stop the pass
                pass
            argv, env = build_command(gcfg, repo, aps)
            short = workers._launch(argv, repo, env)
            workers._record(short, "dispatcher", None, profile.page_profile())
            began = time.monotonic()
            deadline = began + timeout_s
            timed_out, seen_busy = True, False
            while True:
                s = sessions.find(NAME, sessions.list_all(claude) or [])
                if s is not None and s.get("id"):
                    short = s["id"]
                # `ap pass end` is the pass's own end marker (pass_begin cleared it), so it wins over the label.
                if "ended_at" in _last_pass():
                    timed_out = False
                    break
                # Claude Code also labels a finished session "blocked" when its last message asks the user
                # something; only status "waiting" means it is stopped at a prompt.
                if s is not None and s.get("state") == "blocked" and s.get("status") == "waiting":
                    blocked, timed_out = True, False
                    break
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
            res = _last_pass()
            res.update(repo=repo, aps=aps)
            if timed_out:
                res["timeout"] = True
            if blocked:
                res["blocked"] = True
            results.append(res)
        except Exception as e:  # noqa: BLE001 - one repo failing must not stop the next
            results.append({"error": f"{type(e).__name__}: {e}", "repo": repo, "aps": aps})
        finally:
            if short:
                _cleanup(claude, short, remove=not blocked)
            lock.release(lk)
    return results
