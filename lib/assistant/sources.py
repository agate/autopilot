"""Source files (design 5): loading from the plugin's and the user's `sources/` folders, `extends:`, validation.

- Built-in files are `<plugin>/sources/*.yaml`; the user's are `<data>/sources/*.yaml`. A file's name (without
  `.yaml`) is its source's name. A user file with the same name wins.
- A file that has `extends: <name>` is merged over that source: maps key by key, lists and scalars replaced.
  `extends:` its own name means the built-in file of that name; another name means that source as loaded (the
  user's when there is one). The result takes the extending file's name.
- `settings:` maps a name to the user's text (built-in files declare theirs empty). `link`, `key`, `key_for` and
  the read's arguments may name one as `{name}`; one that is empty refuses the read or the scan plan before anything
  runs.
- `key_for: {<category>: <pattern>}` (design 6.2): a record of a listed category keys on that pattern instead of
  `key` (a Slack review ask keys on its PR, `{pr}`, so it lands on the GitHub review request's card). Each category
  is one of `sort`'s; a user's `<category>: null` turns that one off. To turn the PR keying off, also sort that
  category elsewhere (`sort: { review_request: question }`), since a `pr-review` card needs a PR key.
- Every problem raises `SourceError`, whose message starts with the file it is about.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from assistant import form, yamlish
from assistant.config import ConfigError

PLUGIN_DIR = Path(__file__).resolve().parent.parent.parent / "sources"
FOLDER = "sources"
SUFFIX = ".yaml"
# A source's name reaches paths (`<data>/sources/.raw/<name>/`), so it is kept to these characters.
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")

BRINGS = ("messages", "work")
VIAS = ("command", "mcp", "connector", "skill")
FILLS = ("paths", "ai")
KEYS = ("source", "brings", "read", "fill", "form", "link", "key", "sort", "key_for")
# `settings:` maps a name to the user's text, for {name} in link, key and the read's arguments. Kept out of KEYS
# only so the unknown-key message (pinned by tests/test_ap2_sources.py) reads as before.
SETTINGS = ("settings",)
SETTING = re.compile(r"[a-z_][a-z0-9_]*\Z")
REQUIRED = ("source", "brings", "read", "fill", "form", "link", "sort")
PROSE = ("instructions",)
WORK_KEYS = ("status_map", "writes", "never", "plan")
WRITE_EVENTS = ("on_start", "on_pr", "on_merge")
WRITE_ACTIONS = ("transition", "assign", "remote_link")
SORT_SPECIAL = ("update_card", "none")
TEMPLATE = re.compile(r"[a-z][a-z0-9_-]*\Z")
DEFAULT_KEY = "{source}:{id}"
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600
# The largest source file the loader reads.
MAX_SOURCE = 1024 * 1024


class SourceError(ConfigError):
    """A source file is not usable, or a read failed. The message names the file (or the source) and says why."""


def read_capped(path, limit: int, too_large: str) -> bytes:
    """The bytes of `path`, reading at most `limit` + 1 of them, so a pipe, a device or a file that grows cannot
    pass the limit (its size from stat says nothing about them). More than `limit` raises SourceError(`too_large`);
    an OSError is the caller's to wrap."""
    with open(path, "rb") as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise SourceError(too_large)
    return data


@dataclass
class Source:
    name: str
    data: dict                       # the merged file
    files: list                      # every file merged, the base first
    origin: str                      # "built-in", "user", or "user, extends <name>"
    paths: dict = field(default_factory=dict)   # fill: paths only: field -> steps
    prefix: tuple = ()               # the item path
    link: tuple = ("none",)          # ("none",) | ("path", steps) | ("pattern", parts)
    key: tuple = ()                  # pattern parts
    key_for: dict = field(default_factory=dict)  # category -> pattern parts: its records key on that, not `key`

    @property
    def file(self) -> Path:
        return self.files[-1]

    @property
    def brings(self) -> str:
        return self.data["brings"]

    @property
    def fill(self) -> str:
        return self.data["fill"]

    @property
    def via(self) -> str:
        return self.data["read"]["via"]

    @property
    def sort(self) -> dict:
        return self.data["sort"]

    def summary(self) -> dict:
        read = self.data["read"]
        return {"source": self.name, "brings": self.brings, "via": self.via, "fill": self.fill,
                "every": read.get("every"), "origin": self.origin, "file": str(self.file)}


@dataclass
class _File:
    path: Path
    data: dict
    lines: dict


class Loader:
    """Reads source files on demand from the built-in folder and the user's folder, each file at most once."""

    def __init__(self, data_dir, plugin_dir: Optional[Path] = None):
        self.folders = (Path(plugin_dir) if plugin_dir is not None else PLUGIN_DIR, Path(data_dir) / FOLDER)
        self._names = [self._scan(f) for f in self.folders]
        self._files = {}

    @staticmethod
    def _scan(folder: Path) -> dict:
        try:
            paths = sorted(p for p in folder.iterdir() if p.suffix == SUFFIX and not p.name.startswith("."))
        except FileNotFoundError:
            return {}
        except OSError as e:
            raise SourceError(f"{folder}: cannot read the sources folder: {e}") from e
        return {p.stem: p for p in paths}

    def names(self) -> list:
        return sorted(set(self._names[0]) | set(self._names[1]))

    def _layer(self, name: str) -> Optional[int]:
        """The layer that holds `name`: 1 (the user's) wins over 0 (built in)."""
        return 1 if name in self._names[1] else 0 if name in self._names[0] else None

    def _file(self, layer: int, name: str) -> _File:
        key = (layer, name)
        if key not in self._files:
            path = self._names[layer][name]
            if not NAME.match(name):
                raise SourceError(f"{path}: a source's file name must be lowercase letters, digits, '-' and '_' "
                                  f"(at most 64), then .yaml")
            try:
                text = read_capped(path, MAX_SOURCE, f"{path}: larger than {MAX_SOURCE} bytes").decode("utf-8")
                text = text.replace("\r\n", "\n").replace("\r", "\n")  # as read_text read it
            except (OSError, UnicodeDecodeError) as e:
                raise SourceError(f"{path}: cannot read: {e}") from e
            try:
                data, lines = yamlish.parse(text)
            except yamlish.YamlError as e:
                raise SourceError(f"{path}: {e}") from e
            if "source" in data and data["source"] != name:
                raise SourceError(f"{path}: line {lines[('source',)]}: source is {data['source']!r}, but the file "
                                  f"is named {name}{SUFFIX}; they must match")
            if "source" not in data and "extends" not in data:
                raise SourceError(f"{path}: source missing (write 'source: {name}', or 'extends: <name>')")
            self._files[key] = _File(path, data, lines)
        return self._files[key]

    def _resolve(self, layer: int, name: str, chain: tuple) -> tuple:
        """The merged data of `name` in `layer`, and the files merged (the base first)."""
        f = self._file(layer, name)
        if (layer, name) in chain:
            names = " -> ".join(self._names[lay][n].name for lay, n in chain + ((layer, name),))
            raise SourceError(f"{f.path}: extends makes a cycle ({names})")
        if "extends" not in f.data:
            return copy.deepcopy(f.data), [f.path]
        base = f.data["extends"]
        line = f.lines[("extends",)]
        if not (isinstance(base, str) and NAME.match(base)):
            raise SourceError(f"{f.path}: line {line}: extends must name a source, not {base!r}")
        if base == name:
            if layer == 0 or name not in self._names[0]:
                raise SourceError(f"{f.path}: line {line}: extends {base}, but there is no built-in {base}")
            base_layer = 0
        else:
            base_layer = self._layer(base)
            if base_layer is None:
                raise SourceError(f"{f.path}: line {line}: extends {base}, but there is no source named {base}")
        merged, files = self._resolve(base_layer, base, chain + ((layer, name),))
        over = {k: v for k, v in f.data.items() if k != "extends"}
        merged = merge(merged, over)
        merged["source"] = name
        return merged, files + [f.path]

    def load(self, name: str) -> Source:
        """The source `name`, merged and checked. Raises SourceError."""
        layer = self._layer(name) if isinstance(name, str) else None
        if layer is None:
            raise SourceError(f"no source named {name!r} (looked in {self.folders[0]} and {self.folders[1]})")
        data, files = self._resolve(layer, name, ())
        f = self._file(layer, name)
        if layer == 0:
            origin = "built-in"
        elif "extends" in f.data:
            origin = f"user, extends {f.data['extends']}"
        else:
            origin = "user"
        return validate(Source(name, data, files, origin), f.lines)

    def load_all(self) -> list:
        """Every source, by name. One bad file fails the whole call, naming that file."""
        return [self.load(n) for n in self.names()]


def merge(base: dict, over: dict) -> dict:
    """`over` merged over `base`: maps key by key; lists, scalars and null replace."""
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _text(v) -> bool:
    return isinstance(v, str) and bool(v.strip())


def validate(src: Source, lines: Optional[dict] = None) -> Source:
    """Check the merged file and fill `src`'s parsed parts. Raises SourceError naming the file."""
    lines = lines or {}
    d = src.data

    def fail(reason: str, path: tuple = ()):
        where = f"line {lines[path]}: " if path in lines else ""
        what = f"{src.file}" if len(src.files) == 1 else f"{src.file} (merged over {src.files[0]})"
        raise SourceError(f"{what}: {where}{reason}")

    for key in d:
        if key not in KEYS + PROSE + WORK_KEYS + SETTINGS:
            fail(f"unknown key '{key}' (known keys: {', '.join(KEYS + PROSE + WORK_KEYS)})", (key,))
    for key in REQUIRED:
        if key not in d:
            fail(f"{key} missing")
    if not (isinstance(d["source"], str) and NAME.match(d["source"])):
        fail(f"source must be a name of lowercase letters, digits, '-' and '_', not {d['source']!r}", ("source",))
    if d["brings"] not in BRINGS:
        fail(f"brings must be one of {', '.join(BRINGS)}, not {d['brings']!r}", ("brings",))
    if d["brings"] != "work":
        for key in WORK_KEYS:
            if key in d:
                fail(f"{key} is only for brings: work sources", (key,))

    read = d["read"]
    if not isinstance(read, dict):
        fail(f"read must be a map with via, not {read!r}", ("read",))
    if read.get("via") not in VIAS:
        fail(f"read.via must be one of {', '.join(VIAS)}, not {read.get('via')!r}", ("read", "via"))
    if read["via"] == "command" and not _text(read.get("run")):
        fail("read.run must be the command to run (via: command)", ("read", "run"))
    if "skip_channels" in read and not (isinstance(read["skip_channels"], list)
                                        and all(_text(x) for x in read["skip_channels"])):
        fail("read.skip_channels must be a list of channel names or ids, such as [\"#random\"]",
             ("read", "skip_channels"))
    if "every" in read and not (_text(read["every"]) or (isinstance(read["every"], int)
                                                          and not isinstance(read["every"], bool))):
        fail(f"read.every must be a text such as 15m, not {read['every']!r}", ("read", "every"))
    if "timeout" in read:
        t = read["timeout"]
        if isinstance(t, bool) or not isinstance(t, int) or not 1 <= t <= MAX_TIMEOUT:
            fail(f"read.timeout must be a whole number of seconds from 1 to {MAX_TIMEOUT}, not {t!r}",
                 ("read", "timeout"))

    if d["fill"] not in FILLS:
        fail(f"fill must be one of {', '.join(FILLS)}, not {d['fill']!r}", ("fill",))

    fm = d["form"]
    if not isinstance(fm, dict):
        fail(f"form must be a map of fields, not {fm!r}", ("form",))
    for f in fm:
        if f not in form.FIELDS:
            fail(f"form: unknown field '{f}' (fields: {', '.join(form.FIELDS)})", ("form", f))
    needed = form.REQUIRED + (("status",) if d["brings"] == "work" else ())
    missing = [f for f in needed if f not in fm]
    if missing:
        fail(f"form must name {', '.join(needed)}; missing: {', '.join(missing)}", ("form",))
    if d["fill"] == "paths":
        for f, p in fm.items():
            try:
                src.paths[f] = form.parse_path(p)
            except form.PathError as e:
                fail(f"form.{f}: {e}", ("form", f))
        src.prefix = form.item_prefix(src.paths["id"])
        for f, steps in src.paths.items():
            if steps[:len(src.prefix)] != src.prefix:
                fail(f"form.{f}: every path must start with the item path of form.id "
                     f"({fm['id'][:_prefix_len(fm['id'])]})", ("form", f))
            if f not in form.LIST_FIELDS and form.fans_out(steps[len(src.prefix):]):
                fail(f"form.{f}: only depends_on may take several values ([] or a filter after the item path)",
                     ("form", f))
    else:
        for f, v in fm.items():
            if not _text(v):
                fail(f"form.{f} must say what fills it, not {v!r}", ("form", f))

    link = d["link"]
    if not _text(link):
        fail(f"link must be a {{field}} pattern, a path such as .url, or none; not {link!r}", ("link",))
    if link == "none":
        src.link = ("none",)
    elif link.startswith("."):
        if d["fill"] != "paths":
            fail("link: a path needs fill: paths (a fill: ai record has no JSON to read it from)", ("link",))
        try:
            steps = form.parse_path(link)
        except form.PathError as e:
            fail(f"link: {e}", ("link",))
        if steps[:len(src.prefix)] != src.prefix or form.fans_out(steps[len(src.prefix):]):
            fail("link: the path must start with the item path of form.id and give one value", ("link",))
        src.link = ("path", steps)
    else:
        try:
            src.link = ("pattern", form.parse_pattern(link))
        except form.PathError as e:
            fail(f"link: {e}", ("link",))

    key = d.get("key", DEFAULT_KEY)
    if not _text(key) or key.startswith("."):
        fail(f"key must be a {{field}} pattern such as \"{{id}}\", not {key!r}", ("key",))
    try:
        src.key = form.parse_pattern(key)
    except form.PathError as e:
        fail(f"key: {e}", ("key",))
    if not any(kind == "field" for kind, _ in src.key):
        fail(f"key must name at least one {{field}}, or every item would be one card: {key!r}", ("key",))

    sort = d["sort"]
    if not (isinstance(sort, dict) and sort):
        fail(f"sort must map each category to a template, update_card or none; not {sort!r}", ("sort",))
    for cat, to in sort.items():
        if not (isinstance(to, str) and (to in SORT_SPECIAL or TEMPLATE.match(to))):
            fail(f"sort.{cat} must be a template name, update_card or none, not {to!r}", ("sort", cat))

    if "key_for" in d:
        kf = d["key_for"]
        if not isinstance(kf, dict):
            fail(f"key_for must map a category of sort to a {{field}} pattern, not {kf!r}", ("key_for",))
        for cat, pattern in kf.items():
            # A user's `review_request: null` keys that category on `key` again; they also sort it elsewhere
            # (`sort: { review_request: question }`), since a pr-review card needs a PR key.
            if pattern is None:
                continue
            at = ("key_for", cat) if ("key_for", cat) in lines else ("key_for",)
            if cat not in sort:
                fail(f"key_for.{cat}: '{cat}' is not one of sort's categories ({', '.join(map(str, sort))})", at)
            if not _text(pattern) or pattern.startswith("."):
                fail(f"key_for.{cat} must be a {{field}} pattern such as \"{{pr}}\", not {pattern!r}", at)
            try:
                parts = form.parse_pattern(pattern)
            except form.PathError as e:
                fail(f"key_for.{cat}: {e}", at)
            if not any(kind == "field" for kind, _ in parts):
                fail(f"key_for.{cat} must name at least one {{field}}, or every such item would be one card: "
                     f"{pattern!r}", at)
            src.key_for[cat] = parts

    if "instructions" in d and not _text(d["instructions"]):
        fail("instructions must be a text", ("instructions",))
    if "status_map" in d:
        sm = d["status_map"]
        if not isinstance(sm, dict) or not all(isinstance(v, (str, int)) and not isinstance(v, bool)
                                               for v in sm.values()):
            fail("status_map must map each status there to a card status", ("status_map",))
    if "never" in d and not (isinstance(d["never"], list) and all(_text(x) for x in d["never"])):
        fail("never must be a list of what the engine must not do", ("never",))
    if "writes" in d:
        w = d["writes"]
        if not isinstance(w, dict):
            fail(f"writes must map {', '.join(WRITE_EVENTS)} to what to change, not {w!r}", ("writes",))
        never = d.get("never") or []
        for event, actions in w.items():
            if event not in WRITE_EVENTS:
                fail(f"writes: unknown event '{event}' (events: {', '.join(WRITE_EVENTS)})", ("writes", event))
            if actions is None:  # a user's `on_pr: null` turns that write off
                continue
            if not (isinstance(actions, dict) and actions):
                fail(f"writes.{event} must map an action ({', '.join(WRITE_ACTIONS)}) to its value, not "
                     f"{actions!r}", ("writes", event))
            for action, value in actions.items():
                if value is None:  # a user's `assign: null` turns that one action off
                    continue
                at = ("writes", event, action) if ("writes", event, action) in lines else ("writes", event)
                if action in never:
                    fail(f"writes.{event}.{action}: '{action}' is listed in never, so no write may do it", at)
                if action not in WRITE_ACTIONS:
                    fail(f"writes.{event}: unknown action '{action}' (actions: {', '.join(WRITE_ACTIONS)})", at)
                if not _text(value):
                    fail(f"writes.{event}.{action} must be a text, not {value!r}", at)
    if "plan" in d and not _text(d["plan"]):
        fail("plan must be a text such as split_here", ("plan",))
    if "settings" in d:
        st = d["settings"]
        if not isinstance(st, dict):
            fail(f"settings must map each name to its text, not {st!r}", ("settings",))
        for name, value in st.items():
            if not SETTING.match(name):
                fail(f"settings: '{name}' must be lowercase letters, digits and '_', so a {{pattern}} can name it",
                     ("settings", name))
            if name in form.FIELDS or name in form.DERIVED:
                fail(f"settings.{name}: '{name}' is a form or derived field, so a pattern naming it would be "
                     f"ambiguous; pick another name", ("settings", name))
            if value is not None and (isinstance(value, bool) or not isinstance(value, (str, int))):
                fail(f"settings.{name} must be a text, not {value!r}", ("settings", name))
    return src


def settings(src: Source) -> dict:
    """The source's settings, name -> text ("" for one that is declared but not set)."""
    return {k: "" if v is None else str(v) for k, v in (src.data.get("settings") or {}).items()}


# The read keys the engine itself uses; every other key is an argument of the scan session's call.
READ_OWN = ("via", "every", "timeout", "run", "server", "tool", "tools", "skill", "skip_channels")


def read_args(src: Source) -> dict:
    """The read's arguments as written: the `read` keys other than the engine's own."""
    return {k: copy.deepcopy(v) for k, v in src.data["read"].items() if k not in READ_OWN}


def _pattern_names(value) -> list:
    """Every {name} in the text values of `value` (texts, lists and maps), in order."""
    if isinstance(value, str):
        return form.PATTERN_FIELD.findall(value)
    if isinstance(value, dict):
        return [n for v in value.values() for n in _pattern_names(v)]
    if isinstance(value, list):
        return [n for v in value for n in _pattern_names(v)]
    return []


def unset_settings(src: Source) -> None:
    """Refuse a source whose link, key, key_for or read arguments name a declared setting that is empty, before anything is
    run, copied or saved. Raises SourceError saying where to set it."""
    st = settings(src)
    named = [n for kind, n in src.key if kind == "field"]
    named += [n for parts in src.key_for.values() for kind, n in parts if kind == "field"]
    if src.link[0] == "pattern":
        named += [n for kind, n in src.link[1] if kind == "field"]
    named += _pattern_names(read_args(src))
    for name in dict.fromkeys(named):
        if name in st and not st[name].strip():
            where = (f"<data>/sources/{src.name}.yaml (extends: {src.name})" if src.origin == "built-in"
                     else str(src.file))
            raise SourceError(f"{src.name}: settings.{name} is not set; add it in {where}")


def fill_args(src: Source) -> dict:
    """The read's arguments with each {setting} filled into its text values. Raises SourceError for a {name} that
    is not a setting, or a setting that is empty."""
    st = settings(src)
    unset_settings(src)

    def fill(value, where):
        if isinstance(value, str):
            def one(m):
                if m.group(1) not in st:
                    raise SourceError(f"{src.name}: read.{where} names {{{m.group(1)}}}, which is not in settings")
                return st[m.group(1)]
            return form.PATTERN_FIELD.sub(one, value)
        if isinstance(value, dict):
            return {k: fill(v, f"{where}.{k}") for k, v in value.items()}
        if isinstance(value, list):
            return [fill(v, where) for v in value]
        return value

    return {k: fill(v, k) for k, v in read_args(src).items()}


def _prefix_len(path: str) -> int:
    i = path.find("[]")
    return i + 2 if i >= 0 else 0
