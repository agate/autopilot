"""Gateway key health (design section 12, key health): the cached key check, one refresh after the gateway rejects
the key, and `ap2 profile rotate`. Ported from v1's `lib/autopilot/profile.py` "key health, refresh and rotation"
(commit 519346b), never imported: v1's version keeps its records in v1's data folder.

A gateway profile reaches the gateway through its key command (Claude Code runs it as `apiKeyHelper`). This module
runs that command only to learn whether it works: stdin, stdout and stderr are all `DEVNULL`, so no key is ever
read, kept, logged or printed. Only "exit N" or the exception's class name is kept.

- `check` runs the key command for a gateway profile and keeps the answer in `<data>/profile-key.json` (`STATE`):
  a working key is checked again after KEY_TTL, a failing one after KEY_FAIL_TTL, and a failure set by a second
  rejection (`auth: true`) holds KEY_TTL. `cap.allowance` refuses every start and resume while it fails; nothing
  ever falls back to the subscription.
- `note_session_end` (from every worker close): when the session ended because the gateway rejected the key (HTTP
  401 or 403, among the errors `apistop.tail` reads at the end of its transcript), the refresh command (else the
  key command) runs once and `<data>/profile-refresh.json` (`REFRESH`) says when; a second rejection within
  KEY_TTL of that refresh sets the key error instead. A refusal that is not about the key (a 400 from the
  gateway's safety guard, a 429 limit) never refreshes anything.
- `rotate` (`ap2 profile rotate NAME`) runs the rotate command; nothing else ever does.

Both files keep one record per profile, `{"profiles": {name: record}}`: each function reads and writes only its
own profile's record, so one gateway profile's check, refresh or rotation never lifts or overwrites another's
answer or hold. A file in the older shape (one record with a top-level `profile`) is read as that profile's record,
and the next write rewrites it in this shape.

Every function takes the data folder and the time; there is no module clock. Everything is kept inside the data
folder, so it all works in shadow mode. A symlink at either file is refused, never followed.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from assistant import apistop, profiles
from assistant.profiles import ProfileError
from assistant.store import write_json

# Both files are {"profiles": {name: record}}, one record per profile, each with its own "profile".
STATE = "profile-key.json"  # record {"at", "ok", "error", "profile", "auth"?}: the profile's last key check
REFRESH = "profile-refresh.json"  # record {"at", "profile"}: the profile's last refresh after a rejected key
KEY_TTL = timedelta(minutes=10)  # a working key is checked again after this; a second rejection holds this long
KEY_FAIL_TTL = timedelta(seconds=60)  # a failing key is checked again sooner: the user may just have fixed it
KEY_TIMEOUT = 60  # seconds the key and refresh commands may take
ROTATE_TIMEOUT = 120  # seconds the rotate command may take
KEY_FIX = ("Run the profile's key command in a terminal (sign in again if it asks); the next start or check tries "
           "it again.")
AUTH_AGAIN = "the gateway rejected the key again right after a refresh"
AUTH_STATUS = (401, 403)
# v1's `_AUTH_TEXT`: an API error whose text names an authentication failure, read only when it has no status.
_AUTH_TEXT = re.compile(r"\b40[13]\b|authentication[_ ]error|authentication[_ ]failed|invalid[ _-]*(x-)?api[ _-]*key",
                        re.IGNORECASE)
# The gateway's safety guard refuses a conversation's content (lessons 4.3): a new key would not help.
_SAFETY_GUARD = re.compile(r"safety[ _-]*guard", re.IGNORECASE)


# -- where it is kept ------------------------------------------------------------------------------------------------

def _no_link(path: Path) -> Path:
    try:
        st = os.lstat(path)  # lstat: a dangling link counts too
    except (FileNotFoundError, NotADirectoryError):
        return path
    except OSError as e:
        raise ProfileError(f"cannot look at {path}: {e.strerror or type(e).__name__}") from e
    if stat.S_ISLNK(st.st_mode):
        raise ProfileError(f"{path} is a symlink; ap2 never follows a link where it keeps the key health. Remove "
                           "the link")
    return path


def _read(path: Path) -> dict:
    """The file's object, or {} when it is missing, unreadable or not an object (read as never checked). A symlink
    raises."""
    f = _no_link(path)
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # UnicodeDecodeError is a ValueError
        return {}
    return raw if isinstance(raw, dict) else {}


def _records(path: Path) -> dict:
    """Every profile's record in a key health file, by name. `{"profiles": {...}}` keeps the entries that are
    objects; a `profiles` that is not an object is no record at all; a file in the older shape (one record, its
    `profile` a name) is that profile's one record. Anything else reads as never checked. A symlink raises."""
    raw = _read(path)
    if "profiles" in raw:
        book = raw["profiles"]
        if not isinstance(book, dict):
            return {}
        return {k: v for k, v in book.items() if isinstance(k, str) and isinstance(v, dict)}
    name = raw.get("profile")
    return {name: raw} if isinstance(name, str) and name else {}


def _record(path: Path, name) -> dict:
    """Profile `name`'s record, or {} when it has none. A record whose own `profile` is not `name` is not its."""
    rec = _records(path).get(name) if isinstance(name, str) else None
    return rec if rec is not None and rec.get("profile") == name else {}


def _put(path: Path, name: str, rec: Optional[dict]) -> None:
    """Set profile `name`'s record to `rec`, or drop it (None); every other profile's record is kept. The file is
    removed when no profile is left; a drop for a profile with no record writes nothing. A symlink raises."""
    book = _records(path)
    if rec is None:
        if name not in book and book:
            return
        book.pop(name, None)
        if not book:
            _remove(path)
            return
    else:
        book[name] = rec
    _write(path, {"profiles": book})


def _write(path: Path, rec: dict) -> None:
    f = _no_link(path)
    try:
        write_json(f, rec)
    except OSError as e:
        raise ProfileError(f"cannot write {f}: {e.strerror or type(e).__name__}") from e


def _remove(path: Path) -> None:
    f = _no_link(path)
    try:
        os.unlink(f)
    except FileNotFoundError:
        pass
    except OSError as e:
        raise ProfileError(f"cannot remove {f}: {e.strerror or type(e).__name__}") from e


def _when(text) -> Optional[datetime]:
    try:
        t = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.astimezone()


def _aware(now: datetime) -> datetime:
    return now if now.tzinfo else now.astimezone()


def _fresh(rec: dict, now: datetime, ttl: timedelta) -> bool:
    """The record's `at` is within `ttl` of `now`, either way: one far in the future is not fresh either."""
    t = _when(rec.get("at"))
    return t is not None and abs(_aware(now) - t) < ttl


def _run_silent(cmd: str, timeout: int) -> str:
    """Run a command with every stream closed. "" on success, else "exit N" or the exception's class name: never
    its output, which may hold a key."""
    try:
        r = subprocess.run(cmd, shell=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return type(e).__name__
    return "" if r.returncode == 0 else f"exit {r.returncode}"


def _is_gateway(p: dict) -> bool:
    return isinstance(p, dict) and p.get("mode") == "gateway" and isinstance(p.get("gateway"), dict)


# -- the check -------------------------------------------------------------------------------------------------------

def check(data, p: dict, now: datetime) -> Optional[str]:
    """The key command's error text for profile `p`, or None when it works. The subscription runs nothing and is
    None. The answer is kept as `p`'s record in `<data>/profile-key.json`: a working key for KEY_TTL, a failing one
    for KEY_FAIL_TTL, a failure set by a second rejection (`auth`) for KEY_TTL. Only `p`'s own record is read and
    written; an unreadable or malformed one, or one whose `at` is too far off, is not used: the command runs
    again. Raises ProfileError when the file is a symlink or cannot be written."""
    if not _is_gateway(p):
        return None
    f = Path(data) / STATE
    rec = _record(f, p.get("name"))
    ok = rec.get("ok") is True
    ttl = KEY_TTL if ok or rec.get("auth") is True else KEY_FAIL_TTL
    if isinstance(rec.get("ok"), bool) and _fresh(rec, now, ttl):
        if ok:
            return None
        err = rec.get("error")
        return err if isinstance(err, str) and err else "the key command failed"
    why = _run_silent(p["gateway"].get("key_command") or "", KEY_TIMEOUT)
    err = f"the key command failed ({why})" if why else None
    _put(f, p.get("name"), {"at": _aware(now).isoformat(), "ok": err is None, "error": err, "profile": p.get("name")})
    return err


def cached_error(data, p: dict) -> Optional[str]:
    """The last key check's error for `p`, from `p`'s own record only (no command runs): None when it worked or
    never ran for `p` (or for the subscription). A symlinked record is that error itself."""
    if not _is_gateway(p):
        return None
    try:
        rec = _record(Path(data) / STATE, p.get("name"))
    except ProfileError as e:
        return str(e)
    if rec.get("ok") is not False:
        return None
    err = rec.get("error")
    return err if isinstance(err, str) and err else "the key command failed"


# -- a rejected key ---------------------------------------------------------------------------------------------------

def auth_failed(errors: list, after: Optional[datetime]) -> bool:
    """True when one of the API errors (`apistop.tail`'s `errors`) newer than `after` is an authentication failure:
    its `status` is 401 or 403, or, with no status, its text or `error` names one (`_AUTH_TEXT`). Another status
    (400, 429, 500, ...) never is, whatever the text says, and neither is a refusal by the gateway's safety guard
    (lessons 4.3): it refuses the conversation's content, and a new key would not help. An error with no time
    counts even after a refresh."""
    for e in errors or []:
        t = e.get("at")
        if after is not None and isinstance(t, datetime) and t <= _aware(after):
            continue  # older than the last refresh
        text = f"{e.get('text') or ''} {e.get('error') or ''}"
        if _SAFETY_GUARD.search(text):
            continue
        status = e.get("status")
        if isinstance(status, int) and not isinstance(status, bool):
            if status in AUTH_STATUS:
                return True
            continue
        if _AUTH_TEXT.search(text):
            return True
    return False


def note_session_end(data, projects, session_id: str, profile_name: Optional[str], now: datetime) -> bool:
    """A worker's session ended on profile `profile_name` (its own; None is the subscription). When it is a gateway
    profile and the session's transcript ends on an authentication failure newer than the profile's last refresh:
    run the refresh command (else the key command) once, write the profile's record in `profile-refresh.json` and
    forget the profile's key check, so its next check runs the key command. A second failure within KEY_TTL of that
    refresh writes the profile's key error (`auth: true`) instead, which holds new work for KEY_TTL. Another
    profile's records are never touched. The failure's own time is what counts (an error with no time: the close
    time `now`): a session the gateway rejected ends its turn and is closed as idle up to an hour later. True when
    a failure was found."""
    if not session_id or not profile_name:
        return False
    try:
        p = profiles.load(data, profile_name)
    except ProfileError:
        return False  # a profile that is gone or broken: nothing to refresh
    if not _is_gateway(p):
        return False
    rf = Path(data) / REFRESH
    last = _record(rf, p["name"])
    errors = (apistop.tail(projects, session_id) or {}).get("errors") or []
    after = _when(last.get("at"))
    if not auth_failed(errors, after):
        return False

    def soon(e: dict) -> bool:
        """The error happened within KEY_TTL of the refresh (one with no time: the session closed within it)."""
        t = e.get("at")
        if isinstance(t, datetime):
            return _aware(t) - after < KEY_TTL
        return _fresh(last, now, KEY_TTL)
    if last and after is not None and auth_failed([e for e in errors if soon(e)], after):
        _put(Path(data) / STATE, p["name"], {"at": _aware(now).isoformat(), "ok": False, "auth": True,
                                             "profile": p["name"], "error": AUTH_AGAIN})
        return True
    gw = p["gateway"]
    _run_silent(gw.get("refresh_command") or gw.get("key_command") or "", KEY_TIMEOUT)
    _put(rf, p["name"], {"at": _aware(now).isoformat(), "profile": p["name"]})
    _put(Path(data) / STATE, p["name"], None)
    return True


# -- rotation ---------------------------------------------------------------------------------------------------------

def rotate(data, name: str, now: datetime) -> dict:
    """`ap2 profile rotate NAME`: run the profile's rotate command (120 s, every stream closed), then forget NAME's
    key check and NAME's last refresh (another profile's records stay). Raises ProfileError for the subscription, an
    unknown name, a profile with no rotate_command, and a failed command (its exit code only, never its output).
    `now` is not used: a rotation keeps no time of its own (the signature matches the module's other functions)."""
    p = profiles.load(data, name)
    if not _is_gateway(p):
        raise ProfileError(f"{name} is a subscription profile: it has no key to rotate")
    cmd = p["gateway"].get("rotate_command")
    if not cmd:
        raise ProfileError(f"{name} has no rotate_command, so there is nothing to run")
    why = _run_silent(cmd, ROTATE_TIMEOUT)
    if why:
        raise ProfileError(f"rotate_command failed ({why})")
    _put(Path(data) / STATE, name, None)
    _put(Path(data) / REFRESH, name, None)
    return {"rotated": name}
