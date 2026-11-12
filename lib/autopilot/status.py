from __future__ import annotations

import sys
from datetime import datetime, timezone

from autopilot import budget, cap, cli, config, events, paths, state, text, workers


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
    cur = budget.current(gcfg, datetime.now(timezone.utc))
    b, a, wa = cur["budget"], cur["passes"], cur["workers"]
    lines.append("Budget: " + ", ".join(f"{w['kind']} {w['percent']}%" for w in b.get("windows") or [])
                 + f" · new passes: {'no limit' if a['allow'] is None else a['allow']} ({a['reason']})"
                 + f" · new workers: {'no limit' if wa['allow'] is None else wa['allow']} ({wa['reason']})")
    if cur["spent"] is not None:
        lines.append(f"Gateway spend today: ${cur['spent']['today']:.2f} of ${cap.today_limit(cur['profile'], datetime.now()):.2f}"
                     + (" (some cost unknown)" if cur["spent"]["unknown"] else ""))
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
    """Global settings: `ap config list [--json]`, `get [key]`, `set key=value ...`, `reset key ...`.
    Keys may be dotted (`reply_check.every_s`); values are JSON, or plain text when not valid JSON."""
    import json
    known = lambda k: config.is_setting(k) or k in config.DEFAULT_GLOBAL
    if args[:1] == ["list"]:
        rows = config.rows()
        if "--json" in args:
            print(json.dumps(rows, indent=1, ensure_ascii=False))
            return 0
        dump = lambda v: json.dumps(v, ensure_ascii=False)
        w = max(len(r["key"]) for r in rows)
        for r in rows:
            print(f"{'*' if r['changed'] else ' '} {r['key']:<{w}}  {dump(r['value'])}"
                  + (f"  (default {dump(r['default'])})" if r["changed"] else "") + f"  — {r['description']}")
        return 0
    if args[:1] == ["get"]:
        cfg = config.load_global()
        try:
            value = config.lookup(cfg, args[1]) if len(args) > 1 else cfg
        except (KeyError, TypeError):
            print(f"unknown setting {args[1]!r}", file=sys.stderr)
            return 2
        print(json.dumps(value, indent=1, ensure_ascii=False))
        return 0
    if args[:1] in (["set"], ["reset"]) and len(args) > 1:
        pairs = [a.partition("=") for a in args[1:]]
        bad = [k for k, _, _ in pairs if not known(k)]
        if bad:
            print(f"unknown setting {bad[0]!r}; `ap config list` shows them all", file=sys.stderr)
            return 2
        if any(k == "active_profile" for k, _, _ in pairs):
            # The settings files must change with it, which only `ap profile use` does.
            print("active_profile: use `ap profile use <name>` (it rewrites the settings files too)", file=sys.stderr)
            return 2
        for k, _, v in pairs:
            if args[0] == "reset":
                config.reset_setting(k)
                continue
            try:
                value = json.loads(v)
            except ValueError:
                value = v
            config.set_setting(k, value)
        return 0
    print("usage: ap config list [--json] | get [key] | set key=value ... | reset key ...", file=sys.stderr)
    return 2
