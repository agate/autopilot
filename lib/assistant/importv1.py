"""`ap2 import-v1`: a read-only copy of v1 projects as v2 cards, so v2 can show them in shadow mode (design 10,
12 "v1 import").

- v1's projects are found the way v1 finds them (`lib/autopilot/paths.py`): every `<from>/projects/<name>/` with a
  `config.json`, and a legacy `<from>/<name>/config.json` whose `projects/<name>/config.json` does not exist. A
  name v1's `valid_name` refuses is not a project.
- v1's files are only read, with `open(...).read()` and `json.loads`: never through v1's `state.load` or `mutate`,
  which take a lock file inside v1's folder. Nothing is created, changed or touched under `--from`.
- Each project becomes a `project` card, each task a `code` card under it, each open `ask` or `gate` decision a
  `question` card under its task (else the project). Keys (`v1:<name>`, `v1:<name>:<id>`) make a re-run update the
  same cards; a card is never removed.
- Plain words are made by code from fixed phrases (`PHRASES`, `en` and `zh`), in every configured language.
- A card's title is one text per configured language, the same texts as its `plain.what` (the project card: its
  name in each slot). A re-run moves a card the old import wrote with a one-text title to that shape once.

`--take-over NAME` moves one project to v2 (design 12, "Take over a v1 project"). The project must be paused in v1
first (`ap pause NAME`: v1's `paused` file, or `stopped: true`) with no v1 worker listed and no answer v1 has not
acted on yet (`handling: true`); v2 never pauses v1 and v1's folder is only read (`os.scandir`, `os.stat`, and
copies from it). Its cards turn live (`taken_over`, the branch, worktree, needs, spec and saved sessions v2's
workers continue), every decision gets a card (settled ones `done`, with the answer), the whole project folder is
copied to `<data>/v1/<name>/` and each open task's run folder into its card's run folder (same bytes and mtime,
through a temp file linked in place, or renamed in place after a check that the destination is absent where the file
system has no hard links; never overwritten, never removed, v1's links skipped, a link on the v2 side refused), and
`<data>/projects/<name>.yaml` is written from config.json when it does not exist (its values are checked only then).
A card with `taken_over` is never rebuilt from v1, and the plain import skips a project whose project card has it.
"""
from __future__ import annotations

import copy
import errno
import filecmp
import hashlib
import io
import json
import os
import re
import shutil
import string
from pathlib import Path
from typing import Optional

from autopilot import paths as v1_paths

from assistant import cards, config, datadir, projects, transcripts, yamlish
from assistant.cards import CardError
from assistant.config import ConfigError
from assistant.store import Store, check_writable
from assistant.workers import ROLES, _slug

FEED = "v1-import"
BY = "v1-import"
PRI = "P2"
LANE = {"todo": "planned", "in_progress": "running", "in_review": "running", "blocked": "needs",
        "done": "done", "skipped": "done"}
FINISHED = ("done", "skipped")
ASK = {"ask": "answer", "gate": "approve"}
# The fields a re-run sets on a card that has the key, when they changed (as `engine.build` does).
UPDATED = ("title", "plain", "lane", "status", "ask", "source")
TASK_RECORD = ("pr", "branch", "depends_on", "link")
PROJECT_RECORD = ("repo", "branch_pattern", "branch_prefix")
# The fields a take-over also sets on a card that has the key (beyond UPDATED): what v2's workers continue from.
LIVE = ("taken_over", "branch", "worktree", "needs", "spec", "sessions")
# The v1 config.json keys the take-over writes into `<data>/projects/<name>.yaml` (`projects.DEFAULTS`, no `tools`).
PROJECT_FILE_KEYS = ("repo", "branch_pattern", "branch_prefix", "resources", "health_checks", "extra_hosts",
                     "mcp_servers")
RECORD = "v1"  # <data>/v1/<name>/: the copy of a taken-over project's folder
V1_LOCK = "state.lock"  # v1's lock file, the one file of the project folder not copied
TAKEN_OVER = "taken over from v1"
# What `git check-ref-format` refuses in a branch name and a v1 task id or branch_pattern may give: white space or
# a control character, `:`, `~`, `^`, `?`, `*`, `[`, `\`, `..` and `@{`; a leading `-`, `/` or `.`; a trailing `/`
# or `.`; `//`; a part starting with `.` (`/.`); a part ending in `.lock`.
BAD_BRANCH = re.compile(r"[\s\x00-\x1f\x7f:~^?*\[\\]|\.\.|@\{|^[-/.]|[/.]$|//|/\.|\.lock(/|$)")
# The temp file a copy or the project file is written to first, next to it, then linked in place: a write cut short
# never leaves a part file at the final name. Its name is short and fixed (`_temp`), so a long v1 name still fits.
PART = ".ap2-part"
# What `os.link` raises on a file system without hard links (exFAT, FAT, some network shares): the temp file is
# then renamed into place after a check that the destination is still absent.
NO_LINK = {getattr(errno, n) for n in ("ENOTSUP", "EOPNOTSUPP", "EPERM", "EXDEV") if hasattr(errno, n)}
# The longest text a v1 label may give, in characters: a template longer than this is shown as its key (before it
# is parsed), and a label that would fill longer is shown as written. v1's own labels are about 100 characters.
MAX_LABEL = 1_000

# The code-made plain words, one entry per language. A language is matched exactly, then by its first part
# (zh-Hans -> zh), as `engine.phrases` does.
PHRASES = {
    "en": {
        "project_where": "The v1 autopilot project {project}.",
        "task_where": "Task {task} of the v1 project {project}. Its v1 status is \"{status}\".",
        "question_where": "A question v1 asks in the project {project}{about}.",
        "about": ", about task {task}",
        "do": "Nothing here: this is a read-only copy of v1. Act on it on v1's own page.",
        "do_question": "Answer it on v1's own page: this is a read-only copy, and an answer here changes nothing.",
        "do_closed": "Nothing: v1 has settled this question. This is a read-only copy of v1.",
        "do_live": "v2 works on this now; v1 is paused for this project.",
        "do_question_live": "Answer it here: v2 works on this project now, and v1 is paused for it.",
        "do_settled": "Nothing: this question is settled. v2 works on this project now; v1 is paused for it.",
    },
    "zh": {
        "project_where": "v1 的 autopilot 项目 {project}。",
        "task_where": "v1 项目 {project} 里的任务 {task}。它在 v1 里的状态是“{status}”。",
        "question_where": "v1 在项目 {project} 里提的问题{about}。",
        "about": "，关于任务 {task}",
        "do": "这里不用做什么：这是 v1 的只读副本，请到 v1 自己的页面上处理。",
        "do_question": "请到 v1 自己的页面上回答：这是只读副本，在这里回答不会改变任何东西。",
        "do_closed": "不用做什么：v1 已经处理完这个问题。这是 v1 的只读副本。",
        "do_live": "现在由 v2 处理这件事；v1 已经为这个项目暂停。",
        "do_question_live": "请在这里回答：现在由 v2 处理这个项目，v1 已经为它暂停。",
        "do_settled": "不用做什么：这个问题已经处理完。现在由 v2 处理这个项目，v1 已经为它暂停。",
    },
}


class V1ImportError(ConfigError):
    """The import cannot start: no v1 folder, an unknown project, a language without phrases. One `ap2:` line."""


class _Refused(Exception):
    """One project cannot be read; the others still import."""


def phrases(lang: str) -> Optional[dict]:
    return PHRASES.get(lang) or PHRASES.get(lang.split("-")[0])


# -- v1's text ------------------------------------------------------------------------------------------------------

class _Keep(dict):
    """format_map's arguments: a placeholder with no argument stays as written, as v1's page leaves it."""

    def __missing__(self, key):
        return "{" + key + "}"


class Labels:
    """v1's label texts for `{key, args}` titles: the package's `templates/labels.<lang>.json`, then the v1 data
    folder's `labels.<lang>.json` on top (where v1's page reads its translations). A file that cannot be read is
    no labels, as v1's own reader treats it."""

    def __init__(self, root: Path):
        self.root = root
        self.tables = {}

    def table(self, lang: str) -> dict:
        if lang not in self.tables:
            out = {}
            for path in (v1_paths.package_root() / "templates" / f"labels.{lang}.json", self.root / f"labels.{lang}.json"):
                got = _read_quietly(path)
                if isinstance(got, dict):
                    out.update({k: v for k, v in got.items() if isinstance(v, str)})
            self.tables[lang] = out
        return self.tables[lang]

    def fill(self, label: dict, lang: str) -> str:
        key = label.get("key")
        if not isinstance(key, str):
            return cards.shown(key)
        args = label.get("args") if isinstance(label.get("args"), dict) else {}
        for each in dict.fromkeys((lang, lang.split("-")[0], "en")):
            template = self.table(each).get(key)
            if template is not None:
                if len(template) > MAX_LABEL:
                    return str(key)  # too long to parse or to show as written
                size = _filled_size(template, args)
                if size is None:
                    return template  # a format v1's page would not apply
                if size > MAX_LABEL:
                    return template  # one that would fill too long
                try:
                    return template.format_map(_Keep(args))
                except (ValueError, IndexError, KeyError, AttributeError, TypeError):
                    return template  # a brace or a format v1's page would print as written
        return str(key)


def _filled_size(template: str, args: dict) -> Optional[int]:
    """How many characters `template` has once filled from `args` (a placeholder without an argument counts as
    written), so a label repeating one field cannot build a huge string. None when a `{...}` is not a bare name:
    a format spec (`{id:>9}`), a conversion (`{id!r}`), an index or attribute (`{id[0]}`, `{id.real}`), none of
    which v1's page applies. A template that does not parse is None too. It stops as soon as the size passes
    MAX_LABEL (the size so far is returned), and turns each argument into text once."""
    size = 0
    lengths = {}
    try:
        for literal, field, spec, conversion in string.Formatter().parse(template):
            size += len(literal)
            if field is not None:
                if spec or conversion is not None or "." in field or "[" in field:
                    return None
                if field not in lengths:
                    lengths[field] = len(str(args[field])) if field in args else len(field) + 2
                size += lengths[field]
            if size > MAX_LABEL:
                return size
    except ValueError:
        return None
    return size


def _read_quietly(path: Path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.loads(f.read())
    except (OSError, ValueError, RecursionError):
        return None


def text_for(value, lang: str, labels: Labels) -> str:
    """One v1 text field in one language: `{en, zh, ...}` gives the language, else its base language, else `en`,
    else its first non-blank string, else ""; `{key, args}` is filled from v1's labels; a plain string is itself.
    Non-blank is `cards.blank`, so a slot of only U+FEFF and white space is skipped like a slot of spaces, and words
    next to U+FEFF are returned as they are."""
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    if "key" in value:
        return labels.fill(value, lang)
    for each in (lang, lang.split("-")[0], "en"):
        got = value.get(each)
        if not cards.blank(got):
            return got
    return next((v for v in value.values() if not cards.blank(v)), "")


# -- finding and reading v1's projects ------------------------------------------------------------------------------

def source_root(given: Optional[str]) -> Path:
    """`--from`, else v1's own data folder (`AUTOPILOT_DATA`, else ~/.claude/autopilot-data), absolute."""
    if given == "":
        raise V1ImportError("--from is empty; give v1's data folder")
    raw = None
    try:
        # Inside the try: `~nosuchuser` in --from, or a home folder that cannot be found, raises RuntimeError here.
        raw = Path(given).expanduser() if given is not None else v1_paths.data_root()
        root = raw.resolve()
        # Inside the try: a folder that cannot be looked into or a name too long raises here, not in resolve().
        is_dir = root.is_dir()
    except (OSError, RuntimeError) as e:
        named = raw if raw is not None else given
        where = f"the v1 data folder {named}" if named is not None else "the default v1 data folder"
        raise V1ImportError(f"cannot use {where}: {e}") from e
    if not is_dir:
        raise V1ImportError(f"no v1 data folder at {root} (give it with --from DIR)")
    return root


def _has_config(folder: Path) -> bool:
    """Whether `folder` holds config.json. A folder that cannot be looked into counts as found, so
    `read_project` refuses that one project with its reason and the others still import."""
    try:
        os.stat(folder / "config.json")
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True
    return True


def find(root: Path) -> dict:
    """The v1 projects under `root`, as {name: folder}, by name. v1's `ap_dir` rule: the folder in projects/
    counts once it holds config.json; before that a legacy top-level folder with config.json is the project."""
    found = {}
    projects = root / "projects"
    try:
        listed = sorted(projects.iterdir()) if projects.is_dir() else []
        legacy = sorted(root.iterdir())
    except OSError as e:
        raise V1ImportError(f"cannot list the v1 data folder {root}: {e}") from e
    for d in listed:
        if v1_paths.valid_name(d.name) and _has_config(d):
            found[d.name] = d
    # A name already found has projects/<name>/config.json, which shadows its legacy folder.
    for d in legacy:
        if d.name not in found and v1_paths.valid_name(d.name) and _has_config(d):
            found[d.name] = d
    return dict(sorted(found.items()))


def _read_object(folder: Path, name: str) -> dict:
    path = folder / name
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        raise _Refused(f"{name} missing") from None
    except (OSError, UnicodeDecodeError) as e:
        raise _Refused(f"cannot read {name}: {e}") from None
    try:
        obj = json.loads(text)
    except RecursionError:
        raise _Refused(f"{name} is nested too deeply") from None
    except ValueError as e:
        raise _Refused(f"{name} is not JSON: {e}") from None
    if not isinstance(obj, dict):
        raise _Refused(f"{name} is not a JSON object")
    return obj


def _items(state: dict, field: str) -> list:
    items = state.get(field, [])
    if not isinstance(items, list):
        raise _Refused(f"state.json: {field} is not a list")
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise _Refused(f"state.json: {field}[{i}] is not an object")
        # cards.blank: an id that is not text, or holds only white space and U+FEFF, is no id.
        if cards.blank(item.get("id")):
            raise _Refused(f"state.json: {field}[{i}] has no id")
    return items


def read_project(folder: Path) -> tuple:
    """(config, tasks, decisions) of one v1 project. Raises _Refused with the reason."""
    cfg = _read_object(folder, "config.json")
    state = _read_object(folder, "state.json")
    # v1 cannot load a state.json without tasks either; read as none, a done project would turn running again.
    if "tasks" not in state:
        raise _Refused("state.json: tasks missing")
    tasks = _items(state, "tasks")
    # v1 cannot load a state.json without decisions; read as none, every open question card would be closed.
    if "decisions" not in state:
        raise _Refused("state.json: decisions missing")
    decisions = _items(state, "decisions")
    seen = set()
    for t in tasks + decisions:
        if t["id"] in seen:
            raise _Refused(f"state.json: the id {t['id']} is used twice")
        seen.add(t["id"])
    for t in tasks:
        if not (isinstance(t.get("status"), str) and t["status"] in LANE):
            raise _Refused(f"state.json: task {t['id']} has the unknown status {cards.shown(t.get('status'))}")
    return cfg, tasks, decisions


# -- the cards ------------------------------------------------------------------------------------------------------

class _Maker:
    def __init__(self, name: str, folder: Path, langs: tuple, labels: Labels, live: Optional[dict] = None):
        """`live` (a take-over): {at, project (its setup, for a branch), from (the file that setup came from),
        record (`<data>/v1/<name>`)}."""
        self.name, self.folder, self.langs, self.labels, self.live = name, folder, langs, labels, live

    def texts(self, value, fallback: str) -> dict:
        """`value`'s text in each configured language (`text_for`), stripped, or `fallback` when it is blank
        (`cards.blank`): a text of only U+FEFF and white space gives `fallback` like an empty one."""
        out = {}
        for lang in self.langs:
            text = text_for(value, lang, self.labels)
            out[lang] = fallback if cards.blank(text) else text.strip()
        return out

    def card(self, kind: str, key: str, lane: str, what: dict, where: dict, do: dict, record: dict,
             parent_key: Optional[str], **more) -> dict:
        card = {"kind": kind, "lane": lane, "feed": FEED, "links": [], "pri": more.pop("pri", PRI),
                "origin": more.pop("origin", "source"), "title": dict(what), "key": key,
                "project": self.name, "plain": {"what": what, "where": where, "do": do},
                "source": {"name": FEED, "record": record}}
        card.update(more)
        return {"key": key, "kind": kind, "parent_key": parent_key, "card": card}

    def say(self, line: str, **values) -> dict:
        return {lang: phrases(lang)[line].format(**values) for lang in self.langs}

    def project(self, cfg: dict, tasks: list) -> dict:
        lane = "done" if tasks and all(t["status"] in FINISHED for t in tasks) else "running"
        record = {k: cfg.get(k) for k in PROJECT_RECORD}
        record["folder"] = str(self.folder)
        return self._mark(self.card("project", f"v1:{self.name}", lane, {lang: self.name for lang in self.langs},
                                    self.say("project_where", project=self.name),
                                    self.say("do_live" if self.live else "do"), record, None))

    def _mark(self, entry: dict) -> dict:
        if self.live:
            entry["card"]["taken_over"] = self.live["at"]
        return entry

    def task(self, t: dict) -> dict:
        where = {lang: phrases(lang)["task_where"].format(task=t["id"], project=self.name, status=t["status"])
                 for lang in self.langs}
        entry = self.card("code", f"v1:{self.name}:{t['id']}", LANE[t["status"]], self.texts(t.get("title"), t["id"]),
                          where, self.say("do_live" if self.live else "do"), {k: t.get(k) for k in TASK_RECORD},
                          f"v1:{self.name}", pri="P1" if t.get("critical") is True else PRI,
                          origin="found" if t.get("origin") == "found" else "source", status=t["status"])
        if self.live:
            self._work(t, self._mark(entry)["card"])
        return entry

    def _work(self, t: dict, card: dict) -> None:
        """What v2's workers continue from: v1's branch (an open task without one gets one from the project's
        pattern and the v1 id, never the card key, whose colons git refuses), worktree, needs, spec and saved
        sessions, in the shape `Workers._save_session` writes. Raises _Refused on a session v2 cannot resume."""
        branch, worktree = _given(t.get("branch")), _given(t.get("worktree"))
        if branch is None and t["status"] not in FINISHED:
            try:
                branch = projects.branch(self.live["project"], t["id"], _slug(card["title"][self.langs[0]]))
            except ConfigError as e:
                raise _Refused(f"task {t['id']}: {self.live['from']}: {e}") from None
            if BAD_BRANCH.search(branch):
                raise _Refused(f"state.json: task {t['id']}: its branch {cards.shown(branch)} (from the project's "
                               f"branch_pattern and the task id) is not one git takes; give the task a branch in v1")
        needs = list(t["needs"]) if isinstance(t.get("needs"), list) else []
        card.update({k: v for k, v in (("branch", branch), ("worktree", worktree)) if v is not None}, needs=needs)
        spec = _given(t.get("spec"))
        if spec is not None:
            card["spec"] = self._spec(spec)
        saved = t.get("sessions")
        if saved is None:
            return
        if not isinstance(saved, dict):
            raise _Refused(f"state.json: task {t['id']}: sessions is not an object")
        out = {}
        for role, sid in saved.items():
            if role not in ROLES:
                raise _Refused(f"state.json: task {t['id']}: sessions names the role {cards.shown(role)} "
                               f"(known: {', '.join(ROLES)})")
            if not (isinstance(sid, str) and transcripts.SESSION_ID.match(sid)):
                raise _Refused(f"state.json: task {t['id']}: {cards.shown(sid)} is not a session id")
            out[role] = {"id": sid, "ids": [sid], "cwd": worktree or "", "needs": needs, "state": "ended",
                         "why": TAKEN_OVER, "at": self.live["at"]}
        if out:
            card["sessions"] = out

    def _spec(self, spec: str) -> str:
        """A spec path inside the v1 project folder points at its copy under `<data>/v1/<name>/`; any other is
        kept as given."""
        if not os.path.isabs(spec):
            return spec
        normal = Path(os.path.normpath(spec))
        # The spec as written and its real path (a link in it, such as a `--from` reached through one), against
        # `--from`'s project folder and its real path. Nothing is read through it beyond `os.path.realpath`.
        for path in dict.fromkeys((normal, Path(os.path.realpath(spec)))):
            for base in dict.fromkeys((self.folder, self.folder.resolve())):
                try:
                    return str(self.live["record"] / path.relative_to(base))
                except ValueError:
                    continue
        return spec

    def question(self, d: dict, task_ids: set, open_: bool) -> dict:
        task = d.get("task") if isinstance(d.get("task"), str) and d.get("task") in task_ids else None
        where = {lang: phrases(lang)["question_where"].format(
            project=self.name, about=phrases(lang)["about"].format(task=task) if task else "") for lang in self.langs}
        parent = f"v1:{self.name}:{task}" if task else f"v1:{self.name}"
        record = {"kind": d.get("kind"), "task": d.get("task"), "status": d.get("status"),
                  "created_at": d.get("created_at")}
        do = ("do_question_live" if open_ else "do_settled") if self.live else ("do_question" if open_ else "do_closed")
        ask = {"type": ASK[d["kind"]], "from": "v1"} if open_ else None
        if self.live:
            record.update(answer=d.get("answer"), answered_at=d.get("answered_at"), body=d.get("body"),
                          options=d.get("options"), links=d.get("links"), handling=d.get("handling"),
                          action=d.get("action"))
            body = self.texts(d.get("body"), "")[self.langs[0]]
            if ask is not None and not cards.blank(body):
                ask["text"] = body  # what v2's page shows with the ask
        return self._mark(self.card("question", f"v1:{self.name}:{d['id']}", "needs" if open_ else "done",
                                    self.texts(d.get("title"), d["id"]), where, self.say(do), record, parent,
                                    ask=ask))

    def kept(self, old, new: dict) -> dict:
        """`new` with `old`'s non-blank texts in the languages that are not configured. Non-blank is `cards.blank`, so
        a text of only U+FEFF and white space is dropped like an empty one, and words next to U+FEFF are kept as
        they are."""
        extra = {k: v for k, v in old.items() if isinstance(k, str) and k not in self.langs
                 and not cards.blank(v)} if isinstance(old, dict) else {}
        return {**new, **extra}

    def closed(self, old: dict) -> dict:
        """The closed question card for `old`, a question card whose decision has left state.json: as `question`
        makes it with `open_=False`, keeping the old card's words, `where`, source and parent. Each language's
        `what` (and title slot) is the old `plain.what` text when it is not blank (`cards.blank`), else the old title
        as `cards.title_for` picks it when that is not blank (a one-text title the old import wrote as it is; an
        object title's non-blank text slot in that language, then the primary's, then the first other one in sorted
        key order), else the decision id. Blank is `cards.blank` in both checks, so old `plain.what` words or a
        one-text title of only U+FEFF are skipped too. Each configured language's `where` is the old text when it is
        not blank (`cards.blank`), else the fresh `question_where` phrase, so an old slot that is missing, not a text,
        or only U+FEFF and white space gets the phrase. In a language no longer configured, the old `what` and `where`
        words are kept, as v1 cannot make them again, and `do` is the closed phrase (none in a language without
        phrases)."""
        plain = old.get("plain") if isinstance(old.get("plain"), dict) else {}
        what = plain.get("what") if isinstance(plain.get("what"), dict) else {}
        where = plain.get("where") if isinstance(plain.get("where"), dict) else {}
        fresh = self.say("question_where", project=self.name, about="")
        source = old.get("source") if isinstance(old.get("source"), dict) else {}
        decision = old["key"].rsplit(":", 1)[-1]
        words = {}
        for lang in self.langs:
            if not cards.blank(what.get(lang)):
                words[lang] = what[lang]
                continue
            text = cards.title_for(old, lang, self.langs[0])  # a one-text title is returned as it is, blank or not
            words[lang] = decision if cards.blank(text) else text
        what = self.kept(what, words)
        where = self.kept(where, {lang: fresh[lang] if cards.blank(where.get(lang)) else where[lang]
                                  for lang in self.langs})
        do = self.say("do_closed")
        for lang in sorted({*what, *where, *self.kept(plain.get("do"), {})} - set(self.langs)):
            if phrases(lang):  # the old `do` may still say to go answer it: write the closed phrase instead
                do[lang] = phrases(lang)["do_closed"]
        if self.live:
            do = {lang: phrases(lang)["do_settled"] for lang in do}
        entry = self._mark(self.card("question", old["key"], "done", what, where, do, source.get("record"), None,
                                     ask=None))
        if old.get("parent"):
            entry["card"]["parent"] = old["parent"]
        return entry


def _given(value) -> Optional[str]:
    """A v1 text field that is set: a non-blank text, else None."""
    return None if cards.blank(value) else value


def plan(name: str, folder: Path, langs: tuple, labels: Labels, existing: dict, own: list,
         live: Optional[dict] = None) -> tuple:
    """The cards one project makes, parents first, and how many decisions it does not import. `existing` is this
    feed's cards by key; `own` is the keys in it under this project (`v1:<name>:...`), so closing looks only at
    those. Each card is checked as the store will check it, so a project is refused before any of its cards is
    written. With `live` (a take-over, see `_Maker`) the cards are live and every decision makes one. Raises
    _Refused."""
    cfg, tasks, decisions = read_project(folder)
    make = _Maker(name, folder, langs, labels, live)
    out = [make.project(cfg, tasks)] + [make.task(t) for t in tasks]
    task_ids = {t["id"] for t in tasks}
    skipped = 0
    for d in decisions:
        asks = isinstance(d.get("kind"), str) and d["kind"] in ASK
        is_open = asks and d.get("status") == "open"
        if live:  # a take-over brings every decision: the settled ones as done cards, with the answer
            out.append(make.question(d, task_ids, is_open))
            continue
        if not is_open:
            skipped += 1
            # A question card made while it was open is closed when v1 settles it; no card is made for it otherwise.
            if asks and f"v1:{name}:{d['id']}" in existing:
                out.append(make.question(d, task_ids, False))
            continue
        out.append(make.question(d, task_ids, True))
    # v1 removes a decision from state.json only when it archives it, and it archives only settled ones: a question
    # card still open whose decision is gone is closed too, and so is a closed one whose title is still one text.
    ids = task_ids | {d["id"] for d in decisions}
    prefix = f"v1:{name}:"
    for key in sorted(own):
        old = existing[key]
        if (key.startswith(prefix) and key[len(prefix):] not in ids and old.get("kind") == "question"
                and (old.get("lane") != "done" or old.get("ask") is not None or isinstance(old.get("title"), str))):
            out.append(make.closed(old))
    for entry in out:
        try:
            cards.validate(entry["card"], langs)
            check_writable(entry["card"])  # the store's own depth bound, before any card is copied or written
        except CardError as e:
            raise _Refused(f"{entry['key']}: {e}") from None
    return out, skipped


# -- the import -----------------------------------------------------------------------------------------------------

def _cards_by_key(store: Store, dry_run: bool) -> dict:
    """This feed's cards that have a key, the oldest first on a tie. A dry run with no card file yet does not
    touch the store (its lock file included), as `engine` does."""
    if dry_run and not any((store.data / "items").glob("*/*.json")):
        return {}
    out = {}
    for c in store.list():
        if c.get("feed") == FEED and isinstance(c.get("key"), str):
            out.setdefault(c["key"], c)
    return out


def run(data: Path, source: Optional[str] = None, names: Optional[list] = None, dry_run: bool = False,
        take_over: Optional[str] = None) -> dict:
    """Import the v1 projects under `source` (or only `names`) into the card store in `data`; return the report.
    With `take_over`, move that one project to v2 instead (`_take_over`). With `dry_run` nothing is written.
    Raises V1ImportError (nothing written) or CardError (the store failed)."""
    root = source_root(source)
    data = Path(data)
    # datadir's own test: letter case folded, then the same folder by file identity (symlinks, firmlinks).
    try:
        overlap = datadir._inside(data, root) or datadir._inside(root, data)
    except (OSError, RuntimeError) as e:
        raise V1ImportError(f"cannot use the data folder {data}: {e}") from e
    if overlap:
        raise V1ImportError(f"data folder {data} and the v1 data folder {root} overlap (one is inside the other); "
                            f"the import never writes under --from")
    # The projects folder may be a symlink to a folder outside --from: the data folder must not overlap that either.
    # A dangling link counts too (lexists, not exists): its target may be the data folder ensure is about to create.
    projects = root / "projects"
    try:
        real = projects.resolve() if os.path.lexists(projects) else None
        overlap = real is not None and (datadir._inside(data, real) or datadir._inside(real, data))
    except (OSError, RuntimeError) as e:
        raise V1ImportError(f"cannot use the v1 projects folder {projects}: {e}") from e
    if overlap:
        raise V1ImportError(f"data folder {data} and the v1 projects folder {real} overlap (one is inside the "
                            f"other); the import never writes under --from")
    # A projects folder that cannot be looked into (a link into a locked folder) is one line too, also where exists()
    # above says False instead of raising. A dangling link, or one to a non-folder, stops the import: its name was
    # compared above, but a differently spelled name for the same place (Unicode forms, firmlinks) cannot be ruled
    # out until the place exists, and ensure would create it. v1 cannot use such a projects folder either.
    try:
        if real is not None:
            os.stat(real)
    except (FileNotFoundError, NotADirectoryError) as e:
        raise V1ImportError(f"cannot use the v1 projects folder {projects}: it is a link to {real}, "
                            f"which is not a folder") from e
    except OSError as e:
        raise V1ImportError(f"cannot use the v1 projects folder {projects}: {e}") from e
    cfg = config.load(data)
    langs = cards.languages(cfg)
    missing = [lang for lang in langs if phrases(lang) is None]
    if missing:
        raise V1ImportError(f"no code-made plain words for {', '.join(missing)} yet (built in: {', '.join(PHRASES)}); "
                            f"the store would refuse every card")
    found = find(root)
    if take_over is not None:
        names = [take_over]
    # A project folder may be a symlink to a folder outside --from: the data folder must not overlap that either,
    # for every project found, not only the ones --project names (an unnamed one is still a v1 folder).
    # A folder whose real place cannot be found (a link through a folder the import cannot look into, where resolve()
    # stops early) stops the import: an overlap cannot be ruled out. os.stat checks resolve() did reach a real place.
    for name, folder in found.items():
        try:
            real = folder.resolve()
            os.stat(real)
            overlap = datadir._inside(data, real) or datadir._inside(real, data)
        except (OSError, RuntimeError) as e:
            raise V1ImportError(f"cannot use the v1 project {name}'s folder {folder}: {e}") from e
        if overlap:
            raise V1ImportError(f"data folder {data} and the v1 project {name}'s folder {real} overlap (one is "
                                f"inside the other); the import never writes under a v1 project")
    if names:
        unknown = [n for n in dict.fromkeys(names) if n not in found]
        if unknown:
            raise V1ImportError(f"no v1 project named {', '.join(unknown)} in {root} "
                                f"(found: {', '.join(found) or 'none'})")
        found = {n: f for n, f in found.items() if n in names}
    store = Store(data, cfg=cfg)
    labels = Labels(root)
    report = {"from": str(root), "dry_run": dry_run, "projects": [], "refused": [], "taken_over": []}
    if take_over is not None:
        report["projects"].append(_take_over(store, take_over, found[take_over], langs, labels, dry_run))
        return report
    if not dry_run:
        datadir.ensure(data)
    existing = _cards_by_key(store, dry_run)
    # Each project's keys, grouped once: a v1 name never holds ":", so `v1:<name>:<id>` splits at the first one.
    by_project = {}
    for key in existing:
        if key.startswith("v1:") and ":" in key[3:]:
            by_project.setdefault(key[3:].split(":", 1)[0], []).append(key)
    for name, folder in found.items():
        if (existing.get(f"v1:{name}") or {}).get("taken_over"):
            report["taken_over"].append(name)  # v2 works on it now: never rebuilt from v1
            continue
        try:
            entries, skipped = plan(name, folder, langs, labels, existing, by_project.get(name, []))
        except _Refused as e:
            report["refused"].append({"name": name, "folder": str(folder), "reason": str(e)})
            continue
        report["projects"].append({"name": name, "folder": str(folder),
                                   "cards": [_apply(store, existing, e, name, dry_run) for e in entries],
                                   "not_imported": {"decisions": skipped}})
    return report


def _apply(store: Store, existing: dict, entry: dict, name: str, dry_run: bool, take_over: bool = False) -> dict:
    """Add or update one card (nothing when `dry_run`); its line in the report. A card with `taken_over` is never
    rebuilt from v1. With `take_over`, a card the read-only import made turns live (`action: take_over`)."""
    key, new = entry["key"], copy.deepcopy(entry["card"])
    out = {"key": key, "kind": entry["kind"]}
    old = existing.get(key)
    if old is None:
        parent = existing.get(entry["parent_key"]) if entry["parent_key"] else None
        if parent is not None and parent.get("id"):
            new["parent"] = parent["id"]
        out["action"] = "add"
        if dry_run:
            out.update(card=None, would_add=new)
            existing[key] = new
        else:
            added = store.add(new, by=BY)
            out["card"] = added["id"]
            existing[key] = added
        return out
    if old.get("taken_over"):
        out.update(action="unchanged", card=old.get("id"))
        return out
    changes = {k: new[k] for k in UPDATED + (LIVE if take_over else ()) if k in new and old.get(k) != new[k]}
    out.update(action="take_over" if take_over else "update" if changes else "unchanged", card=old.get("id"))
    if changes and dry_run:
        out["would_change"] = sorted(changes)
        existing[key] = {**old, **changes}
    elif changes:
        existing[key] = store.update(old["id"], changes, by=BY,
                                     did=f"{'take over' if take_over else 'import'} v1 {name}")
    return out


# -- the take-over --------------------------------------------------------------------------------------------------

def _take_over(store: Store, name: str, folder: Path, langs: tuple, labels: Labels, dry_run: bool) -> dict:
    """Move one v1 project to v2; its entry in the report. Every check runs before anything is written (the data
    folder's layout included): v1 must show the pause and list no worker (a dry run only reports both), the
    project must read, its saved sessions and setup must be ones v2 takes, and no link may sit where a copy goes.
    Then the project folder is copied, the project file written, the cards turned live, and each open task's run
    folder copied into its card's run folder. Raises V1ImportError (or ConfigError) with one line."""
    data = store.data

    def stop(reason) -> None:
        raise V1ImportError(f"cannot take over the v1 project {name}: {reason}")

    existing = _cards_by_key(store, True)  # reads only: nothing is created before the checks pass
    try:
        cfg, tasks, decisions = read_project(folder)
        state = _read_object(folder, "state.json")
    except _Refused as e:
        stop(e)
    listed = state.get("workers", [])
    if not isinstance(listed, list):
        stop("state.json: workers is not a list")
    working = [f"{w.get('task')} {w.get('role')}" if isinstance(w, dict) else cards.shown(w) for w in listed]
    # An answer v1 took but has not acted on yet (`ap decision resolve ... --handling`): its next pass would act on it,
    # then `ap decision handled` drops the flag. Paused, no v1 pass runs, and a settled card would never act on it.
    handling = [d["id"] for d in decisions if d.get("handling") is True]
    # How v1 records the pause (`ap pause` writes `paused`; `stopped: true` in config.json), read as v1 reads it.
    try:
        paused = (folder / "paused").exists() or cfg.get("stopped") is True
    except OSError as e:
        stop(f"cannot look for its paused file: {e}")
    if not dry_run and not paused:
        stop(f"it is not paused in v1; run `ap pause {name}` in v1 first, so v1 and v2 never both work on it")
    if not dry_run and working:
        stop(f"v1 still lists a worker on it ({', '.join(working)}); run `ap worker end {name} TASK ROLE` in v1 "
             f"for each first, so its session is saved on the task")
    if not dry_run and handling:
        stop(f"v1 has not acted on the answer{'s' if len(handling) > 1 else ''} to {', '.join(handling)} yet; run "
             f"`ap resume {name}` in v1, let one pass act on {'them' if len(handling) > 1 else 'it'}, then "
             f"`ap pause {name}` and take over again")
    pfile = projects.path(data, name)
    seen = set()
    _refuse_link(pfile, data, seen)
    has_file = os.path.lexists(pfile)
    # config.json's values matter only when the project file is written: a kept one is never checked against them.
    text, values = ("", {}) if has_file else _project_file(cfg, stop)
    setup = projects.load(data, name) if has_file else dict(copy.deepcopy(projects.DEFAULTS), **values, name=name)
    record = data / RECORD / name
    live = {"at": cards.stamp(store.clock()), "project": setup, "from": str(pfile) if has_file else "config.json",
            "record": record}
    try:
        entries, _ = plan(name, folder, langs, labels, existing, [k for k in existing if k.startswith(f"v1:{name}:")],
                          live=live)
    except _Refused as e:
        stop(e)
    ops = _walk(folder, record, data, seen, skip=(V1_LOCK,))
    runs = []  # (card key, the v1 run folder) of each open task; none when v1's runs/ is a link (never followed)
    for t in tasks if not os.path.islink(folder / "runs") else ():
        src = folder / "runs" / t["id"]
        if (t["status"] not in FINISHED and t["id"] not in (".", "..") and Path(t["id"]).name == t["id"]
                and src.is_dir() and not src.is_symlink()):
            runs.append((f"v1:{name}:{t['id']}", src))
    for key, src in runs:
        if key in existing:  # a card made later has no run folder yet: it is checked when it is copied
            ops += _walk(src, transcripts.run_dir(data, existing[key]["id"]), data, seen)
    # A new card's run folder is walked only once it has an id, after the writes: `sessions/`, which will hold it,
    # is checked now, so every link check runs before anything is written.
    _refuse_link(data / transcripts.SESSIONS, data, seen)
    files = {"copied": [], "same": [], "kept": [], "skipped": []}
    if not dry_run:
        datadir.ensure(data)
    _copy(ops, folder, data, files, dry_run)
    project_file = "kept" if has_file else "write" if dry_run else "written"
    if has_file and not dry_run:
        try:
            _clear_temp(pfile)  # one a run killed after its link left
        except OSError as e:
            raise V1ImportError(f"cannot remove {_temp(pfile)}: {e}") from e
    if project_file == "written":
        try:
            pfile.parent.mkdir(exist_ok=True)
            _place(pfile, io.BytesIO(text.encode("utf-8")))  # a file made meanwhile is never overwritten
        except OSError as e:
            raise V1ImportError(f"cannot write {pfile}: {e}") from e
    added = {k for k in (key for key, _ in runs) if k not in existing}
    report = [_apply(store, existing, e, name, dry_run, take_over=True) for e in entries]
    if not dry_run:
        for key, src in runs:
            if key in added:
                _copy(_walk(src, transcripts.run_dir(data, existing[key]["id"]), data, seen), folder, data, files,
                      False)
    files["skipped"] = list(dict.fromkeys(files["skipped"]))
    return {"name": name, "folder": str(folder), "cards": report, "not_imported": {"decisions": 0},
            "take_over": {"paused": paused, "workers": working, "handling": handling}, "files": files,
            "project_file": project_file}


def _project_file(cfg: dict, stop) -> tuple:
    """(text, values) of `<data>/projects/<name>.yaml` from v1's config.json: one `key: <JSON>` line per key of
    PROJECT_FILE_KEYS that is set (null and "" are not), JSON being inside the YAML subset's flow values. Each value
    is checked by `projects.load`'s rules and the text must read back to the same values, else `stop`."""
    values = {}
    for key in PROJECT_FILE_KEYS:
        value = cfg.get(key)
        if value is None or value == "":
            continue
        if key in PROJECT_RECORD:
            ok, want = isinstance(value, str), "a text"
        elif key == "health_checks":
            ok = isinstance(value, list) and all(isinstance(c, dict) and isinstance(c.get("name"), str)
                                                 and isinstance(c.get("command"), str) for c in value)
            want = "a list of {name, command} maps"
        else:
            ok, want = isinstance(value, list) and all(isinstance(v, str) and v for v in value), "a list of names"
        if not ok:
            stop(f"config.json: {key} must be {want} for v2's project file, not {cards.shown(value)}")
        try:
            json.dumps(value, ensure_ascii=False).encode("utf-8")
        except UnicodeEncodeError:  # a lone surrogate json.loads takes from a `\ud800` escape
            stop(f"config.json: {key} holds text that is not valid Unicode, which v2's project file cannot hold")
        values[key] = value
    text = "# From v1's config.json by `ap2 import-v1 --take-over`; yours to edit.\n" + "".join(
        f"{k}: {json.dumps(v, ensure_ascii=False)}\n" for k, v in values.items())
    try:
        back = yamlish.load(text)
    except (yamlish.YamlError, RecursionError):
        back = None
    if back != values:
        stop("config.json: a value does not read back from v2's project file the same; write the project file "
             "by hand first")
    return text, values


def _refuse_link(path: Path, data: Path, seen: set) -> None:
    """Raise V1ImportError when `path` or a folder between it and the data folder is a link: a copy never writes
    through one. `seen` holds the folders already checked."""
    for p in (path, *path.parents):
        if p == data or p in seen:
            return
        if os.path.islink(p):
            raise V1ImportError(f"{p} is a link; the take-over never writes through a link in the data folder "
                                f"{data} (move it away)")
        if p != path:
            seen.add(p)


def _walk(src: Path, dst: Path, data: Path, seen: set, skip=()) -> list:
    """The copy of the tree at `src` to `dst`: ("dir", None, dst), ("file", src, dst) and ("skip", src, None) for
    a link or anything not a file or folder (never followed). A folder comes before what it holds; `skip` names
    left out at the top. Every destination is checked for links (`_refuse_link`). Only reads `src`."""
    ops, todo = [], [(src, dst)]
    while todo:
        s, d = todo.pop()
        ops.append(("dir", None, d))
        try:
            with os.scandir(s) as it:
                listed = sorted(it, key=lambda e: e.name)
        except OSError as e:
            raise V1ImportError(f"cannot read the v1 folder {s}: {e}") from e
        for e in listed:
            if s == src and e.name in skip:
                continue
            if e.is_dir(follow_symlinks=False):
                todo.append((Path(e.path), d / e.name))
            elif e.is_file(follow_symlinks=False) and not e.name.endswith(PART):  # PART: the copy's own temp name
                ops.append(("file", Path(e.path), d / e.name))
            else:
                ops.append(("skip", Path(e.path), None))
    for _, _, d in ops:
        if d is not None:
            _refuse_link(d, data, seen)
    return ops


def _copy(ops: list, folder: Path, data: Path, files: dict, dry_run: bool) -> None:
    """Carry out `_walk`'s ops (with `dry_run`, only report them): a destination that exists is never written,
    `same` when its bytes are the source's, else `kept`. The copy keeps the mtime (`_place`), so an old result file
    never looks newer than a v2 start."""
    for kind, src, dst in ops:
        try:
            if kind == "skip":
                files["skipped"].append(str(src.relative_to(folder)))
            elif kind == "dir":
                if not dry_run:
                    dst.mkdir(parents=True, exist_ok=True)
            elif os.path.lexists(dst):
                if not dry_run:
                    _clear_temp(dst)  # one a run killed after its link left
                same = dst.is_file() and not dst.is_symlink() and filecmp.cmp(src, dst, shallow=False)
                files["same" if same else "kept"].append(str(dst.relative_to(data)))
            else:
                files["copied"].append(str(dst.relative_to(data)))
                if not dry_run:
                    fd = os.open(src, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))  # v1's links never followed
                    with os.fdopen(fd, "rb") as f:
                        _place(dst, f, like=src)
        except OSError as e:
            raise V1ImportError(f"cannot copy {src or ''} to {dst}: {e}") from e


def _temp(dst: Path) -> Path:
    """The temp file of `dst`, next to it: `.` + the first 16 hex digits of the SHA-256 of its name + PART, 26 bytes
    whatever the name's length (a name of up to 255 bytes, the most a file system takes, would not fit with PART
    added). Ending in PART, it is never copied from v1 (`_walk`)."""
    return dst.with_name("." + hashlib.sha256(os.fsencode(dst.name)).hexdigest()[:16] + PART)


def _clear_temp(dst: Path) -> None:
    """Remove the temp file of `dst` when it is there (unlinked, a link never followed)."""
    tmp = _temp(dst)
    if os.path.lexists(tmp):
        os.unlink(tmp)


def _place(dst: Path, source, like: Optional[Path] = None) -> None:
    """Write the bytes of the open file `source` to `dst`, which must not exist: into its temp file (`_temp`) next
    to it, then `os.link` puts it at `dst` (FileExistsError when `dst` was made meanwhile: never overwritten). On a
    file system without hard links (`os.link` raises an errno in NO_LINK) the temp is renamed to `dst` with
    `os.replace` after a check that `dst` is still absent (FileExistsError when it is not).
    With `like`, the temp takes `like`'s mode and mtime first (`shutil.copystat`). The temp file is removed in every
    case, so a write cut short (a full disk, Ctrl-C) leaves nothing at `dst`; one a killed run left is replaced, never
    written through (it is unlinked, then made new with O_EXCL). One left next to a `dst` that exists (a run killed
    after the link) is removed by the next run (`_clear_temp`)."""
    _clear_temp(dst)
    tmp = _temp(dst)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o666)
    try:
        with os.fdopen(fd, "wb") as f:
            shutil.copyfileobj(source, f)
        if like is not None:
            shutil.copystat(like, tmp, follow_symlinks=False)
        try:
            os.link(tmp, dst)
        except OSError as e:
            if e.errno not in NO_LINK:  # FileExistsError included: `dst` made meanwhile
                raise
            if os.path.lexists(dst):
                raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(dst)) from e
            os.replace(tmp, dst)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
