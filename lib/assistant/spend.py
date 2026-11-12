"""The spend ledger: what ap2's own workers cost, counted locally from their session transcripts (design section 12,
spend ledger). Ported from v1's `lib/autopilot/spend.py` (commit 519346b), never imported: v1's version keeps its
ledger in v1's data folder.

A gateway shows no dollars to the account that pays for it, so ap2 prices every request itself: the tokens in a
transcript times the list price in `prices.json` next to this file (`<data>/prices.json` overrides one model at a
time). A model with no price counts as unknown spend; it never counts as free silently.

Everything is kept in `<data>/spend/`: `launches.jsonl` (one row per worker start or resume), `settled.jsonl` (a
session's requests, kept when it is stopped or removed, so its spend outlives the transcript) and `cache.json`
(each transcript file's priced requests, keyed by its mtime and size, and the short ids resolved to session ids).
Every function takes the data folder, and the transcripts folder where it reads transcripts; the `claude` session
listing is a function the caller passes in (`listing`), so tests never run `claude`. It writes only inside the data
folder, so it works in shadow mode.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from assistant import transcripts
from assistant.config import ConfigError

FOLDER = "spend"
PRICES = "prices.json"  # the data folder's override, one model at a time
LAUNCHES = "launches.jsonl"
SETTLED = "settled.jsonl"
CACHE = "cache.json"
KINDS = ("worker", "dispatcher", "replies", "refresh", "discover", "oneoff")  # only `worker` is recorded today
_PRICE_KEYS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")
_LONG_PROMPT_TOKENS = 100_000
GRACE_S = 900  # a launch this young that is not resolved or has no transcript yet is not unknown spend
_RETRY_S = 600  # retry resolving one short id at most this often
_YOUNG_RETRY_S = 60  # ... and this often while its launch is younger than GRACE_S
_GIVE_UP_S = 2 * 86400  # and stop trying once its launch is older than this
COPY_SLACK_S = 300  # a request older than its session's first launch minus this is a copy of an older session's
# A request time outside these cannot be shown as a date in every zone (datetime ends at year 9999): unknown spend.
_MIN_TS = 86400.0
_MAX_TS = datetime(9998, 12, 31, tzinfo=timezone.utc).timestamp()

Listing = Optional[Callable[[], Optional[list]]]


class SpendError(ConfigError):
    """The ledger cannot be read or written as it is (a symlink, an unreadable file, a broken price override)."""


# -- the ledger's files ----------------------------------------------------------------------------------------------

def _no_link(path: Path) -> Path:
    if path.is_symlink():  # lstat: a dangling link counts too
        raise SpendError(f"{path} is a symlink; ap2 never follows a link where it keeps its spend ledger. Remove "
                         "the link")
    return path


def folder(data) -> Path:
    return _no_link(Path(data) / FOLDER)


def _file(data, name: str) -> Path:
    return _no_link(folder(data) / name)


def _append(data, name: str, obj: dict) -> None:
    d = folder(data)
    d.mkdir(parents=True, exist_ok=True)
    _no_link(d)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(_file(data, name), flags, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")  # escaped: a lone surrogate in a transcript still writes


def _read_lines(data, name: str) -> list:
    """The file's JSON object lines; a line that is not one is skipped. A missing file is empty; one that cannot
    be read raises SpendError."""
    try:
        raw = _file(data, name).read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []
    except OSError as e:
        raise SpendError(f"cannot read the spend ledger {Path(data) / FOLDER / name}: {e}") from e
    out = []
    for line in raw:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def _local(at: Optional[datetime]) -> datetime:
    return (at or datetime.now()).astimezone()


def _iso(at: Optional[datetime]) -> str:
    return _local(at).isoformat(timespec="seconds")


def _epoch(iso) -> float:
    try:
        return datetime.fromisoformat(str(iso)).timestamp()
    except (ValueError, TypeError, OverflowError):
        return 0.0


def record_launch(data, short: str, kind: str, project, profile: dict, session_id: str = "",
                  at: Optional[datetime] = None) -> None:
    """Remember that a background session started under `profile`; `totals` prices it later. `session_id` is ""
    when the listing did not show the session yet: `totals` resolves the short id then."""
    if kind not in KINDS:
        raise ValueError(f"unknown launch kind {kind!r}")
    if not (isinstance(short, str) and short):
        raise ValueError(f"not a short id: {short!r}")
    _append(data, LAUNCHES, {"short": short, "session": session_id or "", "kind": kind, "project": project,
                             "profile": profile["name"], "mode": profile["mode"], "at": _iso(at)})


# -- prices ----------------------------------------------------------------------------------------------------------

def prices(data) -> dict:
    """The shipped prices, with `<data>/prices.json` (same shape as the shipped file's `models`) over them, one model
    at a time. A broken override raises SpendError: it never makes spend free."""
    base = json.loads((Path(__file__).resolve().parent / PRICES).read_text(encoding="utf-8"))["models"]
    f = _no_link(Path(data) / PRICES)
    try:
        over = json.loads(f.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return base
    except (OSError, ValueError) as e:
        raise SpendError(f"cannot read the price override {f}: {e}") from e
    if not isinstance(over, dict):
        raise SpendError(f"the price override {f} must be a JSON object of model -> prices")
    return {**base, **over}


def _model_key(model) -> str:
    m = re.sub(r"\[[^\[\]]*\]$", "", model or "")
    return re.sub(r"-\d{8}$", "", m)


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _valid(p) -> bool:
    """A price needs every token kind as a number: a half-filled override must not price the rest at $0."""
    if not isinstance(p, dict) or not all(_number(p.get(k)) for k in _PRICE_KEYS):
        return False
    return "over_100k" not in p or _valid(p["over_100k"])


def _price_of(table: dict, model) -> Optional[dict]:
    for key in (_model_key(model), model or ""):
        if key in table:
            return table[key] if _valid(table[key]) else None
    return None


def _cost(table: dict, model, u: dict):
    p = _price_of(table, model)
    if not p:
        return None
    cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
    c1 = int(cc.get("ephemeral_1h_input_tokens") or 0)
    c5_raw = int(cc["ephemeral_5m_input_tokens"] or 0) if "ephemeral_5m_input_tokens" in cc else None
    c_all = int(u.get("cache_creation_input_tokens") or 0)
    tok = {"input": int(u.get("input_tokens") or 0), "output": int(u.get("output_tokens") or 0),
           "cache_read": int(u.get("cache_read_input_tokens") or 0), "cache_write_1h": c1}
    if min(list(tok.values()) + [c_all, c5_raw or 0]) < 0:
        return None  # a negative count: unknown, never a discount
    # v1's rule: without the 5m count, the rest of the cache tokens are 5m ones (none when the 1h kind is more).
    tok["cache_write_5m"] = c5_raw if c5_raw is not None else max(c_all - c1, 0)
    if "over_100k" in p and (tok["input"] + tok["cache_read"] + tok["cache_write_5m"] + tok["cache_write_1h"]
                             > _LONG_PROMPT_TOKENS):
        p = p["over_100k"]
    usd = sum(tok[k] * float(p[k]) for k in _PRICE_KEYS) / 1e6
    return usd if math.isfinite(usd) else None  # a price too big for a number: unknown


# -- transcripts -----------------------------------------------------------------------------------------------------

def _has_tokens(u: dict) -> bool:
    cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
    vals = [u.get(k) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                               "cache_creation_input_tokens")]
    vals += list(cc.values())
    return any(_number(v) and v > 0 for v in vals)


def _parse_file(path: Path, table: dict) -> list:
    """One file's requests as {"id", "ts", "model", "usd"}; streaming writes several lines per requestId with
    growing counts, so only the last usage seen for each counts. `id` is the requestId (or message id) that a
    resumed copy repeats, or a "~<line>" key local to this file. A line without a usable timestamp keeps ts None and
    usd None: unknown spend, never dropped. Only a regular file is read (checked on the open file, so a FIFO or a
    device swapped in after a check never blocks the read)."""
    per_req = {}
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return []
    try:
        with os.fdopen(fd, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                return []
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for n, raw in enumerate(lines):
        try:
            d = json.loads(raw)
        except (ValueError, RecursionError):  # a line nested too deep for the parser is broken, not fatal
            continue
        msg = d.get("message") if isinstance(d, dict) else None
        u = msg.get("usage") if isinstance(msg, dict) else None
        if not isinstance(u, dict) or not u or d.get("type") != "assistant" or not _has_tokens(u):
            continue  # e.g. "<synthetic>" lines Claude Code writes for API errors: all counts 0
        try:
            ts = datetime.fromisoformat(str(d.get("timestamp")).replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError):
            ts = None
        if not _usable_ts(ts):
            ts = None
        model = msg.get("model")
        rid = str(d.get("requestId") or msg.get("id") or f"~{n}")
        try:
            cost = _cost(table, model, u) if ts is not None else None
        except (TypeError, ValueError, OverflowError):  # a count that is not a number: unknown, never $0
            cost = None
        name = model if isinstance(model, str) and model.isprintable() else ""  # never printed raw: an unnamed model
        per_req[rid] = {"id": rid, "ts": ts, "model": _model_key(name), "usd": cost}
    return list(per_req.values())


def _files(main: Path) -> list:
    """A session's transcript and its subagents' (`<dir>/<session id>/subagents/agent-*.jsonl`), only the ones that
    are regular files (a link is followed: one to a FIFO or a device is left out)."""
    return [main] + sorted(p for p in (main.parent / main.stem / "subagents").glob("agent-*.jsonl") if p.is_file())


def _find(projects, sid) -> Optional[Path]:
    if not (isinstance(sid, str) and transcripts.SESSION_ID.match(sid)):
        return None
    return transcripts.find(projects, sid)


def _usable_ts(ts) -> bool:
    return _number(ts) and _MIN_TS <= ts <= _MAX_TS


def _day(ts, tz) -> Optional[str]:
    """The day `ts` is in, in `tz`; None for a time that is not one a date can show (a ledger row an older version
    wrote)."""
    if not _usable_ts(ts):
        return None
    try:
        return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")
    except (ValueError, OverflowError, OSError):
        return None


def request_costs(data, path, tz=None) -> list:
    """Every request of a session transcript and its subagent transcripts as {"day", "model", "usd"}, bucketed by
    the day it happened in `tz` (this computer's timezone by default). usd is None for a model without a price."""
    table = prices(data)
    out = []
    for f in _files(Path(path)):
        for it in _parse_file(f, table):
            out.append({"day": _day(it["ts"], tz), "model": it["model"], "usd": it["usd"]})
    return out


# -- cache -----------------------------------------------------------------------------------------------------------

def _price_key(table: dict, zone: str) -> str:
    """The cache's key: the prices and how the month is cut (`zone`: a fixed offset, or "local" and the offset this
    month starts at)."""
    return hashlib.sha1((json.dumps(table, sort_keys=True) + zone).encode()).hexdigest()


def _load_cache(data, key: Optional[str]) -> dict:
    """The cache as saved; under another `key` its files' requests are dropped. A None key keeps them (for a caller
    that only reads and writes the short ids)."""
    f = _file(data, CACHE)
    try:
        c = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        c = {}
    if not isinstance(c, dict):
        c = {}
    if key is not None and c.get("key") != key:  # new prices or zone: the requests are priced again, short ids stay
        c = {"key": key, "shorts": c.get("shorts"), "tries": c.get("tries")}
    for k in ("shorts", "tries", "files"):
        if not isinstance(c.get(k), dict):
            c[k] = {}
    return c


def _save_cache(data, c: dict) -> None:
    d = folder(data)
    d.mkdir(parents=True, exist_ok=True)
    target = _file(data, CACHE)
    tmp = d / f"{CACHE}.{os.getpid()}.tmp"  # per process: the CLI and a worker check may save at the same time
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    try:
        with open(tmp, "x", encoding="utf-8") as f:
            f.write(json.dumps(c))
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _reqs(items: list, since: float) -> dict:
    """{request id: [ts, usd]} (with the model as a third item when usd is None) of one file's requests from `since`
    on; an undated one is always kept."""
    return {it["id"]: [it["ts"], it["usd"]] + ([it["model"]] if it["usd"] is None else [])
            for it in items if it["ts"] is None or it["ts"] >= since}


def _cached_reqs(c: dict, f: Path, table: dict, since: float, seen: set) -> dict:
    try:
        st = f.stat()
    except OSError:
        return {}
    stamp = [st.st_mtime_ns, st.st_size]
    ent = c["files"].get(str(f))
    if not isinstance(ent, dict) or ent.get("stamp") != stamp or not isinstance(ent.get("reqs"), dict):
        ent = {"stamp": stamp, "reqs": _reqs(_parse_file(f, table), since)}
        c["files"][str(f)] = ent
        c["dirty"] = True
    elif any(v[0] is not None and v[0] < since for v in ent["reqs"].values()):  # the month or the week moved on
        ent["reqs"] = {k: v for k, v in ent["reqs"].items() if v[0] is None or v[0] >= since}
        c["dirty"] = True
    seen.add(str(f))
    return ent["reqs"]


def _session_reqs(files_reqs: list, sid: str) -> dict:
    """One session's requests from all its files; a file-local key is made unique to its file."""
    out = {}
    for i, reqs in enumerate(files_reqs):
        for k, v in reqs.items():
            out[f"~{sid}:{i}:{k[1:]}" if k.startswith("~") else k] = v
    return out


def _resolve(c: dict, launched: dict, now: float, listing: Listing, force: bool = False) -> None:
    """Fill c["shorts"] (short id -> session id) for the ones not known yet, from one call of `listing` (the
    `claude agents --json --all` rows, or None when it failed). `launched` maps a short id to its launch time
    (epoch). One short is retried at most every _RETRY_S (every _YOUNG_RETRY_S while its launch is younger than
    GRACE_S), and no more once its launch is older than _GIVE_UP_S; `force` skips both limits. With no `listing`
    nothing is tried."""
    if listing is None:
        return

    def due(s, at):
        if s in c["shorts"]:
            return False
        if force:
            return True
        wait = _YOUNG_RETRY_S if now - at < GRACE_S else _RETRY_S
        last = c["tries"].get(s, 0)
        return (now - (last if _number(last) else 0)) >= wait and now - at <= _GIVE_UP_S

    todo = [s for s, at in launched.items() if due(s, at)]
    if not todo:
        return
    rows = listing() or []
    for s in todo:
        c["tries"][s] = now
        hit = next((r for r in rows if isinstance(r, dict) and r.get("id") == s), None)
        if hit and isinstance(hit.get("sessionId"), str) and hit["sessionId"]:
            c["shorts"][s] = hit["sessionId"]
            c["tries"].pop(s, None)
        c["dirty"] = True


# -- settle and totals -----------------------------------------------------------------------------------------------

def settle(data, projects, short: str = "", session_id: str = "", listing: Listing = None,
           now: Optional[datetime] = None) -> dict:
    """Price a session now and keep its requests ({request id: [ts, usd]}) in `settled.jsonl`, so its spend outlives
    the transcript and a resumed copy of it is still counted once. Keyed by `session_id` when given; else the short
    id is looked up in the launch rows, the cache, then the listing. A session that cannot be found is recorded as
    unsettled, and its launch stays unknown spend in `totals`. Returns the row written."""
    if not short and not session_id:
        raise ValueError("settle needs a short id or a session id")
    now = _local(now)
    sid = session_id
    if not sid:
        sid = next((r["session"] for r in reversed(_read_lines(data, LAUNCHES))
                    if r.get("short") == short and isinstance(r.get("session"), str) and r["session"]), "")
    if not sid:
        c = _load_cache(data, None)  # only its short ids: the priced files stay as `totals` keyed them
        _resolve(c, {short: now.timestamp()}, now.timestamp(), listing, force=True)
        if c.pop("dirty", False):
            _save_cache(data, c)
        sid = c["shorts"].get(short) or ""
    t = _find(projects, sid) if sid else None
    batch = _iso(now)
    if not t:
        row = {"batch": batch, "short": short, "session": sid, "unsettled": True}
    else:
        table = prices(data)
        reqs = _session_reqs([_reqs(_parse_file(f, table), 0.0) for f in _files(t)], sid)
        row = {"batch": batch, "session": sid, "short": short, "reqs": reqs}
    _append(data, SETTLED, row)
    return row


def totals(data, projects, now: Optional[datetime] = None, mode: str = "gateway", listing: Listing = None) -> dict:
    """Dollars spent today, this week (from Monday; a week that began last month counts its days from last month
    too) and this calendar month on sessions launched in `mode`. `now`'s timezone decides where the days begin. A
    naive (or no) `now` is local time, and then each moment is put on its day by this computer's local rules at that
    moment, so a daylight saving change keeps every request on its local day. Keys:

    - today, week, month: dollars.
    - unknown: a request this month had no price, or a launch this month cannot be matched to its session (its spend
      is not counted, and that shows). A launch younger than GRACE_S that is not resolved or has no transcript yet
      is not unknown: its session may simply not be listed or written yet.
    - unknown_today: the same, but only for cost dated today or a launch made today (what a daily cap trips on).
    - unknown_models: the unpriced model ids of today; unknown_launches: today's launches that could not be priced.

    A session resumed under a new short id counts once, in the mode of its latest launch. A request that two
    sessions hold (a resumed copy repeats its original's) counts once, for the session that has it first. A request
    older than its session's first launch minus COPY_SLACK_S is a copy of an older session's history: not counted
    for it, and not unknown."""
    if now is None or now.tzinfo is None:  # local rules per moment: tz None buckets by the local time of each one
        now = now or datetime.now()
        monday = (now - timedelta(days=now.weekday())).date()
        tz = None
        since = min(datetime(now.year, now.month, 1), datetime(monday.year, monday.month, monday.day)).timestamp()
        zone = "local" + datetime(now.year, now.month, 1).astimezone().strftime("%z")
    else:  # the caller's fixed zone
        tz, zone = now.tzinfo, now.strftime("%z")
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        since = min(midnight.replace(day=1), midnight - timedelta(days=now.weekday())).timestamp()
    # `since` (the earlier of the month's and the week's start) never moves back as `now` moves on, so the cache
    # only ever drops requests from before it.
    now_ts = now.timestamp()
    today = now.strftime("%Y-%m-%d")
    week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
    month_start = now.strftime("%Y-%m-01")
    first_day = min(week_start, month_start)

    rows = [r for r in _read_lines(data, LAUNCHES) if isinstance(r.get("short"), str) and r["short"]]
    last_launch = {}  # short -> launch; the last line wins
    for ln in rows:
        last_launch[ln["short"]] = ln
    table = prices(data)
    c = _load_cache(data, _price_key(table, zone))

    def own_sid(ln: dict) -> str:
        s = ln.get("session")
        return s if isinstance(s, str) and s else ""

    # A settled row names its session and its short id: that matches a launch row written before the session was
    # listed, also once the listing no longer shows it and whatever the cache holds.
    settled, settled_sid = {}, {}
    for ln in _read_lines(data, SETTLED):
        if isinstance(ln.get("short"), str) and ln["short"] and own_sid(ln):
            settled_sid[ln["short"]] = ln["session"]
        if own_sid(ln) and not ln.get("unsettled") and isinstance(ln.get("reqs"), dict):
            settled.setdefault(ln["session"], {}).setdefault(str(ln.get("batch", "")), []).append(ln)

    mine = {s: ln for s, ln in last_launch.items() if ln.get("mode") == mode}
    workers_any = {s: ln for s, ln in last_launch.items() if ln.get("kind") == "worker"}
    _resolve(c, {s: _epoch(ln.get("at")) for s, ln in {**workers_any, **mine}.items()
                 if not own_sid(ln) and s not in settled_sid}, now_ts, listing)

    def sid_of(ln: dict) -> str:
        return own_sid(ln) or settled_sid.get(ln["short"]) or c["shorts"].get(ln["short"]) or ""

    out = {"today": 0.0, "week": 0.0, "month": 0.0, "unknown": False, "unknown_today": False}
    models, launches = set(), 0  # what today's unknown is: unpriced model ids, launches that could not be priced

    def launch_day(ln: dict) -> str:
        return datetime.fromtimestamp(_epoch(ln.get("at")), tz).strftime("%Y-%m-%d")

    def young(at: float) -> bool:
        return now_ts - at < GRACE_S

    for s, ln in mine.items():
        if not sid_of(ln) and launch_day(ln) >= month_start and not young(_epoch(ln.get("at"))):
            out["unknown"] = True
            if launch_day(ln) == today:
                out["unknown_today"] = True
                launches += 1

    session_mode, first_launch = {}, {}
    for ln in rows:
        sid = sid_of(ln)
        if not sid:
            continue
        at = _epoch(ln.get("at"))
        first_launch[sid] = min(first_launch.get(sid, at), at)
        if last_launch[ln["short"]] is not ln:
            continue
        if sid not in session_mode or at >= session_mode[sid][3]:
            session_mode[sid] = (ln.get("mode"), launch_day(ln) >= month_start, launch_day(ln) == today, at,
                                 ln.get("kind"))

    def add(day: str, usd, unknown) -> None:
        if not (first_day <= day <= today):
            return
        if unknown and day >= month_start:  # unknown is this month's
            out["unknown"] = True
            if day == today:
                out["unknown_today"] = True
        if usd:
            if day >= month_start:
                out["month"] += usd
            if day >= week_start:
                out["week"] += usd
            if day == today:
                out["today"] += usd

    # The requests of every session that can share them with one of this mode: its own sessions, and every worker
    # (a resumed copy repeats its original's requests, also across a profile switch).
    seen, per_session = set(), {}
    for sid, (m, recent, launched_today, at, kind) in session_mode.items():
        if m != mode and kind != "worker":
            continue
        if sid in settled:
            batches = settled[sid]
            rows_ = batches[max(batches, key=lambda b: (_epoch(b), b))]
            reqs = {}
            for r in rows_:
                reqs.update(r["reqs"])
        else:
            t = _find(projects, sid)
            if not t:
                if m == mode and not young(at):  # neither a transcript nor settled lines
                    out["unknown"] = out["unknown"] or recent
                    out["unknown_today"] = out["unknown_today"] or launched_today
                    launches += 1 if launched_today else 0
                continue
            reqs = _session_reqs([_cached_reqs(c, f, table, since, seen) for f in _files(t)], sid)
        cutoff = first_launch[sid] - COPY_SLACK_S
        per_session[sid] = {rid: rec for rid, rec in reqs.items()
                            if isinstance(rec, list) and len(rec) >= 2 and (rec[0] is None or
                                                                            (_number(rec[0]) and rec[0] >= cutoff))}

    # A request belongs to the session that has it first (by its line's time; the earlier launch on a tie).
    owner = {}
    for sid, reqs in per_session.items():
        rank = session_mode[sid][3]
        for rid, rec in reqs.items():
            key = (rec[0] if rec[0] is not None else float("inf"), rank, sid)
            if rid not in owner or key < owner[rid]:
                owner[rid] = key
    for sid, reqs in per_session.items():
        m, recent, launched_today, _, _ = session_mode[sid]
        if m != mode:
            continue
        for rid, rec in reqs.items():
            if owner[rid][2] != sid:
                continue  # a copy of a request another session made
            cost = rec[1] if _number(rec[1]) else None
            day = _day(rec[0], tz)
            if cost is None and (day == today or (day is None and launched_today)):
                models.add(rec[2] if len(rec) > 2 and rec[2] else "an unnamed model")
            if day is None:  # no usable time: unknown spend, on the day (and in the month) the session was launched
                out["unknown"] = out["unknown"] or recent
                out["unknown_today"] = out["unknown_today"] or launched_today
                continue
            add(day, cost, cost is None)
    for k in [k for k in c["files"] if k not in seen and not Path(k).exists()]:
        del c["files"][k]
        c["dirty"] = True
    if c.pop("dirty", False):
        _save_cache(data, c)
    out["unknown_models"], out["unknown_launches"] = sorted(models), launches
    return out
