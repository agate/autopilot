from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

from autopilot import cli, config, events, paths, sessions, state

AGENTS = {"implement": "autopilot:implementer", "review": "autopilot:reviewer"}
_BG = re.compile(r"backgrounded · (\w+)")  # a resumed copy prints no name after the id


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
    settings = paths.ap_dir(ap) / "settings.json"
    if not settings.exists():
        raise RuntimeError(f"{ap}: no permission floor yet; run `ap perms write {ap}` first")
    argv = [claude_bin(gcfg), "--bg", "-n", sessions.name_for(ap, task, role),
            "--permission-mode", gcfg["permission_mode"], "--settings", str(settings)]
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


def _saved_session(ap: str, task: str, role: str) -> str:
    t = next((t for t in state.load(ap)["tasks"] if t["id"] == task), {})
    return (t.get("sessions") or {}).get(role) or ""


def start(ap: str, task: str, role: str, cwd: str, prompt: str, needs: list = None, resume: bool = False) -> dict:
    """Start a worker. With resume, continue the task's saved session for this role (a fix round)."""
    gcfg, apcfg = config.load_global(), config.load_ap(ap)
    port_range = ports(gcfg["port_base"], next_slot(all_workers(), gcfg["port_base"]))
    prompt = (f"{prompt}\n\nYour port range: {port_range[0]}-{port_range[1]}. "
              f"The autopilot CLI: {paths.package_root() / 'bin' / 'ap'}.")
    argv, env = build_start(gcfg, apcfg, ap, task, role, prompt)
    sid = _saved_session(ap, task, role) if resume else ""
    if resume and not sid:
        raise RuntimeError(f"{ap} {task}: no saved {role} session to resume")
    if sid:
        # Flags on --resume start a copy under a new id; -n keeps the worker's name on that copy.
        argv = ([argv[0], "--bg", "--resume", sid, "-n", sessions.name_for(ap, task, role)]
                + argv[argv.index("--permission-mode"):argv.index("--settings") + 2])
        argv.append(prompt if role in AGENTS else "/autopilot:pr-loop " + prompt)
    short = _launch(argv, cwd, env)
    name = sessions.name_for(ap, task, role)
    listed = sessions.list_all(claude_bin(gcfg))
    found = sessions.find_by_id(short, listed) or sessions.find(name, listed)
    rec = {"task": task, "role": role, "session_name": name,
           "session_id": (found or {}).get("sessionId") or "", "short_id": short,
           "ports": port_range,
           "needs": list(needs or []), "started_at": events.now_iso(), "resumes": 0, "cwd": str(cwd)}
    with state.mutate(ap) as st:
        st["workers"] = [w for w in st["workers"] if not (w["task"] == task and w["role"] == role)] + [rec]
    events.append(ap, "worker_started", {"task": task, "role": role, "short_id": short})
    return rec


def resume(ap: str, worker: dict) -> dict:
    gcfg = config.load_global()
    prompt = ("Your session was interrupted. Continue the task from where you stopped; "
              "check your worktree and run files first.")
    argv = [claude_bin(gcfg), "--bg", "--resume", worker["session_id"], prompt]
    short = _launch(argv, worker.get("cwd") or str(paths.ap_dir(ap)), _env("worker"))
    with state.mutate(ap) as st:
        for w in st["workers"]:
            if w["task"] == worker["task"] and w["role"] == worker["role"]:
                w["short_id"] = short
                w["resumes"] = w.get("resumes", 0) + 1
                w["started_at"] = events.now_iso()
                worker = w
    events.append(ap, "worker_resumed", {"task": worker["task"], "role": worker["role"], "short_id": short})
    return worker


def end(ap: str, task: str, role: str) -> None:
    gcfg = config.load_global()
    """Stop a worker and keep its conversation on the task, so a fix round can resume it."""
    from autopilot import usage
    with state.mutate(ap) as st:
        keep, tokens = [], usage.zero()
        for w in st["workers"]:
            if w["task"] == task and w["role"] == role:
                tokens = usage.session_tokens(w.get("session_id"))
                subprocess.run([claude_bin(gcfg), "stop", w["short_id"]], capture_output=True, timeout=60)
                for t in st["tasks"]:
                    if t["id"] == task and w.get("session_id"):
                        t.setdefault("sessions", {})[role] = w["session_id"]
            else:
                keep.append(w)
        st["workers"] = keep
    events.append(ap, "worker_ended", {"task": task, "role": role, "tokens": tokens})


def cleanup(ap: str, task: str) -> list:
    """Delete every saved session of a finished task."""
    gcfg = config.load_global()
    removed = []
    with state.mutate(ap) as st:
        for t in st["tasks"]:
            if t["id"] == task:
                for sid in (t.pop("sessions", None) or {}).values():
                    subprocess.run([claude_bin(gcfg), "rm", sid], capture_output=True, timeout=60)
                    removed.append(sid)
    events.append(ap, "task_cleaned", {"task": task, "sessions": len(removed)})
    return removed


def listing(ap: str) -> list:
    gcfg = config.load_global()
    all_s = sessions.list_all(claude_bin(gcfg))
    runs = paths.ap_dir(ap) / "runs"
    return [dict(w, state=sessions.classify(w, all_s, runs / w["task"])) for w in state.load(ap)["workers"]]


def _opt(args: list, flag: str, default=None):
    if flag in args:
        i = args.index(flag)
        return args[i + 1]
    return default


@cli.command("worker")
def _cmd(args: list) -> int:
    if args[:1] == ["list"] and len(args) >= 2:
        rows = listing(args[1])
        if "--json" in args:
            import json
            print(json.dumps(rows, indent=1, ensure_ascii=False))
        else:
            for w in rows:
                print(f"{w['session_name']}: {w['state']} (resumes {w['resumes']})")
        return 0
    if len(args) < 4:
        print("usage: ap worker start|resume|end <ap> <task> <role> [--cwd P --prompt-file F --needs a,b --resume]"
              " | ap worker list <ap> [--json]", file=sys.stderr)
        return 2
    sub, ap, task, role = args[:4]
    if sub == "start":
        prompt = open(_opt(args, "--prompt-file")).read()
        needs = [n for n in (_opt(args, "--needs", "") or "").split(",") if n]
        rec = start(ap, task, role, _opt(args, "--cwd", os.getcwd()), prompt, needs, resume="--resume" in args)
        print(rec["session_name"], rec["short_id"], rec["ports"])
    elif sub == "resume":
        w = next(w for w in state.load(ap)["workers"] if w["task"] == task and w["role"] == role)
        print(resume(ap, w)["short_id"])
    elif sub == "end":
        end(ap, task, role)
    else:
        return 2
    return 0
