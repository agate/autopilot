"""Per-project setup carried over from v1 (`lib/autopilot/config.py` DEFAULT_AP): one file per project,
`<data>/projects/<name>.yaml`, read with the YAML subset. A card names its project in its `project` field.

Keys (every one optional):

- `repo`: the project's checkout.
- `branch_pattern` (default `{prefix}.{task}.{short}`) and `branch_prefix`: the branch a card's work goes on.
- `resources`: the single-use resources this project's cards may `need` (for example `native-dev-loop`).
- `health_checks`: `[{name, command, fix?}]`, checks of the project's own access (v1 `access.py`).
- `extra_hosts`: host names a worker may reach besides the usual ones.
- `mcp_servers`: MCP server names the project's workers use.
- `tools`: the MCP tool names a session in the repo has; the outward ones are denied in the worker's permission
  floor. When none are listed, each worker start finds them the way v1's `ap perms write` does
  (`perms.discover_tools`, in `workers.py`).

Nothing here writes; a missing file is the defaults.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

from assistant import yamlish
from assistant.config import ConfigError

FOLDER = "projects"
NAME = re.compile(r"[a-z][a-z0-9-]{0,30}\Z")  # v1's autopilot name rule (`lib/autopilot/paths.py`)
DEFAULTS = {
    "repo": None,
    "branch_pattern": "{prefix}.{task}.{short}",
    "branch_prefix": None,
    "resources": [],
    "health_checks": [],
    "extra_hosts": [],
    "mcp_servers": [],
    "tools": [],
}
LISTS = ("resources", "extra_hosts", "mcp_servers", "tools")


def path(data, name: str) -> Path:
    if not (isinstance(name, str) and NAME.match(name)):
        raise ConfigError(f"not a project name: {name!r} (lowercase letters, digits and -, starting with a letter)")
    return Path(data) / FOLDER / f"{name}.yaml"


def load(data, name: str) -> dict:
    """The project's setup with defaults filled in, plus `name`. Raises ConfigError naming the file and line."""
    p = path(data, name)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"{p}: cannot read: {e}") from e
    try:
        given, lines = yamlish.parse(text)
    except yamlish.YamlError as e:
        raise ConfigError(f"{p}: {e}") from e

    def fail(key, reason):
        raise ConfigError(f"{p}: line {lines[(key,)]}: {reason}")

    out = copy.deepcopy(DEFAULTS)
    for key, value in given.items():
        if key not in DEFAULTS:
            fail(key, f"unknown key '{key}' (known keys: {', '.join(DEFAULTS)})")
        if value is None:
            continue
        if key in LISTS and not (isinstance(value, list) and all(isinstance(v, str) and v for v in value)):
            fail(key, f"{key} must be a list of names, not {value!r}")
        if key in ("repo", "branch_pattern", "branch_prefix") and not (isinstance(value, str) and value):
            fail(key, f"{key} must be a text, not {value!r}")
        if key == "health_checks" and not (isinstance(value, list) and all(
                isinstance(c, dict) and isinstance(c.get("name"), str) and isinstance(c.get("command"), str)
                for c in value)):
            fail(key, "health_checks must be a list of {name, command, fix} maps")
        out[key] = value
    out["name"] = name
    return out


def branch(project: dict, task: str, short: str) -> str:
    """The branch for a card's work, from `branch_pattern` (v1's `{prefix}.{task}.{short}`). With no
    `branch_prefix`, the prefix and its dot are left out."""
    pattern = project.get("branch_pattern") or DEFAULTS["branch_pattern"]
    prefix = project.get("branch_prefix")
    if not prefix:
        pattern = pattern.replace("{prefix}.", "").replace("{prefix}", "")
    try:
        return pattern.format(prefix=prefix or "", task=task, short=short)
    except (KeyError, IndexError, ValueError) as e:
        raise ConfigError(f"branch_pattern {pattern!r}: {e}") from e
