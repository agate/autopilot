from __future__ import annotations

import os
import re
from pathlib import Path

_NAME = re.compile(r"^[a-z][a-z0-9-]{0,30}$")


def package_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def data_root() -> Path:
    env = os.environ.get("AUTOPILOT_DATA")
    return Path(env) if env else Path.home() / ".claude" / "autopilot-data"


def projects_root() -> Path:
    return data_root() / "projects"


def ap_dir(name: str) -> Path:
    """One autopilot's folder. An autopilot made before projects/ existed stays at the top of the data
    root until `config.migrate_legacy` moves it. The new place counts only once it holds config.json, so a
    file written there early cannot hide the autopilot."""
    new = projects_root() / name
    old = data_root() / name
    if not (new / "config.json").exists() and (old / "config.json").exists():
        return old
    return new


# `projects` is the folder every autopilot lives in, so no autopilot may take that name.
RESERVED = {"projects"}


def valid_name(name: str) -> bool:
    return bool(_NAME.match(name or "")) and name not in RESERVED
