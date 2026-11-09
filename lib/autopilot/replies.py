"""The minute check for answers on the page.

Answers live in the page's database on claude.ai. The gate uses no Claude, so it cannot read them;
this check starts a tiny Haiku background session that lists the `replies` collection once. Only the
tool result recorded in that session's transcript counts, never the model's own words (with the tool
missing, a model has been seen to answer a made-up number). A reply id not seen before queues a pass
for its autopilot; the pass, not this check, handles the reply.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta

from autopilot import config, paths, sessions, state, usage, workers

NAME = "autopilot · replies"
SYSTEM = ("You check one database collection with the ArtifactData tool and reply with the number of documents only. "
          "Never guess: if you cannot call the tool, reply ERROR.")
# Every other tool is disallowed so the session stays small (about 60k cached tokens). `--tools` cannot be used:
# naming tools there drops the artifact tools entirely.
DISALLOWED = ("Task,Agent,Bash,CronCreate,CronDelete,CronList,DesignSync,Edit,EnterWorktree,ExitWorktree,ListAgents,"
              "LSP,Monitor,NotebookEdit,PublishPlugin,PushNotification,Read,RemoteTrigger,ReportFindings,"
              "ScheduleWakeup,SendMessage,ShareOnboardingGuide,Skill,TaskStop,WebSearch,Workflow,Write,"
              "ArtifactComments,Glob,Grep,WebFetch,AskUserQuestion,EnterPlanMode,ExitPlanMode,SendFeedback")


def quiet(now: datetime, window) -> bool:
    """True inside the quiet window ["22:00", "06:00"], which may wrap past midnight."""
    if not window or len(window) != 2:
        return False
    start, end = window
    t = now.strftime("%H:%M")
    return (t >= start or t < end) if start > end else (start <= t < end)


def _active() -> list:
    """Active autopilots; one with a broken config is skipped here (the gate reports it separately)."""
    out = []
    for n in config.list_aps():
        try:
            if config.status_of(n) == "active":
                out.append(n)
        except Exception:
            continue
    return out


def waiting(aps: list) -> list:
    """Autopilots with a decision a reply can still change: an open ask, or a self decision not undone."""
    out = []
    for n in aps:
        try:
            decs = state.load(n)["decisions"]
        except Exception:
            continue
        if any(d.get("status") == "open" and d.get("kind") in ("ask", "self") for d in decs):
            out.append(n)
    return out


def _db_lines(text: str) -> list:
    out = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def parse(path, url: str):
    """The replies the session really read: [{"id", "ap"}], [] for an empty collection, None when the
    transcript holds no ArtifactData list of this page's `replies`."""
    try:
        lines = [json.loads(l) for l in open(path, errors="replace") if l.strip()]
    except (OSError, TypeError, ValueError):
        return None
    calls = set()
    for e in lines:
        for c in ((e.get("message") or {}).get("content") or []) if e.get("type") == "assistant" else []:
            inp = c.get("input") or {} if isinstance(c, dict) else {}
            if (isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "ArtifactData"
                    and inp.get("action") == "list" and inp.get("collection") == "replies" and inp.get("url") == url):
                calls.add(c.get("id"))
    for e in lines:
        read = (e.get("toolUseResult") or {}).get("db_read") if isinstance(e.get("toolUseResult"), dict) else None
        if not read or read.get("collection") != "replies" or not isinstance(read.get("docs"), list):
            continue
        content = (e.get("message") or {}).get("content") or []
        results = [c for c in content if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("tool_use_id") in calls]
        if not results:
            continue
        data = {d.get("id"): d.get("data") or {} for d in _db_lines(results[0].get("content"))}
        out = []
        for d in read["docs"]:
            doc = data.get(d.get("id")) or {}
            # "Mark all read" sends one reply per autopilot that lists its cards under `decisions`.
            many = doc.get("decisions") if isinstance(doc.get("decisions"), list) else []
            dec = str(doc.get("decision") or (many[0] if many else ""))
            out.append({"id": d.get("id"), "ap": dec.split(":", 1)[0] if ":" in dec else None})
        return out
    return None


def _cleanup(claude: str, short: str) -> None:
    for sub in ("stop", "rm"):
        try:
            subprocess.run([claude, sub, short], capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass


def check(gcfg: dict, url: str, poll_s: float = 2) -> dict:
    """List the page's replies once in a throwaway Haiku session. {"ok": True, "replies": [...]} or
    {"ok": False, "error": ...}. The session is always removed."""
    rc = gcfg["reply_check"]
    claude = workers.claude_bin(gcfg)
    prompt = (f"Load the ArtifactData tool with ToolSearch (query select:ArtifactData) if needed, then call it: "
              f"action list, url {url}, collection replies. Reply with only the number of documents.")
    argv = [claude, "--bg", "-n", NAME, "--model", rc.get("model") or "haiku", "--strict-mcp-config",
            "--setting-sources", "project", f"--disallowedTools={DISALLOWED}", "--system-prompt", SYSTEM,
            "--allowedTools=ArtifactData", "--", prompt]
    cwd = paths.data_root() / "replies-check"   # no CLAUDE.md or project settings here
    cwd.mkdir(parents=True, exist_ok=True)
    try:
        short = workers._launch(argv, str(cwd), workers._env("replies"))
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "error": f"could not start the check: {e}"}
    try:
        s, why = sessions.wait_turn(claude, short, float(rc.get("timeout_s") or 120), poll_s)
        if why:
            return {"ok": False, "error": f"the check {why}"}
        found = parse(usage.find_transcript(s.get("sessionId")), url)
        if found is None:
            return {"ok": False, "error": "no ArtifactData read of the replies in the check's transcript"}
        return {"ok": True, "replies": found}
    finally:
        _cleanup(claude, short)


def step(gcfg: dict, gs: dict, now: datetime) -> set:
    """Run the check when it is due and return the autopilots to queue a pass for. It runs whatever the budget
    allows: a paused budget still reads answers (the gate lets a ticked autopilot's pass go, and the pass starts
    no workers). Keeps its own
    bookkeeping in gs["replies"]: last_at, ok_at, ids (the reply ids seen last time), errors, error."""
    from autopilot import publish
    rc = gcfg.get("reply_check") or {}
    every = rc.get("every_s") or 0
    rs = gs.setdefault("replies", {})
    if not every or quiet(now, rc.get("quiet")):
        return set()
    last = rs.get("last_at")
    if last and now - datetime.fromisoformat(last) < timedelta(seconds=every - 5):
        return set()
    url = (publish._page() or {}).get("url")
    if not url:
        return set()
    active = _active()
    aps = waiting(active)
    if not aps:
        return set()
    rs["last_at"] = now.isoformat()
    try:
        res = check(gcfg, url)
    except Exception as e:   # the gate must keep running whatever the check does
        res = {"ok": False, "error": f"the check failed: {e}"}
    if not res.get("ok"):
        rs["errors"] = int(rs.get("errors") or 0) + 1
        rs["error"] = res.get("error")
        return set()
    rs.update(errors=0, ok_at=now.isoformat())
    rs.pop("error", None)
    seen = set(rs.get("ids") or [])
    rs["ids"] = [r["id"] for r in res["replies"]]
    new = [r for r in res["replies"] if r["id"] not in seen]
    if not new:
        return set()
    named = {r["ap"] for r in new if r.get("ap") in set(active)}
    return named if len(named) == len(new) else named | set(aps)
