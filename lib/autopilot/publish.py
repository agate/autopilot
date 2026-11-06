from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from autopilot import budget, cli, config, nextwork, paths, state, text, usage

COLLECTIONS = ("meta", "autopilots", "tasks", "workers", "decisions", "archive", "reports", "evidence")
REPORTS_KEPT = 10
ARCHIVE_KEPT = 200
# The page shows each screenshot as an <img>, so only these are screenshots (the asset store takes them all).
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")
# The page shows each log as text. The asset store refuses .log and .tap, so those go up under a .txt name.
LOG_EXT = (".txt", ".log", ".tap", ".json", ".md")
UPLOAD_EXT = IMAGE_EXT + (".txt", ".json", ".md")


def _page_file():
    return paths.data_root() / "page.json"


def _page() -> dict:
    try:
        return json.loads(_page_file().read_text())
    except (OSError, ValueError):
        return {}


def _save_page(p: dict) -> None:
    f = _page_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(p, indent=1, ensure_ascii=False, sort_keys=True) + "\n")


def published() -> dict:
    return _page().get("published") or {}


def _read_json(p):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def _labels(gcfg: dict) -> dict:
    lang = gcfg.get("reader_lang") or "en"
    out = {"reader_lang": lang, "en": text.labels()}
    if lang != "en":
        out[lang] = _read_json(paths.data_root() / f"labels.{lang}.json") or {}
    return out


def _inside(run, entry):
    """The entry's path relative to the run directory, or None when it points outside it."""
    try:
        return (run / entry["path"]).resolve().relative_to(run.resolve())
    except (KeyError, TypeError, ValueError):
        return None


def _files(ap: str, task: str, run, entries, exts: tuple, assets: dict) -> list:
    out = []
    for s in entries or []:
        inside = _inside(run, s)
        if inside is None or inside.suffix.lower() not in exts:
            continue  # a path outside the run directory is never uploaded; a log is no <img>, nor a picture a log
        key = f"{ap}/{task}/{inside.name}"
        out.append(dict(s, path=str(inside), key=key, asset_id=assets.get(key)))
    return out


def _handback_criteria(hb: dict, review) -> list:
    """The hand-back's criteria in the page's shape, with the reviewer's status where it gave one."""
    seen = (review or {}).get("criteria")
    seen = seen if isinstance(seen, dict) else {}
    out = []
    for c in hb.get("criteria") or []:
        status = (seen.get(c.get("id")) or {}).get("status") or c.get("status")
        out.append({"text": c.get("id"), "ok": status == "met", "proof": c.get("evidence")})
    return out


def _evidence(ap: str, task: str, assets: dict):
    run = paths.ap_dir(ap) / "runs" / task
    parts = {k: _read_json(run / f"{k}.json") for k in ("handback", "review", "prloop", "evidence")}
    if not any(parts.values()):
        return None
    ev = dict(parts["evidence"] or {})
    ev["screenshots"] = _files(ap, task, run, ev.get("screenshots"), IMAGE_EXT, assets)
    ev["logs"] = _files(ap, task, run, ev.get("logs"), LOG_EXT, assets)
    hb = parts["handback"] or {}
    ev["criteria"] = ev.get("criteria") or _handback_criteria(hb, parts["review"])
    ev["checks"] = hb.get("checks")
    ev["review"] = parts["review"]
    ev["prloop"] = parts["prloop"]
    ev["ap"], ev["task"] = ap, task
    return ev


def collect(aps: list) -> dict:
    gcfg = config.load_global()
    assets = _page().get("assets") or {}
    out = {c: {} for c in COLLECTIONS}
    b = budget.read()
    out["meta"]["budget"] = {"budget": b, "allowance": budget.allowance(b, gcfg, datetime.now(timezone.utc))}
    active = config.active_aps()
    out["meta"]["cap"] = {"max_workers": gcfg["max_workers"], "active": len(active),
                          "warning": gcfg["max_workers"] < len(active)}
    out["meta"]["labels"] = _labels(gcfg)
    gate = _read_json(paths.data_root() / "gate.json") or {}
    out["meta"]["health"] = {"broken": gate.get("broken") or {}, "last_gate_at": gate.get("last_run_at")}
    rc = gcfg.get("reply_check") or {}
    offset = datetime.now().astimezone().utcoffset()
    # The page tells the reader when an answer is picked up: within a minute, or at the next forced pass
    # in the quiet hours (machine time, hence the offset) or when the check is off.
    out["meta"]["reply_check"] = {"every_s": rc.get("every_s") or 0, "quiet": rc.get("quiet") or [],
                                  "forced_pass_min": gcfg["forced_pass_min"],
                                  "utc_offset_min": int(offset.total_seconds() // 60) if offset else 0}
    for ap in aps:
        try:
            cfg, st = config.load_ap(ap), state.load(ap)
        except (state.StateError, FileNotFoundError):
            continue
        counts = {}
        for t in st["tasks"]:
            counts[t["status"]] = counts.get(t["status"], 0) + 1
        out["autopilots"][ap] = {"name": ap, "status": config.status_of(ap), "source": cfg.get("source"),
                                 "counts": counts, "workers": len(st["workers"]),
                                 "waiting": sum(1 for d in st["decisions"] if d["status"] == "open" and d["kind"] == "ask"),
                                 "last_report": st.get("last_report"),
                                 "tokens": usage.autopilot_tokens(ap, [w["session_id"] for w in st["workers"]])}
        ready = {t["id"] for t in nextwork.ready(st)}
        for t in st["tasks"]:
            out["tasks"][f"{ap}:{t['id']}"] = dict(t, ap=ap, ready=t["id"] in ready)
            ev = _evidence(ap, t["id"], assets)
            if ev:
                out["evidence"][f"{ap}:{t['id']}"] = ev
        for w in st["workers"]:
            out["workers"][f"{ap}:{w['task']}:{w['role']}"] = {
                "ap": ap, "task": w["task"], "role": w["role"], "session_name": w["session_name"],
                "started_at": w["started_at"], "resumes": w["resumes"]}
            if w.get("parked"):
                out["workers"][f"{ap}:{w['task']}:{w['role']}"]["parked"] = {
                    "resume_at": w["parked"].get("resume_at"), "percent": w["parked"].get("percent")}
        for d in st["decisions"]:
            out["decisions"][f"{ap}:{d['id']}"] = dict(d, ap=ap)
        arch = paths.ap_dir(ap) / "archive.jsonl"
        if arch.exists():
            for line in arch.read_text().splitlines()[-ARCHIVE_KEPT:]:
                d = json.loads(line)
                out["archive"][f"{ap}:{d['id']}"] = dict(d, ap=ap)
        reps = sorted((paths.ap_dir(ap) / "reports").glob("*.json"))[-REPORTS_KEPT:]
        for r in reps:
            data = _read_json(r)
            if data:
                out["reports"][f"{ap}:{r.stem}"] = dict(data, ap=ap)
    return out


def _same(a, b) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def diff(prev: dict, cur: dict, only: list = None) -> dict:
    """What to send to the page. With `only`, just those collections (and no asset uploads)."""
    cols = [c for c in COLLECTIONS if not only or c in only]
    sets, dels = [], []
    for c in cols:
        p, n = prev.get(c) or {}, cur.get(c) or {}
        for k, v in n.items():
            if k not in p or not _same(p[k], v):
                sets.append({"collection": c, "doc_id": k, "data": v})
        for k in p:
            if k not in n:
                dels.append({"collection": c, "doc_id": k})
    assets = []
    for ev in (cur.get("evidence") or {}).values() if "evidence" in cols else []:
        for s in (ev.get("screenshots") or []) + (ev.get("logs") or []):
            if not s.get("asset_id"):
                name = Path(s["path"]).name
                assets.append({"key": s["key"], "local_path": str(paths.ap_dir(ev["ap"]) / "runs" / ev["task"] / s["path"]),
                               "upload_name": name if name.lower().endswith(UPLOAD_EXT) else name + ".txt"})
    return {"set": sets, "delete": dels, "assets": assets}


def record_assets(new_assets: dict) -> None:
    """Remember uploaded asset ids by key, so the next diff's rows carry them. Publishes nothing."""
    p = _page()
    # Keep the bare id: the page builds "/_blob/<id>" itself, so a stored url would double the prefix.
    p.setdefault("assets", {}).update({k: v.rsplit("/", 1)[-1] for k, v in new_assets.items()})
    _save_page(p)


def mark(cur: dict, new_assets: dict, only: list = None) -> None:
    """Record `cur` as published. With `only`, just those collections: the rest stay as they were, so the
    next full diff still lists what changed in them."""
    record_assets(new_assets)
    p = _page()
    p["published"] = cur if not only else dict(p.get("published") or {}, **{c: cur.get(c) or {} for c in only})
    p["published_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _save_page(p)


@cli.command("publish")
def _cmd(args: list) -> int:
    aps = config.list_aps()
    only = None
    if "--only" in args:
        i = args.index("--only")
        only = (args[i + 1] if i + 1 < len(args) else "").split(",")
        args = args[:i] + args[i + 2:]
        if args[:1] not in (["diff"], ["mark"]) or not all(c in COLLECTIONS for c in only):
            print(f"--only takes {', '.join(COLLECTIONS)} (comma-separated) after diff or mark", file=sys.stderr)
            return 2
    if args[:1] == ["diff"]:
        d = diff(published(), collect(aps), only)
        d["url"] = _page().get("url")
        print(json.dumps(d, indent=1, ensure_ascii=False))
    elif args[:1] == ["assets"]:
        record_assets(dict(a.split("=", 1) for a in args[1:] if "=" in a))
    elif args[:1] == ["mark"]:
        new = dict(a.split("=", 1) for a in args[1:] if a != "--assets" and "=" in a)
        mark(collect(aps), new, only)
    elif args[:1] == ["url"]:
        if len(args) == 2:
            p = _page()
            p["url"] = args[1]
            _save_page(p)
        print(_page().get("url") or "")
    else:
        print("usage: ap publish diff [--only <collections>] | assets key=<asset id> ... | mark [--only <collections>] [--assets key=<asset id> ...] | url [url]",
              file=sys.stderr)
        return 2
    return 0


def build_page(stub: str = None):
    """Write the publishable page: the template with the English labels inlined.
    With `stub`, also inline a fake runtime serving that JSON file's collections (local preview only)."""
    html = (paths.package_root() / "templates" / "page.html").read_text()
    html = html.replace("/*LABELS_EN*/null", json.dumps(text.labels(), ensure_ascii=False))
    out = paths.data_root() / "page.html"
    if stub:
        data = json.loads(open(stub).read())
        fake = ("<script>window.claude={use:async function(n){if(n!=='db')return null;var D=" + json.dumps(data) +
                ";return{collection:function(c){return{onSnapshot:function(next){var docs=Object.keys(D[c]||{})"
                ".map(function(k){return{id:k,exists:true,data:function(){return D[c][k]}}});setTimeout(function()"
                "{next({docs:docs,metadata:{fromCache:false}})},0);return function(){}},add:async function(){}}}}}};"
                "</script>")
        html = fake + html
        out = paths.data_root() / "page-preview.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    return out


@cli.command("page")
def _page_cmd(args: list) -> int:
    if args[:1] != ["build"]:
        print("usage: ap page build [--stub data.json]", file=sys.stderr)
        return 2
    print(build_page(args[args.index("--stub") + 1] if "--stub" in args else None))
    return 0


def evidence_errors(ap: str, task: str) -> list:
    """evidence.json must exist and carry every reader-facing text in every required language."""
    p = paths.ap_dir(ap) / "runs" / task / "evidence.json"
    ev = _read_json(p)
    if not isinstance(ev, dict):
        return [f"{p}: missing or not valid JSON"]
    langs = text.required_langs(config.load_global())
    errs = []
    for k in ("verdict", "what_changed"):
        errs += text.check(ev.get(k), langs, k)
    for i, c in enumerate(ev.get("criteria") or []):
        errs += text.check(c.get("text"), langs, f"criteria[{i}].text")
        if "proof" in c:
            errs += text.check(c["proof"], langs, f"criteria[{i}].proof")
    for field, exts, kind, other in (("screenshots", IMAGE_EXT, "an image", "logs"),
                                     ("logs", LOG_EXT, "a log", "screenshots")):
        for i, sh in enumerate(ev.get(field) or []):
            errs += text.check(sh.get("caption"), langs, f"{field}[{i}].caption")
            inside = _inside(p.parent, sh)
            if inside is None or not (p.parent / inside).is_file():
                errs.append(f"{field}[{i}].path: {sh.get('path')} is not a file in the run directory")
            elif inside.suffix.lower() not in exts:
                errs.append(f"{field}[{i}].path: {sh.get('path')} is not {kind} ({' '.join(exts)}); "
                            f"list it under {other} instead")
    return errs


def add_media(ap: str, task: str, files: list) -> list:
    """Copy files into the task's media/ folder. A worker's shell may not write into the data folder and its
    Write tool cannot write a binary file, so a screenshot comes in through this command."""
    if not paths.valid_name(ap) or task not in {t["id"] for t in state.load(ap)["tasks"]}:
        raise ValueError(f"{ap}: no task {task}")
    srcs = [Path(f) for f in files]
    for s in srcs:
        if not s.is_file():
            raise ValueError(f"{s}: not a file")
    if len({s.name for s in srcs}) < len(srcs):
        raise ValueError("two files share a name")
    media = paths.ap_dir(ap) / "runs" / task / "media"
    media.mkdir(parents=True, exist_ok=True)
    for s in srcs:
        shutil.copyfile(s, media / s.name)
    return [f"media/{s.name}" for s in srcs]


@cli.command("evidence")
def _evidence_cmd(args: list) -> int:
    if args[:1] == ["add"] and len(args) >= 4:
        try:
            print("\n".join(add_media(args[1], args[2], args[3:])))
        except (ValueError, OSError, state.StateError) as e:
            print(e, file=sys.stderr)
            return 1
        return 0
    if args[:1] != ["check"] or len(args) != 3:
        print("usage: ap evidence check <ap> <task> | add <ap> <task> <file> ...", file=sys.stderr)
        return 2
    errs = evidence_errors(args[1], args[2])
    print("\n".join(errs) if errs else "ok")
    return 1 if errs else 0
