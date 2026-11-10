"""ap2 command line. Run `ap2` with no arguments for the command list."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from assistant import config, datadir
from assistant.cards import LANES, CardError
from assistant.config import ConfigError, ShadowModeError
from assistant.resources import WorkerError
from assistant.store import Store

DATA_HELP = f"data folder (default: ${datadir.ENV}, else ~/Documents/Assistant)"
BY_HELP = "who makes the change, for the audit list (default: ap2)"


class _Parser(argparse.ArgumentParser):
    """argparse's parser, with its help, usage and error text written the way `ap2 config` writes its output."""

    def _print_message(self, message, file=None):
        # argparse sends help, usage and error text here. `file` is None when that stream is closed; argparse
        # would then fall back to stderr (help meant for stdout), so say nothing instead.
        if not message or file is None:
            return
        try:
            _write(message, file)
        except BrokenPipeError:
            pass  # the reader went away; argparse goes on to its own exit code (0 for help, 2 for an error)

    def error(self, message):
        if sys.stderr is None:  # stderr is closed; print_usage(None) would print the usage to stdout
            self.exit(2)
        super().error(message)


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="ap2", description="Autopilot v2 (shadow mode by default).")
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

    _worker_parser(sub)
    return p


# -- ap2 worker (design 8.1, 8.2) ------------------------------------------------------------------------------------

TRANSCRIPTS_HELP = "folder Claude Code keeps conversations in, read only (default: ~/.claude/projects)"
USAGE_HELP = "Claude Code's usage cache (default: ~/.claude.json)"


def _worker_parser(sub) -> None:
    w = sub.add_parser("worker", help="start, check, end or list workers",
                       description="Workers are `claude --bg` sessions, one per card and role. Starting or resuming "
                                   "one needs live mode; listing, checking, ending and archiving work in shadow mode.")
    ws = w.add_subparsers(dest="worker_command", metavar="<worker command>", title="worker commands")
    ws.required = True

    def add(name, help_, description, card=True, role=True, transcripts=False):
        c = ws.add_parser(name, help=help_, description=description)
        if card:
            c.add_argument("card", metavar="CARD")
        if role:
            c.add_argument("role", metavar="ROLE", choices=_roles())
        if transcripts:
            c.add_argument("--transcripts", metavar="PATH", help=TRANSCRIPTS_HELP)
        c.add_argument("--by", default="ap2 worker", help=BY_HELP.replace("ap2)", "ap2 worker)"))
        c.add_argument("--data", metavar="PATH", help=DATA_HELP)
        return c

    add("list", "print every worker and its state", "Print every worker on record, with its state, as a JSON list.",
        card=False, role=False)
    s = add("start", "start a worker on a card",
            "Start a `claude --bg` worker for ROLE on CARD in --cwd, and print its record. Live mode only.")
    s.add_argument("--cwd", metavar="PATH", help="the folder the session runs in (a worktree)")
    s.add_argument("--prompt-file", metavar="FILE", help="the brief, as a text file")
    s.add_argument("--needs", default=None, metavar="A,B", help="single-use resources the work needs")
    s.add_argument("--resume", action="store_true", help="continue the saved (or parked) session")
    add("end", "stop a worker and keep its session on the card",
        "Stop the worker, save its session id on the card and archive its transcript.", transcripts=True)
    c = add("check", "look at every worker and act on its state",
            "Classify every worker and act: end the done, ask for questions, resume the dead once, pause the idle "
            "and the blocked. Prints what it did as a JSON list.", card=False, role=False, transcripts=True)
    c.add_argument("--usage", metavar="PATH", help="resume parked workers whose usage window has reset; " + USAGE_HELP)
    pk = add("park", "park a worker at the usage line", "A worker parks itself while usage is at the park line; it "
             "keeps its slot and ports.")
    pk.add_argument("--note", default="", help="one line: where the worker is and the next step")
    pk.add_argument("--usage", metavar="PATH", help=USAGE_HELP)
    a = add("answer", "answer a worker's question and resume it",
            "Resume the worker that asked CARD's question, with TEXT in its prompt. Live mode only.", role=False)
    a.add_argument("text", nargs="+", metavar="TEXT")
    add("finish", "archive a done card's sessions",
        "Make the last gzipped copy of each of CARD's sessions; in live mode also delete the originals "
        "(`claude rm`).", role=False, transcripts=True)


def _roles():
    from assistant.workers import ROLES
    return ROLES


def cmd_worker(args) -> int:
    from assistant.workers import Workers

    data = datadir.ensure(datadir.resolve(args.data))
    w = Workers(data, transcripts_dir=getattr(args, "transcripts", None))
    sub = args.worker_command
    if sub == "list":
        out = w.list()
    elif sub == "start":
        prompt = ""
        if args.prompt_file:
            try:
                prompt = Path(args.prompt_file).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                raise WorkerError(f"cannot read {args.prompt_file!r}: {e}") from e
        needs = None if args.needs is None else [n for n in args.needs.split(",") if n]
        out = w.start(args.card, args.role, cwd=args.cwd, prompt=prompt, needs=needs, resume=args.resume, by=args.by)
    elif sub == "end":
        out = w.end(args.card, args.role, by=args.by)
    elif sub == "check":
        out = w.check(by=args.by, usage_file=args.usage)
    elif sub == "park":
        out = w.park(args.card, args.role, args.note, usage_file=args.usage)
    elif sub == "answer":
        out = w.answer(args.card, " ".join(args.text), by=args.by)
    else:
        out = w.finish(args.card, by=args.by)
    _print_json(out)
    return 0


class _OutputError(Exception):
    """Writing the output failed for a reason other than a broken pipe (e.g. a full disk)."""


def _write(text: str, stream) -> None:
    """Write `text` to `stream` and flush it now, so a write error shows up here and not in the flush at exit.
    A closed stream (None) gets nothing. When the write fails, the stream is pointed at devnull first, so the
    flush at exit cannot fail again."""
    if stream is None:
        return
    try:
        stream.write(text)
        stream.flush()
    except OSError as e:  # only the output's errors; the commands turn their own OSErrors into ConfigError
        _to_devnull(stream)
        if isinstance(e, BrokenPipeError):
            raise
        raise _OutputError(e) from e


def _write_out(text: str) -> None:
    """Print `text` (and a newline) to stdout; see `_write`."""
    _write(text + "\n", sys.stdout)


def _to_devnull(stream) -> None:
    """Point the stream's file descriptor at devnull (the recipe from the Python signal module docs). Does
    nothing when the stream is not a real file."""
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):  # an in-memory stream (io.UnsupportedOperation)
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
    # !r keeps a newline in the name from splitting the one-line error
    try:
        if name != "-":
            text = Path(name).read_text(encoding="utf-8")
        elif getattr(sys.stdin, "buffer", None) is not None:
            text = sys.stdin.buffer.read().decode("utf-8")  # UTF-8 like a file, whatever the locale says
        else:
            text = sys.stdin.read()
    except (OSError, UnicodeDecodeError) as e:
        raise CardError(f"cannot read {name!r}: {e}") from e
    try:
        return json.loads(text)
    except ValueError as e:
        raise CardError(f"{name!r}: not JSON: {e}") from e


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
COMMANDS["worker"] = cmd_worker


def main(argv: list) -> int:
    # sys.stderr is None when stderr is closed; print(file=None) would then write to stdout, so say nothing.
    parser = _parser()
    try:
        args = parser.parse_args(argv)  # help and argument errors exit here (0 and 2), through _Parser
        if args.command is None:
            if sys.stderr is not None:
                parser.print_help(sys.stderr)
            return 2
        code = COMMANDS[args.command](args)
    except (ConfigError, CardError, WorkerError, ShadowModeError) as e:
        try:
            _write(f"ap2: {e}\n", sys.stderr)  # a closed stderr (None) gets nothing
        except (BrokenPipeError, _OutputError):
            pass  # stderr's reader is gone or its write failed; the line is lost, ap2 still exits 1
        return 1
    except BrokenPipeError:
        # The reader went away (`ap2 config | true`); the data folder is still set up.
        return 1
    except _OutputError as e:
        # e.g. `ap2 config > /dev/full` (disk full); the data folder is still set up. If stderr is the stream that
        # failed (`ap2 2> /dev/full`), this line cannot be shown either.
        try:
            _write(f"ap2: cannot write the output: {e}\n", sys.stderr)
        except (OSError, _OutputError):
            pass
        return 1
    return code
