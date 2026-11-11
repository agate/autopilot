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

import contextlib
import errno
import json
import os
import posixpath
import re
import secrets
import selectors
import shlex
import signal
import sqlite3
import stat
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
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
# Seconds a read waits for the rest of its output after its timeout kill.
DRAIN = 5
# The most stdout a read keeps; past it the command is stopped. Only the end of its stderr is kept.
MAX_OUTPUT = 64 * 1024 * 1024
ERR_TAIL = 64 * 1024
# The most items one read handles; a reply (or a scan session's records) with more fails the read.
MAX_ITEMS = 10_000
# How many times a stop kills the command's group before it gives up on emptying it.
STOP_TRIES = 100
# The catchable signals whose default action ends the process and that a terminal, shell, supervisor or user may
# send. While a read waits, those still at their default stop the command's group too (SIGINT does it through
# KeyboardInterrupt). A missing one is skipped.
STOP_SIGNALS = tuple(s for s in (getattr(signal, n, None) for n in (
    "SIGHUP", "SIGQUIT", "SIGTERM", "SIGUSR1", "SIGUSR2", "SIGALRM", "SIGVTALRM", "SIGPROF", "SIGXCPU"))
    if s is not None)
# fill: ai fields that must appear word for word in the raw tool output (design 5.3); place_id builds links and keys.
WORD_FOR_WORD = ("id", "thread_id", "at", "place_id")
# Around a word-for-word match: a letter or digit, or one of these followed by a letter or digit, means the value
# is only part of a longer token (1730000000 inside 1730000000.123456, C0123 inside C0123ABCD).
JOINERS = "._:-"

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


# What the phone page shows instead of a record's words when `phone_hide_quotes` is on (design 9.2): who and where
# only, never the text. `{thing}` is the kind of record, by the source's `brings` (`other` when it is not known).
# Matched like PHRASES.
HIDDEN_PHRASES = {
    "en": {"both": "{author} wrote {thing} in {place}", "author": "{author} wrote {thing}",
           "place": "Someone wrote {thing} in {place}", "none": "Someone wrote {thing}",
           "messages": "a message", "work": "an item", "other": "something"},
    "zh": {"both": "{author} 在 {place} 写了{thing}", "author": "{author} 写了{thing}",
           "place": "有人在 {place} 写了{thing}", "none": "有人写了{thing}",
           "messages": "一条消息", "work": "一个条目", "other": "一些内容"},
}


def hidden_phrases(lang: str) -> Optional[dict]:
    return HIDDEN_PHRASES.get(lang) or HIDDEN_PHRASES.get(lang.split("-")[0])


def hidden_text(lang: str, record: dict, brings: Optional[str]) -> str:
    """The phrase that stands for `record` in `lang`: "Ana wrote a message in #team", or the shorter form without
    an author or a place. Never holds the record's text."""
    p = hidden_phrases(lang)
    author, place = (record.get(k) if isinstance(record.get(k), str) and record[k].strip() else None
                     for k in ("author", "place"))
    form = "both" if author and place else "author" if author else "place" if place else "none"
    return p[form].format(author=author, place=place, thing=p[brings if brings in ("messages", "work") else "other"])


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
        if done.returncode != 0:
            last = done.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["no output on stderr"]
            saved, failed = ("", None) if dry_run else self._keep_raw(src, done.stdout, "output")
            raise SourceError(f"{src.name}: read.run exited with {done.returncode}: {last[0]}{saved}") from failed
        raw = None if dry_run else self._save_raw(src, done.stdout)  # nothing else failed: a save error is the error
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
            raw_bytes = sources.read_capped(
                path, MAX_OUTPUT, f"{src.name}: the saved output {path} is larger than {MAX_OUTPUT} bytes")
        except OSError as e:
            raise SourceError(f"{src.name}: cannot read the saved output {path}: {e}") from e
        raw = None
        if not dry_run:
            here = (self.data / RAW / src.name).resolve()
            real = path.resolve()
            # Named from the folder's own names, not the resolved ones: `.raw` may be a link to another folder.
            raw = (RAW / src.name / real.name).as_posix() if real.parent == here else self._save_raw(src, raw_bytes)
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
        guard = _Guard()
        previous, proc, failed = {}, None, None
        out, err = bytearray(), bytearray()
        try:
            try:
                # A signal to ap2's group does not reach the command's group. Ctrl-C and a stop signal at its
                # default go to `guard` from before the command starts, so the branch below stops the command; one
                # that is ignored (nohup) or already handled is left alone. The command does not inherit `guard`:
                # a handler set from Python is back at its default after exec.
                if threading.current_thread() is threading.main_thread():
                    previous[signal.SIGINT] = signal.getsignal(signal.SIGINT)  # held while the group is stopped
                    if previous[signal.SIGINT] is signal.default_int_handler:
                        signal.signal(signal.SIGINT, guard)
                    for signum in STOP_SIGNALS:
                        if signal.getsignal(signum) is signal.SIG_DFL:
                            previous[signum] = signal.signal(signum, guard)
                try:
                    # Its own process group, so a stop reaches the children it started too, not only the command.
                    proc = subprocess.Popen(argv, cwd=str(self.data), stdin=subprocess.DEVNULL,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                                            start_new_session=True)
                except (OSError, ValueError) as e:  # ValueError: a NUL in an argument
                    failed = SourceError(f"{src.name}: cannot run {argv[0]!r}: {getattr(e, 'strerror', None) or e}")
                    failed.__cause__ = e
                guard.start()  # a signal that came while the command started acts now
                if failed is not None:
                    raise failed
                ended = _pump(proc, out, err, timeout)
            except BaseException:
                # Ctrl-C does not reach the command's own group, so an interrupted read stops it here, and a
                # second signal cannot cut that short.
                if previous:
                    _hold_signals()
                if proc is not None:
                    _stop(proc)
                    proc.wait()
                raise
            if ended != "done":
                code = proc.poll() if ended == "late" else None
                held = _hold_signals() if previous else {}
                _stop(proc)
                for signum, handler in held.items():
                    signal.signal(signum, handler)
                if ended == "late":
                    _pump(proc, out, err, DRAIN)  # what the command printed before the kill
                # Something outside the group may still hold the output open: keep what came so far.
                proc.stdout.close()
                proc.stderr.close()
                proc.wait()
                del out[MAX_OUTPUT:]
                what = "output" if code is not None else "partial output"
                saved, failed = ("", None) if dry_run else self._keep_raw(src, bytes(out), what)
                if ended == "full":
                    raise SourceError(f"{src.name}: read.run printed more than {MAX_OUTPUT} bytes; "
                                      f"stopped{saved}") from failed
                if code is not None:
                    raise SourceError(f"{src.name}: read.run exited with {code}, but something it started still "
                                      f"held its output open after {timeout} seconds; stopped{saved}") from failed
                raise SourceError(f"{src.name}: read.run took longer than {timeout} seconds; stopped{saved}") \
                    from failed
        finally:
            # Ctrl-C last, so a new one cannot cut the others short; a failed restore never hides the read's error.
            for signum, handler in reversed(list(previous.items())):
                if handler is not None:  # None: set outside Python, so it cannot be put back from here
                    try:
                        signal.signal(signum, handler)
                    except (OSError, ValueError):
                        pass
        proc.stdout.close()
        proc.stderr.close()
        return subprocess.CompletedProcess(argv, proc.returncode, stdout=bytes(out), stderr=bytes(err))

    def _save_raw(self, src: sources.Source, raw: bytes) -> str:
        """Save one read's raw output; return its path relative to the data folder."""
        folder = self.data / RAW / src.name
        at = self.clock()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            while True:
                path = folder / f"{at.astimezone(timezone.utc).strftime(STAMP)}-{secrets.token_hex(3)}.json"
                try:
                    f = open(path, "xb")
                except FileExistsError:
                    continue
                try:
                    with f:
                        f.write(raw)
                        f.flush()
                        os.fsync(f.fileno())
                except OSError:
                    with contextlib.suppress(OSError):  # a full disk would leave an empty or partial file
                        path.unlink()
                    raise
                break
        except OSError as e:
            raise SourceError(f"{src.name}: cannot save the raw output in {folder}: {e}") from e
        return path.relative_to(self.data).as_posix()

    def _keep_raw(self, src: sources.Source, raw: bytes, what: str) -> tuple:
        """Save the raw output of a read that fails anyway. Return (the note for the read's own error, the save
        failure or None): the note names the file `what` was saved as, or says why it was not, so a failed save
        (a full disk) never hides why the read failed."""
        try:
            path = self._save_raw(src, raw)
        except SourceError as e:
            why = str(e).split(": ", 1)[1]  # without the source's name, which the read's error starts with
            return f" (the {what} was not saved: {why})", e
        return f" ({what} saved as {path})", None

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
            entries, dropped = self.check_records(src, records, raw_bytes.decode("utf-8", errors="replace"), raw=raw)
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
        _check_count(src, len(found), raw)
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
    def check_records(src: sources.Source, records, raw_text: str, raw: Optional[str] = None) -> tuple:
        """`fill: ai`: check the records a scan session filled (design 5.3). Each record's `id`, and its `thread_id`,
        `at` and `place_id` when present (the derived `place_id`, so also a `place` that stands in for it, named
        `place` in the reason), must appear word for word in `raw_text`, the tool's raw output, as a whole token
        (`word_for_word`), so a model cannot invent them. A record whose `place` or `place_id` is in
        `read.skip_channels` (a leading `#` ignored) is dropped. Each record carries its `category`, one of
        `sort`'s. Too many records is refused (SourceError, naming `raw`, where the raw output was saved)."""
        if not isinstance(records, list):
            raise SourceError(f"{src.name}: records must be a list, not {type(records).__name__}")
        _check_count(src, len(records), raw)
        entries, dropped = [], []
        for n, rec in enumerate(records):
            try:
                record = form.check_record(rec, required(src))
                skipped = _skipped(src, record)
                if skipped:
                    raise ValueError(skipped)
                values = form.derived(record, src.name)  # a `place_id` made from `place` is checked too
                for field in WORD_FOR_WORD:
                    if field in values and not word_for_word(values[field], raw_text):
                        shown = "place" if field == "place_id" and "place_id" not in record else field
                        raise ValueError(f"form.{shown} {values[field]!r} is not in the raw output")
                category = rec.get("category")
                if not isinstance(category, str) or category not in src.sort:
                    raise ValueError(f"category {category!r} is not one of sort's ({', '.join(src.sort)})")
            except ValueError as e:
                dropped.append({"item": n, "reason": str(e)})
                continue
            entries.append({"item": n, "record": record, "link": None, "category": category})
        return _one_per_key(src, entries), dropped

    # -- link, key, sort, card ----------------------------------------------------------------------------------

    def link_and_key(self, src: sources.Source, record: dict, link: Optional[str] = None) -> tuple:
        """The record's (link, key). Patterns may name the record's fields, the derived ones and the source's
        settings (which never share a name with those). A setting is the user's own text, so it goes into a link as
        written (`site: jira.example.com:8443`); only the record's values are percent-encoded. Raises ValueError
        naming what is missing or wrong."""
        settings = sources.settings(src)
        values = {**settings, **form.derived(record, src.name)}
        try:
            key = form.fill_pattern(src.key, values)
        except ValueError as e:
            raise ValueError(f"key: {e}") from None
        if src.link[0] == "none":
            return None, key
        if src.link[0] == "pattern":
            parts = tuple(("text", settings[s]) if kind == "field" and settings.get(s, "").strip() else (kind, s)
                          for kind, s in src.link[1])
            try:
                link = form.fill_pattern(parts, values, url=True)
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
        written = {}  # the ids of the cards this read added or updated, in item order (a dict keeps one of each)
        try:
            existing = self._cards_by_key(store, dry_run)
        except CardError as err:
            if dry_run:
                raise  # it wrote nothing and saved no raw file: there is nothing more to say
            raise self._store_failed(err, written, raw) from err
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
                        if isinstance(err.__cause__, (OSError, sqlite3.Error)):  # the store, not this one card
                            raise self._store_failed(err, written, raw) from err
                        dropped.append({"item": e["item"], "reason": f"new card with key {key}: {err}"})
                        continue
                    written.setdefault(added["id"])
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
                        if isinstance(err.__cause__, (OSError, sqlite3.Error)):  # the store, not this one card
                            raise self._store_failed(err, written, raw) from err
                        dropped.append({"item": e["item"], "reason": f"card {old['id']}: {err}"})
                        continue
                    written.setdefault(old["id"])
                elif not same:
                    try:
                        cards.validate({**old, **changes}, self.languages)  # as store.update checks it
                    except CardError as err:
                        dropped.append({"item": e["item"], "reason": f"card {old['id']}: {err}"})
                        continue
                    out["would_change"] = sorted(k for k, v in changes.items() if not _same(k, old.get(k), v))
                    existing[key] = {**old, **changes}
            items.append(out)
        return {"source": src.name, "dry_run": dry_run, "raw": raw, "read_at": cards.stamp(self.clock()),
                "items": items, "dropped": dropped}

    @staticmethod
    def _store_failed(err: CardError, written: dict, raw: Optional[str]) -> CardError:
        """The error of a card store that failed mid-read (a file or index error): the store's own message, then
        the cards this read already added or updated and the raw file, since the read report is not written."""
        done = f"already written by this read: {', '.join(written)}" if written else "no card written yet"
        where = f"raw output saved as {raw}" if raw else "no raw output saved"
        return CardError(f"{err} ({done}; {where})")

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


class _Guard:
    """The handler a read sets for Ctrl-C and the stop signals. Until `start()` (the command is running) it only
    notes the first signal; from then on it holds the signals and ends ap2: KeyboardInterrupt for Ctrl-C,
    SystemExit(128 + the signal number) for the others."""

    def __init__(self):
        self.started = False
        self.pending = None

    def __call__(self, signum, frame) -> None:
        if not self.started:
            if self.pending is None:
                self.pending = signum
            return
        self._end(self.pending or signum)

    def start(self) -> None:
        self.started = True
        if self.pending is not None:
            self._end(self.pending)

    @staticmethod
    def _end(signum) -> None:
        _hold_signals()
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + signum)


def _hold_signals() -> dict:
    """While a read's group is being stopped, ignore Ctrl-C and the stop signals `_run` handles, so a second
    signal cannot skip the stop; `_run` puts them back. Main thread only. Returns what each was before."""
    held = {}
    for signum in (signal.SIGINT, *STOP_SIGNALS):
        handler = signal.getsignal(signum)
        if isinstance(handler, _Guard) or (signum == signal.SIGINT and handler is not None):
            held[signum] = signal.signal(signum, signal.SIG_IGN)
    return held


def _pump(proc: subprocess.Popen, out: bytearray, err: bytearray, seconds: float) -> str:
    """Read a read's stdout into `out` and its stderr into `err` (only the last ERR_TAIL bytes kept) for at most
    `seconds`. Returns "done" once both reached their end and the command exited, "full" once `out` holds more
    than MAX_OUTPUT bytes, and "late" when the time ran out first."""
    deadline = time.monotonic() + seconds
    with selectors.DefaultSelector() as sel:
        for pipe, buf in ((proc.stdout, out), (proc.stderr, err)):
            sel.register(pipe.fileno(), selectors.EVENT_READ, buf)
        while sel.get_map():
            left = deadline - time.monotonic()
            if left <= 0:
                return "late"
            for key, _ in sel.select(left):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    sel.unregister(key.fd)
                    continue
                key.data.extend(chunk)
                if key.data is err:
                    del err[:-ERR_TAIL]
                elif len(out) > MAX_OUTPUT:
                    return "full"
    try:
        proc.wait(timeout=max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        return "late"
    return "done"


def _stop(proc: subprocess.Popen) -> None:
    """Kill a read's whole process group, again until it is empty (at most STOP_TRIES times): on macOS a child
    that is being forked when its group is killed can miss the kill. A group that is already gone is fine."""
    for _ in range(STOP_TRIES):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            return
        proc.poll()  # reap the command, so its zombie does not keep the group alive
        time.sleep(0.001)


def _check_fill(src: sources.Source, records) -> None:
    """Refuse records that do not suit the source's `fill`, and a source whose patterns name an empty setting,
    before anything is run or saved."""
    if src.fill == "paths" and records is not None:
        raise SourceError(f"{src.name}: fill: paths fills its own records; give only the raw output")
    if src.fill == "ai" and records is None:
        raise SourceError(f"{src.name}: fill: ai records are filled by a scan session; give them with the raw output")
    sources.unset_settings(src)


def word_for_word(value: str, text: str) -> bool:
    """Whether `value` appears in `text` as a whole token: the character just before it and just after it is not a
    letter or digit, nor one of JOINERS with a letter or digit beyond it. So `1730000000.123456` is found at the end
    of a sentence ("... 1730000000.123456."), but `1730000000` is not found inside it. An empty value is found."""
    if not value:
        return True
    start = text.find(value)
    while start >= 0:
        end = start + len(value)
        if _edge(text, start - 1, -1) and _edge(text, end, 1):
            return True
        start = text.find(value, start + 1)
    return False


def _edge(text: str, i: int, step: int) -> bool:
    """Whether text[i], the character next to a match (`step` points away from it), ends the token there."""
    if i < 0 or i >= len(text):
        return True
    c = text[i]
    if c.isalnum():
        return False
    beyond = i + step
    return not (c in JOINERS and 0 <= beyond < len(text) and text[beyond].isalnum())


def _one_per_key(src: sources.Source, entries: list) -> list:
    """`fill: ai` entries that share a key (the messages of one Slack thread) all read as one record, so a read makes
    one card and a re-read of the same thread is `unchanged`, instead of updating the card once per message on every
    read. The latest record (by `at`, then `id`, as text) gives every entry of the key its record; the earliest
    record whose category makes a card (a template, not `update_card` or `none`) gives the category, else the latest
    `update_card` one's, else the latest's, so a recovery and a later `fyi` reply still update the card in either
    order. Each entry keeps its `item`, so the report still lists every record. An entry whose key cannot be made is
    kept as it is, for `build` to drop with the reason."""
    groups, order = {}, []
    for e in entries:
        try:
            key = form.fill_pattern(src.key, {**sources.settings(src), **form.derived(e["record"], src.name)})
        except ValueError:
            order.append([e])
            continue
        if key not in groups:
            groups[key] = []
            order.append(groups[key])
        groups[key].append(e)
    out = []
    for group in order:
        by_time = sorted(group, key=lambda e: (e["record"]["at"], e["record"]["id"]))
        making = [e["category"] for e in by_time if src.sort[e["category"]] not in sources.SORT_SPECIAL]
        updating = [e["category"] for e in by_time if src.sort[e["category"]] == "update_card"]
        category = making[0] if making else (updating[-1] if updating else by_time[-1]["category"])
        out.extend({**e, "record": by_time[-1]["record"], "category": category} for e in group)
    return sorted(out, key=lambda e: e["item"])


def _skipped(src: sources.Source, record: dict) -> Optional[str]:
    """The reason a `fill: ai` record is dropped by `read.skip_channels`, or None. A leading `#` is ignored on both
    sides, and letter case too (Slack channel names are lowercase)."""
    skip = {str(x).lstrip("#").casefold() for x in src.data["read"].get("skip_channels") or []}
    for field in ("place", "place_id"):
        value = record.get(field)
        if value and value.lstrip("#").casefold() in skip:
            return f"form.{field} {value!r} is in read.skip_channels"
    return None


def _check_count(src: sources.Source, n: int, raw: Optional[str]) -> None:
    """Refuse a read with more than MAX_ITEMS items, naming `raw`, where its output was saved."""
    if n > MAX_ITEMS:
        where = f" (saved as {raw})" if raw else ""
        raise SourceError(f"{src.name}: the reply has {n} items; at most {MAX_ITEMS} are read{where}")


def required(src: sources.Source) -> tuple:
    """The fields a record of `src` must have (design 5.2): the form's, and `status` for `brings: work`."""
    return form.REQUIRED + (("status",) if src.brings == "work" else ())


def _same(key: str, old, new) -> bool:
    """Whether a card's field already holds the new value. The source block's `raw` does not count: a new read of
    the same item is not a change."""
    if key == "source" and isinstance(old, dict) and isinstance(new, dict):
        return {k: v for k, v in old.items() if k != "raw"} == {k: v for k, v in new.items() if k != "raw"}
    return old == new


# -- old raw files (each gate run prunes them) --------------------------------------------------------------------

# A read's raw file and its report are kept this long, counted from the UTC stamp in their names, the same boundary
# as restore points. A constant, not a config.yaml key: a new key would change `ap2 config`'s output.
RAW_KEEP = timedelta(days=30)
RAW_NAME = re.compile(r"([0-9]{8}T[0-9]{6}Z)-[0-9a-f]{6}(\.report)?\.json\Z", re.ASCII)
REPORT = ".report.json"
_OPEN_DIR = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def prune_raw(data, now: datetime, list_cards: Callable[[], list]) -> dict:
    """Remove the raw files and reports under `<data>/sources/.raw/<source>/` whose name stamp is more than
    RAW_KEEP before `now` (kept at exactly RAW_KEEP). Returns `{"removed": [paths relative to data], "errors":
    [{"prune_raw": <path or true>, "error": ...}], "link": None or the folder that is a link}`.

    - A file's age comes from its name, `<YYYYMMDDTHHMMSSZ>-<6 hex>.json` (its report: `.report.json`), not from its
      mtime. Any other name, a hidden one, a stamp that cannot be read and a stamp later than `now` are kept.
    - A report goes with its raw file: it is kept while the raw file is there, and judged by its own name once the
      raw file is gone.
    - `list_cards()` (the store's `list`) is called once, only when a file is old enough to remove. A file named in
      a card's `source.raw`, and its report, are kept. When the cards cannot be read nothing is removed.
    - No link is followed or removed: when `sources` or `sources/.raw` is a link nothing is pruned (`link` names
      it); a source folder or file that is a link, and a hidden entry, are skipped. Each folder is opened once
      without following a link, and every remove is relative to it, so a folder swapped for a link meanwhile does
      not send a remove elsewhere. Never raises OSError: a folder that cannot be read or a file that cannot be
      removed is one entry in `errors`, and the rest still go."""
    out = {"removed": [], "errors": [], "link": None}
    fds = []
    try:
        top = _open_raw_folder(Path(data), fds, out)
        if top is None:
            return out
        old = []  # (source, folder fd, names in the folder, [(old name, is a report)])
        try:
            folders = sorted(e.name for e in os.scandir(top)
                             if not e.name.startswith(".") and e.is_dir(follow_symlinks=False))
        except OSError as e:
            out["errors"].append({"prune_raw": RAW.as_posix(), "error": f"{RAW}: cannot list the raw folder: {e}"})
            return out
        for source in folders:
            where = (RAW / source).as_posix()
            try:
                fd = _open_dir(source, top)
                if fd is None:  # no longer a folder
                    continue
                fds.append(fd)
                entries = list(os.scandir(fd))
            except _Link:  # it became a link after the listing: skipped
                continue
            except OSError as e:
                out["errors"].append({"prune_raw": where, "error": f"{where}: cannot list the raw files: {e}"})
                continue
            names = {e.name for e in entries}
            found = []
            try:
                for e in sorted(entries, key=lambda e: e.name):
                    m = RAW_NAME.match(e.name)
                    if m is None or not e.is_file(follow_symlinks=False):  # may lstat, so may raise
                        continue
                    try:
                        at = datetime.strptime(m.group(1), STAMP).replace(tzinfo=timezone.utc)
                    except ValueError:  # e.g. month 13: its age cannot be read, so it is kept
                        continue
                    if now - at > RAW_KEEP:
                        found.append((e.name, m.group(2) is not None))
            except OSError as e:
                out["errors"].append({"prune_raw": where, "error": f"{where}: cannot list the raw files: {e}"})
                continue
            if found:
                old.append((source, fd, names, found))
        if not old:
            return out
        try:
            linked = {posixpath.normpath(c["source"]["raw"]) for c in list_cards()
                      if isinstance(c, dict) and isinstance(c.get("source"), dict)
                      and isinstance(c["source"].get("raw"), str)}
        except (CardError, OSError, RecursionError) as e:  # RecursionError: a card file nested too deeply
            why = "nested too deeply" if isinstance(e, RecursionError) else e
            out["errors"].append({"prune_raw": True,
                                  "error": f"cannot read the cards, so no raw file is pruned: {why}"})
            return out
        for source, fd, names, found in old:
            gone = set()
            # Raw files first, so a report knows whether its raw file is still there.
            for n, is_report in sorted(found, key=lambda f: f[1]):
                raw_name = n[:-len(REPORT)] + ".json" if is_report else n
                if (RAW / source / raw_name).as_posix() in linked or (RAW / source / n).as_posix() in linked:
                    continue
                if is_report and raw_name in names and raw_name not in gone:
                    continue
                where = (RAW / source / n).as_posix()
                try:
                    if not stat.S_ISREG(os.stat(n, dir_fd=fd, follow_symlinks=False).st_mode):
                        continue  # it became something else meanwhile
                    os.unlink(n, dir_fd=fd)
                except FileNotFoundError:
                    gone.add(n)
                    continue
                except OSError as e:
                    out["errors"].append({"prune_raw": where, "error": f"{where}: cannot remove the old raw file: "
                                                                      f"{e}"})
                    continue
                gone.add(n)
                out["removed"].append(where)
        return out
    finally:
        for fd in fds:
            os.close(fd)


def _open_raw_folder(data: Path, fds: list, out: dict) -> Optional[int]:
    """The fd of `<data>/sources/.raw`, opened without following a link below the data folder; None when there
    is nothing to prune (no folder, or a link, which `out["link"]` then names)."""
    try:
        fd = os.open(data, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))  # the data folder itself may be a link
        fds.append(fd)
        for part in (Path(sources.FOLDER), RAW):
            try:
                fd = _open_dir(part.name, fd)
            except _Link:
                out["link"] = part.as_posix()
                return None
            if fd is None:
                return None
            fds.append(fd)
        return fd
    except OSError as e:
        out["errors"].append({"prune_raw": RAW.as_posix(), "error": f"{RAW}: cannot open the raw folder: {e}"})
        return None


class _Link(Exception):
    pass


def _open_dir(name: str, dir_fd: int) -> Optional[int]:
    """Open the folder `name` in `dir_fd` without following a link. None when it is missing or not a folder;
    _Link when it is a link."""
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode):
        raise _Link(name)
    if not stat.S_ISDIR(st.st_mode):
        return None
    try:
        return os.open(name, _OPEN_DIR, dir_fd=dir_fd)
    except OSError as e:  # it changed after the stat
        if e.errno == errno.ELOOP:
            raise _Link(name) from None
        if e.errno in (errno.ENOENT, errno.ENOTDIR):
            return None
        raise
