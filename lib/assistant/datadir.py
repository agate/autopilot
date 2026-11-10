"""Finding and laying out the data folder (design 3.4).

Lookup order: `--data <path>`, else the `AP2_DATA` environment variable, else `~/Documents/Assistant`.
A folder inside `~/.claude` is refused.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from assistant.config import DEFAULT_TEXT, FILE, ConfigError

ENV = "AP2_DATA"
# The search index (index.sqlite) is not made here: the card store builds it.
FOLDERS = ("items", "chat", "sessions", "templates", "sources", "routines", "restore", "attachments")


def default() -> Path:
    return Path.home() / "Documents" / "Assistant"


def resolve(flag: Optional[str] = None) -> Path:
    """The data folder as an absolute path, symlinks followed. Nothing is created. Raises ConfigError."""
    raw = flag or os.environ.get(ENV) or str(default())
    path = Path(raw).expanduser().resolve()
    claude = (Path.home() / ".claude").resolve()
    # The name check, then a same-folder check: on a case-insensitive file system ~/.CLAUDE is ~/.claude.
    if path == claude or claude in path.parents or (
            claude.exists() and any(p.exists() and os.path.samefile(p, claude) for p in (path, *path.parents))):
        raise ConfigError(f"data folder {path} is inside {claude}; pick a folder outside ~/.claude "
                          "(for example ~/Documents/Assistant)")
    return path


def ensure(path: Path) -> Path:
    """Create the layout on first use. An existing config.yaml is never overwritten. Raises ConfigError."""
    path = Path(path)
    if path.exists() and not path.is_dir():
        raise ConfigError(f"data folder {path} exists and is not a folder")
    try:
        for name in FOLDERS:
            (path / name).mkdir(parents=True, exist_ok=True)
        try:
            with open(path / FILE, "x", encoding="utf-8") as f:
                f.write(DEFAULT_TEXT)
        except FileExistsError:
            pass
    except OSError as e:
        raise ConfigError(f"cannot set up the data folder {path}: {e}") from e
    return path
