from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

RESULT_FILES = {"implement": "handback.json", "review": "review.json", "pr": "prloop.json"}


def name_for(ap: str, task: str, role: str) -> str:
    return f"{ap} · {task} · {role}"


def list_all(claude_bin: str) -> list:
    try:
        out = subprocess.run([claude_bin, "agents", "--json", "--all"], capture_output=True, text=True, timeout=60)
        data = json.loads(out.stdout)
        return data if isinstance(data, list) else []
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def find(name: str, all_sessions: list):
    hits = [s for s in all_sessions if s.get("kind") == "background" and s.get("name") == name]
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


def classify(worker: dict, all_sessions: list, run_dir: Path) -> str:
    f = Path(run_dir) / result_file(worker["role"])
    if f.exists() and f.stat().st_mtime >= _epoch(worker.get("started_at", "")):
        return "done"
    s = find(worker["session_name"], all_sessions)
    if s is None:
        return "dead"
    if s.get("state") == "blocked":
        return "blocked"
    if s.get("state") == "done" or not pid_alive(s.get("pid")):
        return "dead"
    return "running"
