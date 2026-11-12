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
import sys
from datetime import datetime, timedelta

from autopilot import cap, cli, config, events, paths, profile, sessions, spend, state, usage, workers

NAME = "autopilot · replies"
SWITCH_CHECK_S = 300   # with several profiles and no card waiting, a profile switch is looked for this often
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
            item = {"id": d.get("id"), "ap": dec.split(":", 1)[0] if ":" in dec else None}
            if doc.get("kind") is not None:   # an ask's option is the pass's business; only kind replies carry it here
                item.update(kind=doc["kind"], option=doc.get("option"))
                item.update({k: doc[k] for k in ("day", "at", "profile", "model", "effort") if doc.get(k) is not None})
            out.append(item)
        return out
    return None


def _cleanup(claude: str, short: str) -> None:
    for sub in ("stop", "rm"):
        if sub == "rm":
            workers._settle(short)
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
    s = profile.page_profile()
    argv = [claude, "--bg", "-n", NAME, "--model", s.get("small_model") or "haiku", "--strict-mcp-config",
            "--settings", str(profile.ensure_light()), "--setting-sources", "project", f"--disallowedTools={DISALLOWED}", "--system-prompt", SYSTEM,
            "--allowedTools=ArtifactData", "--", prompt]
    cwd = paths.data_root() / "replies-check"   # no CLAUDE.md or project settings here
    cwd.mkdir(parents=True, exist_ok=True)
    try:
        short = workers._launch(argv, str(cwd), workers._env("replies"))
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "error": f"could not start the check: {e}"}
    workers._record(short, "replies", None, s)
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


KINDS = ("profile", "profile_choice", "cap")
KINDS_TEXT = "kind: profile, kind: profile_choice or kind: cap"
APPLIED_KEPT = 500   # kind reply ids already applied, newest last


def _applied_file():
    return paths.data_root() / "replies-applied.json"


def _applied() -> list:
    try:
        ids = json.loads(_applied_file().read_text()).get("ids")
    except (OSError, ValueError, AttributeError):
        return []
    return [str(i) for i in ids] if isinstance(ids, list) else []


def _mark_applied(rid: str) -> None:
    ids = [i for i in _applied() if i != rid] + [rid]
    config._write(_applied_file(), {"ids": ids[-APPLIED_KEPT:]})


def _ignore(r: dict, why: str) -> str:
    events.append_global("cap_reply_ignored", {"reason": why, "option": r.get("option"), "day": r.get("day")})
    return f"ignored: {why}"


def _refused(kind: str, option: str, why: str) -> str:
    events.append_global("reply_failed", {"kind": kind, "option": option, "error": why})
    return f"refused: {why}"


def _apply_choice(r: dict) -> str:
    """The choice card's answer: check it against a list fetched now, save it, then switch. A pick not offered (or
    a list that cannot be fetched) is refused and the card shows the fresh list (or the error)."""
    name, model, effort = str(r.get("profile") or ""), str(r.get("model") or ""), str(r.get("effort") or "")
    if (profile.choice_card() or {}).get("profile") != name:
        events.append_global("profile_choice_ignored", {"profile": name})
        return f"ignored: no choice card open for {name!r}"
    if name not in profile.names():
        profile.close_choice(name)
        events.append_global("profile_unknown", {"option": name})
        return f"ignored: no profile named {name!r}"
    try:
        models = profile.model_choices(profile.load(name))
    except profile.ModelsUnavailable as e:
        profile.open_choice(name)   # the card shows the error instead of a list
        return _refused("profile_choice", name, str(e))
    if model not in models or effort not in profile.EFFORTS:
        why = f"{model!r} / {effort!r} is not offered"
        c = profile.open_choice(name, refused=why)
        if c.get("error"):
            return _refused("profile_choice", name, c["error"])
        return _refused("profile_choice", name, why)
    try:
        profile.choose(name, model, effort, models)
    except ValueError as e:   # the profile itself no longer saves (e.g. its small model left the gateway)
        profile.open_choice(name, refused=str(e))
        return _refused("profile_choice", name, str(e))
    profile.use(name)
    return f"applied: profile_choice {name} {model} {effort}"


def _apply(r: dict, now: datetime) -> str:
    """Apply one kind reply and say what happened. A reply that is wrong in itself (unknown profile, bad option,
    wrong day, card not open) is logged and reported, never raised. A failure while applying it (use, apply,
    answer) raises, so apply_kind leaves the reply unmarked and the next check tries again."""
    kind, option = r.get("kind"), str(r.get("option") or "")
    if kind == "profile_choice":
        return _apply_choice(r)
    if kind == "profile":
        if option not in profile.names():
            events.append_global("profile_unknown", {"option": option})
            return f"ignored: no profile named {option!r}"
        if profile.needs_choice(profile.load(option)):
            # Nothing switches before a model and effort are chosen: the page shows the choice card instead.
            c = profile.open_choice(option)
            events.append_global("profile_choice_needed", {"profile": option, "error": c.get("error")})
            return f"choice needed: {option} has no model and effort chosen yet; the page shows the choice card"
        profile.use(option)
        profile.close_choice()   # a card left for another profile: that switch was given up
        return f"applied: profile {option}"
    if option not in cap.OPTIONS and option not in ("pause", cap.ACCEPT_UNKNOWN):
        return _refused(kind, option, f"unknown option {option!r}: use raise50, raise100, accept_unknown or pause")
    st = cap.state(now)
    if r.get("day") != st["day"]:
        return _ignore(r, "not today's card")
    if st["card"] != "open":
        return _ignore(r, "the card is not open")
    cap.answer(option, now)
    return f"applied: cap {option}"


def apply_kind(r: dict, now: datetime) -> str:
    """The one path a kind reply takes, from the minute check and from `ap reply-kind` in a pass: each reply id is
    applied once, whoever reads it first."""
    if r.get("kind") not in KINDS:
        raise ValueError(f"not a kind reply (it has no {KINDS_TEXT})")
    rid = str(r.get("id") or "")
    if rid and rid in _applied():
        return "already applied"
    try:
        out = _apply(r, now)
    except Exception as e:   # noqa: BLE001 - a failure while applying: keep the reply unmarked so it is retried
        events.append_global("reply_failed", {"kind": r.get("kind"), "option": r.get("option"),
                                              "error": f"{type(e).__name__}: {e}"[:300]})
        raise
    if rid:
        _mark_applied(rid)
    return out


def _quiet_window(gcfg: dict):
    return (gcfg.get("reply_check") or {}).get("quiet")


def _cap_card_open(now: datetime) -> bool:
    try:
        return profile.active().get("mode") == "gateway" and cap.state(now)["card"] == "open"
    except Exception:  # noqa: BLE001 - an unreadable active profile has no cap card
        return False


def step(gcfg: dict, gs: dict, now: datetime) -> set:
    """Run the check when it is due and return the autopilots to queue a pass for. It runs whatever the budget
    allows: a paused budget still reads answers (the gate lets a ticked autopilot's pass go, and the pass starts
    no workers). In the quiet hours it runs only while the cap card is open (money is at stake): it applies
    profile and cap answers then, and leaves other answers for after the quiet hours or the forced pass. Keeps its
    own bookkeeping in gs["replies"]: last_at, ok_at, ids (the reply ids seen last time), errors, error."""
    from autopilot import publish
    rc = gcfg.get("reply_check") or {}
    every = rc.get("every_s") or 0
    rs = gs.setdefault("replies", {})
    night = quiet(now, _quiet_window(gcfg))
    if not every or (night and not _cap_card_open(now)):
        return set()
    last = rs.get("last_at")
    if last and now - datetime.fromisoformat(last) < timedelta(seconds=every - 5):
        return set()
    url = (publish._page() or {}).get("url")
    if not url:
        return set()
    active = _active()
    aps = waiting(active)
    try:
        gateway = profile.active().get("mode") == "gateway"
        several = len(profile.names()) >= 2
    except Exception:  # noqa: BLE001 - a broken profile file must not stop the gate; a switch may fix it
        gateway, several = False, True
    if not gateway:
        cap.close_card(now)   # no daily cap here: an open card must not keep the one-minute check running
    if not aps and cap.state(now)["card"] != "open" and not profile.choice_card():
        # Nothing to answer: only a possible profile switch is worth a look, and a slower one.
        if not several or (last and now - datetime.fromisoformat(last) < timedelta(seconds=SWITCH_CHECK_S - 5)):
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
    new = [r for r in res["replies"] if r["id"] not in seen]
    # A profile or cap answer is applied here, in code, oldest first (the list order is not the click order).
    acc = sorted((r for r in new if r.get("kind") in KINDS), key=lambda r: str(r.get("at") or ""))
    failed = set()
    for r in acc:
        try:
            apply_kind(r, now)
        except Exception:   # noqa: BLE001 - not seen: the next check tries it again (reply_failed is logged)
            failed.add(r["id"])
    new = [r for r in new if r.get("kind") not in KINDS]
    if night:
        # Other answers wait: they stay unseen, so the first check after the quiet hours queues their pass.
        rs["ids"] = sorted((seen & {r["id"] for r in res["replies"]}) | ({r["id"] for r in acc} - failed))
        return set()
    rs["ids"] = [r["id"] for r in res["replies"] if r["id"] not in failed]
    if not new:
        return set()
    named = {r["ap"] for r in new if r.get("ap") in set(active)}
    return named if len(named) == len(new) else named | set(aps)


def _doc(raw: str) -> dict:
    """A reply document as the pass read it: {"id", "data": {...}} or the fields at the top level."""
    d = json.loads(raw)
    if not isinstance(d, dict):
        raise ValueError("a reply document is a JSON object")
    data = d.get("data") if isinstance(d.get("data"), dict) else {}
    out = {**{k: v for k, v in d.items() if k != "data"}, **data}
    if "id" in d:
        out["id"] = d["id"]
    return out


@cli.command("reply-kind")
def _reply_kind(args: list) -> int:
    """`ap reply-kind '<reply doc json>'`: apply one profile, profile choice or cap reply in a pass, the way the minute check
    does. Exit 0 when the reply is done with (applied, ignored, refused or applied before): delete it then."""
    if len(args) != 1:
        print("usage: ap reply-kind '<reply document JSON>'", file=sys.stderr)
        return 2
    try:
        r = _doc(args[0])
        if r.get("kind") not in KINDS:
            raise ValueError(f"not a kind reply (it has no {KINDS_TEXT})")
    except ValueError as e:
        print(f"ap reply-kind: {e}", file=sys.stderr)
        return 2
    try:
        out = apply_kind(r, datetime.now().astimezone())
    except Exception as e:   # noqa: BLE001 - a failure while applying: the reply stays unmarked
        print(f"ap reply-kind: could not apply it ({e}); keep the reply and try again next pass", file=sys.stderr)
        return 1
    print(out)
    return 0
