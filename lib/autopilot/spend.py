"""Autopilot's own spend on a gateway, counted locally from session transcripts.

A gateway shows no dollars to the account that pays for it, so autopilot prices every request itself: the
tokens in a transcript times the list price in prices.json (`prices` in the global config overrides one model at
a time). A model with no price counts as unknown spend; it never counts as free silently."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from autopilot import config, paths, sessions, usage

KINDS = ("worker", "dispatcher", "replies", "refresh", "discover", "oneoff")
_PRICE_KEYS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")
_LONG_PROMPT_TOKENS = 100_000
_RETRY_S = 600          # retry resolving one short id at most this often
_GIVE_UP_S = 2 * 86400  # and stop trying once its launch is older than this
_clock = time.time


def _dir() -> Path:
    return paths.data_root() / "spend"


def _append(name: str, obj: dict) -> None:
    d = _dir()
    d.mkdir(parents=True, exist_ok=True)
    with (d / name).open("a") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _read_lines(name: str) -> list:
    out = []
    try:
        raw = (_dir() / name).read_text(errors="replace").splitlines()
    except OSError:
        return out
    for line in raw:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def record_launch(short: str, kind: str, ap, p: dict) -> None:
    """Remember that a background session started under this profile; `totals` prices it later."""
    if kind not in KINDS:
        raise ValueError(f"unknown launch kind {kind!r}")
    _append("launches.jsonl", {"short": short, "kind": kind, "ap": ap, "profile": p["name"], "mode": p["mode"],
                               "at": _now_iso()})
    # The listing may not show the session yet; `totals` keeps trying (and counts it as unknown meanwhile).
    c = _load_cache(_price_key())
    _resolve(c, {short: _clock()})
    if c.pop("dirty", False):
        _save_cache(c)


# --- prices ---

def prices() -> dict:
    base = json.loads((Path(__file__).resolve().parent / "prices.json").read_text())["models"]
    over = config.load_global().get("prices")
    return {**base, **(over if isinstance(over, dict) else {})}


def _model_key(model) -> str:
    m = re.sub(r"\[[^\]]*\]$", "", model or "")
    return re.sub(r"-\d{8}$", "", m)


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _valid(p) -> bool:
    """A price needs every token kind as a number: a half-filled override must not price the rest at $0."""
    if not isinstance(p, dict) or not all(_number(p.get(k)) for k in _PRICE_KEYS):
        return False
    return "over_100k" not in p or _valid(p["over_100k"])


def _price_of(table: dict, model) -> dict | None:
    for key in (_model_key(model), model or ""):
        if key in table:
            return table[key] if _valid(table[key]) else None
    return None


def _cost(table: dict, model, u: dict):
    p = _price_of(table, model)
    if not p:
        return None
    cc = u.get("cache_creation") or {}
    c1 = int(cc.get("ephemeral_1h_input_tokens") or 0)
    c5 = int(cc["ephemeral_5m_input_tokens"]) if "ephemeral_5m_input_tokens" in cc \
        else int(u.get("cache_creation_input_tokens") or 0) - c1
    tok = {"input": int(u.get("input_tokens") or 0), "output": int(u.get("output_tokens") or 0),
           "cache_read": int(u.get("cache_read_input_tokens") or 0), "cache_write_5m": max(c5, 0),
           "cache_write_1h": c1}
    if "over_100k" in p and tok["input"] + tok["cache_read"] + tok["cache_write_5m"] + tok["cache_write_1h"] > _LONG_PROMPT_TOKENS:
        p = p["over_100k"]
    return sum(tok[k] * float(p.get(k) or 0) for k in _PRICE_KEYS) / 1e6


# --- transcripts ---

def _has_tokens(u: dict) -> bool:
    cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
    vals = [u.get(k) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
    vals += list(cc.values())
    return any(_number(v) and v > 0 for v in vals)


def _parse_file(path: Path, table: dict | None = None) -> list:
    """One file's requests as {"ts", "model", "usd"}; streaming writes several lines per requestId with growing
    counts, so only the last usage seen for each counts."""
    table = prices() if table is None else table
    per_req = {}
    try:
        lines = Path(path).read_text(errors="replace").splitlines()
    except OSError:
        return []
    for n, raw in enumerate(lines):
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        msg = d.get("message") if isinstance(d, dict) else None
        u = msg.get("usage") if isinstance(msg, dict) else None
        if not u or d.get("type") != "assistant" or not _has_tokens(u):
            continue  # e.g. "<synthetic>" lines Claude Code writes for API errors: all counts 0
        try:
            ts = datetime.fromisoformat(str(d.get("timestamp")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        model = msg.get("model")
        per_req[d.get("requestId") or msg.get("id") or f"line-{n}"] = {"ts": ts, "model": _model_key(model), "usd": _cost(table, model, u)}
    return list(per_req.values())


def _files(main: Path) -> list:
    sub = sorted((main.parent / main.stem / "subagents").glob("agent-*.jsonl"))
    return [main] + sub


def _day(ts: float, tz) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")


def request_costs(path: Path, tz=None) -> list:
    """Every request of a session transcript and its subagent transcripts, bucketed by the day it happened in
    `tz` (this computer's timezone by default). usd is None for a model without a price."""
    table = prices()
    out = []
    for f in _files(Path(path)):
        for it in _parse_file(f, table):
            out.append({"day": _day(it["ts"], tz), "model": it["model"], "usd": it["usd"]})
    return out


# --- cache ---

def _cache_path() -> Path:
    return _dir() / "cache.json"


def _load_cache(key: str) -> dict:
    try:
        c = json.loads(_cache_path().read_text())
    except (OSError, ValueError):
        c = {}
    if not isinstance(c, dict):
        c = {}
    if c.get("key") != key:  # new prices or timezone offset: per-day totals must be redone, short ids stay valid
        c = {"key": key, "shorts": c.get("shorts") or {}, "tries": c.get("tries") or {}}
    for k in ("shorts", "tries", "files"):
        c.setdefault(k, {})
    return c


def _save_cache(c: dict) -> None:
    _dir().mkdir(parents=True, exist_ok=True)
    tmp = _cache_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(c))
    os.replace(tmp, _cache_path())


def _aggregate(items: list, tz, month_start: str) -> dict:
    """Per-day {"usd", "unknown"} of one file's requests, from the first day of the current month on."""
    days = {}
    for it in items:
        day = _day(it["ts"], tz)
        if day < month_start:
            continue
        d = days.setdefault(day, {"usd": 0.0, "unknown": 0})
        if it["usd"] is None:
            d["unknown"] += 1
        else:
            d["usd"] += it["usd"]
    return days


def _cached_days(c: dict, f: Path, table: dict, tz, month_start: str, seen: set) -> dict:
    try:
        st = f.stat()
    except OSError:
        return {}
    stamp = [st.st_mtime_ns, st.st_size]
    ent = c["files"].get(str(f))
    if not ent or ent["stamp"] != stamp:
        ent = {"stamp": stamp, "days": _aggregate(_parse_file(f, table), tz, month_start)}
        c["files"][str(f)] = ent
        c["dirty"] = True
    elif any(d < month_start for d in ent["days"]):  # the month rolled over since this was cached
        ent["days"] = {d: v for d, v in ent["days"].items() if d >= month_start}
        c["dirty"] = True
    seen.add(str(f))
    return ent["days"]


def _resolve(c: dict, launched: dict, force: bool = False) -> None:
    """Fill c["shorts"] (short id -> full session id) for the ones not known yet, from one `claude agents`
    listing. `launched` maps a short id to its launch time (epoch). One short is retried at most every
    _RETRY_S, and no more once its launch is older than _GIVE_UP_S; `force` skips both limits."""
    now = _clock()
    due = [s for s, at in launched.items() if s not in c["shorts"]
           and (force or (now - c["tries"].get(s, 0) >= _RETRY_S and now - at <= _GIVE_UP_S))]
    if not due:
        return
    from autopilot import workers
    listing = sessions.list_all(workers.claude_bin(config.load_global())) or []
    for s in due:
        c["tries"][s] = now
        hit = sessions.find_by_id(s, listing)
        if hit and hit.get("sessionId"):
            c["shorts"][s] = hit["sessionId"]
            c["tries"].pop(s, None)
        c["dirty"] = True


# --- settle and totals ---

def _price_key(tz=None) -> str:
    off = datetime.now().astimezone(tz).strftime("%z")
    return hashlib.sha1((json.dumps(prices(), sort_keys=True) + off).encode()).hexdigest()


def settle(short: str) -> None:
    """Price a finished session now and keep per-day totals, so its spend outlives the transcript file. A
    session that cannot be found is recorded as unsettled, and its launch stays unknown spend in `totals`."""
    tz = datetime.now().astimezone().tzinfo
    c = _load_cache(_price_key(tz))
    _resolve(c, {short: _clock()}, force=True)
    if c.pop("dirty", False):
        _save_cache(c)
    sid = c["shorts"].get(short)
    t = usage.find_transcript(sid) if sid else None
    batch = _now_iso()
    if not t:
        _append("settled.jsonl", {"batch": batch, "short": short, "session": sid, "unsettled": True})
        return
    for day, d in sorted(_aggregate([{"ts": it["ts"], "usd": it["usd"]} for f in _files(t) for it in _parse_file(f)],
                                    tz, "0000-00-00").items()):
        _append("settled.jsonl", {"batch": batch, "session": sid, "short": short, "day": day,
                                  "usd": d["usd"], "unknown": d["unknown"]})


def _launch_epoch(ln: dict) -> float:
    try:
        return datetime.fromisoformat(ln["at"]).timestamp()
    except (KeyError, ValueError, TypeError):
        return 0.0


def totals(now: datetime, mode: str = "gateway") -> dict:
    """Dollars spent today, this week (from Monday) and this calendar month on sessions launched in `mode`.
    `now`'s timezone decides where the days begin. unknown is True when a request this month had no price, or
    when a launch this month cannot be matched to its session (its spend is not counted, and that shows)."""
    if now.tzinfo is None:
        now = now.astimezone()
    tz = now.tzinfo
    today = now.strftime("%Y-%m-%d")
    week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
    month_start = now.strftime("%Y-%m-01")

    last_launch = {}  # short -> launch; the last line wins
    for ln in _read_lines("launches.jsonl"):
        if ln.get("short"):
            last_launch[ln["short"]] = ln
    mine = {s: ln for s, ln in last_launch.items() if ln.get("mode") == mode}
    table = prices()
    c = _load_cache(_price_key(tz))
    _resolve(c, {s: _launch_epoch(ln) for s, ln in mine.items()})

    out = {"today": 0.0, "week": 0.0, "month": 0.0, "unknown": False}

    def this_month(ln: dict) -> bool:
        return datetime.fromtimestamp(_launch_epoch(ln), tz).strftime("%Y-%m-%d") >= month_start

    for s, ln in mine.items():
        if s not in c["shorts"] and this_month(ln):
            out["unknown"] = True

    # A session resumed under a new short id is one session: it counts once, in the mode of its latest launch.
    session_mode = {}
    for s, ln in last_launch.items():
        sid = c["shorts"].get(s)
        if sid:
            session_mode[sid] = (ln.get("mode"), this_month(ln))

    settled = {}
    for ln in _read_lines("settled.jsonl"):
        if ln.get("session") and not ln.get("unsettled"):
            settled.setdefault(ln["session"], {}).setdefault(ln.get("batch", ""), []).append(ln)

    seen = set()

    def add(day: str, usd, unknown) -> None:
        if not (month_start <= day <= today):
            return
        if unknown:
            out["unknown"] = True
        if usd:
            out["month"] += usd
            if day >= week_start:
                out["week"] += usd
            if day == today:
                out["today"] += usd

    for sid, (m, recent) in session_mode.items():
        if m != mode:
            continue
        if sid in settled:
            for ln in settled[sid][max(settled[sid])]:
                add(ln["day"], ln.get("usd"), ln.get("unknown"))
            continue
        t = usage.find_transcript(sid)
        if not t:
            out["unknown"] = out["unknown"] or recent  # launched this session but neither a transcript nor settled lines remain
            continue
        for f in _files(t):
            for day, d in _cached_days(c, f, table, tz, month_start, seen).items():
                add(day, d["usd"], d["unknown"])
    for k in [k for k in c["files"] if k not in seen and not Path(k).exists()]:
        del c["files"][k]
        c["dirty"] = True
    if c.pop("dirty", False):
        _save_cache(c)
    return out
