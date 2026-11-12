from __future__ import annotations

import json
import os
import sys
from datetime import datetime

from autopilot import budget, cli, config, events, paths, state, text

LIMIT = 6000
MAX_EVENTS = 15
VERBS = {"status", "init", "doctor", "uninstall", "pause", "resume", "tick", "stop"}


def _bind_path(sid: str):
    safe = "".join(c for c in sid if c.isalnum() or c in "-_")
    return paths.data_root() / "sessions" / f"{safe}.json"


def binding(sid: str):
    try:
        return json.loads(_bind_path(sid).read_text())
    except (OSError, ValueError):
        return None


def _save_binding(sid: str, b: dict) -> None:
    p = _bind_path(sid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(b))


def bind(sid: str, name: str) -> None:
    if name not in config.list_aps():
        raise ValueError(f"no autopilot named {name!r}")
    _save_binding(sid, {"name": name, "offset": None, "rules_mtimes": {}})


def unbind(sid: str) -> None:
    try:
        _bind_path(sid).unlink()
    except FileNotFoundError:
        pass


def _txt(obj) -> str:
    if not isinstance(obj, dict):
        return str(obj)
    if "key" in obj:
        s = text.labels().get(obj["key"], obj["key"])
        for k, v in (obj.get("args") or {}).items():
            s = s.replace("{" + k + "}", str(v))
        return s
    lang = config.load_global().get("reader_lang") or "en"
    return obj.get(lang) or obj.get("en") or ""


def _parked(w: dict) -> str:
    pk = w.get("parked")
    return f" [parked for quota, resumes {pk.get('resume_at') or 'when usage drops'}]" if pk else ""


def _self_line(st: dict) -> str:
    """Its own unread calls: the ones that want a look by title, the routine rest as a count."""
    opened = [d for d in st["decisions"] if d["kind"] == "self" and d["status"] == "open"]
    look = [d for d in opened if d.get("attention") is True]
    named = "; ".join(f"{d['id']} {_txt(d['title'])[:120]}" for d in look[:5]) or "-"
    if len(look) > 5:
        named += f"; {len(look) - 5} more"
    return f"Decided by itself, unread ({len(opened)}): want a look: {named}; routine: {len(opened) - len(look)}"


def _budget_line() -> str:
    cur = budget.current(config.load_global(), datetime.now().astimezone())
    b = cur["budget"]
    if cur["spent"] is not None:
        wa = cur["workers"]
        return (f"Budget (gateway profile {cur['profile']['name']}): ${cur['spent']['today']:.2f} spent today"
                f"{' (some cost unknown)' if cur['spent']['unknown'] else ''}, new workers "
                f"{'no limit' if wa['allow'] is None else wa['allow']} ({wa['reason']}); dispatcher passes use the "
                "subscription")
    if not b.get("ok"):
        return "Budget: unknown"
    parts = [f"{w.get('kind')}{'/' + w['model'] if w.get('model') else ''} {w.get('percent')}%" for w in b["windows"]]
    parts.append(f"credits ${b['credits_used_usd']:.0f}/${b['credits_limit_usd']:.0f}")
    return "Budget (whole account): " + ", ".join(parts) + (" (stale)" if b.get("stale") else "")


def _rules_files(name: str) -> list:
    return [paths.data_root() / "RULES.md", paths.ap_dir(name) / "RULES.md"]


def build(name: str, offset, rules_mtimes: dict, now: datetime) -> tuple:
    head = [f"autopilot snapshot: {name} at {now.strftime('%Y-%m-%d %H:%M %Z').strip()}",
            "Treat this snapshot as the current truth; it is newer than anything earlier in this chat."]
    try:
        st = state.load(name)
    except state.StateError as e:
        return "\n".join(head + [f"State file is broken: {e}"]), offset or 0, rules_mtimes
    counts = {}
    for t in st["tasks"]:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    asks = [d for d in st["decisions"] if d["status"] == "open" and d["kind"] == "ask"]
    gates = [d for d in st["decisions"] if d["status"] == "open" and d["kind"] == "gate"]
    body = [
        f"Folder: {paths.ap_dir(name)}",
        "Tasks: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none"),
        "Workers: " + (", ".join(f"{w['session_name']} (ports {w['ports'][0]}-{w['ports'][1]})" + _parked(w)
                                  for w in st["workers"]) or "none"),
        f"Waiting on you ({len(asks)}): " + ("; ".join(f"{d['id']} {_txt(d['title'])}" for d in asks) or "-"),
        f"Human gates ({len(gates)}): " + ("; ".join(f"{d['id']} {_txt(d['title'])}" for d in gates) or "-"),
        _self_line(st),
        _budget_line(),
    ]
    evs, new_off = events.since(name, offset or 0)
    if offset is None:
        evs = evs[-5:]
    ev_lines = [f"{e['at'][11:16]}Z {e['type']} {json.dumps(e.get('data'), ensure_ascii=False)[:160]}"
                for e in evs[-MAX_EVENTS:]]
    rules = []
    new_mt = dict(rules_mtimes)
    for f in _rules_files(name):
        if f.exists():
            mt = str(int(f.stat().st_mtime))
            if rules_mtimes.get(str(f)) != mt:
                rules.append(f"Rules updated ({f}):\n{f.read_text()[:2000]}")
            new_mt[str(f)] = mt
    parts = head + body
    if ev_lines:
        parts += ["Since your last message:"] + ev_lines
    parts += rules
    out = "\n".join(parts)
    while len(out) > LIMIT and ev_lines:
        ev_lines.pop(0)
        out = "\n".join(head + body + (["Since your last message:"] + ev_lines if ev_lines else []) + rules)
    return out[:LIMIT], new_off, new_mt


def _is_ours(prompt: str) -> bool:
    return prompt == "/autopilot" or prompt.startswith("/autopilot ")


def handle(payload: dict):
    sid = payload["session_id"]
    prompt = (payload.get("prompt") or "").strip()
    hint = None
    if _is_ours(prompt):
        words = prompt.split()
        arg = words[1] if len(words) > 1 else ""
        if arg == "off":
            unbind(sid)
            return "autopilot: unbound"
        if arg in config.list_aps():
            bind(sid, arg)
        elif arg not in VERBS or binding(sid) is None:
            hint = f"autopilot: session_id={sid}"
    b = binding(sid)
    if b is None:
        return hint
    out, off, mt = build(b["name"], b.get("offset"), b.get("rules_mtimes") or {}, datetime.now().astimezone())
    b.update(offset=off, rules_mtimes=mt)
    _save_binding(sid, b)
    return out if hint is None else f"{hint}\n{out}"


@cli.command("hook-prompt")
def _hook(args: list) -> int:
    try:
        payload = json.loads(sys.stdin.read())
        ctx = handle(payload)
        if ctx:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                     "additionalContext": ctx}}, ensure_ascii=False))
    except Exception:  # a hook must never break the user's session
        pass
    return 0


@cli.command("bind")
def _bind(args: list) -> int:
    bind(args[0], args[1])
    print(f"bound {args[0]} to {args[1]}")
    return 0


@cli.command("unbind")
def _unbind(args: list) -> int:
    unbind(args[0])
    return 0
