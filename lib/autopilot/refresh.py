"""A cheap page refresh that needs no dispatcher pass.

A pass is the only thing that publishes the page, so while the budget pauses passes the page would go
stale. This step keeps the light collections fresh instead. Python prepares everything (one JSON file per
document, the list of writes); a tiny Haiku background session only reads the documents' current versions
and sends the writes in `ArtifactData` batches. Evidence and its uploaded files stay with passes.

Only the session's own tool calls and results recorded in its transcript count as proof, never its words.
After proof, the collections are marked published (`publish.mark(..., only=...)`), so a later full diff
still lists whatever the refresh did not carry (evidence, archive).
"""
from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autopilot import access, config, lock, paths, publish, replies, sessions, usage, workers

NAME = "autopilot · page refresh"
LIGHT = ("meta", "autopilots", "tasks", "workers", "decisions", "reports")
DEBOUNCE_MIN = 3        # a change refreshes the page at most this often
BACKOFF_MIN = (5, 10, 20, 40, 60)
SIGNAL_AFTER = 3        # failures in a row before an access card opens
TIMEOUT_S = 180
MAX_WRITES = 50         # one ArtifactData batch
MAX_BYTES = 600_000     # of document data per batch (the tool's limit is 1 MiB for the whole body)
GET_LIMIT = 10          # up to this many touched documents in a collection are read one by one; more, one list
SYSTEM = ("You copy prepared writes to one page database with the ArtifactData tool. Do exactly the steps you are "
          "given, nothing else. Never guess: if you cannot call the tool, reply FAILED.")
FAIL_WORDS = re.compile(r"refus|conflict|mismatch|nothing (was )?written|fail|error|invalid|denied|not allowed", re.I)


def _volatile_free(cur: dict) -> dict:
    """The data without numbers that change on their own: usage and spend, the gate's check time, token totals."""
    out = copy.deepcopy({c: cur.get(c) or {} for c in LIGHT})
    meta = out["meta"]
    b = meta.get("budget")
    if isinstance(b, dict):
        a = b.get("allowance") if isinstance(b.get("allowance"), dict) else {}
        meta["budget"] = {"allowance": {"reason": a.get("reason"), "paused_until": a.get("paused_until")}}
    if isinstance(meta.get("health"), dict):
        meta["health"].pop("last_gate_at", None)
    for a in out["autopilots"].values():
        a.pop("tokens", None)
    return out


def meaningful_change(prev: dict, cur: dict) -> bool:
    """True when the published snapshot differs from the current data in a light collection, ignoring the
    volatile numbers (usage percent, ages, the gate's check time, token totals)."""
    d = publish.diff(_volatile_free(prev), _volatile_free(cur), list(LIGHT))
    return bool(d["set"] or d["delete"])


def _age_min(iso, now: datetime):
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (now.astimezone(timezone.utc) - t).total_seconds() / 60


def due(gcfg: dict, rs: dict, now: datetime, prev: dict, cur: dict):
    """Why a refresh should run now: "change", "periodic", or None."""
    if rs.get("next_at") and now < datetime.fromisoformat(rs["next_at"]):
        return None
    last = rs.get("last_at")
    if last and now - datetime.fromisoformat(last) < timedelta(minutes=DEBOUNCE_MIN):
        return None
    if meaningful_change(prev, cur):
        return "change"
    every = gcfg.get("page_refresh_min") or 0
    age = _age_min(publish._page().get("published_at"), now)
    if every and (age is None or age >= every):
        return "periodic"
    return None


def prepare(prev: dict, cur: dict, folder) -> dict:
    """Write one JSON file per document to `folder` and return the plan:
    {"writes": [{op, collection, doc_id, file_path?}], "collections": {name: [doc ids]}}."""
    d = publish.diff(prev, cur, list(LIGHT))
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    writes, touched = [], {}
    for i, row in enumerate(d["set"]):
        f = folder / f"{i:04d}.json"
        f.write_text(json.dumps(row["data"], ensure_ascii=False))
        writes.append({"op": "set", "collection": row["collection"], "doc_id": row["doc_id"], "file_path": str(f)})
        touched.setdefault(row["collection"], []).append(row["doc_id"])
    for row in d["delete"]:
        writes.append({"op": "delete", "collection": row["collection"], "doc_id": row["doc_id"]})
        touched.setdefault(row["collection"], []).append(row["doc_id"])
    plan = {"writes": writes, "collections": touched}
    (folder / "manifest.json").write_text(json.dumps(plan, indent=1, ensure_ascii=False))
    return plan


def batches(writes: list) -> list:
    """Split into groups the tool accepts: at most MAX_WRITES entries and MAX_BYTES of document data."""
    out, cur, size = [], [], 0
    for w in writes:
        n = Path(w["file_path"]).stat().st_size if w.get("file_path") else 0
        if cur and (len(cur) >= MAX_WRITES or size + n > MAX_BYTES):
            out.append(cur)
            cur, size = [], 0
        cur.append(w)
        size += n
    return out + ([cur] if cur else [])


def build_prompt(url: str, plan: dict) -> str:
    reads = []
    for c, ids in sorted(plan["collections"].items()):
        if len(ids) <= GET_LIMIT:
            reads.append(f'- collection {c}: call action get once for each doc_id in {json.dumps(ids)}')
        else:
            reads.append(f"- collection {c}: call action list once (query limit 1000; follow next_cursor if there is one)")
    groups = batches(plan["writes"])
    sends = [f"- batch {i + 1}: {json.dumps(g, ensure_ascii=False)}" for i, g in enumerate(groups)]
    return (f"Load the ArtifactData tool with ToolSearch (query select:ArtifactData) if needed. The page url is {url}.\n"
            f"Step 1, read the current version of each document you will write, with ArtifactData and the url above:\n"
            + "\n".join(reads) + "\n"
            f"Step 2, send the batches below, one call each: action batch, the url, and writes = that batch's entries "
            f"exactly as given (keep file_path as it is, never read or retype a file). Add if_version (the version "
            f"your step 1 showed) to every entry whose document exists; leave if_version out for a document that does "
            f"not exist; drop a delete entry whose document does not exist.\n" + "\n".join(sends) + "\n"
            f"Reply DONE when every batch call succeeded, otherwise FAILED and the error.")


def verify(path, url: str, plan: dict):
    """Check the transcript: every planned `set` and nothing unplanned was sent in ArtifactData batch calls
    that all succeeded. Returns "" when proven, else the reason."""
    try:
        lines = [json.loads(l) for l in Path(path).read_text(errors="replace").splitlines() if l.strip()]
    except (OSError, TypeError, ValueError):
        return "no transcript to check"
    calls = {}
    for e in lines:
        for c in ((e.get("message") or {}).get("content") or []) if e.get("type") == "assistant" else []:
            if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "ArtifactData":
                inp = c.get("input") or {}
                if inp.get("action") == "batch" and inp.get("url") == url:
                    calls[c.get("id")] = inp
    if not calls:
        return "no ArtifactData batch call in the transcript"
    results = {}
    for e in lines:
        for c in ((e.get("message") or {}).get("content") or []) if e.get("type") == "user" else []:
            if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("tool_use_id") in calls:
                results[c["tool_use_id"]] = c
    sent, want = set(), {(w["op"], w["collection"], w["doc_id"]): w.get("file_path") for w in plan["writes"]}
    for cid, inp in calls.items():
        r = results.get(cid)
        if r is None:
            return "a batch call has no result"
        body = r.get("content")
        body = " ".join(b.get("text", "") for b in body if isinstance(b, dict)) if isinstance(body, list) else str(body or "")
        if r.get("is_error") or FAIL_WORDS.search(body):
            return "a batch call failed: " + re.sub(r"\s+", " ", body).strip()[:200]
        for w in inp.get("writes") or []:
            key = (w.get("op"), w.get("collection"), w.get("doc_id"))
            if key not in want or (key[0] == "set" and w.get("file_path") != want[key]):
                return f"an unplanned write was sent: {key}"
            sent.add(key)
    missing = [k for k in want if k[0] == "set" and k not in sent]
    return f"{len(missing)} planned writes were never sent" if missing else ""


def run_session(gcfg: dict, url: str, prompt: str, cwd, poll_s: float = 2) -> dict:
    """Run the prompt in a throwaway Haiku session. {"ok": True, "transcript": path} or {"ok": False, "error"}."""
    rc = gcfg.get("reply_check") or {}
    claude = workers.claude_bin(gcfg)
    argv = [claude, "--bg", "-n", NAME, "--model", rc.get("model") or "haiku", "--strict-mcp-config",
            "--setting-sources", "project", f"--disallowedTools={replies.DISALLOWED}", "--system-prompt", SYSTEM,
            "--allowedTools=ArtifactData", "--", prompt]
    try:
        short = workers._launch(argv, str(cwd), workers._env("refresh"))
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "error": f"could not start the refresh: {e}"}
    try:
        deadline = time.monotonic() + TIMEOUT_S
        s = None
        while time.monotonic() < deadline:
            s = sessions.find_by_id(short, sessions.list_all(claude) or [])
            if s and s.get("state") == "done":
                return {"ok": True, "transcript": usage.find_transcript(s.get("sessionId"))}
            time.sleep(poll_s)
        return {"ok": False, "error": "the refresh did not finish in time"}
    finally:
        replies._cleanup(claude, short)


def _fail(gcfg: dict, rs: dict, now: datetime, error: str) -> None:
    n = int(rs.get("errors") or 0) + 1
    rs.update(errors=n, error=error,
              next_at=(now + timedelta(minutes=BACKOFF_MIN[min(n, len(BACKOFF_MIN)) - 1])).isoformat())
    if n >= SIGNAL_AFTER:
        rs["signaled"] = True
        for ap in config.active_aps():
            try:
                access.record(ap, "page_refresh", False, error,
                              "run /autopilot doctor; the next pass publishes the page",
                              {"key": "blocks_check", "args": {"name": access.NAMES["page_refresh"]}})
            except Exception:   # noqa: BLE001 - the gate must keep running
                pass


def _ok(rs: dict, now: datetime) -> None:
    if rs.get("signaled"):
        for ap in config.active_aps():
            try:
                access.record(ap, "page_refresh", True)
            except Exception:   # noqa: BLE001
                pass
    for k in ("error", "next_at", "signaled"):
        rs.pop(k, None)
    rs.update(errors=0, ok_at=now.isoformat())


def step(gcfg: dict, gs: dict, now: datetime) -> bool:
    """Refresh the page when due. True when a session ran. Keeps its bookkeeping in gs["refresh"]:
    last_at, ok_at, errors, error, next_at."""
    page = publish._page()
    if not page.get("url") or not page.get("published_at") or not config.list_aps():
        return False   # nothing is published yet: the first publish is a pass's job
    rs = gs.setdefault("refresh", {})
    cur = publish.collect(config.list_aps())
    reason = due(gcfg, rs, now, page.get("published") or {}, cur)
    if not reason:
        return False
    lk = paths.data_root() / "lock"
    if not lock.acquire(lk, "page refresh"):   # a pass started, or another refresh is running
        return False
    try:
        rs["last_at"] = now.isoformat()
        folder = paths.data_root() / "page-refresh"
        plan = prepare(page.get("published") or {}, cur, folder)
        if not plan["writes"]:   # only the publish time is stale; the data is the same
            publish.mark(cur, {}, list(LIGHT))
            _ok(rs, now)
            return False
        try:
            res = run_session(gcfg, page["url"], build_prompt(page["url"], plan), folder)
            err = "" if res.get("ok") else res["error"]
            if not err:
                err = verify(res.get("transcript"), page["url"], plan)
        except Exception as e:   # noqa: BLE001 - the gate must keep running whatever the refresh does
            err = f"the refresh failed: {e}"
        if err:
            _fail(gcfg, rs, now, err)
        else:
            publish.mark(cur, {}, list(LIGHT))
            _ok(rs, now)
        return True
    finally:
        lock.release(lk)
