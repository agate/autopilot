"""What running workers hold, across every project: port ranges, single-use resources and the worker cap.

The record of every worker (running or parked) is one file, `<data>/workers.json`, changed only while
`<data>/.workers.lock` is held, so two starts never take the same ports or the same resource. Port ranges and
the start plan reuse v1 (`lib/autopilot/workers.py` `ports` / `next_slot`, `lib/autopilot/nextwork.py`
`plan_starts`).
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

from autopilot import nextwork
from autopilot import workers as v1_workers

from assistant.store import write_json

try:
    import fcntl
except ImportError:  # not POSIX: one process at a time is then the caller's job
    fcntl = None

FILE = "workers.json"
LOCK = ".workers.lock"
MAX_WORKERS = 5  # v1's default: running and parked workers across all projects
PORT_BASE = 7200  # v1 starts at 7100; v2 starts higher so the two never hand out the same range


class WorkerError(RuntimeError):
    """A worker cannot be started, found or changed. The message says why."""


def ports(slot: int, base: int = PORT_BASE) -> list:
    return v1_workers.ports(base, slot)


def next_ports(records: list, base: int = PORT_BASE) -> list:
    """The first free range of 10 ports: a gap left by an ended worker is filled first."""
    return ports(v1_workers.next_slot(records, base), base)


def holder(records: list, needs, skip=None):
    """The record that holds one of `needs`, and that resource; (None, None) when all are free."""
    for r in records:
        if r is skip:
            continue
        for n in needs or []:
            if n in (r.get("needs") or []):
                return r, n
    return None, None


def check_start(records: list, needs, max_workers: int = MAX_WORKERS, skip=None) -> None:
    """Raise WorkerError when a new worker may not start: the cap is reached, or a resource it needs is held."""
    others = [r for r in records if r is not skip]
    if len(others) >= max_workers:
        raise WorkerError(f"{len(others)} workers already hold a slot (max_workers is {max_workers}); end or "
                          "finish one first")
    r, n = holder(records, needs, skip)
    if r is not None:
        raise WorkerError(f"resource {n} is held by {r['session_name']}; it starts when that worker ends")


def plan(candidates: list, records: list, max_workers: int = MAX_WORKERS) -> list:
    """Pick which of `candidates` ([{project, card, needs}], each ready to start) start now: round-robin across
    projects (in the order each project first appears), never past the cap, and never two at once that need the
    same resource or one a running worker holds. Returns `[(project, card)]`. v1's `plan_starts`, given the cards
    as todo tasks; within a project the order is v1's (`nextwork.ready` sorts the tasks, here by card id), not the
    given order."""
    order, snaps = [], {}
    for c in candidates:
        if c["project"] not in snaps:
            snaps[c["project"]] = {"tasks": [], "workers": [], "decisions": []}
            order.append(c["project"])
        snaps[c["project"]]["tasks"].append({"id": c["card"], "status": "todo", "needs": list(c.get("needs") or [])})
    busy = {r.get("card") for r in records}
    for st in snaps.values():
        st["tasks"] = [t for t in st["tasks"] if t["id"] not in busy]
    return nextwork.plan_starts([(p, {}, snaps[p]) for p in order], records, max_workers, None)


class Registry:
    """`<data>/workers.json`: `{"workers": [record, ...]}`."""

    def __init__(self, data):
        self.data = Path(data)
        self.path = self.data / FILE

    @contextlib.contextmanager
    def locked(self):
        """Hold the lock and yield the records; what the block leaves in the list is written back, however the
        block ends (normally or by any exception, Ctrl-C included), so every change already made reaches disk.
        An exception is re-raised after the write. Callers change the list only once what it records is true.
        When the block raised and the write fails too, the block's own exception is the one raised (the write
        error is noted on it); the write error is raised only when the block itself succeeded."""
        self.data.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.data / LOCK), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            records = self._read()
            before = json.dumps(records, sort_keys=True)

            def write_back():
                if json.dumps(records, sort_keys=True) != before:
                    write_json(self.path, {"workers": records})

            try:
                yield records
            except BaseException as first:
                try:
                    write_back()
                except Exception as second:  # noqa: BLE001 - the block's own error is the one that matters
                    if hasattr(first, "add_note"):  # Python 3.11 and later
                        first.add_note(f"the worker list could not be written back: {second!r}")
                raise
            write_back()
        finally:
            os.close(fd)

    def read(self) -> list:
        return self._read()

    def _read(self) -> list:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            raise WorkerError(f"{self.path}: cannot read the worker list: {e}") from e
        if not (isinstance(data, dict) and isinstance(data.get("workers"), list)
                and all(isinstance(r, dict) for r in data["workers"])):
            raise WorkerError(f"{self.path}: not a worker list (expected {{\"workers\": [...]}})")
        return data["workers"]
