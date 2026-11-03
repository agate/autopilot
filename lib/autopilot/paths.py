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


def ap_dir(name: str) -> Path:
    return data_root() / name


def valid_name(name: str) -> bool:
    return bool(_NAME.match(name or ""))
