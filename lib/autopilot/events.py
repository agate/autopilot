from __future__ import annotations

import json
from datetime import datetime, timezone

from autopilot import paths


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _path(name: str):
    return paths.ap_dir(name) / "events.jsonl"


def append(name: str, type: str, data: dict) -> int:
    p = _path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"at": now_iso(), "type": type, "data": data}, ensure_ascii=False) + "\n"
    with open(p, "ab") as f:
        f.write(line.encode("utf-8"))
        return f.tell()


def since(name: str, offset: int) -> tuple:
    p = _path(name)
    if not p.exists():
        return [], 0
    with open(p, "rb") as f:
        f.seek(0, 2)
        end = f.tell()
        if offset > end:
            offset = 0
        f.seek(offset)
        raw = f.read()
    out = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out, end
