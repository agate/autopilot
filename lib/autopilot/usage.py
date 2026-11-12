from __future__ import annotations

import json
from datetime import datetime
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


TAIL_BYTES = 65536


def _when(text):
    try:
        at = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return at if at.tzinfo else at.astimezone()


def _api_error(e: dict):
    """An API error line as {"at", "text", "error", "status"}, or None for any other line. Claude Code writes an
    API error as an assistant line flagged isApiErrorMessage (model <synthetic>), with an `error` field; any of
    the three counts, so a change to one of them is still read."""
    msg = e.get("message") if isinstance(e.get("message"), dict) else {}
    err = e.get("error") if isinstance(e.get("error"), str) and e.get("error") else None
    if not (e.get("isApiErrorMessage") is True or msg.get("model") == "<synthetic>" or err):
        return None
    content = msg.get("content")
    parts = [c.get("text", "") for c in content if isinstance(c, dict)] if isinstance(content, list) else [str(content or "")]
    status = e.get("apiErrorStatus")
    return {"at": _when(e.get("timestamp")) if e.get("timestamp") else None,
            "text": " ".join(x for x in parts + [str(e.get("content") or "")] if x).strip(),
            "error": err, "status": status if isinstance(status, int) and not isinstance(status, bool) else None}


def transcript_tail(session_id: str):
    """How a session's transcript ends: {"errors": the API errors after its last normal reply, oldest first;
    "reply_at": when that reply was written, or None}. None when the session has no transcript. Only the last
    TAIL_BYTES are read, and a line that does not parse is skipped."""
    f = find_transcript(session_id)
    if not f:
        return None
    try:
        with open(f, "rb") as h:
            h.seek(0, 2)
            start = max(0, h.tell() - TAIL_BYTES)
            h.seek(start)
            lines = h.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    if start:
        lines = lines[1:]   # the first line read is cut off
    errors, reply_at = [], None
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict) or e.get("type") not in ("assistant", "system"):
            continue
        err = _api_error(e)
        if err:
            errors.append(err)
        elif e.get("type") == "assistant":
            errors, reply_at = [], _when(e.get("timestamp"))   # a normal reply after an error: recovered
    return {"errors": errors, "reply_at": reply_at}


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
