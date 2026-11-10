"""ap2 command line. Run `ap2` with no arguments for the command list."""
from __future__ import annotations

import argparse
import json
import os
import sys

from assistant import config, datadir
from assistant.config import ConfigError


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ap2", description="Autopilot v2 (shadow mode by default).")
    sub = p.add_subparsers(dest="command", metavar="<command>", title="commands")
    c = sub.add_parser("config", help="print the effective config as JSON",
                       description="Print the effective config (defaults filled in) and the data folder as JSON. "
                                   "Creates the data folder's layout on first use.")
    c.add_argument("--data", metavar="PATH", help=f"data folder (default: ${datadir.ENV}, else ~/Documents/Assistant)")
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


def cmd_config(args) -> int:
    data = datadir.ensure(datadir.resolve(args.data))
    out = config.load(data).as_dict()
    out["data_dir"] = str(data)
    text = json.dumps(out, sort_keys=True, indent=2, ensure_ascii=False)
    try:
        text.encode(getattr(sys.stdout, "encoding", None) or "utf-8")  # sys.stdout is None when stdout is closed
    except UnicodeEncodeError:  # e.g. a Latin-1 terminal: \uXXXX escapes are still the same JSON
        text = json.dumps(out, sort_keys=True, indent=2, ensure_ascii=True)
    _write_out(text)
    return 0


COMMANDS = {"config": cmd_config}


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
    except ConfigError as e:
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
