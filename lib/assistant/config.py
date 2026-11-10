"""`<data>/config.yaml`: defaults, validation, and the shadow-mode check every write path calls.

The file lives in the data folder, so the folder is found first (see `datadir`) and there is no `data_dir` key.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from assistant import yamlish

FILE = "config.yaml"
KEYS = ("primary_language", "secondary_language", "mode", "keep_transcripts")
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

    def as_dict(self) -> dict:
        return asdict(self)


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
        if v is None and key == "secondary_language":
            continue
        if not (isinstance(v, str) and LANGUAGE.match(v)):
            fail(key, f"{key} must be a short language code such as en, zh or pt-BR, not {v!r}")
        values[key] = v
    cfg = Config(**values)
    if cfg.secondary_language is not None and cfg.secondary_language == cfg.primary_language:
        fail("secondary_language", f"secondary_language is the same as primary_language ({cfg.primary_language});"
                                   " leave it empty for one language")
    return cfg


def require_live(cfg: Config, what: str) -> None:
    """Return when `mode` is live; raise ShadowModeError naming `what` when it is shadow. Every write path
    outside the data folder (Jira, Slack, GitHub, a published page) calls this first."""
    if cfg.mode != "live":
        raise ShadowModeError(f"shadow mode: refusing to {what}. Set 'mode: live' in config.yaml to allow it.")
