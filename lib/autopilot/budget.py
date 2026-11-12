from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autopilot import cli, config, paths

DEFAULT_PATH = Path.home() / ".claude.json"


def read(path: Path = None, now_ms: int = None, stale_min: int = 60) -> dict:
    """Usage windows and extra-usage credits from Claude Code's cached utilization."""
    path = path or DEFAULT_PATH   # looked up per call, so tests can point it at their own file
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    try:
        cache = json.loads(Path(path).read_text()).get("cachedUsageUtilization") or {}
    except Exception as e:  # the cache shape is internal; any change lands here
        return {"ok": False, "error": str(e)}
    return _parse(cache, now_ms, stale_min)


def _parse(cache: dict, now_ms: int, stale_min: int) -> dict:
    out = {"ok": False}
    try:
        util = cache.get("utilization") or {}
        age_min = round((now_ms - cache.get("fetchedAtMs", 0)) / 60000)
        extra = util.get("extra_usage") or {}
        scale = 10 ** int(extra.get("decimal_places") or 2)
        windows = []
        for lim in util.get("limits") or []:
            model = ((lim.get("scope") or {}).get("model") or {}).get("display_name")
            windows.append({"kind": lim.get("kind"), "model": model, "percent": lim.get("percent"),
                            "resets_at": lim.get("resets_at")})
        out = {
            "ok": True,
            "age_min": age_min,
            "stale": age_min > stale_min,
            "windows": windows,
            "credits_used_usd": (extra.get("used_credits") or 0) / scale,
            "credits_limit_usd": (extra.get("monthly_limit") or 0) / scale,
            "spend_limit_reached": bool(extra.get("spend_limit_reached")),
        }
    except Exception as e:  # the cache shape is internal; any change lands here
        out["error"] = str(e)
    return out


def allowance(b: dict, cfg: dict, now: datetime, p: dict = None, spent: dict = None) -> dict:
    """How many new workers the budget allows: None = no limit, 0 = none, 1 = numbers unknown.
    A gateway profile `p` with its `spent` totals replaces the subscription rules (windows, credits and
    staleness) by the daily dollar cap alone."""
    if p is not None and p.get("mode") == "gateway":
        from autopilot import cap, profile
        key_error = profile.check_key(p, now)
        if key_error:   # never a silent fallback to the subscription: workers wait until the key works
            return {"allow": 0, "paused_until": None, "reason": "key", "error": key_error}
        spent = spent or {}
        unknown = bool(spent.get("unknown_today"))
        if unknown or (spent.get("today") or 0) >= cap.today_limit(p, now):
            cap.open_card(now)
            return {"allow": 0, "paused_until": None, "reason": "cap", "cause": "unknown" if unknown else "spent"}
        cap.close_card(now)   # the condition cleared before anyone answered: the card may reopen later today
        return {"allow": None, "paused_until": None, "reason": "ok"}
    rules = cfg["budget"]
    if not b.get("ok"):
        return {"allow": 1, "paused_until": None, "reason": "unknown"}
    stale = b.get("age_min", 0) > rules["stale_min"]
    worst = None
    for w in b.get("windows") or []:
        if (w.get("percent") or 0) >= rules["window_pct"]:
            if worst is None or (w.get("resets_at") or "") > (worst.get("resets_at") or ""):
                worst = w
    if worst is not None:
        until = None
        if worst.get("resets_at"):
            reset = datetime.fromisoformat(worst["resets_at"])
            until = (reset + timedelta(minutes=rules["resume_after_min"])).isoformat()
        return {"allow": 0, "paused_until": until, "reason": "window"}
    limit = b.get("credits_limit_usd") or 0
    if b.get("spend_limit_reached") or (limit and b.get("credits_used_usd", 0) >= limit * rules["credit_pct"] / 100):
        return {"allow": 0, "paused_until": None, "reason": "credits"}
    if stale:
        return {"allow": 1, "paused_until": None, "reason": "stale"}
    return {"allow": None, "paused_until": None, "reason": "ok"}


def read_fast(path: Path = None, now_ms: int = None, stale_min: int = 60) -> dict:
    """Same numbers as `read`, for a hook that runs on every tool call. `~/.claude.json` can be large, so the
    small utilization block is kept in the data folder and re-read only when that file's size or mtime moves."""
    path = path or DEFAULT_PATH
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    side = paths.data_root() / "usage-cache.json"
    try:
        st = Path(path).stat()
        key = [st.st_mtime_ns, st.st_size]
        try:
            kept = json.loads(side.read_text())
        except (OSError, ValueError):
            kept = {}
        if kept.get("key") == key and isinstance(kept.get("cache"), dict):
            return _parse(kept["cache"], now_ms, stale_min)
        cache = json.loads(Path(path).read_text()).get("cachedUsageUtilization") or {}
    except Exception as e:  # noqa: BLE001 - unreadable means unknown
        return {"ok": False, "error": str(e)}
    try:
        side.parent.mkdir(parents=True, exist_ok=True)
        tmp = side.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"key": key, "cache": cache}))
        os.replace(tmp, side)
    except OSError:
        pass
    return _parse(cache, now_ms, stale_min)


def workers_allowance(b: dict, cfg: dict, now: datetime, p: dict = None) -> tuple:
    """(profile, spend totals or None, what the budget allows new workers) for `p`, default the active
    profile. Only a gateway profile pays for spend parsing."""
    from autopilot import profile, spend
    if p is None:
        p, err = profile.active_or_error()
        if err:   # never a guess at which rules apply: no new worker until a profile loads again
            return p, None, {"allow": 0, "paused_until": None, "reason": "profile", "error": err}
    if p.get("mode") != "gateway":
        return p, None, allowance(b, cfg, now)
    s = spend.totals(now.astimezone())
    return p, s, allowance(b, cfg, now, p, s)


def current(gcfg: dict, now: datetime) -> dict:
    """Every budget number in one place. `passes` follows the subscription rules in every mode (dispatcher
    passes always run on the subscription); `workers` follows the active profile."""
    b = read()
    p, s, workers = workers_allowance(b, gcfg, now)
    return {"budget": b, "passes": allowance(b, gcfg, now), "workers": workers, "profile": p, "spent": s}


def parking(b: dict, cfg: dict, p: dict = None):
    """The window that parks running workers: the one at or over `park_pct` that resets last, with when its
    workers may resume. None when usage is under the line, stale or unreadable (unknown is not over).
    Always None for a gateway profile: subscription windows never park gateway workers."""
    if p is not None and p.get("mode") == "gateway":
        return None
    rules = cfg["budget"]
    if not b.get("ok") or b.get("stale") or b.get("age_min", 0) > rules["stale_min"]:
        return None
    worst = None
    for w in b.get("windows") or []:
        if (w.get("percent") or 0) >= rules["park_pct"]:
            if worst is None or (w.get("resets_at") or "") > (worst.get("resets_at") or ""):
                worst = w
    if worst is None:
        return None
    resume_at = None
    try:
        reset = datetime.fromisoformat(worst["resets_at"])
        resume_at = (reset + timedelta(minutes=rules["resume_after_min"])).isoformat()
    except (KeyError, TypeError, ValueError):
        pass
    return {"kind": worst.get("kind"), "model": worst.get("model"), "percent": worst.get("percent"),
            "resets_at": worst.get("resets_at"), "resume_at": resume_at}


@cli.command("budget")
def _cmd(args: list) -> int:
    cur = current(config.load_global(), datetime.now(timezone.utc))
    print(json.dumps({"budget": cur["budget"], "allowance": cur["workers"], "passes": cur["passes"],
                      "spent": cur["spent"]}, indent=1))
    return 0
