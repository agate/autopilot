"""The daily cap of a gateway profile (design section 12, daily cap): today's raise, the one card per day, and the
pause. Ported from v1's `lib/autopilot/cap.py` (commit 519346b), never imported: v1's version keeps its state in
v1's data folder and pauses v1's autopilots.

The cap is soft. When today's gateway spend (`spend.totals`, every gateway profile together) reaches the active
gateway profile's `daily_cap_usd`, raised by today's raises, or when some of today's gateway cost cannot be priced,
no worker starts or resumes; running workers keep going. One card per local day asks the user to raise today's limit
by 50% or 100%, to accept today's cost that cannot be priced, or to pause. The pause (`paused.json`) refuses every
start and resume in any mode, also past midnight, until `ap2 cap resume` lifts it; the `pause` answer also stops and
parks every running worker (`Workers.cap_stop`). A subscription profile has no cap: its usage windows stay the park
hook's job. Before any spend is read, a gateway profile whose key command fails (`keys.check`) refuses every start
and resume too (`reason: key`), with no card and no fallback to the subscription.

"Today" is the calendar day of `now`: an aware `now` at its own offset, a naive one by this computer's local rules,
as in `spend.totals`. Every function takes the data folder and the time; there is no module clock. Everything is
kept inside the data folder (`cap-state.json`, `paused.json`), so it works in shadow mode. Each gateway allowance
also keeps the totals it read (`spend.remember`, `spend/last.json`), so the pages show today's count without reading
the ledger; a failed write there never changes the allowance.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from assistant import cards, config, keys, spend
from assistant.cards import CardError
from assistant.config import ConfigError
from assistant.store import Store, write_json

STATE = "cap-state.json"  # {"day", "raise_pct", "card", "opened"?, "accept_unknown"?, "card_id"?}: today only
PAUSED = "paused.json"  # {"reason": "cap", "at": <stamp>}: lasts until `ap2 cap resume`, also past midnight
OPTIONS = {"raise50": 50, "raise100": 100}  # a raise in percent of `daily_cap_usd`, for today only
ACCEPT_UNKNOWN = "accept_unknown"  # today's cost that cannot be priced counts as $0 for the cap until midnight
PAUSE = "pause"
ANSWERS = (*OPTIONS, ACCEPT_UNKNOWN, PAUSE)
PAUSE_NOTE = "paused at the daily cap"
REASONS = ("ok", "cap", "paused", "key")  # the allowance's reasons: `key` is a gateway key that fails (keys.py)
CAUSES = ("spent", "unknown")
CARD_STATES = ("open", "answered", None)
FEED = "cap"
KIND = "cap"
BY = "ap2 cap"


class CapError(ConfigError):
    """The cap refuses a start or a resume, or its files cannot be used. A ConfigError, so `ap2` prints one line
    and exits 1."""


# -- where it is kept ------------------------------------------------------------------------------------------------

def _lstat(path: Path):
    try:
        return os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as e:
        raise CapError(f"cannot look at {path}: {e.strerror or type(e).__name__}") from e


def _no_link(path: Path) -> Path:
    st = _lstat(path)  # lstat: a dangling link counts too
    if st is not None and stat.S_ISLNK(st.st_mode):
        raise CapError(f"{path} is a symlink; ap2 never follows a link where it keeps the daily cap. Remove the link")
    return path


def day(now: Optional[datetime]) -> str:
    """The calendar day of `now`: an aware one at its own offset, a naive one (local wall time) as it is."""
    return (now if now is not None else datetime.now()).strftime("%Y-%m-%d")


def _blank(d: str) -> dict:
    return {"day": d, "raise_pct": 0, "card": None}


def _raw(data) -> Optional[dict]:
    """What cap-state.json holds, or None when it is missing, unreadable or not an object. A symlink raises."""
    f = _no_link(Path(data) / STATE)
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # UnicodeDecodeError is a ValueError
        return None
    return raw if isinstance(raw, dict) else None


def _today(raw: Optional[dict], d: str) -> dict:
    if not raw or raw.get("day") != d:
        return _blank(d)
    rp = raw.get("raise_pct")
    if isinstance(rp, bool) or not isinstance(rp, int) or rp < 0 or raw.get("card") not in CARD_STATES:
        return _blank(d)
    out = {"day": d, "raise_pct": rp, "card": raw["card"]}
    if isinstance(raw.get("opened"), dict):
        out["opened"] = raw["opened"]
    if raw.get("accept_unknown") is True:
        out["accept_unknown"] = True
    cid = raw.get("card_id")
    if isinstance(cid, str) and cards.CARD_ID.match(cid):
        out["card_id"] = cid
    return out


def state(data, now: Optional[datetime]) -> dict:
    """Today's cap state. A new local day starts blank (`raise_pct` 0, `card` None), so a raise ends at midnight; an
    unreadable or malformed file reads as blank for today. `opened` is {limit, cause} of the last time the card
    opened; `accept_unknown` is True once today's unknown cost was accepted; `card_id` is today's card."""
    return _today(_raw(data), day(now))


def _load(data, now) -> tuple:
    """(today's state, the id of an earlier day's card that was left open, or None)."""
    raw, d = _raw(data), day(now)
    s = _today(raw, d)
    stale = None
    if raw and raw.get("day") != d and raw.get("card") == "open" and isinstance(raw.get("card_id"), str) \
            and cards.CARD_ID.match(raw["card_id"]):
        stale = raw["card_id"]
    return s, stale


def _save(data, s: dict) -> None:
    f = _no_link(Path(data) / STATE)
    try:
        write_json(f, s)  # a temp file of this process, then os.replace
    except OSError as e:
        raise CapError(f"cannot write {f}: {e}") from e


def today_limit(data, p: dict, now: Optional[datetime]) -> Optional[float]:
    """The profile's `daily_cap_usd` raised by today's raises; None when the profile has no cap."""
    base = p.get("daily_cap_usd")
    if isinstance(base, bool) or not isinstance(base, (int, float)):
        return None
    return float(base) * (1 + state(data, now)["raise_pct"] / 100)


def unknown_accepted(data, now: Optional[datetime]) -> bool:
    return state(data, now).get("accept_unknown") is True


# -- the pause -------------------------------------------------------------------------------------------------------

def paused(data) -> Optional[dict]:
    """The pause, or None. A `paused.json` that is there but cannot be read (or is a symlink, never followed)
    counts as paused, with `reason: unknown`: never start work by accident."""
    f = Path(data) / PAUSED
    try:
        st = os.lstat(f)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as e:
        return {"reason": "unknown", "error": f"cannot look at {f}: {e.strerror or type(e).__name__}"}
    if stat.S_ISLNK(st.st_mode):
        return {"reason": "unknown", "error": f"{f} is a symlink"}
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"reason": "unknown", "error": f"cannot read {f}: {e}"}
    return raw if isinstance(raw, dict) else {"reason": "unknown", "error": f"{f} is not a JSON object"}


def pause(data, now: Optional[datetime], reason: str = "cap") -> dict:
    """Write `paused.json`. Every start and resume is refused from now on, until `resume`."""
    f = _no_link(Path(data) / PAUSED)
    rec = {"reason": reason, "at": cards.stamp(now if now is not None else datetime.now())}
    try:
        write_json(f, rec)
    except OSError as e:
        raise CapError(f"cannot write {f}: {e}") from e
    return rec


def resume(data) -> dict:
    """Lift the pause (`ap2 cap resume`): remove `paused.json`. A no-op when not paused."""
    f = _no_link(Path(data) / PAUSED)
    if _lstat(f) is None:
        return {"paused": False, "was": False}
    try:
        os.unlink(f)
    except FileNotFoundError:
        return {"paused": False, "was": False}
    except OSError as e:
        raise CapError(f"cannot remove {f}: {e}") from e
    return {"paused": False, "was": True}


# -- the card ----------------------------------------------------------------------------------------------------------

def _usd(x, lang: str = "en") -> str:
    """A dollar amount with two decimals, `$?` for a non-number. A total `spend.totals` clamped (a sum too big for a
    number, `sys.float_info.max`) is a marker with no `$`, not a 309-digit amount that looks real."""
    if not isinstance(x, (int, float)) or isinstance(x, bool):
        return "$?"
    if x == sys.float_info.max:
        return "太大，无法显示" if lang.split("-")[0] == "zh" else "too large to show"
    return f"${x:,.2f}"


def options(cause: str) -> list:
    return ["raise50", "raise100"] + ([ACCEPT_UNKNOWN] if cause == "unknown" else []) + [PAUSE]


def _words(lang: str, limit, cause: str, t: dict, profile: str, prices: Path) -> dict:
    spent, cap_usd = _usd(t.get("today"), lang), _usd(limit, lang)
    models = ", ".join(t.get("unknown_models") or [])
    launches = t.get("unknown_launches") or 0
    if lang.split("-")[0] == "zh":
        if cause == "spent":
            what = f"今天的网关花费已到每日上限：{spent} / {cap_usd}（访问配置 {profile}）。"
        else:
            what = f"今天有一部分网关花费无法计价，所以每日上限生效：已计价 {spent} / {cap_usd}（访问配置 {profile}）。"
        where = "在你回答之前，不会有新的 worker 启动或恢复；正在运行的 worker 会继续。"
        if cause == "unknown":
            where += (f"今天未计价的模型：{models or '无'}。今天没有对上会话的启动：{launches} 次。要给模型定价，"
                      f"把它加到 {prices}（格式同自带 prices.json 的 `models`）。")
        do = "运行 `ap2 cap answer raise50` 或 `ap2 cap answer raise100`，把今天的上限提高 50% 或 100%；"
        if cause == "unknown":
            do += "或 `ap2 cap answer accept_unknown`，今天无法计价的花费按 $0 计，直到午夜；"
        do += "或 `ap2 cap answer pause`，停下并暂停所有正在运行的 worker（之后用 `ap2 cap resume` 解除暂停）。"
        return {"what": what, "where": where, "do": do}
    if cause == "spent":
        what = f"Today's gateway spend reached the daily cap: {spent} of {cap_usd} (access profile {profile})."
    else:
        what = (f"Some of today's gateway spend cannot be priced, so the daily cap holds: {spent} priced of "
                f"{cap_usd} (access profile {profile}).")
    where = "No new worker starts or resumes until you answer; running workers keep going."
    if cause == "unknown":
        where += (f" Unpriced models today: {models or 'none'}. Launches not matched to a session today: {launches}."
                  f" To price a model, add it to {prices} (the same shape as the shipped prices.json `models`).")
    do = "Run `ap2 cap answer raise50` or `ap2 cap answer raise100` to raise today's limit by 50% or 100%"
    if cause == "unknown":
        do += ", `ap2 cap answer accept_unknown` to count today's unpriced cost as $0 until midnight"
    do += (", or `ap2 cap answer pause` to stop and park every running worker (`ap2 cap resume` lifts the "
           "pause later).")
    return {"what": what, "where": where, "do": do}


def _fields(store: Store, d: str, limit, cause: str, t: dict, profile: str, data) -> dict:
    langs = store.languages
    words = {lang: _words(lang, limit, cause, t, profile, Path(data) / spend.PRICES) for lang in langs}
    title = {lang: ("每日上限" if lang.split("-")[0] == "zh" else "Daily cap reached") for lang in langs}
    ask = {"type": "approve", "from": "cap", "reason": "cap", "options": options(cause), "limit": limit,
           "cause": cause, "text": words[langs[0]]["what"]}
    return {"lane": "needs", "pri": "P0", "title": title, "ask": ask, "limit": limit, "cause": cause,
            "plain": {line: {lang: words[lang][line] for lang in langs} for line in cards.PLAIN_LINES}}


def _put_card(data, store: Store, s: dict, limit, cause: str, t: dict, profile: str) -> str:
    """Today's card, made or opened again with the limit and cause: one card per day (`key` cap:<day>)."""
    key = f"cap:{s['day']}"
    fields = _fields(store, s["day"], limit, cause, t, profile, data)
    cid = s.get("card_id")
    if cid is None:  # the state file was lost: the day's card is found by its key
        cid = next((c["id"] for c in store.list(kind=KIND) if c.get("key") == key), None)
    if cid is not None:
        try:
            return store.update(cid, fields, by=BY, did=f"daily cap holds ({cause})")["id"]
        except CardError:
            if _exists(store, cid):
                raise
    card = dict(fields, kind=KIND, feed=FEED, links=[], origin="found", key=key, day=s["day"])
    return store.add(card, by=BY)["id"]


def _exists(store: Store, cid: str) -> bool:
    try:
        store.get(cid)
        return True
    except CardError:
        return False


def _close_ask(store: Store, cid: Optional[str], did: str) -> None:
    """Close the card's cap ask and move it to Done; a user's own ask that landed meanwhile stays."""
    if not cid:
        return
    try:
        store.update_checked(cid, {"ask": None, "lane": "done"}, by=BY,
                             check=lambda c: did if (c.get("ask") or {}).get("from") == "cap" else False)
    except CardError:
        pass  # the card is gone: nothing to close


def _store(data, now) -> Store:
    return Store(Path(data), config.load(Path(data)), clock=lambda: now if now is not None else datetime.now())


def _expire(store: Store, stale: Optional[str]) -> None:
    _close_ask(store, stale, "daily cap card closed: its day ended")


def open_card(data, now, limit, cause: str, t: dict, profile: str, store: Optional[Store] = None) -> bool:
    """Open today's card: once per local day, and again whenever the cap holds at another limit or for another
    cause than the one it last opened for (spend crossed a raised limit; or spend crossed the limit after today's
    unknown cost was accepted). True when this call opened it. The card is written before the state, so a card
    that could not be written is tried again next time."""
    store = store or _store(data, now)
    s, stale = _load(data, now)
    _expire(store, stale)
    if s["card"] == "open":
        return False
    seen = {"limit": limit, "cause": cause}
    if s["card"] == "answered" and s.get("opened") == seen:
        return False
    cid = _put_card(data, store, s, limit, cause, t, profile)
    s.update(card="open", opened=seen, card_id=cid)
    _save(data, s)
    return True


def close_card(data, now, store: Optional[Store] = None) -> None:
    """The cap no longer holds and nobody answered: take the card back (its ask closed, moved to Done), so it can
    open again later today."""
    s, stale = _load(data, now)
    if stale is None and s["card"] != "open":
        return
    store = store or _store(data, now)
    _expire(store, stale)
    if s["card"] == "open":
        _close_ask(store, s.get("card_id"), "daily cap card closed: the cap no longer holds")
        s["card"] = None
    _save(data, s)


# -- the allowance -----------------------------------------------------------------------------------------------------

def allowance(data, projects, now, p: dict, listing=None, store: Optional[Store] = None) -> dict:
    """May a worker start or resume now on profile `p`? `{"allow": 0 | None, "reason": REASONS, "cause"?, "limit"?,
    "spent"?, "error"?, "profile"?}`. Paused (any mode): `allow 0`, `reason paused`. A subscription profile: `allow
    None`, `reason ok`, with no spend parsed, and an unanswered card is taken back. A gateway profile whose key command
    fails (`keys.check`, cached): `allow 0`, `reason key`, with its `error` and the profile's name, before any spend is
    read and with no card opened; nothing falls back to the subscription. Otherwise for a gateway profile: today's
    gateway spend (every gateway profile together) at or over today's limit, or today's cost that cannot be priced
    (unless accepted), opens the card and is `allow 0`, `reason cap`; otherwise an unanswered card is taken back and it
    is `allow None`, `reason ok`. A card that cannot be written never lets work start: the refusal stands, with
    `card_error`."""
    pz = paused(data)
    if pz is not None:
        return {"allow": 0, "reason": "paused", "paused": pz}
    if p.get("mode") != "gateway":
        out = {"allow": None, "reason": "ok"}
        try:
            close_card(data, now, store)  # no cap on this profile: a card the gateway opened is taken back
        except (CardError, OSError) as e:
            out["card_error"] = str(e) or type(e).__name__
        return out
    err = keys.check(data, p, now)
    if err:
        return {"allow": 0, "reason": "key", "error": err, "profile": p.get("name")}
    limit = today_limit(data, p, now)
    try:
        t = spend.totals(data, projects, now, "gateway", listing)
    except OSError as e:
        raise CapError(f"cannot read the spend ledger for the daily cap: {e}") from e
    try:
        spend.remember(data, t, now)  # for the pages: they never read the ledger themselves
    except Exception:  # noqa: BLE001 - a count the pages show must never change the allowance; they show the older one
        pass
    over = limit is not None and t["today"] >= limit
    unknown = bool(t.get("unknown_today")) and not unknown_accepted(data, now)
    if over or unknown:
        cause = "spent" if over else "unknown"
        out = {"allow": 0, "reason": "cap", "cause": cause, "limit": limit, "spent": t}
        try:
            open_card(data, now, limit, cause, t, p.get("name") or "?", store)
        except (CardError, OSError) as e:
            out["card_error"] = str(e) or type(e).__name__
        return out
    out = {"allow": None, "reason": "ok", "limit": limit, "spent": t}
    try:
        close_card(data, now, store)
    except (CardError, OSError) as e:
        out["card_error"] = str(e) or type(e).__name__
    return out


def refusal(a: dict) -> str:
    """The one line a refused start or resume says: the reason, today's spend, the limit and the fix. During the hold
    after a second rejected key (`error` is `keys.AUTH_AGAIN`) the fix is `keys.KEY_HOLD_FIX`, with NAME replaced by
    the profile's name when `a` has one; any other key error gets `keys.KEY_FIX`."""
    if a.get("reason") == "key":
        err = a.get("error") or "the key command failed"
        fix = keys.KEY_FIX
        if err == keys.AUTH_AGAIN:
            name = a.get("profile")
            fix = keys.KEY_HOLD_FIX.replace("NAME", name) if isinstance(name, str) and name else keys.KEY_HOLD_FIX
        return f"the gateway key does not work: {err}. {fix}"
    if a.get("reason") == "paused":
        pz = a.get("paused") or {}
        why = f"work is paused at the daily cap since {pz['at']}" if pz.get("reason") == "cap" and pz.get("at") \
            else f"work is paused ({pz.get('error') or pz.get('reason') or 'paused.json'})"
        return f"no worker starts or resumes: {why}; run `ap2 cap resume` to lift the pause"
    t = a.get("spent") or {}
    if a.get("cause") == "unknown":
        return (f"the daily cap holds: some of today's gateway spend cannot be priced ({_usd(t.get('today'))} priced, "
                f"today's limit {_usd(a.get('limit'))}), so no worker starts or resumes; answer the cap card with "
                "`ap2 cap answer accept_unknown`, `raise50`, `raise100` or `pause`, or add the model's price to "
                f"prices.json in the data folder")
    return (f"the daily cap holds: today's gateway spend is {_usd(t.get('today'))} of today's limit "
            f"{_usd(a.get('limit'))}, so no worker starts or resumes; answer the cap card with `ap2 cap answer "
            "raise50`, `raise100` or `pause`")


# -- answers ------------------------------------------------------------------------------------------------------------

def answer(data, option: str, now, workers=None, store: Optional[Store] = None) -> dict:
    """Answer today's card: `raise50` / `raise100` add to today's raise (a second raise adds to the first),
    `accept_unknown` counts today's unknown cost as $0 for the cap until midnight, `pause` writes `paused.json`
    first and then stops and parks every running worker through `workers.cap_stop()` (no `workers`, as in shadow
    mode: nothing is stopped). The card's ask is closed and the state says `card: answered`. Raises CapError for
    any other option."""
    if option not in ANSWERS:
        raise CapError(f"unknown answer {option!r}: use {', '.join(ANSWERS)}")
    s, stale = _load(data, now)
    if option == PAUSE:
        pause(data, now)  # the flag first: nothing starts while the workers are stopped
    if option in OPTIONS:
        s["raise_pct"] += OPTIONS[option]
    if option == ACCEPT_UNKNOWN:
        s["accept_unknown"] = True
    if s["card"] == "open" or stale:
        store = store or _store(data, now)
        _expire(store, stale)
        if s["card"] == "open":
            _close_ask(store, s.get("card_id"), f"daily cap answered: {option}")
    s["card"] = "answered"
    _save(data, s)
    out = {"answered": option, "day": s["day"], "raise_pct": s["raise_pct"],
           "accept_unknown": s.get("accept_unknown") is True, "card": s["card"], "card_id": s.get("card_id"),
           "paused": paused(data) is not None}
    if option == PAUSE:
        if workers is None:
            out["note"] = ("paused.json is written, so no worker starts or resumes; no worker was stopped (shadow "
                           "mode): running workers keep going")
        else:
            out.update(workers.cap_stop())
    return out
