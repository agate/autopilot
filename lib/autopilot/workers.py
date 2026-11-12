from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from autopilot import budget, cli, config, events, paths, profile, sessions, shellenv, spend, state, usage

AGENTS = {"implement": "autopilot:implementer", "review": "autopilot:reviewer"}
PROTECTED_ENV = ("AUTOPILOT_", "ANTHROPIC_", "CLAUDE")
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
    p = profile.launch_profile(settings)   # the profile the file was written for; refused when out of date
    if p.get("model"):
        argv += ["--model", p["model"]]
    if p.get("effort"):
        argv += ["--effort", p["effort"]]
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
    # The session gets the environment of its own folder, as the user's terminal builds it (shellenv). What
    # decides who the session is and who pays for it stays the gate's: the autopilot's own variables, and any
    # Anthropic or Claude Code setting a shell profile exports for scripts (an API key would move billing).
    if shellenv.enabled():
        project, why = shellenv.read(cwd)
        if project is not None:
            keep = {k: v for k, v in env.items() if k.startswith(PROTECTED_ENV)}
            env = {**env, **{k: v for k, v in project.items() if not k.startswith(PROTECTED_ENV)}, **keep}
        elif why != shellenv.UNSUPPORTED:
            print(f"autopilot: could not read the shell environment of {cwd} ({why}); "
                  "the session keeps the current one", file=sys.stderr)
    out = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=120,
                         stdin=subprocess.DEVNULL)
    m = _BG.search(out.stdout + out.stderr)
    if not m:
        raise RuntimeError(f"could not start session: {out.stdout.strip()} {out.stderr.strip()}")
    return m.group(1)


def _settle(short: str) -> None:
    """Price a finished session. Never raises: pricing must not stop a cleanup. A failure is logged."""
    try:
        spend.settle(short)
    except Exception as e:  # noqa: BLE001
        _spend_failed("settle", short, None, None, None, e)


def _record(short: str, kind: str, ap, p: dict) -> None:
    try:
        spend.record_launch(short, kind, ap, p)
    except Exception as e:  # noqa: BLE001 - spend bookkeeping must not undo a started session
        _spend_failed("record", short, kind, ap, p, e)


def _spend_failed(op: str, short, kind, ap, p, err: Exception) -> None:
    """Log a spend bookkeeping failure. A gateway launch that could not be recorded gets a launch row that never
    matches a session, so the totals show unknown cost (which holds the daily cap) instead of $0."""
    try:
        events.append_global("spend_record_failed", {"op": op, "short": short, "kind": kind, "ap": ap,
                                                     "error": f"{type(err).__name__}: {err}"[:300]})
    except Exception:  # noqa: BLE001 - nothing more to do
        pass
    if op == "record" and (p or {}).get("mode") == "gateway":
        try:
            spend.record_session("", kind if kind in spend.KINDS else "worker", ap, p)
        except Exception:  # noqa: BLE001
            pass


BLOCKING = ("cap", "key", "profile")


def check_allowed() -> None:
    """Raise RuntimeError (and launch nothing) when the budget allows no new worker for a reason only the user
    can clear: the gateway's daily cap, a failing key, or an active profile that cannot be loaded. Every worker
    launch and every one-off action calls this, whatever the pass decided."""
    w = budget.current(config.load_global(), datetime.now(timezone.utc))["workers"]
    if w.get("allow") != 0 or w.get("reason") not in BLOCKING:
        return
    why = {"cap": f"the gateway's daily cap is reached ({w.get('cause') or 'spent'}); answer the cap card first",
           "key": f"the gateway key does not work: {w.get('error')}. {profile.KEY_FIX}",
           "profile": f"{w.get('error')}. {profile.PROFILE_FIX}"}[w["reason"]]
    raise RuntimeError(f"no session started: {why}")


def _saved_session(ap: str, task: str, role: str) -> str:
    t = next((t for t in state.load(ap)["tasks"] if t["id"] == task), {})
    return (t.get("sessions") or {}).get(role) or ""


def start(ap: str, task: str, role: str, cwd: str, prompt: str, needs: list = None, resume: bool = False) -> dict:
    """Start a worker. With resume, continue the task's saved session for this role (a fix round)."""
    check_allowed()
    gcfg, apcfg = config.load_global(), config.load_ap(ap)
    port_range = ports(gcfg["port_base"], next_slot(all_workers(), gcfg["port_base"]))
    old = next((w for w in state.load(ap)["workers"] if w["task"] == task and w["role"] == role), None)
    parked = old if resume and old and old.get("parked") else None
    if parked and parked.get("ports"):
        port_range = parked["ports"]  # a parked worker keeps its slot: its ports and servers stay its own
    prompt = (f"{prompt}\n\nYour autopilot: {ap}. Your task: {task}. Your role: {role}. "
              f"Your port range: {port_range[0]}-{port_range[1]}. "
              f"The autopilot CLI: {paths.package_root() / 'bin' / 'ap'}.")
    argv, env = build_start(gcfg, apcfg, ap, task, role, prompt)
    p = profile.launch_profile(argv[argv.index("--settings") + 1])
    # A parked worker resumes its own conversation; the saved session of the role is an earlier run's.
    sid = ((parked or {}).get("session_id") or _saved_session(ap, task, role)) if resume else ""
    if resume and not sid:
        raise RuntimeError(f"{ap} {task}: no saved {role} session to resume")
    if sid:
        # Flags on --resume start a copy under a new id; -n keeps the worker's name on that copy.
        argv = ([argv[0], "--bg", "--resume", sid, "-n", sessions.name_for(ap, task, role),
                 "--settings", argv[argv.index("--settings") + 1]]
                + argv[argv.index("--permission-mode"):argv.index("--permission-mode") + 2])
        argv.append(prompt if role in AGENTS else "/autopilot:pr-loop " + prompt)
    if parked:
        # The parked session sits idle; the copy that continues takes its place.
        subprocess.run([claude_bin(gcfg), "stop", parked["short_id"]], capture_output=True, timeout=60)
    short = _launch(argv, cwd, env)
    _record(short, "worker", ap, p)
    name = sessions.name_for(ap, task, role)
    listed = sessions.list_all(claude_bin(gcfg))
    found = sessions.find_by_id(short, listed) or sessions.find(name, listed)
    rec = {"task": task, "role": role, "session_name": name,
           "session_id": (found or {}).get("sessionId") or "", "short_id": short,
           "ports": port_range,
           "needs": list(needs or []), "started_at": events.now_iso(), "resumes": 0, "cwd": str(cwd),
           "profile": p["name"]}
    if parked and parked.get("limit_parks"):
        rec["limit_parks"] = parked["limit_parks"]   # cleared once this run gets a normal reply
    if sid and (found or {}).get("sessionId") and found["sessionId"] != sid:
        # The resumed copy has a new session id; the original stays on disk until the task is cleaned up.
        rec["replaced_sessions"] = list((old or {}).get("replaced_sessions") or []) + [sid]
    elif old and old.get("replaced_sessions"):
        rec["replaced_sessions"] = list(old["replaced_sessions"])
    with state.mutate(ap) as st:
        st["workers"] = [w for w in st["workers"] if not (w["task"] == task and w["role"] == role)] + [rec]
    if parked:
        try:
            (paths.ap_dir(ap) / "runs" / task / "parked.json").unlink()
        except FileNotFoundError:
            pass
        events.append(ap, "worker_resumed", {"task": task, "role": role, "short_id": short, "from": "parked"})
    events.append(ap, "worker_started", {"task": task, "role": role, "short_id": short})
    return rec


def resume(ap: str, worker: dict) -> dict:
    check_allowed()
    gcfg = config.load_global()
    prompt = ("Your session was interrupted. Continue the task from where you stopped; "
              "check your worktree and run files first.")
    settings = paths.ap_dir(ap) / "settings.json"
    if not settings.exists():
        raise RuntimeError(f"{ap}: no permission floor yet; run `ap perms write {ap}` first")
    p = profile.launch_profile(settings)
    argv = [claude_bin(gcfg), "--bg", "--resume", worker["session_id"],
            "-n", sessions.name_for(ap, worker["task"], worker["role"]), "--settings", str(settings), prompt]
    short = _launch(argv, worker.get("cwd") or str(paths.ap_dir(ap)), _env("worker"))
    _record(short, "worker", ap, p)
    # --settings on --resume starts a copy under a new id (like start's flags do): follow it.
    found = sessions.find_by_id(short, sessions.list_all(claude_bin(gcfg)))
    with state.mutate(ap) as st:
        for w in st["workers"]:
            if w["task"] == worker["task"] and w["role"] == worker["role"]:
                w["short_id"] = short
                if (found or {}).get("sessionId") and found["sessionId"] != w["session_id"]:
                    w["replaced_sessions"] = list(w.get("replaced_sessions") or []) + [w["session_id"]]
                    w["session_id"] = found["sessionId"]
                w["resumes"] = w.get("resumes", 0) + 1
                w["started_at"] = events.now_iso()
                w["profile"] = p["name"]
                worker = w
    events.append(ap, "worker_resumed", {"task": worker["task"], "role": worker["role"], "short_id": short})
    return worker


def park(ap: str, task: str, role: str, note: str, now: datetime = None, b: dict = None) -> dict:
    """A worker stops itself because a usage window is at the park line: record why and when it may continue.
    Raises ValueError when usage is under the line (nothing to wait for) or the worker is not on record."""
    gcfg = config.load_global()
    now = now or datetime.now(timezone.utc)
    p = budget.parking(b if b is not None else budget.read(stale_min=gcfg["budget"]["stale_min"]), gcfg,
                       profile.for_env(os.environ))
    if p is None:
        raise ValueError("usage is under the park line (or unknown): keep working, do not park")
    rec = dict(p, reason="quota", note=note, parked_at=now.isoformat(), role=role)
    with state.mutate(ap) as st:
        w = next((w for w in st["workers"] if w["task"] == task and w["role"] == role), None)
        if w is None:
            raise ValueError(f"{ap}: no {role} worker on {task}")
        w["parked"] = rec
    run = paths.ap_dir(ap) / "runs" / task
    run.mkdir(parents=True, exist_ok=True)
    (run / "parked.json").write_text(json.dumps(rec, indent=1, ensure_ascii=False) + "\n")
    events.append(ap, "worker_parked", {"task": task, "role": role, "window": p["kind"], "percent": p["percent"],
                                        "resume_at": p["resume_at"]})
    return rec


def end(ap: str, task: str, role: str) -> None:
    gcfg = config.load_global()
    """Stop a worker and keep its conversation on the task, so a fix round can resume it."""
    from autopilot import usage
    with state.mutate(ap) as st:
        keep, tokens = [], usage.zero()
        for w in st["workers"]:
            if w["task"] == task and w["role"] == role:
                # The listing knows the session behind the short id; a resumed copy has a new session id.
                hit = sessions.find_by_id(w["short_id"], sessions.list_all(claude_bin(gcfg)))
                sid = (hit or {}).get("sessionId") or w.get("session_id")
                tokens = usage.session_tokens(sid)
                try:
                    profile.note_session_end(sid, w.get("profile") or profile.active()["name"],
                                             datetime.now(timezone.utc))
                except Exception:  # noqa: BLE001 - key bookkeeping must not stop a worker from ending
                    pass
                subprocess.run([claude_bin(gcfg), "stop", w["short_id"]], capture_output=True, timeout=60)
                _settle(w["short_id"])
                for t in st["tasks"]:
                    if t["id"] == task:
                        if sid:
                            t.setdefault("sessions", {})[role] = sid
                        old = [x for x in w.get("replaced_sessions") or [] if x != sid]
                        if sid and sid != w.get("session_id") and w.get("session_id") not in old:
                            old.append(w["session_id"])
                        if old:
                            t["replaced_sessions"] = sorted(set(t.get("replaced_sessions") or []) | set(old))
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
                ids = list((t.pop("sessions", None) or {}).values()) + list(t.pop("replaced_sessions", None) or [])
                for sid in ids:
                    # `claude rm` takes the full session id; pricing needs the short id of the same session.
                    hit = next((s for s in sessions.list_all(claude_bin(gcfg)) or [] if s.get("sessionId") == sid), None)
                    if hit and hit.get("id"):
                        _settle(hit["id"])
                    subprocess.run([claude_bin(gcfg), "rm", sid], capture_output=True, timeout=60)
                    removed.append(sid)
    events.append(ap, "task_cleaned", {"task": task, "sessions": len(removed)})
    return removed


def text_cap_parked() -> str:
    return "paused at the daily cap; resumes when you resume the autopilot"


_RUN_SKEW = timedelta(minutes=2)          # an error this much before a run's start may still be the run's own
_LIMIT_BACKOFF = timedelta(minutes=30)    # a limit with no readable reset: wait this long, doubling per park
_LIMIT_BACKOFF_MAX = timedelta(hours=4)


def _listed(w: dict, all_s):
    return sessions.find_by_id(w.get("short_id"), all_s) or sessions.find(w["session_name"], all_s)


def _since(w: dict) -> datetime:
    try:
        at = datetime.fromisoformat(str(w.get("started_at")).replace("Z", "+00:00"))
    except ValueError:
        return datetime.max.replace(tzinfo=timezone.utc)   # no start time: nothing can be told apart from history
    return (at if at.tzinfo else at.astimezone()) - _RUN_SKEW


def _api_stop(w: dict, all_s, run_dir, tail=None):
    """The API error this worker's current run stopped on (`sessions.api_stop`), or None. Only for a session
    that is not working: a parked or finished worker, or an unreadable listing, is never read."""
    if all_s is None or w.get("parked") or sessions.classify(w, all_s, run_dir) == "done":
        return None
    s = _listed(w, all_s)
    if s and (s.get("state") == "working" or s.get("status") == "busy"):
        return None
    if tail is None:
        tail = usage.transcript_tail((s or {}).get("sessionId") or w.get("session_id"))
    return sessions.api_stop(tail, _since(w))


def worker_state(w: dict, all_s, run_dir) -> tuple:
    """(state, blocked_reason or None). A session a gateway refuses is `blocked` with the reason
    `gateway_refused` however the listing labels its ended turn: resuming it on that gateway never helps."""
    st = sessions.classify(w, all_s, run_dir)
    if st in ("running", "blocked", "dead") and (_api_stop(w, all_s, run_dir) or {}).get("kind") == "gateway_refused":
        return "blocked", "gateway_refused"
    return st, None


def _limit_park(w: dict, stop: dict, now: datetime, gcfg: dict) -> dict:
    n = int(w.get("limit_parks") or 0) + 1
    reset = sessions.reset_time(stop["text"], stop["at"] or now)
    if reset is not None:
        resume_at = reset + timedelta(minutes=gcfg["budget"]["resume_after_min"])
    else:
        resume_at = now + min(_LIMIT_BACKOFF * 2 ** (n - 1), _LIMIT_BACKOFF_MAX)
    w["limit_parks"] = n
    return {"kind": None, "model": None, "percent": None, "resets_at": reset.isoformat() if reset else None,
            "resume_at": resume_at.isoformat(), "reason": "limit", "note": stop["text"], "parked_at": now.isoformat(),
            "role": w["role"]}


def note_api_stops(ap: str, all_s, now: datetime = None) -> None:
    """Act on workers whose last turn ended on an API error. A usage or spend limit parks the worker like a quota
    park (reason `limit`) until the reset the message names, plus `budget.resume_after_min`; with no readable
    reset it waits 30 minutes, doubling with each limit park in a row, at most 4 hours. A normal reply in a run
    clears that count. Runs under the state lock and changes nothing the second time; with nothing to change it
    does not write the state at all. A gateway refusal changes no state: `worker_state` reports it."""
    if all_s is None:
        return
    now = now or datetime.now(timezone.utc)
    runs = paths.ap_dir(ap) / "runs"

    def change(w: dict):
        """("park", stop) or ("clear", None) for one worker, or None."""
        s = _listed(w, all_s)
        tail = usage.transcript_tail((s or {}).get("sessionId") or w.get("session_id"))
        if w.get("limit_parks") and not w.get("parked") and tail and tail["reply_at"] and tail["reply_at"] >= _since(w):
            if not tail["errors"]:
                return "clear", None
            w = dict(w, limit_parks=0)   # it replied in this run before this limit: the count starts again
        stop = _api_stop(w, all_s, runs / w["task"], tail)
        if stop and stop["kind"] == "limit":
            return "park", dict(stop, reset_count=not w.get("limit_parks"))
        return None

    if not any(change(w) for w in state.load(ap)["workers"]):
        return
    parked = []
    with state.mutate(ap) as st:
        for w in st["workers"]:
            what = change(w)
            if what is None:
                continue
            if what[0] == "clear" or what[1]["reset_count"]:
                w.pop("limit_parks", None)
            if what[0] == "park":
                sid = (_listed(w, all_s) or {}).get("sessionId")
                if sid and not w.get("session_id"):
                    w["session_id"] = sid
                w["parked"] = _limit_park(w, what[1], now, config.load_global())
                parked.append(w)
    for w in parked:
        run = runs / w["task"]
        run.mkdir(parents=True, exist_ok=True)
        (run / "parked.json").write_text(json.dumps(w["parked"], indent=1, ensure_ascii=False) + "\n")
        events.append(ap, "worker_parked", {"task": w["task"], "role": w["role"], "window": None, "percent": None,
                                            "resume_at": w["parked"]["resume_at"], "reason": "limit"})


def listing(ap: str) -> list:
    gcfg = config.load_global()
    all_s = sessions.list_all(claude_bin(gcfg))
    note_api_stops(ap, all_s)
    runs = paths.ap_dir(ap) / "runs"
    rows = []
    for w in state.load(ap)["workers"]:
        st, why = worker_state(w, all_s, runs / w["task"])
        rows.append(dict(w, state=st, **({"blocked_reason": why} if why else {})))
    return rows


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
                tail = ", the gateway refuses its conversation" if w.get("blocked_reason") == "gateway_refused" else ""
                if w["state"] == "parked":
                    tail = (f", {text_cap_parked()}" if w["parked"].get("kind") == "cap"
                            else f", resumes {w['parked']['resume_at']}")
                print(f"{w['session_name']}: {w['state']} (resumes {w['resumes']}{tail})")
        return 0
    if len(args) < 4:
        print("usage: ap worker start|resume|end <ap> <task> <role> [--cwd P --prompt-file F --needs a,b --resume]"
              " | ap worker park <ap> <task> <role> --note TEXT | ap worker list <ap> [--json]", file=sys.stderr)
        return 2
    sub, ap, task, role = args[:4]
    try:
        return _run(sub, ap, task, role, args)
    except RuntimeError as e:
        print(f"ap worker {sub}: {e}", file=sys.stderr)
        return 1


def _run(sub: str, ap: str, task: str, role: str, args: list) -> int:
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
    elif sub == "park":
        try:
            rec = park(ap, task, role, _opt(args, "--note", "") or "")
        except ValueError as e:
            print(f"ap worker park: {e}", file=sys.stderr)
            return 1
        print(f"parked until {rec['resume_at'] or 'usage drops'}; end your turn now with no further tool call")
    else:
        return 2
    return 0
