from __future__ import annotations

import importlib
import sys
from typing import Callable, Dict

COMMANDS: Dict[str, Callable] = {}

# Modules that register commands. Missing modules are skipped so the CLI works while it grows.
_MODULES = [
    "autopilot.budget", "autopilot.nextwork", "autopilot.workers", "autopilot.ops",
    "autopilot.gate", "autopilot.dispatch", "autopilot.mergecheck", "autopilot.snapshot",
    "autopilot.guard", "autopilot.perms", "autopilot.publish", "autopilot.checkpkg",
    "autopilot.doctor", "autopilot.status", "autopilot.install", "autopilot.access", "autopilot.park",
    "autopilot.profile",
]


def command(name: str) -> Callable:
    def deco(fn: Callable) -> Callable:
        COMMANDS[name] = fn
        return fn
    return deco


def load_all() -> None:
    for mod in _MODULES:
        try:
            importlib.import_module(mod)
        except ModuleNotFoundError as e:
            if e.name != mod:
                raise


def usage() -> str:
    return "usage: ap <command> [args]\ncommands: " + ", ".join(sorted(COMMANDS))


# Hooks run on every message or tool call, so they load only their own module.
FAST = {"hook-prompt": "autopilot.snapshot", "hook-guard": "autopilot.guard",
        "hook-park": "autopilot.park"}


def main(argv: list) -> int:
    from autopilot import profile
    profile.migrate_from_config()
    if argv and argv[0] in FAST:
        importlib.import_module(FAST[argv[0]])
    else:
        load_all()
    if not argv or argv[0] not in COMMANDS:
        print(usage(), file=sys.stderr)
        return 2
    return int(COMMANDS[argv[0]](argv[1:]) or 0)
