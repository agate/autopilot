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
import os
import re
import stat
import subprocess
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


class AngleError(ConfigError):
    """An angle file is not usable. The message starts with the file, then `line N:` when a line is known."""


class AngleWidenError(AngleError):
    """An angle holds a key that would give a reviewer more tools or permissions than its role ships with."""

    def __init__(self, file: str, key: str, line: Optional[int]):
        at = f"line {line}: " if line else ""
        super().__init__(f"{file}: {at}'{key}' is not allowed: an angle cannot add tools or permissions; it is "
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
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _git(repo, args: list, where: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "-C", str(repo)] + args, capture_output=True, timeout=GIT_TIMEOUT,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        raise AngleError(f"{where}: cannot run git: {e}") from e


def _git_failed(p: subprocess.CompletedProcess, where: str, what: str) -> AngleError:
    err = p.stderr.decode("utf-8", "replace").strip().splitlines()
    return AngleError(f"{where}: {what} (git exit {p.returncode}{': ' + err[-1] if err else ''})")


def _read_repo(repo, commit: str) -> tuple:
    """(text or None, where) of `.autopilot/review.yaml` at `commit` in `repo`. Read-only: `rev-parse`, `ls-tree`
    and `cat-file`. A path missing at that commit is no layer; every other failure is an AngleError."""
    where = f"{repo}/{REPO_FILE} at {commit}"
    if not isinstance(commit, str) or not _COMMIT.match(commit):
        raise AngleError(f"{where}: {commit!r} is not a commit name")
    p = _git(repo, ["rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"], where)
    if p.returncode != 0:
        raise _git_failed(p, where, f"no commit {commit!r} in {repo}")
    sha = p.stdout.decode("ascii", "replace").strip()
    where = f"{repo}/{REPO_FILE} at {sha[:12]}"
    p = _git(repo, ["ls-tree", "-l", "-z", sha, "--", REPO_FILE], where)
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
    if not size.isdigit() or int(size) > MAX_FILE:
        raise AngleError(f"{where}: larger than {MAX_FILE} bytes")
    p = _git(repo, ["cat-file", "blob", blob], where)
    if p.returncode != 0:
        raise _git_failed(p, where, "cannot read the file from the commit")
    if len(p.stdout) > MAX_FILE:
        raise AngleError(f"{where}: larger than {MAX_FILE} bytes")
    return _decode(p.stdout, where), where


# --- checking one file ----------------------------------------------------------------------------------------

class _Checker:
    def __init__(self, where: str, lines: dict):
        self.where, self.lines = where, lines

    def line(self, path: tuple) -> Optional[int]:
        """The line of a key's path in the file; for a list item, the first line of its keys."""
        if path in self.lines:
            return self.lines[path]
        found = [n for p, n in self.lines.items() if p[:len(path)] == path]
        return min(found) if found else None

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

    def when(self, v, path: tuple) -> None:
        at = path + ("when",)
        if v == "always":
            return
        if not isinstance(v, dict) or not v:
            self.fail(at, f"when must be 'always' or a map with paths and/or kinds, not {_show(v)}")
        for k in v:
            if k not in WHEN_KEYS:
                self.fail(at + (k,), f"unknown key '{k}' in when (known keys: {', '.join(WHEN_KEYS)})")
        for k in WHEN_KEYS:
            if k not in v:
                continue
            items = v[k]
            if not isinstance(items, list) or not items:
                self.fail(at + (k,), f"when.{k} must be a non-empty list, not {_show(items)}")
            for x in items:
                if not isinstance(x, str) or not x.strip():
                    self.fail(at + (k,), f"when.{k} entries must be non-empty text, not {_show(x)}")
                if k == "kinds" and x not in KINDS:
                    self.fail(at + (k,), f"unknown kind '{x}' in when.kinds (kinds: {', '.join(KINDS)})")
                if k == "paths" and x.startswith("/"):
                    self.fail(at + (k,), f"glob '{x}' starts with '/'; globs match repo-relative paths, so drop it")


def _show(v) -> str:
    s = repr(v)
    return s if len(s) <= 80 else s[:77] + "..."


def _parse(text: str, where: str) -> tuple:
    """(entries, checker) of one file's text: a list of maps, checked for widening keys first."""
    try:
        yamlish.parse(text)
    except yamlish.YamlError as e:
        if not e.reason.endswith("not a list"):
            raise AngleError(f"{where}: {e}") from e
    else:
        if text.strip() and yamlish.load(text):
            raise AngleError(f"{where}: an angle file must be a list of angles ('- name: ...'), not a map")
        return [], _Checker(where, {})
    try:
        data, lines = yamlish.parse(_WRAP + text)
    except yamlish.YamlError as e:
        raise AngleError(f"{where}: line {e.line - 1}: {e.reason}") from e
    if set(data) != {"angles"} or not isinstance(data["angles"], list):
        raise AngleError(f"{where}: an angle file must be a list of angles ('- name: ...')")
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
            c.fail(path + (key,), f"unknown key '{key}' (known keys: {', '.join(KEYS)})")
    entry = dict(entry)
    if "off" in entry:
        if entry["off"] is not True:
            c.fail(path + ("off",), f"off must be true, not {_show(entry['off'])}")
        extra = [k for k in entry if k not in ("name", "off")]
        if extra:
            c.fail(path + (extra[0],), f"an entry with off: true holds only name, not '{extra[0]}'")
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


def _apply(table: Table, layer: int, text: Optional[str], where: str) -> None:
    """Apply one layer's file to `table` in place."""
    if text is None:
        return
    entries, c = _parse(text, where)
    earlier = {a.name: a for a in table.angles}
    seen = {}
    for i, raw in enumerate(entries):
        entry = _entry(c, i, raw)
        name = entry["name"]
        if name in seen:
            c.fail((i,), f"angle '{name}' is already named on line {seen[name]}; a name is used once per file")
        seen[name] = c.line((i,))
        if "off" in entry:
            if name == CORRECTNESS:
                c.fail((i, "off"), f"{CORRECTNESS} cannot be turned off; extend or replace it instead")
            if name in earlier:
                table.angles = [a for a in table.angles if a.name != name]
            else:
                table.unknown_off.append({"name": name, "file": where, "line": c.line((i,))})
            continue
        if "extends" in entry:
            base = earlier.get(entry["extends"])
            if base is None:
                where_from = ", ".join(LAYERS[:layer]) or "no earlier layer"
                c.fail((i, "extends"), f"extends '{entry['extends']}', but no earlier layer has an angle of that "
                                       f"name (looked in: {where_from})")
            fields = {k: copy.deepcopy(getattr(base, k)) for k in FIELDS}
            fields.update({k: copy.deepcopy(entry[k]) for k in FIELDS if k in entry})
            origin = f"{LAYERS[layer]}, extends {entry['extends']}"
        else:
            fields = {k: copy.deepcopy(entry[k]) for k in FIELDS}
            origin = LAYERS[layer]
        angle = Angle(fields["name"], fields["question"], fields["when"], fields["brief"], origin, where, layer, i)
        at = next((j for j, a in enumerate(table.angles) if a.name == name), None)
        if at is None:
            table.angles.append(angle)
        else:
            table.angles[at] = angle


def load(data_dir=None, repo=None, commit=None, plugin_file=None) -> Table:
    """The angle table: built in, then the user's file under `data_dir`, then the repo's file at `commit` in
    `repo` (both or neither). Raises AngleError (AngleWidenError for a widening key)."""
    if (repo is None) != (commit is None):
        raise AngleError(f"{REPO_FILE}: the repo layer needs both the repo and the commit "
                         f"(got repo={repo!r}, commit={commit!r})")
    plugin = Path(plugin_file) if plugin_file is not None else PLUGIN_FILE
    table = Table([])
    text = _read_file(plugin)
    if text is None:
        raise AngleError(f"{plugin}: the built-in angle file is missing")
    _apply(table, 0, text, str(plugin))
    if table.get(CORRECTNESS) is None:
        raise AngleError(f"{plugin}: the built-in angles must include {CORRECTNESS}")
    if data_dir is not None:
        path = Path(data_dir) / USER_FILE
        _apply(table, 1, _read_file(path), str(path))
    if repo is not None:
        text, where = _read_repo(repo, commit)
        _apply(table, 2, text, where)
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


def _glob_regex(pattern: str) -> str:
    parts = pattern.split("/")
    out = []
    for i, seg in enumerate(parts):
        last = i == len(parts) - 1
        if seg == "**":
            out.append(".+" if last else "(?:[^/]+/)*")
            continue
        rx = "".join("[^/]*" if ch == "*" else "[^/]" if ch == "?" else re.escape(ch) for ch in seg)
        out.append(rx if last else rx + "/")
    return "".join(out)


def glob_match(pattern: str, path: str) -> bool:
    """Whether the repo-relative `path` matches `pattern` as a whole: `**` is any number of folders (a final `**`
    is everything under its folder), `*` and `?` stay inside one path segment, every other character is itself."""
    return re.fullmatch(_glob_regex(pattern), path, re.S) is not None


def _examples(files: list) -> str:
    shown = ", ".join(files[:3])
    return shown + (" (+%d more)" % (len(files) - 3) if len(files) > 3 else "")


def _why(angle: Angle, files: list, found: dict) -> Optional[str]:
    """Why `angle` matches the PR, in one line, or None when it does not."""
    if angle.when == "always":
        return "always"
    reasons = []
    for pattern in angle.when.get("paths", []):
        hit = [f for f in files if glob_match(pattern, f)]
        if hit:
            reasons.append(f"paths {pattern} matched {_examples(hit)}")
    for kind in angle.when.get("kinds", []):
        if kind in found:
            reasons.append(f"kind {kind}: {_examples(found[kind])}")
    return "; ".join(reasons) or None


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
    matched = []
    for a in table.angles:
        if a.name == CORRECTNESS:
            continue
        why = _why(a, files, found)
        if why:
            matched.append((-a.layer, a.pos, a, why))
    matched.sort(key=lambda m: (m[0], m[1]))
    out += [_picked(a, why) for _, _, a, why in matched[:most - 1]]
    for name in FALLBACK:
        if len(out) >= MIN_COUNT:
            break
        a = table.get(name)
        if a is not None and all(p["name"] != name for p in out):
            out.append(_picked(a, "fallback"))
    return out
