"""Scan sessions (design 5.3, 8.0): the contract for the reads only a Claude session can make (`via: mcp`,
`connector` and `skill`). Code plans the calls, a hook saves what each tool returned, and code turns the finished
scan into cards. The session only reads; it decides nothing about work.

- `plan(data, names)` writes `<data>/sources/.scan/<UTC stamp>-<6 hex>/plan.json`: one call per source (its
  tools, its arguments with the settings filled in, and for `fill: ai` its records file, form hints and
  categories), the sources that cannot be planned with the reason, and a fixed English brief built by code. It
  starts no process. No `writes` or `never` text reaches a plan.
- `save_raw(plan_file, payload)` is the PostToolUse hook: it appends one line `{tool_name, tool_input, text}` to the
  scan folder's `calls.jsonl` for a tool the plan names. The raw output is saved by code, not by the model.
- `take(data, plan_file)` plans each call again from its source as it loads now (a call that differs is refused),
  builds its raw file from that log and hands it to `Engine.read_saved`, which copies it under
  `<data>/sources/.raw/<source>/`, checks the records word for word and makes the cards.

- `start(data, plan, profile, now)` starts the session (`claude --bg -n "ap2 scan · <scan>" --model <small model>
  --settings <scan>/settings.json <brief>`, in the scan folder) and records it in `<data>/scan-session.json`. The
  settings hold two hooks: `ap2 scan hook-allow` before every tool call (`hook_allow`: only the planned tools, the
  records files and, for a skill, the skill and plain reads; all else is denied) and `ap2 scan save-raw` after.
  `launch_profile` is asked first: the active profile, refused while the cap is paused or at its limit.
- `finish(data, now)` looks the session up in `claude agents`; once its turn ended, it is gone or it ran past
  SCAN_MAX_MIN minutes, it is stopped and taken, and the record removed. The gate calls both (one scan at a time).

The session writes only its records files, under `<scan>/records/`; plan.json and calls.jsonl are code's.

Not here yet (design 12): measuring what a scan costs beyond its spend rows.
"""
from __future__ import annotations

import codecs
import errno
import fcntl
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from autopilot import sessions as v1_sessions

from assistant import cards, config, engine as engine_module, sources
from assistant.config import ConfigError
from assistant.sources import SourceError
from assistant.store import write_json

SCAN = Path(sources.FOLDER) / ".scan"
PLAN = "plan.json"
LOG = "calls.jsonl"
RECORDS = "records"  # the only folder the session writes in
SETTINGS = "settings.json"  # the session's --settings, in its scan folder
SESSION = "scan-session.json"  # <data>/scan-session.json: the open scan session, one at a time
SCAN_MAX_MIN = 15  # a scan still working after this many minutes is stopped and taken with what it logged
SMALL_MODEL = "haiku"  # the scan model on a profile with no small_model (the subscription)
NAME_PREFIX = "ap2 scan · "
# The tools a skill scan may use besides the Skill itself: plain reads.
SKILL_READS = ("Read", "Glob", "Grep")
STAMP = "%Y%m%dT%H%M%SZ"
VIAS = ("mcp", "connector", "skill")
# The session's own file writes: never a tool's reply, so a skill scan does not log them.
OWN_WRITES = ("Write", "Edit", "MultiEdit", "NotebookEdit", "TodoWrite")
# The line that marks a log cut short (past engine.MAX_OUTPUT in total).
CUT = {"cut_short": True}
VERSION = 1
# How far into an oversized hook payload `save_raw` looks for its top-level `tool_name` (the hook's session_id,
# transcript_path, cwd, permission_mode and hook_event_name come before it and are short).
NAME_LOOKAHEAD = 1024 * 1024
# The largest plan file `load_plan` reads (a plan is a few KiB; the cap only keeps a bad file from being read whole).
MAX_PLAN = 16 * 1024 * 1024

BRIEF_HEAD = """\
You are an ap2 scan session. You only read sources; you decide nothing about work.

Rules:
1. Call only the tools listed below, with exactly the arguments given.
2. Do not copy tool replies into files. ap2 saves every reply itself.
3. For a call with a records file, write that file as a JSON list with one object per item. Use only the form's
   fields and category. Copy id, thread_id, at and place_id (or place, when there is no place_id) exactly as the
   tool returned them, character for character. Take category from the call's categories. Leave out a field the
   reply does not have; never make one up.
4. Follow a call's instructions when it has some.
5. Post, send, create, edit or comment nowhere. Write no file other than the records files.
"""


def _text(v) -> bool:
    return not cards.blank(v)


# A server or tool name as it appears in Claude Code's tool names (mcp__<server>__<tool>).
_TOOL_PART = re.compile(r"[A-Za-z0-9_-]+")
_AS_IN_TOOL_NAMES = "as it appears in its tool names (mcp__<server>__<tool>)"


class ScanError(ConfigError):
    """A scan plan, hook payload or log is not usable. Printed by ap2 as one `ap2: ...` line."""


# -- plan -----------------------------------------------------------------------------------------------------------

def plan(data, names=(), cfg: Optional[config.Config] = None, plugin_dir: Optional[Path] = None,
         clock: Callable = cards.now_utc) -> dict:
    """Plan one scan session for `names` (none: every `mcp`, `connector` and `skill` source) and write its
    plan.json. A source that cannot be planned is listed in `errors` with the reason; the others are still planned.
    Returns the plan with its path."""
    data = Path(data)
    loader = sources.Loader(data, plugin_dir)
    wanted = list(dict.fromkeys(names)) if names else None
    calls, errors, loaded = [], [], []
    for name in (wanted if wanted is not None else loader.names()):
        try:
            src = loader.load(name)
        except SourceError as e:
            errors.append({"source": name, "error": str(e)})
            continue
        if wanted is None and src.via not in VIAS:
            continue  # a command source is read by the gate, not a session
        loaded.append(src)
    folder = _new_folder(data, clock)
    for src in loaded:
        try:
            calls.append(_call(src, folder))
        except SourceError as e:
            errors.append({"source": src.name, "error": str(e)})
    out = {"version": VERSION, "made_at": cards.stamp(clock()), "folder": str(folder), "calls": calls,
           "errors": errors}
    out["brief"] = brief(out)
    try:
        write_json(folder / PLAN, out)
    except OSError as e:
        raise ScanError(f"cannot write the scan plan in {folder}: {e}") from e
    return {"plan": str(folder / PLAN), **out}


def _new_folder(data: Path, clock: Callable) -> Path:
    base = data / SCAN
    at = clock().astimezone(timezone.utc).strftime(STAMP)
    try:
        base.mkdir(parents=True, exist_ok=True)
        while True:
            folder = base / f"{at}-{secrets.token_hex(3)}"
            try:
                folder.mkdir()
            except FileExistsError:
                continue
            (folder / RECORDS).mkdir()
            return folder
    except OSError as e:
        raise ScanError(f"cannot make a scan folder in {base}: {e}") from e


def _call(src: sources.Source, folder: Path) -> dict:
    """One planned call. Raises SourceError when the source cannot be read by a scan session."""
    read = src.data["read"]
    if src.via == "command":
        raise SourceError(f"{src.name}: read.via is command; the gate runs it with ap2 source read {src.name}, "
                          f"not a scan session")
    if src.via == "skill":
        if src.fill == "paths":
            raise SourceError(f"{src.name}: via: skill gives text, not one tool's JSON reply; use fill: ai")
        if not _text(read.get("skill")):
            raise SourceError(f"{src.name}: via: skill needs read.skill, the skill to run")
    else:
        tool, tools = read.get("tool"), read.get("tools")
        if src.fill == "paths" and not (_text(tool) and tools is None):
            raise SourceError(f"{src.name}: fill: paths reads one tool's reply; give exactly one read.tool "
                              f"(and no read.tools)")
        if tool is not None and not _text(tool):
            raise SourceError(f"{src.name}: read.tool must be a tool name, not {tool!r}")
        if tools is not None and not (isinstance(tools, list) and tools and all(_text(t) for t in tools)):
            raise SourceError(f"{src.name}: read.tools must be a list of tool names, not {tools!r}")
        if tool is None and tools is None:
            raise SourceError(f"{src.name}: via: {src.via} needs read.tool or read.tools, the tools to call")
        for key, t in [("read.tool", tool)] + [("a read.tools entry", t) for t in tools or []]:
            if t is not None and not _TOOL_PART.fullmatch(t):
                raise SourceError(f"{src.name}: {key} must be the tool's name {_AS_IN_TOOL_NAMES}, such as "
                                  f"get_issue, not {t!r}")
        server = read.get("server")
        if server is not None and not _text(server):
            raise SourceError(f"{src.name}: read.server must be a server name, not {server!r}")
        if server is not None and not _TOOL_PART.fullmatch(server):
            raise SourceError(f"{src.name}: read.server must be the server's name {_AS_IN_TOOL_NAMES}, such as "
                              f"claude_ai_Atlassian, not {server!r}")
    args = sources.fill_args(src)  # refuses an unset setting, in the args, the link or the key
    call = {"source": src.name, "via": src.via, "fill": src.fill, "args": args}
    for key in ("server", "tool", "tools", "skill"):
        if key in read:
            call[key] = read[key]
    call["allowed_tools"] = allowed_tools(call)
    if src.fill == "ai":
        call["records"] = str(records_path(folder, src.name))
        call["form"] = dict(src.data["form"])
        call["categories"] = list(src.sort)
    if "instructions" in src.data:
        call["instructions"] = src.data["instructions"]
    return call


def call_tools(call: dict) -> list:
    """The tool names a planned call reads with (none for a skill)."""
    if call.get("via") == "skill":
        return []
    return [call["tool"]] if isinstance(call.get("tool"), str) else list(call.get("tools") or [])


def allowed_tools(call: dict) -> list:
    """The tools the session may call for `call`: `mcp__<server>__<tool>` when the source names its server, else
    `mcp__*__<tool>` (a claude.ai connector's server name is known only inside the session); `Skill` for a skill."""
    if call["via"] == "skill":
        return ["Skill"]
    server = call.get("server") or "*"
    return [f"mcp__{server}__{t}" for t in call_tools(call)]


def records_path(folder: Path, name: str) -> Path:
    return Path(folder) / RECORDS / f"{name}.records.json"


def brief(p: dict) -> str:
    """The fixed English prompt for the session, built by code from the plan's calls."""
    lines = [BRIEF_HEAD]
    if not p["calls"]:
        lines.append("There is nothing to call. End now.")
        return "\n".join(lines) + "\n"
    lines.append("Calls:")
    for n, call in enumerate(p["calls"], 1):
        if call["via"] == "skill":
            what = f"run the skill {call['skill']}"
        else:
            tools = call_tools(call)
            where = f" on the {call['server']} server" if call.get("server") else ""
            what = f"call {'the tool' if len(tools) == 1 else 'the tools'} {', '.join(tools)}{where}"
        lines.append(f"{n}. {call['source']} ({call['via']}): {what}.")
        lines.append(f"   Arguments: {json.dumps(call['args'], sort_keys=True, ensure_ascii=False)}")
        if call["fill"] == "ai":
            lines.append(f"   Records file: {call['records']}")
            hints = "; ".join(f"{k} = {v}" for k, v in call["form"].items())
            lines.append(f"   Form: {hints}")
            lines.append(f"   Categories: {', '.join(call['categories'])}")
        else:
            lines.append("   Code fills the records from the reply; write no records file for this call.")
        if call.get("instructions"):
            lines.append(f"   Instructions: {' '.join(call['instructions'].split())}")
    return "\n".join(lines) + "\n"


# -- loading a plan -------------------------------------------------------------------------------------------------

def load_plan(plan_file) -> tuple:
    """(the plan, its scan folder) from a plan.json that `plan` wrote. Raises ScanError."""
    path = Path(plan_file)
    if path.name != PLAN or path.resolve().parent.parent.name != SCAN.name:
        raise ScanError(f"{path}: not a scan plan (expected <data>/{SCAN.as_posix()}/<scan>/{PLAN})")
    try:
        held = sources.read_capped(path, MAX_PLAN, f"{path}: larger than {MAX_PLAN} bytes")
        p = json.loads(held.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as e:
        raise ScanError(f"{path}: cannot read the scan plan: {e}") from e
    if not (isinstance(p, dict) and p.get("version") == VERSION and isinstance(p.get("calls"), list)):
        raise ScanError(f"{path}: not a scan plan of version {VERSION}")
    for call in p["calls"]:
        ok = (isinstance(call, dict) and isinstance(call.get("source"), str) and sources.NAME.match(call["source"])
              and call.get("via") in VIAS and call.get("fill") in sources.FILLS and isinstance(call.get("args"), dict)
              and all(isinstance(t, str) and t for t in call_tools(call)))
        if not ok:
            raise ScanError(f"{path}: a call is not a planned call: {call!r}"[:500])
    return p, path.resolve().parent


# -- the hook: save each reply ----------------------------------------------------------------------------------

def response_text(response) -> str:
    """A tool reply as text: a text as is; a list of {"type": "text"} blocks, their texts joined by a newline;
    anything else, its JSON text."""
    if isinstance(response, str):
        return response
    if (isinstance(response, list) and response
            and all(isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
                    for b in response)):
        return "\n".join(b["text"] for b in response)
    return json.dumps(response, sort_keys=True, ensure_ascii=False)


def keeps(p: dict, tool_name: str, tool_input=None, folder: Optional[Path] = None, tool_response=None,
          data: Optional[Path] = None) -> bool:
    """Whether the hook logs a call of `tool_name`: a tool of a planned call (`calls_tool`), or, when
    the plan runs a skill, any tool but the session's own file writes. A call whose `tool_input` names the scan
    folder (its `<stamp>-<hex>` name) or the data folder as a path (as given, resolved, or `~/...` under the home
    folder), in any text at any depth and in any letter case, is never kept; nor is a call kept only for a skill
    whose `tool_response` names either (a Read's file path, a Grep's matching lines). So the session reading back
    its own records file, the report or the cards does not put its own ids in the raw output. `data` defaults to
    the scan folder's `<data>` (`<data>/sources/.scan/<scan>`)."""
    if data is None and folder is not None:
        data = Path(folder).parent.parent.parent
    words = () if folder is None else (Path(folder).name.casefold(),)
    paths = () if data is None else _spellings(Path(data))
    if _names(tool_input, words, paths):
        return False
    if any(calls_tool(call, tool_name) for call in p["calls"]):
        return True  # a planned tool's reply is the source's own text, whatever it says
    if not any(call["via"] == "skill" for call in p["calls"]) or tool_name in OWN_WRITES:
        return False
    return not _names(tool_response, words, paths)


def calls_tool(call: dict, tool_name: str) -> bool:
    """Whether `tool_name` is one of `call`'s tools: exactly `mcp__<server>__<tool>` when the source names its
    server, else any name ending in `__<tool>` (a connector's server name is known only inside the session). So a
    tool of the same name on another server is not the source's. A skill call has no tools."""
    server = call.get("server")
    return any(tool_name == f"mcp__{server}__{t}" if server else tool_name.endswith(f"__{t}")
               for t in call_tools(call))


def _spellings(data: Path) -> tuple:
    """The data folder as the session may spell it: as given (made absolute), resolved, and each as `~/...` when
    it is under the home folder; letter case folded."""
    found = {os.path.abspath(data), str(data.resolve())}
    try:
        home = {os.path.abspath(Path.home()), str(Path.home().resolve())}
    except (KeyError, RuntimeError, OSError):
        home = set()
    for d in list(found):
        for h in home:
            if h != os.sep and d.startswith(h + os.sep):
                found.add("~" + d[len(h):])
    return tuple(sorted({d.casefold() for d in found}))


def _names_path(text: str, path: str) -> bool:
    """Whether `text` holds `path` as a path: followed by a path separator, the end of the text, or a character a
    file name would not go on with (a quote, a space, `:`), so `<data>-backup` or `<data>2` is another folder."""
    at = text.find(path)
    while at != -1:
        nxt = text[at + len(path):at + len(path) + 1]
        if not nxt or nxt == os.sep or not (nxt.isalnum() or nxt in "._-"):
            return True
        at = text.find(path, at + 1)
    return False


def _names(value, words: tuple, paths: tuple) -> bool:
    """Whether a text in `value` (texts, lists and maps, keys too) holds one of `words` or names one of `paths` as
    a path, letter case ignored (`words` and `paths` come case-folded)."""
    stack = [value]
    while stack:
        v = stack.pop()
        if isinstance(v, str):
            text = v.casefold()
            if any(w in text for w in words) or any(_names_path(text, d) for d in paths):
                return True
        elif isinstance(v, dict):
            stack.extend(v.keys())
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)
    return False


def save_raw(plan_file, payload: bytes) -> dict:
    """The PostToolUse hook: log one tool call the plan names in the scan folder's calls.jsonl. A payload that is
    not a hook payload is refused (ScanError) and the log is left as it is. Past engine.MAX_OUTPUT in total (or a
    payload larger than that), the log is marked cut short and nothing more is added; but a larger payload whose
    top-level `tool_name` (read from its start, `_leading_tool_name`) is no planned call's tool, in a plan that runs
    no skill, is skipped and the log left as it is."""
    p, folder = load_plan(plan_file)
    cap = engine_module.MAX_OUTPUT
    if len(payload) > cap:
        name = _leading_tool_name(payload)
        if name is not None and not any(call["via"] == "skill" or calls_tool(call, name) for call in p["calls"]):
            return {"kept": False, "tool_name": name, "too_large": True}  # a reply `keeps` refuses by name alone
        _append(folder, None, cap)
        raise ScanError(f"the hook payload is larger than {cap} bytes; the scan log {folder / LOG} is marked cut "
                        f"short")
    try:
        hook = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as e:
        raise ScanError(f"the hook payload is not JSON: {e}") from e
    if not isinstance(hook, dict):
        raise ScanError("the hook payload must be a JSON object with tool_name, tool_input and tool_response")
    name, tool_input = hook.get("tool_name"), hook.get("tool_input")
    if not (isinstance(name, str) and name):
        raise ScanError("the hook payload has no tool_name")
    if not isinstance(tool_input, dict):
        raise ScanError("the hook payload's tool_input must be a JSON object")
    if "tool_response" not in hook:
        raise ScanError("the hook payload has no tool_response")
    data = Path(os.path.abspath(plan_file)).parent.parent.parent.parent  # <data>/sources/.scan/<scan>/plan.json
    if not keeps(p, name, tool_input, folder, hook["tool_response"], data):
        return {"kept": False, "tool_name": name}
    line = {"tool_name": name, "tool_input": tool_input, "text": response_text(hook["tool_response"])}
    kept = _append(folder, line, cap)
    return {"kept": kept, "tool_name": name, **({} if kept else {"cut_short": True})}


_JSON_SPACE = re.compile(r"[ \t\n\r]*")


def _leading_tool_name(payload: bytes) -> Optional[str]:
    """The top-level `tool_name` of a payload too large to parse whole (it arrives cut off), or None when it cannot
    be read. Only the first NAME_LOOKAHEAD bytes are looked at: the object's keys are read in order, one key and one
    value at a time, until the `tool_name` key, whose value must be a non-empty text; nothing after that value is
    parsed (the whole look-ahead must be valid UTF-8). Not a JSON object, bad UTF-8, a bad or cut-off key or value
    before it, or a name that is not a text is None."""
    try:
        # Strict, but holds back a character cut in two at the limit instead of failing on it.
        text = codecs.getincrementaldecoder("utf-8")().decode(payload[:NAME_LOOKAHEAD], final=False)
    except UnicodeDecodeError:
        return None
    decoder, space = json.JSONDecoder(), _JSON_SPACE.match
    at = space(text).end()
    if text[at:at + 1] != "{":
        return None
    at = space(text, at + 1).end()
    try:
        while text[at:at + 1] == '"':
            key, at = decoder.raw_decode(text, at)
            at = space(text, at).end()
            if text[at:at + 1] != ":":
                return None
            at = space(text, at + 1).end()
            if key == "tool_name":
                if text[at:at + 1] != '"':
                    return None
                name, _ = decoder.raw_decode(text, at)
                return name or None
            _, at = decoder.raw_decode(text, at)
            at = space(text, at).end()
            if text[at:at + 1] != ",":
                return None
            at = space(text, at + 1).end()
    except (ValueError, RecursionError):
        return None
    return None


def _append(folder: Path, line: Optional[dict], cap: int) -> bool:
    """Append `line` to the log under its lock; when it would take the log past `cap` (or `line` is None), append
    the cut-short mark instead, once. Returns whether `line` was added. A log that is a symbolic link is refused, so
    the hook never writes outside the scan folder."""
    path = folder / LOG
    data = None if line is None else (json.dumps(line, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")
    cut = (json.dumps(CUT) + "\n").encode("ascii")
    try:
        try:
            fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise ScanError(f"the scan log {path} is a symbolic link; the hook writes only in the scan folder") \
                    from None
            raise
        with os.fdopen(fd, "ab") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            size = os.fstat(f.fileno()).st_size
            if size and _is_cut(f.fileno(), size):
                return False
            if data is not None and size + len(data) <= cap:
                f.write(data)
                added = True
            else:
                f.write(cut)
                added = False
            f.flush()
            os.fsync(f.fileno())
            return added
    except OSError as e:
        raise ScanError(f"cannot write the scan log {path}: {e}") from e


def _is_cut(fd: int, size: int) -> bool:
    """Whether the open log ends with the cut-short mark (read through `fd`, not the path)."""
    return os.pread(fd, 64, max(0, size - 64)).endswith((json.dumps(CUT) + "\n").encode("ascii"))


def read_log(folder: Path) -> tuple:
    """(the logged calls, whether the log was cut short). A missing log is no calls."""
    path = Path(folder) / LOG
    try:
        held = sources.read_capped(path, engine_module.MAX_OUTPUT + 4096, f"{path}: the scan log is too large")
    except FileNotFoundError:
        return [], False
    except OSError as e:
        raise ScanError(f"cannot read the scan log {path}: {e}") from e
    calls, cut = [], False
    for n, text in enumerate(held.decode("utf-8", errors="replace").splitlines(), 1):
        if not text.strip():
            continue
        try:
            line = json.loads(text)
        except (ValueError, RecursionError) as e:
            raise ScanError(f"{path}: line {n} is not JSON: {e}") from e
        if line == CUT:
            cut = True
            continue
        if not (isinstance(line, dict) and isinstance(line.get("tool_name"), str)
                and isinstance(line.get("tool_input"), dict) and isinstance(line.get("text"), str)):
            raise ScanError(f"{path}: line {n} is not a logged call")
        calls.append(line)
    return calls, cut


# -- take: from a finished scan to cards ------------------------------------------------------------------------

def raw_for(call: dict, logged: list) -> Optional[str]:
    """The raw output of one planned call, from the log; None when no logged call matches.
    `fill: paths`: the text of the first call of its tool whose tool_input holds every planned argument with the
    same value. `fill: ai`: every text of its tools (a skill's: every logged text), in order, joined by a blank line,
    with nothing added, so no id can match text that code wrote; a call whose tool_input holds a planned argument
    with another value is left out (one with none of the planned argument names, such as a thread read, still
    counts). A logged call is its tool's by `calls_tool`."""

    def mine(line):
        return call["via"] == "skill" or calls_tool(call, line["tool_name"])

    def planned(line):
        ti = line["tool_input"]
        return call["via"] == "skill" or all(ti[k] == v for k, v in call["args"].items() if k in ti)

    if call["fill"] == "paths":
        for line in logged:
            ti = line["tool_input"]
            if mine(line) and all(k in ti and ti[k] == v for k, v in call["args"].items()):
                return line["text"]
        return None
    texts = [line["text"] for line in logged if mine(line) and planned(line)]
    return "\n\n".join(texts) if texts else None


def _records(path: Path):
    cap = engine_module.MAX_OUTPUT
    for p in (path.parent, path):
        try:
            if stat.S_ISLNK(os.lstat(p).st_mode):
                raise SourceError(f"the records {'folder' if p == path.parent else 'file'} {p} is a symbolic link; "
                                  f"the session writes its records only in the scan folder")
        except FileNotFoundError:
            break  # read_capped below says so
        except OSError as e:
            raise SourceError(f"cannot read the records {path}: {e}") from e
    try:
        held = sources.read_capped(path, cap, f"the records file {path} is larger than {cap} bytes")
        records = json.loads(held.decode("utf-8"))
    except FileNotFoundError:
        raise SourceError(f"the session wrote no records file {path}") from None
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as e:
        why = "nested too deeply" if isinstance(e, RecursionError) else e
        raise SourceError(f"cannot read the records {path}: {why}") from e
    if not isinstance(records, list):
        raise SourceError(f"the records file {path} must hold a JSON list")
    return records


def _replan(loader: sources.Loader, call: dict, folder: Path) -> dict:
    """The call planned again from its source as it loads now, when it is the plan's call (its `records` path aside:
    take reads the records only from `records_path`). Raises SourceError when it is not, or cannot be planned:
    plan.json sits where the session could reach it, so take never trusts its calls."""
    name = call["source"]
    why = ""
    try:
        now = _call(loader.load(name), folder)
    except SourceError as e:
        now, why = None, f" ({e})"
    if now is None or {k: v for k, v in now.items() if k != "records"} != \
            {k: v for k, v in call.items() if k != "records"}:
        raise SourceError(f"{name}: the plan's call does not match the source as it loads now{why}")
    return now


def _write_raw(raw: Path, text: str) -> None:
    """Write the raw output to a temp file next to `raw`, then rename it into place, so a symbolic link left at
    `raw` is replaced, never followed."""
    fd, tmp = tempfile.mkstemp(dir=raw.parent, prefix=f".{raw.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode("utf-8", errors="replace"))
        os.replace(tmp, raw)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def take(data, plan_file, dry_run: bool = False, cfg: Optional[config.Config] = None,
         plugin_dir: Optional[Path] = None, clock: Callable = cards.now_utc) -> dict:
    """Read every planned call of a finished scan through `Engine.read_saved`. A call that is not the source's call
    as it loads now, a call with no matching logged call, a cut-short log or a missing or bad records file is listed
    in `errors`; the other calls still read. With `dry_run`, nothing is written anywhere in the data folder."""
    data = Path(data)
    p, folder = load_plan(plan_file)
    here = (data / SCAN).resolve()
    if folder.parent != here:
        raise ScanError(f"{plan_file}: the plan is not in this data folder's {SCAN.as_posix()}/ ({here})")
    logged, cut = read_log(folder)
    eng = engine_module.Engine(data, cfg=cfg, plugin_dir=plugin_dir, clock=clock)
    reads, errors = [], []
    scratch = Path(tempfile.mkdtemp(prefix="ap2-scan-")) if dry_run else None
    try:
        for call in p["calls"]:
            name = call["source"]
            try:
                call = _replan(eng.loader, call, folder)
                if cut and call["fill"] == "ai":
                    raise SourceError(f"the scan log {folder / LOG} was cut short, so the raw output may be "
                                      f"missing replies")
                text = raw_for(call, logged)
                if text is None:
                    short = " (the scan log was cut short)" if cut else ""
                    raise SourceError(f"no logged call matches {', '.join(call_tools(call)) or 'the skill'} with "
                                      f"the planned arguments{short}")
                records = _records(records_path(folder, name)) if call["fill"] == "ai" else None
                ext = "json" if call["fill"] == "paths" else "txt"
                raw = (scratch or folder) / f"{name}.raw.{ext}"
                try:
                    _write_raw(raw, text)
                except OSError as e:
                    raise SourceError(f"cannot write the raw output {raw}: {e}") from e
                reads.append(eng.read_saved(name, raw, records=records, dry_run=dry_run))
            except SourceError as e:
                msg = str(e)
                errors.append({"source": name, "error": msg if msg.startswith(f"{name}:") else f"{name}: {msg}"})
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
    return {"plan": str(folder / PLAN), "dry_run": dry_run, "reads": reads, "errors": errors}


# -- the session: the guard, the start and the finish ---------------------------------------------------------------

# Built-in tools no scan needs, denied in its settings as well, so a guard hook that fails (a timeout) still cannot
# let a shell or a file edit through; the guard (`hook_allow`) is what keeps the session to its planned calls.
DENY = ("Bash", "Edit", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch")


def _decision(allow: bool, why: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow" if allow else "deny",
                                   "permissionDecisionReason": why}}


def allows(p: dict, tool_name: str, tool_input: dict) -> tuple:
    """`(allowed, why)` for one tool call of the session of plan `p`: a tool of a planned call (`calls_tool`, the
    same match the take uses); `Write` to a planned records file; or, when the plan runs a skill, `Skill` with that
    skill's name or a plain read (`SKILL_READS`). Nothing else."""
    if any(calls_tool(call, tool_name) for call in p["calls"]):
        return True, "a planned scan call"
    if tool_name == "Write":
        path = tool_input.get("file_path")
        wanted = {os.path.normpath(c["records"]) for c in p["calls"] if isinstance(c.get("records"), str)}
        if isinstance(path, str) and os.path.isabs(path) and os.path.normpath(path) in wanted:
            return True, "a planned records file"
    skills = {c.get("skill") for c in p["calls"] if c["via"] == "skill"}
    if skills and ((tool_name == "Skill" and tool_input.get("skill") in skills) or tool_name in SKILL_READS):
        return True, "the planned skill"
    return False, (f"ap2 scan: {tool_name} is not one of this scan's planned calls. Call only the tools the brief "
                   f"lists, with its arguments, and write only its records files.")


def hook_allow(plan_file, payload: bytes) -> dict:
    """The PreToolUse hook (`ap2 scan hook-allow --plan <plan>`): the allow or deny for one tool call. It fails
    closed: a plan or payload that cannot be read is a deny, never no opinion."""
    try:
        p, _ = load_plan(plan_file)
        hook = json.loads(payload.decode("utf-8"))
        name = hook.get("tool_name") if isinstance(hook, dict) else None
        if not (isinstance(name, str) and name):
            raise ScanError("the hook payload has no tool_name")
        tool_input = hook.get("tool_input")
        ok, why = allows(p, name, tool_input if isinstance(tool_input, dict) else {})
    except Exception as e:  # noqa: BLE001 - any doubt is a deny
        return _decision(False, f"ap2 scan: denied, the guard could not decide: {e}"[:500])
    return _decision(ok, why)


def hook_allow_main(argv: list, stdin) -> int:
    """`ap2 scan hook-allow --plan PLAN`: the payload on stdin, the decision JSON on stdout, exit 0 always."""
    plan_file = argv[1] if len(argv) == 2 and argv[0] == "--plan" else ""
    try:
        src = getattr(stdin, "buffer", stdin)
        payload = src.read(engine_module.MAX_OUTPUT + 1) if src is not None else b""
        if isinstance(payload, str):
            payload = payload.encode("utf-8", errors="surrogateescape")
    except Exception:  # noqa: BLE001 - no payload: denied below
        payload = b""
    out = hook_allow(plan_file, payload) if plan_file else _decision(False, "ap2 scan: hook-allow needs --plan PLAN")
    try:
        os.write(1, (json.dumps(out) + "\n").encode())
    except OSError:
        pass
    return 0


def _ap2() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "bin" / "ap2"


def _claude(claude: Optional[str]) -> str:
    return claude or shutil.which("claude") or "claude"


def launch_profile(data, now: datetime, transcripts_dir=None, claude: Optional[str] = None, store=None) -> dict:
    """The profile a scan session starts on: the active one. Raises (a ProfileError, or a ScanError naming the
    cap's refusal) when it does not load, or the cap is paused or at its limit (`cap.allowance`), so nothing starts."""
    from assistant import cap, profiles, transcripts

    data = Path(data)
    prof = profiles.active(data)
    projects = Path(transcripts_dir) if transcripts_dir else transcripts.default_projects()
    a = cap.allowance(data, projects, now.astimezone(), prof, listing=lambda: v1_sessions.list_all(_claude(claude)),
                      store=store)
    if a.get("allow") == 0:
        raise ScanError(f"no scan session started: {cap.refusal(a)}")
    return prof


def session_settings(plan_file, profile: dict) -> dict:
    """The session's `--settings`: the guard before every tool call, the save after, the built-in denials, and the
    profile's overlay merged under them as the worker floor merges it (`profiles.merge`)."""
    from assistant import profiles

    ap2, plan_arg = shlex.quote(str(_ap2())), shlex.quote(str(plan_file))
    hooks = {"PreToolUse": [{"matcher": "*", "hooks": [
                 {"type": "command", "command": f"{ap2} scan hook-allow --plan {plan_arg}", "timeout": 10}]}],
             "PostToolUse": [{"matcher": "*", "hooks": [
                 {"type": "command", "command": f"{ap2} scan save-raw --plan {plan_arg}", "timeout": 30}]}]}
    floor = {"permissions": {"deny": list(DENY), "allow": []}, "env": {}, "hooks": hooks}
    return profiles.merge(floor, profiles.overlay(profile))


def start(data, p: dict, profile: dict, now: datetime, claude: Optional[str] = None) -> dict:
    """Start the scan session of plan `p` (as `plan` returned it) on `profile`, in its scan folder, and record it in
    `<data>/scan-session.json` `{plan, short_id, session_id, started_at, profile}`, with a spend launch row (kind
    `scan`). Raises ScanError when it cannot start."""
    from autopilot import workers as v1_workers

    from assistant import spend

    data, folder, claude = Path(data), Path(p["folder"]), _claude(claude)
    settings = session_settings(p["plan"], profile)
    try:
        write_json(folder / SETTINGS, settings)
    except OSError as e:
        raise ScanError(f"cannot write the scan settings in {folder}: {e}") from e
    argv = [claude, "--bg", "-n", NAME_PREFIX + folder.name, "--model", profile.get("small_model") or SMALL_MODEL,
            "--settings", str(folder / SETTINGS), p["brief"]]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AUTOPILOT_", "AP2_"))}
    env.update(settings["env"])
    try:
        short = v1_workers._launch(argv, str(folder), env)
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        raise ScanError(f"cannot start the scan session: {e}") from e
    sid = (v1_sessions.find_by_id(short, v1_sessions.list_all(claude)) or {}).get("sessionId") or ""
    rec = {"plan": str(p["plan"]), "short_id": short, "session_id": sid, "started_at": cards.stamp(now),
           "profile": profile["name"]}
    write_json(data / SESSION, rec)
    try:
        spend.record_launch(data, short, "scan", None, profile, session_id=sid, at=now)
    except Exception as e:  # noqa: BLE001 - bookkeeping never undoes a started session
        return dict(rec, note=f"the spend launch row was not written: {e}")
    return rec


def _read_session(path: Path) -> dict:
    rec = json.loads(path.read_text(encoding="utf-8"))
    ok = (isinstance(rec, dict) and all(isinstance(rec.get(k), str) for k in ("plan", "short_id", "started_at"))
          and re.fullmatch(r"[A-Za-z0-9_]+", rec["short_id"]))
    if not ok:
        raise ValueError("not a scan session record ({plan, short_id, session_id, started_at, profile})")
    cards.parse_stamp(rec["started_at"])
    return rec


def finish(data, now: datetime, claude: Optional[str] = None, transcripts_dir=None,
           cfg: Optional[config.Config] = None) -> Optional[dict]:
    """Look at the open scan session (`<data>/scan-session.json`); None when there is none. Its turn ended (`state`
    done, blocked or failed, or `status` idle), it is gone, its process is dead, or it ran past SCAN_MAX_MIN minutes
    (one more error): it is stopped (`claude stop`, its spend settled), taken (`take`) and the record removed.
    A failed listing, or a session still working inside the time, waits for the next run. Returns `{open, ended,
    short_id, reads, errors}`; each read also says its `via`."""
    from assistant import spend, transcripts

    data, claude = Path(data), _claude(claude)
    path = data / SESSION
    out = {"open": True, "ended": False, "short_id": None, "reads": [], "errors": []}
    try:
        rec = _read_session(path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, RecursionError) as e:
        path.unlink(missing_ok=True)  # else it would hold every later scan back
        out.update(open=False, errors=[{"scan": True, "error": f"{path}: cannot read the scan session record ({e}); "
                                                                 f"it was removed"}])
        return out
    short, sid = rec["short_id"], rec.get("session_id") or ""
    out["short_id"] = short
    listing = v1_sessions.list_all(claude)
    if listing is None:
        return out  # unknown, not gone: the next run looks again
    s = v1_sessions.find_by_id(short, listing)
    ended = (s is None or (s.get("pid") is not None and not v1_sessions.pid_alive(s.get("pid")))
             or s.get("state") in ("done", "blocked", "failed") or s.get("status") == "idle")
    late = not ended and now - cards.parse_stamp(rec["started_at"]) >= timedelta(minutes=SCAN_MAX_MIN)
    if not (ended or late):
        return out
    projects = Path(transcripts_dir) if transcripts_dir else transcripts.default_projects()
    try:
        if s is not None:
            subprocess.run([claude, "stop", short], capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        out["errors"].append({"scan": True, "error": f"cannot stop the scan session {short}: {e}; the next run "
                                                     f"tries again"})
        return out
    finally:
        try:
            spend.settle(data, projects, short=short, session_id=sid, listing=lambda: v1_sessions.list_all(claude),
                         now=now)
        except Exception:  # noqa: BLE001 - bookkeeping never stops a finish
            pass
    if late:
        out["errors"].append({"scan": True, "error": f"the scan session {short} was still working after "
                                                     f"{SCAN_MAX_MIN} minutes; it was stopped and taken with what "
                                                     f"it logged"})
    try:
        p, _ = load_plan(rec["plan"])
        via = {c["source"]: c["via"] for c in p["calls"]}
        taken = take(data, rec["plan"], cfg=cfg, clock=lambda: now)
        out["reads"] = [dict(r, via=via.get(r["source"], "mcp")) for r in taken["reads"]]
        out["errors"] += taken["errors"]
    except ConfigError as e:  # a ScanError or SourceError: the plan or its log cannot be read
        out["errors"].append({"scan": True, "error": str(e)})
    path.unlink(missing_ok=True)
    out.update(open=False, ended=True)
    return out
