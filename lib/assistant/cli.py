"""ap2 command line. Run `ap2` with no arguments for the command list."""
from __future__ import annotations

import argparse
import json
import os
import sys

from assistant import config, datadir
from assistant.config import ConfigError


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
    c.add_argument("--data", metavar="PATH", help=f"data folder (default: ${datadir.ENV}, else ~/Documents/Assistant)")
    return p


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
    try:
        args = parser.parse_args(argv)  # help and argument errors exit here (0 and 2), through _Parser
        if args.command is None:
            if sys.stderr is not None:
                parser.print_help(sys.stderr)
            return 2
        code = COMMANDS[args.command](args)
    except ConfigError as e:
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
