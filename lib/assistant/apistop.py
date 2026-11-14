"""What the end of a worker's transcript means: did its last turn stop on an API error that ap2 acts on?

Claude Code writes an API error as an `assistant` (or `system`) line flagged `isApiErrorMessage`, from the model
`<synthetic>`, with an `error` code and the HTTP status in `apiErrorStatus`. Two of them matter to a worker:

- a usage or spend limit (429, `rate_limit`, or a message naming a limit): the session sits idle until the limit
  resets, so the worker is parked until the reset the message names (`reset_time`), else for `backoff(n)`;
- a gateway's safety guard refusing the conversation (a 400 that says `safety guard`): the guard reads the whole
  conversation, so every later call of it is refused too, on a resume as well.

`tail` reads only the end of the transcript (TAIL_BYTES) under the projects folder it is given, and never raises:
a transcript that is missing or cannot be read is None ("no API error seen"). Standard library only. Ported from
v1 (`usage.transcript_tail`, `sessions.api_stop` and `reset_time`, the back-off of `workers._limit_park`).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from assistant import transcripts

TAIL_BYTES = 65536  # how much of a transcript's end is read
BACKOFF = timedelta(minutes=30)  # a limit with no readable reset: wait this long, doubling per limit park in a row
BACKOFF_MAX = timedelta(hours=4)
_BACKOFF_STEPS = 10  # 30 minutes doubled 9 times is far past BACKOFF_MAX: larger counts need no arithmetic

_LIMIT_TEXT = re.compile(r"\b(spend|usage|session|weekly|5-hour|hourly|daily|monthly)\s+limit\b|\bhit your\b.*\blimit\b"
                         r"|\blimit\b.*\bresets\b", re.IGNORECASE)
_REFUSED_TEXT = re.compile(r"safety guard", re.IGNORECASE)
_RESET = re.compile(r"\bresets\s+(?:(?P<mon>[A-Za-z]{3,9})\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+(?:at\s+)?)?"
                    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>[ap]\.?m\.?)?\s*\((?P<tz>[^()\s]+)\)", re.IGNORECASE)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def _when(text) -> Optional[datetime]:
    """A transcript line's `timestamp`, or None. One with no zone is read as local time; one that cannot be put in
    the local zone (year 1, say) is None."""
    if not isinstance(text, str) or not text:
        return None
    try:
        at = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return at if at.tzinfo else at.astimezone()
    except (ValueError, OverflowError):
        return None


def _error(e: dict) -> Optional[dict]:
    """An API error line as {"at", "text", "error", "status"}, or None for any other line. Any one of the three
    marks (`isApiErrorMessage: true`, the model `<synthetic>`, a non-empty string `error`) counts, so a change to
    one of them is still read."""
    msg = e.get("message") if isinstance(e.get("message"), dict) else {}
    err = e.get("error") if isinstance(e.get("error"), str) and e.get("error") else None
    if not (e.get("isApiErrorMessage") is True or msg.get("model") == "<synthetic>" or err):
        return None
    content = msg.get("content")
    if isinstance(content, list):
        parts = [c.get("text") for c in content if isinstance(c, dict) and isinstance(c.get("text"), str)]
    else:
        parts = [content] if isinstance(content, str) else []
    status = e.get("apiErrorStatus")
    return {"at": _when(e.get("timestamp")), "text": " ".join(p for p in parts if p).strip(), "error": err,
            "status": status if isinstance(status, int) and not isinstance(status, bool) else None}


def tail(projects, session_id) -> Optional[dict]:
    """How the session's transcript (`transcripts.find` under `projects`) ends: {"errors": the API error lines after
    its last normal assistant reply, oldest first; "reply_at": when that reply was written, or None}. None when there
    is no transcript or it cannot be read. Only the last TAIL_BYTES are read (the first line read is dropped when it
    was cut), and a line that does not parse is skipped."""
    try:
        path = transcripts.find(projects, session_id) if session_id else None
        if path is None:
            return None
        h = transcripts.open_regular(path)  # never waits: a FIFO swapped in after `find` is no transcript
        if h is None:
            return None
        with h:
            h.seek(0, 2)
            start = max(0, h.tell() - TAIL_BYTES)
            h.seek(start)
            lines = h.read().decode("utf-8", "replace").split("\n")  # not splitlines: U+2028 stays raw in a line
    except (OSError, TypeError, ValueError):  # TypeError: no projects folder; ValueError: not a session id
        return None
    if start:
        lines = lines[1:]  # cut off at its start
    errors, reply_at = [], None
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict) or e.get("type") not in ("assistant", "system"):
            continue
        err = _error(e)
        if err:
            errors.append(err)
        elif e.get("type") == "assistant":
            errors, reply_at = [], _when(e.get("timestamp"))  # a normal reply after an error: it recovered
    return {"errors": errors, "reply_at": reply_at}


def stop(t: Optional[dict], since: datetime, until: Optional[datetime] = None) -> Optional[dict]:
    """What the API error a transcript ends on means: {"kind": "limit" (a usage or spend limit: wait for the reset)
    or "gateway_refused" (a gateway's safety guard refuses this conversation), "text", "at"}, or None. `t` is
    `tail()`. Only the last error counts, and only when it is dated at or after `since`: a resumed copy repeats the
    old conversation, its last error included, with the old times. With `until`, one dated after it is ignored too:
    an error from the future is not a real one (a process the worker left can write any time)."""
    errors = (t or {}).get("errors") or []
    if not errors:
        return None
    e = errors[-1]
    if e["at"] is None or e["at"] < since or (until is not None and e["at"] > until):
        return None
    if e["status"] == 429 or e["error"] == "rate_limit" or _LIMIT_TEXT.search(e["text"]):
        kind = "limit"
    elif (e["status"] == 400 or re.search(r"\b400\b", e["text"])) and _REFUSED_TEXT.search(e["text"]):
        kind = "gateway_refused"
    else:
        return None
    return {"kind": kind, "text": e["text"], "at": e["at"]}


def reset_time(text, after: datetime) -> Optional[datetime]:
    """The reset a limit message names ("resets 8:40pm (America/Los_Angeles)", "resets Nov 14, 9am (UTC)"): the
    first such wall time in that zone after `after` (a dated one already a day past is next year's), or None when
    the text names no time and zone it can read, or the reset would be past the last date a datetime holds."""
    m = _RESET.search(text if isinstance(text, str) else "")
    if not m:
        return None
    try:
        zone = ZoneInfo(m["tz"])
    except (ValueError, KeyError, OSError):  # ZoneInfoNotFoundError is a KeyError; a path-like key is a ValueError
        return None
    h, mi = int(m["h"]), int(m["m"] or 0)
    if m["ap"]:
        if not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if m["ap"][0].lower() == "p" else 0)
    if h > 23 or mi > 59:
        return None
    try:
        base = after.astimezone(zone)
        if m["mon"]:
            mon = m["mon"][:3].lower()
            if mon not in _MONTHS:
                return None
            for year in (base.year, base.year + 1):
                at = datetime(year, _MONTHS.index(mon) + 1, int(m["day"]), h, mi, tzinfo=zone)
                if at > base - timedelta(days=1):  # a date already past is next year's
                    return at
            return None
        day = base.date()
        at = datetime(day.year, day.month, day.day, h, mi, tzinfo=zone)
        if at <= base:
            day += timedelta(days=1)
            at = datetime(day.year, day.month, day.day, h, mi, tzinfo=zone)
        return at
    except (ValueError, OverflowError):  # no such day (Feb 30), or past the last date
        return None


def backoff(n: int) -> timedelta:
    """How long the `n`-th limit park in a row waits when its message names no readable reset: 30 minutes, doubling
    each time, at most 4 hours. A count under 1 is the first."""
    n = min(max(int(n), 1), _BACKOFF_STEPS)
    return min(BACKOFF * 2 ** (n - 1), BACKOFF_MAX)
