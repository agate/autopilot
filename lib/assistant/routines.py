"""Routine files and their schedule (design 7.2, 7.3).

One routine per file, `<data>/routines/<name>.json`:

    {"title": "Daily digest", "schedule": {"days": "weekdays", "at": ["09:00"]},
     "tz": "America/Los_Angeles", "missed_run": "once_within: 3h",
     "last_slot": "2026-11-07T09:00", "paused": false}

JSON, not YAML: the gate writes `last_slot` back into the file, and JSON reads back exactly as written. A slot is
the wall-clock time in the routine's `tz`, `YYYY-MM-DDTHH:MM`. Nothing here makes cards; `gate.py` does.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import MAXYEAR, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # Python before 3.9
    ZoneInfo = None

    class ZoneInfoNotFoundError(KeyError):
        pass

FOLDER = "routines"
SUFFIX = ".json"
KEYS = ("title", "schedule", "tz", "missed_run", "last_slot", "last_slot_tz", "paused")
REQUIRED = ("title", "schedule", "tz")
SCHEDULE_KEYS = ("days", "at")
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")  # date.weekday() order
DAY_SETS = {"daily": frozenset(range(7)), "weekdays": frozenset(range(5))}
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z", re.ASCII)
AT = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])\Z", re.ASCII)
SLOT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}\Z", re.ASCII)
SLOT_FORMAT = "%Y-%m-%dT%H:%M"
MISSED = re.compile(r"(skip|once|once_within: ?([1-9][0-9]{0,2})h)\Z", re.ASCII)
DEFAULT_MISSED = "once_within: 3h"  # design 11, question 1: the author's recommendation
GRACE = timedelta(minutes=5)  # a slot older than this at the run is missed
LOOK_BACK_DAYS = 7  # a routine that runs once a week still has a slot in the last 7 days


class RoutineError(ValueError):
    """A routine file is not usable. The message names the file and the field."""


@dataclass(frozen=True)
class Routine:
    name: str
    path: Path
    title: str
    days: frozenset
    at: tuple  # ((hour, minute), ...)
    tz_name: str
    tz: object
    missed_run: str
    last_slot: Optional[str]
    paused: bool
    # When `last_slot` was moved from the file's `last_slot_tz` to `tz`: the file's text and that old zone, such as
    # "2026-11-11T09:00 in Asia/Tokyo". None when `last_slot` is the file's text as it is.
    moved_from: Optional[str] = None
    # With `moved_from`: the exact UTC moment of the file's `last_slot` in its old zone. The moved text alone can
    # name a time that happens twice in `tz` (the hour the clocks go back); this says which one. None otherwise.
    moved_at: Optional[datetime] = None

    def within(self) -> Optional[timedelta]:
        """How late a missed slot may still run: None for `once` (any time), zero for `skip`."""
        m = MISSED.match(self.missed_run)
        if m.group(1) == "skip":
            return timedelta(0)
        if m.group(1) == "once":
            return None
        return timedelta(hours=int(m.group(2)))


@dataclass(frozen=True)
class Due:
    """A slot the gate must handle now. `action` is `run` (on time), `late` (missed, run once now), `skip`
    (missed, filed as skipped) or `start` (a routine that never ran: only `last_slot` is written)."""
    slot: str
    at: datetime  # the slot as a UTC time
    action: str
    late_by: timedelta


def _fail(path: Path, field: str, reason: str):
    raise RoutineError(f"{path}: {field}: {reason}")


def _days(path: Path, value) -> frozenset:
    if isinstance(value, str) and value in DAY_SETS:
        return DAY_SETS[value]
    if isinstance(value, list) and value:
        if not all(isinstance(d, str) and d in DAY_NAMES for d in value):
            _fail(path, "schedule.days", f"each day must be one of {', '.join(DAY_NAMES)}, not {value!r}")
        if len(set(value)) != len(value):
            _fail(path, "schedule.days", f"a day is listed twice in {value!r}")
        return frozenset(DAY_NAMES.index(d) for d in value)
    _fail(path, "schedule.days", f"days must be daily, weekdays or a list of {', '.join(DAY_NAMES)}, not {value!r}")


def _at(path: Path, value) -> tuple:
    if not (isinstance(value, list) and value):
        _fail(path, "schedule.at", f"at must be a non-empty list of HH:MM times, not {value!r}")
    times = []
    for t in value:
        m = AT.match(t) if isinstance(t, str) else None
        if m is None:
            _fail(path, "schedule.at", f"not an HH:MM time (00:00 to 23:59): {t!r}")
        times.append((int(m.group(1)), int(m.group(2))))
    if len(set(times)) != len(times):
        _fail(path, "schedule.at", f"a time is listed twice in {value!r}")
    return tuple(sorted(times))


def _tz(path: Path, value):
    if not (isinstance(value, str) and value.strip()):
        _fail(path, "tz", f"tz must be an IANA time zone name such as America/Los_Angeles, not {value!r}")
    if ZoneInfo is None:
        _fail(path, "tz", "this Python has no zoneinfo module (Python 3.9 or later is needed)")
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        _fail(path, "tz", f"not a known IANA time zone name: {value!r}")


def check_slot(text) -> bool:
    if not (isinstance(text, str) and SLOT.match(text)):
        return False
    try:
        datetime.strptime(text, SLOT_FORMAT)
    except ValueError:
        return False
    return True


def parse(path: Path, obj) -> Routine:
    """The routine in `obj`, read from `path`. Raises RoutineError naming the file and the field."""
    path = Path(path)
    name = path.name[:-len(SUFFIX)] if path.name.endswith(SUFFIX) else path.name
    if not NAME.match(name):
        _fail(path, "file name", f"a routine's name is its file name: lowercase letters, digits, - and _, "
                                 f"starting with a letter or digit, not {name!r}")
    if not isinstance(obj, dict):
        _fail(path, "(file)", f"a routine is a JSON object, not {type(obj).__name__}")
    for key in obj:
        if key not in KEYS:
            _fail(path, repr(key), f"unknown key (known keys: {', '.join(KEYS)})")
    for key in REQUIRED:
        if key not in obj:
            _fail(path, key, "missing")
    title = obj["title"]
    if not (isinstance(title, str) and title.strip()):
        _fail(path, "title", f"title must be a non-empty text, not {title!r}")
    try:
        title.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate, e.g. a \ud800 escape: it could not be written back
        _fail(path, "title", f"title is not valid Unicode: {title!r}")
    schedule = obj["schedule"]
    if not isinstance(schedule, dict):
        _fail(path, "schedule", f"schedule must be an object with days and at, not {schedule!r}")
    for key in schedule:
        if key not in SCHEDULE_KEYS:
            _fail(path, f"schedule.{key!r}", f"unknown key (known keys: {', '.join(SCHEDULE_KEYS)})")
    for key in SCHEDULE_KEYS:
        if key not in schedule:
            _fail(path, f"schedule.{key}", "missing")
    days = _days(path, schedule["days"])
    at = _at(path, schedule["at"])
    tz = _tz(path, obj["tz"])
    missed = obj.get("missed_run", DEFAULT_MISSED)
    if missed is None:
        missed = DEFAULT_MISSED
    if not (isinstance(missed, str) and MISSED.match(missed)):
        _fail(path, "missed_run", f"missed_run must be skip, once or once_within: <N>h (N from 1 to 999), "
                                  f"not {missed!r}")
    text = obj.get("last_slot")
    if text is not None and not check_slot(text):
        _fail(path, "last_slot", f"last_slot must be a YYYY-MM-DDTHH:MM time or null, not {text!r}")
    last_tz = obj.get("last_slot_tz")
    last, moved_at = _in_zone(path, text, last_tz, obj["tz"], tz)
    moved_from = f"{text} in {last_tz}" if text is not None and last_tz not in (None, obj["tz"]) else None
    paused = obj.get("paused", False)
    if not isinstance(paused, bool):
        _fail(path, "paused", f"paused must be true or false, not {paused!r}")
    return Routine(name=name, path=path, title=title, days=days, at=at, tz_name=obj["tz"], tz=tz,
                   missed_run=missed, last_slot=last, paused=paused, moved_from=moved_from, moved_at=moved_at)


def _in_zone(path: Path, last: Optional[str], last_tz, tz_name: str, tz):
    """`(slot, moment)`: `last_slot` as a slot in the routine's zone, and its UTC moment when it was moved (else
    None). A file whose `last_slot_tz` names another zone (the user changed `tz`) has its slot read as a wall time in
    that old zone and written again as wall-clock text in `tz`, so a move west does not skip slots; the text and the
    moment come from that one reading. No `last_slot_tz` (a file from before it was saved) means the slot is already
    in `tz`. The file itself is changed only by the next `write_last_slot`."""
    if last_tz is not None and not isinstance(last_tz, str):
        _fail(path, "last_slot_tz", f"last_slot_tz must be an IANA time zone name or null, not {last_tz!r}")
    if last_tz is None or last_tz == tz_name:
        return last, None
    try:
        old = ZoneInfo(last_tz)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        _fail(path, "last_slot_tz", f"not a known IANA time zone name: {last_tz!r}")
    if last is None:
        return None, None
    try:
        moment = slot_time(datetime.strptime(last, SLOT_FORMAT), old)
        return moment.astimezone(tz).strftime(SLOT_FORMAT), moment
    except OverflowError:  # e.g. year 9999 west of UTC: past the end of the calendar Python can hold
        _fail(path, "last_slot", f"{last} in {last_tz} cannot be moved to {tz_name}: it is out of range")


def read(path: Path) -> dict:
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as e:
        raise RoutineError(f"{path}: (file): cannot read: {e}") from e
    except ValueError as e:
        raise RoutineError(f"{path}: (file): not JSON: {e}") from e


def load(path: Path) -> Routine:
    return parse(path, read(path))


def files(data: Path) -> list:
    """The routine files in `<data>/routines/`, by name. Hidden files (the writer's temp files) are left out."""
    folder = Path(data) / FOLDER
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.name.endswith(SUFFIX) and not p.name.startswith("."))


def load_all(data: Path):
    """Every routine, and one error per bad file: `(routines, [{"routine", "file", "error"}])`. A bad file never
    stops the others."""
    found, errors = [], []
    try:
        paths = files(data)
    except OSError as e:
        return [], [{"routine": None, "file": str(Path(data) / FOLDER), "error": f"cannot list routines: {e}"}]
    for p in paths:
        try:
            found.append(load(p))
        except RoutineError as e:
            errors.append({"routine": p.name[:-len(SUFFIX)], "file": str(p), "error": str(e)})
    return found, errors


def _exists(wall: datetime, tz) -> bool:
    """True when the wall-clock time happens in `tz` (it reads back the same after a round trip through UTC)."""
    at = wall.replace(tzinfo=tz, fold=0)
    return at.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) == wall


def slot_time(wall: datetime, tz) -> datetime:
    """The UTC time of a wall-clock slot in `tz`. A time that happens twice (the hour the clocks go back) is its
    first time. A time that does not exist (the hour they go forward) is the moment the clocks jump: 02:30 on that
    day runs at 03:00. So a slot never comes later in UTC than a slot with a later wall-clock text; two slots that
    land on the same moment run once, as the later one (see `latest_slot`)."""
    step = wall
    while not _exists(step, tz):  # step forward a minute at a time to the first wall time after the jump
        step += timedelta(minutes=1)
    return step.replace(tzinfo=tz, fold=0).astimezone(timezone.utc)


def slot_at(r: Routine) -> datetime:
    """The UTC time of `r.last_slot` (which must be set): its moment when it was moved from another zone, else read
    as a wall time in the routine's zone. A slot at the very ends of the calendar (year 1 east of UTC, year 9999 west
    of it) has no UTC time; it is the earliest or the latest time there is."""
    if r.moved_at is not None:
        return r.moved_at
    wall = datetime.strptime(r.last_slot, SLOT_FORMAT)
    try:
        return slot_time(wall, r.tz)
    except OverflowError:
        edge = datetime.max if wall.year == MAXYEAR else datetime.min
        return edge.replace(tzinfo=timezone.utc)


def latest_slot(r: Routine, now: datetime):
    """`(slot text, UTC time)` of the latest slot at or before `now`, or None when there is none in the last
    `LOOK_BACK_DAYS` days. Slots are compared by their wall-clock text, so each one happens once, even on a day
    the clocks change."""
    today = now.astimezone(r.tz).date()
    best = None
    for back in range(LOOK_BACK_DAYS + 1):
        day = today - timedelta(days=back)
        if day.weekday() not in r.days:
            continue
        for hour, minute in r.at:
            wall = datetime(day.year, day.month, day.day, hour, minute)
            at = slot_time(wall, r.tz)
            text = wall.strftime(SLOT_FORMAT)
            if at <= now and (best is None or text > best[0]):
                best = (text, at)
    return best


def due(r: Routine, now: datetime, grace: timedelta = GRACE) -> Optional[Due]:
    """What the gate must do for `r` at `now`, or None. Only the latest slot is considered, so a Mac that slept
    through several slots runs at most one of them. After a `tz` change, a slot at or before the moved moment has
    already passed, even when its text is later (a move into the second pass of the hour the clocks go back)."""
    if r.paused:
        return None
    latest = latest_slot(r, now)
    if latest is None:
        return None
    slot, at = latest
    if r.last_slot is not None and slot <= r.last_slot:
        return None
    if r.moved_at is not None and at <= r.moved_at:
        return None
    late_by = now - at
    if late_by <= grace:
        return Due(slot, at, "run", late_by)
    if r.last_slot is None:
        # A routine that never ran did not miss this slot: it is older than the routine (or than its last
        # unpause with last_slot cleared). Count from the next slot; no card, nothing in History.
        return Due(slot, at, "start", late_by)
    within = r.within()
    if within is None or late_by <= within:
        return Due(slot, at, "late", late_by)
    return Due(slot, at, "skip", late_by)


def write_last_slot(path: Path, slot: str, write, tz_name: Optional[str] = None) -> None:
    """Set `last_slot`, and `last_slot_tz` (the zone `slot` is in), in the routine file, atomically, with
    `write(path, obj)` (the store's JSON writer). The file is read again first, so a change made since the gate read
    it is kept. `tz_name` defaults to the `tz` the file holds now."""
    obj = read(path)
    if not isinstance(obj, dict):
        raise RoutineError(f"{path}: (file): a routine is a JSON object, not {type(obj).__name__}")
    if tz_name is None:
        tz_name = obj.get("tz") if isinstance(obj.get("tz"), str) else None
    obj["last_slot"] = slot
    if tz_name is not None:
        obj["last_slot_tz"] = tz_name
    write(Path(path), obj)
