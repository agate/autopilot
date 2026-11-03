from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

from autopilot import cli, config, events, paths, sessions, state

AGENTS = {"implement": "autopilot:implementer", "review": "autopilot:reviewer"}
_BG = re.compile(r"backgrounded · (\w+) · ")


def claude_bin(gcfg: dict) -> str:
    return gcfg["tools"].get("claude") or shutil.which("claude") or "claude"


def ports(base: int, k: int) -> list:
    return [base + 10 * k, base + 10 * k + 9]


def next_slot(all_workers: list, base: int) -> int:
    used = {(w["ports"][0] - base) // 10 for w in all_workers if w.get("ports")}
    k = 0
    while k in used:
        k += 1
    return k


def _env(role: str) -> dict:
    env = dict(os.environ)
    env["AUTOPILOT_ROLE"] = role
    env["AUTOPILOT_DATA"] = str(paths.data_root())
    return env


def build_start(gcfg: dict, apcfg: dict, ap: str, task: str, role: str, prompt: str) -> tuple:
    argv = [claude_bin(gcfg), "--bg", "-n", sessions.name_for(ap, task, role),
            "--permission-mode", gcfg["permission_mode"],
            "--settings", str(paths.ap_dir(ap) / "settings.json")]
    if role in AGENTS:
        argv += ["--agent", AGENTS[role]]
    else:
        prompt = "/autopilot:pr-loop " + prompt
    if gcfg["models"].get("worker"):
        argv += ["--model", gcfg["models"]["worker"]]
    if gcfg.get("worker_effort"):
        argv += ["--effort", gcfg["worker_effort"]]
    argv.append(prompt)
    return argv, _env("worker")


def all_workers() -> list:
    out = []
    for n in config.list_aps():
        try:
            out += state.load(n)["workers"]
        except state.StateError:
            continue
    return out


def _launch(argv: list, cwd: str, env: dict) -> str:
    out = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=120,
                         stdin=subprocess.DEVNULL)
    m = _BG.search(out.stdout + out.stderr)
    if not m:
        raise RuntimeError(f"could not start session: {out.stdout.strip()} {out.stderr.strip()}")
    return m.group(1)


def start(ap: str, task: str, role: str, cwd: str, prompt: str, needs: list = None) -> dict:
    gcfg, apcfg = config.load_global(), config.load_ap(ap)
    argv, env = build_start(gcfg, apcfg, ap, task, role, prompt)
    short = _launch(argv, cwd, env)
    name = sessions.name_for(ap, task, role)
    found = sessions.find(name, sessions.list_all(claude_bin(gcfg)))
    rec = {"task": task, "role": role, "session_name": name,
           "session_id": (found or {}).get("sessionId") or "", "short_id": short,
           "ports": ports(gcfg["port_base"], next_slot(all_workers(), gcfg["port_base"])),
           "needs": list(needs or []), "started_at": events.now_iso(), "resumes": 0}
    st = state.load(ap)
    st["workers"] = [w for w in st["workers"] if not (w["task"] == task and w["role"] == role)] + [rec]
    state.save(ap, st)
    events.append(ap, "worker_started", {"task": task, "role": role, "short_id": short})
    return rec


def resume(ap: str, worker: dict) -> dict:
    gcfg = config.load_global()
    prompt = ("Your session was interrupted. Continue the task from where you stopped; "
              "check your worktree and run files first.")
    argv = [claude_bin(gcfg), "--bg", "--resume", worker["session_id"], prompt]
    short = _launch(argv, paths.ap_dir(ap), _env("worker"))
    st = state.load(ap)
    for w in st["workers"]:
        if w["task"] == worker["task"] and w["role"] == worker["role"]:
            w["short_id"] = short
            w["resumes"] = w.get("resumes", 0) + 1
            w["started_at"] = events.now_iso()
            worker = w
    state.save(ap, st)
    events.append(ap, "worker_resumed", {"task": worker["task"], "role": worker["role"], "short_id": short})
    return worker


def end(ap: str, task: str, role: str) -> None:
    gcfg = config.load_global()
    st = state.load(ap)
    keep = []
    for w in st["workers"]:
        if w["task"] == task and w["role"] == role:
            for sub in ("stop", "rm"):
                subprocess.run([claude_bin(gcfg), sub, w["short_id"]], capture_output=True, timeout=60)
        else:
            keep.append(w)
    st["workers"] = keep
    state.save(ap, st)
    events.append(ap, "worker_ended", {"task": task, "role": role})


def _opt(args: list, flag: str, default=None):
    if flag in args:
        i = args.index(flag)
        return args[i + 1]
    return default


@cli.command("worker")
def _cmd(args: list) -> int:
    if len(args) < 4:
        print("usage: ap worker start|resume|end <ap> <task> <role> [--cwd P --prompt-file F --needs a,b]",
              file=sys.stderr)
        return 2
    sub, ap, task, role = args[:4]
    if sub == "start":
        prompt = open(_opt(args, "--prompt-file")).read()
        needs = [n for n in (_opt(args, "--needs", "") or "").split(",") if n]
        rec = start(ap, task, role, _opt(args, "--cwd", os.getcwd()), prompt, needs)
        print(rec["session_name"], rec["short_id"], rec["ports"])
    elif sub == "resume":
        w = next(w for w in state.load(ap)["workers"] if w["task"] == task and w["role"] == role)
        print(resume(ap, w)["short_id"])
    elif sub == "end":
        end(ap, task, role)
    else:
        return 2
    return 0
