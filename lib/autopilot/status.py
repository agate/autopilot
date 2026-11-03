from __future__ import annotations

import sys
from datetime import datetime, timezone

from autopilot import budget, cli, config, events, paths, state, text, workers


def cap_warning(gcfg: dict):
    active = len(config.active_aps())
    cap = gcfg["max_workers"]
    if cap >= active:
        return None
    return text.labels()["cap_warning"].format(active=active, cap=cap, waiting=active - cap)


def render(aps: list) -> str:
    gcfg = config.load_global()
    lines = []
    for n in aps:
        try:
            st = state.load(n)
        except state.StateError as e:
            lines.append(f"{n} · broken state: {e}")
            continue
        waiting = sum(1 for d in st["decisions"] if d["status"] == "open" and d["kind"] == "ask")
        done = sum(1 for t in st["tasks"] if t["status"] == "done")
        lines.append(f"{n} · {len(st['workers'])} running · {waiting} waiting on you · "
                     f"{done}/{len(st['tasks'])} done · {config.status_of(n)}")
    if not aps:
        lines.append("No autopilots yet. Start one with /autopilot init.")
    b = budget.read()
    a = budget.allowance(b, gcfg, datetime.now(timezone.utc))
    lines.append("Budget: " + ", ".join(f"{w['kind']} {w['percent']}%" for w in b.get("windows") or [])
                 + f" · new workers: {'no limit' if a['allow'] is None else a['allow']} ({a['reason']})")
    warn = cap_warning(gcfg)
    if warn:
        lines.append("Warning: " + warn)
    return "\n".join(lines)


@cli.command("status")
def _status(args: list) -> int:
    print(render(args or config.list_aps()))
    return 0


@cli.command("pause")
def _pause(args: list) -> int:
    (paths.ap_dir(args[0]) / "paused").write_text("")
    events.append(args[0], "paused", {})
    return 0


@cli.command("resume")
def _resume(args: list) -> int:
    try:
        (paths.ap_dir(args[0]) / "paused").unlink()
    except FileNotFoundError:
        pass
    events.append(args[0], "resumed", {})
    return 0


@cli.command("stop")
def _stop(args: list) -> int:
    name = args[0]
    st = state.load(name)
    if "--yes" not in args:
        print(f"Would stop {name}: end {len(st['workers'])} worker(s) and mark it stopped. Re-run with --yes.",
              file=sys.stderr)
        return 1
    for w in list(st["workers"]):
        workers.end(name, w["task"], w["role"])
    cfg = config.load_ap(name)
    cfg["stopped"] = True
    config.save_ap(name, cfg)
    events.append(name, "stopped", {})
    return 0


@cli.command("config")
def _config(args: list) -> int:
    """Read or set top-level global settings: `ap config get [key]`, `ap config set key=value ...`."""
    import json
    cfg = config.load_global()
    if args[:1] == ["get"]:
        print(json.dumps(cfg.get(args[1]) if len(args) > 1 else cfg, indent=1, ensure_ascii=False))
        return 0
    if args[:1] == ["set"] and len(args) > 1:
        for pair in args[1:]:
            k, _, v = pair.partition("=")
            if k not in config.DEFAULT_GLOBAL:
                print(f"unknown setting {k!r}", file=sys.stderr)
                return 2
            try:
                cfg[k] = json.loads(v)
            except ValueError:
                cfg[k] = v
        config.save_global(cfg)
        return 0
    print("usage: ap config get [key] | ap config set key=value ...", file=sys.stderr)
    return 2
