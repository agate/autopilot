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
    if flag == "":
        raise ConfigError("--data is empty; give a folder path")
    raw = flag
    try:
        if raw is None:
            raw = os.environ.get(ENV) or str(default())
        path = Path(raw).expanduser().resolve()
        claude = (Path.home() / ".claude").resolve()
        # resolve() follows symlinks. The name check ignores letter case (on a case-insensitive file system
        # ~/.CLAUDE is ~/.claude, even before ~/.claude exists). The same-folder check then catches paths resolve()
        # does not fold (macOS firmlinks such as /System/Volumes/Data/..., bind mounts).
        parts, claude_parts = [p.casefold() for p in path.parts], [p.casefold() for p in claude.parts]
        inside = parts[:len(claude_parts)] == claude_parts or (
            claude.exists() and any(p.exists() and os.path.samefile(p, claude) for p in (path, *path.parents)))
    except (OSError, RuntimeError) as e:  # a folder we may not read, a name too long, no such user for ~name
        raise ConfigError(f"cannot use the data folder {raw or '~/Documents/Assistant'}: {e}") from e
    if inside:
        raise ConfigError(f"data folder {path} is inside {claude}; pick a folder outside ~/.claude "
                          "(for example ~/Documents/Assistant)")
    return path


def ensure(path: Path) -> Path:
    """Create the layout on first use. An existing config.yaml is never overwritten. Raises ConfigError."""
    path = Path(path)
    try:
        if path.exists() and not path.is_dir():
            raise ConfigError(f"data folder {path} exists and is not a folder")
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
