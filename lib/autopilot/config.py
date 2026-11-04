from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from autopilot import paths

DEFAULT_GLOBAL: dict = {
    "max_workers": 5,
    "budget": {"window_pct": 85, "stale_min": 60, "credit_pct": 90, "resume_after_min": 5},
    "forced_pass_min": 30,
    # The gate wakes every minute: its own checks use no tokens, and the reply check (Haiku, ~$0.0008 a run at
    # list price) answers within a minute outside the quiet hours. At night the forced pass picks answers up.
    "gate_interval_s": 60,
    "reply_check": {"every_s": 60, "quiet": ["22:00", "06:00"], "model": "haiku", "timeout_s": 120},
    "report_times": ["09:00", "17:00", "21:00"],
    "port_base": 7100,
    "archive_after_days": 7,
    "permission_mode": "auto",
    "page_host": "claude.ai",
    "models": {"dispatcher": None, "worker": None},
    "worker_effort": "xhigh",
    "dispatch_timeout_s": 1800,
    "worktree_root": "~/worktrees",
    "reader_lang": "en",
    "tools": {"claude": None, "gh": None, "git": None, "python3": None},
}

DEFAULT_AP: dict = {
    "branch_pattern": "{prefix}.{task}.{short}",
    "branch_prefix": None,
    "resources": [],
    "authority": {},
    "report_times": None,
    "watches": [],
    "extra_hosts": [],
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


def save_global(cfg: dict) -> None:
    _write(paths.data_root() / "config.json", cfg)


def load_ap(name: str) -> dict:
    path = paths.ap_dir(name) / "config.json"
    if not path.exists():
        raise FileNotFoundError(f"no autopilot named {name!r}")
    cfg = _merge(DEFAULT_AP, _read(path))
    cfg["name"] = name
    return cfg


def save_ap(name: str, cfg: dict) -> None:
    _write(paths.ap_dir(name) / "config.json", cfg)


def list_aps() -> list:
    root = paths.data_root()
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if (p / "config.json").exists() and paths.valid_name(p.name))


def status_of(name: str) -> str:
    if load_ap(name).get("stopped"):
        return "stopped"
    if (paths.ap_dir(name) / "paused").exists():
        return "paused"
    return "active"


def active_aps() -> list:
    return [n for n in list_aps() if status_of(n) == "active"]
