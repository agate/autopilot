"""Card templates (design 4.2, 6): loading, the user's override, validation, and filling briefs.

A template says what a card of one kind shows (`view`), its `buttons` and what each one does, and which `roles`
do its work, each with a prose `brief`. Built-in templates live in `builtin/templates/<kind>.yaml` next to this
file (the design's `<plugin>`); the user's live in `<data>/templates/<kind>.yaml` and win on the same kind. Files
are read with `yamlish`.

A template can combine roles and write briefs. It cannot add tools or permissions: a key that would widen a role
(`tools`, `permissions`, `allowed_tools`, `settings`, ...) anywhere in the file is refused with
`TemplateWidenError`, and a role's tools always come from `roles.ROLES`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from assistant import roles, yamlish
from assistant.cards import LANES
from assistant.config import ConfigError

BUILTIN_DIR = Path(__file__).resolve().parent / "builtin" / "templates"
FOLDER = "templates"  # in the data folder
SUFFIX = ".yaml"

KEYS = ("kind", "title", "description", "source", "view", "buttons", "roles")
REQUIRED = ("kind", "view", "buttons", "roles")
SOURCES = ("any", "none")
KIND = re.compile(r"[a-z][a-z0-9-]{0,39}\Z")
BUTTON_ID = re.compile(r"[a-z][a-z0-9_-]{0,39}\Z")
BUTTON_KEYS = ("id", "label", "lane", "restore", "child")
# What a button does: a lane change, a restore point (put back the card as it was before its last change), or a
# child card of a kind.
ACTIONS = ("lane", "restore", "child")
RESTORE = ("last",)
ROLE_KEYS = ("role", "when", "brief")

# The blocks a card can show, in the order its `view` lists them. The plain-words block (design 3.7) is not one:
# it always comes first, above the view.
BLOCKS = {
    "source_block": "who, where, when, their words, and the Open-in-source button (5.4); only with source: any",
    "plan": "what the work will do, what it touches and how to undo it, to confirm",
    "draft": "a reply drafted for the user to send; never sent",
    "findings": "what the work found",
    "verdict": "a review's TL;DR verdict and its findings in four groups (6.2)",
    "analysis": "an alert's analysis: still firing, how big, what changed, what the playbook says",
    "report": "a research or routine run's report",
    "pr": "the pull request, its branch and CI",
    "merge_checks": "the checks a merge waits for",
    "children": "the child cards",
    "links": "the linked cards",
    "schedule": "a routine's schedule, next run and missed-run rule",
    "runs": "a routine's past runs",
    "fix_steps": "what the user must do to repair something (a login)",
    "questions": "the Ask-about-this-card box and its answers (9.4)",
    "activity": "the audit trail",
}

# Keys that would widen a role, compared without letter case, `-`, `_` or spaces (`allowedTools`,
# `allowed-tools` and `allowed_tools` are one key). Refused anywhere in a template.
_WIDEN = ("tools", "tool", "allowed_tools", "disallowed_tools", "permissions", "permission", "permission_mode",
          "settings", "setting_sources", "mcp", "mcp_servers", "mcp_config", "hooks", "env", "add_dir", "add_dirs",
          "dangerously_skip_permissions", "floor", "cwd", "folder", "sandbox", "agents")
_PLACEHOLDER = re.compile(r"\{\{|\}\}|\{([^{}]*)\}|[{}]")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*\Z")


def _norm(key: str) -> str:
    return re.sub(r"[-_ ]", "", key.casefold())


WIDEN = frozenset(_norm(k) for k in _WIDEN)


class TemplateError(ConfigError):
    """A template file is not usable. The message starts with the file, then `line N:` when a line is known."""


class TemplateWidenError(TemplateError):
    """A template holds a key that would give a role more tools or permissions than it ships with."""

    def __init__(self, file: str, key: str, line: Optional[int]):
        at = f"line {line}: " if line else ""
        super().__init__(f"{file}: {at}'{key}' is not allowed: a template cannot add tools or permissions; each "
                         "role keeps the tools and permission floor it ships with")
        self.file, self.key, self.line = file, key, line


class BriefError(TemplateError):
    """A brief names a card field the card does not have (or has empty). `missing` lists the field names."""

    def __init__(self, message: str, missing: list):
        super().__init__(message)
        self.missing = missing


@dataclass(frozen=True)
class Template:
    kind: str
    data: dict
    path: Path
    origin: str                    # "builtin" or "user"
    replaces_builtin: bool = False

    def runs(self, card: dict) -> list:
        """The template's roles for `card`, in order: each with its brief filled from the card, and the tools and
        floor the role ships with. Raises BriefError naming the template, the role and every missing field."""
        out = []
        for i, entry in enumerate(self.data["roles"]):
            role = roles.ROLES[entry["role"]]
            try:
                brief = fill_brief(entry["brief"], card)
            except BriefError as e:
                raise BriefError(f"template {self.kind!r}, role {role.name!r} (roles[{i}]): {e}", e.missing) from None
            run = {"role": role.name, "brief": brief, "tools": list(role.tools), "floor": role.floor}
            if "when" in entry:
                run["when"] = entry["when"]
            out.append(run)
        return out


# --- briefs ---------------------------------------------------------------------------------------------------

def placeholders(brief: str) -> list:
    """The field names a brief uses, in order, once each. Raises ValueError on a lone brace or a bad name."""
    names = []
    for m in _PLACEHOLDER.finditer(brief):
        tok = m.group(0)
        if tok in ("{{", "}}"):
            continue
        if m.group(1) is None:
            raise ValueError(f"a lone brace {tok!r} (write {tok * 2} for a brace)")
        if not _NAME.match(m.group(1)):
            raise ValueError(f"{tok} is not a card field name (letters, digits and _, parts joined by dots)")
        if m.group(1) not in names:
            names.append(m.group(1))
    return names


def _lookup(card, name: str):
    value = card
    for part in name.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def fill_brief(brief: str, card: dict) -> str:
    """Fill each `{field}` (or `{a.b}`, a field inside a field) from `card`; `{{` and `}}` are literal braces.
    A field the card lacks, or holds as null or blank text, is an error naming it: never an empty fill. A value
    must be text or a number."""
    try:
        names = placeholders(brief)
    except ValueError as e:
        raise BriefError(f"bad brief: {e}", []) from None
    missing, wrong, values = [], [], {}
    for name in names:
        v = _lookup(card, name)
        if v is None or (isinstance(v, str) and not v.strip()):
            missing.append(name)
        elif isinstance(v, bool) or not isinstance(v, (str, int, float)):
            wrong.append(name)
        else:
            values[name] = str(v)
    if missing or wrong:
        parts = []
        if missing:
            parts.append("the card has no " + ", ".join("{" + n + "}" for n in missing))
        if wrong:
            parts.append("not text or a number: " + ", ".join("{" + n + "}" for n in wrong))
        raise BriefError("brief needs " + "; ".join(parts), missing + wrong)

    def sub(m):
        tok = m.group(0)
        return tok[0] if tok in ("{{", "}}") else values[m.group(1)]
    return _PLACEHOLDER.sub(sub, brief)


# --- validation -----------------------------------------------------------------------------------------------

class _Checker:
    def __init__(self, where: str, lines: dict):
        self.where, self.lines = where, lines

    def line(self, path: tuple) -> Optional[int]:
        """The line of a key's path; for a list item, the first line of its keys."""
        if path in self.lines:
            return self.lines[path]
        found = [n for p, n in self.lines.items() if p[:len(path)] == path]
        return min(found) if found else None

    def fail(self, path: tuple, reason: str):
        n = self.line(path) if path else None
        raise TemplateError(f"{self.where}: line {n}: {reason}" if n else f"{self.where}: {reason}")

    def widen(self, value, path: tuple) -> None:
        """Refuse a widening key anywhere, before any other check, so it always gets its own error class."""
        if isinstance(value, dict):
            for k, v in value.items():
                if _norm(k) in WIDEN:
                    raise TemplateWidenError(self.where, k, self.line(path + (k,)))
                self.widen(v, path + (k,))
        elif isinstance(value, list):
            for i, v in enumerate(value):
                self.widen(v, path + (i,))

    def text(self, value, path: tuple, what: str) -> None:
        if not isinstance(value, str) or not value.strip():
            self.fail(path, f"{what} must be non-empty text, not {value!r}")


def parse(text: str, where: str, kinds=None) -> dict:
    """Validate one template's text; return its data. `where` names the file in errors; its name (without
    `.yaml`) must be the template's kind. `kinds`, when given, are the kinds a `child` button may name (the
    template's own kind is always one). Raises TemplateError (TemplateWidenError for a widening key)."""
    try:
        data, lines = yamlish.parse(text)
    except yamlish.YamlError as e:
        raise TemplateError(f"{where}: {e}") from e
    c = _Checker(where, lines)
    c.widen(data, ())
    for key in data:
        if key not in KEYS:
            c.fail((key,), f"unknown key '{key}' (known keys: {', '.join(KEYS)})")
    for key in REQUIRED:
        if key not in data:
            c.fail((), f"missing key '{key}'")

    kind = data["kind"]
    if not (isinstance(kind, str) and KIND.match(kind)):
        c.fail(("kind",), f"kind must be lowercase letters, digits and -, starting with a letter, not {kind!r}")
    stem = Path(where).name
    stem = stem[:-len(SUFFIX)] if stem.endswith(SUFFIX) else stem
    if kind != stem:
        c.fail(("kind",), f"kind '{kind}' does not match the file name '{Path(where).name}' (name it {kind}.yaml)")
    for key in ("title", "description"):
        if key in data:
            c.text(data[key], (key,), key)
    source = data.get("source", "none")
    if source not in SOURCES:
        c.fail(("source",), f"source must be any or none, not {source!r}")

    view = data["view"]
    if not isinstance(view, list) or not view:
        c.fail(("view",), "view must be a non-empty list of blocks")
    seen = set()
    for block in view:
        if not isinstance(block, str):
            c.fail(("view",), f"view entries must be block names, not {block!r}")
        if block not in BLOCKS:
            c.fail(("view",), f"unknown block '{block}' in view (known blocks: {', '.join(BLOCKS)})")
        if block in seen:
            c.fail(("view",), f"view lists '{block}' twice")
        seen.add(block)
    if source == "any" and view[0] != "source_block":
        c.fail(("view",), "a template with source: any shows source_block first in its view")
    if source != "any" and "source_block" in view:
        c.fail(("view",), "source_block needs source: any (it is drawn from the source's record)")

    buttons = data["buttons"]
    if not isinstance(buttons, list):
        c.fail(("buttons",), "buttons must be a list")
    ids = set()
    own = {kind} | set(kinds or ())
    for i, b in enumerate(buttons):
        at = ("buttons", i)
        if not isinstance(b, dict):
            c.fail(("buttons",), f"buttons[{i}] must be a map with id, label and one action")
        for key in b:
            if key not in BUTTON_KEYS:
                c.fail(at + (key,), f"unknown key '{key}' in buttons[{i}] (known keys: {', '.join(BUTTON_KEYS)})")
        for key in ("id", "label"):
            if key not in b:
                c.fail(at, f"buttons[{i}] has no {key}")
        if not (isinstance(b["id"], str) and BUTTON_ID.match(b["id"])):
            c.fail(at + ("id",), f"button id must be lowercase letters, digits, - and _, not {b['id']!r}")
        if b["id"] in ids:
            c.fail(at + ("id",), f"button id '{b['id']}' twice")
        ids.add(b["id"])
        c.text(b["label"], at + ("label",), "label")
        actions = [a for a in ACTIONS if a in b]
        if not actions:
            c.fail(at, f"button '{b['id']}' has no action (give one of: {', '.join(ACTIONS)})")
        if len(actions) > 1:
            c.fail(at, f"button '{b['id']}' has more than one action ({', '.join(actions)}); give one")
        if "lane" in b and b["lane"] not in LANES:
            c.fail(at + ("lane",), f"lane must be one of {', '.join(LANES)}, not {b['lane']!r}")
        if "restore" in b and b["restore"] not in RESTORE:
            c.fail(at + ("restore",), f"restore must be 'last' (the card as it was before its last change), "
                                      f"not {b['restore']!r}")
        if "child" in b:
            child = b["child"]
            if not (isinstance(child, str) and KIND.match(child)) or (kinds is not None and child not in own):
                c.fail(at + ("child",), f"child names no template kind: {child!r}")

    entries = data["roles"]
    if not isinstance(entries, list):
        c.fail(("roles",), "roles must be a list of maps with role, an optional when, and brief")
    for i, r in enumerate(entries):
        at = ("roles", i)
        if not isinstance(r, dict):
            c.fail(("roles",), f"roles[{i}] must be a map with role, an optional when, and brief")
        for key in r:
            if key not in ROLE_KEYS:
                c.fail(at + (key,), f"unknown key '{key}' in roles[{i}] (known keys: {', '.join(ROLE_KEYS)})")
        for key in ("role", "brief"):
            if key not in r:
                c.fail(at, f"roles[{i}] has no {key}")
        if not isinstance(r["role"], str) or r["role"] not in roles.ROLES:
            c.fail(at + ("role",), f"unknown role {r['role']!r} (known roles: {', '.join(roles.ROLES)})")
        if "when" in r:
            c.text(r["when"], at + ("when",), "when")
        c.text(r["brief"], at + ("brief",), "brief")
        try:
            placeholders(r["brief"])
        except ValueError as e:
            c.fail(at + ("brief",), f"bad brief: {e}")
    return data


def load_file(path, origin: str = "user", kinds=None) -> Template:
    """Read and validate one template file. Raises TemplateError naming the file."""
    path = Path(path)
    if not path.name.endswith(SUFFIX):
        raise TemplateError(f"{path}: a template file is named <kind>{SUFFIX}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise TemplateError(f"{path}: cannot read: {e}") from e
    data = parse(text, str(path), kinds)
    return Template(kind=data["kind"], data=data, path=path, origin=origin)


def _files(folder: Path) -> list:
    """The `*.yaml` files in `folder`, by name; hidden files and folders are skipped. A missing folder has none."""
    try:
        entries = sorted(folder.iterdir())
    except FileNotFoundError:
        return []
    except OSError as e:
        raise TemplateError(f"{folder}: cannot read: {e}") from e
    return [p for p in entries if p.name.endswith(SUFFIX) and not p.name.startswith(".") and p.is_file()]


def _user_dir(data_dir) -> Optional[Path]:
    return None if data_dir is None else Path(data_dir) / FOLDER


def catalog(data_dir=None) -> dict:
    """Every template by kind: the built-in ones, then the user's from `<data_dir>/templates/`, a user file
    replacing the built-in one of the same kind. Reads only; creates nothing. Raises TemplateError naming the
    first bad file."""
    builtin, user = _files(BUILTIN_DIR), _files(_user_dir(data_dir)) if data_dir is not None else []
    kinds = {p.name[:-len(SUFFIX)] for p in builtin + user}
    out = {}
    for p in builtin:
        t = load_file(p, "builtin", kinds)
        out[t.kind] = t
    for p in user:
        t = load_file(p, "user", kinds)
        out[t.kind] = Template(t.kind, t.data, t.path, "user", replaces_builtin=t.kind in out)
    return dict(sorted(out.items()))


def check(path, data_dir=None) -> dict:
    """Validate a template file without installing it (design 6: installed only after the user checks it). Says
    which template it would replace: `builtin`, `user` (one already in the data folder), or None (a new kind).
    Other user files are not read, so a bad one elsewhere does not hide this file's result."""
    path = Path(path)
    user_dir = _user_dir(data_dir)
    user = _files(user_dir) if user_dir is not None else []
    kinds = {p.name[:-len(SUFFIX)] for p in _files(BUILTIN_DIR) + user}
    t = load_file(path, "user", kinds)
    installed_path = user_dir / path.name if user_dir is not None else None
    installed = installed_path is not None and installed_path.is_file() and _same(installed_path, path)
    if not installed and installed_path is not None and installed_path.is_file():
        replaces = "user"
    elif (BUILTIN_DIR / path.name).is_file():
        replaces = "builtin"
    else:
        replaces = None
    return {"ok": True, "kind": t.kind, "file": str(path), "replaces": replaces, "installed": installed}


def _same(a: Path, b: Path) -> bool:
    try:
        return a.samefile(b)
    except OSError:
        return False


def describe(t: Template) -> dict:
    """A template for `ap2 template show`: where it comes from, the file as written, and its roles with the tools
    and floor each one keeps."""
    return {"kind": t.kind, "origin": t.origin, "file": str(t.path), "replaces_builtin": t.replaces_builtin,
            "template": t.data,
            "roles": [{**{k: e[k] for k in ("role", "when", "brief") if k in e},
                       "tools": list(roles.ROLES[e["role"]].tools), "floor": roles.ROLES[e["role"]].floor}
                      for e in t.data["roles"]]}


def summary(t: Template) -> dict:
    return {"kind": t.kind, "title": t.data.get("title"), "origin": t.origin, "file": str(t.path),
            "replaces_builtin": t.replaces_builtin}
