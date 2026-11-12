"""The daily dollar cap of a gateway profile: today's raise, the one card per day, and the pause answer.

The cap is soft. At the cap running workers keep going and no new worker starts; one card asks the user to
raise the limit for today (or, when the cost cannot be priced, to accept it) or to pause. "Today" is the local calendar day of the computer's clock."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime

from autopilot import config, events, paths

OPTIONS = {"raise50": 50, "raise100": 100}
ACCEPT_UNKNOWN = "accept_unknown"   # today's cost that cannot be priced counts as $0 for the cap until midnight
PAUSE_NOTE = "paused at the daily cap"
UNKNOWN_FIX = ("Add the model's list price under `prices` in the global config (`ap config set prices=...`, same "
               "shape as lib/autopilot/prices.json), or accept today's unknown cost.")


def _file():
    return paths.data_root() / "cap-state.json"


def _day(now: datetime = None) -> str:
    now = now or datetime.now().astimezone()
    return now.astimezone().strftime("%Y-%m-%d")


def _blank(day: str) -> dict:
    return {"day": day, "raise_pct": 0, "card": None}


def state(now: datetime = None) -> dict:
    """Today's cap state. A new local day starts blank. `opened` is {limit, cause} of the last time the card
    opened; `accept_unknown` is True once today's unknown cost was accepted."""
    day = _day(now)
    try:
        s = json.loads(_file().read_text())
        if s.get("day") == day and isinstance(s.get("raise_pct"), int) and s.get("card") in ("open", "answered", None):
            out = {"day": day, "raise_pct": s["raise_pct"], "card": s["card"]}
            if isinstance(s.get("opened"), dict):
                out["opened"] = s["opened"]
            if s.get("accept_unknown") is True:
                out["accept_unknown"] = True
            return out
    except (OSError, ValueError, AttributeError):
        pass
    return _blank(day)


def _save(s: dict) -> None:
    f = _file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(s))
    os.replace(tmp, f)


def today_limit(p: dict, now: datetime) -> float:
    return p["daily_cap_usd"] * (1 + state(now)["raise_pct"] / 100)


def unknown_accepted(now: datetime) -> bool:
    return state(now).get("accept_unknown") is True


def open_card(now: datetime, limit: float = None, cause: str = None) -> bool:
    """Open the card: once per local day, and again whenever the cap holds at another limit or for another
    cause than the one it last opened for (spend crossed a raised limit; or spend crossed the limit after
    today's unknown cost was accepted). True when this call opened it."""
    s = state(now)
    if s["card"] == "open":
        return False
    seen = {"limit": limit, "cause": cause}
    if s["card"] == "answered" and s.get("opened") == seen:
        return False
    s["card"], s["opened"] = "open", seen
    _save(s)
    events.append_global("cap_reached", {"day": s["day"], "limit": limit, "cause": cause})
    return True


def close_card(now: datetime) -> None:
    """The cap no longer holds and nobody answered: take the card back so it can open again later today."""
    s = state(now)
    if s["card"] == "open":
        s["card"] = None
        _save(s)


def answer(option: str, now: datetime) -> None:
    if option not in OPTIONS and option not in ("pause", ACCEPT_UNKNOWN):
        raise ValueError(f"unknown option {option!r}: use raise50, raise100, accept_unknown or pause")
    s = state(now)
    if option in OPTIONS:
        s["raise_pct"] += OPTIONS[option]   # a second raise today adds to the first
    if option == ACCEPT_UNKNOWN:
        s["accept_unknown"] = True
    s["card"] = "answered"
    _save(s)
    events.append_global("cap_answered", {"day": s["day"], "option": option})
    if option == "pause":
        _pause_all(now)


MARK = "cap"   # the content of a `paused` file written by the cap pause: the gate retries its failed stops


def _pause_all(now: datetime) -> None:
    """`ap pause` for every autopilot not stopped (also one the user paused by hand), and stop and park its
    running workers. Nothing resumes by itself; a worker resumed later continues its own conversation.
    Every pause flag is written first, and a session that will not stop never blocks the rest."""
    aps = [ap for ap in config.list_aps() if config.status_of(ap) != "stopped"]
    for ap in aps:
        flag = paths.ap_dir(ap) / "paused"
        if not flag.exists():
            events.append(ap, "paused", {"reason": "cap"})
        flag.write_text(MARK)
    for ap in aps:
        stop_and_park(ap, now)


def retry_stops(now: datetime) -> None:
    """Called on every gate tick: an autopilot paused by the cap that still has a running worker (its stop
    failed) gets the stop again, until it works."""
    for ap in config.list_aps():
        try:
            marked = (paths.ap_dir(ap) / "paused").read_text() == MARK
        except OSError:
            continue
        if marked:
            stop_and_park(ap, now)


def stop_and_park(ap: str, now: datetime) -> None:
    """Stop every running (not parked) worker of `ap`, then record the ones that stopped as parked. A worker
    whose stop failed stays unparked, so it still shows as running, and the next gate tick tries again."""
    from autopilot import state as ap_state, workers
    gcfg = config.load_global()
    try:
        running = [w for w in ap_state.load(ap)["workers"] if not w.get("parked")]
    except ap_state.StateError as e:
        events.append(ap, "cap_stop_failed", {"error": str(e)})
        return
    stopped = []
    for w in running:   # outside the state lock: stopping a session can be slow
        try:
            workers._settle(w["short_id"])
            subprocess.run([workers.claude_bin(gcfg), "stop", w["short_id"]], capture_output=True, timeout=60)
            stopped.append(w)
        except (OSError, subprocess.SubprocessError) as e:
            events.append(ap, "cap_stop_failed", {"task": w["task"], "role": w["role"], "error": str(e)})
    rec = {"kind": "cap", "model": None, "percent": None, "resets_at": None, "resume_at": None,
           "reason": "cap", "note": PAUSE_NOTE, "parked_at": now.isoformat()}
    keys = {(w["task"], w["role"]) for w in stopped}
    with ap_state.mutate(ap) as st:
        for w in st["workers"]:
            if (w["task"], w["role"]) in keys and not w.get("parked"):
                w["parked"] = dict(rec, role=w["role"])
    for w in stopped:
        run = paths.ap_dir(ap) / "runs" / w["task"]
        run.mkdir(parents=True, exist_ok=True)
        (run / "parked.json").write_text(json.dumps(dict(rec, role=w["role"]), indent=1) + "\n")
        events.append(ap, "worker_parked", {"task": w["task"], "role": w["role"], "window": "cap",
                                            "percent": None, "resume_at": None})
