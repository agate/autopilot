"""The card's shape (design 4.1) and its checks, including the plain-words block (design 3.7).

A card is a JSON object. The store sets `id`, `created_at`, `updated_at` and `audit`; the writer gives the rest.
Keys not named here are kept as they are, so later templates can add their own fields.
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timezone

LANES = ("needs", "running", "planned", "done")
PRIS = ("P0", "P1", "P2", "P3")
ASK_TYPES = ("confirm", "approve", "answer", "fix")
ORIGINS = ("source", "found")
PLAIN_LINES = ("what", "where", "do")
# Set by the store on every add, update and restore; a writer may not give or change them.
STORE_KEYS = ("id", "created_at", "updated_at", "audit")
REQUIRED = ("kind", "lane", "feed", "links", "pri", "origin", "plain")
OPTIONAL = ("parent", "ask", "title")  # title: null, a text, or {<lang>: text} (design 3.1)

# c-YYYYMMDD-HHMMSS-xxxxxx: the UTC creation time, then 6 random hex digits. The month is in the id, so a card's
# file is found without a scan, and nothing but these characters can reach a path.
# ASCII digits only: `\d` alone would also match other scripts' digits (Arabic-Indic, full-width).
CARD_ID = re.compile(r"c-(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})-[0-9a-f]{6}\Z", re.ASCII)
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# An error shows at most this many characters of a bad value's repr, then `...`: a hand-edited card can hold a huge
# value, and its whole repr would make one error line (in gate.json, the routine error, the terminal) that size.
SHOWN_CHARS = 200


class CardError(ValueError):
    """A card, a change or a card id is not usable. The message says which field and why."""


class PlainWordsError(CardError):
    """The plain-words block is missing a line or a language. The message names each, e.g. `plain.where.zh
    missing`."""


class TooDeepError(CardError, RecursionError):
    """A file or value is nested deeper than Python's JSON reader or writer can follow. A CardError, so every
    caller that handles a damaged file handles this one; also a RecursionError, so a caller that already says
    `nested too deeply` for a RecursionError (the raw prune) keeps its words."""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def stamp(at: datetime) -> str:
    """A UTC time as text, to the second: 2026-11-10T14:30:05Z."""
    return at.astimezone(timezone.utc).strftime(TIME_FORMAT)


def parse_stamp(text: str) -> datetime:
    return datetime.strptime(text, TIME_FORMAT).replace(tzinfo=timezone.utc)


def new_id(prefix: str, at: datetime) -> str:
    return f"{prefix}-{at.astimezone(timezone.utc):%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def shown(value) -> str:
    """A value as an error shows it: its repr, cut to its first SHOWN_CHARS characters then `...` when longer.
    Raises RecursionError, as repr does, when the value is nested too deeply to print."""
    text = repr(value)
    return text if len(text) <= SHOWN_CHARS else text[:SHOWN_CHARS] + "..."


def _not(field: str, what: str, value) -> str:
    """`<field> must be <what>, not <value>`, the value cut by `shown`; a TooDeepError when the value is nested
    too deeply to print."""
    try:
        return f"{field} must be {what}, not {shown(value)}"
    except RecursionError:
        raise TooDeepError(f"{field} must be {what}, and is nested too deeply") from None


def _not_an_id(head: str, value, tail: str = "") -> str:
    """`<head>not a card id: <value><tail>`, the value cut by `shown`; a TooDeepError when it is too deep."""
    try:
        return f"{head}not a card id: {shown(value)}{tail}"
    except RecursionError:
        raise TooDeepError(f"{head}not a card id: nested too deeply{tail}") from None


def check_id(card_id) -> str:
    """Return `card_id` when it is a well-formed card id; raise CardError otherwise."""
    if not (isinstance(card_id, str) and CARD_ID.match(card_id)):
        raise CardError(_not_an_id("", card_id, " (expected c-YYYYMMDD-HHMMSS-xxxxxx)"))
    return card_id


def month(card_id: str) -> str:
    """The items/ folder of a card: its creation month, yyyy-mm."""
    m = CARD_ID.match(check_id(card_id))
    return f"{m.group(1)}-{m.group(2)}"


def _enum(card: dict, key: str, allowed: tuple) -> None:
    if card[key] not in allowed:
        raise CardError(_not(key, f"one of {', '.join(allowed)}", card[key]))


def _text(card: dict, key: str) -> None:
    if not (isinstance(card[key], str) and card[key].strip()):
        raise CardError(_not(key, "a non-empty text", card[key]))


def check_shape(card) -> None:
    """Every rule of design 4.1 except the plain-words languages. Raises CardError."""
    if not isinstance(card, dict):
        raise CardError(f"a card is a JSON object, not {type(card).__name__}")
    for key in REQUIRED:
        if key not in card:
            raise CardError(f"{key} missing")
    _text(card, "kind")
    _text(card, "feed")
    _enum(card, "lane", LANES)
    _enum(card, "pri", PRIS)
    _enum(card, "origin", ORIGINS)
    if not isinstance(card["links"], list):
        raise CardError(_not("links", "a list of card ids", card["links"]))
    for link in card["links"]:
        if not (isinstance(link, str) and CARD_ID.match(link)):
            raise CardError(_not_an_id("links: ", link))
    parent = card.get("parent")
    if parent is not None and not (isinstance(parent, str) and CARD_ID.match(parent)):
        raise CardError(_not("parent", "a card id or null", parent))
    _title_shape(card.get("title"))
    ask = card.get("ask")
    if ask is not None:
        if not isinstance(ask, dict):
            raise CardError(_not("ask", "an object with a type, or null", ask))
        if ask.get("type") not in ASK_TYPES:
            raise CardError(_not("ask.type", f"one of {', '.join(ASK_TYPES)}", ask.get("type")))
    if not isinstance(card["plain"], dict):
        raise CardError(_not("plain", "an object with what, where and do", card["plain"]))


def _title_shape(title) -> None:
    """A title is null, one text (the same words in every language: what every card stored before titles had one
    slot per language holds, and what a quote is) or an object of one text per language (design 3.1)."""
    if title is None or isinstance(title, str):
        return
    if not (isinstance(title, dict) and title and all(isinstance(k, str) for k in title)):
        raise CardError(_not("title", "a text, an object of one text per language, or null", title))
    for lang in sorted(title):
        if not isinstance(title[lang], str):
            raise CardError(_not(f"title.{lang}", "a text", title[lang]))


def check_unicode(value, what: str = "text") -> None:
    """Refuse text that cannot be written as UTF-8: a lone surrogate, e.g. from command-line bytes that are not
    UTF-8 or a `\\ud800` escape in a hand-edited file. Refuse NaN and Infinity too: cards are standard JSON. And
    refuse what JSON cannot write with sorted keys (mixed key types, sets), as every card file is written."""
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True).encode("utf-8")
    except UnicodeEncodeError as e:  # a ValueError too, so it is caught first
        raise CardError(f"{what} is not valid Unicode: {shown(e.object[e.start:e.end])}") from None
    except ValueError:
        raise CardError(f"{what} is not valid JSON: NaN or Infinity") from None
    except TypeError as e:
        raise CardError(f"{what} is not JSON: {e}") from None
    except RecursionError:
        raise TooDeepError(f"{what} is not valid JSON: nested too deeply") from None


def check_stored(card) -> None:
    """The least the store needs to use a card file it reads (or a card in a restore point): `audit` a list,
    `plain` an object, `ask` an object or null, and text that is valid Unicode. Enums and required fields are not
    checked here, so a hand-edited file can still be read, and any point the store saved can be put back."""
    if not isinstance(card, dict):
        raise CardError(f"a card is a JSON object, not {type(card).__name__}")
    if not isinstance(card.get("audit"), list):
        raise CardError("audit missing" if "audit" not in card else _not("audit", "a list", card["audit"]))
    if not isinstance(card.get("plain"), dict):
        raise CardError("plain missing" if "plain" not in card else _not("plain", "an object", card["plain"]))
    if card.get("ask") is not None and not isinstance(card["ask"], dict):
        raise CardError(_not("ask", "an object or null", card["ask"]))
    check_unicode(card)


def plain_problems(plain, languages: tuple) -> list:
    """Each missing or empty plain-words line, as `plain.<line>.<language> missing|is empty`."""
    problems = []
    plain = plain if isinstance(plain, dict) else {}
    for line in PLAIN_LINES:
        texts = plain.get(line)
        texts = texts if isinstance(texts, dict) else {}
        for lang in languages:
            if lang not in texts or texts[lang] is None:
                problems.append(f"plain.{line}.{lang} missing")
            elif not (isinstance(texts[lang], str) and texts[lang].strip()):
                problems.append(f"plain.{line}.{lang} is empty")
    return problems


def title_problems(title, languages: tuple) -> list:
    """Each language an object title is missing or has empty, as `title.<language> missing|is empty`. A text title
    (or null) has none: one text is the same words in every language."""
    if not isinstance(title, dict):
        return []
    problems = []
    for lang in languages:
        if lang not in title or title[lang] is None:
            problems.append(f"title.{lang} missing")
        elif not (isinstance(title[lang], str) and title[lang].strip()):
            problems.append(f"title.{lang} is empty")
    return problems


def title_for(card, lang: str, primary: str):
    """The card's title in `lang`: an object title's `lang` slot, else its `primary` slot, else its first non-blank
    slot in sorted key order (a stored title's words beat "Untitled", say after the primary language changed),
    else None. A text title is the same words in every language, so it is returned as it is; null and anything
    else give None."""
    title = card.get("title") if isinstance(card, dict) else None
    if isinstance(title, str):
        return title
    if isinstance(title, dict):
        for key in (lang, primary, *sorted(k for k in title if isinstance(k, str))):
            if isinstance(title.get(key), str) and title[key].strip():
                return title[key]
    return None


def languages(cfg) -> tuple:
    """The languages every card's plain words must be written in: the primary, then the secondary when set."""
    return tuple(lang for lang in (cfg.primary_language, cfg.secondary_language) if lang)


def check_plain(card: dict, langs: tuple) -> None:
    problems = plain_problems(card.get("plain"), langs)
    if problems:
        raise PlainWordsError("; ".join(problems))


def check_title(card: dict, langs: tuple) -> None:
    problems = title_problems(card.get("title"), langs)
    if problems:
        raise CardError("; ".join(problems))


def validate(card, langs: tuple) -> None:
    """The full check run on every add and update. Raises CardError (PlainWordsError for the plain words)."""
    check_unicode(card)
    if isinstance(card, dict) and not isinstance(card.get("plain"), dict):
        check_plain(card, langs)  # name the missing lines and languages, not just "plain missing"
    check_shape(card)
    check_plain(card, langs)
    check_title(card, langs)


def search_text(card: dict) -> str:
    """What the search index holds for a card: its plain words and title in every language, kind and feed."""
    parts = []
    plain = card.get("plain")
    plain = plain if isinstance(plain, dict) else {}
    for line in PLAIN_LINES:
        texts = plain.get(line)
        if isinstance(texts, dict):
            parts.extend(str(texts[k]) for k in sorted(texts) if isinstance(texts[k], str))
    title = card.get("title")
    if isinstance(title, dict):
        parts.extend(title[k] for k in sorted(k for k in title if isinstance(k, str)) if isinstance(title[k], str))
    elif isinstance(title, str):
        parts.append(title)
    for key in ("kind", "feed"):
        if isinstance(card.get(key), str):
            parts.append(card[key])
    return "\n".join(parts)

