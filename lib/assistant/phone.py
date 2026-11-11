"""The phone page's data (design 9.2): the card store turned into one JSON document, read only.

`build(data, cfg=None, now=None, plugin_dir=None)` returns `{generated_at, mode, primary_language,
secondary_language, languages, settings, lanes, projects, heartbeat, cards, full}`; `ap2 phone build` prints it.

- The window: a card is on the phone when its lane is in `phone_lanes` and it is open (not `done`), or it is `done`
  and its `updated_at` is at most `phone_history_days` days before `now` (0 shows no History; a `done` card with no
  readable `updated_at` stays off).
- `cards` are the local page's rows (`server.row`) in its order (`server.order`); `full` maps each one's id to the
  local page's full card (`server.full_card`), less what stays on the Mac: the card's `sessions` and
  `source.raw`, and every child or link that is not on the phone, which becomes `{id, off_phone: true}`.
- `phone_hide_quotes`: a card made from a source record shows "Ana wrote a message in #team" (engine's
  HIDDEN_PHRASES) instead of the record's words, in its row (`title: null`, `title_hidden` per language) and in
  its full card (title and `plain.what` per language, `source.record` and the source block).
- Local paths: in three texts that can name a file on this Mac, `full[id].template_error`, `heartbeat.error` and
  `full[id].card.ask.text`, the data folder (as given and resolved) becomes `<data>` and then the home folder (as
  given and resolved) becomes `~`; the rest of each text stays. A prefix counts only as a whole path name: no ASCII
  name character (a letter, digit, `_ - + @ ~ $ % # =`, or a dot and one) may come next, and no ASCII letter, digit,
  `_`, `.` or `-` before (not `/Users/xy` or `/Users/x.old` for home `/Users/x`). Another script next to a path
  (Chinese) does not stop it, so a folder `/Users/xé` reads `~é`: a wrong name, never a leak. An ask text that is not
  a string (an object per language, a list) has each of its strings scrubbed. A home of `/` is left alone. Other
  paths (`/private/var/...`) stay.

Building writes nothing (the store is read without its lock file, as the local page's GETs do), starts no process
and opens no connection. Publishing the data is a later dispatcher step, live mode only.
"""
from __future__ import annotations

import copy
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from assistant import cards, config, engine, server, templates
from assistant.config import ConfigError
from assistant.sources import Loader, SourceError

MAC_ONLY = ("sessions",)  # card fields that stay on the Mac (and `source.raw`, a path to a file on it)


class _Scrub:
    """Replaces the data folder with `<data>` and then the home folder with `~` in a text (see the module's words)."""

    def __init__(self, data: Path):
        self.steps = [(self._pattern(_forms(data)), "<data>")]
        try:
            home = Path.home()
        except (RuntimeError, KeyError, OSError):  # no HOME and no password entry: nothing to replace
            home = None
        if home is not None:
            self.steps.append((self._pattern(_forms(home)), "~"))

    @staticmethod
    def _pattern(forms: list) -> Optional["re.Pattern"]:
        if not forms:
            return None
        # Longest first, so `/private/var/x` wins over `/var/x`; a whole path name only, not a longer name's start:
        # no ASCII name character (or a dot and one) may come next. ASCII only, so Chinese next to a path still ends it.
        alts = "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True))
        name = r"[\w+@~$%#=-]"
        return re.compile(rf"(?<![\w.-])(?:{alts})(?!{name}|\.{name})", re.ASCII)

    def __call__(self, text):
        if isinstance(text, dict):  # an ask text that is not a string: each of its strings is scrubbed
            return {k: self(v) for k, v in text.items()}
        if isinstance(text, list):
            return [self(v) for v in text]
        if not isinstance(text, str):
            return text
        for pattern, by in self.steps:
            if pattern is not None:
                text = pattern.sub(by, text)
        return text


def _forms(path: Path) -> list:
    """The path as given and resolved, each without a trailing slash; the root and empty paths are left out."""
    out = []
    for p in (str(path), _resolved(path)):
        p = p.rstrip("/") if p else p
        if p and p not in out and os.path.isabs(p):
            out.append(p)
    return out


def _resolved(path: Path) -> str:
    try:
        return str(path.resolve())
    except (OSError, RuntimeError):
        return ""


def on_phone(card: dict, cfg: config.Config, now: datetime) -> bool:
    """True when the card is inside the phone's window (see the module's words)."""
    lane = card.get("lane")
    if not (isinstance(lane, str) and lane in cfg.phone_lanes):
        return False
    if lane != "done":
        return True
    if cfg.phone_history_days == 0:
        return False
    try:
        at = cards.parse_stamp(card.get("updated_at"))
    except (TypeError, ValueError):
        return False
    return at >= now - timedelta(days=cfg.phone_history_days)


def _record(card: dict) -> Optional[dict]:
    """The source record the card was made from, or None."""
    src = card.get("source")
    return src["record"] if isinstance(src, dict) and isinstance(src.get("record"), dict) else None


class _Hider:
    """The hiding phrases of the cards made from a record, one per language, with each source's `brings` read once.
    A source that cannot be loaded (removed, or a bad file) makes the phrase say `something`."""

    def __init__(self, data: Path, plugin_dir: Optional[Path], langs: tuple):
        missing = [lang for lang in langs if engine.hidden_phrases(lang) is None]
        if missing:
            raise ConfigError(f"phone_hide_quotes: no phrase to show instead of what people wrote in "
                              f"{', '.join(missing)} yet (built in: {', '.join(engine.HIDDEN_PHRASES)})")
        self.data, self.plugin_dir, self.langs = data, plugin_dir, langs
        self.loader = None
        self.brings = {}

    def _brings(self, name) -> Optional[str]:
        if not isinstance(name, str):
            return None
        if name not in self.brings:
            try:
                if self.loader is None:
                    self.loader = Loader(self.data, self.plugin_dir)
                self.brings[name] = self.loader.load(name).brings
            except (SourceError, ConfigError):
                self.brings[name] = None
        return self.brings[name]

    def texts(self, card: dict) -> dict:
        record = _record(card)
        brings = self._brings(card["source"].get("name"))
        return {lang: engine.hidden_text(lang, record, brings) for lang in self.langs}


def build(data, cfg: Optional[config.Config] = None, now: Optional[datetime] = None,
          plugin_dir: Optional[Path] = None) -> dict:
    """The phone page's data for the data folder `data`. `cfg` defaults to its config.yaml, `now` to the clock,
    `plugin_dir` to the plugin's `sources/` (read only to name a hidden record's kind). Raises ConfigError (a bad
    config, or hiding asked for in a language with no phrases) and CardError (a damaged card file)."""
    data = Path(data)
    cfg = cfg if cfg is not None else config.load(data)
    now = now if now is not None else cards.now_utc()
    langs = cards.languages(cfg)
    hider = _Hider(data, plugin_dir, langs) if cfg.phone_hide_quotes else None
    store = server._ReadStore(data, cfg=cfg, clock=lambda: now)
    found = store.list()
    shown = server.order([c for c in found if on_phone(c, cfg, now)])
    by_id = {c["id"]: c for c in shown}
    hidden = {c["id"]: hider.texts(c) for c in shown if hider is not None and _record(c) is not None}

    def phone_row(card: dict) -> dict:
        out = server.row(card, langs)
        if card["id"] in hidden:
            out["title"], out["title_hidden"] = None, dict(hidden[card["id"]])
        return out

    def neighbour(r: dict) -> dict:
        """A child's or a link's row: the phone's own row, `{id, off_phone}` when it is not on the phone, or the
        local page's `{id, missing}` as it is."""
        if r.get("missing"):
            return r
        return phone_row(by_id[r["id"]]) if r["id"] in by_id else {"id": r["id"], "off_phone": True}

    catalog = _once(lambda: templates.catalog(data))
    scrub = _Scrub(data)
    full = {}
    for c in shown:
        out = copy.deepcopy(server.full_card(store, data, c, langs, found=found, catalog=catalog))
        card = out["card"]
        for key in MAC_ONLY:
            card.pop(key, None)
        if isinstance(card.get("source"), dict):
            card["source"].pop("raw", None)
        if c["id"] in hidden:
            texts = hidden[c["id"]]
            card["title"] = dict(texts)
            card["plain"]["what"] = dict(texts)
            card.pop("key", None)  # a source's key may be made from the record's text; the phone does not use it
            record = {k: v for k, v in card["source"]["record"].items() if k != "text"}
            card["source"]["record"] = record
            out["source_block"]["text"] = None
            out["source_block"]["record"] = copy.deepcopy(record)
        if "template_error" in out:
            out["template_error"] = scrub(out["template_error"])
        if isinstance(card.get("ask"), dict) and "text" in card["ask"]:
            card["ask"]["text"] = scrub(card["ask"]["text"])
        out["children"] = [neighbour(r) for r in out["children"]]
        out["links"] = [neighbour(r) for r in out["links"]]
        full[c["id"]] = out

    lanes = {lane: 0 for lane in cards.LANES}
    children = {}
    for c in shown:
        if c.get("lane") in lanes:
            lanes[c["lane"]] += 1
        if isinstance(c.get("parent"), str):
            children.setdefault(c["parent"], []).append(c)
    projects = []
    for c in found:  # the store's order, as the local page lists them
        if c.get("kind") != "project" or c["id"] not in by_id:
            continue
        kids = children.get(c["id"], [])
        p = {"id": c["id"], "title": server.titles(c, langs),
             "done": sum(1 for k in kids if k.get("lane") == "done"), "total": len(kids)}
        if c["id"] in hidden:
            p["title"], p["title_hidden"] = None, dict(hidden[c["id"]])
        projects.append(p)
    return {"generated_at": cards.stamp(now), "mode": cfg.mode, "primary_language": cfg.primary_language,
            "secondary_language": cfg.secondary_language, "languages": list(langs),
            "settings": {"history_days": cfg.phone_history_days, "lanes": list(cfg.phone_lanes),
                         "hide_quotes": cfg.phone_hide_quotes},
            "lanes": lanes, "projects": projects, "heartbeat": _scrubbed_heartbeat(data, now, scrub),
            "cards": [phone_row(c) for c in shown], "full": full}


def _scrubbed_heartbeat(data: Path, now: datetime, scrub: _Scrub) -> dict:
    out = server.heartbeat(data, now)
    if "error" in out:
        out["error"] = scrub(out["error"])
    return out


def _once(fn):
    """`fn()` run at most once: later calls return its result, or raise its error, again."""
    memo = []

    def call():
        if not memo:
            try:
                memo.append((True, fn()))
            except ConfigError as e:
                memo.append((False, str(e)))
        ok, value = memo[0]
        if not ok:
            raise ConfigError(value)
        return value
    return call
