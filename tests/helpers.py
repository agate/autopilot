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


class TempData:
    """Point AUTOPILOT_DATA at a fresh temp dir for the duration of a test."""

    def __enter__(self) -> Path:
        self.dir = Path(tempfile.mkdtemp(prefix="ap-test-"))
        self.old = os.environ.get("AUTOPILOT_DATA")
        os.environ["AUTOPILOT_DATA"] = str(self.dir)
        return self.dir

    def __exit__(self, *exc) -> None:
        if self.old is None:
            os.environ.pop("AUTOPILOT_DATA", None)
        else:
            os.environ["AUTOPILOT_DATA"] = self.old
        shutil.rmtree(self.dir, ignore_errors=True)


def write_ap(data: Path, name: str, **cfg) -> Path:
    """Create a minimal autopilot config under the temp data root."""
    import json

    d = data / "projects" / name
    d.mkdir(parents=True, exist_ok=True)
    base = {"name": name, "source": {"preset": "jira-epic", "key": "PROJ-100"}, "repo": str(data)}
    base.update(cfg)
    (d / "config.json").write_text(json.dumps(base))
    if not (d / "settings.json").exists():
        (d / "settings.json").write_text("{}")  # init writes the permission floor before any worker starts
    return d
