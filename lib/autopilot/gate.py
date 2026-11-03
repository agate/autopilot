from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

from autopilot import budget, cli, config, events, lock, nextwork, ops, paths, sessions, state, workers


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


def pr_fingerprint(gh_bin: str, repo: str, pr) -> str:
    try:
        out = subprocess.run([gh_bin, "pr", "view", str(pr), "--json",
                              "headRefOid,reviewDecision,statusCheckRollup,latestReviews"],
                             cwd=repo, capture_output=True, text=True, timeout=60)
        return _hash(out.stdout) if out.returncode == 0 else "err"
    except (OSError, subprocess.SubprocessError):
        return "err"


def watch_fingerprint(cmd: str, cwd: str) -> str:
    try:
        out = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=30)
        return _hash(out.stdout)
    except (OSError, subprocess.SubprocessError):
        return "err"


def fingerprint(ap: str, apcfg: dict, st: dict, all_sessions: list, now: datetime, gh_bin: str,
                free: int, gcfg: dict) -> dict:
    runs = paths.ap_dir(ap) / "runs"
    files = []
    if runs.exists():
        for f in sorted(runs.glob("*/*.json")):
            if f.name in sessions.RESULT_FILES.values():
                files.append(f"{f.parent.name}/{f.name}:{int(f.stat().st_mtime)}")
    wk = sorted(f"{w['session_name']}={sessions.classify(w, all_sessions, runs / w['task'])}"
                for w in st["workers"])
    ready = [t["id"] for t in nextwork.ready(st)] if free > 0 else []
    prs = {t["id"]: pr_fingerprint(gh_bin, apcfg["repo"], t["pr"])
           for t in st["tasks"] if t.get("pr") and t["status"] == "in_review"}
    watches = {w["name"]: watch_fingerprint(w["cmd"], apcfg["repo"]) for w in apcfg.get("watches") or []}
    resume = None
    if st.get("paused_until"):
        try:
            if now.astimezone(timezone.utc) >= datetime.fromisoformat(st["paused_until"]):
                resume = "resume"
        except ValueError:
            resume = "bad"
    times = apcfg.get("report_times") or gcfg["report_times"]
    return {"files": _hash("|".join(files)), "workers": wk, "ready": ready, "prs": prs, "watches": watches,
            "report": report_due(times, st.get("last_report"), now), "budget_resume": resume}


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


def _sessions(gcfg: dict) -> list:
    return sessions.list_all(workers.claude_bin(gcfg))


def _gh(gcfg: dict) -> str:
    return gcfg["tools"].get("gh") or shutil.which("gh") or "gh"


def _gate_file():
    return paths.data_root() / "gate.json"


def _read_gate() -> dict:
    try:
        return json.loads(_gate_file().read_text())
    except (OSError, ValueError):
        return {}


def _write_gate(g: dict) -> None:
    p = _gate_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(g, indent=1, sort_keys=True) + "\n")


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _fingerprints(gcfg: dict, names: list, now: datetime, gs: dict) -> tuple:
    all_sessions = _sessions(gcfg)
    loaded, broken = {}, {}
    for n in names:
        try:
            loaded[n] = (config.load_ap(n), state.load(n))
        except (state.StateError, FileNotFoundError, ValueError) as e:
            broken[n] = str(e)
    live = sum(len(st["workers"]) for _, st in loaded.values())
    free = gcfg["max_workers"] - live
    fps = {n: fingerprint(n, c, st, all_sessions, now, _gh(gcfg), free, gcfg) for n, (c, st) in loaded.items()}
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


def run(now: datetime = None, dispatch_fn=None) -> int:
    gcfg = config.load_global()
    if lock.held(paths.data_root() / "lock"):
        return 0
    now = now or _local_now()
    gs = _read_gate()
    names = config.active_aps()
    fps, broken = _fingerprints(gcfg, names, now, gs)
    for n, err in broken.items():
        if (gs.get("broken") or {}).get(n) != err:
            events.append(n, "state_broken", {"error": err})
            notify(f"autopilot {n}: state file is broken")
    last_at = gs.get("last_pass_at")
    last_at = datetime.fromisoformat(last_at) if last_at else None
    prev = gs.get("fps") or {}
    todo = [n for n in fps if should_dispatch(prev.get(n), fps[n], last_at, now, gcfg["forced_pass_min"])[0]]
    if todo:
        if dispatch_fn is None:
            from autopilot import dispatch
            dispatch_fn = dispatch.run
        results = dispatch_fn(gcfg, todo)
        if any(idle_failed(r) for r in results):
            results = dispatch_fn(gcfg, todo)
            if any(idle_failed(r) for r in results):
                _flag_stuck(todo)
        gs["last_pass_at"] = now.isoformat()
        fps, _ = _fingerprints(gcfg, names, now, gs)
    gs.update(fps=fps, broken=broken, last_run_at=now.isoformat())
    _write_gate(gs)
    return 0


@cli.command("gate")
def _cmd(args: list) -> int:
    return run()


@cli.command("tick")
def _tick(args: list) -> int:
    from autopilot import dispatch
    names = args or config.active_aps()
    results = dispatch.run(config.load_global(), names)
    print(json.dumps(results, indent=1))
    return 0
