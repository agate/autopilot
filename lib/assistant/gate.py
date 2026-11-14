"""One gate run (design 7.1 to 7.4, 8.2, 5.3): due routine slots, the worker check, the due watches and their reads,
whether a dispatcher pass is due, the heartbeat and restore-point prune.

A run is short and uses no AI itself: it reads files, decides, makes cards through the store, writes
`<data>/gate.json` and exits. Its own file writes stay inside the data folder. The processes it starts are the
`claude` CLI (`agents` and `stop` for the worker check and the scan session, and at most one `--bg` scan session per
run, through `scan.start`; `stop` and `rm` for the dispatcher pass and at most one `--bg` pass per run, through
`dispatch.start`) and each due `via: command` watch's `read.run`. In shadow mode it starts and resumes no
worker (`Workers` refuses that) and runs no source `writes`. One global lock,
`<data>/.gate.lock`, keeps a second run from doing anything while one is going.

Per routine (7.3): find the latest slot; when it is newer than `last_slot`, make the run's card first (keyed
`<routine>@<slot>`), then write `last_slot`. A run that dies in between leaves the card; the next run finds it by
its key and only writes `last_slot`.

Then (phase 2): every worker on record is checked (`Workers.check`, 8.2); the watches come from the user's own source
files (`watch_list`); a due `via: command` watch is read here (`Engine.read`), all of them in READ_BUDGET (120)
seconds per run (a read's timeout is cut to what is left, less READ_FLOOR (10) seconds kept for each read still to go,
or an even share when that is more, so one hanging read cannot use the time of the others; a read stopped at that
cut while it still ran, or after it exited 0, is read again once each due watch had its first try, when what is
left gives it more time than its first try had, so one slow read is not cut by its place in the order; a watch
whose first try was stopped by a timeout, its own or the cap, while it held more than an even share gets `cut_at`
in watches.json and is read after the others next time (the oldest stamp first among them), until a first try ends
by itself; once the budget is spent a watch waits for its next due time, so one slow read cannot hold the lock,
and with it every later run and routine slot, for long; a long read belongs to `ap2 source read`); the due
`mcp`, `connector` and `skill` watches share one scan session (`scan.start`), which a later run takes into cards
when it ends (`scan.finish`); and
`<data>/pass.json` records whether a dispatcher pass is due and why (`pass_due`).

The dispatcher pass (8.0, `dispatch.py`): early in each run the open pass is looked at (`dispatch.finish`) and ended
when its turn is over, it said `ap2 pass end`, it is gone or it ran past `dispatch.DISPATCH_MAX_MIN` minutes; after the
pass-due check, when no pass is open, the run starts at most one (`_start_dispatch`): the next group of the round in
`<data>/dispatch.json`'s `queue`, or, when a pass is due, the first group of a new round (`dispatch.groups`), and
then `pass_started` moves `seen`. The gate never waits for a pass: a later run ends it. In shadow mode the pass still
runs; its floor denies the outward writes.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from typing import Optional

from assistant import cards, dispatch, engine, routines, scan, sources, transcripts
from assistant import workers as workers_module
from assistant.cards import CardError
from assistant.config import ConfigError
from assistant.importv1 import FEED as V1_FEED, TAKEN_OVER
from assistant.resources import Registry, WorkerError
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
PASS = "pass.json"
EACH_PASS = "each pass"  # `every: each pass`: read by the dispatcher pass, not timed by the gate
SCAN_VIAS = scan.VIAS  # the watches one scan session reads
EVERY = re.compile(r"([1-9][0-9]{0,3})(m|h)\Z", re.ASCII)
BY = "gate"
# Ends the watch error for a time ahead of now, the one error after which the watch still runs (the routine
# error for a last_slot ahead of now says it too).
WENT_BACK = routines.WENT_BACK
# Seconds one run gives all its command reads together, well under routines.GRACE: a read holds the gate lock, and
# every later run waits on it, so a routine slot that came due meanwhile could be found past the grace and missed.
READ_BUDGET = 120
# Seconds of READ_BUDGET kept for each read still to go while one read runs (unless an even share is more), so one
# read may use most of what is left but never the time owed to the later ones.
READ_FLOOR = 10


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
        "links": [], "pri": "P2", "origin": "source",
        "title": {lang: r.title for lang in languages},  # the user's own words, the same in every language
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



def watch_list(data, plugin_dir: Optional[Path] = None) -> tuple:
    """The watches in the user's own source files, as `(watches, errors)`: each name in `<data>/sources/` that loads
    and whose `read.every` is `<N>m` or `<N>h` is `{name, every, via}` (a one-line `extends: <built-in>` turns that
    built-in on; a built-in file alone is never watched). `every: each pass` and no `every` are left out with no
    error; any other `every` is one `{"watch", "error"}`, and a file that does not load one `{"source", "error"}`."""
    data = Path(data)
    try:
        loader = sources.Loader(data, plugin_dir)
    except sources.SourceError as e:  # the user's sources folder cannot be listed
        return [], [{"source": str(data / sources.FOLDER), "error": str(e)}]
    watches, errors = [], []
    for name in sorted(loader._names[1]):  # the user's layer
        try:
            src = loader.load(name)
        except sources.SourceError as e:
            errors.append({"source": name, "error": str(e)})
            continue
        every = src.data["read"].get("every")
        if every is None or every == EACH_PASS:
            continue
        try:
            parse_every(every)
        except ValueError as e:
            errors.append({"watch": name, "error": f"{src.file}: read.{e}"})
            continue
        watches.append({"name": name, "every": every, "via": src.via})
    return watches, errors

def _read_watch_state(data: Path):
    """The saved watch times, as `(state, error)`. A damaged or unreadable watches.json is treated like a damaged
    saved time: the run goes on from `{}` (every watch is due) and `error` says why; the file is written again,
    atomically, when a watch is due."""
    path = data / WATCHES
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError, RecursionError) as e:  # UnicodeDecodeError is a ValueError; too deep is damaged
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
    for each due one in `<data>/watches.json` (a saved watch's other keys, such as `cut_at`, are kept). Each watch
    is `{name, every, last_check_at?}`; the time saved in
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
            if cards.blank(name):
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
            kept = state.get(name) if isinstance(state.get(name), dict) else {}
            state[name] = dict(kept, last_check_at=cards.stamp(now))
    if found:
        write_json(data / WATCHES, state)
    return found, errors


# -- the run ------------------------------------------------------------------------------------------------------

def _safe(value):
    """Text that can be written as UTF-8: a lone surrogate becomes its backslash escape, as in gate.log."""
    return value.encode("utf-8", "backslashreplace").decode("utf-8") if isinstance(value, str) else value


def _raw_prune_due(data: Path, now: datetime) -> bool:
    """True on the first gate run of a UTC day (design 12): when the previous run's `last_run_at` in gate.json is on
    an earlier UTC date than `now`. The raw prune reads every card once a file is old, and a card keeps its first raw
    file forever, so it runs at most once a day. Read before this run writes gate.json. Any doubt means True, so
    nothing waits forever: gate.json missing or unreadable, not JSON, not an object, no `last_run_at` or not a UTC
    stamp, or a `last_run_at` later than `now` (the clock went back). Never raises and logs nothing."""
    try:
        then = cards.parse_stamp(json.loads((data / HEARTBEAT).read_text(encoding="utf-8"))["last_run_at"])
    except Exception:  # OSError, ValueError (UTF-8, JSON, stamp), TypeError, KeyError, RecursionError (too deep)
        return True
    return then > now or then.date() != now.astimezone(timezone.utc).date()


def run(data, now: datetime, watches=None, store: Optional[Store] = None, claude: Optional[str] = None,
        transcripts_dir=None, usage_file=None) -> dict:
    """One gate run at `now` (an aware datetime). Returns what it found, also written to `<data>/gate.json`; a run
    that finds the lock held returns `{"locked": true, ...}` and does nothing. Raises ConfigError when the data
    folder or config.yaml is not usable; that error is also the last line of `<data>/gate.log`, `fatal: ...`.

    `watches=None` takes the watches from the user's source files (`watch_list`) and reads the due ones; a list
    given by a caller (`[{name, every, last_check_at?}]`) is only timed, never read. `claude`, `transcripts_dir` and
    `usage_file` are the worker check's (`Workers`, `Workers.check`): the claude CLI (default: the one on PATH), the
    folder Claude Code keeps conversations in (default `~/.claude/projects`, read only) and the usage cache a parked
    worker resumes by (default: none)."""
    data = Path(data)
    if now.tzinfo is None:
        raise ValueError("now must be an aware datetime")
    try:
        with _lock(data) as held:
            if not held:
                return {"locked": True, "lock": str(data / LOCK)}
            try:
                return _run(data, now, watches, store, claude, transcripts_dir, usage_file)
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


# -- the worker check (design 8.2) ------------------------------------------------------------------------------

def _error_text(e: BaseException) -> str:
    return " ".join(f"{type(e).__name__}: {e}".split())


def _check_workers(data: Path, now: datetime, cfg, store: Store, claude, transcripts_dir, usage_file, errors: list,
                   log) -> list:
    """`Workers.check` once, only when the worker list names a worker (so an empty folder runs no `claude agents`).
    Its entries are returned; one that did something is one gate.log line. Any exception is one error."""
    try:
        if not Registry(data).read():
            return []
        w = workers_module.Workers(data, cfg, claude=claude, transcripts_dir=transcripts_dir, clock=lambda: now,
                                   store=store)
        entries = w.check(by=BY, usage_file=usage_file)
    except Exception as e:  # noqa: BLE001 - the rest of the run goes on
        errors.append({"workers": True, "error": _error_text(e)})
        log(f"workers: error: {_error_text(e)}")
        return []
    for entry in entries:
        if entry.get("did") != "nothing":
            note = f" ({entry['note']})" if entry.get("note") else ""
            log(f"worker {entry['card']} {entry['role']}: {entry['did']}{note}")
    return entries


# -- reads: command watches here, scan watches in one scan session (design 5.3) ----------------------------------

def _read_entry(name: str, via: str, result: dict) -> dict:
    """One `reads` entry: what a read made of its items, by the counts of the result `Engine` returns."""
    actions = [i.get("action") for i in result.get("items") or []]
    return {"watch": name, "via": via, "made": actions.count("add"), "updated": actions.count("update"),
            "unchanged": actions.count("unchanged"), "dropped": len(result.get("dropped") or [])}


def _log_read(log, entry: dict) -> None:
    log(f"watch {entry['watch']}: read: {entry['made']} made, {entry['updated']} updated, {entry['unchanged']} "
        f"unchanged, {entry['dropped']} dropped")


def _cut_last_time(entry, now: datetime) -> Optional[datetime]:
    """The `cut_at` of a watch's saved entry when it is a UTC stamp no more than routines.GRACE later than `now`;
    anything else (none, damaged, not text, ahead of the clock) means it was not cut (None)."""
    at = entry.get("cut_at") if isinstance(entry, dict) else None
    if not isinstance(at, str):
        return None
    try:
        at = cards.parse_stamp(at)
    except ValueError:
        return None
    # Gaps, not `now + GRACE`, so a `now` at the end of the calendar cannot overflow.
    return None if at - now > routines.GRACE else at


def _read_commands(data: Path, now: datetime, cfg, names: list, reads: list, errors: list, log) -> None:
    """Read each due `via: command` watch (`Engine.read`); a failed read is one error and the next still reads.
    The reads share READ_BUDGET seconds: each read's timeout is cut to what is left, less READ_FLOOR seconds kept for
    each read still to go (or to an even share of what is left, when that is more), so one hanging read cannot use
    the time of the others. A read stopped at that cut while it still ran or after it exited 0 (`engine.ReadCut`) is
    no error yet: once every due watch had its first try, it is read again with what is left, shared the same way
    among the reads cut, so no read fails only by its place in the order; that is its one error when it fails. A
    second try that would get no more time than the first try had is not run: the first try's error is the one. A
    read that fails any other way (exit not 0 by itself too) is its error at once. Once it is spent a watch is not
    read (one error); its time stays written, so it is read at its next due time.

    The watches cut last time go last, the oldest `cut_at` first among them (the one cut over its share most
    recently goes last), by name otherwise; a `cut_at` more than routines.GRACE ahead of `now` means not cut. A
    watch whose first try is stopped by a timeout, its own or the gate's cap, while it held more than an even share
    (it used time owed to the reads after it; the error's `waited`), whether that was a cut or a failure at the
    cap, gets this run's stamp as `cut_at` in watches.json; a first try stopped by a timeout at no more than an even
    share keeps what it had (an old stamp is not refreshed: that read only lost time to the ones before it); a first
    try that ends by itself (read, or failed another way) removes it. The write is best effort (one error); it
    changes no other key."""
    if not names:
        return
    path = data / WATCHES
    state, _ = _read_watch_state(data)  # as `due_watches` just wrote it; damaged, it was reported there

    def order(n: str):
        at = _cut_last_time(state.get(n), now)
        return (False,) if at is None else (True, at, n)

    names = sorted(names, key=order)
    marks = {}  # name -> True (a timeout stop over its share: this run's stamp) or False (ended by itself: clear)
    began = monotonic()
    eng = engine.Engine(data, cfg, clock=lambda: now)

    def failed(name: str, e: BaseException) -> None:
        why = str(e) if isinstance(e, (ConfigError, CardError, OSError)) else _error_text(e)
        errors.append({"watch": name, "error": why})
        log(f"watch {name}: error: {why}")

    def share(left: float, after: int) -> float:
        return max(left / (after + 1), left - READ_FLOOR * after)

    def mark(name: str, e: BaseException, left: float, after: int) -> None:
        waited = getattr(e, "waited", None)  # set on every timeout stop, its own timeout or the cap
        if waited is None:  # ended by itself
            marks[name] = False
        elif waited > left / (after + 1):  # it used time owed to the reads after it
            marks[name] = True
        # else: stopped at no more than an even share, it keeps what it had (an old stamp stays as it was)

    cut = []  # (name, the first try's ReadCut, the first try's cap), in order
    for i, name in enumerate(names):
        left = READ_BUDGET - (monotonic() - began)
        if left <= 0:
            why = (f"not read: this run's {READ_BUDGET} seconds for command reads were spent; it is read at its next "
                   f"due time")
            errors.append({"watch": name, "error": why})
            log(f"watch {name}: error: {why}")
            continue
        after = len(names) - i - 1  # the reads still to go after this one
        cap = share(left, after)
        try:
            result = eng.read(name, max_timeout=cap)
        except engine.ReadCut as e:
            cut.append((name, e, cap))
            mark(name, e, left, after)
            continue
        except Exception as e:  # noqa: BLE001 - one watch's failure never stops the run; a SourceError is ours
            mark(name, e, left, after)  # a timeout stop, its own or at the cap, held its time like a cut
            failed(name, e)
            continue
        marks[name] = False
        reads.append(_read_entry(name, "command", result))
        _log_read(log, reads[-1])
    for i, (name, first, first_cap) in enumerate(cut):  # the second try, with what is left after every first try
        cap = share(READ_BUDGET - (monotonic() - began), len(cut) - i - 1)
        if cap <= first_cap:  # no more time than the first try had: running it again could only be cut again
            failed(name, first)
            continue
        try:
            result = eng.read(name, max_timeout=cap)
        except Exception as e:  # noqa: BLE001 - as above; a second cut is its error too
            failed(name, e)
            continue
        reads.append(_read_entry(name, "command", result))
        _log_read(log, reads[-1])
    changed = False
    for name, mark in marks.items():
        entry = state.get(name)  # a dict for each due watch, unless watches.json could not be read (then none)
        if isinstance(entry, dict) and mark:
            entry["cut_at"] = cards.stamp(now)
            changed = True
        elif isinstance(entry, dict) and "cut_at" in entry:
            del entry["cut_at"]
            changed = True
    if changed:
        try:
            write_json(path, state)
        except OSError as e:
            why = f"{path}: cannot save which command reads were cut: {e}"
            errors.append({"watches": str(path), "error": why})
            log(f"watches: error: {why}")


def _start_scan(data: Path, now: datetime, cfg, store: Store, claude, transcripts_dir, names: list, errors: list,
                log) -> None:
    """One scan session for the due scan watches: the profile and the cap first, then the plan, then the start.
    A refusal starts nothing and is one error; the watch times stay written."""
    try:
        prof = scan.launch_profile(data, now, transcripts_dir, claude=claude, store=store)
        p = scan.plan(data, names, cfg=cfg, clock=lambda: now)
    except Exception as e:  # noqa: BLE001 - e.g. a ProfileError, a paused cap: nothing started
        errors.append({"scan": True, "error": str(e) or type(e).__name__})
        log(f"scan: not started: {e}")
        return
    for e in p["errors"]:
        errors.append(e)
        log(f"source {e['source']}: refused: {e['error']}")
    if not p["calls"]:
        return
    try:
        rec = scan.start(data, p, prof, now, claude=claude)
    except Exception as e:  # noqa: BLE001 - one error, the run goes on
        errors.append({"scan": True, "error": str(e) or type(e).__name__})
        log(f"scan: not started: {e}")
        return
    log(f"scan: started {rec['short_id']} for {', '.join(c['source'] for c in p['calls'])}")
    if rec.get("note"):  # e.g. the spend launch row was not written, so the cap would undercount
        errors.append({"scan": True, "error": rec["note"]})
        log(f"scan: {rec['note']}")


def _finish_scan(data: Path, now: datetime, cfg, claude, transcripts_dir, reads: list, errors: list, log) -> bool:
    """Take the open scan session into cards when it has ended (`scan.finish`). Returns True while one is open."""
    try:
        out = scan.finish(data, now, claude=claude, transcripts_dir=transcripts_dir, cfg=cfg)
    except Exception as e:  # noqa: BLE001 - one error; the record is kept, so the next run looks again
        errors.append({"scan": True, "error": _error_text(e)})
        log(f"scan: error: {_error_text(e)}")
        return (data / scan.SESSION).exists()
    if out is None:
        return False
    for e in out["errors"]:
        errors.append(e)
        log(f"source {e['source']}: error: {e['error']}" if "source" in e else f"scan: error: {e['error']}")
    for r in out["reads"]:
        reads.append(_read_entry(r["source"], r["via"], r))
        _log_read(log, reads[-1])
    if out["ended"]:
        log(f"scan: {out['short_id']} ended; {len(out['reads'])} read(s) taken")
    return out["open"]


# -- whether a dispatcher pass is due (design 8.0, 8.5) -------------------------------------------------------------

def _empty_seen() -> dict:
    return {"ready": [], "results": {}, "asks": {}}


def _present(data: Path, store: Store) -> dict:
    """What a pass would look at now: the ready card ids, the result files with their whole-second mtime, and each
    open ask as `card -> type/from/reason/role`. Ready is a cheap wake-up, not the start plan: a planned card with
    no ask and no worker on record, a routine run card in Running with no worker and no sessions, or a taken-over
    v1 code card in Running with no ask and no worker whose session on some role is still the one the take-over
    wrote (`_left_by_take_over`); never an imported v1 copy that is not taken over. Raises CardError, WorkerError or
    OSError."""
    on_record = {r.get("card") for r in Registry(data).read()}
    ready, asks = [], {}
    for c in store.list():
        cid, ask = c["id"], c.get("ask")
        if isinstance(ask, dict):
            asks[cid] = "/".join(str(ask.get(k) or "") for k in ("type", "from", "reason", "role"))
        if ask is not None or cid in on_record or (c.get("feed") == V1_FEED and not c.get("taken_over")):
            continue
        if c.get("lane") == "planned" or (c.get("kind") == "routine" and c.get("lane") == "running"
                                          and not c.get("sessions")) or _left_by_take_over(c):
            ready.append(cid)
    results = {}
    base = data / transcripts.SESSIONS  # each card's run directory is <data>/sessions/<card>
    for name in sorted(set(workers_module.RESULT_FILES.values())):
        for path in sorted(base.glob(f"*/{name}")):
            if not cards.CARD_ID.match(path.parent.name):
                continue
            try:
                results[f"{path.parent.name}/{name}"] = int(path.stat().st_mtime)
            except FileNotFoundError:
                continue
    return {"ready": ready, "results": results, "asks": asks}


def _left_by_take_over(c: dict) -> bool:
    """A v1 task taken over while in progress: a code card in Running with a session the take-over wrote (its `why`
    is importv1's TAKEN_OVER). v2's own stop leaves a resume ask instead, so without this nothing would wake a pass
    for it; once v2 saves a session on that role its `why` changes and this rule lets go."""
    sessions = c.get("sessions")
    return (bool(c.get("taken_over")) and c.get("kind") == "code" and c.get("lane") == "running"
            and isinstance(sessions, dict)
            and any(isinstance(s, dict) and s.get("why") == TAKEN_OVER for s in sessions.values()))


def _read_pass(path: Path):
    """`(the saved pass.json, error)`: `({}, None)` when there is none; `({}, why)` when it is damaged."""
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError, RecursionError) as e:
        return {}, f"{path}: cannot read the pass record: {e}"
    seen = saved.get("seen") if isinstance(saved, dict) else None
    if not (isinstance(seen, dict) and isinstance(seen.get("ready"), list) and isinstance(seen.get("results"), dict)
            and isinstance(seen.get("asks"), dict) and all(isinstance(c, str) for c in seen["ready"])):
        return {}, f"{path}: not a pass record (expected an object with seen: {{ready, results, asks}})"
    return saved, None


def _why(present: dict, seen: dict) -> list:
    """Each change since `seen`: a ready card that was not ready, a result file that is new or changed, an ask that
    was open and is not open the same way now."""
    was_ready = set(seen["ready"])
    why = [{"why": "ready", "card": c} for c in present["ready"] if c not in was_ready]
    for key, mtime in present["results"].items():
        if seen["results"].get(key) != mtime:
            card, file = key.split("/", 1)
            why.append({"why": "result", "card": card, "file": file})
    why += [{"why": "answered", "card": c} for c, sig in sorted(seen["asks"].items()) if present["asks"].get(c) != sig]
    return why


def _pass_due(data: Path, now: datetime, store: Store, errors: list, log) -> bool:
    """Write `<data>/pass.json` atomically: `{due, since, why, seen, checked_at}`, and return `due`. A change since
    `seen` (what was there when the last pass started) makes a pass due; a standing state does not. `since` is
    kept from the first run that found it due. Nothing is started here."""
    path = data / PASS
    saved, damaged = _read_pass(path)
    if damaged:
        errors.append({"pass": str(path), "error": damaged})
        log(f"pass: error: {damaged}")
    try:
        present = _present(data, store)
    except (CardError, WorkerError, OSError, RecursionError) as e:  # RecursionError: a workers.json nested too deep
        errors.append({"pass": True, "error": str(e)})
        log(f"pass: error: {e}")
        return False
    seen = saved.get("seen") or _empty_seen()
    why = _why(present, seen)
    since = None
    if why:
        since = cards.stamp(now)
        try:
            if saved.get("due") is True:
                since = cards.stamp(cards.parse_stamp(saved["since"]))
        except (KeyError, TypeError, ValueError):
            pass
        log(f"pass due: {len(why)} reason(s)")
    write_json(path, {"due": bool(why), "since": since, "why": why, "seen": seen, "checked_at": cards.stamp(now)})
    return bool(why)


def pass_started(data, now: datetime, store: Optional[Store] = None) -> dict:
    """A dispatcher pass starts at `now`: what is there now becomes `seen`, and the pass is no longer due. Returns
    the pass record written to `<data>/pass.json`. Raises CardError, WorkerError or OSError."""
    data = Path(data)
    store = store if store is not None else Store(data, clock=lambda: now)
    out = {"due": False, "since": None, "why": [], "seen": _present(data, store), "checked_at": cards.stamp(now)}
    write_json(data / PASS, out)
    return out


# -- the dispatcher pass (design 8.0, section 12 "Dispatcher pass") ----------------------------------------------------

def _finish_dispatch(data: Path, now: datetime, store: Store, claude, transcripts_dir, entries: list, errors: list,
                     log) -> None:
    """End the open pass when it is over (`dispatch.finish`): one gate.log line and one `dispatch` entry per end."""
    try:
        out = dispatch.finish(data, now, claude=claude, transcripts_dir=transcripts_dir, store=store)
    except Exception as e:  # noqa: BLE001 - one error; the record is kept, so the next run looks again
        errors.append({"dispatch": True, "error": _error_text(e)})
        log(f"dispatch: error: {_error_text(e)}")
        return
    if out is None:
        return
    for e in out["errors"]:
        errors.append(e)
        log(f"dispatch: error: {e['error']}")
    if out["ended"]:
        entry = {"did": out["how"], "short_id": out["short_id"], "group": out["group"], "projects": out["projects"]}
        if out.get("card"):
            entry["card"] = out["card"]
        entries.append(entry)
        card = f"; card {out['card']} asks the user to attach it" if out.get("card") else ""
        log(f"dispatch: {out['short_id']} ({dispatch.label(out)}) ended ({out['how']}: {dispatch.HOW[out['how']]})"
            f"{card}")


def _start_dispatch(data: Path, now: datetime, store: Store, due: bool, claude, transcripts_dir, entries: list,
                    errors: list, log) -> None:
    """Start at most one pass, never while one is open: the next group of the round's queue, or, when a pass is
    due, the first group of a new round, and then `pass_started` moves `seen` (once per round). A new round freezes
    pass.json's `why` (written by `_pass_due` in this run, before `pass_started` clears it) on the pass's record, and
    the queue carries it to each next group. The profile and the cap are asked first: a refusal starts nothing and
    keeps the queue, and a due pass stays due. A group whose start fails is one error and the next group of the
    round is tried in the same run, except when `claude --bg` itself fails (`dispatch.LaunchError`): that is one
    error, no other group is tried this run and the queue is kept from that group on. A new round where none starts
    stays due."""
    try:
        rec = dispatch.read_record(data)
    except (OSError, ValueError, RecursionError):  # damaged since `_finish_dispatch` looked: the next run removes it
        return
    if rec is not None and "short_id" in rec:
        return  # one pass at a time
    queue = list((rec or {}).get("queue") or [])
    why = list((rec or {}).get("why") or [])
    new_round = not queue
    if new_round:
        if not due:
            return
        saved, _ = _read_pass(data / PASS)
        why = saved.get("why") if isinstance(saved.get("why"), list) else []
        try:
            queue, bad = dispatch.groups(data, store)
        except (CardError, OSError, RecursionError) as e:
            errors.append({"dispatch": True, "error": str(e)})
            log(f"dispatch: error: {e}")
            return
        for e in bad:
            errors.append(e)
            log(f"project {e['project']}: refused: {e['error']}")
        if not queue:
            if not bad:  # nothing live to serve: what changed is seen, so the pass is not due every run
                try:
                    pass_started(data, now, store)
                except (CardError, WorkerError, OSError, RecursionError) as e:
                    errors.append({"pass": True, "error": str(e)})
                    log(f"pass: error: {e}")
            return
    try:
        prof = dispatch.launch_profile(data, now, transcripts_dir, claude=claude, store=store)
    except Exception as e:  # noqa: BLE001 - e.g. a ProfileError, a paused cap: nothing started
        errors.append({"dispatch": True, "error": str(e) or type(e).__name__})
        log(f"dispatch: not started: {e}")
        return
    started, stopped = None, None
    for i, group in enumerate(queue):
        try:
            started = dispatch.start(data, group, queue[i + 1:], prof, now, store.cfg.mode, claude=claude, why=why)
            break
        except dispatch.LaunchError as e:  # claude --bg failed (or hung 120 s): every other group would too
            errors.append({"dispatch": True, "error": str(e)})
            log(f"dispatch: not started: {e}; no other group is tried this run")
            stopped = i
            break
        except Exception as e:  # noqa: BLE001 - one error per group; the next group of the round is tried
            errors.append({"dispatch": True, "error": str(e) or type(e).__name__})
            log(f"dispatch: not started: {e}")
    if started is None:
        if new_round:
            return
        try:
            if stopped is None:  # every group left in the round failed: the round is over
                (data / dispatch.DISPATCH).unlink(missing_ok=True)
            else:  # the next run tries again from the group that could not launch
                write_json(data / dispatch.DISPATCH, {"queue": queue[stopped:], "why": why})
        except OSError as e:
            errors.append({"dispatch": True, "error": str(e)})
            log(f"dispatch: error: {e}")
        return
    entries.append({"did": "started", "short_id": started["short_id"], "group": started["group"],
                    "projects": started["projects"]})
    log(f"dispatch: started {started['short_id']} for {dispatch.label(started)}; {len(started['queue'])} group(s) "
        f"left in the round")
    if started.get("note"):
        errors.append({"dispatch": True, "error": started["note"]})
        log(f"dispatch: {started['note']}")
    if new_round:
        try:
            pass_started(data, now, store)
        except (CardError, WorkerError, OSError, RecursionError) as e:  # at worst one extra pass
            errors.append({"pass": True, "error": str(e)})
            log(f"pass: error: {e}")


def _run(data: Path, now: datetime, watches, store: Optional[Store], claude, transcripts_dir,
         usage_file) -> dict:
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

    cfg = store.cfg
    checked = _check_workers(data, now, cfg, store, claude, transcripts_dir, usage_file, errors, log)
    reads = []
    scan_open = _finish_scan(data, now, cfg, claude, transcripts_dir, reads, errors, log)
    dispatched = []
    _finish_dispatch(data, now, store, claude, transcripts_dir, dispatched, errors, log)

    read_them = watches is None
    if read_them:
        listed, list_errors = watch_list(data)
        for e in list_errors:
            errors.append(e)
            log(f"source {e['source']}: refused: {e['error']}" if "source" in e else
                f"watch {e['watch']}: refused: {e['error']}")
        # While a scan session is open, its kind of watch is not timed: the next run looks again.
        timed = [w for w in listed if not (scan_open and w["via"] in SCAN_VIAS)]
    else:
        timed = list(watches)
    due_names, watch_errors = due_watches(data, timed, now)
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
    if read_them and due_names:
        via = {w["name"]: w["via"] for w in listed}
        _read_commands(data, now, cfg, [n for n in due_names if via[n] == "command"], reads, errors, log)
        scans = [n for n in due_names if via[n] in SCAN_VIAS]
        if scans:
            _start_scan(data, now, cfg, store, claude, transcripts_dir, scans, errors, log)

    try:
        pruned = store.prune(now)
    except CardError as e:
        pruned = []
        errors.append({"prune": True, "error": str(e)})
        log(f"prune: error: {e}")
    if pruned:
        log(f"pruned {len(pruned)} restore point(s) older than 30 days")

    # Old raw source files and their reports (design 12), on the first run of a UTC day only: logged only, not
    # in gate.json.
    if _raw_prune_due(data, now):
        raw = engine.prune_raw(data, now, store.list)
        if raw["link"]:
            log(f"prune raw: {raw['link']} is a link; raw source files are not pruned")
        for e in raw["errors"]:
            errors.append(e)
            log(f"prune raw: error: {e['error']}")
        if raw["removed"]:
            log(f"pruned {len(raw['removed'])} raw source file(s) older than {engine.RAW_KEEP.days} days")

    pass_due = _pass_due(data, now, store, errors, log)
    _start_dispatch(data, now, store, pass_due, claude, transcripts_dir, dispatched, errors, log)
    errors = [{k: _safe(v) for k, v in e.items()} for e in errors]  # e.g. a file name that is not UTF-8
    out = {"last_run_at": cards.stamp(now), "routines": found, "workers": checked, "watches_due": due_names,
           "reads": reads, "pruned": pruned, "pass_due": pass_due, "dispatch": dispatched, "errors": errors}
    write_json(data / HEARTBEAT, out)
    log(f"run: {len(found)} routine slot(s), {len(due_names)} watch(es) due, {len(pruned)} pruned, "
        f"{len(errors)} error(s)")
    return out
