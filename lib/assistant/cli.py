"""ap2 command line. Run `ap2` with no arguments for the command list."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from assistant import config, datadir, templates
from assistant.cards import LANES, CardError
from assistant.config import ConfigError
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
    _source_parser(sub)
    _gate_parser(sub)
    _template_parser(sub)
    _serve_parser(sub)
    return p


class _OutputError(Exception):
    """Writing the output failed for a reason other than a broken pipe (e.g. a full disk)."""


def _write(text: str, stream) -> None:
    """Write `text` to `stream` and flush it now, so a write error shows up here and not in the flush at exit.
    A closed stream (None) gets nothing. When the write fails, the stream is pointed at devnull first (when devnull
    can be opened), so the flush at exit does not fail again."""
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
    nothing when the stream is not a real file. Never raises: when devnull cannot be opened or moved onto the fd
    (e.g. EMFILE, too many open files), the stream is left as it is and the caller still raises the write's own
    error. The devnull fd it opens is closed again, unless it landed on the stream's own fd."""
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):  # an in-memory stream (io.UnsupportedOperation)
        return
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        return
    try:
        os.dup2(devnull, fd)
    except OSError:
        pass
    finally:
        if devnull != fd:  # cannot happen after a fresh os.open while fd is open, but never close the stream's fd
            try:
                os.close(devnull)
            except OSError:
                pass


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


# -- ap2 source (the source engine, design 5) ---------------------------------------------------------------------

def _source_parser(sub) -> None:
    src = sub.add_parser("source", help="list, show or read sources",
                         description="List, show or read the source files in the plugin's sources/ and the data "
                                     "folder's sources/ (the user's file wins on the same name).")
    src_sub = src.add_subparsers(dest="source_command", metavar="<source command>", title="source commands")
    src_sub.required = True
    li = src_sub.add_parser("list", help="print every source as a JSON list",
                            description="Print every source (name, brings, via, fill, every, origin, file) as a "
                                        "JSON list, by name.")
    li.add_argument("--data", metavar="PATH", help=DATA_HELP)
    sh = src_sub.add_parser("show", help="print one source, merged, as JSON",
                            description="Print the source NAME as the engine reads it: extends merged, checked.")
    sh.add_argument("name", metavar="NAME")
    sh.add_argument("--data", metavar="PATH", help=DATA_HELP)
    rd = src_sub.add_parser("read", help="read a source and make or update its cards",
                            description="Read the source NAME and print what each item became. A via: command "
                                        "source runs its read.run; any other source needs --raw FILE, the output "
                                        "a scan session saved. The raw output is saved under "
                                        "sources/.raw/NAME/ first.")
    rd.add_argument("name", metavar="NAME")
    rd.add_argument("--raw", metavar="FILE", help="the saved output of the read, instead of running read.run")
    rd.add_argument("--records", metavar="FILE",
                    help="fill: ai only: the records a scan session filled, as a JSON list")
    rd.add_argument("--dry-run", action="store_true",
                    help="read and show what each item would become; save nothing")
    rd.add_argument("--data", metavar="PATH", help=DATA_HELP)


def cmd_source(args) -> int:
    from assistant import engine as engine_module
    from assistant.engine import Engine
    from assistant.sources import Loader, SourceError, read_capped

    path = datadir.resolve(args.data)
    dry_run = getattr(args, "dry_run", False)
    if args.source_command == "read" and args.raw is None and args.records is not None:
        # First, so the refused command reads nothing (a --records /dev/stdin would wait) and creates nothing.
        raise SourceError("--records needs --raw FILE, the output the records were filled from")
    if not dry_run:  # a dry run saves nothing, the data folder's layout included
        datadir.ensure(path)
    if args.source_command == "list":
        out = [s.summary() for s in Loader(path).load_all()]
    elif args.source_command == "show":
        out = Loader(path).load(args.name).data
    else:
        engine = Engine(path)
        records = None
        if args.records is not None:
            try:
                # Read with a byte limit, as a --raw file is: json.loads of a huge file takes many times its size.
                cap = engine_module.MAX_OUTPUT
                held = read_capped(args.records, cap, f"the records file {args.records!r} is larger than {cap} bytes")
                records = json.loads(held.decode("utf-8"))
            except (OSError, UnicodeDecodeError, ValueError, RecursionError) as e:
                why = "nested too deeply" if isinstance(e, RecursionError) else e
                raise SourceError(f"cannot read the records {args.records!r}: {why}") from e
        if args.records is not None and not isinstance(records, list):
            raise SourceError(f"the records file {args.records!r} must hold a JSON list")
        if args.raw is not None:
            out = engine.read_saved(args.name, args.raw, records=records, dry_run=dry_run)
        else:
            out = engine.read(args.name, dry_run=dry_run)
    _print_json(out)
    return 0


COMMANDS["source"] = cmd_source


# -- ap2 gate (slice 5) -------------------------------------------------------------------------------------------

def _gate_now(text: str):
    """--now: an ISO time with a zone (2026-11-10T09:00:00Z or ...-08:00), for trying a run at another time. It
    may not be later than the real time (plus the slot grace): a run writes last_slot, makes cards and prunes
    restore points, so a run in the future would hold back every real slot until then. A year before 1971 is
    refused too (the slot and prune arithmetic would go below year 1)."""
    from datetime import datetime, timezone
    from assistant import cards, routines
    try:
        at = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO time: {text!r} (for example 2026-11-10T09:00:00Z)") from None
    if at.tzinfo is None:
        raise argparse.ArgumentTypeError(f"{text!r} has no time zone; add Z or an offset such as -08:00")
    try:
        in_utc = at.astimezone(timezone.utc)
    except OverflowError:
        in_utc = None
    if in_utc is None or in_utc.year < 1971:
        raise argparse.ArgumentTypeError(f"{text!r} is out of range (years 1971 to now)")
    if in_utc > cards.now_utc() + routines.GRACE:
        raise argparse.ArgumentTypeError(f"{text!r} cannot be later than the real time: a run writes last_slot, "
                                         f"makes cards and prunes restore points")
    return at


def _gate_parser(sub) -> None:
    g = sub.add_parser("gate", help="run the gate once and print what it did",
                       description="One gate run, no AI: start the routine slots that are due (card first, then "
                                   "last_slot), list the watches that are due, prune restore points older than 30 "
                                   "days, and write gate.json and gate.log. Prints the run as JSON. While another "
                                   "run holds the gate lock, prints {\"locked\": true, ...} and exits 0.")
    g.add_argument("--now", type=_gate_now, metavar="ISO", help="run as if it were this time (default: now)")
    g.add_argument("--data", metavar="PATH", help=DATA_HELP)


def cmd_gate(args) -> int:
    from assistant import cards, gate
    data = datadir.ensure(datadir.resolve(args.data))
    _print_json(gate.run(data, args.now or cards.now_utc()))
    return 0


COMMANDS["gate"] = cmd_gate


# --- ap2 template (design 6) ---------------------------------------------------------------------------------
# Read-only: none of these creates the data folder or writes a file. A bad template is a TemplateError (a
# ConfigError), printed as one `ap2: <file>: line N: ...` line with exit 1.

def _template_parser(sub) -> None:
    t = sub.add_parser("template", help="list, show or check card templates",
                       description="List, show or check card templates: the built-in ones and the user's own in "
                                   "<data>/templates/, which win on the same kind.")
    t_sub = t.add_subparsers(dest="template_command", metavar="<template command>", title="template commands")
    t_sub.required = True
    li = t_sub.add_parser("list", help="print every template's kind and origin as a JSON list",
                          description="Print every template (built in, and the user's own) as a JSON list, by kind.")
    li.add_argument("--data", metavar="PATH", help=DATA_HELP)
    sh = t_sub.add_parser("show", help="print one template, with each role's tools",
                          description="Print the template in use for KIND as JSON: its file, what it shows, and "
                                      "each role with the tools and permission floor it keeps.")
    sh.add_argument("kind", metavar="KIND")
    sh.add_argument("--data", metavar="PATH", help=DATA_HELP)
    ch = t_sub.add_parser("check", help="validate a template file without installing it",
                          description="Validate the template in FILE (named <kind>.yaml) and say which template it "
                                      "would replace. Nothing is copied or installed.")
    ch.add_argument("file", metavar="FILE")
    ch.add_argument("--data", metavar="PATH", help=DATA_HELP)


def cmd_template(args) -> int:
    data = datadir.resolve(args.data)  # refused folders are refused here too; nothing is created
    if args.template_command == "list":
        out = [templates.summary(t) for t in templates.catalog(data).values()]
    elif args.template_command == "show":
        cat = templates.catalog(data)
        if args.kind not in cat:
            raise templates.TemplateError(f"no template for kind {args.kind!r} (kinds: {', '.join(cat)})")
        out = templates.describe(cat[args.kind])
    else:
        out = templates.check(args.file, data)
    _print_json(out)
    return 0


COMMANDS["template"] = cmd_template


# --- ap2 serve (the local page, design 9.1) --------------------------------------------------------------------
# Serves the page and its JSON on 127.0.0.1 only, until Ctrl-C or SIGTERM (exit 0). A port that cannot be used is
# a ConfigError: one `ap2: cannot listen on 127.0.0.1:<port>: ...` line, exit 1.

def _serve_port(text: str) -> int:
    import re
    if not re.fullmatch(r"[0-9]{1,5}", text, re.ASCII) or int(text) > 65535:
        raise argparse.ArgumentTypeError(f"not a port from 0 to 65535: {text!r}")
    return int(text)


def _serve_parser(sub) -> None:
    from assistant import server
    s = sub.add_parser("serve", help="serve the local page on 127.0.0.1",
                       description="Serve the local page and its JSON on 127.0.0.1 (this Mac only), until Ctrl-C or "
                                   "SIGTERM. Prints one JSON line first: url, data_dir and mode. Read-only, but for "
                                   "saving a question on a card. Sets up the data folder's layout on first use.")
    s.add_argument("--port", type=_serve_port, default=server.PORT, metavar="N",
                   help=f"the port (default: {server.PORT}; 0 takes a free one)")
    s.add_argument("--data", metavar="PATH", help=DATA_HELP)


class _ServeStop(Exception):
    """SIGTERM while serving."""


def cmd_serve(args) -> int:
    import signal
    from assistant import server
    data = datadir.ensure(datadir.resolve(args.data))
    cfg = config.load(data)  # a bad config.yaml is refused before listening
    srv = server.make_server(data, port=args.port)

    def stop(signum, frame):
        raise _ServeStop

    old = signal.signal(signal.SIGTERM, stop)
    try:
        _write_out(json.dumps({"url": srv.url, "data_dir": str(data), "mode": cfg.mode}, sort_keys=True,
                              ensure_ascii=False))
        srv.serve_forever()
    except (KeyboardInterrupt, _ServeStop):
        pass
    finally:
        signal.signal(signal.SIGTERM, old)
        srv.server_close()
    return 0


COMMANDS["serve"] = cmd_serve


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
    except (ConfigError, CardError) as e:
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
