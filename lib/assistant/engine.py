"""The source engine (design 5.1): read -> fill the form -> check -> link and key -> sort -> card.

- `read(name)` runs a `via: command` source's `read.run` itself (argv split with `shlex`, no shell, a timeout,
  the data folder as the working folder) and saves its raw stdout first, under
  `<data>/sources/.raw/<source>/<UTC stamp>-<6 hex>.json`.
- `read_saved(name, raw_path, records=None)` takes output saved by someone else (a scan session for `mcp`,
  `connector` and `skill`, slice 4). For `fill: ai` the scan session's records come in `records`.
- A record missing a required field (`status` too for `brings: work`), or a field a link or key pattern names, is
  dropped and listed in the result's `dropped`, which is also saved next to the raw file as
  `<same name without its extension>.report.json`.
- The same key updates the card that has it; `none` makes nothing. Plain words are made by code (no AI), in every
  configured language.
- A source's `writes` are never run here: `apply_writes` calls `config.require_live` first, and runs nothing.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import sqlite3
import subprocess
from datetime import timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from assistant import cards, config, form, sources
from assistant.cards import CardError
from assistant.sources import SourceError
from assistant.store import Store, write_json

RAW = Path(sources.FOLDER) / ".raw"
STAMP = "%Y%m%dT%H%M%SZ"
SNIPPET = 120
TITLE = 80
LANE = {"messages": "needs", "work": "planned"}
PRI = "P2"
# fill: ai fields that must appear word for word in the raw tool output (design 5.3).
WORD_FOR_WORD = ("id", "thread_id", "at")

# The code-made plain words (design 3.7), one entry per language. A language is matched exactly, then by its first
# part (zh-Hans -> zh). The quoted text stays in the words its author wrote.
PHRASES = {
    "en": {
        "what": "New from {source}{by}{in}: {snippet}", "by": " by {author}", "in": " in {place}",
        "at": "It was written at {at}.", "status": " Its status there is \"{status}\".",
        "tail": " Nothing has been done with it yet.",
        "do_messages": "Read it{link} and decide what it needs.", "link": " at the link",
        "do_work": "Nothing yet; it waits in Planned until it is started.",
    },
    "zh": {
        "what": "来自 {source} 的新内容{by}{in}：{snippet}", "by": "，作者 {author}", "in": "，在 {place}",
        "at": "写于 {at}。", "status": "它在原系统里的状态是“{status}”。",
        "tail": "还没有人处理它。",
        "do_messages": "看一下{link}，再决定要怎么处理。", "link": "（点链接看全文）",
        "do_work": "现在不用做什么；它在“计划中”等着开始。",
    },
}


def phrases(lang: str) -> Optional[dict]:
    return PHRASES.get(lang) or PHRASES.get(lang.split("-")[0])


def snippet(text: str, limit: int = SNIPPET) -> str:
    """The first non-blank line of `text`, white space collapsed, cut to `limit` characters with an ellipsis."""
    line = next((ln for ln in text.splitlines() if ln.strip()), text)
    line = re.sub(r"\s+", " ", line).strip()
    return line if len(line) <= limit else line[:limit - 1].rstrip() + "…"


def plain_words(src: sources.Source, record: dict, link: Optional[str], langs: tuple) -> dict:
    """The three plain-words lines in each language, made from the record by fixed phrases."""
    out = {line: {} for line in cards.PLAIN_LINES}
    for lang in langs:
        p = phrases(lang)
        what = p["what"].format(
            source=src.name, snippet=snippet(record["text"]),
            by=p["by"].format(author=record["author"]) if record.get("author") else "",
            **{"in": p["in"].format(place=record["place"]) if record.get("place") else ""})
        where = p["at"].format(at=record["at"])
        if src.brings == "work" and record.get("status"):
            where += p["status"].format(status=record["status"])
        where += p["tail"]
        if src.brings == "work":
            do = p["do_work"]
        else:
            do = p["do_messages"].format(link=p["link"] if link else "")
        out["what"][lang], out["where"][lang], out["do"][lang] = what, where, do
    return out


class Engine:
    def __init__(self, data, cfg: Optional[config.Config] = None, plugin_dir: Optional[Path] = None,
                 clock: Callable = cards.now_utc):
        """`data` is the data folder; `cfg` defaults to its config.yaml; `plugin_dir` to the plugin's `sources/`;
        `clock()` returns an aware datetime (tests pass a fixed one)."""
        self.data = Path(data)
        self.cfg = cfg if cfg is not None else config.load(self.data)
        self.loader = sources.Loader(self.data, plugin_dir)
        self.clock = clock
        self.languages = cards.languages(self.cfg)

    def source(self, name: str) -> sources.Source:
        return self.loader.load(name)

    # -- reading ------------------------------------------------------------------------------------------------

    def read(self, name: str, dry_run: bool = False) -> dict:
        """Run a `via: command` source and make its cards; with `dry_run`, save nothing and say what each item
        would become."""
        src = self.source(name)
        if src.via != "command":
            raise SourceError(f"{src.file}: read.via is {src.via}; only a scan session can make that read. Give "
                              f"its saved output instead (ap2 source read {name} --raw FILE)")
        if not self.data.is_dir():
            raise SourceError(f"data folder {self.data} does not exist (run ap2 config to set it up)")
        self._check_languages(src)
        _check_fill(src, None)
        done = self._run(src, dry_run)
        raw = None if dry_run else self._save_raw(src, done.stdout)
        if done.returncode != 0:
            last = done.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["no output on stderr"]
            saved = f" (output saved as {raw})" if raw else ""
            raise SourceError(f"{src.name}: read.run exited with {done.returncode}: {last[0]}{saved}")
        return self._process(src, done.stdout, raw, dry_run, None)

    def read_saved(self, name: str, raw_path, records: Optional[list] = None, dry_run: bool = False) -> dict:
        """Make the cards of a read someone else made (a scan session): `raw_path` holds the tool's raw output.
        `fill: paths` fills the records from it; `fill: ai` takes the scan session's `records`. Unless `dry_run`,
        the raw output is copied under `<data>/sources/.raw/<source>/` first (when it is not there already)."""
        src = self.source(name)
        self._check_languages(src)
        _check_fill(src, records)
        path = Path(raw_path)
        try:
            raw_bytes = path.read_bytes()
        except OSError as e:
            raise SourceError(f"{src.name}: cannot read the saved output {path}: {e}") from e
        raw = None
        if not dry_run:
            here = (self.data / RAW / src.name).resolve()
            raw = (path.resolve().relative_to(self.data.resolve()).as_posix() if path.resolve().parent == here
                   else self._save_raw(src, raw_bytes))
        return self._process(src, raw_bytes, raw, dry_run, records)

    def _run(self, src: sources.Source, dry_run: bool = False) -> subprocess.CompletedProcess:
        run = src.data["read"]["run"]
        try:
            argv = shlex.split(run)
        except ValueError as e:
            raise SourceError(f"{src.file}: read.run cannot be split into words: {e}") from e
        if not argv:
            raise SourceError(f"{src.file}: read.run is empty")
        timeout = src.data["read"].get("timeout", sources.DEFAULT_TIMEOUT)
        try:
            return subprocess.run(argv, cwd=str(self.data), stdin=subprocess.DEVNULL, capture_output=True,
                                  timeout=timeout, shell=False)
        except subprocess.TimeoutExpired as e:
            saved = "" if dry_run else f" (partial output saved as {self._save_raw(src, e.stdout or b'')})"
            raise SourceError(f"{src.name}: read.run took longer than {timeout} seconds; stopped{saved}") from None
        except OSError as e:
            raise SourceError(f"{src.name}: cannot run {argv[0]!r}: {e.strerror or e}") from e

    def _save_raw(self, src: sources.Source, raw: bytes) -> str:
        """Save one read's raw output; return its path relative to the data folder."""
        folder = self.data / RAW / src.name
        at = self.clock()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            while True:
                path = folder / f"{at.astimezone(timezone.utc).strftime(STAMP)}-{secrets.token_hex(3)}.json"
                try:
                    with open(path, "xb") as f:
                        f.write(raw)
                        f.flush()
                        os.fsync(f.fileno())
                    break
                except FileExistsError:
                    continue
        except OSError as e:
            raise SourceError(f"{src.name}: cannot save the raw output in {folder}: {e}") from e
        return path.relative_to(self.data).as_posix()

    def _process(self, src, raw_bytes: bytes, raw: Optional[str], dry_run: bool, records) -> dict:
        _check_fill(src, records)
        if src.fill == "paths":
            try:
                reply = json.loads(raw_bytes.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError) as e:
                where = f" (saved as {raw})" if raw else ""
                why = "nested too deeply" if isinstance(e, RecursionError) else e
                raise SourceError(f"{src.name}: the output is not JSON{where}: {why}") from e
            entries, dropped = self.fill(src, reply, raw=raw)
        else:
            entries, dropped = self.check_records(src, records, raw_bytes.decode("utf-8", errors="replace"))
        result = self.build(src, entries, dropped, raw, dry_run)
        if raw is not None:
            p = PurePosixPath(raw)
            report = self.data / p.with_name(p.stem + ".report.json")
            try:
                write_json(report, result)
            except OSError as e:
                raise SourceError(f"{src.name}: cannot save the read report {report}: {e}") from e
        return result

    # -- fill and check -----------------------------------------------------------------------------------------

    @staticmethod
    def fill(src: sources.Source, reply, raw: Optional[str] = None) -> tuple:
        """`fill: paths`: one record per item of the reply. Returns (entries, dropped). A reply without the list
        of the item path is refused (SourceError, naming `raw`, where it was saved), not read as no items."""
        try:
            found = form.items(src.prefix, reply)
        except ValueError as e:
            where = f" (saved as {raw})" if raw else ""
            raise SourceError(f"{src.name}: {e}{where}") from None
        entries, dropped = [], []
        for n, item in enumerate(found):
            try:
                record = form.fill_item(src.paths, src.prefix, item, required(src))
                link = None
                if src.link[0] == "path":
                    value = form.evaluate(src.link[1][len(src.prefix):], item)
                    if value is form.MISSING or value is None:
                        raise ValueError("link missing")
                    link = form.as_text(value, "link")
            except ValueError as e:
                dropped.append({"item": n, "reason": str(e)})
                continue
            entries.append({"item": n, "record": record, "link": link})
        return entries, dropped

    @staticmethod
    def check_records(src: sources.Source, records, raw_text: str) -> tuple:
        """`fill: ai`: check the records a scan session filled (design 5.3). Each record's `id`, and its `thread_id`
        and `at` when present, must appear word for word in `raw_text`, the tool's raw output, so a model cannot
        invent them. Each record carries its `category`, one of `sort`'s."""
        if not isinstance(records, list):
            raise SourceError(f"{src.name}: records must be a list, not {type(records).__name__}")
        entries, dropped = [], []
        for n, rec in enumerate(records):
            try:
                record = form.check_record(rec, required(src))
                for field in WORD_FOR_WORD:
                    if field in record and record[field] not in raw_text:
                        raise ValueError(f"form.{field} {record[field]!r} is not in the raw output")
                category = rec.get("category")
                if not isinstance(category, str) or category not in src.sort:
                    raise ValueError(f"category {category!r} is not one of sort's ({', '.join(src.sort)})")
            except ValueError as e:
                dropped.append({"item": n, "reason": str(e)})
                continue
            entries.append({"item": n, "record": record, "link": None, "category": category})
        return entries, dropped

    # -- link, key, sort, card ----------------------------------------------------------------------------------

    def link_and_key(self, src: sources.Source, record: dict, link: Optional[str] = None) -> tuple:
        """The record's (link, key). Raises ValueError naming what is missing or wrong."""
        values = form.derived(record, src.name)
        try:
            key = form.fill_pattern(src.key, values)
        except ValueError as e:
            raise ValueError(f"key: {e}") from None
        if src.link[0] == "none":
            return None, key
        if src.link[0] == "pattern":
            try:
                link = form.fill_pattern(src.link[1], values, url=True)
            except ValueError as e:
                raise ValueError(f"link: {e}") from None
        if not re.match(r"https?://[^\s/]", link or ""):
            raise ValueError(f"link {link!r} is not an http(s) link")
        return link, key

    def _check_languages(self, src) -> None:
        missing = [lang for lang in self.languages if phrases(lang) is None]
        if missing:
            raise SourceError(f"{src.name}: no code-made plain words for {', '.join(missing)} yet (built in: "
                              f"{', '.join(PHRASES)}); the store would refuse every card")

    def build(self, src: sources.Source, entries: list, dropped: Optional[list] = None, raw: Optional[str] = None,
              dry_run: bool = False) -> dict:
        """Link, key and sort each entry, then add or update its card (nothing when `dry_run`)."""
        self._check_languages(src)
        dropped = list(dropped or [])
        store = Store(self.data, cfg=self.cfg, clock=self.clock)
        existing = self._cards_by_key(store, dry_run)
        items = []
        for e in entries:
            record = e["record"]
            try:
                link, key = self.link_and_key(src, record, e.get("link"))
            except ValueError as err:
                dropped.append({"item": e["item"], "reason": str(err)})
                continue
            category = e.get("category")
            out = {"item": e["item"], "record": record, "key": key, "link": link}
            if category is None and len(src.sort) == 1:
                category = next(iter(src.sort))
            if category is None:
                out.update(category=None, becomes=None, action="none", card=None,
                           reason=f"sort has {len(src.sort)} categories; fill: paths cannot pick one")
                items.append(out)
                continue
            becomes = src.sort[category]
            out.update(category=category, becomes=becomes)
            old = existing.get(key)
            if becomes == "none" or (becomes == "update_card" and old is None):
                out.update(action="none", card=None,
                           reason="sort says none" if becomes == "none" else "no card has this key")
                items.append(out)
                continue
            new = self._card(src, becomes, record, link, key, raw)
            if old is None:
                out["action"] = "add"
                if dry_run:
                    out["card"] = None
                    out["would_add"] = new
                    existing[key] = new
                else:
                    try:
                        added = store.add(new, by=f"source:{src.name}")
                    except CardError as err:
                        if isinstance(err.__cause__, (OSError, sqlite3.Error)):
                            raise  # the store itself failed, not this one card
                        dropped.append({"item": e["item"], "reason": f"new card with key {key}: {err}"})
                        continue
                    out["card"] = added["id"]
                    existing[key] = added
            else:
                changes = {k: new[k] for k in ("plain", "title", "source", "status") if k in new}
                same = all(_same(k, old.get(k), v) for k, v in changes.items())
                out["action"] = "unchanged" if same else "update"
                out["card"] = old.get("id")
                if not same and not dry_run:
                    try:
                        existing[key] = store.update(old["id"], changes, by=f"source:{src.name}",
                                                     did=f"read {src.name}")
                    except CardError as err:
                        if isinstance(err.__cause__, (OSError, sqlite3.Error)):
                            raise  # the store itself failed, not this one card
                        dropped.append({"item": e["item"], "reason": f"card {old['id']}: {err}"})
                        continue
                elif not same:
                    out["would_change"] = sorted(k for k, v in changes.items() if not _same(k, old.get(k), v))
                    existing[key] = {**old, **changes}
            items.append(out)
        return {"source": src.name, "dry_run": dry_run, "raw": raw, "read_at": cards.stamp(self.clock()),
                "items": items, "dropped": dropped}

    @staticmethod
    def _cards_by_key(store: Store, dry_run: bool) -> dict:
        """The cards that have a `key`, the oldest first on a tie. A dry run with no card file yet does not touch
        the store at all (its lock file included), so it saves nothing."""
        if dry_run and not any((store.data / "items").glob("*/*.json")):
            return {}
        out = {}
        for c in store.list():
            if isinstance(c.get("key"), str):
                out.setdefault(c["key"], c)
        return out

    def _card(self, src, kind: str, record: dict, link, key: str, raw) -> dict:
        card = {"kind": kind, "lane": LANE[src.brings], "feed": src.name, "links": [], "pri": PRI,
                "origin": "source", "plain": plain_words(src, record, link, self.languages),
                "title": snippet(record["text"], TITLE), "key": key,
                "source": {"name": src.name, "record": record, "link": link, "raw": raw}}
        if src.brings == "work" and "status" in record:
            card["status"] = str(src.data.get("status_map", {}).get(record["status"], record["status"]))
        return card

    # -- writes (shadow mode) -----------------------------------------------------------------------------------

    def apply_writes(self, name: str, event: str, card_id: Optional[str] = None):
        """What a `brings: work` source's `writes.<event>` would change. Shadow mode refuses before anything; in
        live mode it still runs nothing (a later slice does), and says so. Returns None when there is no write."""
        src = self.source(name)
        if event not in sources.WRITE_EVENTS:
            raise SourceError(f"{src.name}: unknown write event {event!r} (events: {', '.join(sources.WRITE_EVENTS)})")
        spec = (src.data.get("writes") or {}).get(event)
        if spec is None:
            return None
        config.require_live(self.cfg, f"apply {src.name}'s writes.{event}" + (f" for {card_id}" if card_id else ""))
        raise SourceError(f"{src.name}: writes.{event} is not run by this version of ap2")


def _check_fill(src: sources.Source, records) -> None:
    """Refuse records that do not suit the source's `fill`, before anything is run or saved."""
    if src.fill == "paths" and records is not None:
        raise SourceError(f"{src.name}: fill: paths fills its own records; give only the raw output")
    if src.fill == "ai" and records is None:
        raise SourceError(f"{src.name}: fill: ai records are filled by a scan session; give them with the raw output")


def required(src: sources.Source) -> tuple:
    """The fields a record of `src` must have (design 5.2): the form's, and `status` for `brings: work`."""
    return form.REQUIRED + (("status",) if src.brings == "work" else ())


def _same(key: str, old, new) -> bool:
    """Whether a card's field already holds the new value. The source block's `raw` does not count: a new read of
    the same item is not a change."""
    if key == "source" and isinstance(old, dict) and isinstance(new, dict):
        return {k: v for k, v in old.items() if k != "raw"} == {k: v for k, v in new.items() if k != "raw"}
    return old == new
