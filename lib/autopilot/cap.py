"""The daily dollar cap of a gateway profile: today's raise, the one card per day, and the pause answer.

The cap is soft. At the cap running workers keep going and no new worker starts; one card asks the user to
raise the limit for today or to pause. "Today" is the local calendar day of the computer's clock."""
from __future__ import annotations

import json
import os
from datetime import datetime

from autopilot import config, events, paths

OPTIONS = {"raise50": 50, "raise100": 100}
PAUSE_NOTE = "paused at the daily cap"


def _file():
    return paths.data_root() / "cap-state.json"


def _day(now: datetime = None) -> str:
    now = now or datetime.now().astimezone()
    return now.astimezone().strftime("%Y-%m-%d")


def _blank(day: str) -> dict:
    return {"day": day, "raise_pct": 0, "card": None}


def state(now: datetime = None) -> dict:
    """Today's cap state. A new local day starts blank."""
    day = _day(now)
    try:
        s = json.loads(_file().read_text())
        if s.get("day") == day and isinstance(s.get("raise_pct"), int) and s.get("card") in ("open", "answered", None):
            return {"day": day, "raise_pct": s["raise_pct"], "card": s["card"]}
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


def open_card(now: datetime) -> bool:
    """Open the card once per local day. True when this call opened it."""
    s = state(now)
    if s["card"] is not None:
        return False
    s["card"] = "open"
    _save(s)
    events.append_global("cap_reached", {"day": s["day"]})
    return True


def answer(option: str, now: datetime) -> None:
    if option not in OPTIONS and option != "pause":
        raise ValueError(f"unknown option {option!r}: use raise50, raise100 or pause")
    s = state(now)
    if option in OPTIONS:
        s["raise_pct"] = OPTIONS[option]
    s["card"] = "answered"
    _save(s)
    events.append_global("cap_answered", {"day": s["day"], "option": option})
    if option == "pause":
        _pause_all(now)


def _pause_all(now: datetime) -> None:
    """`ap pause` for every active autopilot, and park its running workers. Nothing resumes by itself."""
    from autopilot import state as ap_state
    for ap in config.active_aps():
        (paths.ap_dir(ap) / "paused").write_text("")
        events.append(ap, "paused", {"reason": "cap"})
        parked = []
        with ap_state.mutate(ap) as st:
            for w in st["workers"]:
                if w.get("parked"):
                    continue
                w["parked"] = {"kind": "cap", "model": None, "percent": None, "resets_at": None, "resume_at": None,
                               "reason": "cap", "note": PAUSE_NOTE, "parked_at": now.isoformat(), "role": w["role"]}
                parked.append(w)
        for w in parked:
            run = paths.ap_dir(ap) / "runs" / w["task"]
            run.mkdir(parents=True, exist_ok=True)
            (run / "parked.json").write_text(json.dumps(w["parked"], indent=1) + "\n")
            events.append(ap, "worker_parked", {"task": w["task"], "role": w["role"], "window": "cap",
                                                "percent": None, "resume_at": None})
