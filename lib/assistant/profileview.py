"""What the pages show about access (design section 12, access on the pages): which profile pays for the workers, the
other profiles to switch to, a key or profile that does not work, today's cap card and pause, and the usage that
matters in that mode. The local page's `/api/state` and the phone data (`phone.build`) use `summary` as it is.

Everything here is a read of what the other modules keep, made with their own readers: `profiles.active` and
`profiles.names` / `load`, `keys.cached_error` (the cached key record: no key command runs), `cap.state`,
`cap.today_limit` and `cap.paused`, `spend.last` (the totals the cap read last: the ledger is never read here) and
v1's `budget.read` (Claude Code's cached usage windows). It runs no process, opens no connection and writes nothing,
so a page request may call it. A ProfileError or CapError becomes text in the block it comes from, never a crash;
so does an ArithmeticError or RecursionError from a profiles, key record or cap read (a hand-edited file: a cap or a
raise too big for a float, a JSON nest too deep), as `<name>: <error type>: <error>` with no path in it. A pause file
that raises one counts as paused, as `cap.paused` counts a pause file it cannot read.
Named `profileview`, not `access`: the design keeps `access.py` for the health checks.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from autopilot import budget as v1_budget

from assistant import cap, keys, profiles, spend
from assistant.config import ConfigError

PAGE_SESSIONS = "subscription"  # page sessions (dispatcher, page answers, refresh) always run on the subscription
PROFILE_KEYS = ("name", "label", "mode", "model", "effort", "daily_cap_usd")
ODD = (ArithmeticError, RecursionError)  # what a read can raise beside ConfigError on a hand-edited file


def _odd(name, e: BaseException) -> str:
    return f"{name}: {type(e).__name__}: {e}"


def summary(data, now: datetime, usage_file) -> dict:
    """`{"access": ..., "usage": ...}` for the data folder `data` at `now` (an aware time at the local offset: its
    calendar day is "today", as the cap counts it), with the usage windows read from `usage_file` (Claude Code's
    `~/.claude.json`)."""
    data = Path(data)
    p, profile_error = _active(data)
    a = access(data, now, p, profile_error)
    return {"access": a, "usage": usage(data, now, usage_file, p, a["cap"].get("limit"))}


def _active(data: Path) -> tuple:
    try:
        return profiles.active(data), None
    except ConfigError as e:  # ProfileError: it names the file and holds profiles.FIX
        return None, str(e)
    except ODD as e:
        try:
            name = profiles.active_name(data)
        except (ConfigError, *ODD):
            name = "the active profile"
        return None, f"{_odd(name, e)}; {profiles.FIX}"


def access(data: Path, now: datetime, p: Optional[dict], profile_error: Optional[str]) -> dict:
    out = {"profile": None if p is None else {k: p.get(k) for k in PROFILE_KEYS}, "profile_error": profile_error,
           "page_sessions": PAGE_SESSIONS, "profiles": [], "broken_profiles": [], "key_error": None}
    try:
        found = profiles.names(data)
    except ConfigError as e:
        found = []
        out["error"] = str(e)
    except ODD as e:
        found = []
        out["error"] = _odd("profiles", e)
    for name in found:
        try:
            q = profiles.load(data, name)
        except ConfigError as e:
            out["broken_profiles"].append({"name": name, "error": str(e)})
            continue
        except ODD as e:
            out["broken_profiles"].append({"name": name, "error": _odd(name, e)})
            continue
        out["profiles"].append({"name": name, "label": q.get("label"), "mode": q.get("mode"),
                                "needs_choice": profiles.needs_choice(data, q)})
    if p is not None and p.get("mode") == "gateway":
        try:
            err = keys.cached_error(data, p)
        except ConfigError as e:
            err = str(e)
        except ODD as e:
            err = _odd(keys.STATE, e)
        if err:  # the exact line a refused start prints, its fix line included (the hold's own, with the name)
            out["key_error"] = cap.refusal({"reason": "key", "error": err, "profile": p.get("name")})
    out["cap"] = cap_block(data, now, p)
    return out


def cap_block(data: Path, now: datetime, p: Optional[dict]) -> dict:
    """Today's cap state: `{day, card, card_id, limit, raise_pct, paused, pause}`, with `error` when the state file
    cannot be used (a symlink, a hand-edited raise too big for a float)."""
    try:
        pz = cap.paused(data)  # a pause file it cannot read counts as paused
    except ODD as e:  # a JSON nest too deep, on some Pythons: paused too
        pz = {"reason": "unknown", "error": _odd(cap.PAUSED, e)}
    out = {"day": cap.day(now), "card": None, "card_id": None, "limit": None, "raise_pct": 0,
           "paused": pz is not None, "pause": pz}
    try:
        s = cap.state(data, now)
        out.update(card=s["card"], card_id=s.get("card_id"), raise_pct=s["raise_pct"],
                   limit=cap.today_limit(data, p, now) if p is not None else None)
    except ConfigError as e:  # CapError
        out["error"] = str(e)
    except ODD as e:
        out["error"] = _odd(cap.STATE, e)
    return out


def usage(data: Path, now: datetime, usage_file, p: Optional[dict], limit) -> dict:
    """The usage by mode. `windows`, `stale` and the credits are the subscription's (on a gateway, the page sessions'
    windows), null when the usage file cannot be read; `gateway` is the remembered count, on a gateway only."""
    b = v1_budget.read(Path(usage_file), now_ms=int(now.timestamp() * 1000))
    ok = b.get("ok") is True
    mode = p.get("mode") if p is not None else None
    return {"mode": mode, "windows": b.get("windows") if ok else None, "stale": b.get("stale") if ok else None,
            "credits_used_usd": b.get("credits_used_usd") if ok else None,
            "credits_limit_usd": b.get("credits_limit_usd") if ok else None,
            "gateway": gateway_usage(data, now, limit) if mode == "gateway" else None}


def gateway_usage(data: Path, now: datetime, limit) -> dict:
    """`{day, counted_at, today, week, month, unknown, unknown_today, limit}` from `spend.last`: `day` is the day
    the count was made. Each total is kept only while its period is still the current one, as `spend.totals` counts
    them (the week from Monday, the month from the 1st): a count from another day leaves `today` and `unknown_today`
    null (not counted yet today), one from an earlier week leaves `week` null, and one from an earlier month leaves
    `month` and `unknown` null."""
    t = spend.last(data)
    out = {"day": None, "counted_at": None, "today": None, "week": None, "month": None, "unknown": None,
           "unknown_today": None, "limit": limit}
    if t is None:
        return out
    out.update(day=t["day"], counted_at=t["at"])
    today = date.fromisoformat(cap.day(now))
    try:
        counted = date.fromisoformat(t["day"])
    except ValueError:  # not a day: none of its totals is this period's
        return out
    if counted > today:  # a count from a later day (the clock moved back): none of its periods is this one
        return out
    if counted >= today - timedelta(days=today.weekday()):
        out["week"] = t["week"]
    if (counted.year, counted.month) == (today.year, today.month):
        out.update(month=t["month"], unknown=t["unknown"])
    if counted == today:
        out.update(today=t["today"], unknown_today=t["unknown_today"])
    return out
