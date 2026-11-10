"""ap2 command line. Run `ap2` with no arguments for the command list."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from assistant import config, datadir
from assistant.cards import LANES, CardError
from assistant.config import ConfigError
from assistant.store import Store

DATA_HELP = f"data folder (default: ${datadir.ENV}, else ~/Documents/Assistant)"
BY_HELP = "who makes the change, for the audit list (default: ap2)"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ap2", description="Autopilot v2 (shadow mode by default).")
    sub = p.add_subparsers(dest="command", metavar="<command>", title="commands")
    c = sub.add_parser("config", help="print the effective config as JSON",
                       description="Print the effective config (defaults filled in) and the data folder as JSON. "
                                   "Creates the data folder's layout on first use.")
    c.add_argument("--data", metavar="PATH", help=DATA_HELP)

    card = sub.add_parser("card", help="add, show or list cards", description="Add, show or list cards.")
    card_sub = card.add_subparsers(dest="card_command", metavar="<card command>", title="card commands")
    card_sub.required = True
    a = card_sub.add_parser("add", help="add a card from a JSON file and print it",
                            description="Add the card in FILE (JSON; - reads stdin) and print it with its id. The "
                                        "store sets id, created_at, updated_at and audit.")
    a.add_argument("file", metavar="FILE")
    a.add_argument("--by", default="ap2", help=BY_HELP)
    a.add_argument("--data", metavar="PATH", help=DATA_HELP)
    s = card_sub.add_parser("show", help="print one card", description="Print one card as JSON.")
    s.add_argument("id", metavar="ID")
    s.add_argument("--data", metavar="PATH", help=DATA_HELP)
    li = card_sub.add_parser("list", help="print the cards as a JSON list",
                             description="Print every card, oldest first, as a JSON list.")
    li.add_argument("--lane", choices=LANES)
    li.add_argument("--kind")
    li.add_argument("--parent", metavar="ID")
    li.add_argument("--data", metavar="PATH", help=DATA_HELP)

    se = sub.add_parser("search", help="find cards by their words",
                        description="Print, as a JSON list, the cards whose plain words, title, kind or feed hold "
                                    "every word of TEXT (any letter case, part of a word is enough).")
    se.add_argument("text", nargs="+", metavar="TEXT")
    se.add_argument("--data", metavar="PATH", help=DATA_HELP)

    r = sub.add_parser("restore", help="put a card back as a restore point saved it",
                       description="Put back the card saved in restore point POINT and print it. The current card is "
                                   "saved as a new point first, so this can be undone too.")
    r.add_argument("point", metavar="POINT")
    r.add_argument("--by", default="ap2", help=BY_HELP)
    r.add_argument("--data", metavar="PATH", help=DATA_HELP)
    return p


class _OutputError(Exception):
    """Writing the output failed for a reason other than a broken pipe (e.g. a full disk)."""


def _write_out(text: str) -> None:
    """Print `text` and flush it now, so a write error shows up here and not in the flush at exit."""
    if sys.stdout is None:  # stdout is closed
        return
    try:
        print(text)
        sys.stdout.flush()
    except BrokenPipeError:
        raise
    except OSError as e:  # only the output's errors; the commands turn their own OSErrors into ConfigError
        raise _OutputError(e) from e


def _stdout_to_devnull() -> None:
    """Point stdout's file descriptor at devnull so the flush at exit cannot fail again (the recipe from the
    Python signal module docs). Does nothing when stdout is not a real file."""
    try:
        fd = sys.stdout.fileno()
    except (AttributeError, OSError, ValueError):  # None, or an in-memory stream (io.UnsupportedOperation)
        return
    os.dup2(os.open(os.devnull, os.O_WRONLY), fd)


def _print_json(out) -> None:
    text = json.dumps(out, sort_keys=True, indent=2, ensure_ascii=False)
    try:
        text.encode(getattr(sys.stdout, "encoding", None) or "utf-8")  # sys.stdout is None when stdout is closed
    except UnicodeEncodeError:  # e.g. a Latin-1 terminal: \uXXXX escapes are still the same JSON
        text = json.dumps(out, sort_keys=True, indent=2, ensure_ascii=True)
    _write_out(text)


def cmd_config(args) -> int:
    data = datadir.ensure(datadir.resolve(args.data))
    out = config.load(data).as_dict()
    out["data_dir"] = str(data)
    _print_json(out)
    return 0


def _store(args) -> Store:
    return Store(datadir.ensure(datadir.resolve(args.data)))


def _read_card_file(name: str):
    if name == "-" and sys.stdin is None:
        raise CardError("cannot read -: stdin is closed")
    try:
        text = sys.stdin.read() if name == "-" else Path(name).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise CardError(f"cannot read {name}: {e}") from e
    try:
        return json.loads(text)
    except ValueError as e:
        raise CardError(f"{name}: not JSON: {e}") from e


def cmd_card(args) -> int:
    if args.card_command == "add":
        card = _read_card_file(args.file)  # read before the data folder is touched
        out = _store(args).add(card, by=args.by)
    elif args.card_command == "show":
        out = _store(args).get(args.id)
    else:
        out = _store(args).list(lane=args.lane, kind=args.kind, parent=args.parent)
    _print_json(out)
    return 0


def cmd_search(args) -> int:
    _print_json(_store(args).search(" ".join(args.text)))
    return 0


def cmd_restore(args) -> int:
    _print_json(_store(args).restore(args.point, by=args.by))
    return 0


COMMANDS = {"config": cmd_config, "card": cmd_card, "search": cmd_search, "restore": cmd_restore}


def main(argv: list) -> int:
    # sys.stderr is None when stderr is closed; print(file=None) would then write to stdout, so say nothing.
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command is None:
        if sys.stderr is not None:
            parser.print_help(sys.stderr)
        return 2
    try:
        code = COMMANDS[args.command](args)
    except (ConfigError, CardError) as e:
        if sys.stderr is not None:
            print(f"ap2: {e}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        # The reader went away (`ap2 config | true`); the data folder is still set up.
        _stdout_to_devnull()
        return 1
    except _OutputError as e:
        # e.g. `ap2 config > /dev/full` (disk full); the data folder is still set up.
        if sys.stderr is not None:
            print(f"ap2: cannot write the output: {e}", file=sys.stderr)
        _stdout_to_devnull()
        return 1
    return code
