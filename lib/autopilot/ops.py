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
    if d.get("kind") == "self" and not isinstance(d.get("attention"), bool):
        raise ValueError("a self card needs \"attention\": true when the user should look at it (see the runbook), "
                         "false for a routine call")
    for f in ("title", "body"):
        miss = text.unfilled(d.get(f))
        if miss:
            raise ValueError(f"{f}: label {d[f]['key']!r} needs args {', '.join(miss)} "
                             f"(for example {{\"key\": \"{d[f]['key']}\", \"args\": {{\"{miss[0]}\": \"...\"}}}})")
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


KEEP = "keep"


class AnswerConflict(Exception):
    """The decision already has a different answer; only an explicit replace changes it."""


def decision_resolve(ap: str, did: str, answer: str, status: str = "answered", replace: bool = False,
                     handling: bool = False) -> str:
    """Record an answer once. "unchanged" when the same answer is already recorded (a reply seen twice),
    AnswerConflict when a different one is, unless `replace` (the user asked to change it).
    With `handling` the page shows the answer as received and being handled until `decision_handled`."""
    with state.mutate(ap) as st:
        d = next((d for d in st["decisions"] if d["id"] == did), None)
        back = None
        if d is None and status == "undone":
            back = d = _archived_read(ap, did)
            if d is not None:
                st["decisions"].append(d)
        if d is None:
            raise state.StateError(f"{ap}: no decision {did}")
        # OK on a self card means "read", not "final": an undo may follow it.
        undo_after_ok = d["kind"] == "self" and d.get("answer") == KEEP and status == "undone"
        if d["status"] != "open" and not replace and not undo_after_ok:
            if d["status"] == status and d.get("answer") == answer:
                return "unchanged"
            raise AnswerConflict(f"{ap}: {did} already has the answer {d.get('answer')!r} ({d['status']})")
        d.update(status=status, answer=answer, answered_at=events.now_iso())
        if handling:
            d["handling"] = True
        else:
            d.pop("handling", None)
        if back is not None:
            # The state file holds the card before the archive lets go of it: a failed save loses nothing.
            state.save(ap, st)
            _drop_archived(ap, did)
            events.append(ap, "decision_unarchived", {"id": did})
    events.append(ap, "decision_resolved", {"id": did, "status": status, "answer": answer})
    return "resolved"


def decision_keep(ap: str, ids: list) -> dict:
    """The user read these self cards and lets them stand (OK, or "mark all read"). Nothing is left to act on,
    so none is shown as being handled. Cards that are not open self cards are skipped, never changed."""
    out = {"kept": [], "unchanged": [], "skipped": []}
    with state.mutate(ap) as st:
        by_id = {d["id"]: d for d in st["decisions"]}
        for did in ids:
            d = by_id.get(did)
            if d is None or d["kind"] != "self" or d["status"] == "undone":
                out["skipped"].append(did)
            elif d["status"] != "open" and d.get("answer") == KEEP:
                d.pop("handling", None)   # an early resolve may have set it; nothing is left to act on
                out["unchanged"].append(did)
            elif d["status"] != "open":
                out["skipped"].append(did)
            else:
                d.update(status="answered", answer=KEEP, answered_at=events.now_iso())
                d.pop("handling", None)
                out["kept"].append(did)
    if out["kept"]:
        events.append(ap, "decisions_kept", {"ids": out["kept"]})
    return out


def decision_attention(ap: str, did: str, flag: bool) -> str:
    """Say whether a self card wants the user's look; for cards added before every self card had to say so."""
    with state.mutate(ap) as st:
        d = next((d for d in st["decisions"] if d["id"] == did), None)
        if d is None or d["kind"] != "self":
            raise state.StateError(f"{ap}: no self decision {did}")
        if d.get("attention") is flag:
            return "unchanged"
        d["attention"] = flag
    events.append(ap, "decision_attention", {"id": did, "attention": flag})
    return "set"


def _when(iso: str):
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def self_unread(st: dict, cfg: dict, now: datetime) -> dict:
    """Open self cards: how many routine ones wait to be read, the oldest of them, how many want a look, and
    whether the reminder is due. A card saved before the flag existed counts as routine."""
    opened = [d for d in st["decisions"] if d.get("kind") == "self" and d.get("status") == "open"]
    routine = [d for d in opened if d.get("attention") is not True]
    oldest = min((d["created_at"] for d in routine if d.get("created_at")), default=None, key=_when)
    at, days = int(cfg.get("remind_at") or 0), float(cfg.get("remind_days") or 0)
    remind = bool(routine) and ((at > 0 and len(routine) >= at) or
                                (days > 0 and oldest is not None and now - _when(oldest) > timedelta(days=days)))
    return {"count": len(routine), "attention": len(opened) - len(routine), "oldest_at": oldest, "remind": remind}


def decision_handled(ap: str, did: str) -> str:
    """The pass finished acting on an answer: the page stops showing it as being handled."""
    with state.mutate(ap) as st:
        d = next((d for d in st["decisions"] if d["id"] == did), None)
        if d is None or d["status"] == "open":
            raise state.StateError(f"{ap}: no answered decision {did}")
        if not d.pop("handling", False):
            return "unchanged"
    events.append(ap, "decision_handled", {"id": did})
    return "handled"


def _archived_read(ap: str, did: str):
    """A read self card the archive took (a week after OK) whose Undo arrived, or None. Read only."""
    return next((d for d in _archive_lines(ap)
                 if d.get("id") == did and d.get("kind") == "self" and d.get("answer") == KEEP), None)


def _drop_archived(ap: str, did: str) -> None:
    """Remove a card's line from the archive. Called under the state lock, which `archive` holds while it writes."""
    p = paths.ap_dir(ap) / "archive.jsonl"
    keep = [d for d in _archive_lines(ap) if d.get("id") != did]
    tmp = p.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(d, ensure_ascii=False) + "\n" for d in keep))
    os.replace(tmp, p)


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
        try:
            print(decision_add(args[1], _load_arg(args[2])))
        except ValueError as e:
            print(f"ap decision add: {e}", file=sys.stderr)
            return 1
    elif len(args) >= 3 and args[0] == "resolve":
        try:
            print(decision_resolve(args[1], args[2], _flag(args, "--answer", ""), _flag(args, "--status", "answered"),
                                   replace="--replace" in args, handling="--handling" in args))
        except AnswerConflict as e:
            print(f"conflict: {e}", file=sys.stderr)
            return 3
    elif len(args) == 3 and args[0] == "handled":
        print(decision_handled(args[1], args[2]))
    elif len(args) >= 3 and args[0] == "keep":
        print(json.dumps(decision_keep(args[1], args[2:])))
    elif len(args) == 4 and args[0] == "attention" and args[3] in ("true", "false"):
        print(decision_attention(args[1], args[2], args[3] == "true"))
    elif len(args) == 2 and args[0] == "unread":
        print(json.dumps(self_unread(state.load(args[1]), config.load_global()["self_review"], datetime.now(timezone.utc))))
    else:
        print("usage: ap decision add <ap> <file> | resolve <ap> <id> --answer X [--status answered|undone] [--replace] [--handling]"
              " | handled <ap> <id> | keep <ap> <id> ... | attention <ap> <id> true|false | unread <ap>",
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
