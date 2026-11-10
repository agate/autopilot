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
OPTIONAL = ("parent", "ask", "title")

# c-YYYYMMDD-HHMMSS-xxxxxx: the UTC creation time, then 6 random hex digits. The month is in the id, so a card's
# file is found without a scan, and nothing but these characters can reach a path.
# ASCII digits only: `\d` alone would also match other scripts' digits (Arabic-Indic, full-width).
CARD_ID = re.compile(r"c-(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})-[0-9a-f]{6}\Z", re.ASCII)
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class CardError(ValueError):
    """A card, a change or a card id is not usable. The message says which field and why."""


class PlainWordsError(CardError):
    """The plain-words block is missing a line or a language. The message names each, e.g. `plain.where.zh
    missing`."""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def stamp(at: datetime) -> str:
    """A UTC time as text, to the second: 2026-11-10T14:30:05Z."""
    return at.astimezone(timezone.utc).strftime(TIME_FORMAT)


def parse_stamp(text: str) -> datetime:
    return datetime.strptime(text, TIME_FORMAT).replace(tzinfo=timezone.utc)


def new_id(prefix: str, at: datetime) -> str:
    return f"{prefix}-{at.astimezone(timezone.utc):%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def check_id(card_id) -> str:
    """Return `card_id` when it is a well-formed card id; raise CardError otherwise."""
    if not (isinstance(card_id, str) and CARD_ID.match(card_id)):
        raise CardError(f"not a card id: {card_id!r} (expected c-YYYYMMDD-HHMMSS-xxxxxx)")
    return card_id


def month(card_id: str) -> str:
    """The items/ folder of a card: its creation month, yyyy-mm."""
    m = CARD_ID.match(check_id(card_id))
    return f"{m.group(1)}-{m.group(2)}"


def _enum(card: dict, key: str, allowed: tuple) -> None:
    if card[key] not in allowed:
        raise CardError(f"{key} must be one of {', '.join(allowed)}, not {card[key]!r}")


def _text(card: dict, key: str) -> None:
    if not (isinstance(card[key], str) and card[key].strip()):
        raise CardError(f"{key} must be a non-empty text, not {card[key]!r}")


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
        raise CardError(f"links must be a list of card ids, not {card['links']!r}")
    for link in card["links"]:
        if not (isinstance(link, str) and CARD_ID.match(link)):
            raise CardError(f"links: not a card id: {link!r}")
    parent = card.get("parent")
    if parent is not None and not (isinstance(parent, str) and CARD_ID.match(parent)):
        raise CardError(f"parent must be a card id or null, not {parent!r}")
    if card.get("title") is not None and not isinstance(card["title"], str):
        raise CardError(f"title must be a text or null, not {card['title']!r}")
    ask = card.get("ask")
    if ask is not None:
        if not isinstance(ask, dict):
            raise CardError(f"ask must be an object with a type, or null, not {ask!r}")
        if ask.get("type") not in ASK_TYPES:
            raise CardError(f"ask.type must be one of {', '.join(ASK_TYPES)}, not {ask.get('type')!r}")
    if not isinstance(card["plain"], dict):
        raise CardError(f"plain must be an object with what, where and do, not {card['plain']!r}")


def check_unicode(value, what: str = "text") -> None:
    """Refuse text that cannot be written as UTF-8: a lone surrogate, e.g. from command-line bytes that are not
    UTF-8 or a `\\ud800` escape in a hand-edited file. Refuse NaN and Infinity too: cards are standard JSON."""
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except UnicodeEncodeError as e:  # a ValueError too, so it is caught first
        raise CardError(f"{what} is not valid Unicode: {e.object[e.start:e.end]!r}") from None
    except ValueError:
        raise CardError(f"{what} is not valid JSON: NaN or Infinity") from None


def check_stored(card) -> None:
    """The least the store needs to use a card file it reads (or a card in a restore point): `audit` a list,
    `plain` an object, `ask` an object or null, and text that is valid Unicode. Enums and required fields are not
    checked here, so a hand-edited file can still be read, and any point the store saved can be put back."""
    if not isinstance(card, dict):
        raise CardError(f"a card is a JSON object, not {type(card).__name__}")
    if not isinstance(card.get("audit"), list):
        raise CardError("audit missing" if "audit" not in card else f"audit must be a list, not {card['audit']!r}")
    if not isinstance(card.get("plain"), dict):
        raise CardError("plain missing" if "plain" not in card else f"plain must be an object, not {card['plain']!r}")
    if card.get("ask") is not None and not isinstance(card["ask"], dict):
        raise CardError(f"ask must be an object or null, not {card['ask']!r}")
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


def languages(cfg) -> tuple:
    """The languages every card's plain words must be written in: the primary, then the secondary when set."""
    return tuple(lang for lang in (cfg.primary_language, cfg.secondary_language) if lang)


def check_plain(card: dict, langs: tuple) -> None:
    problems = plain_problems(card.get("plain"), langs)
    if problems:
        raise PlainWordsError("; ".join(problems))


def validate(card, langs: tuple) -> None:
    """The full check run on every add and update. Raises CardError (PlainWordsError for the plain words)."""
    check_unicode(card)
    if isinstance(card, dict) and not isinstance(card.get("plain"), dict):
        check_plain(card, langs)  # name the missing lines and languages, not just "plain missing"
    check_shape(card)
    check_plain(card, langs)


def search_text(card: dict) -> str:
    """What the search index holds for a card: its plain words in every language, title, kind and feed."""
    parts = []
    plain = card.get("plain")
    plain = plain if isinstance(plain, dict) else {}
    for line in PLAIN_LINES:
        texts = plain.get(line)
        if isinstance(texts, dict):
            parts.extend(str(texts[k]) for k in sorted(texts) if isinstance(texts[k], str))
    for key in ("title", "kind", "feed"):
        if isinstance(card.get(key), str):
            parts.append(card[key])
    return "\n".join(parts)

