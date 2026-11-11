"""`<data>/config.yaml`: defaults, validation, and the shadow-mode check every write path calls.

The file lives in the data folder, so the folder is found first (see `datadir`) and there is no `data_dir` key.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from assistant import yamlish
from assistant.cards import LANES, shown

FILE = "config.yaml"
KEYS = ("primary_language", "secondary_language", "mode", "keep_transcripts", "phone_history_days", "phone_lanes",
        "phone_hide_quotes")
# The phone page's settings (design 9.2). `as_dict()` leaves them out, so `ap2 config` and the local page's
# Settings show the same keys as before; the phone data names them in its own `settings`.
PHONE_KEYS = ("phone_history_days", "phone_lanes", "phone_hide_quotes")
HISTORY_MAX = 365
MODES = ("shadow", "live")
# A short language code: a 2-3 letter language, then an optional script (Hans) and/or region (BR, 419).
LANGUAGE = re.compile(r"[a-z]{2,3}(-[A-Z][a-z]{3})?(-([A-Z]{2}|[0-9]{3}))?\Z")
KEEP = re.compile(r"(always|never|[1-9][0-9]*d)\Z")

# Written on first use. Every key has a comment, and the file reads back as exactly the defaults.
DEFAULT_TEXT = """\
# Assistant settings. Every key is optional: a missing key takes the default shown here.

# The language Claude writes in, as a short code such as en, zh or pt-BR. Default: en.
primary_language: en

# Optional second language. When set, every user-facing output is written in both and the pages show a
# language switch. Leave it empty for one language.
secondary_language:

# shadow: read sources and make cards, but write nothing outside this folder (no Jira, Slack or GitHub
# writes). live: allow those writes. Default: shadow.
mode: shadow

# How long to keep the gzipped AI conversations in sessions/: always, never, or a number of days such as 90d.
keep_transcripts: always

# The phone page (published only in live mode) shows open cards always, and History (done cards) changed in the
# last this many days: a whole number from 0 (no History) to 365. Default: 3.
phone_history_days: 3

# The lanes the phone page shows, any of needs, running, planned and done, each at most once. Default: all four.
phone_lanes: [needs, running, planned, done]

# true: the phone page shows who wrote and where ("Ana wrote a message in #team") instead of what people wrote,
# in the list and in the full card. Default: false.
phone_hide_quotes: false
"""


class ConfigError(Exception):
    """config.yaml or the data folder is not usable. The message says where and why."""


class ShadowModeError(RuntimeError):
    """A write was asked for while `mode` is `shadow`."""


@dataclass(frozen=True)
class Config:
    primary_language: str = "en"
    secondary_language: Optional[str] = None
    mode: str = "shadow"
    keep_transcripts: str = "always"
    phone_history_days: int = 3
    phone_lanes: tuple = LANES
    phone_hide_quotes: bool = False

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k not in PHONE_KEYS}


def load(data_dir) -> Config:
    """Read `<data_dir>/config.yaml`; a missing file or key takes the default. Raises ConfigError."""
    path = Path(data_dir) / FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Config()
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"{path}: cannot read: {e}") from e
    return parse(text, str(path))


def parse(text: str, where: str = FILE) -> Config:
    try:
        data, lines = yamlish.parse(text)
    except yamlish.YamlError as e:
        raise ConfigError(f"{where}: {e}") from e

    def fail(key: str, reason: str):
        raise ConfigError(f"{where}: line {lines[(key,)]}: {reason}")

    for key in data:
        if key not in KEYS:
            fail(key, f"unknown key '{key}' (known keys: {', '.join(KEYS)})")
    values = {}
    if "mode" in data:
        if data["mode"] not in MODES:
            fail("mode", f"mode must be shadow or live, not {data['mode']!r}")
        values["mode"] = data["mode"]
    if "keep_transcripts" in data:
        v = data["keep_transcripts"]
        if not (isinstance(v, str) and KEEP.match(v)):
            fail("keep_transcripts", f"keep_transcripts must be always, never or a number of days such as 90d, "
                                     f"not {v!r}")
        values["keep_transcripts"] = v
    for key in ("primary_language", "secondary_language"):
        if key not in data:
            continue
        v = data[key]
        if (v is None or v == "") and key == "secondary_language":
            continue
        if not (isinstance(v, str) and LANGUAGE.match(v)):
            fail(key, f"{key} must be a short language code such as en, zh or pt-BR, not {v!r}")
        values[key] = v
    values.update(_phone(data, fail))
    cfg = Config(**values)
    if cfg.secondary_language is not None and cfg.secondary_language == cfg.primary_language:
        fail("secondary_language", f"secondary_language is the same as primary_language ({cfg.primary_language});"
                                   " leave it empty for one language")
    return cfg


def _phone(data: dict, fail) -> dict:
    """The phone page's three settings, checked; `fail(key, reason)` raises the ConfigError naming the line."""
    values = {}
    if "phone_history_days" in data:
        v = data["phone_history_days"]
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= HISTORY_MAX:
            fail("phone_history_days", f"phone_history_days must be a whole number from 0 to {HISTORY_MAX}, "
                                       f"not {shown(v)}")
        values["phone_history_days"] = v
    if "phone_lanes" in data:
        v = data["phone_lanes"]
        if not isinstance(v, list):
            fail("phone_lanes", f"phone_lanes must be a list of lanes such as [needs, running], not {shown(v)}")
        if not v:
            fail("phone_lanes", f"phone_lanes must name at least one lane ({', '.join(LANES)})")
        for i, lane in enumerate(v):
            if not (isinstance(lane, str) and lane in LANES):
                fail("phone_lanes", f"phone_lanes: unknown lane {shown(lane)} (lanes: {', '.join(LANES)})")
            if lane in v[:i]:
                fail("phone_lanes", f"phone_lanes names {lane!r} more than once")
        values["phone_lanes"] = tuple(v)
    if "phone_hide_quotes" in data:
        v = data["phone_hide_quotes"]
        if not isinstance(v, bool):
            fail("phone_hide_quotes", f"phone_hide_quotes must be true or false, not {shown(v)}")
        values["phone_hide_quotes"] = v
    return values


def require_live(cfg: Config, what: str) -> None:
    """Return when `mode` is live; raise ShadowModeError naming `what` when it is shadow. Every write path
    outside the data folder (Jira, Slack, GitHub, a published page other than v2's own) calls this first."""
    if cfg.mode != "live":
        raise ShadowModeError(f"shadow mode: refusing to {what}. Set 'mode: live' in config.yaml to allow it.")
