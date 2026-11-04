from __future__ import annotations

import json

from autopilot import cli, config, state as state_mod


def ready(st: dict) -> list:
    """Tasks that can start now, in start order."""
    done = {t["id"] for t in st.get("tasks", []) if t.get("status") == "done"}
    busy = {w.get("task") for w in st.get("workers", [])}
    held = {d.get("task") for d in st.get("decisions", []) if d.get("status") == "open" and d.get("task")}
    out = [
        t for t in st.get("tasks", [])
        if t.get("status") == "todo"
        and all(d in done for d in t.get("depends_on") or [])
        and t["id"] not in busy
        and t["id"] not in held
    ]

    def key(t):
        phase = t.get("phase")
        return (0 if t.get("critical") else 1, 1 if phase is None else 0, phase or 0, t["id"])

    return sorted(out, key=key)


def plan_starts(snaps: list, live_workers: list, max_workers: int, allow) -> list:
    """Pick (autopilot, task) pairs to start, round-robin across autopilots."""
    free = max_workers - len(live_workers)
    if allow is not None:
        free = min(free, allow)
    if free <= 0:
        return []
    held = {r for w in live_workers for r in (w.get("needs") or [])}
    queues = [(name, list(ready(st))) for name, _cfg, st in snaps]
    picked = []
    while len(picked) < free and any(q for _, q in queues):
        for name, q in queues:
            if len(picked) >= free:
                break
            while q:
                task = q.pop(0)
                needs = set(task.get("needs") or [])
                if needs & held:
                    continue
                held |= needs
                picked.append((name, task["id"]))
                break
    return picked


def snapshots(names: list) -> list:
    out = []
    for n in names:
        try:
            out.append((n, config.load_ap(n), state_mod.load(n)))
        except (state_mod.StateError, FileNotFoundError):
            continue
    return out


@cli.command("next")
def _cmd(args: list) -> int:
    from datetime import datetime, timezone
    from autopilot import budget
    active = config.active_aps()
    names = [a for a in args if not a.startswith("--")] or active
    snaps = snapshots(sorted(set(active) | set(names)))
    g = config.load_global()
    live = [w for _, _, st in snapshots(config.list_aps()) for w in st.get("workers", [])]
    allow = budget.allowance(budget.read(), g, datetime.now(timezone.utc))["allow"]
    ready_map = {n: [t["id"] for t in ready(st)] for n, _, st in snaps if n in names}
    # Plan across every active autopilot so round-robin stays fair, then hand back only the named ones:
    # a pass runs in one repo and must not start another repo's tasks.
    starts = [s for s in plan_starts(snaps, live, g["max_workers"], allow) if s[0] in names]
    out = {
        "ready": ready_map,
        "ready_count": sum(len(v) for v in ready_map.values()),
        "free": g["max_workers"] - len(live),
        "allow": allow,
        "starts": starts,
    }
    print(json.dumps(out, indent=1) if "--json" in args else
          "\n".join(f"{n}: {', '.join(ids) or '-'}" for n, ids in out["ready"].items()))
    return 0
