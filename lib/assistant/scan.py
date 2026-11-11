"""Scan sessions (design 5.3, 8.0): the contract for the reads only a Claude session can make (`via: mcp`,
`connector` and `skill`). Code plans the calls, a hook saves what each tool returned, and code turns the finished
scan into cards. The session only reads; it decides nothing about work.

- `plan(data, names)` writes `<data>/sources/.scan/<UTC stamp>-<6 hex>/plan.json`: one call per source (its
  tools, its arguments with the settings filled in, and for `fill: ai` its records file, form hints and
  categories), the sources that cannot be planned with the reason, and a fixed English brief built by code. It
  starts no process. No `writes` or `never` text reaches a plan.
- `save_raw(plan_file, payload)` is the PostToolUse hook: it appends one line `{tool_name, tool_input, text}` to the
  scan folder's `calls.jsonl` for a tool the plan names. The raw output is saved by code, not by the model.
- `take(data, plan_file)` builds each call's raw file from that log and hands it to `Engine.read_saved`, which
  copies it under `<data>/sources/.raw/<source>/`, checks the records word for word and makes the cards.

Not here yet (design 12): starting the session itself, the gate turning `every:` into due scans, and measuring
what a scan costs.
"""
from __future__ import annotations

import fcntl
import json
import os
import secrets
import shutil
import tempfile
from datetime import timezone
from pathlib import Path
from typing import Callable, Optional

from assistant import cards, config, engine as engine_module, sources
from assistant.config import ConfigError
from assistant.sources import SourceError
from assistant.store import write_json

SCAN = Path(sources.FOLDER) / ".scan"
PLAN = "plan.json"
LOG = "calls.jsonl"
STAMP = "%Y%m%dT%H%M%SZ"
VIAS = ("mcp", "connector", "skill")
# The session's own file writes: never a tool's reply, so a skill scan does not log them.
OWN_WRITES = ("Write", "Edit", "NotebookEdit")
# The line that marks a log cut short (past engine.MAX_OUTPUT in total).
CUT = {"cut_short": True}
VERSION = 1
# The largest plan file `load_plan` reads (a plan is a few KiB; the cap only keeps a bad file from being read whole).
MAX_PLAN = 16 * 1024 * 1024

BRIEF_HEAD = """\
You are an ap2 scan session. You only read sources; you decide nothing about work.

Rules:
1. Call only the tools listed below, with exactly the arguments given.
2. Do not copy tool replies into files. ap2 saves every reply itself.
3. For a call with a records file, write that file as a JSON list with one object per item. Use only the form's
   fields and category. Copy id, thread_id, at and place_id exactly as the tool returned them, character for
   character. Take category from the call's categories. Leave out a field the reply does not have; never make one
   up.
4. Follow a call's instructions when it has some.
5. Post, send, create, edit or comment nowhere. Write no file other than the records files.
"""


def _text(v) -> bool:
    return isinstance(v, str) and bool(v.strip())


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
        server = read.get("server")
        if server is not None and not _text(server):
            raise SourceError(f"{src.name}: read.server must be a server name, not {server!r}")
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
    return Path(folder) / f"{name}.records.json"


def brief(p: dict) -> str:
    """The fixed English prompt for the session, built by code from the plan's calls."""
    lines = [BRIEF_HEAD, f"Plan: {Path(p['folder']) / PLAN}", ""]
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


def keeps(p: dict, tool_name: str) -> bool:
    """Whether the hook logs a call of `tool_name`: a name ending in `__<tool>` for a tool in the plan, or, when
    the plan runs a skill, any tool but the session's own file writes."""
    if any(tool_name.endswith(f"__{t}") for call in p["calls"] for t in call_tools(call)):
        return True
    return any(call["via"] == "skill" for call in p["calls"]) and tool_name not in OWN_WRITES


def save_raw(plan_file, payload: bytes) -> dict:
    """The PostToolUse hook: log one tool call the plan names in the scan folder's calls.jsonl. A payload that is
    not a hook payload is refused (ScanError) and the log is left as it is. Past engine.MAX_OUTPUT in total (or a
    payload larger than that), the log is marked cut short and nothing more is added."""
    p, folder = load_plan(plan_file)
    cap = engine_module.MAX_OUTPUT
    if len(payload) > cap:
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
    if not keeps(p, name):
        return {"kept": False, "tool_name": name}
    line = {"tool_name": name, "tool_input": tool_input, "text": response_text(hook["tool_response"])}
    kept = _append(folder, line, cap)
    return {"kept": kept, "tool_name": name, **({} if kept else {"cut_short": True})}


def _append(folder: Path, line: Optional[dict], cap: int) -> bool:
    """Append `line` to the log under its lock; when it would take the log past `cap` (or `line` is None), append
    the cut-short mark instead, once. Returns whether `line` was added."""
    path = folder / LOG
    data = None if line is None else (json.dumps(line, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")
    cut = (json.dumps(CUT) + "\n").encode("ascii")
    try:
        with open(path, "ab") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            size = os.fstat(f.fileno()).st_size
            if size and _is_cut(path):
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


def _is_cut(path: Path) -> bool:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - 64))
        return f.read().endswith((json.dumps(CUT) + "\n").encode("ascii"))


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
    with nothing added, so no id can match text that code wrote."""
    tools = call_tools(call)

    def mine(line):
        return call["via"] == "skill" or any(line["tool_name"].endswith(f"__{t}") for t in tools)

    if call["fill"] == "paths":
        for line in logged:
            ti = line["tool_input"]
            if mine(line) and all(k in ti and ti[k] == v for k, v in call["args"].items()):
                return line["text"]
        return None
    texts = [line["text"] for line in logged if mine(line)]
    return "\n\n".join(texts) if texts else None


def _records(path: Path):
    cap = engine_module.MAX_OUTPUT
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


def take(data, plan_file, dry_run: bool = False, cfg: Optional[config.Config] = None,
         plugin_dir: Optional[Path] = None, clock: Callable = cards.now_utc) -> dict:
    """Read every planned call of a finished scan through `Engine.read_saved`. A call with no matching logged call,
    a cut-short log or a missing or bad records file is listed in `errors`; the other calls still read. With
    `dry_run`, nothing is written anywhere in the data folder."""
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
                    raw.write_bytes(text.encode("utf-8", errors="replace"))
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
