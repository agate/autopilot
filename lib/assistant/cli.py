"""ap2 command line. Run `ap2` with no arguments for the command list."""
from __future__ import annotations

import argparse
import json
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


def cmd_config(args) -> int:
    data = datadir.ensure(datadir.resolve(args.data))
    out = config.load(data).as_dict()
    out["data_dir"] = str(data)
    text = json.dumps(out, sort_keys=True, indent=2, ensure_ascii=False)
    try:
        text.encode(sys.stdout.encoding or "utf-8")
    except UnicodeEncodeError:  # e.g. a Latin-1 terminal: \uXXXX escapes are still the same JSON
        text = json.dumps(out, sort_keys=True, indent=2, ensure_ascii=True)
    print(text)
    return 0


COMMANDS = {"config": cmd_config}


def main(argv: list) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return 2
    try:
        return COMMANDS[args.command](args)
    except ConfigError as e:
        print(f"ap2: {e}", file=sys.stderr)
        return 1
