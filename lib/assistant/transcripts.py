"""Gzipped copies of workers' conversations (design 3.5) under `keep_transcripts`.

Claude Code keeps a session's conversation in `<projects>/<cwd>/<session id>.jsonl`, `<projects>` being
`~/.claude/projects`. Each time a round ends, a gzipped copy goes to
`<data>/sessions/<card>/<role>-<session id>.jsonl.gz`. The folder of originals is always passed in (tests give a
temp folder); this module only reads it. Deleting an original is `claude rm`, which needs live mode.
"""
from __future__ import annotations

import contextlib
import errno
import gzip
import os
import re
import shutil
import stat
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from assistant import cards, config
from assistant.resources import WorkerError

SESSIONS = "sessions"
SUFFIX = ".jsonl.gz"
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,99}\Z")
ROLE = re.compile(r"[a-z]+")  # a role is part of an archive's file name: lowercase letters only


def default_projects() -> Path:
    return Path.home() / ".claude" / "projects"


def run_dir(data, card_id: str) -> Path:
    """The card's folder in `sessions/`: its archives and its workers' result files (brief, journal, hand-back)."""
    return Path(data) / SESSIONS / cards.check_id(card_id)


def _check_sid(session_id) -> str:
    if not (isinstance(session_id, str) and SESSION_ID.match(session_id)):
        raise ValueError(f"not a session id: {session_id!r}")
    return session_id


def find(projects, session_id: str) -> Optional[Path]:
    """The session's jsonl under `projects`, or None. When more than one folder holds it, the newest wins."""
    folder = Path(projects)
    if not folder.is_dir():
        return None
    hits = [p for p in folder.glob(f"*/{_check_sid(session_id)}.jsonl") if p.is_file()]
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def open_regular(path):
    """`path` opened for binary reading, or None when it is not a regular file at the time it is opened (a FIFO, a
    device, a symlink): a process the worker left can swap its transcript after `find` saw a file. It is opened
    without waiting (a FIFO's plain open waits for a writer) and checked on the open file. Other errors raise."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
    except OSError as e:
        if e.errno == errno.ELOOP:  # a symlink, refused by O_NOFOLLOW
            return None
        raise
    try:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    os.close(fd)
    return None


def _check_role(role) -> str:
    if not (isinstance(role, str) and ROLE.fullmatch(role)):
        raise ValueError(f"not a role: {role!r}")
    return role


def archive_path(data, card_id: str, role: str, session_id: str) -> Path:
    _check_role(role)
    return run_dir(data, card_id) / f"{role}-{_check_sid(session_id)}{SUFFIX}"


def archive(data, cfg: config.Config, card_id: str, role: str, session_id: str, projects,
            now: Optional[datetime] = None) -> Optional[Path]:
    """Copy the session's jsonl, gzipped, into the card's folder; return the copy, or None when there is nothing
    to copy (no original) or `keep_transcripts` is `never`. Then prune by the setting. Works in shadow mode: it
    writes only inside the data folder."""
    out = None
    if cfg.keep_transcripts != "never":
        src = find(projects, session_id)
        if src is not None:
            out = archive_path(data, card_id, role, session_id)
            out.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=f".{out.name}.", suffix=".tmp", dir=str(out.parent))
            try:
                with os.fdopen(fd, "wb") as raw:
                    f = open_regular(src)
                    if f is not None:
                        with f, gzip.GzipFile(filename=src.name, mode="wb", fileobj=raw, mtime=0) as z:
                            shutil.copyfileobj(f, z)
                if f is None:  # not a regular file when opened: nothing to copy
                    os.unlink(tmp)
                    out = None
                else:
                    os.replace(tmp, out)
            except BaseException:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp)
                raise
    prune(data, cfg, now or cards.now_utc())
    return out


def prune(data, cfg: config.Config, now: datetime) -> list:
    """Apply `keep_transcripts` to every archive: `never` removes them all, `<N>d` removes those older than N days
    (by file time), `always` keeps all. The summary files next to them are never touched. Returns the removed."""
    keep = cfg.keep_transcripts
    if keep == "always":
        return []
    cutoff = None if keep == "never" else (now - timedelta(days=int(keep[:-1]))).timestamp()
    removed = []
    for p in sorted((Path(data) / SESSIONS).glob(f"*/*{SUFFIX}")):
        if cutoff is None or p.stat().st_mtime < cutoff:
            p.unlink()
            removed.append(p)
    return removed


def remove_original(cfg: config.Config, claude: str, session_id: str) -> None:
    """`claude rm <session id>`: delete Claude Code's own copy. It deletes outside the data folder, so it needs
    live mode (raises ShadowModeError in shadow mode, before anything runs). Raises WorkerError, with claude's
    message, when `claude rm` fails."""
    config.require_live(cfg, f"delete the Claude Code session {session_id} (claude rm)")
    try:
        out = subprocess.run([claude, "rm", _check_sid(session_id)], capture_output=True, text=True, timeout=60,
                             stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        raise WorkerError(f"cannot delete the Claude Code session {session_id}: {e}") from e
    if out.returncode != 0:
        why = (out.stderr or out.stdout or "").strip()[-300:]
        raise WorkerError(f"cannot delete the Claude Code session {session_id}: claude rm {session_id} exited "
                          f"{out.returncode}: {why}")
