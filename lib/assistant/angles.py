"""Review angles (design 6.1): one table in three layers, PR kinds, path globs, and the angle picker.

An angle is one reviewer's point of view: a `name`, the one `question` it answers, `when` it applies, and a prose
`brief`. Both the PR loop (the user's own PRs) and the PR review (other people's PRs) pick from one table.

- Layers, the later one winning on the same name: built in (`<plugin>/review/angles.yaml`), the user's
  (`<data>/review/angles.yaml`) and the repo's (`.autopilot/review.yaml`, read from the reviewed commit with
  `git show`, never from the working tree). Each file is a YAML list of maps, at most `sources.MAX_SOURCE` bytes.
- An entry with neither `extends` nor `off` is a whole angle and adds or replaces. `extends: <name>` merges the
  entry's fields over that angle as the earlier layers left it (fields replaced, never merged deeper); `name` may
  be left out and is then the `extends` name. `off: true` (with only `name`) removes an angle; one naming no
  earlier angle lands in `Table.unknown_off`. Correctness can be extended or replaced, never turned off.
- `when` is `always`, or a map with `paths` (globs) and/or `kinds` (`KINDS`, detected from file paths).
- An angle is prose for a reviewer: a key that would widen a role (`templates.WIDEN`), at any depth, raises
  `AngleWidenError`. Every other problem raises `AngleError`; both messages start with the file.
"""
from __future__ import annotations

import copy
import functools
import os
import re
import stat
import subprocess
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from assistant import sources, templates, yamlish
from assistant.config import ConfigError

PLUGIN_FILE = Path(__file__).resolve().parent.parent.parent / "review" / "angles.yaml"
USER_FILE = Path("review") / "angles.yaml"          # in the data folder
REPO_FILE = ".autopilot/review.yaml"                 # in the reviewed commit
MAX_FILE = sources.MAX_SOURCE
GIT_TIMEOUT = 30
# A repo's file comes from a PR author, so its cost is capped: at most this many globs in the file in all (an extends
# without its own `when` counts the globs it takes), at most this many characters in one glob, and '[' / '{' (or
# '- ' items) nested at most this deep on a line (a real angle needs ~5).
MAX_GLOBS = 200
MAX_GLOB = 256
MAX_NESTING = 32

LAYERS = ("built-in", "user", "repo")
KEYS = ("name", "question", "when", "brief", "extends", "off")
FIELDS = ("name", "question", "when", "brief")
WHEN_KEYS = ("paths", "kinds")
CORRECTNESS = "Correctness"
# When fewer than 2 reviewers match, these fill to 2 (secretary's CODE_ROLES), whichever is loaded.
FALLBACK = ("Failure modes & observability", "Test value")
MODES = ("loop", "review")
LOOP_MAX = 4
REVIEW_COUNT = 3
SMALL_COUNT = 2
MIN_COUNT = 2
# A PR review that changes fewer lines than this is small (secretary's review_small_lines).
SMALL_LINES = 30

# PR kinds by file path, copied from secretary (lib/secretary/review.py AREAS, TESTS; sources/github_common.py
# DOCS). Riskiest first. The five code kinds are detected only on files that are neither tests nor docs.
CODE_KINDS = (
    ("data", re.compile(r"\.sql$|(^|/)migrations/")),
    ("infra", re.compile(r"(^|/)(terraform|manifests|k8s)/|\.tf$|(^|/)deploy/(.*/)?[^/]+\.ya?ml$")),
    ("api", re.compile(r"\.proto$|(^|/)(api|server|handlers)/")),
    ("ci", re.compile(r"(^|/)\.github/|\.sh$|(^|/)bin/|(^|/)Makefile$")),
    ("ui", re.compile(r"\.(tsx|jsx|css)$|(^|/)components/")),
)
TESTS = re.compile(r"(^|/)(tests?|__tests__)/|(^|/)test_[^/]+$|_test\.[^/]+$|\.(test|spec)\.[^/]+$")
DOCS = re.compile(r"(^|/)docs/|\.(md|mdx|txt|rst)$", re.I)
KINDS = tuple(k for k, _ in CODE_KINDS) + ("tests", "docs")

_COMMIT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/~^@{}-]{0,254}\Z")
_WRAP = "angles:\n"   # yamlish reads only a top-level map, so a file is read as the list under this key
_NOT_A_LIST = "an angle file must be a list of angles ('- name: ...')"
_ONE_LINE = "must be one line of text with no control characters"
_PROSE = "must be text with no control characters but newlines and tabs"
_MISSING = "the file's blob is not in this clone (a partial clone); fetch it first"
# Every git call stays local: a partial clone never fetches a missing blob from its remote (git 2.44 and later; an
# older git ignores the variable), and a fetch an older git still starts never waits on a password prompt.
_GIT_ENV = {"GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"}
# Bidi overrides and isolates: they make text read in another order than it is (U+202A-U+202E, U+2066-U+2069).
_BIDI = frozenset(map(chr, list(range(0x202A, 0x202F)) + list(range(0x2066, 0x206A))))


class AngleError(ConfigError):
    """An angle file is not usable. The message starts with the file, then `line N:` when a line is known."""


class AngleWidenError(AngleError):
    """An angle holds a key that would give a reviewer more tools or permissions than its role ships with."""

    def __init__(self, file: str, key: str, line: Optional[int]):
        at = f"line {line}: " if line else ""
        super().__init__(f"{file}: {at}{_show(key)} is not allowed: an angle cannot add tools or permissions; it is "
                         "prose for a reviewer, and each reviewer keeps the tools and permission floor its role "
                         "ships with")
        self.file, self.key, self.line = file, key, line


@dataclass
class Angle:
    name: str
    question: str
    when: object          # "always" or {"paths": [...], "kinds": [...]}
    brief: str
    origin: str           # "built-in", "user", "repo", "user, extends <name>" or "repo, extends <name>"
    file: str             # the file that last set it
    layer: int            # index in LAYERS of that file
    pos: int              # the entry's index in that file

    def summary(self) -> dict:
        return {"name": self.name, "question": self.question, "when": copy.deepcopy(self.when),
                "brief": self.brief, "origin": self.origin, "file": self.file}


@dataclass
class Table:
    angles: list                                         # in table order: built in first, new names appended
    unknown_off: list = field(default_factory=list)      # [{"name", "file", "line"}]

    def get(self, name: str) -> Optional[Angle]:
        return next((a for a in self.angles if a.name == name), None)

    def names(self) -> list:
        return [a.name for a in self.angles]


# --- reading the three files ----------------------------------------------------------------------------------

def _read_file(path: Path) -> Optional[str]:
    """The text of a local angle file, or None when there is none."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise AngleError(f"{path}: cannot read: {e}") from e
    if not stat.S_ISREG(st.st_mode):
        raise AngleError(f"{path}: not a regular file")
    try:
        data = sources.read_capped(path, MAX_FILE, f"{path}: larger than {MAX_FILE} bytes")
    except sources.SourceError as e:
        raise AngleError(str(e)) from None
    except OSError as e:
        raise AngleError(f"{path}: cannot read: {e}") from e
    return _decode(data, str(path))


def _decode(data: bytes, where: str) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise AngleError(f"{where}: cannot read: {e}") from e
    # yamlish drops a leading BOM only at the very start of its text, and the file is read under a wrapper key.
    return text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def _git(repo, args: list, where: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "-C", str(repo)] + args, capture_output=True, timeout=GIT_TIMEOUT,
                              stdin=subprocess.DEVNULL, env={**os.environ, **_GIT_ENV})
    except (OSError, subprocess.SubprocessError) as e:
        raise AngleError(f"{where}: cannot run git: {e}") from e


def _git_failed(p: subprocess.CompletedProcess, where: str, what: str) -> AngleError:
    err = p.stderr.decode("utf-8", "replace").strip().splitlines()
    return AngleError(f"{where}: {what} (git exit {p.returncode}{': ' + err[-1] if err else ''})")


def _read_repo(repo, commit: str) -> tuple:
    """(text or None, where) of `.autopilot/review.yaml` at `commit` in `repo`. Read-only: `rev-parse`, `ls-tree`
    and `cat-file`. A path missing at that commit is no layer; every other failure is an AngleError."""
    # The file is read from the repo root (ls-tree --full-tree), so it is named from there too. A bare repo has no
    # top folder and keeps the path given; a folder that is no repo fails on the next command. Finding the top does
    # not use the commit, so the commit name is checked after it and its refusal names the root's file too.
    p = _git(repo, ["rev-parse", "--show-toplevel"], f"{repo}/{REPO_FILE}")
    top = (os.fsdecode(p.stdout).rstrip("\n") if p.returncode == 0 else "") or str(repo)
    if not isinstance(commit, str) or not _COMMIT.match(commit):
        raise AngleError(f"{top}/{REPO_FILE}: {_show(commit)} is not a commit name")
    where = f"{top}/{REPO_FILE} at {commit}"
    p = _git(repo, ["rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"], where)
    if p.returncode != 0:
        raise _git_failed(p, where, f"no commit {commit!r} in {repo}")
    sha = p.stdout.decode("ascii", "replace").strip()
    where = f"{top}/{REPO_FILE} at {sha[:12]}"
    p = _git(repo, ["ls-tree", "--full-tree", "-l", "-z", sha, "--", REPO_FILE], where)
    if p.returncode != 0:
        raise _git_failed(p, where, "cannot list the commit's files")
    out = p.stdout.rstrip(b"\0")
    if not out:
        return None, where
    meta, _, name = out.partition(b"\t")
    parts = meta.split()
    if name != REPO_FILE.encode() or len(parts) != 4:
        raise AngleError(f"{where}: cannot read git's listing of the file")
    mode, kind, blob, size = (x.decode("ascii", "replace") for x in parts)
    if kind != "blob" or mode not in ("100644", "100755"):
        raise AngleError(f"{where}: not a regular file (git mode {mode}, {kind})")
    # With lazy fetch off, a blob a partial clone does not hold is listed with no size ('BAD' in git 2.51).
    if not size.isdigit():
        raise AngleError(f"{where}: {_MISSING}")
    if int(size) > MAX_FILE:
        raise AngleError(f"{where}: larger than {MAX_FILE} bytes")
    p = _git(repo, ["cat-file", "blob", blob], where)
    if p.returncode != 0:
        if _git(repo, ["cat-file", "-e", blob], where).returncode != 0:
            raise AngleError(f"{where}: {_MISSING}")
        raise _git_failed(p, where, "cannot read the file from the commit")
    if len(p.stdout) > MAX_FILE:
        raise AngleError(f"{where}: larger than {MAX_FILE} bytes")
    return _decode(p.stdout, where), where


# --- checking one file ----------------------------------------------------------------------------------------

class _Checker:
    def __init__(self, where: str, lines: dict):
        self.where, self.lines = where, lines
        self.globs = 0    # the globs of every when.paths so far, capped at MAX_GLOBS for the file
        # A list item (i,) -> the smallest line of its keys. Every other path line() is asked about is a map key,
        # so only one-element prefixes are kept: one step per key, whatever its depth.
        self.first = {}
        for p, n in lines.items():
            if p and n < self.first.get(p[:1], n + 1):
                self.first[p[:1]] = n

    def line(self, path: tuple) -> Optional[int]:
        """The line of a key's path in the file; for a list item `(i,)`, the first line of its keys."""
        if path in self.lines:
            return self.lines[path]
        return self.first.get(path)

    def fail(self, path: tuple, reason: str):
        n = self.line(path) if path else None
        raise AngleError(f"{self.where}: line {n}: {reason}" if n else f"{self.where}: {reason}")

    def widen(self, value, path: tuple) -> None:
        """Refuse a widening key anywhere, before any other check, so it always gets its own error class."""
        if isinstance(value, dict):
            for k, v in value.items():
                if isinstance(k, str) and templates._norm(k) in templates.WIDEN:
                    raise AngleWidenError(self.where, k, self.line(path + (k,)))
                self.widen(v, path + (k,))
        elif isinstance(value, list):
            for i, v in enumerate(value):
                self.widen(v, path + (i,))

    def text(self, entry: dict, key: str, path: tuple) -> None:
        v = entry[key]
        if not isinstance(v, str) or not v.strip():
            self.fail(path + (key,), f"{key} must be non-empty text, not {_show(v)}")
        # A name is printed on one line (pick's name and why, unknown_off).
        if key in ("name", "extends") and not _one_line(v):
            self.fail(path + (key,), f"{key} {_ONE_LINE}, not {_show(v)}")
        # A question or brief is prose that may span lines, and pick() hands it on as it is.
        if key in ("question", "brief") and any(_unsafe(ch) for ch in v if ch not in "\n\t"):
            self.fail(path + (key,), f"{key} {_PROSE}, not {_show(v)}")

    def when(self, v, path: tuple) -> None:
        at = path + ("when",)
        if v == "always":
            return
        if not isinstance(v, dict) or not v:
            self.fail(at, f"when must be 'always' or a map with paths and/or kinds, not {_show(v)}")
        for k in v:
            if k not in WHEN_KEYS:
                self.fail(at + (k,), f"unknown key {_show(k)} in when (known keys: {', '.join(WHEN_KEYS)})")
        for k in WHEN_KEYS:
            if k not in v:
                continue
            items = v[k]
            if not isinstance(items, list) or not items:
                self.fail(at + (k,), f"when.{k} must be a non-empty list, not {_show(items)}")
            if k == "paths":
                self.globs += len(items)
                if self.globs > MAX_GLOBS:
                    self.fail(at + (k,), f"the file holds more than {MAX_GLOBS} globs in all")
            seen = set()
            for x in items:
                if not isinstance(x, str) or not x.strip():
                    self.fail(at + (k,), f"when.{k} entries must be non-empty text, not {_show(x)}")
                if x in seen:
                    self.fail(at + (k,), f"when.{k} holds {_show(x)} more than once")
                seen.add(x)
                if k == "paths" and not _one_line(x):
                    self.fail(at + (k,), f"when.paths entries {_ONE_LINE}, not {_show(x)}")
                if k == "paths" and len(x) > MAX_GLOB:
                    self.fail(at + (k,), f"glob {_show(x)} is longer than {MAX_GLOB} characters")
                if k == "kinds" and x not in KINDS:
                    self.fail(at + (k,), f"unknown kind {_show(x)} in when.kinds (kinds: {', '.join(KINDS)})")
                if k == "paths" and x.startswith("/"):
                    self.fail(at + (k,), f"glob {_show(x)} starts with '/'; globs match repo-relative paths, "
                                         "so drop it")


def _unsafe(ch: str) -> bool:
    """Whether `ch` breaks a line or hides what a line says: a control character, a line or paragraph separator,
    or a bidi override or isolate. Every other character, a joined emoji or a no-break space included, is text."""
    return unicodedata.category(ch) in ("Cc", "Zl", "Zp") or ch in _BIDI


def _one_line(s: str) -> bool:
    return not any(map(_unsafe, s))


def _show(v) -> str:
    """A value from a file as a message shows it: its repr (control characters escaped), at most 80 characters."""
    s = repr(v)
    return s if len(s) <= 80 else s[:77] + "..."


def _reason(reason: str) -> str:
    """yamlish's reason with any file text in it made safe to print: control characters escaped, at most 200
    characters."""
    s = "".join(ch if ch.isprintable() else repr(ch)[1:-1] for ch in reason)
    return s if len(s) <= 200 else s[:197] + "..."


def _items(s: str) -> int:
    """How many `- ` list items start the line text `s` (its indent dropped): yamlish reads `- - x` as a list in a
    list, one level per `-`. A `-` before a tab counts too: yamlish cuts the comment from `- -\t# c` and reads two
    items, and refuses `-\t` anywhere else. So the count is never below yamlish's."""
    j, n, count = 0, len(s), 0
    while j < n and s[j] == "-" and (j + 1 == n or s[j + 1] in " \t"):
        count += 1
        j += 1
        while j < n and s[j] == " ":
            j += 1
    return count


def _too_deep(s: str, no: int) -> bool:
    """Whether the flow collections on the line text `s` (its indent dropped) nest deeper than MAX_NESTING, read as
    yamlish reads them: the scan of `yamlish._strip_comment`, whose helpers decide what it decides. A comment ends
    the line; a quote opens a string only where a value starts; a '[' / '{' opens a collection only where a value
    starts or inside an open one, and any ']' / '}' inside one closes it, as yamlish's flow reader does. A line
    yamlish refuses as it reads (an unclosed quote) is left to it."""
    i, n, depth, quote_end = 0, len(s), 0, -1
    lead = yamlish._lead_end(s)
    last = before_last = -1
    try:
        while i < n:
            c = s[i]
            if c == "#" and (i == 0 or s[i - 1] in " \t"):
                return False
            if c in "\"'" and yamlish._value_start(s, i, depth, quote_end, lead, last, before_last):
                i = quote_end = yamlish._quote_end(s, i, no)
            elif c in "[{" and (depth or yamlish._value_start(s, i, 0, quote_end, lead, last, before_last)):
                depth += 1
                if depth > MAX_NESTING:
                    return True
            elif c in "]}" and depth:
                depth -= 1
            if s[i] not in " \t":
                last, before_last = i, last
            i += 1
    except yamlish.YamlError:
        return False
    return False


def _nesting(text: str, where: str) -> None:
    """Refuse a line that nests deeper than MAX_NESTING, before yamlish reads it: its '[' / '{' flow collections, or
    its leading `- ` items. yamlish keeps a key path per nested key and a copy of the rest of the line per item, so a
    deep line costs memory in step with its depth squared or its depth times its length. Both counts are taken as
    yamlish reads the line, so text yamlish reads as text is not counted, and never fall below its real depth."""
    for no, line in enumerate(text.split("\n"), 1):
        s = line.lstrip(" ")
        if _items(s) > MAX_NESTING:
            raise AngleError(f"{where}: line {no}: '- ' list items nested more than {MAX_NESTING} deep on one line; "
                             "an angle needs only a few levels")
        if ("[" in s or "{" in s) and _too_deep(s, no):
            raise AngleError(f"{where}: line {no}: '[' and '{{' nested more than {MAX_NESTING} deep; an angle needs "
                             "only a few levels")


def _parse(text: str, where: str) -> tuple:
    """(entries, checker) of one file's text: a list of maps, checked for widening keys first."""
    _nesting(text, where)
    try:
        yamlish.parse(text)
    except yamlish.YamlError as e:
        if e.reason == "the top level must be a map of 'key: value' lines":
            # A flow list or a bare word on the first line: neither a map nor a list of maps.
            raise AngleError(f"{where}: line {e.line}: {_NOT_A_LIST}") from e
        if not e.reason.endswith("not a list"):
            raise AngleError(f"{where}: line {e.line}: {_reason(e.reason)}") from e
    else:
        if text.strip() and yamlish.load(text):
            raise AngleError(f"{where}: {_NOT_A_LIST}, not a map")
        return [], _Checker(where, {})
    try:
        data, lines = yamlish.parse(_WRAP + text)
    except yamlish.YamlError as e:
        if e.reason.startswith("duplicate key 'angles' (first on line 1)"):
            # The file's own top-level 'angles:' line beside its list items: not a list of angles.
            raise AngleError(f"{where}: line {e.line - 1}: {_NOT_A_LIST}") from e
        # Every line number in it is one past the file's own: the wrapper key is line 1.
        reason = re.sub(r"\(first on line (\d+)\)", lambda m: f"(first on line {int(m.group(1)) - 1})", e.reason)
        raise AngleError(f"{where}: line {e.line - 1}: {_reason(reason)}") from e
    if set(data) != {"angles"} or not isinstance(data["angles"], list):
        raise AngleError(f"{where}: {_NOT_A_LIST}")
    lines = {p[1:]: n - 1 for p, n in lines.items() if p[:1] == ("angles",) and len(p) > 1}
    c = _Checker(where, lines)
    c.widen(data["angles"], ())
    return data["angles"], c


def _entry(c: _Checker, i: int, entry) -> dict:
    """One entry checked on its own; returns it with `name` filled from `extends` when left out."""
    path = (i,)
    if not isinstance(entry, dict):
        c.fail((), f"item {i + 1} must be a map with name, question, when and brief, not {_show(entry)}")
    for key in entry:
        if key not in KEYS:
            c.fail(path + (key,), f"unknown key {_show(key)} (known keys: {', '.join(KEYS)})")
    entry = dict(entry)
    if "off" in entry:
        if entry["off"] is not True:
            c.fail(path + ("off",), f"off must be true, not {_show(entry['off'])}")
        extra = [k for k in entry if k not in ("name", "off")]
        if extra:
            c.fail(path + (extra[0],), f"an entry with off: true holds only name, not {_show(extra[0])}")
        if "name" not in entry:
            c.fail(path + ("off",), "an entry with off: true needs the name of the angle to turn off")
        c.text(entry, "name", path)
        return entry
    if "extends" in entry:
        c.text(entry, "extends", path)
        entry.setdefault("name", entry["extends"])
    else:
        for key in FIELDS:
            if key not in entry:
                c.fail(path, f"missing key '{key}' (an angle has {', '.join(FIELDS)}; or write extends: <name>)")
    for key in ("name", "question", "brief"):
        if key in entry:
            c.text(entry, key, path)
    if "when" in entry:
        c.when(entry["when"], path)
    return entry


def _apply(table: Table, layer: int, text: Optional[str], where: str, gone: Optional[dict] = None) -> None:
    """Apply one layer's file to `table` in place. `gone` maps each name an earlier layer of this load turned off to
    (file, line), and gets this layer's."""
    gone = {} if gone is None else gone
    if text is None:
        return
    entries, c = _parse(text, where)
    earlier = {a.name: a for a in table.angles}
    # The table by name; a dict keeps a replaced name in its place and appends a new one, like the table.
    current = dict(earlier)
    seen = {}
    for i, raw in enumerate(entries):
        entry = _entry(c, i, raw)
        name = entry["name"]
        named = (i, "name") if c.line((i, "name")) else (i, "extends") if c.line((i, "extends")) else (i,)
        if name in seen:
            c.fail(named, f"angle {_show(name)} is already named on line {seen[name]}; a name is used once per "
                          "file")
        seen[name] = c.line(named)
        if "off" in entry:
            if name == CORRECTNESS:
                c.fail((i, "off"), f"{CORRECTNESS} cannot be turned off; extend or replace it instead")
            if name in earlier:
                del current[name]
                gone[name] = (where, c.line((i,)))
            else:
                table.unknown_off.append({"name": name, "file": where, "line": c.line((i,))})
            continue
        if "extends" in entry:
            base = earlier.get(entry["extends"])
            if base is None and entry["extends"] in gone:
                off_file, off_line = gone[entry["extends"]]
                c.fail((i, "extends"), f"extends {_show(entry['extends'])}, but {off_file} line {off_line} turned it "
                                       "off")
            if base is None:
                where_from = ", ".join(LAYERS[:layer]) or "no earlier layer"
                c.fail((i, "extends"), f"extends {_show(entry['extends'])}, but no earlier layer has an angle of that "
                                       f"name (looked in: {where_from})")
            if "when" not in entry and isinstance(base.when, dict):
                # The base's globs come along, so they count toward this file's cap as its own do.
                c.globs += len(base.when.get("paths", []))
                if c.globs > MAX_GLOBS:
                    c.fail((i, "extends"), f"the file holds more than {MAX_GLOBS} globs in all")
            fields = {k: copy.deepcopy(getattr(base, k)) for k in FIELDS}
            fields.update({k: copy.deepcopy(entry[k]) for k in FIELDS if k in entry})
            origin = f"{LAYERS[layer]}, extends {entry['extends']}"
        else:
            fields = {k: copy.deepcopy(entry[k]) for k in FIELDS}
            origin = LAYERS[layer]
        current[name] = Angle(fields["name"], fields["question"], fields["when"], fields["brief"], origin, where,
                              layer, i)
    table.angles = list(current.values())


def load(data_dir=None, repo=None, commit=None, plugin_file=None) -> Table:
    """The angle table: built in, then the user's file under `data_dir`, then the repo's file at `commit` in
    `repo` (both or neither). Raises AngleError (AngleWidenError for a widening key)."""
    if (repo is None) != (commit is None):
        raise AngleError(f"{REPO_FILE}: the repo layer needs both the repo and the commit "
                         f"(got repo={repo!r}, commit={commit!r})")
    plugin = Path(plugin_file) if plugin_file is not None else PLUGIN_FILE
    table, gone = Table([]), {}
    text = _read_file(plugin)
    if text is None:
        raise AngleError(f"{plugin}: the built-in angle file is missing")
    _apply(table, 0, text, str(plugin), gone)
    if table.get(CORRECTNESS) is None:
        raise AngleError(f"{plugin}: the built-in angles must include {CORRECTNESS}")
    if data_dir is not None:
        path = Path(data_dir) / USER_FILE
        _apply(table, 1, _read_file(path), str(path), gone)
    if repo is not None:
        text, where = _read_repo(repo, commit)
        _apply(table, 2, text, where, gone)
    return table


# --- kinds, globs and the picker ------------------------------------------------------------------------------

def kinds(files: list) -> dict:
    """The PR kinds of `files`, in KINDS order (riskiest first), each with the files that gave it."""
    found = {}
    for f in files:
        test, doc = bool(TESTS.search(f)), bool(DOCS.search(f))
        hits = [k for k, pattern in CODE_KINDS if not (test or doc) and pattern.search(f)]
        hits += (["tests"] if test else []) + (["docs"] if doc else [])
        for k in hits:
            found.setdefault(k, []).append(f)
    return {k: found[k] for k in KINDS if k in found}


def _bits(positions: list, size: int) -> int:
    """An integer with the bits at `positions` set, built in one step (OR-ing bit by bit costs size squared)."""
    digits = bytearray(b"0" * (size + 1))
    for p in positions:
        digits[size - p] = 49    # "1"
    return int(digits, 2)


@functools.lru_cache(maxsize=1024)
def _compile(pattern: str) -> tuple:
    """`pattern` as a bit-parallel automaton: one bit per character the glob matches with (bit 0 is the start), so
    a whole set of states is one integer and one path character moves them all in a few integer steps. A literal
    character, `?` and each '/' between parts take one character; `*` (a run of them is one) takes any run inside a
    segment; a middle `**/` is two bits, a folder name (any run of one or more) and its '/', looped; a final `**`
    takes one character or more, '/' included. Cached, so a pick compiles each glob once."""
    parts = pattern.split("/")
    # A run of '**' parts matches what one does.
    parts = [part for i, part in enumerate(parts) if not (part == "**" and i and parts[i - 1] == "**")]
    lit, other, slash, loop, back, skip2, skip1 = {}, [], [], [], [], [], []
    pos, last = 0, len(parts) - 1
    for i, part in enumerate(parts):
        if part == "**" and i == last:
            pos += 1
            for bits in (other, slash, loop):
                bits.append(pos)
            break
        if part == "**":
            skip2.append(pos)                       # zero folders: skip both bits
            other.append(pos + 1)                   # a folder name, one character or more
            loop.append(pos + 1)
            slash.append(pos + 2)                   # its '/', then back to the next folder's name
            back.append(pos + 2)
            pos += 2
            continue
        prev = ""
        for ch in part:
            if ch == "*" and prev == "*":
                continue
            prev = ch
            pos += 1
            if ch == "*":
                skip1.append(pos - 1)               # an empty run: skip the bit
                other.append(pos)
                loop.append(pos)
            else:
                (other if ch == "?" else lit.setdefault(ch, [])).append(pos)
        if i != last:
            pos += 1
            lit.setdefault("/", []).append(pos)
    other_bits = _bits(other, pos)
    # What one path character lets through: its own literal bits, plus `?`, `*` and folder-name bits for every
    # character but '/', or the bits that take a '/'.
    takes = {ch: _bits(ps, pos) | other_bits for ch, ps in lit.items() if ch != "/"}
    takes["/"] = _bits(lit.get("/", []), pos) | _bits(slash, pos)
    return (takes, other_bits, _bits(loop, pos), _bits(back, pos), _bits(skip2, pos), _bits(skip1, pos),
            1 << pos)


def glob_match(pattern: str, path: str) -> bool:
    """Whether the repo-relative `path` matches `pattern` as a whole: `**` is any number of folders, each with a
    name (a final `**` is everything under its folder), `*` and `?` stay inside one path segment, every other
    character is itself. A glob comes from a file a PR author can write, so there is no backtracking: the path is
    read once, a fixed few integer steps a character on an integer of about the glob's length in bits."""
    takes, other, loop, back, skip2, skip1, accept = _compile(pattern)
    get = takes.get
    states = 1
    states |= (states & skip2) << 2
    states |= (states & skip1) << 1
    for ch in path:
        # Each state steps to the next bit, stays on a looping bit, or goes from a folder's '/' back to its name.
        states = ((states << 1) | (states & loop) | ((states & back) >> 1)) & get(ch, other)
        if not states:
            return False
        # Then the empty matches: zero folders for a middle `**/` (which only a `*` can follow), an empty `*`.
        states |= (states & skip2) << 2
        states |= (states & skip1) << 1
    return bool(states & accept)


def _examples(files: list) -> str:
    # A file's name comes from the PR author and may hold a newline: escaped, so the why stays one line.
    shown = ", ".join("".join(repr(ch)[1:-1] if _unsafe(ch) else ch for ch in f) for f in files[:3])
    return shown + (" (+%d more)" % (len(files) - 3) if len(files) > 3 else "")


def _matches(angle: Angle, files: list, found: dict) -> bool:
    """Whether `angle` matches a PR that changes `files`, whose PR kinds are `found`, without writing why."""
    if angle.when == "always":
        return True
    return (any(kind in found for kind in angle.when.get("kinds", []))
            or any(glob_match(pattern, f) for pattern in angle.when.get("paths", []) for f in files))


def _why(angle: Angle, files: list, shown: dict) -> Optional[str]:
    """Why `angle` matches the PR, in one line, or None when it does not. `shown` is each PR kind's examples, as
    `_examples` writes them: written once per pick, and a why only for the angles picked, since a table may hold
    any number of kinds-only angles and each why holds up to three kinds' examples."""
    if angle.when == "always":
        return "always"
    # At most three reasons are written out; the rest are only counted.
    reasons, more = [], 0
    for pattern in angle.when.get("paths", []):
        if len(reasons) < 3:
            hit = [f for f in files if glob_match(pattern, f)]
            if hit:
                reasons.append(f"paths {pattern} matched {_examples(hit)}")
        elif any(glob_match(pattern, f) for f in files):
            more += 1
    for kind in angle.when.get("kinds", []):
        if kind in shown:
            if len(reasons) < 3:
                reasons.append(f"kind {kind}: {shown[kind]}")
            else:
                more += 1
    return "; ".join(reasons) + (" (+%d more)" % more if more else "") or None


def _picked(angle: Angle, why: str) -> dict:
    return {"name": angle.name, "question": angle.question, "brief": angle.brief, "origin": angle.origin,
            "file": angle.file, "why": why}


def count(files: list, mode: str, changed_lines: Optional[int] = None) -> int:
    """The most reviewers, Correctness included: 4 for the PR loop; 3 for a PR review, or 2 when it is docs only
    or changes fewer than SMALL_LINES lines."""
    if mode == "loop":
        return LOOP_MAX
    if mode != "review":
        raise ValueError(f"mode must be one of {', '.join(MODES)}, not {mode!r}")
    if not isinstance(changed_lines, int) or isinstance(changed_lines, bool) or changed_lines < 0:
        raise ValueError(f"a PR review needs changed_lines, a count of lines, not {changed_lines!r}")
    docs_only = bool(files) and all(DOCS.search(f) for f in files)
    return SMALL_COUNT if docs_only or changed_lines < SMALL_LINES else REVIEW_COUNT


def pick(table: Table, files: list, mode: str, changed_lines: Optional[int] = None) -> list:
    """The reviewers for a PR that changes `files` (repo-relative paths): Correctness first, then the matching
    angles, the repo's before the user's before the built-in ones, each layer in file order; at most `count()`.
    When fewer than 2 are picked, FALLBACK fills to 2. Each is {"name", "question", "brief", "origin", "file",
    "why"}."""
    most = count(files, mode, changed_lines)
    first = table.get(CORRECTNESS)
    out = [_picked(first, "always")]
    found = kinds(files)
    matched = [(-a.layer, a.pos, a) for a in table.angles if a.name != CORRECTNESS and _matches(a, files, found)]
    matched.sort(key=lambda m: (m[0], m[1]))
    shown = {k: _examples(v) for k, v in found.items()}
    out += [_picked(a, _why(a, files, shown)) for _, _, a in matched[:most - 1]]
    for name in FALLBACK:
        if len(out) >= MIN_COUNT:
            break
        a = table.get(name)
        if a is not None and all(p["name"] != name for p in out):
            out.append(_picked(a, "fallback"))
    return out
