from __future__ import annotations

import json
import shutil
import subprocess
import sys

from autopilot import cli, config

GREEN = {"SUCCESS", "NEUTRAL", "SKIPPED"}
THREADS_QUERY = """
query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){
reviewThreads(first:100){nodes{isResolved comments(first:1){nodes{author{__typename login}}}}}}}}
"""


def latest_reviews(reviews: list) -> dict:
    """Each reviewer's last review that took a stand (comments alone do not replace an earlier verdict)."""
    out = {}
    for r in sorted(reviews or [], key=lambda r: r.get("submittedAt") or ""):
        if r.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            out[(r.get("author") or {}).get("login") or "?"] = r
    return out


def evaluate(pr: dict, threads: list, stale_overlap: list) -> tuple:
    why = []
    rollup = pr.get("statusCheckRollup") or []
    if not rollup or any((c.get("conclusion") or c.get("state") or "").upper() not in GREEN for c in rollup):
        why.append("checks_not_green")
    latest = latest_reviews(pr.get("reviews"))
    if any(r.get("state") == "CHANGES_REQUESTED" for r in latest.values()):
        why.append("changes_requested")
    approvals = [r for r in latest.values() if r.get("state") == "APPROVED"]
    decision = pr.get("reviewDecision") or ""  # empty when the repo requires no review
    if decision == "REVIEW_REQUIRED":
        # GitHub still wants a review that counts (for example a code owner's), whoever else approved.
        why.append("review_required")
    elif decision:
        if not approvals:
            why.append("approval_missing")
        elif not any((r.get("commit") or {}).get("oid") == pr.get("headRefOid") for r in approvals):
            why.append("approval_not_on_head")
    for t in threads:
        first = ((t.get("comments") or {}).get("nodes") or [{}])[0]
        if not t.get("isResolved") and (first.get("author") or {}).get("__typename") != "Bot":
            why.append("unresolved_human_thread")
            break
    if stale_overlap:
        why.append("rerun_ci_on_current_main")
    return (not why, why)


def _run(argv: list, cwd: str) -> str:
    out = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f"{' '.join(argv[:3])}: {out.stderr.strip()[:300]}")
    return out.stdout


def gather(gh_bin: str, repo: str, number: int) -> tuple:
    pr = json.loads(_run([gh_bin, "pr", "view", str(number), "--json",
                          "headRefOid,reviews,reviewDecision,statusCheckRollup,files,baseRefName"], repo))
    meta = json.loads(_run([gh_bin, "repo", "view", "--json", "owner,name"], repo))
    data = json.loads(_run([gh_bin, "api", "graphql", "-f", f"query={THREADS_QUERY}",
                            "-F", f"owner={meta['owner']['login']}", "-F", f"name={meta['name']}",
                            "-F", f"number={number}"], repo))
    threads = data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    base = pr["baseRefName"]
    _run(["git", "fetch", "origin", base], repo)
    mb = _run(["git", "merge-base", pr["headRefOid"], f"origin/{base}"], repo).strip()
    moved = set(_run(["git", "diff", "--name-only", f"{mb}..origin/{base}"], repo).split())
    mine = {f["path"] for f in pr.get("files") or []}
    return pr, threads, sorted(moved & mine)


@cli.command("merge-check")
def _cmd(args: list) -> int:
    if len(args) != 2:
        print("usage: ap merge-check <ap> <pr-number>", file=sys.stderr)
        return 2
    gcfg, apcfg = config.load_global(), config.load_ap(args[0])
    gh = gcfg["tools"].get("gh") or shutil.which("gh") or "gh"
    pr, threads, overlap = gather(gh, apcfg["repo"], int(args[1]))
    ok, why = evaluate(pr, threads, overlap)
    print(json.dumps({"ok": ok, "reasons": why, "overlap": overlap}))
    return 0 if ok else 1
