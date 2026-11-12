"""Autopilot's own spend on a gateway, counted locally from session transcripts.

A gateway shows no dollars to the account that pays for it, so autopilot prices every request itself: the
tokens in a transcript times the list price in prices.json (`prices` in the global config overrides one model at
a time). A model with no price counts as unknown spend; it never counts as free silently."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from autopilot import config, paths, sessions, usage

KINDS = ("worker", "dispatcher", "replies", "refresh", "discover", "oneoff")
_PRICE_KEYS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")
_LONG_PROMPT_TOKENS = 100_000


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


# --- prices ---

def prices() -> dict:
    base = json.loads((Path(__file__).resolve().parent / "prices.json").read_text())["models"]
    return {**base, **(config.load_global().get("prices") or {})}


def _model_key(model) -> str:
    m = re.sub(r"\[[^\]]*\]$", "", model or "")
    return re.sub(r"-\d{8}$", "", m)


def _price_of(table: dict, model) -> dict | None:
    return table.get(_model_key(model)) or table.get(model or "")


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
    if p.get("over_100k") and tok["input"] + tok["cache_read"] + tok["cache_write_5m"] + tok["cache_write_1h"] > _LONG_PROMPT_TOKENS:
        p = p["over_100k"]
    return sum(tok[k] * float(p.get(k) or 0) for k in _PRICE_KEYS) / 1e6


# --- transcripts ---

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
        if not u or d.get("type") != "assistant":
            continue
        try:
            ts = datetime.fromisoformat(str(d.get("timestamp")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        model = msg.get("model")
        per_req[d.get("requestId") or f"line-{n}"] = {"ts": ts, "model": _model_key(model), "usd": _cost(table, model, u)}
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
    if c.get("prices") != key:
        c = {"prices": key, "shorts": c.get("shorts") or {}}
    c.setdefault("shorts", {})
    c.setdefault("files", {})
    return c


def _save_cache(c: dict) -> None:
    _dir().mkdir(parents=True, exist_ok=True)
    tmp = _cache_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(c))
    os.replace(tmp, _cache_path())


def _cached_items(c: dict, f: Path, table: dict, seen: set) -> list:
    try:
        st = f.stat()
    except OSError:
        return []
    stamp = [st.st_mtime_ns, st.st_size]
    ent = c["files"].get(str(f))
    if not ent or ent["stamp"] != stamp:
        ent = {"stamp": stamp, "items": _parse_file(f, table)}
        c["files"][str(f)] = ent
        c["dirty"] = True
    seen.add(str(f))
    return ent["items"]


def _resolve(c: dict, shorts: set) -> None:
    """Fill c["shorts"] (short id -> full session id) for the ones not known yet, from one `claude agents` listing."""
    missing = [s for s in shorts if s not in c["shorts"]]
    if not missing:
        return
    from autopilot import workers
    listing = sessions.list_all(workers.claude_bin(config.load_global())) or []
    for s in missing:
        hit = sessions.find_by_id(s, listing)
        if hit and hit.get("sessionId"):
            c["shorts"][s] = hit["sessionId"]
            c["dirty"] = True


# --- settle and totals ---

def settle(short: str) -> None:
    """Price a finished session now and keep per-day totals, so its spend outlives the transcript file."""
    c = _load_cache(_price_key())
    _resolve(c, {short})
    if c.pop("dirty", False):
        _save_cache(c)
    sid = c["shorts"].get(short)
    t = usage.find_transcript(sid) if sid else None
    if not t:
        return
    tz = datetime.now().astimezone().tzinfo
    days = {}
    for it in request_costs(t, tz):
        d = days.setdefault(it["day"], {"usd": 0.0, "unknown": 0})
        if it["usd"] is None:
            d["unknown"] += 1
        else:
            d["usd"] += it["usd"]
    batch = _now_iso()
    for day, d in sorted(days.items()):
        _append("settled.jsonl", {"batch": batch, "session": sid, "short": short, "day": day,
                                  "usd": d["usd"], "unknown": d["unknown"]})


def _price_key() -> str:
    return hashlib.sha1(json.dumps(prices(), sort_keys=True).encode()).hexdigest()


def totals(now: datetime, mode: str = "gateway") -> dict:
    """Dollars spent today, this week (from Monday) and this calendar month on sessions launched in `mode`.
    `now`'s timezone decides where the days begin. unknown is True when a request this month had no price."""
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
    table = prices()
    c = _load_cache(_price_key())
    _resolve(c, {s for s, ln in last_launch.items() if ln.get("mode") == mode})

    # A session resumed under a new short id is one session: it counts once, in the mode of its latest launch.
    session_mode = {}
    for s, ln in last_launch.items():
        sid = c["shorts"].get(s)
        if sid:
            session_mode[sid] = ln.get("mode")

    settled = {}
    for ln in _read_lines("settled.jsonl"):
        if ln.get("session"):
            settled.setdefault(ln["session"], {}).setdefault(ln.get("batch", ""), []).append(ln)

    out = {"today": 0.0, "week": 0.0, "month": 0.0, "unknown": False}
    seen = set()

    def add(day: str, usd, unknown: bool) -> None:
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

    for sid, m in session_mode.items():
        if m != mode:
            continue
        if sid in settled:
            for ln in settled[sid][max(settled[sid])]:
                add(ln["day"], ln.get("usd"), bool(ln.get("unknown")))
            continue
        t = usage.find_transcript(sid)
        if not t:
            continue
        for f in _files(t):
            for it in _cached_items(c, f, table, seen):
                add(_day(it["ts"], tz), it["usd"], it["usd"] is None)
    # Keep cache entries of other modes' files; drop only files that no longer exist.
    for k in [k for k in c["files"] if k not in seen and not Path(k).exists()]:
        del c["files"][k]
        c["dirty"] = True
    if c.pop("dirty", False):
        _save_cache(c)
    return out
