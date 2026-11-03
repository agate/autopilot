from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from autopilot import budget, cli, config, nextwork, paths, state, text, usage

COLLECTIONS = ("meta", "autopilots", "tasks", "workers", "decisions", "archive", "reports", "evidence")
REPORTS_KEPT = 10
ARCHIVE_KEPT = 200


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


def _evidence(ap: str, task: str, assets: dict):
    run = paths.ap_dir(ap) / "runs" / task
    parts = {k: _read_json(run / f"{k}.json") for k in ("handback", "review", "prloop", "evidence")}
    if not any(parts.values()):
        return None
    ev = dict(parts["evidence"] or {})
    shots = []
    for s in ev.get("screenshots") or []:
        key = f"{ap}/{task}/{s['path'].split('/')[-1]}"
        shots.append(dict(s, key=key, asset_id=assets.get(key)))
    ev["screenshots"] = shots
    hb = parts["handback"] or {}
    ev["criteria"] = ev.get("criteria") or hb.get("criteria")
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


def diff(prev: dict, cur: dict) -> dict:
    sets, dels = [], []
    for c in COLLECTIONS:
        p, n = prev.get(c) or {}, cur.get(c) or {}
        for k, v in n.items():
            if k not in p or not _same(p[k], v):
                sets.append({"collection": c, "doc_id": k, "data": v})
        for k in p:
            if k not in n:
                dels.append({"collection": c, "doc_id": k})
    assets = []
    for ev in (cur.get("evidence") or {}).values():
        for s in ev.get("screenshots") or []:
            if not s.get("asset_id"):
                assets.append({"key": s["key"], "local_path": str(paths.ap_dir(ev["ap"]) / "runs" / ev["task"] / s["path"])})
    return {"set": sets, "delete": dels, "assets": assets}


def mark(cur: dict, new_assets: dict) -> None:
    p = _page()
    p.setdefault("assets", {}).update(new_assets)
    p["published"] = cur
    p["published_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _save_page(p)


@cli.command("publish")
def _cmd(args: list) -> int:
    aps = config.list_aps()
    if args[:1] == ["diff"]:
        d = diff(published(), collect(aps))
        d["url"] = _page().get("url")
        print(json.dumps(d, indent=1, ensure_ascii=False))
    elif args[:1] == ["mark"]:
        new = dict(a.split("=", 1) for a in args[1:] if a != "--assets" and "=" in a)
        mark(collect(aps), new)
    elif args[:1] == ["url"]:
        if len(args) == 2:
            p = _page()
            p["url"] = args[1]
            _save_page(p)
        print(_page().get("url") or "")
    else:
        print("usage: ap publish diff | mark [--assets key=url ...] | url [url]", file=sys.stderr)
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
