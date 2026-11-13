"""The card store: one JSON file per card under `<data>/items/<yyyy-mm>/<id>.json` (design 3.4, 4.1).

Every add, update and restore appends `{at, by, did, restore}` to the card's `audit`; every change to an existing
card first saves its file as a restore point (`restore.py`); the search index (`index.py`) follows each write.
Writes are atomic (a temp file, then `os.replace`) and hold `<data>/.store.lock`, so two processes never lose
each other's change. Nothing here writes outside the data folder: a symlink where the store keeps its own files
(`items/`, a month folder, a card file, `restore/`, a point file, the lock, the index and its marker) is refused,
never followed to write and never replaced (pruning reads a linked point and removes only the link), so a link into
another folder (v1's data, for one) cannot carry a write out.
"""
from __future__ import annotations

import contextlib
import copy
import errno
import itertools
import json
import os
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from assistant import cards, config, index, restore
from assistant.cards import CardError
from assistant.index import Index

try:
    import fcntl
except ImportError:  # not POSIX: one process at a time is then the caller's job
    fcntl = None

ITEMS = "items"
LOCK = ".store.lock"
# The store's own names right under the data folder, checked for links once per call (`_guard`). The lock is
# checked before it is opened; month folders and card files where they are opened, point files where they are put back.
OWN = (ITEMS, restore.FOLDER, index.STALE, *(index.FILE + s for s in index.SUFFIXES))
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)  # 0 where the platform has none: the lstat check alone then


class _Refused(Exception):
    """`update_checked`'s check said no: nothing is written."""


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


# The deepest dict / list nesting a card or its changes may hold; far under where the copy or the writer stop.
MAX_DEPTH = 200
# The most items (list or tuple items, dict keys and values, of any kind) `check_writable(shared=True)` looks at
# after it first meets a cycle (see there); a value needing more is refused as too deep. Well under a second.
CYCLE_WALK_BUDGET = 100_000


def check_writable(value, what: str = "card is", *, shared: bool = True) -> None:
    """Refuse `value` when its dicts, lists and tuples nest more than MAX_DEPTH deep (the top one is depth 1).
    `json.loads` and the checks' unindented `json.dumps` run in C and follow a value much deeper than
    `copy.deepcopy` and the indented writer, which stop at the recursion limit. Where they stop depends on how deep
    the caller's stack already is, so trying the copy gives one answer on write and another on read (a card the
    store wrote and then could not read). A fixed bound well under that limit gives the same answer everywhere, and
    walking the value (with a list, not recursion; nothing copied, no text built) is cheap on every read. Raises
    TooDeepError `<what> nested too deeply to copy or write` at the first level past the bound. Anything else (a
    value JSON cannot write, a container already on the path, i.e. a cycle) is left to the card checks.

    A depth is counted along non-repeating paths: a container already on the path is not walked into again. A
    shared value is walked again only when met deeper, which is exact while no cycle has been seen (what lies
    below a container then does not depend on the path to it). Once the walk meets the first cycle, a container
    walked before may have been cut short by a back edge to a container that a later route does not hold, so from
    then on every non-repeating path is walked. That work is capped: past CYCLE_WALK_BUDGET items looked at after
    the first cycle (every list or tuple item and dict key and value, a number or a container on the path
    included), the value is refused with the same TooDeepError (a value with a cycle can never be written as
    JSON, so nothing writable is lost). A small shallow cycle still passes here and is refused by the card checks.

    `shared=False` is for a value fresh from `json.loads`, a tree with no shared containers or cycles: the memo of
    what was walked would then skip nothing and only hold one entry per container."""
    kinds = (dict, list, tuple)
    if not isinstance(value, kinds):
        return

    def inside(node):
        return itertools.chain.from_iterable(node.items()) if isinstance(node, dict) else iter(node)

    def too_deep():
        return cards.TooDeepError(f"{what} nested too deeply to copy or write")

    path = [(value, inside(value))]
    on_path = {id(value)}
    walked = {id(value): 1} if shared else None  # id -> the deepest depth a container was already walked from
    left = None  # once a cycle is seen (shared only): how many more items may be looked at
    while path:
        node, rest = path[-1]
        for child in rest:
            if left is not None:
                if left <= 0:
                    raise too_deep()
                left -= 1
            if not isinstance(child, kinds):
                continue
            if id(child) in on_path:  # a cycle: from here on the memo no longer tells the depth below a container
                if walked is not None:
                    walked, left = None, CYCLE_WALK_BUDGET
                continue
            if len(path) >= MAX_DEPTH:
                raise too_deep()
            depth = len(path) + 1
            # an empty one holds nothing deeper: counted above, never walked into; one already walked from as
            # deep or deeper holds nothing new (with the memo, i.e. until the first cycle, each is walked at most
            # MAX_DEPTH times, not once per path; after it, the budget bounds the work)
            if child and (walked is None or walked.get(id(child), 0) < depth):
                if walked is not None:
                    walked[id(child)] = depth
                path.append((child, inside(child)))
                on_path.add(id(child))
                break
        else:
            path.pop()
            on_path.discard(id(node))


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

    def _refuse_link(self, path: Path) -> Path:
        """Return `path`, or raise CardError when it is a symlink (dangling or not). Missing is fine."""
        if path.is_symlink():
            raise CardError(f"{path} is a symlink; the card store never follows a link inside its data folder "
                            f"{self.data} (move the link away)")
        return path

    def _card_path(self, card_id: str) -> Path:
        """`path(card_id)`, its month folder and file checked for links; use it wherever the file is opened."""
        p = self.path(card_id)
        self._refuse_link(p.parent)
        return self._refuse_link(p)

    @contextlib.contextmanager
    def _locked(self):
        if fcntl is None:
            yield
            return
        self.data.mkdir(parents=True, exist_ok=True)
        lock = self.data / LOCK
        try:
            # O_NOFOLLOW: a link swapped in after _guard's check is still refused, not opened
            fd = os.open(str(lock), os.O_CREAT | os.O_RDWR | NOFOLLOW, 0o644)
        except OSError as e:
            if e.errno in (errno.ELOOP, errno.EMLINK) and lock.is_symlink():  # EMLINK: FreeBSD's O_NOFOLLOW
                self._refuse_link(lock)
            raise
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)  # closing releases the lock

    def _read(self, path: Path) -> dict:
        try:
            card = json.loads(path.read_text(encoding="utf-8"))
        except RecursionError:
            raise cards.TooDeepError(f"{path}: cannot read the card: nested too deeply") from None
        except (OSError, ValueError) as e:
            raise CardError(f"{path}: cannot read the card: {e}") from e
        if not (isinstance(card, dict) and card.get("id") == path.stem):
            raise CardError(f"{path}: not a card file (its id is not its file name)")
        try:
            cards.check_stored(card)
            check_writable(card, shared=False)  # fresh from json.loads: a tree
        except CardError as e:
            raise (cards.TooDeepError if isinstance(e, cards.TooDeepError) else CardError)(
                f"{path}: not a card file: {e}") from None
        return card

    def _load(self, card_id: str) -> dict:
        p = self._card_path(card_id)
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
            if not self._refuse_link(sub).is_dir():
                continue
            for p in sorted(sub.iterdir()):
                if p.suffix == ".json" and cards.CARD_ID.match(p.stem) and sub.name == cards.month(p.stem):
                    yield self._read(self._refuse_link(p))

    def _write(self, card: dict) -> None:
        behind = self.index.mark_stale()  # before the card file, so a stop in between is caught up later
        write_json(self._card_path(card["id"]), card)
        self.index.put(card, behind=behind)

    def _entry(self, at: datetime, by: str, did: str, point: Optional[str]) -> dict:
        return {"at": cards.stamp(at), "by": by, "did": did, "restore": point}

    @staticmethod
    def _by(by) -> str:
        if not (isinstance(by, str) and by.strip()):
            raise CardError(f"by must name who made the change, not {by!r}")
        cards.check_unicode(by, "by")
        return by

    @staticmethod
    def _did(did) -> Optional[str]:
        if did is None:
            return None
        if not (isinstance(did, str) and did.strip()):
            raise CardError(f"did must say what the change did, or be left out, not {did!r}")
        cards.check_unicode(did, "did")
        return did

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
        check_writable(card)  # first, as in update: a value with a cycle past the bound gets the depth words
        cards.validate(card, self.languages)
        with self._guard():
            at = self.clock()
            new = copy.deepcopy(card)
            new["id"] = cards.new_id("c", at)
            while self._card_path(new["id"]).exists():
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
        by, did = self._check_update(card_id, changes, by, did)
        with self._guard():
            return self._update(card_id, changes, by, did)

    def update_checked(self, card_id: str, changes: dict, by: str, check: Callable[[dict], object],
                       did: Optional[str] = None) -> Optional[dict]:
        """`update`, but only when `check(card)` says yes: the card is read, checked and written under one hold of
        the store lock, so no other writer lands between the look and the change. `check` returns False to refuse,
        True to go on, or a text that says what the change did (it replaces `did`, for a line that depends on
        what the check saw). Return the card, or None (and write nothing) when `check` refuses."""
        by, did = self._check_update(card_id, changes, by, did)

        def refuse(card):
            ok = check(card)
            if not ok:
                raise _Refused
            return self._did(ok) if isinstance(ok, str) else None

        with self._guard():
            try:
                return self._update(card_id, changes, by, did, check=refuse)
            except _Refused:
                return None

    def _check_update(self, card_id: str, changes: dict, by: str, did: Optional[str]) -> tuple:
        """What `update` and `update_checked` refuse before the lock; return the checked (by, did)."""
        by = self._by(by)
        did = self._did(did)
        cards.check_id(card_id)
        if not (isinstance(changes, dict) and changes):
            raise CardError("changes must be a non-empty object of fields to set")
        given = [k for k in cards.STORE_KEYS if k in changes]
        if given:
            pronoun = "it" if len(given) == 1 else "them"
            raise CardError(f"{', '.join(given)} cannot be changed; the store sets {pronoun}")
        return by, did

    def _update(self, card_id: str, changes: dict, by: str, did: Optional[str], check=None) -> dict:
        """The update itself; the caller holds the lock. `check(old)` may refuse the change first (it raises), or
        return a text that replaces `did`."""
        old = self._load(card_id)
        said = check(old) if check is not None else None
        if isinstance(said, str):
            did = said
        check_writable(changes, "changes are")
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
            self._refuse_link(restore.path(self.data, point_id))
            point = restore.load(self.data, point_id)
            old = point["card"]
            card_id = cards.check_id(old.get("id"))
            # Only what the store needs, not the enums or plain words: a point holds whatever file was there (a
            # hand edit too), and the languages may have changed since. Any point the store saved can be put back.
            try:
                cards.check_stored(old)
                check_writable(old, shared=False)  # before restore.save, so a point too deep leaves no orphan point
            except CardError as e:
                raise (cards.TooDeepError if isinstance(e, cards.TooDeepError) else CardError)(
                    f"{restore.path(self.data, point_id)}: not a card restore point: {e}") from None
            at = self.clock()
            current = self._read(self._card_path(card_id)) if self._card_path(card_id).exists() else None
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

    def ask_about(self, card_id: str, text: str, by: str) -> dict:
        """Save a question about the card (design 9.4) at the end of its `questions` list (made when missing) as
        `{id, at, by, text, status: "pending", answer: null}`; return the card, the new question last. The card
        is read and written under one hold of the store lock, so two questions at once are never lost; the
        audit entry is `ask <question id>` with its restore point. Nothing answers it here: a later slice picks
        up pending questions and writes the answer."""
        by = self._by(by)
        cards.check_id(card_id)
        if not (isinstance(text, str) and text.strip()):
            raise CardError(f"a question must be a non-empty text, not {text!r}")
        cards.check_unicode(text, "question")
        with self._guard():
            asked = self._load(card_id).get("questions", [])
            if not isinstance(asked, list):
                raise CardError(f"card {card_id}: questions must be a list, not {asked!r}")
            at = self.clock()
            taken = {q["id"] for q in asked if isinstance(q, dict) and isinstance(q.get("id"), str)}
            question_id = cards.new_id("q", at)
            while question_id in taken:
                question_id = cards.new_id("q", at)
            question = {"id": question_id, "at": cards.stamp(at), "by": by, "text": text, "status": "pending",
                        "answer": None}
            return self._update(card_id, {"questions": asked + [question]}, by, f"ask {question_id}")

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
            found = [self._read(self._card_path(i)) for i in ids if self._card_path(i).exists()]
        # A row can be behind its file (a hand edit); keep only the cards whose file holds every word.
        return [c for c in found if all(t in cards.search_text(c).casefold() for t in terms)]

    @contextlib.contextmanager
    def _guard(self):
        """The store lock, with file and database errors turned into one CardError. A symlink at one of the
        store's own names is refused first (a few lstat calls; the lock before it is opened, the rest under it)."""
        try:
            self._refuse_link(self.data / LOCK)
            with self._locked():
                for name in OWN:
                    self._refuse_link(self.data / name)
                yield
        except OSError as e:
            raise CardError(f"cannot use the card store in {self.data}: {e}") from e
        except sqlite3.Error as e:
            raise CardError(f"search index {self.index.path}: {e}") from e
