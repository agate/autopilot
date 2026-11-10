"""Finding and laying out the data folder (design 3.4).

Lookup order: `--data <path>`, else the `AP2_DATA` environment variable, else `~/Documents/Assistant`.
A folder inside `~/.claude` is refused, for the home in HOME and for the account's home in the user database.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from assistant.config import DEFAULT_TEXT, FILE, ConfigError

try:
    import pwd
except ImportError:  # not POSIX
    pwd = None

ENV = "AP2_DATA"
# The search index (index.sqlite) is not made here: the card store builds it.
FOLDERS = ("items", "chat", "sessions", "templates", "sources", "routines", "restore", "attachments")


def default() -> Path:
    return Path.home() / "Documents" / "Assistant"


def _homes() -> list:
    """The home folder from HOME, then the account's home from the user database (an empty or wrong HOME cannot
    hide ~/.claude from the check)."""
    homes = [Path.home()]
    if pwd is not None:
        try:
            pw_dir = pwd.getpwuid(os.getuid()).pw_dir
        except KeyError:  # no entry for this user id (some containers)
            pw_dir = ""
        if os.path.isabs(pw_dir):
            homes.append(Path(pw_dir))
    return homes


def _inside(path: Path, claude: Path) -> bool:
    # resolve() follows symlinks. The name check ignores letter case (on a case-insensitive file system
    # ~/.CLAUDE is ~/.claude, even before ~/.claude exists). The same-folder check then catches paths resolve()
    # does not fold (macOS firmlinks such as /System/Volumes/Data/..., bind mounts).
    parts, claude_parts = [p.casefold() for p in path.parts], [p.casefold() for p in claude.parts]
    return parts[:len(claude_parts)] == claude_parts or (
        claude.exists() and any(p.exists() and os.path.samefile(p, claude) for p in (path, *path.parents)))


def resolve(flag: Optional[str] = None) -> Path:
    """The data folder as an absolute path, symlinks followed. Nothing is created. Raises ConfigError."""
    if flag == "":
        raise ConfigError("--data is empty; give a folder path")
    raw = flag
    if raw is None:
        raw = os.environ.get(ENV)
        home = os.environ.get("HOME")
        if not raw and os.name == "posix" and home is not None and not os.path.isabs(home):
            raise ConfigError(f"HOME is empty or not an absolute path ({home!r}); set it or pass --data")
    try:
        raw = raw or str(default())
        path = Path(raw).expanduser().resolve()
        claude = next((c for c in ((h / ".claude").resolve() for h in _homes()) if _inside(path, c)), None)
    except (OSError, RuntimeError) as e:  # a folder we may not read, a name too long, no such user for ~name
        raise ConfigError(f"cannot use the data folder {raw or '~/Documents/Assistant'}: {e}") from e
    if claude is not None:
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
