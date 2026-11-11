"""Workers: one `claude --bg -n "<card> · <role>"` session per card and role (design 8.1, 8.2).

A worker ends a round by writing its result file in the card's run directory (`<data>/sessions/<card>/`):
`handback.json` (implement), `review.json` (review) or `prloop.json` (pr). `check()` looks at every worker and
acts on what it sees, so no process ever waits for a human:

- done (the result file is newer than the start): stop it, save the session on the card, archive the transcript.
- question (`question.json` is newer than the start): the same, and the question becomes the card's `ask`
  (`type: answer`); `answer()` resumes the session with the answer in the prompt.
- dead (no session, or its process is gone): resume once with "you were interrupted"; dead twice: stop and set the
  card's `ask` (`fix`).
- blocked (waiting at a permission prompt): set the card's `ask` (`fix`) to `claude attach`; after
  BLOCKED_MIN minutes stop it, keeping it resumable (`paused`).
- idle (its turn ended with no result file): after IDLE_MIN minutes stop it and mark it paused, can resume.
- parked (stopped itself at the usage line): keeps its slot and ports; resumes once usage allows.

Starting (or resuming) a session can make branches and PRs, so it needs live mode; listing, classifying, ending
and archiving work in shadow mode. v1 is reused by import (`lib/autopilot/workers.py`, `sessions.py`,
`budget.py`, `park.py`, `perms.py`) and never changed.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from autopilot import budget as v1_budget
from autopilot import config as v1_config
from autopilot import park as v1_park
from autopilot import perms as v1_perms
from autopilot import sessions as v1_sessions
from autopilot import workers as v1_workers

from assistant import cards, config, projects, transcripts
from assistant.cards import CardError
from assistant.resources import MAX_WORKERS, PORT_BASE, Registry, WorkerError, check_start, next_ports
from assistant.store import Store, write_json

RESULT_FILES = dict(v1_sessions.RESULT_FILES)  # implement: handback.json, review: review.json, pr: prloop.json
ROLES = tuple(RESULT_FILES)
AGENTS = dict(v1_workers.AGENTS)  # the pr role runs the PR loop command instead of an agent
PR_COMMAND = "/autopilot:pr-loop "
QUESTION = "question.json"
PERMISSION_MODE = "auto"  # v1's default `permission_mode`
EFFORT = "xhigh"  # v1's default `worker_effort`
IDLE_MIN = 60  # a worker idle this long with no result file is stopped and marked paused, can resume
BLOCKED_MIN = 30  # a worker waiting at a permission prompt this long is stopped, kept resumable
PARK_RULES = dict(v1_config.DEFAULT_GLOBAL["budget"])  # park at 90%, resume 5 minutes after the reset
INTERRUPTED = ("You were interrupted. Check your worktree and run files first, then continue the task from where "
               "you stopped.")
BY = "ap2 worker"
# The longest the tool discovery may take to reach its init event (seconds; v1's `perms.discover` allows 180).
DISCOVER_TIMEOUT = 180
DISCOVER_PROMPT = "Reply with the single word OK."
ATTACH = "permission_prompt"  # the `reason` of the ask a worker at a permission prompt raises
DIED = "died_twice"  # the `reason` of the ask a worker that died twice raises; only the user closes it
# The worker asks a role can have on its card, by kind (see `_worker_ask_kind`):
#   attach   - waiting at a permission prompt: `claude attach` it. Closed by every stop, every start and finish.
#   paused   - stopped after BLOCKED_MIN minutes at that prompt: resume it. Closed by every start and finish.
#   question - the worker's question (type answer). Closed by every start and finish; never by a stop, since the
#              question path stops the session first and then raises it.
WORKER_ASKS = ("attach", "paused", "question")
# Every way a worker is closed (`Workers._finish_close`): path -> (the state saved on the card, the line's `did`,
# drop_on_error). drop_on_error drops the record when a step after the stop fails, where a kept record would be
# misread as dead and resumed (end and both pauses); the other paths keep it, so the next check closes it again.
CLOSES = {"end": ("ended", "ended", True), "done": ("ended", "ended", False),
          "question": ("asked", "asked", False), "died": ("stopped", "stopped", False),
          "idle": ("paused", "paused", True), "blocked": ("paused", "paused", True),
          "parked_failed": ("stopped", "stopped", False)}
# Files a worker may not edit in the data folder. Every card's run directory stays writable except its settings
# files: a deny rule cannot keep one worker out of another card's result files. The user's templates and sources
# are here as well (the worker floor in roles.py).
PROTECTED = ("config.yaml", "workers.json", ".workers.lock", ".store.lock", "index.sqlite", "items/**",
             "restore/**", "projects/**", "templates/**", "sources/**", "sessions/**/settings-*.json")


def package_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def session_name(card_id: str, role: str) -> str:
    return f"{card_id} · {role}"


def _slug(text, words: int = 3) -> str:
    parts = re.findall(r"[a-z0-9]+", (text or "").casefold())
    return "-".join(parts[:words]) or "work"


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill the process group `proc` leads (started with `start_new_session`): a `claude` launcher that does not
    exec, the real claude it runs, and the MCP servers and hooks they started all go."""
    killpg = getattr(os, "killpg", None)
    if killpg is None:  # not POSIX: only the direct child
        if proc.poll() is None:
            proc.kill()
        return
    try:
        killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _server_rule(name: str) -> str:
    """The deny rule for a whole MCP server, keyed the way Claude Code keys it: every character outside
    `[A-Za-z0-9_-]` becomes `_`; for a `claude.ai ` server the runs of `_` are then squeezed to one and a leading or
    trailing `_` is dropped (`claude.ai Jira & Confluence` -> `mcp__claude_ai_Jira_Confluence`)."""
    key = re.sub(r"[^A-Za-z0-9_-]", "_", name)
    if name.startswith("claude.ai "):
        key = re.sub(r"^_|_$", "", re.sub(r"_+", "_", key))
    return f"mcp__{key}"


def _not_connected(event: dict) -> list:
    """The server-level deny rule (`_server_rule`) of every MCP server the init event lists with a status other
    than `connected`: its tools are not in the event yet, but the worker's session may connect it later."""
    servers = event.get("mcp_servers")
    rules = []
    for s in servers if isinstance(servers, list) else []:
        if isinstance(s, dict) and s.get("name") and s.get("status") != "connected":
            rules.append(_server_rule(str(s["name"])))
    return list(dict.fromkeys(rules))


# Characters a gitignore pattern reads as syntax. Claude Code matches Read/Edit rules as gitignore patterns, so a
# path holding one (or ending in a space, which gitignore strips) would make the floor's Edit denials match nothing.
PATTERN_CHARS = "[]*?\\!#"


def require_literal(data: Path) -> None:
    """Refuse (WorkerError) when the data folder or the package path is one a gitignore pattern cannot name
    literally: the floor fails closed instead of writing denials that match nothing."""
    for what, path in (("data folder", str(data)), ("package folder", str(package_root()))):
        if any(c in path for c in PATTERN_CHARS) or path.endswith(" "):
            raise WorkerError(f"the {what} {path} has a character a permission rule reads as a pattern "
                              f"({' '.join(PATTERN_CHARS)}) or ends in a space, so the worker's floor cannot deny "
                              "edits in it; use a folder name without those characters")


def floor(role: str, project: dict, data: Path, env: dict) -> dict:
    """The worker's permission floor (`--settings`): v1's outward-tool denials (`perms.denied_tools`, from the
    project's `tools`; `Workers._tools` fills them in when it lists none, with `deny_servers`, the whole-server
    rules) and Bash denials, the data folder's own files and the package, and the worker's env. Refused for a path
    a gitignore pattern cannot name literally (`require_literal`)."""
    if role not in ROLES:
        raise WorkerError(f"role must be one of {', '.join(ROLES)}, not {role!r}")
    require_literal(data)
    deny = v1_perms.denied_tools(list(project.get("tools") or []))
    deny += list(project.get("deny_servers") or [])  # whole servers `_tools` found not connected at init
    deny += [f"Edit(/{data}/{name})" for name in PROTECTED]
    deny += [f"Edit(/{package_root()}/**)"] + list(v1_perms.BASH_DENY)
    # The park command is the one step a parking worker takes; the auto-mode classifier must not judge it (v1 does
    # the same for `ap worker park`).
    allow = [f"Bash({package_root() / 'bin' / 'ap2'} worker park:*)"]
    return {"permissions": {"deny": deny, "allow": allow}, "env": dict(env)}


class Workers:
    def __init__(self, data, cfg: Optional[config.Config] = None, claude: Optional[str] = None, transcripts_dir=None,
                 clock: Callable[[], datetime] = cards.now_utc, max_workers: int = MAX_WORKERS,
                 port_base: int = PORT_BASE, store: Optional[Store] = None):
        """`data` is the data folder; `claude` the claude CLI (default: the one on PATH); `transcripts_dir` the
        folder Claude Code keeps conversations in (default `~/.claude/projects`, read only); `clock()` the time."""
        self.data = Path(data)
        self.cfg = cfg if cfg is not None else config.load(self.data)
        self.claude = claude or shutil.which("claude") or "claude"
        self.transcripts_dir = Path(transcripts_dir) if transcripts_dir else transcripts.default_projects()
        self.clock = clock
        self.max_workers = max_workers
        self.port_base = port_base
        self.store = store or Store(self.data, self.cfg, clock)
        self.registry = Registry(self.data)

    # -- helpers ---------------------------------------------------------------------------------------------------

    def run_dir(self, card_id: str) -> Path:
        return transcripts.run_dir(self.data, card_id)

    def _card(self, card_id: str) -> dict:
        try:
            return self.store.get(card_id)
        except CardError as e:
            raise WorkerError(str(e)) from None

    def _project(self, card: dict) -> dict:
        name = card.get("project")
        if name is None:
            return dict(projects.DEFAULTS, name=None)
        return projects.load(self.data, name)

    @staticmethod
    def _find(records: list, card_id: str, role: str):
        return next((r for r in records if r["card"] == card_id and r["role"] == role), None)

    def _kill(self, short: str) -> None:
        """Run `claude stop` on a worker's session. Every stop goes through here; it raises when the call timed
        out or could not run. A timeout kills only the `claude stop` client: the stop it sent may still land, so
        after a raise the session may be stopped or still running."""
        if short:
            subprocess.run([self.claude, "stop", short], capture_output=True, timeout=60, stdin=subprocess.DEVNULL)

    def _stop(self, card_id: str, role: str, short: str, by: str) -> None:
        """Stop a worker's session, and the worker's own attach ask goes with it: the session it names is gone (a
        pause sets its resume hint after this)."""
        self._kill(short)
        self._close_worker_asks(card_id, role, by, kinds=("attach",))

    def _env(self, card: dict, role: str, project: dict, port_range: list, branch: str) -> dict:
        checks = [{"name": c["name"], "command": c["command"], "fix": c.get("fix") or c["command"]}
                  for c in project.get("health_checks") or []]
        return {"AP2_ROLE": role, "AP2_DATA": str(self.data), "AP2_CARD": card["id"],
                "AP2_PROJECT": project.get("name") or "", "AP2_RUN_DIR": str(self.run_dir(card["id"])),
                "AP2_PORTS": f"{port_range[0]}-{port_range[1]}", "AP2_BRANCH": branch,
                "AP2_EXTRA_HOSTS": ",".join(project.get("extra_hosts") or []),
                "AP2_MCP_SERVERS": ",".join(project.get("mcp_servers") or []),
                "AP2_HEALTH_CHECKS": json.dumps(checks, ensure_ascii=False)}

    @staticmethod
    def _process_env(env: dict) -> dict:
        # v1's hooks act on AUTOPILOT_ROLE / AUTOPILOT_DATA; a v2 worker never carries them, so v1 never takes it
        # for one of its own.
        out = {k: v for k, v in os.environ.items() if not k.startswith(("AUTOPILOT_", "AP2_"))}
        out.update(env)
        return out

    def _prompt(self, prompt: str, card: dict, role: str, project: dict, port_range: list, branch: str) -> str:
        run = self.run_dir(card["id"])
        hosts = ", ".join(project.get("extra_hosts") or [])
        lines = [prompt.rstrip(), "",
                 f"Your card: {card['id']}. Your role: {role}. Your project: {project.get('name') or '-'}. "
                 f"Your branch: {branch}. Your port range: {port_range[0]}-{port_range[1]}. "
                 f"Your run directory: {run}."]
        if hosts:
            lines.append(f"Hosts you may reach besides the usual ones: {hosts}.")
        lines.append(f"End your round by writing {RESULT_FILES[role]} in your run directory. If you need the user, "
                     f'write {QUESTION} there ({{"text": "your question"}}) and end your turn: nobody answers a '
                     "background session, and the answer comes back as a new prompt.")
        lines.append(f"At the usage limit, park: {package_root() / 'bin' / 'ap2'} worker park {card['id']} {role} "
                     '--note "<where you are and the next step>", then end your turn.')
        text = "\n".join(lines)
        return PR_COMMAND + text if role not in AGENTS else text

    def _discover(self, cwd: str, env: dict) -> tuple:
        """(tools, whole-server deny rules) of a session in `cwd` with `env`: one `claude -p ... --output-format
        stream-json`, read line by line and killed at its first init event, before the model runs, so nothing in
        the folder can make it call a tool. It runs in its own process group, and every kill ends the whole group.
        Raises RuntimeError (no init event, also past DISCOVER_TIMEOUT) or OSError."""
        argv = [self.claude, "-p", DISCOVER_PROMPT, "--output-format", "stream-json", "--verbose"]
        proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, env=self._process_env(env),
                                start_new_session=True)
        late = threading.Event()

        def deadline():
            late.set()
            if proc.returncode is None:  # not reaped yet, so the group id is still this one
                _kill_group(proc)

        timeout = DISCOVER_TIMEOUT
        timer = threading.Timer(timeout, deadline)
        timer.daemon = True
        timer.start()
        try:
            for line in iter(proc.stdout.readline, ""):
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("type") == "system" and event.get("subtype") == "init":
                    _kill_group(proc)  # stopped at its init event: no model reply, so no tool call, ever happens
                    proc.wait()
                    return list(v1_perms.parse_discovery(line)), _not_connected(event)
            code = proc.wait()
            if late.is_set():
                raise RuntimeError(f"no init event within {timeout} s")
            raise RuntimeError(f"no init event (claude exited {code}; is claude logged in?)")
        finally:
            timer.cancel()
            _kill_group(proc)
            proc.wait()
            proc.stdout.close()

    def _tools(self, project: dict, cwd: str, env: dict) -> dict:
        """The project with the exact tools the worker's own session will load, for the permission floor. The
        tools the project file lists (`projects/**` is Edit-denied) are used as they are, always first. Otherwise
        they are found on every floor written, in the worker's own folder `cwd` with its env (so the folder's
        local-scope and `.mcp.json` servers and the plugins are seen), by a session killed at its init event
        (`_discover`), with `deny_servers`: every MCP server not connected at that init event, denied whole. Never
        from a card. A worker never starts without its outward tools denied: no list, no start."""
        if project.get("tools"):
            return project
        name = project.get("name")
        where = f"projects/{name}.yaml `tools`" if name else "its projects/<name>.yaml `tools`"
        ask = "" if name else "give the card a project and "
        try:
            tools, servers = self._discover(cwd, env)
        except (OSError, RuntimeError, subprocess.SubprocessError) as e:
            raise WorkerError(f"cannot list the session's tools for the permission floor: {e}; {ask}list them in "
                              f"{where}") from e
        return dict(project, tools=tools, deny_servers=servers)

    def _launch(self, argv: list, cwd: str, env: dict) -> tuple:
        """Run `claude --bg ...`; return (short id, session id or "")."""
        try:
            short = v1_workers._launch(argv, cwd, self._process_env(env))
        except (OSError, RuntimeError, subprocess.SubprocessError) as e:
            raise WorkerError(f"cannot start the session: {e}") from e
        found = v1_sessions.find_by_id(short, v1_sessions.list_all(self.claude)) or {}
        return short, found.get("sessionId") or ""

    def _argv(self, role: str, name: str, settings: Path, prompt: str, resume_id: str = "") -> list:
        argv = [self.claude, "--bg"]
        if resume_id and not transcripts.SESSION_ID.match(resume_id):
            # It comes from the card or the worker list; one starting with "-" would become a claude flag.
            raise WorkerError(f"{resume_id!r} is not a session id")
        if resume_id:
            # Flags on --resume start a copy under a new id; -n keeps the worker's name on that copy.
            argv += ["--resume", resume_id]
        argv += ["-n", name, "--permission-mode", PERMISSION_MODE, "--settings", str(settings)]
        if not resume_id and role in AGENTS:
            argv += ["--agent", AGENTS[role]]
        if not resume_id and EFFORT:
            argv += ["--effort", EFFORT]
        return argv + [prompt]

    def _save_session(self, card_id: str, role: str, by: str, **fields) -> dict:
        """Merge `fields` into the card's `sessions[role]`; a new `id` is also added to its `ids`. An empty `id`
        (the listing did not show the new session yet) is left out, so it never hides the last known one."""
        if not fields.get("id"):
            fields.pop("id", None)
        card = self._card(card_id)
        sessions = dict(card.get("sessions") or {})
        entry = dict(sessions.get(role) or {})
        sid = fields.get("id")
        if sid:
            entry["ids"] = list(dict.fromkeys(list(entry.get("ids") or []) + [sid]))
        entry.update(fields)
        entry["at"] = cards.stamp(self.clock())
        sessions[role] = entry
        return self.store.update(card_id, {"sessions": sessions}, by=by, did=f"worker {role}: {entry.get('state')}")

    def _set_ask(self, card_id: str, ask: dict, by: str) -> bool:
        """Set the card's ask when it has none open, or replace the role's own worker ask (attach, paused or
        question). Any other open ask (the user's own, another role's, the "died twice" ask) is kept: return
        False."""
        card = self._card(card_id)
        cur = card.get("ask")
        if cur is not None and self._worker_ask_kind(cur, ask["role"]) is None:
            return False
        self.store.update(card_id, {"ask": dict(ask, **{"from": "worker"})}, by=by, did=f"ask {ask['type']}")
        return True

    @staticmethod
    def _worker_ask_kind(ask, role: str) -> Optional[str]:
        """Which of WORKER_ASKS `ask` is for `role`, or None: the user's own ask, another role's, and the "died
        twice" ask are none of them, so no helper ever closes them."""
        if not isinstance(ask, dict) or ask.get("from") != "worker" or ask.get("role") != role:
            return None
        if ask.get("type") == "answer":
            return "question"
        if ask.get("type") == "fix" and ask.get("reason") == ATTACH:
            return "paused" if ask.get("stage") == "paused" else "attach"
        return None

    def _close_worker_asks(self, card_id: str, role: str, by: str, kinds=WORKER_ASKS) -> None:
        """Close the role's own worker ask when it is one of `kinds`, because its cause is gone. One rule for every
        worker ask: any new session of the role (start, `--resume`, an in-place resume) and `finish` close all
        three kinds; every stop closes the attach ask. Nothing else on the card is touched."""
        kind = self._worker_ask_kind(self._card(card_id).get("ask"), role)
        if kind in kinds:
            self.store.update(card_id, {"ask": None}, by=by, did=f"ask closed: the worker's {kind} ask is over")

    def _archive(self, card_id: str, role: str, session_id: str):
        if not session_id:
            return None
        p = transcripts.archive(self.data, self.cfg, card_id, role, session_id, self.transcripts_dir, self.clock())
        return str(p) if p else None

    # -- start -----------------------------------------------------------------------------------------------------

    def start(self, card_id: str, role: str, cwd=None, prompt: str = "", needs=None, resume: bool = False,
              by: str = BY) -> dict:
        """Start a worker on a card, or with `resume` continue its saved session (a parked worker keeps its ports;
        otherwise the card's saved session for the role, e.g. after an answer or a pause). Live mode only.
        Raises ShadowModeError, WorkerError or ConfigError; nothing starts when it raises."""
        config.require_live(self.cfg, f"start a {role} worker on card {card_id}")
        if role not in ROLES:
            raise WorkerError(f"role must be one of {', '.join(ROLES)}, not {role!r}")
        card = self._card(card_id)
        project = self._project(card)
        saved = (card.get("sessions") or {}).get(role) or {}
        now = self.clock()
        with self.registry.locked() as records:
            old = self._find(records, card_id, role)
            other = next((r for r in records if r["card"] == card_id and r is not old), None)
            if other is not None:
                raise WorkerError(f"card {card_id} already has a worker: {other['session_name']}; end it first")
            parked = old if old is not None and old.get("parked") else None
            if old is not None and not (resume and parked):
                raise WorkerError(f"{old['session_name']} is already {'parked' if parked else 'running'}"
                                  f"{'; resume it with --resume' if parked else ''}")
            needs = list(needs if needs is not None else (parked or {}).get("needs") or saved.get("needs") or [])
            check_start(records, needs, self.max_workers, skip=parked)
            port_range = parked["ports"] if parked else next_ports(records, self.port_base)
            sid = ((parked or {}).get("session_id") or saved.get("id") or "") if resume else ""
            if resume and not sid:
                raise WorkerError(f"card {card_id}: no saved {role} session to resume")
            cwd = cwd or (parked or {}).get("cwd") or saved.get("cwd") or ""
            cwd = str(Path(cwd).resolve()) if cwd else ""  # absolute, so later checks from any folder find it
            if not cwd or not Path(cwd).is_dir():
                raise WorkerError(f"the worker's folder {cwd or '(none given)'} is not a folder; pass --cwd")
            branch = card.get("branch") or projects.branch(project, card.get("key") or card_id,
                                                           _slug(card.get("title")))
            env = self._env(card, role, project, port_range, branch)
            name = session_name(card_id, role)
            settings = self.run_dir(card_id) / f"settings-{role}.json"
            argv = self._argv(role, name, settings, self._prompt(prompt, card, role, project, port_range, branch),
                              sid)
            require_literal(self.data)  # before the tool discovery runs a session
            write_json(settings, floor(role, self._tools(project, cwd, env), self.data, env))
            if parked:
                self._stop(card_id, role, parked.get("short_id"), by)  # the parked session sits idle; the copy
                # takes its place
            short, new_sid = self._launch(argv, cwd, env)
            rec = {"card": card_id, "role": role, "project": project.get("name"), "session_name": name,
                   "session_id": new_sid, "short_id": short, "ports": port_range, "needs": needs, "cwd": cwd,
                   "branch": branch, "started_at": cards.stamp(now), "resumes": 0, "parked": None,
                   "idle_since": None, "blocked_since": None}
            records[:] = [r for r in records if r is not old] + [rec]
        self._save_session(card_id, role, by, id=new_sid or sid, cwd=cwd, needs=needs, state="running", why=None,
                           question=None)
        self._close_worker_asks(card_id, role, by)  # a new session: its old prompt, pause or question is over
        return rec

    # -- end, park ---------------------------------------------------------------------------------------------------

    @staticmethod
    def _mark(path: str, why: Optional[str] = None, note: Optional[str] = None, **extra) -> dict:
        """What a close is for (`CLOSES`), kept on the worker's record as `closing` before its stop is sent."""
        state, _did, drop_on_error = CLOSES[path]
        return {"path": path, "state": state, "why": why, "extra": extra, "drop_on_error": drop_on_error,
                "note": note}

    def _close(self, rec: dict, records: list, mark: dict, by: str) -> dict:
        """Stop the worker's process, drop its record from `records`, save its session on the card (with the
        mark's `state`, `why` and `extra` fields) and archive its transcript. The mark goes on the record as
        `closing` before `claude stop` is sent: a stop that raised may still land, so the record stays with its
        mark and the next check finishes this same close (`_finish_close`), whatever the session's state is then.
        The record leaves the list only once the stop has returned. After the stop, a failed step drops the record
        only with the mark's `drop_on_error` (`CLOSES`). The archive copy is best-effort (`archive` None and
        `archive_error`): `finish` copies every id again."""
        sid = rec.get("session_id") or ""
        rec["closing"] = mark
        self._kill(rec.get("short_id"))  # outside the try: a stop that raised keeps the record on every path
        saved = False
        try:
            self._close_worker_asks(rec["card"], rec["role"], by, kinds=("attach",))
            self._save_session(rec["card"], rec["role"], by, id=sid, cwd=rec.get("cwd"),
                               needs=rec.get("needs") or [], state=mark["state"], why=mark["why"],
                               **(mark.get("extra") or {}))
            saved = True
        finally:
            if saved or mark["drop_on_error"]:
                records[:] = [r for r in records if r is not rec]
        out = {"card": rec["card"], "role": rec["role"], "session_id": sid, "archive": None}
        try:
            out["archive"] = self._archive(rec["card"], rec["role"], sid)
        except OSError as e:
            out["archive_error"] = (f"the transcript copy failed ({str(e) or type(e).__name__}); "
                                    "`ap2 worker finish` copies it again")
        return out

    def _finish_close(self, rec: dict, records: list, mark: dict, by: str) -> dict:
        """Close the worker for `mark` (`_mark`) and do that close's follow-up; return the fields for its line.
        Each close lives here only: its first try and the retry of a close that was started (the record's
        `closing`) both run it. Follow-ups: the question's `answer` ask, the `fix` ask of a worker that died twice
        or cannot resume after parking, and the blocked pause's resume hint."""
        card_id, role, path = rec["card"], rec["role"], mark["path"]
        if path == "blocked":
            try:
                closed = self._close(rec, records, mark, by)
            finally:
                # Once the stop returned (with drop_on_error, its record has left the list), the session to attach
                # to is gone: say how to go on instead, also when saving it failed. A stop that raised keeps the
                # record, its mark and the attach ask: the next check finishes this pause.
                if not any(r is rec for r in records):
                    self._set_ask(card_id, {"type": "fix", "role": role, "reason": ATTACH, "stage": "paused",
                                            "text": f"Stopped after {BLOCKED_MIN} minutes at a permission prompt. "
                                                    f"Resume it with `ap2 worker start {card_id} {role} --resume`."},
                                  by)
            return dict(closed, did=CLOSES[path][1])
        closed = self._close(rec, records, mark, by)
        if path == "question":
            text = (mark.get("extra") or {}).get("question")
            set_ = self._set_ask(card_id, {"type": "answer", "text": text, "role": role,
                                           "session_id": closed["session_id"]}, by)
            return dict(closed, did="asked" if set_ else "ended",
                        **({} if set_ else {"note": "the card already has an open ask; the question is kept on "
                                                    f"sessions.{role}.question, and `ap2 worker answer` answers it"}))
        if path in ("died", "parked_failed"):
            self._set_ask(card_id, {"type": "fix", "text": mark["why"], "role": role, "reason": DIED}, by)
        out = dict(closed, did=CLOSES[path][1])
        if mark.get("note"):
            out["note"] = mark["note"]
        return out

    def end(self, card_id: str, role: str, by: str = BY) -> dict:
        """Stop a worker and keep its session on the card, so a later round can resume it. Works in shadow mode."""
        failed = None
        with self.registry.locked() as records:
            rec = self._find(records, card_id, role)
            if rec is None:
                raise WorkerError(f"no {role} worker on card {card_id}")
            try:
                # A close that was started is finished first (its mark and follow-up); otherwise this is an end.
                # Either way end's own drop rule applies: once the stop returned, the record leaves the list, so a
                # save that keeps failing cannot hold the worker's slot and ports forever.
                mark = rec.get("closing") or self._mark("end")
                out = self._finish_close(rec, records, dict(mark, drop_on_error=True), by)
            except Exception as e:  # noqa: BLE001 - raised below, once the list without the record is written back
                failed = e
        if failed is not None:
            raise failed
        return out

    def park(self, card_id: str, role: str, note: str = "", usage_file=None) -> dict:
        """The worker stops itself because a usage window is at the park line (v1's `budget.parking`): it keeps
        its slot and ports. Raises WorkerError when usage is under the line or the worker is not on record."""
        now = self.clock()
        b = v1_budget.read(Path(usage_file) if usage_file else Path.home() / ".claude.json",
                           now_ms=int(now.timestamp() * 1000), stale_min=PARK_RULES["stale_min"])
        p = v1_budget.parking(b, {"budget": PARK_RULES})
        if p is None:
            raise WorkerError("usage is under the park line (or unknown): keep working, do not park")
        rec = dict(p, reason="quota", note=note, parked_at=cards.stamp(now))
        with self.registry.locked() as records:
            w = self._find(records, card_id, role)
            if w is None:
                raise WorkerError(f"no {role} worker on card {card_id}")
            w["parked"] = rec
        return rec

    # -- classify, list, check -----------------------------------------------------------------------------------

    def _newer(self, path: Path, rec: dict) -> bool:
        try:
            started = cards.parse_stamp(rec["started_at"]).timestamp()
        except (KeyError, TypeError, ValueError):
            started = 0.0
        try:
            return path.is_file() and path.stat().st_mtime >= started
        except FileNotFoundError:
            return False  # gone between is_file() and stat() (e.g. `answer` moved question.json away): not newer

    def classify(self, rec: dict, listing) -> str:
        """done | question | parked | unknown | dead | blocked | idle | running. `listing` is
        `claude agents --json --all` (None when it failed: unknown, not empty)."""
        run = self.run_dir(rec["card"])
        if self._newer(run / RESULT_FILES[rec["role"]], rec):
            return "done"
        if self._newer(run / QUESTION, rec):
            return "question"
        if rec.get("parked"):
            return "parked"  # it stopped itself on purpose: not died, not done, never resumed early
        if listing is None:
            return "unknown"
        s = v1_sessions.find_by_id(rec.get("short_id"), listing) or v1_sessions.find(rec["session_name"], listing)
        if s is None or (s.get("pid") is not None and not v1_sessions.pid_alive(s.get("pid"))):
            return "dead"
        if s.get("state") == "failed":
            return "dead"
        if s.get("status") == "waiting":
            return "blocked"  # at a permission prompt
        if s.get("state") in ("blocked", "done") or s.get("status") == "idle":
            return "idle"  # its turn ended, with no result file
        return "running"

    def list(self) -> list:
        """Every worker on record with its state. Works in shadow mode."""
        listing = v1_sessions.list_all(self.claude)
        return [dict(r, state=self.classify(r, listing)) for r in self.registry.read()]

    def _minutes_since(self, stamp: Optional[str]) -> float:
        try:
            return (self.clock() - cards.parse_stamp(stamp)).total_seconds() / 60
        except (TypeError, ValueError):
            return 0.0

    def _read_question(self, card_id: str):
        try:
            q = json.loads((self.run_dir(card_id) / QUESTION).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            q = None
        text = q.get("text") if isinstance(q, dict) else q
        return text if text else "The worker asked a question but its question.json could not be read."

    def _resume_dead(self, rec: dict, prompt: str, count: bool = True) -> None:
        """Resume a worker's own session in place (same slot, same ports). Live mode only. `count` adds one to
        `resumes` (a dead worker's resume; a parked worker's is not counted). The old session is always stopped
        first (a parked one sits idle; a failed one may keep its process): the copy that continues takes its place,
        as in `start()`."""
        config.require_live(self.cfg, f"resume the {rec['role']} worker on card {rec['card']}")
        card = self._card(rec["card"])
        project = self._project(card)
        settings = self.run_dir(rec["card"]) / f"settings-{rec['role']}.json"
        env = self._env(card, rec["role"], project, rec["ports"], rec.get("branch") or "")
        if not (rec.get("cwd") and Path(rec["cwd"]).is_dir()):  # checked first, so a gone worktree says so
            raise WorkerError(f"cannot start the session: the worker's folder {rec.get('cwd') or '(none)'} is gone")
        require_literal(self.data)  # before the tool discovery runs a session
        write_json(settings, floor(rec["role"], self._tools(project, rec["cwd"], env), self.data, env))
        argv = self._argv(rec["role"], rec["session_name"], settings, prompt, rec["session_id"] or rec["short_id"])
        # Whatever its state (parked, failed with its process alive, or gone: a stop of a gone session is harmless),
        # the old session is stopped first, so it never runs on untracked next to the copy. A stop that raises
        # leaves the record as it is.
        self._stop(rec["card"], rec["role"], rec.get("short_id"), BY)
        short, sid = self._launch(argv, rec["cwd"], env)
        # The new session runs now: record it before anything else can fail, so the registry never points at the
        # old one. Its id is the copy's, or "" when the listing does not show the copy yet: the next check fills it
        # in (`_fill_session_id`), as for `start()`.
        rec.update(short_id=short, session_id=sid, started_at=cards.stamp(self.clock()),
                   idle_since=None, blocked_since=None, parked=None,
                   resumes=rec.get("resumes", 0) + (1 if count else 0))
        # A resume runs under a new id; keep it on the card now, so no id is lost if the worker resumes again.
        self._save_session(rec["card"], rec["role"], BY, id=rec["session_id"], state="running", why=None)
        self._close_worker_asks(rec["card"], rec["role"], BY)  # a new session: its old prompt or pause is over

    def check(self, by: str = BY, usage_file=None) -> list:
        """Look at every worker and act on its state (see the module docstring); return one entry per worker:
        `{card, role, state, did, ...}`. With `usage_file`, a parked worker whose window has reset resumes."""
        now = self.clock()
        b = None
        if usage_file:
            b = v1_budget.read(Path(usage_file), now_ms=int(now.timestamp() * 1000), stale_min=PARK_RULES["stale_min"])
        out = []
        with self.registry.locked() as records:
            # Under the lock: a start that lands before it would otherwise be judged against a listing taken
            # before that start, seen as dead and resumed next to itself.
            listing = v1_sessions.list_all(self.claude)
            for rec in list(records):
                entry = {"card": rec["card"], "role": rec["role"], "state": "unknown", "did": "nothing"}
                try:
                    # In the try: a run folder that cannot be read fails this worker's line only, and the earlier
                    # workers' changes are still written back.
                    try:
                        entry["state"] = state = self.classify(rec, listing)
                    except Exception:  # noqa: BLE001 - a started close does not need the state
                        if not rec.get("closing"):
                            raise
                        state = "unknown"  # its mark says what to do: `_act` finishes that close
                    if not rec.get("session_id"):
                        self._fill_session_id(rec, listing, by)
                    self._act(rec, state, records, entry, by, b, now)
                except Exception as e:  # noqa: BLE001 - one worker's error must not cost the others their changes
                    # A shadow-mode refusal, a bad project file, a card or file error, a claude call that failed:
                    # say it on this worker's line and go on, so the registry is still written back below.
                    entry["note"] = str(e) or type(e).__name__
                    if not isinstance(e, (config.ShadowModeError, WorkerError, CardError, config.ConfigError)):
                        entry["error"] = type(e).__name__  # not one of ours: name it, so the gate can report it
                out.append(entry)
        return out

    def _fill_session_id(self, rec: dict, listing, by: str) -> None:
        """The listing did not show the worker's session when it started: take its id from this listing, on the
        record and the card, so a later stop saves and archives it and a later resume continues it."""
        sid = (v1_sessions.find_by_id(rec.get("short_id"), listing) or {}).get("sessionId") or ""
        if sid:
            self._save_session(rec["card"], rec["role"], by, id=sid)
            rec["session_id"] = sid

    def _act(self, rec: dict, state: str, records: list, entry: dict, by: str, b, now: datetime) -> None:
        card_id, role = rec["card"], rec["role"]
        if rec.get("closing"):
            # A close that was started is finished before anything else: its stop may have landed after it timed
            # out, so the session's state now (dead, most likely) says nothing about what to do.
            entry.update(self._finish_close(rec, records, rec["closing"], by))
            return
        # Each clock runs only while its state is seen without a break; "unknown" (the listing failed) says nothing,
        # so it keeps them. A prompt that is gone takes its attach ask with it.
        if state not in ("blocked", "unknown") and rec.get("blocked_since"):
            self._close_worker_asks(card_id, role, by, kinds=("attach",))
            rec["blocked_since"] = None
        if state not in ("idle", "unknown"):
            rec["idle_since"] = None
        if state == "done":
            entry.update(self._finish_close(rec, records, self._mark("done"), by))
        elif state == "question":
            # Kept on the card too, so `answer` works even when the ask cannot be raised (the user's own ask is
            # open).
            entry.update(self._finish_close(rec, records, self._mark("question", "waiting for the user's answer",
                                                                     question=self._read_question(card_id)), by))
        elif state == "dead":
            if rec.get("resumes", 0) < 1:
                try:
                    self._resume_dead(rec, INTERRUPTED)
                except WorkerError:
                    # The resume cannot start (e.g. its worktree is gone): it counts, so the next check stops it as
                    # died twice instead of holding its slot forever. A shadow-mode refusal is not counted.
                    rec["resumes"] = rec.get("resumes", 0) + 1
                    raise
                entry["did"] = "resumed"
            else:
                why = "the worker died twice; look at its run files and its worktree, then start it again"
                entry.update(self._finish_close(rec, records, self._mark("died", why), by))
        elif state == "blocked":
            first = not rec.get("blocked_since")
            if first:
                rec["blocked_since"] = cards.stamp(now)
            if not first and self._minutes_since(rec["blocked_since"]) >= BLOCKED_MIN:
                why = f"stopped after {BLOCKED_MIN} minutes at a permission prompt; paused, can resume"
                entry.update(self._finish_close(rec, records, self._mark("blocked", why), by))
            elif first or self._card(card_id).get("ask") is None:
                # The first sight of the prompt, or a later one once the card has room (the user's own ask that was
                # in the way is closed): raise the attach ask. The 30-minute clock keeps running from the first.
                text = (f"The worker is waiting at a permission prompt. Run `claude attach {rec.get('short_id')}` "
                        f"and answer it; after {BLOCKED_MIN} minutes it is stopped and can be resumed.")
                set_ = self._set_ask(card_id, {"type": "fix", "role": role, "reason": ATTACH, "text": text}, by)
                if set_:
                    entry["did"] = "asked"
                elif first:
                    entry["note"] = f"the card already has an open ask, so this is not on it: {text}"
        elif state == "idle":
            if not rec.get("idle_since"):
                rec["idle_since"] = cards.stamp(now)
            elif self._minutes_since(rec["idle_since"]) >= IDLE_MIN:
                why = f"idle {IDLE_MIN} minutes with no result file; paused, can resume"
                entry.update(self._finish_close(rec, records, self._mark("idle", why), by))
        elif state == "parked":
            if b is not None and v1_park.resume_due(rec, now, {"budget": PARK_RULES}, b):
                try:
                    self._resume_dead(rec, "Usage is under the line again. Continue the task from where you "
                                           "parked.", count=False)
                except WorkerError as e:
                    # The resume cannot start (e.g. its worktree is gone): stop it as a worker that died twice, so
                    # it never holds its slot forever. A shadow-mode refusal is not a WorkerError: it waits.
                    why = f"could not resume after parking ({e}); look at its worktree, then start it again"
                    entry.update(self._finish_close(rec, records, self._mark("parked_failed", why, note=str(e)), by))
                    return
                entry["did"] = "resumed"

    # -- answer, finish ------------------------------------------------------------------------------------------

    def answer(self, card_id: str, text: str, by: str = BY) -> dict:
        """Resume the worker that asked the card's question, with the answer in the prompt, and close the ask.
        Live mode only."""
        if not (isinstance(text, str) and text.strip()):
            raise WorkerError("the answer must be a non-empty text")
        config.require_live(self.cfg, f"resume the worker on card {card_id} with the answer")
        card = self._card(card_id)
        ask = card.get("ask") or {}
        own = ask.get("type") == "answer" and ask.get("from") == "worker" and ask.get("role") in ROLES
        if own:
            role, question = ask["role"], ask.get("text")
        else:
            # The question could not become the ask (the user's own ask is open); it waits on the card instead.
            kept = [(r, e) for r, e in sorted((card.get("sessions") or {}).items())
                    if r in ROLES and isinstance(e, dict) and e.get("state") == "asked" and e.get("question")]
            if not kept:
                raise WorkerError(f"card {card_id} has no question from a worker to answer")
            role, question = kept[0][0], kept[0][1]["question"]
        prompt = (f"The user answered your question.\n\nYour question: {question}\n\nThe answer: {text}\n\n"
                  "Continue the task with this answer.")
        rec = self.start(card_id, role, prompt=prompt, resume=True, by=by)  # clears sessions.<role>.question
        q = self.run_dir(card_id) / QUESTION
        if q.exists():
            os.replace(q, q.with_name(f"question-{cards.stamp(self.clock()).replace(':', '')}.json"))
        if own:
            self.store.update(card_id, {"ask": None}, by=by, did="answer")
        return rec

    def finish(self, card_id: str, by: str = BY) -> dict:
        """The card is done: make the last gzipped copy of each of its sessions and, in live mode only, delete
        the originals with `claude rm`. Refused while a worker of the card is on record."""
        card = self._card(card_id)
        if any(r["card"] == card_id for r in self.registry.read()):
            raise WorkerError(f"card {card_id} still has a worker; end it first")
        archived, removed, note = [], [], None
        sessions = card.get("sessions") or {}
        roles = [r for r in sorted(sessions) if r in ROLES]  # a role is part of a path: no other key is one
        for role in roles:
            self._close_worker_asks(card_id, role, by)  # the card is done: no worker ask of it is still true
        ids = [(role, sid) for role in roles for sid in (sessions[role] or {}).get("ids") or []]
        for role, sid in ids:
            p = self._archive(card_id, role, sid)
            if p:
                archived.append(p)
        failed = []
        try:
            for _role, sid in ids:
                try:
                    transcripts.remove_original(self.cfg, self.claude, sid)  # refuses in shadow mode, before running
                except WorkerError as e:  # claude rm failed: the original stays, the next finish tries again
                    failed.append(str(e))
                    continue
                removed.append(sid)
        except config.ShadowModeError as e:
            note = f"{e} The originals are kept; the copies are made."
        if removed:
            done = dict(sessions)  # any other key, and a role with no ids, is kept as it is
            for role in roles:
                entry = sessions[role] or {}
                if entry.get("ids") and set(entry["ids"]) <= set(removed):  # every original of the role is gone
                    done[role] = dict(entry, state="finished", removed=True)
            self.store.update(card_id, {"sessions": done}, by=by, did="worker sessions removed")
        out = {"card": card_id, "archived": archived, "removed": removed}
        if failed:
            out["failed"] = failed
        if note:
            out["note"] = note
        return out

    def prune(self) -> list:
        return [str(p) for p in transcripts.prune(self.data, self.cfg, self.clock())]
