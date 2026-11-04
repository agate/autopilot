from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

from autopilot import cli, config, events, paths, state, text

SOURCE_WINS = ("title", "depends_on", "link", "assignee", "needs", "critical", "phase")
RANK = {"todo": 0, "blocked": 0, "in_progress": 1, "in_review": 2, "done": 3, "skipped": 3}


def _merge_status(local: str, source: str) -> str:
    """The source wins when it finishes a task or moves it forward; it never moves local progress back
    (a workflow without an In Review column would otherwise drop a PR that is under review)."""
    if source in ("done", "skipped") or RANK.get(source, 0) > RANK.get(local, 0) or local in ("todo", "blocked"):
        return source
    return local
REPORT_KEYS = {"slot", "done", "waiting", "next", "budget", "access"}


def _write_json(path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def tasks_sync(ap: str, tasks: list) -> None:
    with state.mutate(ap) as st:
        _sync(ap, st, tasks)
    events.append(ap, "tasks_synced", {"count": len(tasks)})


def _sync(ap: str, st: dict, tasks: list) -> None:
    by_id = {t["id"]: t for t in st["tasks"]}
    incoming = {t["id"] for t in tasks}
    for t in tasks:
        cur = by_id.get(t["id"])
        if cur is None:
            new = dict(t)
            new["origin"] = "source"
            st["tasks"].append(new)
        else:
            for k in SOURCE_WINS:
                if k in t:
                    cur[k] = t[k]
            if "status" in t:
                cur["status"] = _merge_status(cur["status"], t["status"])
    for t in st["tasks"]:
        if t["origin"] == "source" and t["id"] not in incoming and t["status"] != "skipped":
            t["status"] = "skipped"
            events.append(ap, "task_removed_from_source", {"task": t["id"]})


def task_set(ap: str, task_id: str, fields: dict) -> None:
    with state.mutate(ap) as st:
        t = next((t for t in st["tasks"] if t["id"] == task_id), None)
        if t is None:
            raise state.StateError(f"{ap}: no task {task_id}")
        t.update(fields)
    events.append(ap, "task_set", {"task": task_id, "fields": fields})


def task_add(ap: str, task: dict) -> None:
    with state.mutate(ap) as st:
        if any(t["id"] == task["id"] for t in st["tasks"]):
            raise state.StateError(f"{ap}: task {task['id']} already exists")
        new = dict(task)
        new["origin"] = "found"
        st["tasks"].append(new)
    events.append(ap, "task_added", {"task": task["id"]})


def decision_add(ap: str, d: dict) -> str:
    with state.mutate(ap) as st:
        nums = [int(x["id"][1:]) for x in st["decisions"] if x["id"][1:].isdigit()]
        for line in _archive_lines(ap):
            if line.get("id", "")[1:].isdigit():
                nums.append(int(line["id"][1:]))
        new = dict(d)
        new["id"] = f"D{max(nums, default=0) + 1}"
        new["status"] = "open"
        new["created_at"] = events.now_iso()
        st["decisions"].append(new)
    events.append(ap, "decision_added", {"id": new["id"], "kind": new.get("kind")})
    return new["id"]


class AnswerConflict(Exception):
    """The decision already has a different answer; only an explicit replace changes it."""


def decision_resolve(ap: str, did: str, answer: str, status: str = "answered", replace: bool = False) -> str:
    """Record an answer once. "unchanged" when the same answer is already recorded (a reply seen twice),
    AnswerConflict when a different one is, unless `replace` (the user asked to change it)."""
    with state.mutate(ap) as st:
        d = next((d for d in st["decisions"] if d["id"] == did), None)
        if d is None:
            raise state.StateError(f"{ap}: no decision {did}")
        if d["status"] != "open" and not replace:
            if d["status"] == status and d.get("answer") == answer:
                return "unchanged"
            raise AnswerConflict(f"{ap}: {did} already has the answer {d.get('answer')!r} ({d['status']})")
        d.update(status=status, answer=answer, answered_at=events.now_iso())
    events.append(ap, "decision_resolved", {"id": did, "status": status, "answer": answer})
    return "resolved"


def _archive_lines(ap: str) -> list:
    p = paths.ap_dir(ap) / "archive.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def archive(ap: str) -> int:
    days = config.load_global()["archive_after_days"]
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with state.mutate(ap) as st:
        keep, moved = [], []
        for d in st["decisions"]:
            at = d.get("answered_at")
            old = at and datetime.fromisoformat(at.replace("Z", "+00:00")) < cutoff
            (moved if d["status"] != "open" and old else keep).append(d)
        if moved:
            with open(paths.ap_dir(ap) / "archive.jsonl", "a", encoding="utf-8") as f:
                for d in moved:
                    f.write(json.dumps(d, ensure_ascii=False) + "\n")
            st["decisions"] = keep
    return len(moved)


def report_write(ap: str, rep: dict) -> None:
    langs = state.langs()
    errs = [f"report: unknown key {k}" for k in set(rep) - REPORT_KEYS]
    errs += [f"report: missing {k}" for k in REPORT_KEYS - set(rep)]
    for k in ("done", "waiting", "next", "access"):
        for i, item in enumerate(rep.get(k) or []):
            errs += text.check(item, langs, f"report.{k}[{i}]")
    if "budget" in rep:
        errs += text.check(rep["budget"], langs, "report.budget")
    if errs:
        raise state.StateError("; ".join(errs[:5]))
    _write_json(paths.ap_dir(ap) / "reports" / f"{rep['slot']}.json", rep)
    with state.mutate(ap) as st:
        st["last_report"] = rep["slot"]
    events.append(ap, "report_written", {"slot": rep["slot"]})


def pass_begin() -> None:
    _write_json(paths.data_root() / "last_pass.json", {"begun_at": events.now_iso()})


def pass_end(started: int, free: int, ready: int, allow) -> None:
    p = paths.data_root() / "last_pass.json"
    cur = json.loads(p.read_text()) if p.exists() else {}
    cur.update(ended_at=events.now_iso(), started=started, free=free, ready=ready, allow=allow)
    _write_json(p, cur)


def _load_arg(path: str):
    return json.loads(open(path).read())


def _kv(pairs: list) -> dict:
    out = {}
    for p in pairs:
        k, _, v = p.partition("=")
        try:
            out[k] = json.loads(v)
        except ValueError:
            out[k] = v
    return out


def _flag(args, name, default=None):
    return args[args.index(name) + 1] if name in args else default


def _int_or_none(v):
    return None if v in (None, "none", "null") else int(v)


@cli.command("tasks")
def _tasks(args):
    if args[:1] != ["sync"] or len(args) != 3:
        print("usage: ap tasks sync <ap> <file.json>", file=sys.stderr)
        return 2
    tasks_sync(args[1], _load_arg(args[2]))
    return 0


@cli.command("task")
def _task(args):
    if len(args) >= 3 and args[0] == "set":
        task_set(args[1], args[2], _kv(args[3:]))
    elif len(args) == 3 and args[0] == "cleanup":
        from autopilot import workers
        print(len(workers.cleanup(args[1], args[2])))
    elif len(args) == 3 and args[0] == "add":
        task_add(args[1], _load_arg(args[2]))
    else:
        print("usage: ap task set <ap> <id> k=v ... | ap task add <ap> <file.json>", file=sys.stderr)
        return 2
    return 0


@cli.command("decision")
def _decision(args):
    if len(args) == 3 and args[0] == "add":
        print(decision_add(args[1], _load_arg(args[2])))
    elif len(args) >= 3 and args[0] == "resolve":
        try:
            print(decision_resolve(args[1], args[2], _flag(args, "--answer", ""), _flag(args, "--status", "answered"),
                                   replace="--replace" in args))
        except AnswerConflict as e:
            print(f"conflict: {e}", file=sys.stderr)
            return 3
    else:
        print("usage: ap decision add <ap> <file> | resolve <ap> <id> --answer X [--status answered|undone] [--replace]",
              file=sys.stderr)
        return 2
    return 0


@cli.command("archive")
def _archive(args):
    print(archive(args[0]))
    return 0


@cli.command("report")
def _report(args):
    if args[:1] == ["due"] and len(args) == 2:
        from autopilot import gate
        gcfg, apcfg, st = config.load_global(), config.load_ap(args[1]), state.load(args[1])
        print(gate.report_due(apcfg.get("report_times") or gcfg["report_times"], st.get("last_report"),
                              datetime.now()) or "")
        return 0
    if args[:1] != ["write"] or len(args) != 3:
        print("usage: ap report write <ap> <file.json> | ap report due <ap>", file=sys.stderr)
        return 2
    report_write(args[1], _load_arg(args[2]))
    return 0


@cli.command("event")
def _event(args):
    events.append(args[0], args[1], json.loads(args[2]) if len(args) > 2 else {})
    return 0


@cli.command("pass")
def _pass(args):
    if args[:1] == ["begin"]:
        pass_begin()
    elif args[:1] == ["end"]:
        pass_end(int(_flag(args, "--started", 0)), int(_flag(args, "--free", 0)), int(_flag(args, "--ready", 0)),
                 _int_or_none(_flag(args, "--allow")))
    else:
        print("usage: ap pass begin | ap pass end --started N --free N --ready N --allow N|none", file=sys.stderr)
        return 2
    return 0
