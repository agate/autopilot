"""The card store: one JSON file per card under `<data>/items/<yyyy-mm>/<id>.json` (design 3.4, 4.1).

Every add, update and restore appends `{at, by, did, restore}` to the card's `audit`; every change to an existing
card first saves its file as a restore point (`restore.py`); the search index (`index.py`) follows each write.
Writes are atomic (a temp file, then `os.replace`) and hold `<data>/.store.lock`, so two processes never lose
each other's change. Nothing here writes outside the data folder.
"""
from __future__ import annotations

import contextlib
import copy
import json
import os
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from assistant import cards, config, restore
from assistant.cards import CardError
from assistant.index import Index

try:
    import fcntl
except ImportError:  # not POSIX: one process at a time is then the caller's job
    fcntl = None

ITEMS = "items"
LOCK = ".store.lock"


def write_json(path: Path, obj) -> None:
    """Write `obj` as UTF-8 JSON with sorted keys, atomically: a reader sees the old file or the new one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, sort_keys=True, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


class Store:
    def __init__(self, data, cfg: Optional[config.Config] = None, clock: Callable[[], datetime] = cards.now_utc):
        """`data` is a data folder (see `datadir`); `cfg` defaults to its config.yaml; `clock()` returns the time
        as an aware datetime (tests pass a fixed one)."""
        self.data = Path(data)
        self.cfg = cfg if cfg is not None else config.load(self.data)
        self.languages = cards.languages(self.cfg)
        self.clock = clock
        self.index = Index(self.data, self._all, cards.search_text)

    # -- files ---------------------------------------------------------------------------------------------------

    def path(self, card_id: str) -> Path:
        return self.data / ITEMS / cards.month(card_id) / f"{card_id}.json"

    @contextlib.contextmanager
    def _locked(self):
        if fcntl is None:
            yield
            return
        self.data.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.data / LOCK), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)  # closing releases the lock

    def _read(self, path: Path) -> dict:
        try:
            card = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise CardError(f"{path}: cannot read the card: {e}") from e
        if not (isinstance(card, dict) and card.get("id") == path.stem):
            raise CardError(f"{path}: not a card file (its id is not its file name)")
        try:
            cards.check_stored(card)
        except CardError as e:
            raise CardError(f"{path}: not a card file: {e}") from None
        return card

    def _load(self, card_id: str) -> dict:
        p = self.path(card_id)
        if not p.exists():
            raise CardError(f"no card {card_id}")
        return self._read(p)

    def _all(self):
        folder = self.data / ITEMS
        if not folder.is_dir():
            return
        # iterdir(), not glob(): glob skips a folder it cannot read, and a rebuild would then drop its cards for
        # good. iterdir() raises, and _guard() turns that into one CardError.
        for sub in sorted(folder.iterdir()):
            if not sub.is_dir():
                continue
            for p in sorted(sub.iterdir()):
                if p.suffix == ".json" and cards.CARD_ID.match(p.stem) and sub.name == cards.month(p.stem):
                    yield self._read(p)

    def _write(self, card: dict) -> None:
        behind = self.index.mark_stale()  # before the card file, so a stop in between is caught up later
        write_json(self.path(card["id"]), card)
        self.index.put(card, behind=behind)

    def _entry(self, at: datetime, by: str, did: str, point: Optional[str]) -> dict:
        return {"at": cards.stamp(at), "by": by, "did": did, "restore": point}

    @staticmethod
    def _by(by) -> str:
        if not (isinstance(by, str) and by.strip()):
            raise CardError(f"by must name who made the change, not {by!r}")
        cards.check_unicode(by, "by")
        return by

    # -- the API -------------------------------------------------------------------------------------------------

    def add(self, card: dict, by: str) -> dict:
        """Add a new card; return it with its id, times and first audit entry. Raises CardError."""
        by = self._by(by)
        if not isinstance(card, dict):
            raise CardError(f"a card is a JSON object, not {type(card).__name__}")
        given = [k for k in cards.STORE_KEYS if k in card]
        if given:
            raise CardError(f"{', '.join(given)} {'is' if len(given) == 1 else 'are'} set by the store; leave "
                            f"{'it' if len(given) == 1 else 'them'} out")
        cards.validate(card, self.languages)
        with self._guard():
            at = self.clock()
            new = copy.deepcopy(card)
            new["id"] = cards.new_id("c", at)
            while self.path(new["id"]).exists():
                new["id"] = cards.new_id("c", at)
            new["created_at"] = new["updated_at"] = cards.stamp(at)
            new["audit"] = [self._entry(at, by, "add", None)]
            self._write(new)
            return new

    def get(self, card_id: str) -> dict:
        with self._guard():
            return self._load(cards.check_id(card_id))

    def list(self, lane: Optional[str] = None, kind: Optional[str] = None, parent: Optional[str] = None) -> list:
        """Every card, oldest first, keeping only those that match each filter given."""
        if lane is not None and lane not in cards.LANES:
            raise CardError(f"lane must be one of {', '.join(cards.LANES)}, not {lane!r}")
        with self._guard():
            return [c for c in self._all()
                    if (lane is None or c.get("lane") == lane) and (kind is None or c.get("kind") == kind)
                    and (parent is None or c.get("parent") == parent)]

    def update(self, card_id: str, changes: dict, by: str, did: Optional[str] = None) -> dict:
        """Set the top-level fields in `changes` (null clears `parent`, `ask` or `title`); return the card. The
        old file is saved as a restore point first. A change that changes nothing writes nothing."""
        by = self._by(by)
        cards.check_id(card_id)
        if not (isinstance(changes, dict) and changes):
            raise CardError("changes must be a non-empty object of fields to set")
        given = [k for k in cards.STORE_KEYS if k in changes]
        if given:
            pronoun = "it" if len(given) == 1 else "them"
            raise CardError(f"{', '.join(given)} cannot be changed; the store sets {pronoun}")
        with self._guard():
            return self._update(card_id, changes, by, did)

    def _update(self, card_id: str, changes: dict, by: str, did: Optional[str], check=None) -> dict:
        """The update itself; the caller holds the lock. `check(old)` may refuse the change first."""
        old = self._load(card_id)
        if check is not None:
            check(old)
        new = copy.deepcopy(old)
        new.update(copy.deepcopy(changes))
        cards.validate(new, self.languages)
        if new == old:
            return old
        at = self.clock()
        point = restore.save(self.data, old, at, write_json)
        new["updated_at"] = cards.stamp(at)
        new["audit"] = old["audit"] + [self._entry(at, by, did or f"update: {', '.join(sorted(changes))}", point)]
        self._write(new)
        return new

    def restore(self, point_id: str, by: str) -> dict:
        """Put the card saved in restore point `point_id` back; return it. The current card is saved as a new
        point first, so this restore can be restored too. The audit keeps every entry and adds this one."""
        by = self._by(by)
        with self._guard():
            point = restore.load(self.data, point_id)
            old = point["card"]
            card_id = cards.check_id(old.get("id"))
            # Only what the store needs, not the enums or plain words: a point holds whatever file was there (a
            # hand edit too), and the languages may have changed since. Any point the store saved can be put back.
            cards.check_stored(old)
            at = self.clock()
            current = self._read(self.path(card_id)) if self.path(card_id).exists() else None
            saved = restore.save(self.data, current, at, write_json) if current is not None else None
            back = copy.deepcopy(old)
            back["updated_at"] = cards.stamp(at)
            back["audit"] = (current or old)["audit"] + [self._entry(at, by, f"restore {point_id}", saved)]
            self._write(back)
            return back

    def propose_opposite(self, card_id: str, action: str, run: list, by: str, text: Optional[str] = None) -> dict:
        """Record on the card a proposed opposite action for an outside change, as an `approve` ask holding what
        it would run. Nothing is run: the user approves first, and a later slice runs it in live mode only."""
        by = self._by(by)
        cards.check_id(card_id)
        ask = restore.opposite_ask(action, run, text)

        def no_open_ask(card):
            if card.get("ask") is not None:
                raise CardError(f"card {card_id} already has an open {card['ask'].get('type')} ask; settle it first")

        with self._guard():
            return self._update(card_id, {"ask": ask}, by, f"propose {action}", check=no_open_ask)

    def prune(self, now: Optional[datetime] = None) -> list:
        """Remove restore points older than 30 days; return their ids."""
        with self._guard():
            return restore.prune(self.data, now or self.clock())

    def search(self, text: str) -> list:
        """The cards whose plain words, title, kind or feed hold every word of `text`, oldest first. Read from the
        card files; the index only picks them."""
        cards.check_unicode(text, "search text")
        terms = text.casefold().split()
        with self._guard():
            # The index is never the truth: skip a row that is not a card id, and a duplicate row.
            ids = [i for i in dict.fromkeys(self.index.search(text)) if isinstance(i, str) and cards.CARD_ID.match(i)]
            found = [self._read(self.path(i)) for i in ids if self.path(i).exists()]
        # A row can be behind its file (a hand edit); keep only the cards whose file holds every word.
        return [c for c in found if all(t in cards.search_text(c).casefold() for t in terms)]

    @contextlib.contextmanager
    def _guard(self):
        """The store lock, with file and database errors turned into one CardError."""
        try:
            with self._locked():
                yield
        except OSError as e:
            raise CardError(f"cannot use the card store in {self.data}: {e}") from e
        except sqlite3.Error as e:
            raise CardError(f"search index {self.index.path}: {e}") from e
