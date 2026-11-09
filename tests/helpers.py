from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))


def quiet_usage(path: Path) -> Path:
    """A Claude Code usage cache with every window at 0% and no credits spent, fetched just now."""
    import json
    import time

    path.write_text(json.dumps({"cachedUsageUtilization": {
        "fetchedAtMs": int(time.time() * 1000),
        "utilization": {"limits": [{"kind": "session", "percent": 0, "resets_at": "2099-01-01T00:00:00+00:00"}],
                        "extra_usage": {"used_credits": 0, "monthly_limit": 20000, "decimal_places": 2,
                                        "spend_limit_reached": False}}}}))
    return path


class TempData:
    """Point AUTOPILOT_DATA at a fresh temp dir for the duration of a test, and the usage budget at a quiet usage
    file of its own: this machine's real ~/.claude.json must never decide whether a test's dispatcher starts work."""

    def __enter__(self) -> Path:
        from autopilot import budget

        self.dir = Path(tempfile.mkdtemp(prefix="ap-test-"))
        self.usage_dir = Path(tempfile.mkdtemp(prefix="ap-usage-"))
        self.old = os.environ.get("AUTOPILOT_DATA")
        os.environ["AUTOPILOT_DATA"] = str(self.dir)
        self.old_usage = budget.DEFAULT_PATH
        budget.DEFAULT_PATH = quiet_usage(self.usage_dir / "claude.json")
        return self.dir

    def __exit__(self, *exc) -> None:
        from autopilot import budget

        budget.DEFAULT_PATH = self.old_usage
        if self.old is None:
            os.environ.pop("AUTOPILOT_DATA", None)
        else:
            os.environ["AUTOPILOT_DATA"] = self.old
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(self.usage_dir, ignore_errors=True)


def write_ap(data: Path, name: str, **cfg) -> Path:
    """Create a minimal autopilot config under the temp data root."""
    import json

    d = data / name
    d.mkdir(parents=True, exist_ok=True)
    base = {"name": name, "source": {"preset": "jira-epic", "key": "PROJ-100"}, "repo": str(data)}
    base.update(cfg)
    (d / "config.json").write_text(json.dumps(base))
    if not (d / "settings.json").exists():
        (d / "settings.json").write_text("{}")  # init writes the permission floor before any worker starts
    return d
