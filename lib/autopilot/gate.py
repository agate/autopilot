from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from autopilot import budget, cli, config, dispatch, events, lock, nextwork, ops, paths, park, profile, refresh, replies, sessions, state, workers


def report_due(times: list, last_slot, now: datetime):
    """The most recent report slot at or before now, if no report covers it yet."""
    best = None
    for day in (now.date() - timedelta(days=1), now.date()):
        for t in times:
            hh, mm = (int(x) for x in t.split(":"))
            at = datetime(day.year, day.month, day.day, hh, mm)
            if at <= now.replace(tzinfo=None) and (best is None or at > best):
                best = at
    if best is None:
        return None
    slot = best.strftime("%Y-%m-%d-%H")
    return slot if last_slot is None or slot > last_slot else None


def _hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()[:12]


GREEN = {"SUCCESS", "NEUTRAL", "SKIPPED"}
RED = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}


def pr_summary(data: dict) -> str:
    """What about a PR should wake the dispatcher: head, overall CI state, review decision, each reviewer's
    latest review. Check timestamps and per-check progress are left out on purpose."""
    vals = [(c.get("conclusion") or c.get("state") or "").upper() for c in data.get("statusCheckRollup") or []]
    if not vals:
        ci = "none"
    elif any(v in RED for v in vals):
        ci = "red"
    elif all(v in GREEN for v in vals):
        ci = "green"
    else:
        ci = "pending"
    latest = {}
    for r in data.get("latestReviews") or []:
        latest[(r.get("author") or {}).get("login") or "?"] = f"{r.get('state')}@{(r.get('commit') or {}).get('oid')}"
    return json.dumps({"head": data.get("headRefOid"), "ci": ci, "decision": data.get("reviewDecision"),
                       "reviews": sorted(latest.items())}, sort_keys=True)


def pr_fingerprint(gh_bin: str, repo: str, pr) -> str:
    try:
        out = subprocess.run([gh_bin, "pr", "view", str(pr), "--json",
                              "headRefOid,reviewDecision,statusCheckRollup,latestReviews"],
                             cwd=repo, capture_output=True, text=True, timeout=60)
        return _hash(pr_summary(json.loads(out.stdout))) if out.returncode == 0 else "err"
    except (OSError, ValueError, subprocess.SubprocessError):
        return "err"


def watch_fingerprint(cmd: str, cwd: str) -> str:
    try:
        out = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=30)
        return _hash(out.stdout)
    except (OSError, subprocess.SubprocessError):
        return "err"


def fingerprint(ap: str, apcfg: dict, st: dict, all_sessions, now: datetime, gh_bin: str,
                free: int, gcfg: dict, hide_after: float = None) -> dict:
    """What the gate compares between runs. With hide_after (epoch seconds), result files written after
    that moment are left out, so results that land during a pass still count as new next time."""
    runs = paths.ap_dir(ap) / "runs"
    files = []
    if runs.exists():
        for f in sorted(runs.glob("*/*.json")):
            if f.name in sessions.RESULT_FILES.values():
                mt = f.stat().st_mtime
                if hide_after is None or mt < hide_after:
                    files.append(f"{f.parent.name}/{f.name}:{int(mt)}")
    wk = []
    b = budget.read() if any(w.get("parked") for w in st["workers"]) else None
    for w in st["workers"]:
        cls = sessions.classify(w, all_sessions, runs / w["task"])
        if cls == "parked":
            # Looks like a running worker while it waits, so parking wakes no pass; the entry changes the
            # moment it can continue, which does.
            cls = "parked_due" if park.resume_due(w, now, gcfg, b) else "running"
        if cls == "done" and hide_after is not None:
            rf = runs / w["task"] / sessions.result_file(w["role"])
            if rf.exists() and rf.stat().st_mtime >= hide_after:
                cls = "running"
        wk.append(f"{w['session_name']}={cls}")
    busy = {w["task"] for w in st["workers"]}
    ready = [t["id"] for t in nextwork.ready(st)] if free > 0 else []
    prs = {t["id"]: pr_fingerprint(gh_bin, apcfg["repo"], t["pr"])
           for t in st["tasks"] if t.get("pr") and t["status"] == "in_review" and t["id"] not in busy}
    watches = {w["name"]: watch_fingerprint(w["cmd"], apcfg["repo"]) for w in apcfg.get("watches") or []}
    times = apcfg.get("report_times")
    if times is None:
        times = gcfg["report_times"]
    return {"files": _hash("|".join(files)), "workers": sorted(wk), "ready": ready, "prs": prs,
            "watches": watches, "report": report_due(times, st.get("last_report"), now)}


def should_dispatch(prev, cur: dict, last_pass_at, now: datetime, forced_min: int) -> tuple:
    if prev is None:
        return True, "first"
    changed = sorted(k for k in set(prev) | set(cur) if prev.get(k) != cur.get(k))
    if changed:
        return True, "changed:" + ",".join(changed)
    if last_pass_at is None or now - last_pass_at >= timedelta(minutes=forced_min):
        return True, "forced"
    return False, "same"


def idle_failed(last_pass: dict) -> bool:
    """The dispatcher's own numbers say it had room and work but started nothing."""
    if not last_pass or last_pass.get("timeout"):
        return False
    return (last_pass.get("free", 0) > 0 and last_pass.get("ready", 0) > 0
            and last_pass.get("allow", None) != 0 and last_pass.get("started", 0) == 0)


def notify(msg: str) -> None:
    try:
        subprocess.run(["osascript", "-e", f"display notification {json.dumps(msg)} with title \"autopilot\""],
                       capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def _sessions(gcfg: dict):
    return sessions.list_all(workers.claude_bin(gcfg))


def _move_legacy(gcfg: dict) -> None:
    """Move older autopilots into projects/ only when nothing else can be using their folders: under the data-root
    lock a pass holds, and with no dispatcher session still working. That lock dies with its gate, but the
    dispatcher session it started may not; an unreadable session list counts as one that still works."""
    if not config.legacy_waiting():
        return
    lk = paths.data_root() / "lock"
    if not lock.acquire(lk, "move older autopilots"):
        return
    try:
        rows = _sessions(gcfg)
        if rows is None:
            return
        s = sessions.find(dispatch.NAME, rows)
        if s and s.get("status") in ("busy", "waiting") and sessions.pid_alive(s.get("pid")):
            return
        config.migrate_legacy()
    finally:
        lock.release(lk)


def _allowance(gcfg: dict, now: datetime) -> dict:
    return budget.allowance(budget.read(), gcfg, now.astimezone(timezone.utc))


def _gh(gcfg: dict) -> str:
    return gcfg["tools"].get("gh") or shutil.which("gh") or "gh"


def _gate_file():
    return paths.data_root() / "gate.json"


def _read_json(p) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_gate(g: dict) -> None:
    p = _gate_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(g, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _load_all(gcfg: dict, now: datetime, hide_after: float = None) -> tuple:
    """Load and fingerprint every active autopilot. Anything wrong with one autopilot marks it broken
    and never stops the others."""
    all_sessions = _sessions(gcfg)
    loaded, broken = {}, {}
    for n in config.list_aps():
        try:
            if config.status_of(n) != "active":
                continue
            apcfg = config.load_ap(n)
            if not apcfg.get("repo"):
                raise ValueError("config.json has no repo")
            loaded[n] = (apcfg, state.load(n))
        except Exception as e:  # noqa: BLE001 - one bad autopilot must not stop the gate
            broken[n] = f"{type(e).__name__}: {e}"
    live = sum(len(st["workers"]) for _, st in loaded.values())
    free = gcfg["max_workers"] - live
    fps = {}
    for n, (c, st) in loaded.items():
        try:
            fps[n] = fingerprint(n, c, st, all_sessions, now, _gh(gcfg), free, gcfg, hide_after)
        except Exception as e:  # noqa: BLE001
            broken[n] = f"{type(e).__name__}: {e}"
    return fps, broken


def _flag_stuck(names: list) -> None:
    for n in names:
        try:
            st = state.load(n)
        except state.StateError:
            continue
        if any(d["status"] == "open" and d["title"] == {"key": "dispatcher_stuck"} for d in st["decisions"]):
            continue
        ops.decision_add(n, {"kind": "ask", "title": {"key": "dispatcher_stuck"},
                             "body": {"key": "dispatcher_stuck"}, "options": [], "links": []})
    notify("The dispatcher is stuck: free capacity and ready work, but nothing started.")


def _idle(aps: list, begun_at: str, gcfg: dict, now: datetime) -> bool:
    """Checked by the gate itself on fresh state: work could start now, but the pass started none."""
    snaps = nextwork.snapshots(aps)
    live = workers.all_workers()
    planned = nextwork.plan_starts(snaps, live, gcfg["max_workers"], _allowance(gcfg, now)["allow"])
    if not planned:
        return False
    started = 0
    for a in aps:
        evs, _ = events.since(a, 0)
        started += sum(1 for e in evs if e.get("type") == "worker_started" and (e.get("at") or "") >= (begun_at or ""))
    return started == 0


def _assess(results: list, todo: list, gcfg: dict, now: datetime) -> bool:
    """Record problems a pass reports; True when it has to be run again."""
    failed = False
    for r in results:
        aps = r.get("aps") or todo
        if r.get("skipped"):
            continue
        for flag, msg in (("blocked", "An autopilot dispatcher pass is waiting at a permission prompt nobody can "
                                      "answer in the background; its session is kept for `claude attach`. "
                                      "Run /autopilot doctor."),
                          ("timeout", "An autopilot dispatcher pass ran past its time limit."),
                          ("error", "An autopilot dispatcher pass failed to run.")):
            if r.get(flag):
                for a in aps:
                    events.append(a, f"dispatcher_{flag}" if flag != "error" else "dispatch_error",
                                  {"detail": str(r.get(flag))})
                notify(msg)
                break
        else:
            if "ended_at" not in r or _idle(aps, r.get("begun_at"), gcfg, now):
                failed = True
    return failed


def _epoch(iso: str):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def run(now: datetime = None, dispatch_fn=None) -> int:
    profile.migrate_from_config()
    gcfg = config.load_global()
    if lock.held(paths.data_root() / "lock"):
        return 0
    _move_legacy(gcfg)
    now = now or _local_now()
    gs = _read_json(_gate_file())
    fps, broken = _load_all(gcfg, now)
    for n, err in broken.items():
        if (gs.get("broken") or {}).get(n) != err:
            try:
                events.append(n, "state_broken", {"error": err})
            except OSError:
                pass
            notify(f"autopilot {n} cannot run: {err[:120]}")
    tick_file = paths.data_root() / "tick.json"
    ticked = set(_read_json(tick_file).get("aps") or [])
    allow = _allowance(gcfg, now)
    ticked |= replies.step(gcfg, gs, now)   # a new answer on the page queues its autopilot
    gs["paused_until"] = allow.get("paused_until") if allow["reason"] == "window" else None
    last = gs.get("last_pass_at") if isinstance(gs.get("last_pass_at"), dict) else {}
    prev = gs.get("fps") or {}
    todo = []
    for n, fp in fps.items():
        at = last.get(n)
        go, why = should_dispatch(prev.get(n), fp, datetime.fromisoformat(at) if at else None, now,
                                  gcfg["forced_pass_min"])
        due = fp.get("report") is not None
        if allow["reason"] == "credits":
            go = due or n in ticked
        elif allow["reason"] == "window":
            go = (go and why != "forced") or due or n in ticked
        else:
            go = go or n in ticked
        if go:
            todo.append(n)
    if todo:
        if dispatch_fn is None:
            from autopilot import dispatch
            dispatch_fn = dispatch.run
        began_at = events.now_iso()
        results = dispatch_fn(gcfg, todo)
        begun = [e for e in (_epoch(r.get("begun_at")) for r in results) if e]
        if _assess(results, todo, gcfg, now):
            results = dispatch_fn(gcfg, todo)
            begun += [e for e in (_epoch(r.get("begun_at")) for r in results) if e]
            if _assess(results, todo, gcfg, now):
                _flag_stuck(todo)
        for n in todo:
            last[n] = now.isoformat()
        fps, broken = _load_all(gcfg, now, hide_after=min(begun) if begun else None)
        # A note queued while the pass ran (a late answer) is for the next pass, so it stays.
        if (_read_json(tick_file).get("at") or "") <= began_at:
            try:
                tick_file.unlink()
            except FileNotFoundError:
                pass
    else:
        refresh.step(gcfg, gs, now)   # no pass this minute: keep the page's light collections fresh
    gs.update(fps=fps, broken=broken, last_pass_at=last, last_run_at=now.isoformat())
    _write_gate(gs)
    return 0


@cli.command("gate")
def _cmd(args: list) -> int:
    return run()


@cli.command("tick")
def _tick(args: list) -> int:
    """Queue a pass for the gate to run now. Never runs the pass in the calling session."""
    from autopilot import install
    names = args or config.active_aps()
    unknown = [n for n in names if n not in config.list_aps()]
    if unknown:
        print(f"no autopilot named {', '.join(unknown)}", file=sys.stderr)
        return 2
    p = paths.data_root() / "tick.json"
    queued = set(_read_json(p).get("aps") or []) | set(names)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"aps": sorted(queued), "at": events.now_iso()}))
    install.kick()
    print(f"queued a pass for {', '.join(sorted(queued))}; the gate starts it within seconds")
    return 0
