"""`<data>/index.sqlite`: text search over the cards. Never the source of truth: the card files in `items/` are.

Schema (version 1):

    meta(key TEXT PRIMARY KEY, value TEXT)            -- ('version', '1')
    cards(id TEXT PRIMARY KEY, text TEXT NOT NULL)    -- casefolded search text of one card

A search splits the query on white space and keeps the cards whose text holds every term (casefolded, as a
substring, `LIKE` with `%` and `_` escaped). No FTS5: its default tokenizer cannot split Chinese words, and a
substring match needs no query syntax. When the file is missing, unreadable or of another version, it is removed
and rebuilt from the cards.

`<data>/.index.stale` is written before a card file and removed once the index holds that card. When a write
stops in between (a crash, Ctrl-C), the marker stays and the next use of the index rebuilds it, so the index is
never left behind for good.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
from pathlib import Path

FILE = "index.sqlite"
STALE = ".index.stale"
VERSION = "1"


def _escape(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Index:
    def __init__(self, data: Path, all_cards, text_of):
        """`all_cards()` yields every card from items/ (for a rebuild); `text_of(card)` is its search text."""
        self.path = Path(data) / FILE
        self.stale = Path(data) / STALE
        self._all_cards = all_cards
        self._text_of = text_of

    def mark_stale(self) -> bool:
        """Called before a card file is written. Return True when the marker was already there: an earlier write
        stopped before the index caught up, so the next put() rebuilds."""
        if self.stale.exists():
            return True
        self.stale.touch()
        return False

    def _clear_stale(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.stale)

    def _connect(self, check_stale: bool = True) -> sqlite3.Connection:
        """An open index of this version, rebuilt first when it is missing, unusable or (with `check_stale`)
        marked stale."""
        if check_stale and self.stale.exists():
            return self.rebuild()
        if self.path.exists():
            try:
                conn = sqlite3.connect(str(self.path))
                try:
                    row = conn.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
                    conn.execute("SELECT count(*) FROM cards").fetchone()
                except sqlite3.Error:
                    conn.close()
                    raise
                if row is not None and row[0] == VERSION:
                    return conn
                conn.close()
            except sqlite3.Error:
                pass
        return self.rebuild()

    def _remove(self) -> None:
        for suffix in ("", "-journal", "-wal", "-shm"):
            try:
                os.unlink(f"{self.path}{suffix}")
            except FileNotFoundError:
                pass

    def rebuild(self) -> sqlite3.Connection:
        """Write a fresh index from every card file; return it open."""
        self._remove()
        tmp = self.path.with_name(f".{FILE}.{os.getpid()}.tmp")
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        try:
            conn = sqlite3.connect(str(tmp))
            try:
                with conn:
                    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
                    conn.execute("CREATE TABLE cards(id TEXT PRIMARY KEY, text TEXT NOT NULL)")
                    conn.execute("INSERT INTO meta VALUES ('version', ?)", (VERSION,))
                    conn.executemany("INSERT INTO cards VALUES (?, ?)",
                                     ((c["id"], self._text_of(c).casefold()) for c in self._all_cards()))
            finally:
                conn.close()
            os.replace(tmp, self.path)  # a reader never sees a half-built index
        except BaseException:  # a damaged card file, a full disk: leave no temp file behind
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        self._clear_stale()
        return sqlite3.connect(str(self.path))

    def put(self, card: dict, behind: bool = False) -> None:
        """Keep the index in step after a card was written (`behind`: an earlier write did not finish, so rebuild
        instead). Never raises: the card file is already written, so a failure here must not report the change
        as failed (a retry would make a second card). On failure the index is removed and the next use rebuilds
        it."""
        try:
            if behind:
                self.rebuild().close()
                return
            conn = self._connect(check_stale=False)
            try:
                with conn:
                    conn.execute("INSERT OR REPLACE INTO cards VALUES (?, ?)",
                                 (card["id"], self._text_of(card).casefold()))
            finally:
                conn.close()
            self._clear_stale()
        except Exception:  # sqlite errors, a damaged card file met by a rebuild (CardError), OSError
            with contextlib.suppress(OSError):
                self._remove()

    def search(self, text: str) -> list:
        """The ids of the cards whose search text holds every word of `text`, in id order (oldest first)."""
        terms = text.casefold().split()
        if not terms:
            return []
        where = " AND ".join("text LIKE ? ESCAPE '\\'" for _ in terms)
        params = [f"%{_escape(t)}%" for t in terms]
        for attempt in (1, 2):
            try:
                conn = self._connect()
                try:
                    return [r[0] for r in conn.execute(f"SELECT id FROM cards WHERE {where} ORDER BY id", params)]
                finally:
                    conn.close()
            except sqlite3.Error:
                if attempt == 2:
                    raise
                self._remove()  # broken after it opened; rebuild once
        return []
