from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

from autopilot import events

# The kernel releases an flock when its holder exits, so a crash or reboot never leaves a stale lock.
_HELD: dict = {}


def held(path: Path):
    """The holder's info while someone holds the lock, else None."""
    path = Path(path)
    if str(path) in _HELD:
        return _read(path)
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return _read(path) or {"label": "unknown"}
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


def _read(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def acquire(path: Path, label: str) -> bool:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if str(path) in _HELD:
        return False
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return False
    os.ftruncate(fd, 0)
    os.write(fd, json.dumps({"pid": os.getpid(), "label": label, "at": events.now_iso()}).encode())
    _HELD[str(path)] = fd
    return True


def release(path: Path) -> None:
    fd = _HELD.pop(str(Path(path)), None)
    if fd is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
