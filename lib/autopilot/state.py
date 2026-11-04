from __future__ import annotations

import contextlib
import fcntl
import json
import os

from autopilot import config, paths, text

TOP = {"version", "tasks", "workers", "decisions", "last_report"}
TASK_REQ = {"id", "title", "depends_on", "status", "link", "needs", "critical", "phase", "origin"}
TASK_OPT = {"pr", "branch", "worktree", "spec", "assignee", "evidence", "sessions"}
TASK_STATUS = {"todo", "in_progress", "in_review", "done", "blocked", "skipped"}
ORIGINS = {"source", "found"}
WORKER_REQ = {"task", "role", "session_name", "session_id", "short_id", "ports", "needs", "started_at", "resumes"}
ROLES = {"implement", "review", "pr"}
DEC_REQ = {"id", "kind", "title", "body", "options", "links", "status", "created_at"}
DEC_OPT = {"task", "answer", "action", "answered_at"}
DEC_KINDS = {"ask", "gate", "self"}
DEC_STATUS = {"open", "answered", "undone"}


class StateError(Exception):
    pass


def empty() -> dict:
    return {"version": 1, "tasks": [], "workers": [], "decisions": [], "last_report": None}


def _keys(obj, req: set, opt: set, where: str) -> list:
    if not isinstance(obj, dict):
        return [f"{where}: must be an object"]
    errs = [f"{where}: missing {k}" for k in sorted(req - set(obj))]
    errs += [f"{where}: unknown key {k}" for k in sorted(set(obj) - req - opt)]
    return errs


def validate(st: dict, langs: set) -> list:
    if not isinstance(st, dict):
        return ["state: not an object"]
    errs = _keys(st, TOP, set(), "state")
    if st.get("version") != 1:
        errs.append("state: version must be 1")
    for k in ("tasks", "workers", "decisions"):
        if not isinstance(st.get(k), list):
            return errs + [f"state: {k} must be a list"]
    for k in ("tasks", "workers", "decisions"):
        for i, x in enumerate(st[k]):
            if not isinstance(x, dict):
                return errs + [f"{k}[{i}]: must be an object"]
    for i, d in enumerate(st["decisions"]):
        for f in ("options", "links"):
            if not isinstance(d.get(f, []), list) or not all(isinstance(o, dict) for o in d.get(f, [])):
                return errs + [f"decisions[{i}].{f}: must be a list of objects"]
    seen = set()
    for i, t in enumerate(st.get("tasks") or []):
        w = f"tasks[{i}]"
        errs += _keys(t, TASK_REQ, TASK_OPT, w)
        if t.get("status") not in TASK_STATUS:
            errs.append(f"{w}: bad status {t.get('status')!r}")
        if t.get("origin") not in ORIGINS:
            errs.append(f"{w}: bad origin {t.get('origin')!r}")
        if t.get("id") in seen:
            errs.append(f"{w}: duplicate id {t.get('id')}")
        seen.add(t.get("id"))
        if "title" in t:
            errs += text.check(t["title"], langs, f"{w}.title")
    for i, wk in enumerate(st.get("workers") or []):
        w = f"workers[{i}]"
        errs += _keys(wk, WORKER_REQ, set(), w)
        if wk.get("role") not in ROLES:
            errs.append(f"{w}: bad role {wk.get('role')!r}")
    for i, d in enumerate(st.get("decisions") or []):
        w = f"decisions[{i}]"
        errs += _keys(d, DEC_REQ, DEC_OPT, w)
        if d.get("kind") not in DEC_KINDS:
            errs.append(f"{w}: bad kind {d.get('kind')!r}")
        if d.get("status") not in DEC_STATUS:
            errs.append(f"{w}: bad status {d.get('status')!r}")
        for f in ("title", "body"):
            if f in d:
                errs += text.check(d[f], langs, f"{w}.{f}")
        for j, o in enumerate(d.get("options") or []):
            errs += text.check(o.get("label"), langs, f"{w}.options[{j}].label")
            if "cost" in o:
                errs += text.check(o["cost"], langs, f"{w}.options[{j}].cost")
    return errs


def _path(name: str):
    return paths.ap_dir(name) / "state.json"


def langs() -> set:
    return text.required_langs(config.load_global())


def load(name: str) -> dict:
    p = _path(name)
    if not p.exists():
        return empty()
    try:
        st = json.loads(p.read_text())
    except ValueError as e:
        raise StateError(f"{name}: state.json is not valid JSON ({e})")
    errs = validate(st, langs())
    if errs:
        raise StateError(f"{name}: " + "; ".join(errs[:5]))
    return st


def save(name: str, st: dict) -> None:
    errs = validate(st, langs())
    if errs:
        raise StateError(f"{name}: " + "; ".join(errs[:5]))
    p = _path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, p)


@contextlib.contextmanager
def mutate(name: str):
    """Load, let the caller change, validate and save one autopilot's state, holding its file lock.
    Every writer (dispatcher, talk session, gate, worker commands) goes through here."""
    lk = paths.ap_dir(name) / "state.lock"
    lk.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lk), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        st = load(name)
        yield st
        save(name, st)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
