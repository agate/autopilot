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
"""
from __future__ import annotations

import copy
import json
import os
import string
from pathlib import Path
from typing import Optional

from autopilot import paths as v1_paths

from assistant import cards, config, datadir
from assistant.cards import CardError
from assistant.config import ConfigError
from assistant.store import Store, check_writable

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
    },
    "zh": {
        "project_where": "v1 的 autopilot 项目 {project}。",
        "task_where": "v1 项目 {project} 里的任务 {task}。它在 v1 里的状态是“{status}”。",
        "question_where": "v1 在项目 {project} 里提的问题{about}。",
        "about": "，关于任务 {task}",
        "do": "这里不用做什么：这是 v1 的只读副本，请到 v1 自己的页面上处理。",
        "do_question": "请到 v1 自己的页面上回答：这是只读副本，在这里回答不会改变任何东西。",
        "do_closed": "不用做什么：v1 已经处理完这个问题。这是 v1 的只读副本。",
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
    else its first non-blank string; `{key, args}` is filled from v1's labels; a plain string is itself."""
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    if "key" in value:
        return labels.fill(value, lang)
    for each in (lang, lang.split("-")[0], "en"):
        got = value.get(each)
        if isinstance(got, str) and got.strip():
            return got
    return next((v for v in value.values() if isinstance(v, str) and v.strip()), "")


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
        if not (isinstance(item.get("id"), str) and item["id"].strip()):
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
    def __init__(self, name: str, folder: Path, langs: tuple, labels: Labels):
        self.name, self.folder, self.langs, self.labels = name, folder, langs, labels

    def texts(self, value, fallback: str) -> dict:
        return {lang: (text_for(value, lang, self.labels).strip() or fallback) for lang in self.langs}

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
        return self.card("project", f"v1:{self.name}", lane, {lang: self.name for lang in self.langs},
                         self.say("project_where", project=self.name), self.say("do"), record, None)

    def task(self, t: dict) -> dict:
        where = {lang: phrases(lang)["task_where"].format(task=t["id"], project=self.name, status=t["status"])
                 for lang in self.langs}
        return self.card("code", f"v1:{self.name}:{t['id']}", LANE[t["status"]], self.texts(t.get("title"), t["id"]),
                         where, self.say("do"), {k: t.get(k) for k in TASK_RECORD}, f"v1:{self.name}",
                         pri="P1" if t.get("critical") is True else PRI,
                         origin="found" if t.get("origin") == "found" else "source", status=t["status"])

    def question(self, d: dict, task_ids: set, open_: bool) -> dict:
        task = d.get("task") if isinstance(d.get("task"), str) and d.get("task") in task_ids else None
        where = {lang: phrases(lang)["question_where"].format(
            project=self.name, about=phrases(lang)["about"].format(task=task) if task else "") for lang in self.langs}
        parent = f"v1:{self.name}:{task}" if task else f"v1:{self.name}"
        record = {"kind": d.get("kind"), "task": d.get("task"), "status": d.get("status"),
                  "created_at": d.get("created_at")}
        return self.card("question", f"v1:{self.name}:{d['id']}", "needs" if open_ else "done",
                         self.texts(d.get("title"), d["id"]), where, self.say("do_question" if open_ else "do_closed"),
                         record, parent, ask={"type": ASK[d["kind"]], "from": "v1"} if open_ else None)

    def closed(self, old: dict) -> dict:
        """The closed question card for `old`, a question card whose decision has left state.json: as `question`
        makes it with `open_=False`, keeping the old card's words, `where`, source and parent. Each language's
        `what` (and title slot) is the old `plain.what` text, else the old title in that language (a one-text title
        the old import wrote, or an object title), else the decision id."""
        plain = old.get("plain") if isinstance(old.get("plain"), dict) else {}
        what = plain.get("what") if isinstance(plain.get("what"), dict) else {}
        where = plain.get("where") if isinstance(plain.get("where"), dict) else {}
        fresh = self.say("question_where", project=self.name, about="")
        source = old.get("source") if isinstance(old.get("source"), dict) else {}
        decision = old["key"].rsplit(":", 1)[-1]
        words = {lang: (what[lang] if isinstance(what.get(lang), str) and what[lang].strip()
                        else cards.title_for(old, lang, self.langs[0]) or decision) for lang in self.langs}
        entry = self.card("question", old["key"], "done", words,
                          {lang: where.get(lang) or fresh[lang] for lang in self.langs}, self.say("do_closed"),
                          source.get("record"), None, ask=None)
        if old.get("parent"):
            entry["card"]["parent"] = old["parent"]
        return entry


def plan(name: str, folder: Path, langs: tuple, labels: Labels, existing: dict, own: list) -> tuple:
    """The cards one project makes, parents first, and how many decisions it does not import. `existing` is this
    feed's cards by key; `own` is the keys in it under this project (`v1:<name>:...`), so closing looks only at
    those. Each card is checked as the store will check it, so a project is refused before any of its cards is
    written. Raises _Refused."""
    cfg, tasks, decisions = read_project(folder)
    make = _Maker(name, folder, langs, labels)
    out = [make.project(cfg, tasks)] + [make.task(t) for t in tasks]
    task_ids = {t["id"] for t in tasks}
    skipped = 0
    for d in decisions:
        asks = isinstance(d.get("kind"), str) and d["kind"] in ASK
        is_open = asks and d.get("status") == "open"
        if not is_open:
            skipped += 1
            # A question card made while it was open is closed when v1 settles it; no card is made for it otherwise.
            if asks and f"v1:{name}:{d['id']}" in existing:
                out.append(make.question(d, task_ids, False))
            continue
        out.append(make.question(d, task_ids, True))
    # v1 removes a decision from state.json only when it archives it, and it archives only settled ones: a question
    # card still open whose decision is gone is closed too.
    ids = task_ids | {d["id"] for d in decisions}
    prefix = f"v1:{name}:"
    for key in sorted(own):
        old = existing[key]
        if (key.startswith(prefix) and key[len(prefix):] not in ids and old.get("kind") == "question"
                and (old.get("lane") != "done" or old.get("ask") is not None)):
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


def run(data: Path, source: Optional[str] = None, names: Optional[list] = None, dry_run: bool = False) -> dict:
    """Import the v1 projects under `source` (or only `names`) into the card store in `data`; return the report.
    With `dry_run` nothing is written. Raises V1ImportError (nothing written) or CardError (the store failed)."""
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
    if not dry_run:
        datadir.ensure(data)
    store = Store(data, cfg=cfg)
    existing = _cards_by_key(store, dry_run)
    labels = Labels(root)
    report = {"from": str(root), "dry_run": dry_run, "projects": [], "refused": []}
    # Each project's keys, grouped once: a v1 name never holds ":", so `v1:<name>:<id>` splits at the first one.
    by_project = {}
    for key in existing:
        if key.startswith("v1:") and ":" in key[3:]:
            by_project.setdefault(key[3:].split(":", 1)[0], []).append(key)
    for name, folder in found.items():
        try:
            entries, skipped = plan(name, folder, langs, labels, existing, by_project.get(name, []))
        except _Refused as e:
            report["refused"].append({"name": name, "folder": str(folder), "reason": str(e)})
            continue
        report["projects"].append({"name": name, "folder": str(folder),
                                   "cards": [_apply(store, existing, e, name, dry_run) for e in entries],
                                   "not_imported": {"decisions": skipped}})
    return report


def _apply(store: Store, existing: dict, entry: dict, name: str, dry_run: bool) -> dict:
    """Add or update one card (nothing when `dry_run`); its line in the report."""
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
    changes = {k: new[k] for k in UPDATED if k in new and old.get(k) != new[k]}
    out.update(action="update" if changes else "unchanged", card=old.get("id"))
    if changes and dry_run:
        out["would_change"] = sorted(changes)
        existing[key] = {**old, **changes}
    elif changes:
        existing[key] = store.update(old["id"], changes, by=BY, did=f"import v1 {name}")
    return out
