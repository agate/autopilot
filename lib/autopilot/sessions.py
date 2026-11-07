from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

RESULT_FILES = {"implement": "handback.json", "review": "review.json", "pr": "prloop.json"}


def name_for(ap: str, task: str, role: str) -> str:
    return f"{ap} · {task} · {role}"


def list_all(claude_bin: str):
    """Every session `claude agents` knows, or None when the listing failed (unknown, not empty)."""
    try:
        out = subprocess.run([claude_bin, "agents", "--json", "--all"], capture_output=True, text=True, timeout=60)
        data = json.loads(out.stdout)
        return data if isinstance(data, list) else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def find_by_id(short: str, all_sessions):
    return next((s for s in all_sessions or [] if s.get("id") == short), None)


def wait_turn(claude_bin: str, short: str, timeout_s: float, poll_s: float = 2):
    """Wait for a throwaway background session to end its turn: (session, "") when it did, else (the last listing
    of it or None, why). The listed state is the CLI's own label for how the turn ended (a reply of FAILED has been
    listed as done, blocked and failed), so any state but "working" ends the wait once the session is idle or its
    process is gone; whether the work succeeded is for the transcript to say. A permission prompt ends the wait at
    once, since nobody answers a background session."""
    deadline = time.monotonic() + timeout_s
    s = None
    while time.monotonic() < deadline:
        s = find_by_id(short, list_all(claude_bin) or [])
        if s and s.get("status") == "waiting":
            return s, "stopped at a permission prompt"
        if s and s.get("state") not in (None, "working") and s.get("status") in (None, "idle"):
            return s, ""
        time.sleep(poll_s)
    return s, "did not finish in time"


def find(name: str, all_sessions):
    hits = [s for s in all_sessions or [] if s.get("kind") == "background" and s.get("name") == name]
    return max(hits, key=lambda s: s.get("startedAt") or 0) if hits else None


def pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False
    return True


def result_file(role: str) -> str:
    return RESULT_FILES[role]


def _epoch(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return 0.0


def classify(worker: dict, all_sessions, run_dir: Path) -> str:
    f = Path(run_dir) / result_file(worker["role"])
    if f.exists() and f.stat().st_mtime >= _epoch(worker.get("started_at", "")):
        return "done"
    if worker.get("parked"):
        return "parked"  # it stopped itself on purpose: not died, not done, never resumed early
    if all_sessions is None:
        return "unknown"
    s = find_by_id(worker.get("short_id"), all_sessions) or find(worker["session_name"], all_sessions)
    if s is None:
        return "dead"
    if s.get("state") == "blocked":
        return "blocked"
    if s.get("state") == "done" or (s.get("pid") is not None and not pid_alive(s.get("pid"))):
        return "dead"
    return "running"
