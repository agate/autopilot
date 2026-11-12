from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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


# How a worker's last turn can end on an API error that autopilot acts on.
_LIMIT_TEXT = re.compile(r"\b(spend|usage|session|weekly|5-hour|hourly|daily|monthly)\s+limit\b|\bhit your\b.*\blimit\b"
                         r"|\blimit\b.*\bresets\b", re.IGNORECASE)
_REFUSED_TEXT = re.compile(r"safety guard", re.IGNORECASE)
_RESET = re.compile(r"\bresets\s+(?:(?P<mon>[A-Za-z]{3,9})\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+(?:at\s+)?)?"
                    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>[ap]\.?m\.?)?\s*\((?P<tz>[^()\s]+)\)", re.IGNORECASE)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def api_stop(tail, since: datetime):
    """What the API error a session's transcript ends on means for its worker: {"kind": "limit" (a usage or
    spend limit: wait for the reset) or "gateway_refused" (a gateway rejects this conversation's content), "text",
    "at"}, or None. `tail` is `usage.transcript_tail`. Only an error dated at or after `since` counts: a resumed
    copy repeats the old conversation, its last error included, with the old times."""
    errors = (tail or {}).get("errors") or []
    if not errors:
        return None
    e = errors[-1]
    if e["at"] is None or e["at"] < since:
        return None
    if e["status"] == 429 or e["error"] == "rate_limit" or _LIMIT_TEXT.search(e["text"]):
        kind = "limit"
    elif (e["status"] == 400 or re.search(r"\b400\b", e["text"])) and _REFUSED_TEXT.search(e["text"]):
        kind = "gateway_refused"
    else:
        return None
    return {"kind": kind, "text": e["text"], "at": e["at"]}


def reset_time(text: str, after: datetime):
    """The reset a limit message names ("resets 8:40pm (America/Los_Angeles)", "resets Nov 14, 9am (UTC)"): the
    first such wall time in that zone after `after`, or None when the text names no time and zone it can read."""
    m = _RESET.search(text or "")
    if not m:
        return None
    try:
        zone = ZoneInfo(m["tz"])
    except (ValueError, KeyError, OSError):   # ZoneInfoNotFoundError is a KeyError
        return None
    h, mi = int(m["h"]), int(m["m"] or 0)
    if m["ap"]:
        if not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if m["ap"][0].lower() == "p" else 0)
    if h > 23 or mi > 59:
        return None
    base = after.astimezone(zone)
    try:
        if m["mon"]:
            mon = m["mon"][:3].lower()
            if mon not in _MONTHS:
                return None
            for year in (base.year, base.year + 1):
                at = datetime(year, _MONTHS.index(mon) + 1, int(m["day"]), h, mi, tzinfo=zone)
                if at > base - timedelta(days=1):   # a date already past is next year's
                    return at
            return None
        day = base.date()
        at = datetime(day.year, day.month, day.day, h, mi, tzinfo=zone)
        if at <= base:
            day += timedelta(days=1)
            at = datetime(day.year, day.month, day.day, h, mi, tzinfo=zone)
        return at
    except ValueError:
        return None
