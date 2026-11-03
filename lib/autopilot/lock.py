from __future__ import annotations

import json
import os
from pathlib import Path

from autopilot import events, sessions


def held(path: Path):
    """The live lock holder, or None when free or held by a dead process."""
    try:
        info = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return info if sessions.pid_alive(info.get("pid")) else None


def acquire(path: Path, label: str) -> bool:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and held(path) is None:
        path.unlink()
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as f:
        json.dump({"pid": os.getpid(), "label": label, "at": events.now_iso()}, f)
    return True


def release(path: Path) -> None:
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass
