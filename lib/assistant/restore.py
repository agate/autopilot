"""Restore points and proposed opposite actions (design 3.6).

A restore point is one JSON file, `<data>/restore/<point id>.json`:
`{"id": "r-...", "at": "<UTC time>", "kind": "card", "card_id": "c-...", "card": {...the card before the change}}`.
Only `kind: card` exists so far; later slices add files, branches and settings under the same layout.

Changes outside the Mac cannot be undone here. They get a proposed opposite action: an `approve` ask on the card
that says what would run. Nothing in this module runs it.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from assistant.cards import CardError, new_id, parse_stamp, stamp

FOLDER = "restore"
KEEP = timedelta(days=30)
POINT_ID = re.compile(r"r-\d{8}-\d{6}-[0-9a-f]{6}\Z", re.ASCII)  # ASCII digits only
# The opposite of each outside change (design 3.6): a moved ticket, an opened PR, a pushed branch, a merge.
OPPOSITE_ACTIONS = ("move_ticket_back", "close_pr", "force_push_saved_ref", "revert_pr")


def check_point_id(point_id) -> str:
    if not (isinstance(point_id, str) and POINT_ID.match(point_id)):
        raise CardError(f"not a restore point id: {point_id!r} (expected r-YYYYMMDD-HHMMSS-xxxxxx)")
    return point_id


def path(data: Path, point_id: str) -> Path:
    return Path(data) / FOLDER / f"{check_point_id(point_id)}.json"


def save(data: Path, card: dict, at: datetime, write) -> str:
    """Save `card` (its file as it is now) as a new restore point; return the point id. `write(path, obj)` is the
    store's atomic JSON writer."""
    point_id = new_id("r", at)
    while path(data, point_id).exists():  # two points in the same second with the same 6 random digits
        point_id = new_id("r", at)
    write(path(data, point_id), {"id": point_id, "at": stamp(at), "kind": "card", "card_id": card["id"],
                                 "card": card})
    return point_id


def load(data: Path, point_id: str) -> dict:
    p = path(data, point_id)
    try:
        point = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CardError(f"no restore point {point_id} (points are kept {KEEP.days} days)") from None
    except (OSError, ValueError) as e:
        raise CardError(f"{p}: cannot read the restore point: {e}") from e
    if not (isinstance(point, dict) and point.get("kind") == "card" and isinstance(point.get("card"), dict)):
        raise CardError(f"{p}: not a card restore point")
    if point.get("id") != point_id or point.get("card_id") != point["card"].get("id"):
        raise CardError(f"{p}: not a card restore point (its id must be its file name and its card_id its "
                        f"card's id)")
    return point


def prune(data: Path, now: datetime) -> list:
    """Remove the points older than 30 days at `now`; return their ids. A point whose time cannot be read is
    kept (it is never silently lost)."""
    removed = []
    folder = Path(data) / FOLDER
    if not folder.is_dir():
        return removed
    for p in sorted(folder.glob("r-*.json")):
        if not POINT_ID.match(p.stem):
            continue
        try:
            at = parse_stamp(json.loads(p.read_text(encoding="utf-8"))["at"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if now - at > KEEP:
            p.unlink(missing_ok=True)
            removed.append(p.stem)
    return removed


def opposite_ask(action: str, run, text=None) -> dict:
    """The `approve` ask that proposes `action`, the opposite of an outside change. `run` is the command it would
    run, as a list of arguments; it is shown to the user and never run here."""
    if action not in OPPOSITE_ACTIONS:
        raise CardError(f"action must be one of {', '.join(OPPOSITE_ACTIONS)}, not {action!r}")
    if not (isinstance(run, list) and run and all(isinstance(a, str) and a for a in run)):
        raise CardError(f"run must be a non-empty list of arguments, not {run!r}")
    ask = {"type": "approve", "action": action, "run": list(run), "proposed": True}
    if text is not None:
        ask["text"] = text
    return ask
