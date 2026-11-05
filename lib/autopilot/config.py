from __future__ import annotations

import copy
import fcntl
import json
import os
from pathlib import Path

from autopilot import events, paths

DEFAULT_GLOBAL: dict = {
    "max_workers": 5,
    "budget": {"window_pct": 85, "stale_min": 60, "credit_pct": 90, "park_pct": 90, "resume_after_min": 5},
    "forced_pass_min": 30,
    # The gate wakes every minute: its own checks use no tokens, and the reply check (Haiku, ~$0.0008 a run at
    # list price) answers within a minute outside the quiet hours. At night the forced pass picks answers up.
    "gate_interval_s": 60,
    "reply_check": {"every_s": 60, "quiet": ["22:00", "06:00"], "model": "haiku", "timeout_s": 120},
    # Between passes the gate refreshes the page's light collections on its own (a Haiku session, a fraction of a
    # cent): soon after a real change, and at least this often so usage numbers and times stay current.
    "page_refresh_min": 30,
    "report_times": ["09:00", "17:00", "21:00"],
    "port_base": 7100,
    "archive_after_days": 7,
    # Decisions the autopilot made by itself and nobody has read yet: one line in the reports and on the page
    # once this many pile up, or once the oldest is this many days old. 0 turns that half off.
    "self_review": {"remind_at": 10, "remind_days": 3},
    "permission_mode": "auto",
    "page_host": "claude.ai",
    "models": {"dispatcher": None, "worker": None},
    "worker_effort": "xhigh",
    "dispatch_timeout_s": 1800,
    "worktree_root": "~/worktrees",
    "reader_lang": "en",
    "tools": {"claude": None, "gh": None, "git": None, "python3": None},
}

# One line per setting, shown by `ap config list` and in references/config.md (a test keeps both complete).
DESCRIBE: dict = {
    "max_workers": "Most workers running at once, across all autopilots.",
    "budget.window_pct": "Start no new work once a usage window reaches this percent.",
    "budget.stale_min": "Usage numbers older than this many minutes count as unknown: one new worker at a time.",
    "budget.credit_pct": "Start no new work once extra credits reach this percent of the monthly limit.",
    "budget.park_pct": "Running workers stop and wait once any usage window reaches this percent, and continue after it resets. Unknown or stale numbers never park.",
    "budget.resume_after_min": "Minutes to wait after a usage window resets before work starts again.",
    "forced_pass_min": "Run a dispatcher pass at least this often, in minutes, even when nothing changed.",
    "gate_interval_s": "How often the background gate wakes, in seconds. Takes effect after `ap install`.",
    "reply_check.every_s": "How often to read answers from the page, in seconds. 0 turns it off.",
    "reply_check.quiet": "No answer reads between these two times (this computer's clock); the forced pass picks answers up then.",
    "reply_check.model": "Model for the answer read.",
    "reply_check.timeout_s": "Give up on one answer read after this many seconds.",
    "page_refresh_min": "Refresh the page at least this often, in minutes, even when nothing changed, so usage numbers and times stay current. A real change refreshes it within a few minutes. 0 turns the periodic refresh off.",
    "report_times": "When reports are written, local time. An autopilot can set its own.",
    "port_base": "First port handed to workers; each worker gets a block of 10.",
    "archive_after_days": "Answered decisions older than this many days move to the archive.",
    "self_review.remind_at": "Remind once this many routine decisions the autopilot made by itself are unread: one line in the reports and on the page. 0 turns it off.",
    "self_review.remind_days": "Also remind once the oldest unread routine decision is this many days old. 0 turns it off.",
    "permission_mode": "Claude Code permission mode for the dispatcher and workers.",
    "page_host": "Where the page is published.",
    "models.dispatcher": "Model for dispatcher passes; null uses Claude Code's default.",
    "models.worker": "Model for workers; null uses Claude Code's default.",
    "worker_effort": "Effort level for workers.",
    "dispatch_timeout_s": "Stop a dispatcher pass that runs longer than this many seconds.",
    "worktree_root": "Folder where task worktrees are created.",
    "reader_lang": "The reader's language for the page and reports; set at the first init.",
    "tools.claude": "Path to claude that the gate uses; `ap install` records it.",
    "tools.gh": "Path to gh that the gate uses; `ap install` records it.",
    "tools.git": "Path to git that the gate uses; `ap install` records it.",
    "tools.python3": "Path to python3 that the gate uses; `ap install` records it.",
}

DEFAULT_AP: dict = {
    "branch_pattern": "{prefix}.{task}.{short}",
    "branch_prefix": None,
    "resources": [],
    "authority": {},
    "report_times": None,
    "watches": [],
    "extra_hosts": [],
    "mcp_servers": [],
    "health_checks": [],
    "merge_method": "squash",
    "stopped": False,
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _write(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def load_global() -> dict:
    return _merge(DEFAULT_GLOBAL, _read(paths.data_root() / "config.json"))


def _overrides(cfg: dict, base: dict) -> dict:
    """What differs from the defaults, nested dicts compared key by key."""
    out = {}
    for k, v in (cfg or {}).items():
        b = base.get(k) if isinstance(base, dict) else None
        if isinstance(v, dict) and isinstance(b, dict):
            sub = _overrides(v, b)
            if sub:
                out[k] = sub
        elif not isinstance(base, dict) or k not in base or v != b:
            out[k] = v
    return out


def save_global(cfg: dict) -> None:
    """Saves only what the user changed, so a new default in a later version reaches this install."""
    _write(paths.data_root() / "config.json", _overrides(cfg, DEFAULT_GLOBAL))


def _leaves(d: dict, prefix: str = "") -> list:
    out = []
    for k, v in d.items():
        out += _leaves(v, prefix + k + ".") if isinstance(v, dict) and v else [prefix + k]
    return out


def lookup(d: dict, key: str):
    for part in key.split("."):
        d = d[part]
    return d


def is_setting(key: str) -> bool:
    return key in DESCRIBE


def set_setting(key: str, value) -> None:
    cfg = load_global()
    *parents, last = key.split(".")
    node = cfg
    for p in parents:
        node = node[p]
    node[last] = value
    save_global(cfg)


def reset_setting(key: str) -> None:
    raw = _read(paths.data_root() / "config.json")
    *parents, last = key.split(".")
    node = raw
    for p in parents:
        node = node.get(p) if isinstance(node, dict) else None
    if isinstance(node, dict):
        node.pop(last, None)
    save_global(_merge(DEFAULT_GLOBAL, raw))


def rows() -> list:
    """Every setting with this install's value, the default, whether it differs, and what it does."""
    cfg = load_global()
    out = []
    for key in _leaves(DEFAULT_GLOBAL):
        value, default = lookup(cfg, key), lookup(DEFAULT_GLOBAL, key)
        out.append({"key": key, "value": value, "default": default, "changed": value != default,
                    "description": DESCRIBE.get(key, "")})
    return out


def markdown() -> str:
    """references/config.md, generated from the defaults and DESCRIBE."""
    lines = ["# Settings", "",
             "Every global setting, its default and what it does. `ap config list` shows this install's values",
             "next to the defaults and marks the ones you changed with `*`. `ap config set key=value` changes one",
             "(dotted keys for nested settings, values as JSON); `ap config reset key` goes back to the default.",
             "Only changed settings are saved, so a new default in a later version reaches you unless you changed",
             "that setting.", "",
             "| Setting | Default | What it does |", "|---|---|---|"]
    for key in _leaves(DEFAULT_GLOBAL):
        lines.append(f"| `{key}` | `{json.dumps(lookup(DEFAULT_GLOBAL, key), ensure_ascii=False)}` | {DESCRIBE[key]} |")
    return "\n".join(lines) + "\n"


def load_ap(name: str) -> dict:
    path = paths.ap_dir(name) / "config.json"
    if not path.exists():
        raise FileNotFoundError(f"no autopilot named {name!r}")
    cfg = _merge(DEFAULT_AP, _read(path))
    cfg["name"] = name
    return cfg


def save_ap(name: str, cfg: dict) -> None:
    _write(paths.ap_dir(name) / "config.json", cfg)


def _names_in(root: Path) -> list:
    if not root.exists():
        return []
    return [p.name for p in root.iterdir() if (p / "config.json").exists() and paths.valid_name(p.name)]


def list_aps() -> list:
    return sorted(set(_names_in(paths.projects_root())) | set(_names_in(paths.data_root())))


def _workers(folder: Path):
    """The workers recorded in a folder's state.json, or None when it cannot be read."""
    try:
        return json.loads((folder / "state.json").read_text()).get("workers") or []
    except FileNotFoundError:
        return []
    except (OSError, ValueError, AttributeError):
        return None


def _rebase(value, old: str, new: str):
    if isinstance(value, str):
        return new + value[len(old):] if value.startswith(old + "/") else value
    if isinstance(value, list):
        return [_rebase(v, old, new) for v in value]
    if isinstance(value, dict):
        return {k: _rebase(v, old, new) for k, v in value.items()}
    return value


def _move(name: str) -> bool:
    root, dest = paths.data_root(), paths.projects_root()
    old, new = root / name, dest / name
    if name != "projects" and new.exists():
        return False
    fd = os.open(str(old / "state.lock"), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if _workers(old) != []:
            return False
        if name == "projects":
            # Its folder is the one the others move into: step aside, then move in.
            tmp = root / ".projects-moving"
            os.rename(old, tmp)
            dest.mkdir()
            os.rename(tmp, new)
        else:
            dest.mkdir(exist_ok=True)
            os.rename(old, new)
        state_file = new / "state.json"
        if state_file.exists():
            _write(state_file, _rebase(json.loads(state_file.read_text()), str(old), str(new)))
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    events.append(name, "moved", {"from": str(old), "to": str(new)})
    return True


def migrate_legacy() -> list:
    """Move each autopilot made before projects/ existed into projects/<name>/, once none of its workers
    runs (a running worker holds paths into the old folder). Returns the names moved."""
    moved = []
    for name in sorted(_names_in(paths.data_root()), key=lambda n: (n != "projects", n)):
        try:
            if _move(name):
                moved.append(name)
        except OSError:
            continue  # left where it is; ap_dir still finds it there
    return moved


def status_of(name: str) -> str:
    if load_ap(name).get("stopped"):
        return "stopped"
    if (paths.ap_dir(name) / "paused").exists():
        return "paused"
    return "active"


def active_aps() -> list:
    return [n for n in list_aps() if status_of(n) == "active"]
