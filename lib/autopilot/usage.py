from __future__ import annotations

import json
from pathlib import Path

from autopilot import events

KEYS = (("input", "input_tokens"), ("output", "output_tokens"), ("cache_read", "cache_read_input_tokens"),
        ("cache_write", "cache_creation_input_tokens"))


def zero() -> dict:
    return {k: 0 for k, _ in KEYS}


def add(a: dict, b: dict) -> dict:
    return {k: a.get(k, 0) + (b or {}).get(k, 0) for k, _ in KEYS}


def transcript_tokens(path: Path) -> dict:
    """Token totals of one session transcript. Streaming writes several lines per request with
    growing counts, so only the last usage seen for each requestId counts."""
    per_req = {}
    try:
        lines = Path(path).read_text(errors="replace").splitlines()
    except OSError:
        return zero()
    for raw in lines:
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        u = (d.get("message") or {}).get("usage") if isinstance(d, dict) else None
        if not u or d.get("type") != "assistant":
            continue
        per_req[d.get("requestId") or id(d)] = {k: int(u.get(src) or 0) for k, src in KEYS}
    out = zero()
    for v in per_req.values():
        out = add(out, v)
    return out


def find_transcript(session_id: str):
    if not session_id:
        return None
    hits = list((Path.home() / ".claude" / "projects").glob(f"*/{session_id}.jsonl"))
    return hits[0] if hits else None


def session_tokens(session_id: str) -> dict:
    p = find_transcript(session_id)
    return transcript_tokens(p) if p else zero()


def autopilot_tokens(ap: str, live_sessions: list) -> dict:
    """Ended workers (from their worker_ended events) plus the live ones."""
    total = zero()
    evs, _ = events.since(ap, 0)
    for e in evs:
        if e.get("type") == "worker_ended":
            total = add(total, (e.get("data") or {}).get("tokens"))
    for sid in live_sessions:
        total = add(total, session_tokens(sid))
    return total
