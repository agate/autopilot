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

from assistant import cards, engine, routines
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
# Ends the watch error for a time ahead of now, the one error after which the watch still runs (the routine
# error for a last_slot ahead of now says it too).
WENT_BACK = "the clock may have gone back"


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
        except OSError as e:  # e.g. ENOLCK on a network share: one ConfigError line, not a traceback
            raise ConfigError(f"cannot lock the gate {data / LOCK}: {e}") from e
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


def _read_watch_state(data: Path):
    """The saved watch times, as `(state, error)`. A damaged or unreadable watches.json is treated like a damaged
    saved time: the run goes on from `{}` (every watch is due) and `error` says why; the file is written again,
    atomically, when a watch is due."""
    path = data / WATCHES
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as e:  # UnicodeDecodeError is a ValueError
        return {}, {"watches": str(path), "error": f"{path}: cannot read the watch times: {e}"}
    if not isinstance(state, dict):
        return {}, {"watches": str(path),
                    "error": f"{path}: the watch times must be a JSON object, not {type(state).__name__}"}
    try:
        cards.check_unicode(state, "the watch times")
    except CardError as e:  # e.g. a lone surrogate: the file could not be written again
        return {}, {"watches": str(path), "error": f"{path}: {e}"}
    return state, None


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
    watches.json wins when it is later. A given or saved time later than `now` (past the grace) means the clock went
    back, and it is reported. A saved time ahead makes the watch due now, so its time is written again and it
    recovers on its own (a watch looked at once early is harmless). A given time ahead is left out, so the saved
    time still counts: it is reported on every run the caller passes it, and the watch runs by `every`. The gate
    does not read the sources itself; the caller does."""
    if not watches:
        return [], []
    state, damaged = _read_watch_state(data)
    found, errors = [], [damaged] if damaged else []
    for w in watches:
        name = w.get("name") if isinstance(w, dict) else None
        try:
            if not (isinstance(name, str) and name.strip()):
                raise ValueError(f"a watch needs a name, not {name!r}")
            cards.check_unicode(name, "the watch name")  # a CardError is a ValueError
            every = parse_every(w.get("every"))
            given = _parse_time(w.get("last_check_at"))
            saved = state.get(name, {}).get("last_check_at") if isinstance(state.get(name), dict) else None
            try:
                saved = _parse_time(saved)
            except ValueError:
                saved = None  # a damaged saved time: the watch is due now and the time is written again
        except ValueError as e:
            errors.append({"watch": name if isinstance(name, str) else None, "error": str(e)})
            continue
        # Gaps, not `now + GRACE`, so a `now` at the end of the calendar cannot overflow.
        ahead = [t for t in (given, saved) if t is not None and t - now > routines.GRACE]
        if ahead:  # one error per watch, quoting the latest time that is ahead
            errors.append({"watch": name, "error": f"last_check_at {cards.stamp(max(ahead))} is later than now "
                                                   f"({cards.stamp(now)}); {WENT_BACK}"})
        saved_ahead = saved is not None and saved - now > routines.GRACE  # repaired: `now` is written over it
        last = max((t for t in (given, saved) if t is not None and t not in ahead), default=None)
        if last is None or saved_ahead or now - last >= every:
            found.append(name)
            state[name] = {"last_check_at": cards.stamp(now)}
    if found:
        write_json(data / WATCHES, state)
    return found, errors


# -- the run ------------------------------------------------------------------------------------------------------

def _safe(value):
    """Text that can be written as UTF-8: a lone surrogate becomes its backslash escape, as in gate.log."""
    return value.encode("utf-8", "backslashreplace").decode("utf-8") if isinstance(value, str) else value


def run(data, now: datetime, watches=(), store: Optional[Store] = None) -> dict:
    """One gate run at `now` (an aware datetime). Returns what it found, also written to `<data>/gate.json`; a run
    that finds the lock held returns `{"locked": true, ...}` and does nothing. Raises ConfigError when the data
    folder or config.yaml is not usable; that error is also the last line of `<data>/gate.log`, `fatal: ...`."""
    data = Path(data)
    if now.tzinfo is None:
        raise ValueError("now must be an aware datetime")
    try:
        with _lock(data) as held:
            if not held:
                return {"locked": True, "lock": str(data / LOCK)}
            try:
                return _run(data, now, watches, store)
            except OSError as e:
                raise ConfigError(f"the gate cannot use the data folder {data}: {e}") from e
    except ConfigError as e:
        _log_fatal(data, now, e)
        raise


def _log_fatal(data: Path, now: datetime, error: ConfigError) -> None:
    """One `fatal: <error>` line in gate.log, best effort: a run started by launchd has no terminal, so this is
    where the user finds why it stopped. Nothing is written when the data folder does not exist."""
    try:
        if data.is_dir():
            Log(data, now)(f"fatal: {error}")
    except Exception:
        pass  # e.g. gate.log is a folder, the disk is full or `now` has no UTC stamp: the error itself still comes out


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
            if r.last_slot is not None and routines.slot_at(r) - now > routines.GRACE:
                # The clock went back (or the file was edited ahead). Not repaired: moving last_slot back by
                # itself could run a slot twice. The routine waits, as it would anyway, and the user is told.
                # After a tz change, quote the file's own text and zone first, then the slot it became.
                slot = (f"{r.moved_from} ({r.last_slot} in {r.tz_name})" if r.moved_from is not None
                        else r.last_slot)
                raise routines.RoutineError(
                    f"{r.path}: last_slot {slot} is later than now ({cards.stamp(now)}); {WENT_BACK}. Set "
                    f"last_slot to null or an earlier slot to run it again.")
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
                by_key = {c["key"]: c for c in store.list(kind="routine") if isinstance(c.get("key"), str)}
            card = by_key.get(key)
            if card is None:
                card = store.add(run_card(r, d, store.languages), by=BY)
                by_key[key] = card
                action = d.action
            else:
                action = "exists"
            routines.write_last_slot(r.path, d.slot, write_json)
        except (routines.RoutineError, CardError, OSError, ValueError) as e:  # e.g. text UTF-8 cannot write
            errors.append({"routine": r.name, "file": str(r.path), "error": str(e)})
            log(f"routine {r.name}: error: {e}")
            continue
        found.append({"routine": r.name, "slot": d.slot, "key": key, "action": action, "card": card["id"],
                      "late_minutes": int(d.late_by.total_seconds() // 60)})
        log(f"routine {r.name}: slot {d.slot}: {action} (card {card['id']})")

    due_names, watch_errors = due_watches(data, list(watches), now)
    errors.extend(watch_errors)
    for e in watch_errors:
        if "watches" in e:
            log(f"watches: error: {e['error']}")
        elif e["error"].endswith(WENT_BACK):  # a time later than now: reported, and the watch still ran
            log(f"watch {e['watch']}: error: {e['error']}")
        else:
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

    # Old raw source files and their reports (design 12): logged only; gate.json keeps its five keys.
    raw = engine.prune_raw(data, now, store.list)
    if raw["link"]:
        log(f"prune raw: {raw['link']} is a link; raw source files are not pruned")
    for e in raw["errors"]:
        errors.append(e)
        log(f"prune raw: error: {e['error']}")
    if raw["removed"]:
        log(f"pruned {len(raw['removed'])} raw source file(s) older than {engine.RAW_KEEP.days} days")

    errors = [{k: _safe(v) for k, v in e.items()} for e in errors]  # e.g. a file name that is not UTF-8
    out = {"last_run_at": cards.stamp(now), "routines": found, "watches_due": due_names, "pruned": pruned,
           "errors": errors}
    write_json(data / HEARTBEAT, out)
    log(f"run: {len(found)} routine slot(s), {len(due_names)} watch(es) due, {len(pruned)} pruned, "
        f"{len(errors)} error(s)")
    return out
