"""One gate run (design 7.1 to 7.4): due routine slots, watches by interval, the heartbeat and restore-point prune.

A run is short and uses no AI: it reads files, decides, makes cards through the store, writes `<data>/gate.json`
and exits. It starts no process, makes no network call and writes only inside the data folder. One global lock,
`<data>/.gate.lock`, keeps a second run from doing anything while one is going.

Per routine (7.3): find the latest slot; when it is newer than `last_slot`, make the run's card first (keyed
`<routine>@<slot>`), then write `last_slot`. A run that dies in between leaves the card; the next run finds it by
its key and only writes `last_slot`.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from assistant import cards, routines
from assistant.cards import CardError
from assistant.config import ConfigError
from assistant.store import Store, write_json

try:
    import fcntl
except ImportError:  # not POSIX: one run at a time is then the caller's job
    fcntl = None

LOCK = ".gate.lock"
HEARTBEAT = "gate.json"
LOG = "gate.log"
LOG_MAX_BYTES = 1_000_000  # then gate.log becomes gate.log.1 (one old file is kept)
WATCHES = "watches.json"
EVERY = re.compile(r"([1-9][0-9]{0,3})(m|h)\Z", re.ASCII)
BY = "gate"


@contextlib.contextmanager
def _lock(data: Path):
    """Yield True when this run holds the gate lock, False when another run holds it."""
    if fcntl is None:
        yield True
        return
    try:
        fd = os.open(str(data / LOCK), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as e:
        raise ConfigError(f"cannot open the gate lock {data / LOCK}: {e}") from e
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)  # closing releases the lock


class Log:
    """`<data>/gate.log`: every line starts with the run's UTC time, `2026-11-10T14:30:05Z <message>`."""

    def __init__(self, data: Path, now: datetime):
        self.path = data / LOG
        self.prefix = cards.stamp(now)
        try:
            if self.path.stat().st_size > LOG_MAX_BYTES:
                os.replace(self.path, data / (LOG + ".1"))
        except FileNotFoundError:
            pass

    def __call__(self, message: str) -> None:
        line = " ".join(str(message).split())  # one line, whatever the message holds
        with open(self.path, "a", encoding="utf-8", errors="backslashreplace") as f:
            f.write(f"{self.prefix} {line}\n")


# -- plain words for a routine's run card (design 3.7) -------------------------------------------------------------

def _age(late_by: timedelta, lang: str) -> str:
    minutes = max(0, int(late_by.total_seconds() // 60))
    hours, minutes = divmod(minutes, 60)
    if lang == "zh":
        return f"{hours} 小时 {minutes} 分钟" if hours else f"{minutes} 分钟"
    return f"{hours} h {minutes} min" if hours else f"{minutes} min"


def _words(lang: str, r: routines.Routine, d: routines.Due) -> dict:
    """The what / where / do lines in `lang`. Built in for English and Chinese; any other language gets the
    English lines (the gate has no AI to write them)."""
    base = lang.split("-")[0]
    day, time = d.slot.split("T")
    if base == "zh":
        age = _age(d.late_by, "zh")
        what = f"例行任务「{r.title}」：{day} {time} 这一次（{r.tz_name}）。"
        where = {"run": "闸门按时启动了它；在有 worker 做完之前，它在「运行中」。",
                 "late": f"这一次错过了；它晚了 {age} 才启动，missed_run（{r.missed_run}）允许补跑一次。"
                         f"在有 worker 做完之前，它在「运行中」。",
                 "skip": f"已跳过：这一次错过了 {age}，missed_run（{r.missed_run}）不允许这么晚补跑。"}[d.action]
        do = ("不用做什么；下一次照常运行。如果还需要这一次，请手动运行。" if d.action == "skip"
              else "不用做什么；它会自己运行。")
        return {"what": what, "where": where, "do": do}
    age = _age(d.late_by, "en")
    what = f"Routine “{r.title}” for its {time} slot on {day} ({r.tz_name})."
    where = {"run": "The gate started it on time; it stays in Running until a worker finishes it.",
             "late": f"The slot was missed; it started {age} late, once, as missed_run ({r.missed_run}) allows. "
                     f"It stays in Running until a worker finishes it.",
             "skip": f"Skipped: the slot was missed by {age}, and missed_run ({r.missed_run}) does not run it "
                     f"that late."}[d.action]
    do = ("Nothing; the next slot runs as usual. Run it by hand if you still need this one." if d.action == "skip"
          else "Nothing; it runs on its own.")
    return {"what": what, "where": where, "do": do}


def run_card(r: routines.Routine, d: routines.Due, languages: tuple) -> dict:
    """The card for one routine run (kind `routine`), keyed `<routine>@<slot>`. A skipped run goes straight to
    Done, marked `skipped`, so History shows it."""
    words = {lang: _words(lang, r, d) for lang in languages}
    return {
        "kind": "routine", "lane": "done" if d.action == "skip" else "running", "feed": "routines",
        "links": [], "pri": "P2", "origin": "source", "title": r.title,
        "key": f"{r.name}@{d.slot}", "routine": r.name, "slot": d.slot, "slot_tz": r.tz_name,
        "slot_at": cards.stamp(d.at), "late": d.action != "run", "skipped": d.action == "skip",
        "late_minutes": int(d.late_by.total_seconds() // 60),
        "plain": {line: {lang: words[lang][line] for lang in languages} for line in cards.PLAIN_LINES},
    }


# -- watches (design 4.3, 7.2) ------------------------------------------------------------------------------------

def parse_every(text) -> timedelta:
    m = EVERY.match(text) if isinstance(text, str) else None
    if m is None:
        raise ValueError(f"every must be a number of minutes or hours such as 5m, 15m or 1h, not {text!r}")
    n = int(m.group(1))
    return timedelta(minutes=n) if m.group(2) == "m" else timedelta(hours=n)


def _read_watch_state(data: Path) -> dict:
    path = data / WATCHES
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise ConfigError(f"{path}: cannot read the watch times: {e}") from e
    return state if isinstance(state, dict) else {}


def _parse_time(text) -> Optional[datetime]:
    if text is None:
        return None
    try:
        return cards.parse_stamp(text)
    except (TypeError, ValueError):
        raise ValueError(f"last_check_at must be a UTC time such as 2026-11-10T14:30:05Z or null, not {text!r}")


def due_watches(data: Path, watches, now: datetime):
    """The watches due at `now` (`now - last_check_at >= every`), as `(names, errors)`; records `last_check_at`
    for each due one in `<data>/watches.json`. Each watch is `{name, every, last_check_at?}`; the time saved in
    watches.json wins when it is later. The gate does not read the sources itself; the caller does."""
    if not watches:
        return [], []
    state = _read_watch_state(data)
    found, errors = [], []
    for w in watches:
        name = w.get("name") if isinstance(w, dict) else None
        try:
            if not (isinstance(name, str) and name.strip()):
                raise ValueError(f"a watch needs a name, not {name!r}")
            every = parse_every(w.get("every"))
            given = _parse_time(w.get("last_check_at"))
            saved = state.get(name, {}).get("last_check_at") if isinstance(state.get(name), dict) else None
            try:
                saved = _parse_time(saved)
            except ValueError:
                saved = None  # a damaged saved time: the watch is due now and the time is written again
            last = max((t for t in (given, saved) if t is not None), default=None)
        except ValueError as e:
            errors.append({"watch": name if isinstance(name, str) else None, "error": str(e)})
            continue
        if last is None or now - last >= every:
            found.append(name)
            state[name] = {"last_check_at": cards.stamp(now)}
    if found:
        write_json(data / WATCHES, state)
    return found, errors


# -- the run ------------------------------------------------------------------------------------------------------

def run(data, now: datetime, watches=(), store: Optional[Store] = None) -> dict:
    """One gate run at `now` (an aware datetime). Returns what it found, also written to `<data>/gate.json`; a run
    that finds the lock held returns `{"locked": true, ...}` and does nothing. Raises ConfigError when the data
    folder or config.yaml is not usable."""
    data = Path(data)
    if now.tzinfo is None:
        raise ValueError("now must be an aware datetime")
    with _lock(data) as held:
        if not held:
            return {"locked": True, "lock": str(data / LOCK)}
        try:
            return _run(data, now, watches, store)
        except OSError as e:
            raise ConfigError(f"the gate cannot use the data folder {data}: {e}") from e


def _run(data: Path, now: datetime, watches, store: Optional[Store]) -> dict:
    log = Log(data, now)
    store = store if store is not None else Store(data, clock=lambda: now)
    found, errors = [], []

    loaded, bad = routines.load_all(data)
    for e in bad:
        errors.append(e)
        log(f"routine {e['routine']}: refused: {e['error']}")

    by_key = None  # the routine cards, read once, only when a slot is due
    for r in loaded:
        try:
            d = routines.due(r, now)
            if d is None:
                continue
            key = f"{r.name}@{d.slot}"
            if d.action == "start":
                routines.write_last_slot(r.path, d.slot, write_json)
                found.append({"routine": r.name, "slot": d.slot, "key": key, "action": "start", "card": None,
                              "late_minutes": int(d.late_by.total_seconds() // 60)})
                log(f"routine {r.name}: new; slots count from after {d.slot}")
                continue
            if by_key is None:
                by_key = {c.get("key"): c for c in store.list(kind="routine")}
            card = by_key.get(key)
            if card is None:
                card = store.add(run_card(r, d, store.languages), by=BY)
                by_key[key] = card
                action = d.action
            else:
                action = "exists"
            routines.write_last_slot(r.path, d.slot, write_json)
        except (routines.RoutineError, CardError, OSError) as e:
            errors.append({"routine": r.name, "file": str(r.path), "error": str(e)})
            log(f"routine {r.name}: error: {e}")
            continue
        found.append({"routine": r.name, "slot": d.slot, "key": key, "action": action, "card": card["id"],
                      "late_minutes": int(d.late_by.total_seconds() // 60)})
        log(f"routine {r.name}: slot {d.slot}: {action} (card {card['id']})")

    due_names, watch_errors = due_watches(data, list(watches), now)
    errors.extend(watch_errors)
    for e in watch_errors:
        log(f"watch {e['watch']}: refused: {e['error']}")
    if due_names:
        log(f"watches due: {', '.join(due_names)}")

    try:
        pruned = store.prune(now)
    except CardError as e:
        pruned = []
        errors.append({"prune": True, "error": str(e)})
        log(f"prune: error: {e}")
    if pruned:
        log(f"pruned {len(pruned)} restore point(s) older than 30 days")

    out = {"last_run_at": cards.stamp(now), "routines": found, "watches_due": due_names, "pruned": pruned,
           "errors": errors}
    write_json(data / HEARTBEAT, out)
    log(f"run: {len(found)} routine slot(s), {len(due_names)} watch(es) due, {len(pruned)} pruned, "
        f"{len(errors)} error(s)")
    return out
