from __future__ import annotations

import json
from functools import lru_cache

from autopilot import paths


def required_langs(cfg: dict) -> set:
    lang = (cfg or {}).get("reader_lang") or "en"
    return {"en"} if lang == "en" else {"en", lang}


@lru_cache(maxsize=1)
def labels() -> dict:
    return json.loads((paths.package_root() / "templates" / "labels.en.json").read_text())


def check(obj, langs: set, where: str) -> list:
    """Errors for one reader-facing text field: {en, <lang>} strings, or {key, args} label refs."""
    if not isinstance(obj, dict):
        return [f"{where}: text must be an object, got {type(obj).__name__}"]
    if "key" in obj:
        if obj["key"] not in labels():
            return [f"{where}: unknown label key {obj['key']!r}"]
        extra = set(obj) - {"key", "args"}
        return [f"{where}: unexpected fields {sorted(extra)}"] if extra else []
    errs = []
    for lang in sorted(langs):
        v = obj.get(lang)
        if not isinstance(v, str) or not v.strip():
            errs.append(f"{where}: missing {lang} text")
    return errs
