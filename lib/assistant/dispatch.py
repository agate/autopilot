"""The dispatcher pass (design 8.0; section 12, "Dispatcher pass"): one short `claude --bg` session per repo group,
driven by the v2 runbook (`builtin/RUNBOOK.md`), that decides what happens next for the cards of that group.

- `groups(data, store)`: the groups a pass may serve, one per distinct `repo` of the projects live cards name (a
  taken-over v1 card is live, a plain imported v1 copy is not), plus the group None (cards with no project, and
  projects with no repo), which runs in an empty folder of its own (`OWN_PREFIX`, outside the data folder).
- `named(store, groups, ids)`: the groups that hold a live card of `ids` (a new round serves only those).
- `settings(data, profile, projects, mode, claude)`: the pass's `--settings`, built like the worker floor.
- `start(data, group, queue, profile, now, mode, why)`: start one pass and record it in `<data>/dispatch.json`
  `{group, projects, short_id, session_id, started_at, profile, queue, why, token, looks, seen_busy, cwd?}`, with a
  spend launch row (kind `dispatcher`). `queue` holds the groups of this round still to serve, `why` the round's
  reasons (pass.json's `why` when the round started), `token` the pass's own (`AP2_PASS` in its env), and `cwd`
  (the group None only) its own folder.
- `finish(data, now)`: a later gate run ends the pass: its turn ended, it was stopped or is gone, it ran past
  DISPATCH_MAX_MIN minutes, or it said `ap2 pass end` (`end`, which writes `<data>/dispatch-end.json`). Waiting at
  a permission prompt opens one `ask` card (`fix`, from `dispatch`) and keeps the stopped session for `claude
  attach`. Then the record is removed, or keeps only the queue and the round's `why`, so the next run starts the
  next group. After `claude --bg` fails, the queue-only record also holds `retry_at` (LAUNCH_RETRY_MIN minutes on):
  no pass starts before it.

The gate calls `finish` early in a run and starts a pass after the pass-due check (`gate._start_dispatch`); it never
waits for a pass, and one pass is open at a time for the whole data folder.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from autopilot import perms as v1_perms
from autopilot import sessions as v1_sessions
from autopilot import workers as v1_workers

from assistant import cards, profiles, projects as projects_module, routines, spend, transcripts
from assistant import workers as workers_module
from assistant.cards import CardError
from assistant.config import ConfigError
from assistant.importv1 import FEED as V1_FEED
from assistant.store import Store, write_json

DISPATCH = "dispatch.json"  # <data>/dispatch.json: the open pass and the round's queue
END = "dispatch-end.json"  # <data>/dispatch-end.json: `ap2 pass end`'s marker, a file of its own (see `end`)
RUN_DIR = Path(transcripts.SESSIONS) / "dispatch"  # the pass's own folder: its settings and its ask texts
SETTINGS = "settings-dispatcher.json"  # `sessions/**/settings-*.json`: in the worker floor's protected files
DISPATCH_MAX_MIN = 30  # a pass still working after this many minutes is stopped
LAUNCH_RETRY_MIN = 15  # after `claude --bg` fails (or hangs), no pass starts for this many minutes (`retry_at`)
LOOKS = 3  # a pass never seen busy has ended once a gate run has seen its turn over this many times (v1: 3 polls)
NAME_PREFIX = "ap2 dispatch · "
ROLE = "dispatcher"  # AP2_ROLE: the Bash guard acts on it; the park hook leaves it alone (not a worker role)
FROM = "dispatch"  # the `from` of the asks the pass and its end open
KIND = "dispatch"  # the kind and feed of the card a pass at a permission prompt opens
NO_PROJECT = "cards with no project"  # the label of the group None
# The group None's pass runs in an empty folder of its own (`tempfile.mkdtemp`), never in the data folder: there a
# relative write passes the Bash guard (it judges only the data-folder paths a command names), and the data folder
# would be the session's project root, whose `.claude/`, `.mcp.json` and `CLAUDE.md` a later session loads.
OWN_PREFIX = "ap2-dispatch-"
# How a pass ended, for its gate.log line (`finish` returns the key as `how`).
HOW = {"ended": "it ran ap2 pass end", "turn": "its turn ended", "gone": "its session is gone",
       "late": "it ran too long, or the clock went back", "blocked": "it waits at a permission prompt"}
RUNBOOK = Path(__file__).resolve().parent / "builtin" / "RUNBOOK.md"
# The outward writes, denied in shadow mode (the projects' outward MCP tools are added to them). git and gh are denied
# whole: a prefix rule matches only the command itself or the command and a space, so a rule per verb misses each
# form that puts an option before the verb (`git -P push`, `git --git-dir=/r/.git push`, `gh -R o/r pr merge 5`,
# `gh --repo=o/r pr ready 5`). In shadow mode the runbook runs no git or gh at all (worktrees, fetches, pushes and
# merges are live only). Best effort, as every text rule: `/usr/bin/git` or `env git` gets past it.
SHADOW_DENY = ("Bash(git:*)", "Bash(gh:*)")
# The built-in tools that act outside the floor and the runbook never uses, denied in every mode: a scheduled or
# remote agent, the user's claude.ai pages, a push to their phone, a message to another session, a workflow, a
# plugin or a design sync. Read, Write, Bash, Glob, Grep and the rest stay.
BUILTIN_DENY = ("RemoteTrigger", "CronCreate", "CronDelete", "ScheduleWakeup", "ArtifactComments", "Artifact",
                "ArtifactData", "PushNotification", "SendMessage", "Workflow", "PublishPlugin", "ShareOnboardingGuide",
                "DesignSync")
# Exactly the ap2 steps the runbook runs, allowed by rule so the auto-mode classifier never judges them: `:*` rules
# for the steps that take words after them, exact rules for `ap2 cap` (so `ap2 cap answer|resume`, `ap2 worker
# answer|end|park|finish` and `ap2 card add` are never allowed by rule).
ALLOWED = ("pass status", "pass end", "card list", "card show", "card ask", "card lane", "worker list", "worker start",
           "template show", "search")
ALLOWED_EXACT = ("cap", "cap --json")
_SHORT = re.compile(r"[A-Za-z0-9_]+\Z")


class DispatchError(ConfigError):
    """A pass cannot start, or a dispatcher record is not usable. Printed by ap2 as one `ap2: ...` line."""


class LaunchError(DispatchError):
    """`claude --bg` failed (or hung until its timeout): no other group is tried in this run (each would wait its
    timeout too), and this group is dropped from the round, as any group that does not start."""


# -- groups ---------------------------------------------------------------------------------------------------------

def _live(c: dict) -> bool:
    """Not done, and not an imported v1 copy: a v1 card with `taken_over` is live (v2 works on it), as in the
    gate's ready rule (`gate._present`)."""
    return c.get("lane") != "done" and not (c.get("feed") == V1_FEED and not c.get("taken_over"))


def groups(data, store: Store) -> tuple:
    """The groups a pass may serve, as `(groups, errors)`: each group `{group, projects}` is one distinct `repo`
    of the projects named by live cards (not done, not an imported v1 copy that is not taken over), with those
    project names sorted;
    the group None holds live cards with no project and projects with no repo, and comes last. A project that does
    not load is one error `{"project", "error"}` and is left out. Raises CardError or OSError when the store does not
    read."""
    data = Path(data)
    found, loaded, errors = {}, {}, []
    for c in store.list():
        if not _live(c):
            continue
        name = c.get("project")
        if name is None or (isinstance(name, str) and cards.blank(name)):
            found.setdefault(None, set())
            continue
        key = name if isinstance(name, str) else repr(name)
        if key not in loaded:
            try:
                loaded[key] = projects_module.load(data, name)
            except ConfigError as e:
                loaded[key] = None
                errors.append({"project": key, "error": str(e)})
        if loaded[key] is not None:
            found.setdefault(loaded[key].get("repo") or None, set()).add(key)
    ordered = sorted(found, key=lambda g: (g is None, g or ""))
    return [{"group": g, "projects": sorted(found[g])} for g in ordered], errors


def members(store: Store, served: list, refused=()) -> set:
    """The ids of the live cards the groups `served` hold (a group's `projects`; the group None also holds the live
    cards with no project), and of the live cards naming a project in `refused` (the ones `groups` left out), keyed
    as `groups` keys them. Raises CardError or OSError when the store does not read."""
    names = {n for g in served for n in (g.get("projects") or [])} | set(refused)
    none = any(g.get("group") is None for g in served)
    out = set()
    for c in store.list():
        if not _live(c):
            continue
        name = c.get("project")
        if name is None or (isinstance(name, str) and cards.blank(name)):
            if none:
                out.add(c["id"])
        elif (name if isinstance(name, str) else repr(name)) in names:
            out.add(c["id"])
    return out


def named(store: Store, served: list, ids) -> list:
    """The groups of `served` that hold a live card of `ids` (as `members` counts them), in order. Raises CardError
    or OSError when the store does not read."""
    ids, keys = set(ids), set()
    for c in store.list():
        if c["id"] not in ids or not _live(c):
            continue
        name = c.get("project")
        if name is None or (isinstance(name, str) and cards.blank(name)):
            keys.add(None)
        else:
            keys.add(name if isinstance(name, str) else repr(name))
    return [g for g in served if (g.get("group") is None and None in keys)
            or any(n in keys for n in g.get("projects") or [])]


def label(group: dict) -> str:
    return ", ".join(group.get("projects") or []) or NO_PROJECT


# -- the floor ------------------------------------------------------------------------------------------------------

def settings(data, profile: dict, projects: list, mode: str, claude: Optional[str] = None) -> dict:
    """The pass's `--settings`, built like the worker floor (`workers.floor`): the data folder's protected files
    (with pass.json, dispatch.json and its end marker) and the package denied for Edit, v1's dispatcher denials
    (`settings/dispatcher.json`: `perms.BASH_DENY`), the outward MCP tools of `projects`' `tools` (v1's
    `denied_tools`, as v1's dispatcher floor), no nested session (`workers.nested_session_denies`: the claude CLI
    in every spelling, the one at `claude` included, and v1's `ap`), the built-in tools in BUILTIN_DENY, the
    park and Bash guard hooks, `AP2_ROLE=dispatcher` in its env, and the profile's overlay merged under it
    (`profiles.merge`), in every mode. In any mode but live it also denies SHADOW_DENY (git and gh whole) and the
    ticket-status tools of `projects`' `tools` (live moves the ticket); `start` also loads no MCP server then
    (`--strict-mcp-config`). Refused (WorkerError) for a path a permission rule cannot name literally
    (`workers.require_literal`)."""
    data = Path(data)
    workers_module.require_literal(data)
    pkg = workers_module.package_root()
    deny = [f"Edit(/{data}/{name})" for name in workers_module.PROTECTED]  # pass.json, DISPATCH and END too
    deny += [f"Edit(/{pkg}/**)"] + list(v1_perms.BASH_DENY)
    tools = sorted({t for p in projects for t in (p.get("tools") or [])})
    deny += v1_perms.denied_tools(tools)
    deny += workers_module.nested_session_denies(claude) + list(BUILTIN_DENY)
    if mode != "live":
        deny += list(SHADOW_DENY) + v1_perms.status_tools(tools)
    deny = list(dict.fromkeys(deny))
    ap2 = pkg / "bin" / "ap2"
    allow = [f"Bash({ap2} {verb}:*)" for verb in ALLOWED] + [f"Bash({ap2} {verb})" for verb in ALLOWED_EXACT]
    hooks = {"PreToolUse": [
        {"matcher": "*", "hooks": [{"type": "command", "command": f"\"{ap2}\" hook-park", "timeout": 5}]},
        {"matcher": "Bash", "hooks": [{"type": "command", "command": f"\"{ap2}\" hook-guard", "timeout": 5}]}]}
    env = {"AP2_ROLE": ROLE, "AP2_DATA": str(data), "AP2_RUN_DIR": str(data / RUN_DIR), "AP2_PROFILE": profile["name"]}
    out = {"permissions": {"deny": deny, "allow": allow}, "env": env, "hooks": hooks}
    return profiles.merge(out, profiles.overlay(profile))


# -- start ----------------------------------------------------------------------------------------------------------

def _claude(claude: Optional[str]) -> str:
    from assistant import scan
    return scan._claude(claude)


def launch_profile(data, now: datetime, transcripts_dir=None, claude: Optional[str] = None, store=None) -> dict:
    """The profile a pass starts on: the active one (as the scan session's). Raises a ProfileError when it does not
    load, or a DispatchError naming the cap's refusal while the cap is paused or at its limit."""
    from assistant import cap

    data = Path(data)
    prof = profiles.active(data)
    projects = Path(transcripts_dir) if transcripts_dir else transcripts.default_projects()
    a = cap.allowance(data, projects, now.astimezone(), prof, listing=lambda: v1_sessions.list_all(_claude(claude)),
                      store=store)
    if a.get("allow") == 0:
        raise DispatchError(f"no dispatcher pass started: {cap.refusal(a)}")
    return prof


def prompt(data, group: dict) -> str:
    where = f"the repo {group['group']}" if group.get("group") else "no repo: an empty folder of its own"
    ap2 = workers_module.package_root() / "bin" / "ap2"
    return (f"Run one ap2 dispatcher pass for: {label(group)} (in {where}). Read {RUNBOOK} first and follow it "
            f"exactly. Run ap2 as {ap2} (that exact path; your allow rules name it). The data folder is {data}.")


def start(data, group: dict, queue: list, profile: dict, now: datetime, mode: str,
          claude: Optional[str] = None, why: Optional[list] = None) -> dict:
    """Start one pass for `group` on `profile`, in the group's repo (for the group None, a new empty folder made by
    `tempfile.mkdtemp` with OWN_PREFIX, kept as the record's `cwd` and removed by `finish`, or here when nothing
    started), and record it in `<data>/dispatch.json` with the rest of the round in `queue` and the round's reasons in
    `why` (the pass reads them with `ap2 pass status`). The pass gets a token of its own, `AP2_PASS` in its env and
    `token` on the record, so `ap2 pass end` from another pass ends nothing. In any mode but live it loads no MCP server
    (`--strict-mcp-config`, no `--mcp-config`). The record is written right after the launch (a record that cannot be
    written stops the session again), then with the session id once the listing shows it (best effort); a spend launch
    row (kind `dispatcher`) follows, and a failure there is the record's `note`. Raises LaunchError when `claude --bg`
    fails, DispatchError (or ConfigError, WorkerError) for this group, when nothing started: also when the launch cannot
    enter the group's repo (an OSError naming it), so the next group is still tried."""
    data, claude = Path(data), _claude(claude)
    projs = [projects_module.load(data, n) for n in group.get("projects") or []]
    cwd = Path(group["group"]) if group.get("group") else None
    if cwd is not None and not cwd.is_dir():
        raise DispatchError(f"no dispatcher pass started for {label(group)}: the repo {cwd} is not a folder")
    s = settings(data, profile, projs, mode, claude=claude)
    run = data / RUN_DIR
    try:
        run.mkdir(parents=True, exist_ok=True)
        write_json(run / SETTINGS, s)
    except OSError as e:
        raise DispatchError(f"cannot write the dispatcher settings in {run}: {e}") from e
    argv = [claude, "--bg", "-n", NAME_PREFIX + label(group), "--permission-mode", workers_module.PERMISSION_MODE,
            "--settings", str(run / SETTINGS)]
    model = profile.get("dispatcher_model") or profile.get("model")
    if model:
        argv += ["--model", model]
    if mode != "live":
        argv.append("--strict-mcp-config")  # no `--mcp-config`: the user's MCP servers and connectors stay unloaded
    argv.append(prompt(data, group))
    token = secrets.token_hex(8)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AUTOPILOT_", "AP2_"))}
    env.update(s["env"])
    env["AP2_PASS"] = token  # the launch env only: the shared settings file never holds a newer pass's token
    own = None
    if cwd is None:
        try:
            own = cwd = Path(tempfile.mkdtemp(prefix=OWN_PREFIX))
        except OSError as e:
            raise DispatchError(f"no dispatcher pass started for {label(group)}: cannot make its empty folder: "
                                f"{e}") from e
    try:
        short = v1_workers._launch(argv, str(cwd), env)
    except OSError as e:
        _remove_own(own)  # nothing started
        if e.filename == str(cwd):  # the repo cannot be entered (no access, or gone since the check): this group's
            raise DispatchError(f"no dispatcher pass started for {label(group)}: cannot enter the repo {cwd}: "
                                f"{e.strerror or e}") from e
        raise LaunchError(f"cannot start the dispatcher pass for {label(group)}: {e}") from e
    except (RuntimeError, subprocess.SubprocessError) as e:
        _remove_own(own)
        raise LaunchError(f"cannot start the dispatcher pass for {label(group)}: {e}") from e
    rec = {"group": group.get("group"), "projects": list(group.get("projects") or []), "short_id": short,
           "session_id": "", "started_at": cards.stamp(now), "profile": profile["name"], "queue": list(queue),
           "why": list(why or []), "token": token, "looks": 0, "seen_busy": False}
    if own is not None:
        rec["cwd"] = str(own)
    try:
        write_json(data / DISPATCH, rec)
    except OSError as e:
        _run(claude, "stop", short)
        _remove_own(own)
        raise DispatchError(f"cannot record the dispatcher pass in {data / DISPATCH}: {e}; the session {short} was "
                            f"stopped") from e
    row = v1_sessions.find_by_id(short, v1_sessions.list_all(claude) or []) or {}
    if row.get("sessionId") or row.get("status") == "busy":
        try:
            write_json(data / DISPATCH, dict(rec, session_id=row.get("sessionId") or "",
                                             seen_busy=row.get("status") == "busy"))
            rec = dict(rec, session_id=row.get("sessionId") or "", seen_busy=row.get("status") == "busy")
        except OSError:  # the first record stays; it is enough to stop the pass
            pass
    try:
        spend.record_launch(data, short, "dispatcher", ",".join(rec["projects"]) or None, profile,
                            session_id=rec["session_id"], at=now)
    except Exception as e:  # noqa: BLE001 - bookkeeping never undoes a started session
        return dict(rec, note=f"the spend launch row was not written: {e}")
    return rec


def _remove_own(own: Optional[Path]) -> None:
    if own is not None:
        shutil.rmtree(own, ignore_errors=True)


def _own_folder(rec: dict) -> Optional[Path]:
    """The group None's own folder on the record `rec` (its `cwd`), or None: only a folder `start` could have made
    (named with OWN_PREFIX, right in the temp folder, not a link), so a record naming any other path removes nothing."""
    cwd = rec.get("cwd")
    if rec.get("group") is not None or not isinstance(cwd, str):
        return None
    path = Path(cwd)
    if not (path.is_absolute() and path.name.startswith(OWN_PREFIX) and not path.is_symlink()
            and os.path.realpath(path.parent) == os.path.realpath(tempfile.gettempdir())):
        return None
    return path


# -- the record -----------------------------------------------------------------------------------------------------

def _group_ok(g) -> bool:
    return (isinstance(g, dict) and (g.get("group") is None or isinstance(g.get("group"), str))
            and isinstance(g.get("projects"), list) and all(isinstance(n, str) for n in g["projects"]))


def read_record(data) -> Optional[dict]:
    """`<data>/dispatch.json`, or None when there is none. A record with a `short_id` is an open pass; one without
    holds only the round's `queue` and `why`, and after a failed launch `retry_at` (a time stamp: no pass starts
    before it). `why` (a list of objects) and `token` may be missing (a record written before they were), and `cwd`
    (a string: the group None's own folder) is there only for that group. Raises ValueError (or OSError) when it is
    not one of these."""
    path = Path(data) / DISPATCH
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    shape = "not a dispatcher record ({group, projects, short_id, session_id, started_at, profile, queue, why})"
    queue = rec.get("queue", []) if isinstance(rec, dict) else None
    if not (isinstance(queue, list) and all(map(_group_ok, queue))):
        raise ValueError(shape)
    why = rec.get("why", [])
    if not (isinstance(why, list) and all(isinstance(w, dict) for w in why)
            and isinstance(rec.get("token", ""), str) and isinstance(rec.get("retry_at", ""), str)):
        raise ValueError(shape)
    if "retry_at" in rec:
        cards.parse_stamp(rec["retry_at"])
    if "short_id" in rec:
        looks = rec.get("looks")
        if not (isinstance(rec["short_id"], str) and _SHORT.match(rec["short_id"]) and _group_ok(rec)
                and isinstance(rec.get("started_at"), str)
                and (looks is None or (isinstance(looks, int) and not isinstance(looks, bool)))
                and isinstance(rec.get("seen_busy", False), bool) and isinstance(rec.get("cwd", ""), str)):
            raise ValueError(shape)
        cards.parse_stamp(rec["started_at"])
    return rec


def end(data, now: datetime) -> dict:
    """`ap2 pass end`: mark the open pass ended at `now`, so the next gate run ends its session at once. The marker is
    its own file (`dispatch-end.json`), since the gate rewrites `dispatch.json` on its runs. Raises DispatchError
    when no pass is open, when `AP2_PASS` is set (a pass runs it) and is not the open pass's token (an older pass,
    one stopped at a prompt and resumed by the user, ends nothing), or when `AP2_ROLE` is set without `AP2_PASS` (a
    worker: Workers strip every AP2_* name, so it never has `AP2_PASS`). Without either (a person at a terminal) it
    ends the open pass."""
    data = Path(data)
    try:
        rec = read_record(data)
    except (OSError, ValueError, RecursionError) as e:
        raise DispatchError(f"{data / DISPATCH}: cannot read the dispatcher record: {e}") from e
    if not rec or "short_id" not in rec:
        raise DispatchError("no dispatcher pass is open")
    mine = os.environ.get("AP2_PASS")
    if os.environ.get("AP2_ROLE") and not mine:
        raise DispatchError("only the open dispatcher pass or a person ends it")
    if mine and mine != rec.get("token"):
        raise DispatchError(f"this session is not the open dispatcher pass ({rec['short_id']}); it ends nothing")
    marker = {"short_id": rec["short_id"], "started_at": rec["started_at"], "ended_at": cards.stamp(now)}
    if rec.get("token"):
        marker["token"] = rec["token"]
    try:
        write_json(data / END, marker)
    except OSError as e:
        raise DispatchError(f"cannot write {data / END}: {e}") from e
    return marker


def _ended(data: Path, rec: dict) -> bool:
    try:
        m = json.loads((data / END).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return False
    return (isinstance(m, dict) and m.get("short_id") == rec["short_id"] and m.get("started_at") == rec["started_at"]
            and m.get("token") == rec.get("token"))


# -- finish ---------------------------------------------------------------------------------------------------------

def _run(claude: str, *args) -> subprocess.CompletedProcess:
    return subprocess.run([claude, *args], capture_output=True, timeout=60, stdin=subprocess.DEVNULL)


def _words(lang: str, short: str, group: dict) -> dict:
    if lang.split("-")[0] == "zh":
        return {"what": f"调度这一轮（{label(group)}）停在一个权限提示上，后台没有人能回答它。",
                "where": f"它的会话 {short} 已停下，但保留着，方便你接上去看；下一次到期的调度会照常开始。",
                "do": f"运行 `claude attach {short}`，看看它卡在哪个提示上；处理完后把这张卡移到「完成」。"}
    return {"what": f"A dispatcher pass ({label(group)}) is waiting at a permission prompt nobody can answer in the "
                    f"background.",
            "where": f"Its session {short} was stopped and kept for you to attach; the next due pass starts as usual.",
            "do": f"Run `claude attach {short}` to see which prompt it stopped at; then move this card to Done."}


def _ask_card(store: Store, short: str, group: dict) -> str:
    """One card for a pass at a permission prompt (keyed `dispatch:<short id>`, so a second look opens none)."""
    key = f"{KIND}:{short}"
    for c in store.list(kind=KIND):
        if c.get("key") == key:
            return c["id"]
    langs = store.languages
    words = {lang: _words(lang, short, group) for lang in langs}
    title = {lang: ("调度停在权限提示上" if lang.split("-")[0] == "zh" else "Dispatcher pass at a prompt")
             for lang in langs}
    card = {"kind": KIND, "lane": "needs", "feed": KIND, "links": [], "pri": "P1", "origin": "found", "key": key,
            "title": title, "short_id": short,
            "ask": {"type": "fix", "from": FROM, "reason": workers_module.ATTACH, "text": words[langs[0]]["do"]},
            "plain": {line: {lang: words[lang][line] for lang in langs} for line in cards.PLAIN_LINES}}
    return store.add(card, by=FROM)["id"]


def finish(data, now: datetime, claude: Optional[str] = None, transcripts_dir=None,
           store: Optional[Store] = None) -> Optional[dict]:
    """Look at the open pass; None when none is open (no record, or only a queue). It ended (`ap2 pass end`), its turn
    ended (state done or failed, or status idle, once a run saw it busy or LOOKS runs saw it so), it was stopped or
    is gone (its process dead), it ran past DISPATCH_MAX_MIN minutes, or it started more than `routines.GRACE` after
    now (an error ending with `routines.WENT_BACK`): it is stopped (`claude stop`), its spend settled, and removed
    (`claude rm`), and with it (or when it is gone) the group None's own folder. Waiting at a permission prompt
    (state blocked, status waiting) opens one ask card instead and is stopped but not removed, for `claude attach`
    (its own folder is kept too). Any end but `ap2 pass end` and the prompt is one error. Then the record keeps only
    the round's queue, or goes. A failed listing waits for the next run (one error once the pass is late). The kept
    queue carries the round's `why`, for each next group's record. Returns `{open, ended, how,
    short_id, group, projects, card?, errors}`."""
    data, claude = Path(data), _claude(claude)
    path = data / DISPATCH
    try:
        rec = read_record(data)
    except (OSError, ValueError, RecursionError) as e:
        path.unlink(missing_ok=True)  # else it would hold every later pass back
        return {"open": False, "ended": False, "how": None, "short_id": None, "group": None, "projects": [],
                "errors": [{"dispatch": True, "error": f"{path}: cannot read the dispatcher record ({e}); it was "
                                                       f"removed"}]}
    if rec is None or "short_id" not in rec:
        return None
    short, sid = rec["short_id"], rec.get("session_id") or ""
    out = {"open": True, "ended": False, "how": None, "short_id": short, "group": rec.get("group"),
           "projects": rec.get("projects") or [], "errors": []}
    name = label(rec)
    started = cards.parse_stamp(rec["started_at"])
    ahead = started - now > routines.GRACE  # a start later than now never grows old, so it counts as late
    over = ahead or now - started >= timedelta(minutes=DISPATCH_MAX_MIN)
    went_back = f"; {routines.WENT_BACK}" if ahead else ""
    listing = v1_sessions.list_all(claude)
    if listing is None:  # unknown, not gone: the next run looks again
        if over:
            out["errors"].append({"dispatch": True, "error": f"cannot list the sessions; the dispatcher pass {short} "
                                                             f"({name}) has been open since {rec['started_at']}"
                                                             f"{went_back}"})
        return out
    s = v1_sessions.find_by_id(short, listing)
    sid = sid or (s or {}).get("sessionId") or ""
    looks = int(rec.get("looks") or 0) + 1
    seen_busy = rec.get("seen_busy") is True or (s is not None and s.get("status") == "busy")
    turn_over = s is not None and (s.get("state") in ("done", "failed") or s.get("status") == "idle")
    if _ended(data, rec):
        how = "ended"
    elif s is not None and s.get("state") == "blocked" and s.get("status") == "waiting":
        how = "blocked"
    elif s is None or s.get("state") == "stopped" or (s.get("pid") is not None
                                                       and not v1_sessions.pid_alive(s.get("pid"))):
        how = "gone"
    elif turn_over and (seen_busy or looks >= LOOKS):
        how = "turn"
    elif over:
        how = "late"
    else:
        if (looks, seen_busy) != (rec.get("looks"), rec.get("seen_busy")):
            write_json(path, dict(rec, looks=looks, seen_busy=seen_busy, session_id=sid))
        return out
    projects = Path(transcripts_dir) if transcripts_dir else transcripts.default_projects()
    try:
        if s is not None:
            _run(claude, "stop", short)
    except (OSError, subprocess.SubprocessError) as e:
        out["errors"].append({"dispatch": True, "error": f"cannot stop the dispatcher pass {short}: {e}; the next "
                                                         f"run tries again"})
        return out
    finally:
        try:
            spend.settle(data, projects, short=short, session_id=sid, listing=lambda: v1_sessions.list_all(claude),
                         now=now)
        except Exception:  # noqa: BLE001 - bookkeeping never stops a finish
            pass
    if how == "blocked":
        if store is not None:
            try:
                out["card"] = _ask_card(store, short, rec)
            except (CardError, OSError) as e:
                out["errors"].append({"dispatch": True, "error": f"the dispatcher pass {short} waits at a permission "
                                                                 f"prompt (`claude attach {short}`), but its card "
                                                                 f"cannot be written: {e}"})
    else:
        removed = s is None  # gone: nothing to remove
        if s is not None:
            try:
                done = _run(claude, "rm", short)
                if done.returncode != 0:
                    raise OSError(f"exit {done.returncode}")
                removed = True
            except (OSError, subprocess.SubprocessError) as e:
                out["errors"].append({"dispatch": True, "error": f"cannot remove the dispatcher session {short}: "
                                                                 f"{e}"})
        if removed:  # the session is gone: so is the group None's own folder (a blocked pass keeps it for attach)
            _remove_own(_own_folder(rec))
    if how == "late" and ahead:
        out["errors"].append({"dispatch": True, "error": f"the dispatcher pass {short} ({name}) started at "
                                                         f"{rec['started_at']}, later than now ({cards.stamp(now)}); "
                                                         f"it was stopped{went_back}"})
    elif how == "late":
        out["errors"].append({"dispatch": True, "error": f"the dispatcher pass {short} ({name}) was still working "
                                                         f"after {DISPATCH_MAX_MIN} minutes; it was stopped"})
    elif how in ("turn", "gone"):
        out["errors"].append({"dispatch": True, "error": f"the dispatcher pass {short} ({name}) ended without `ap2 "
                                                         f"pass end`; it was stopped and removed"})
    (data / END).unlink(missing_ok=True)
    queue = rec.get("queue") or []
    if queue:
        write_json(path, {"queue": queue, "why": rec.get("why") or []})
    else:
        path.unlink(missing_ok=True)
    out.update(open=False, ended=True, how=how)
    return out
