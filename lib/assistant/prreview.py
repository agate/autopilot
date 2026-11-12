"""PR review cards (design 6.2): someone asked the user to review a pull request, and the card reviews itself.

The `pr-review` role (`commands/pr-review.md`) does the review. This module is what it and the card need around
it, built from secretary's review checks (`review roles`, `review verdict`, `report validate`), copied, not called
(design 3.2):

- `plan`: which PR the card is, its head, merge base and files (read from GitHub), the angles to run (design 6.1)
  and, for a delta, the diff since the last review. Written to `<data>/sessions/<card>/prreview-plan.json`.
- `verdict` and `check_findings`: the four finding groups and the verdict they give.
- `check`: the role's report, `<data>/sessions/<card>/prreview.json`, against the plan and the card.
- `apply`: a checked report onto the card (one store update, so a restore point is saved).
- `sync`: the PR's state on GitHub: closed, merged, reviewed by the user, request removed, or new commits.
- `plain_for`: the card's plain words (design 3.7), used by the source engine and by `apply`.

Reading GitHub never writes: every `gh` call goes through `gh_read`, which refuses anything but a fixed list of
reads before any process starts. Nothing here posts, in shadow or live mode, so nothing calls
`config.require_live`.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Callable, Optional

from assistant import angles, cards, config, sources
from assistant.config import ConfigError
from assistant.store import Store, write_json

KIND = "pr-review"
STAGES = ("review", "delta")
KEY = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
PLAN_FILE = "prreview-plan.json"
REPORT_FILE = "prreview.json"
GH_TIMEOUT = 60
GIT_TIMEOUT = 30

# The findings (secretary's lib/secretary/review.py).
BUCKETS = ("confirmed", "out_of_scope", "rejected", "questions")
SEVERITIES = ("blocker", "should-fix", "nit")
SERIOUS = ("blocker", "should-fix")
TEXT_FIELDS = ("trigger", "before", "after", "impact", "fix")
FIELDS = ("id", "severity", "where") + TEXT_FIELDS
# `checked`: how the triage verified a confirmed finding (the line opened, the command run, what it showed).
BUCKET_FIELDS = {"confirmed": ("checked",), "out_of_scope": (), "rejected": ("reason",), "questions": ("question",)}
VERDICTS = ("approve", "changes", "question")
VERDICT_WORD = {"approve": "Approve", "changes": "Changes", "question": "Question"}

# The report (secretary's lib/secretary/reports.py).
CI_STATES = ("green", "red", "pending")
REPORT_TEXT = ("tldr", "background", "problem", "solution", "reply_draft")
REPORT_KEYS = ("card", "stage", "head_sha", "reviewed_at", "verdict") + REPORT_TEXT + (
    "diagram", "findings", "angles", "checks", "since_last", "earlier")
DELTA_ONLY = ("since_last", "earlier")
TLDR_MAX = 200
EARLIER_STATES = ("open", "fixed", "answered")
_SVG_UNSAFE = re.compile(r"<\s*script|<\s*foreignObject|\son[a-z]+\s*=|javascript:", re.I)

# The PR's files, every page (secretary's PR_FILES).
PR_FILES = """query($owner:String!, $name:String!, $n:Int!, $after:String){ repository(owner:$owner, name:$name){
 pullRequest(number:$n){ headRefOid baseRefOid additions deletions
  files(first:100, after:$after){ pageInfo{ hasNextPage endCursor } nodes{ path } } } } }"""
# A delta's commits since the last review: how they relate to it, and which of them are merges.
SINCE_JQ = "{status: .status, merges: [.commits[] | select(.parents | length > 1) | .sha]}"
SYNC_FIELDS = "state,headRefOid,reviewRequests,latestReviews"

_GIT_ENV = {"GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": "", "GIT_TERMINAL_PROMPT": "0"}


class ReviewError(ConfigError):
    """A PR review step cannot go on: the card is not a PR review, GitHub could not be read, or a file is broken.
    One `ap2: ...` line, exit 1."""


class PostRefused(ReviewError):
    """A `gh` call that could write (post, comment, review, merge, a POST or a mutation) was asked for. Nothing
    here ever writes to GitHub, in shadow or live mode."""


class NotFound(ReviewError):
    """gh said HTTP 404."""


# --- reading GitHub, never writing -------------------------------------------------------------------------------

_PR_READS = ("view", "diff", "checks")
# `gh api` flags that send a body or change the method: with any of them the call may write.
_API_WRITE_LONG = ("--method", "--input")
_API_WRITE_SHORT = "XfF"
_API_FIELD_LONG = ("--field", "--raw-field")
_API_FIELD_SHORT = "fF"
# `gh api` flags that take a value, which is then not read as a flag.
_API_VALUE_LONG = ("--header", "--jq", "--template", "--cache", "--hostname", "--preview")
_API_VALUE_SHORT = "Hqtp"
_MUTATION = re.compile(r"\bmutation\b")


def refusal(args) -> Optional[str]:
    """Why the gh call `args` (without the leading `gh`) is refused, or None when it is on the read list: `pr view`,
    `pr diff`, `pr checks`, `api graphql` whose query holds no mutation, and `api <path>` with no flag that sends a
    body or changes the method (a field makes `gh api` POST)."""
    if not (isinstance(args, (list, tuple)) and args and all(isinstance(a, str) for a in args)):
        return "a gh call is a list of text arguments"
    shown = " ".join(args[:3])
    if args[0] == "pr":
        if len(args) >= 2 and args[1] in _PR_READS:
            return None
        return f"gh {shown}: only pr view, pr diff and pr checks are read; nothing is posted"
    if args[0] != "api":
        return f"gh {shown}: only pr view, pr diff, pr checks and api reads are run; nothing is posted"
    rest = list(args[1:])
    path, fields, i = None, [], 0
    while i < len(rest):
        a = rest[i]
        if a == "--":
            return f"gh {shown}: -- is refused in an api read"
        if a.startswith("--"):
            name, eq, value = a.partition("=")
            if name in _API_FIELD_LONG:  # a field: allowed only in a graphql read, checked below
                if not eq:
                    i += 1
                    value = rest[i] if i < len(rest) else ""
                fields.append((name, value))
            elif name in _API_WRITE_LONG:
                return f"gh api {name}: may write; nothing is posted"
            elif name in _API_VALUE_LONG and not eq:
                i += 1
        elif a.startswith("-") and len(a) > 1:
            j = 1
            while j < len(a):
                ch = a[j]
                if ch == "=":  # pflag gives the flag before `=` the rest as its value, and the argument ends there
                    break
                if ch in _API_FIELD_SHORT:
                    value = a[j + 1:]
                    if value.startswith("="):  # pflag reads `-f=VALUE` as VALUE, so `=` cannot hide the key
                        value = value[1:]
                    elif not value:
                        i += 1
                        value = rest[i] if i < len(rest) else ""
                    fields.append(("-" + ch, value))
                    break
                if ch in _API_WRITE_SHORT:
                    return f"gh api -{ch}: may write; nothing is posted"
                if ch in _API_VALUE_SHORT:
                    if j + 1 == len(a):
                        i += 1
                    break
                j += 1
        elif path is None:
            path = a
        else:
            return f"gh api: one path only, not {a!r}"
        i += 1
    if path is None:
        return "gh api: no path"
    if path != "graphql":
        if fields:
            return f"gh api {fields[0][0]}: a field makes gh api POST; nothing is posted"
        return None
    has_query = False
    for flag, value in fields:
        key, _, val = value.partition("=")
        if flag in ("-F", "--field") and val.startswith("@"):
            return f"gh api graphql {flag} {key}=@...: a value read from a file cannot be checked"
        if key == "query":
            if flag in ("-F", "--field"):  # gh fills {owner}, {repo} and {branch} in a typed field's value
                return f"gh api graphql {flag} query=...: the query must be a raw field (-f or --raw-field)"
            has_query = True
            if _MUTATION.search(val):
                return "gh api graphql: refusing a mutation; nothing is posted"
    if not has_query:
        return "gh api graphql: no query"
    return None


def _run_gh(argv: list) -> subprocess.CompletedProcess:
    env = dict(os.environ, GH_PROMPT_DISABLED="1")
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                          stdin=subprocess.DEVNULL, timeout=GH_TIMEOUT)


def gh_read(args, run: Optional[Callable] = None) -> str:
    """Run the read `gh <args>` and return its stdout. A call off the read list raises PostRefused before any
    process starts. `run(argv)` returns a CompletedProcess (tests pass recorded replies); the default runs `gh`
    from PATH with GH_PROMPT_DISABLED=1 and a 60 s timeout. A failure is a ReviewError naming the call and gh's
    first stderr line (NotFound for an HTTP 404)."""
    why = refusal(args)
    if why is not None:
        raise PostRefused(why)
    argv = ["gh", *args]
    call = " ".join(a if len(a) <= 60 else a[:57] + "..." for a in args[:4])
    try:
        p = (run or _run_gh)(argv)
    except FileNotFoundError:
        raise ReviewError("gh is not on PATH") from None
    except subprocess.TimeoutExpired:
        raise ReviewError(f"gh {call}: no answer in {GH_TIMEOUT} s") from None
    except OSError as e:
        raise ReviewError(f"gh {call}: cannot run gh: {e}") from None
    if p.returncode != 0:
        err = next((ln.strip() for ln in (p.stderr or "").splitlines() if ln.strip()), f"exit {p.returncode}")
        raise (NotFound if "(HTTP 404)" in (p.stderr or "") else ReviewError)(f"gh {call}: {err}")
    return p.stdout or ""


def _json(text: str, what: str):
    try:
        return json.loads(text)
    except ValueError as e:
        raise ReviewError(f"{what}: gh did not answer JSON ({e})") from None


def _sha(value, what: str) -> str:
    if not (isinstance(value, str) and SHA.match(value)):
        raise ReviewError(f"{what}: not a 40-character commit SHA: {str(value)[:80]!r}")
    return value


def graphql(query: str, run=None, **variables) -> dict:
    """The `data` of a GraphQL read. A query with a mutation is refused (by gh_read)."""
    args = ["api", "graphql", "-f", "query=" + query]
    for k, v in variables.items():
        if v is not None:
            args += ["-F" if isinstance(v, int) else "-f", f"{k}={v}"]
    reply = _json(gh_read(args, run), "api graphql")
    data = reply.get("data") if isinstance(reply, dict) else None
    if not isinstance(data, dict):
        errors = reply.get("errors") if isinstance(reply, dict) else None
        msg = errors[0].get("message") if isinstance(errors, list) and errors and isinstance(errors[0], dict) else None
        raise ReviewError(f"api graphql: no data{': ' + str(msg)[:200] if msg else ''}")
    return data


def pr_files(repo: str, number: int, run=None) -> dict:
    """{head_sha, base_sha, changed_lines, files} of a PR, every page of its file list."""
    owner, name = repo.split("/", 1)
    files, after, first = [], None, None
    for _ in range(1000):  # 100 files a page; GitHub lists at most 3000 files, so this is far more than needed
        data = graphql(PR_FILES, run, owner=owner, name=name, n=number, after=after)
        try:
            pr = (data.get("repository") or {}).get("pullRequest")
            if not pr:
                raise ReviewError(f"{repo}#{number}: no such pull request")
            first = first or pr
            files += [n["path"] for n in pr["files"]["nodes"] if n]
            info = pr["files"]["pageInfo"]
            if not info["hasNextPage"]:
                lines = first["additions"] + first["deletions"]
                if not (isinstance(lines, int) and lines >= 0) or not all(isinstance(f, str) for f in files):
                    raise TypeError("bad counts or paths")
                return {"head_sha": _sha(first["headRefOid"], "headRefOid"),
                        "base_sha": _sha(first["baseRefOid"], "baseRefOid"), "changed_lines": lines, "files": files}
            after = info["endCursor"]
            if not (isinstance(after, str) and after):
                raise TypeError("no endCursor")
        except (KeyError, TypeError, AttributeError) as e:
            raise ReviewError(f"{repo}#{number}: unexpected reply for the PR's files ({e})") from None
    raise ReviewError(f"{repo}#{number}: the file list did not end")


def compare_path(repo: str, a: str, b: str) -> str:
    return f"repos/{repo}/compare/{a}...{b}"


def merge_base(repo: str, base: str, head: str, run=None) -> str:
    """The commit the PR branched from: the "before" side of every comparison (the base tip may have moved on)."""
    out = gh_read(["api", compare_path(repo, base, head), "--jq", ".merge_base_commit.sha"], run).strip()
    return _sha(out, f"the merge base of {base[:7]}...{head[:7]}")


def whole_pr_reason(repo: str, reviewed: str, head: str, run=None) -> Optional[str]:
    """Why the commits since the last review cannot be reviewed alone, or None. After a merge commit (an "update
    branch" merge) that diff holds the base branch's changes; after a rewrite it no longer starts at the review,
    and once the reviewed commit is gone from GitHub (collected after a force-push) the compare is a 404."""
    try:
        since = _json(gh_read(["api", compare_path(repo, reviewed, head), "--jq", SINCE_JQ], run), "compare")
    except NotFound:
        return f"the last reviewed commit ({reviewed[:7]}) is no longer on GitHub (history rewritten)"
    if not isinstance(since, dict):
        raise ReviewError(f"compare {reviewed[:7]}...{head[:7]}: unexpected reply")
    merges = [m for m in since.get("merges") or [] if isinstance(m, str)]
    if merges:
        return (f"the new commits include a merge commit ({', '.join(m[:7] for m in merges)}), so the diff since "
                f"the last review holds the base branch's changes")
    if since.get("status") in ("diverged", "behind"):
        return f"the branch was rewritten since the last review (compare status {since['status']})"
    return None


# --- the card ---------------------------------------------------------------------------------------------------

def pr_of(card: dict) -> dict:
    """{repo, number, url} of a PR review card. Anything else is a ReviewError."""
    cid = card.get("id", "?")
    if card.get("kind") != KIND:
        raise ReviewError(f"card {cid} is a {card.get('kind')!r} card, not a {KIND} card")
    key = card.get("key")
    m = KEY.match(key) if isinstance(key, str) else None
    if not m:
        raise ReviewError(f"card {cid}: key {str(key)[:80]!r} is not <owner>/<repo>#<number>")
    repo, number = m.group(1), int(m.group(2))
    link = (card.get("source") or {}).get("link") if isinstance(card.get("source"), dict) else None
    url = link if isinstance(link, str) and re.match(r"https://\S+\Z", link) else \
        f"https://github.com/{repo}/pull/{number}"
    return {"repo": repo, "number": number, "url": url}


def run_dir(data, card_id: str) -> Path:
    return Path(data) / "sessions" / cards.check_id(card_id)


def _reviewed_sha(card: dict) -> Optional[str]:
    pr = card.get("pr") if isinstance(card.get("pr"), dict) else {}
    sha = pr.get("reviewed_sha")
    return None if sha is None else _sha(sha, f"card {card.get('id')}: pr.reviewed_sha")


def _git_has(checkout: str, sha: str) -> bool:
    env = dict(os.environ, **_GIT_ENV)
    try:
        p = subprocess.run(["git", "-C", checkout, "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True,
                           env=env, stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0


def _earlier_angles(card: dict, table) -> list:
    """The last review's angles that the table still has, in their order, as the table has them now."""
    block = card.get("verdict") if isinstance(card.get("verdict"), dict) else {}
    out = []
    for a in block.get("angles") or []:
        name = a.get("name") if isinstance(a, dict) else None
        angle = table.get(name) if isinstance(name, str) else None
        if angle is not None and all(o["name"] != name for o in out):
            out.append({"name": angle.name, "question": angle.question, "brief": angle.brief,
                        "origin": angle.origin, "file": angle.file, "why": "same angle as the last review"})
    return out


def plan(data, card_id: str, stage: str = "review", checkout=None, cfg=None, run=None) -> dict:
    """What the pr-review role needs before it starts its reviewers (secretary's `review roles`), written to
    `<data>/sessions/<card>/prreview-plan.json`. Nothing is fetched, and nothing on GitHub changes."""
    if stage not in STAGES:
        raise ReviewError(f"unknown stage {stage!r} (stages: {', '.join(STAGES)})")
    data = Path(data)
    cfg = cfg if cfg is not None else config.load(data)
    card = Store(data, cfg=cfg).get(card_id)
    pr = pr_of(card)
    reviewed = _reviewed_sha(card)
    if stage == "delta" and reviewed is None:
        stage = "review"
    got = pr_files(pr["repo"], pr["number"], run)
    head = got["head_sha"]
    if stage == "delta" and head == reviewed:
        raise ReviewError(f"card {card_id}: nothing new since {reviewed[:7]}")
    base = merge_base(pr["repo"], got["base_sha"], head, run)
    whole = whole_pr_reason(pr["repo"], reviewed, head, run) if stage == "delta" else None
    if checkout is None:
        repo_layer, table = "no checkout given", angles.load(data)
    elif not _git_has(str(checkout), head):
        repo_layer, table = f"{checkout} does not have {head[:7]}", angles.load(data)
    else:
        table = angles.load(data, repo=str(checkout), commit=head)
        repo_layer = f"{checkout} at {head[:7]}"
    docs_only = bool(got["files"]) and all(angles.DOCS.search(f) for f in got["files"])
    picked = _earlier_angles(card, table) if stage == "delta" else []
    if len(picked) < angles.MIN_COUNT:
        picked = angles.pick(table, got["files"], "review", got["changed_lines"])
    out = {"card": card_id, "stage": stage, "pr": pr["url"], "repo": pr["repo"], "number": pr["number"],
           "head_sha": head, "base_sha": got["base_sha"], "merge_base": base, "reviewed_sha": reviewed,
           "changed_lines": got["changed_lines"], "file_count": len(got["files"]), "docs_only": docs_only,
           "angles": picked, "repo_layer": repo_layer,
           "report": str((run_dir(data, card_id) / REPORT_FILE).resolve()), "languages": list(cards.languages(cfg))}
    if stage == "delta":
        out["compare"] = compare_path(pr["repo"], got["base_sha"] if whole else reviewed, head)
        out["whole_pr"] = whole
    write_json(run_dir(data, card_id) / PLAN_FILE, out)
    return out


# --- findings and verdict ----------------------------------------------------------------------------------------

def text_problems(value, langs, where: str) -> list:
    """A text the reader reads: {<lang>: text} holding exactly the configured languages, each non-empty."""
    if not isinstance(value, dict):
        return [f"{where}: must be a text object with {', '.join(langs)}"]
    out = []
    for lang in langs:
        if lang not in value:
            out.append(f"{where}.{lang}: missing")
        elif not (isinstance(value[lang], str) and value[lang].strip()):
            out.append(f"{where}.{lang}: must be non-empty text")
    out += [f"{where}.{k}: not a configured language ({', '.join(langs)})" for k in value if k not in langs]
    return out


def shaped(buckets) -> bool:
    """An object holding exactly the four bucket lists."""
    return isinstance(buckets, dict) and set(buckets) == set(BUCKETS) and \
        all(isinstance(buckets[b], list) for b in BUCKETS)


def check_findings(buckets, langs) -> list:
    """Problems with the four finding buckets; [] when each finding has its fields, a known severity and a unique
    id."""
    if not shaped(buckets):
        return ["findings: must be an object with exactly the lists " + ", ".join(BUCKETS)]
    out, seen = [], set()
    for b in BUCKETS:
        allowed = FIELDS + BUCKET_FIELDS[b]
        for i, f in enumerate(buckets[b]):
            at = f"findings.{b}[{i}]"
            if not isinstance(f, dict):
                out.append(f"{at}: must be an object")
                continue
            out += [f"{at}.{k}: missing" for k in allowed if k not in f]
            out += [f"{at}.{k}: not a field in {b}" for k in f if k not in allowed]
            if "id" in f:
                fid = f["id"]
                if not (isinstance(fid, str) and re.match(r"[a-z0-9][a-z0-9-]{0,79}\Z", fid)):
                    out.append(f"{at}.id: must be lowercase words joined by -, like correctness-1")
                elif fid in seen:
                    out.append(f"{at}.id: {fid} is used twice")
                else:
                    seen.add(fid)
            if "severity" in f and f["severity"] not in SEVERITIES:
                out.append(f"{at}.severity: must be one of {'|'.join(SEVERITIES)}, not {str(f['severity'])[:40]!r}")
            if "where" in f and not (isinstance(f["where"], str) and f["where"].strip()):
                out.append(f"{at}.where: must be a non-empty string (path:line at the head)")
            for k in TEXT_FIELDS + BUCKET_FIELDS[b]:
                if k in f:
                    out += text_problems(f[k], langs, f"{at}.{k}")
    return out


def verdict(buckets: dict) -> str:
    """changes when a confirmed finding is a blocker or should-fix; else question when a question is open; else
    approve (nits ride along)."""
    if any(isinstance(f, dict) and f.get("severity") in SERIOUS for f in buckets.get("confirmed") or []):
        return "changes"
    return "question" if buckets.get("questions") else "approve"


def verdict_of(obj, langs) -> dict:
    """`ap2 review verdict`: {verdict, counts, errors} for the buckets (or a report holding them as `findings`)."""
    buckets = obj.get("findings") if isinstance(obj, dict) and "findings" in obj else obj
    errors = check_findings(buckets, langs)
    if errors:
        return {"verdict": None, "counts": None, "errors": errors}
    return {"verdict": verdict(buckets), "counts": {b: len(buckets[b]) for b in BUCKETS}, "errors": []}


# --- the report --------------------------------------------------------------------------------------------------

def read_plan(data, card_id: str) -> dict:
    path = run_dir(data, card_id) / PLAN_FILE
    try:
        raw = sources.read_capped(path, sources.MAX_SOURCE, f"{path}: larger than {sources.MAX_SOURCE} bytes")
        obj = json.loads(raw.decode("utf-8"))
    except FileNotFoundError:
        raise ReviewError(f"card {card_id}: no review plan at {path}; run ap2 review plan first") from None
    except (OSError, ValueError) as e:  # a SourceError is a ConfigError, raised as it is
        raise ReviewError(f"{path}: not a review plan: {e}") from None
    except RecursionError:
        raise ReviewError(f"{path}: not a review plan: nested too deeply") from None
    if not (isinstance(obj, dict) and obj.get("card") == card_id and obj.get("stage") in STAGES
            and isinstance(obj.get("head_sha"), str) and SHA.match(obj["head_sha"])
            and isinstance(obj.get("angles"), list)):
        raise ReviewError(f"{path}: not a review plan for card {card_id}; run ap2 review plan again")
    return obj


def read_report(data, card_id: str):
    """(report, errors): the report as a JSON object, or None and why it cannot be read."""
    path = run_dir(data, card_id) / REPORT_FILE
    try:
        raw = sources.read_capped(path, sources.MAX_SOURCE,
                                  f"{REPORT_FILE}: larger than {sources.MAX_SOURCE} bytes")
    except FileNotFoundError:
        return None, [f"{REPORT_FILE}: no report at {path}"]
    except sources.SourceError as e:
        return None, [str(e)]
    except OSError as e:
        return None, [f"{REPORT_FILE}: cannot read {path}: {e}"]
    try:
        rep = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as e:
        why = "nested too deeply" if isinstance(e, RecursionError) else e
        return None, [f"{REPORT_FILE}: not JSON ({why})"]
    if not isinstance(rep, dict):
        return None, [f"{REPORT_FILE}: must be a JSON object"]
    return rep, []


def _ids(buckets, names=BUCKETS) -> dict:
    """{id: bucket} of the findings in the buckets `names`."""
    out = {}
    if isinstance(buckets, dict):
        for b in names:
            found = buckets.get(b)
            for f in found if isinstance(found, list) else []:
                if isinstance(f, dict) and isinstance(f.get("id"), str):
                    out.setdefault(f["id"], b)
    return out


def _earlier_problems(rep: dict, card: dict) -> list:
    """A delta's marks: each confirmed and question id on the card listed once as open, fixed or answered, and no
    new finding reusing an earlier id."""
    earlier = rep.get("earlier")
    if not isinstance(earlier, list):
        return ["earlier: a delta must list each earlier confirmed and question id as {id, state}"]
    old = card.get("findings") if isinstance(card.get("findings"), dict) else {}
    want = _ids(old, ("confirmed", "questions"))
    now = _ids(rep.get("findings"))
    out, marked = [], {}
    for i, e in enumerate(earlier):
        at = f"earlier[{i}]"
        if not (isinstance(e, dict) and set(e) == {"id", "state"} and isinstance(e["id"], str)):
            out.append(f"{at}: must be an object with exactly id and state")
            continue
        fid, state = e["id"], e["state"]
        if fid not in want:
            out.append(f"{at}: {fid} is not an earlier confirmed or question finding on the card")
            continue
        if fid in marked:
            out.append(f"{at}: {fid} is listed twice")
            continue
        marked[fid] = state
        bucket = want[fid]
        if state not in EARLIER_STATES:
            out.append(f"{at}.state: must be one of {'|'.join(EARLIER_STATES)}")
        elif state == "open":
            if now.get(fid) != bucket:
                out.append(f"{at}: {fid} is open but is not in findings.{bucket}"
                           + (f" (it is in {now[fid]})" if fid in now else ""))
        elif fid in now:
            out.append(f"{at}: {fid} is {state} but is still listed in findings.{now[fid]}")
        elif state == "fixed" and bucket != "confirmed":
            out.append(f"{at}: {fid} is a question; mark it answered, not fixed")
        elif state == "answered" and bucket != "questions":
            out.append(f"{at}: {fid} is a confirmed finding; mark it fixed, not answered")
    out += [f"earlier: {fid} ({b}) is left out" for fid, b in want.items() if fid not in marked]
    gone = _ids(old, ("fixed", "answered"))
    out += [f"findings.{now[fid]}: {fid} reuses an earlier id ({gone[fid]}); a new finding takes a new id"
            for fid in now if fid in gone]
    return out


def check_report(rep: dict, plan_: dict, card: dict, langs) -> list:
    """Problems with a report (secretary's `report validate`), against the plan and the card; [] when it is good."""
    out = []
    stage = plan_["stage"]
    out += [f"{k}: not a field of the report" for k in rep if k not in REPORT_KEYS]
    if rep.get("card") != plan_["card"]:
        out.append(f"card: must be {plan_['card']}")
    if rep.get("stage") != stage:
        out.append(f"stage: must be {stage}, the plan's")
    if rep.get("head_sha") != plan_["head_sha"]:
        out.append(f"head_sha: must be {plan_['head_sha']}, the plan's head")
    if not (isinstance(rep.get("reviewed_at"), str) and STAMP.match(rep["reviewed_at"])):
        out.append("reviewed_at: must be a UTC time in whole seconds, like 2026-11-04T09:30:00Z")
    v = rep.get("verdict")
    if v not in VERDICTS:
        out.append(f"verdict: must be one of {'|'.join(VERDICTS)}")
    for b in REPORT_TEXT:
        out += text_problems(rep[b], langs, b) if b in rep else [f"{b}: missing"]
    tldr = rep.get("tldr")
    if isinstance(tldr, dict):
        for lang in langs:
            t = tldr.get(lang)
            if not isinstance(t, str):
                continue
            if "\n" in t or "\r" in t or " " in t or " " in t:
                out.append(f"tldr.{lang}: must be one line")
            if len(t) > TLDR_MAX:
                out.append(f"tldr.{lang}: {len(t)} characters; at most {TLDR_MAX}")
        if "en" in langs and v in VERDICTS and isinstance(tldr.get("en"), str) \
                and not tldr["en"].startswith(VERDICT_WORD[v]):
            out.append(f"tldr.en: must start with the verdict, {VERDICT_WORD[v]!r}")
    f = rep.get("findings")
    out += check_findings(f, langs)
    if v in VERDICTS and shaped(f) and verdict(f) != v:
        out.append(f"verdict: must be {verdict(f)} (it follows the findings)")
    got = rep.get("angles")
    want = [(a.get("name"), a.get("origin")) for a in plan_["angles"] if isinstance(a, dict)]
    if not isinstance(got, list) or not all(isinstance(a, dict) and set(a) == {"name", "origin", "why"}
                                            for a in got):
        out.append("angles: must list {name, origin, why} for each angle that ran")
    else:
        if [(a["name"], a["origin"]) for a in got] != want:
            out.append("angles: must be the plan's angles, in order: "
                       + ", ".join(f"{n} ({o})" for n, o in want))
        for i, a in enumerate(got):
            out += text_problems(a["why"], langs, f"angles[{i}].why")
    c = rep.get("checks")
    if not isinstance(c, dict):
        out.append("checks: must be an object with ci and notes")
    else:
        if c.get("ci") not in CI_STATES:
            out.append(f"checks.ci: must be one of {'|'.join(CI_STATES)}")
        out += text_problems(c.get("notes"), langs, "checks.notes")
        out += [f"checks.{k}: not a field of checks" for k in c if k not in ("ci", "notes")]
    if "diagram" in rep:
        d = rep["diagram"]
        s = d.strip().lower() if isinstance(d, str) else ""
        if not (s.startswith("<svg") and s.endswith("</svg>") and s.count("<svg") == 1) or _SVG_UNSAFE.search(d):
            out.append("diagram: must be one inline <svg> element with no script, event attributes, javascript: "
                       "links or foreignObject")
    if stage == "delta":
        out += text_problems(rep["since_last"], langs, "since_last") if "since_last" in rep else \
            ["since_last: missing; a delta says what changed since the last review"]
        out += _earlier_problems(rep, card)
    else:
        out += [f"{k}: only in a delta review" for k in DELTA_ONLY if k in rep]
    return out


def check(data, card_id: str, cfg=None) -> dict:
    """`ap2 review check`: {ok, errors} for `<data>/sessions/<card>/prreview.json`. It only reads, so a missing data
    folder is refused, not created."""
    data = Path(data)
    if not data.is_dir():
        raise ReviewError(f"no data folder at {data}")
    cfg = cfg if cfg is not None else config.load(data)
    card = Store(data, cfg=cfg).get(card_id)
    pr_of(card)
    plan_ = read_plan(data, card_id)
    rep, errors = read_report(data, card_id)
    if rep is not None:
        errors = check_report(rep, plan_, card, cards.languages(cfg))
    return {"ok": not errors, "errors": errors}


# --- plain words -------------------------------------------------------------------------------------------------

PHRASES = {
    "en": {"what": "{author} asked you to review PR #{id}: {title}", "someone": "Someone",
           "where_none": "Not reviewed yet.",
           "do_none": "Nothing yet; open it again once its review has run.",
           "do_many": "Read the {n} findings, then approve or ask for changes on GitHub yourself.",
           "do_one": "Read the 1 finding, then approve or ask for changes on GitHub yourself.",
           "do_zero": "Read the review, then approve or ask for changes on GitHub yourself."},
    "zh": {"what": "{author} 请你审查 PR #{id}：{title}", "someone": "有人",
           "where_none": "还没有审查。",
           "do_none": "现在不用做什么；审查跑完后再打开它。",
           "do_many": "看一下这 {n} 条发现，然后自己在 GitHub 上批准或要求修改。",
           "do_one": "看一下这 1 条发现，然后自己在 GitHub 上批准或要求修改。",
           "do_zero": "看一下审查结果，然后自己在 GitHub 上批准或要求修改。"},
}
TITLE_MAX = 120


def phrases(lang: str) -> Optional[dict]:
    return PHRASES.get(lang) or PHRASES.get(lang.split("-")[0])


def _first_line(text, limit: int = TITLE_MAX) -> str:
    text = text if isinstance(text, str) else ""
    line = next((ln for ln in text.splitlines() if ln.strip()), text)
    line = re.sub(r"\s+", " ", line).strip()
    return line if len(line) <= limit else line[:limit - 1].rstrip() + "…"


def plain_for(record, card, langs) -> dict:
    """The plain words of a PR review card in each language (design 6.2 point 4). What: who asked for the review of
    which PR. Where: "Not reviewed yet.", or the last review's TL;DR. Do: wait, or read the open findings and review
    on GitHub yourself. `card` is the card as it is (or {} for a new one): its `verdict` and `findings` blocks."""
    record = record if isinstance(record, dict) else {}
    card = card if isinstance(card, dict) else {}
    block = card.get("verdict") if isinstance(card.get("verdict"), dict) else None
    tldr = block.get("tldr") if block and isinstance(block.get("tldr"), dict) else None
    found = card.get("findings") if isinstance(card.get("findings"), dict) else {}
    n = sum(len(found.get(b) or []) for b in ("confirmed", "questions") if isinstance(found.get(b), list))
    number = record.get("id")
    if not (isinstance(number, (str, int)) and str(number).strip()):
        m = KEY.match(card.get("key") or "") if isinstance(card.get("key"), str) else None
        number = m.group(2) if m else "?"
    author = record.get("author") if isinstance(record.get("author"), str) and record["author"].strip() else None
    out = {line: {} for line in cards.PLAIN_LINES}
    for lang in langs:
        p = phrases(lang)
        if p is None:
            raise ReviewError(f"no plain words for PR review cards in {lang} yet (built in: {', '.join(PHRASES)})")
        title = (_first_line(record.get("text")) or _first_line(cards.title_for(card, lang, langs[0]))
                 or (card.get("key") if isinstance(card.get("key"), str) else ""))
        out["what"][lang] = p["what"].format(author=author or p["someone"], id=number, title=title)
        said = tldr.get(lang) if tldr else None
        if tldr and not (isinstance(said, str) and said.strip()):
            said = next((t for t in tldr.values() if isinstance(t, str) and t.strip()), None)
        out["where"][lang] = said if tldr and said else p["where_none"]
        if not (tldr and said):
            out["do"][lang] = p["do_none"]
        else:
            out["do"][lang] = p["do_zero"] if n == 0 else p["do_one"] if n == 1 else p["do_many"].format(n=n)
    return out


# --- apply and sync ----------------------------------------------------------------------------------------------

def apply(data, card_id: str, by: str = "ap2 review", cfg=None) -> dict:
    """Put a checked report on the card with one store update (a restore point is saved); return the card."""
    data = Path(data)
    cfg = cfg if cfg is not None else config.load(data)
    langs = cards.languages(cfg)
    store = Store(data, cfg=cfg)
    card = store.get(card_id)
    pr = pr_of(card)
    plan_ = read_plan(data, card_id)
    rep, errors = read_report(data, card_id)
    if rep is not None:
        errors = check_report(rep, plan_, card, langs)
    if errors:
        shown = "; ".join(errors[:5]) + (f" (+{len(errors) - 5} more)" if len(errors) > 5 else "")
        raise ReviewError(f"card {card_id}: the report does not pass ap2 review check: {shown}")
    old_block = card.get("verdict") if isinstance(card.get("verdict"), dict) else {}
    if old_block.get("head_sha") == rep["head_sha"] and old_block.get("reviewed_at") == rep["reviewed_at"]:
        raise ReviewError(f"card {card_id}: this report ({rep['head_sha'][:7]}, {rep['reviewed_at']}) is already "
                          f"on the card")
    head, stage, v = rep["head_sha"], rep["stage"], rep["verdict"]
    old = card.get("findings") if isinstance(card.get("findings"), dict) else {}
    findings = {b: rep["findings"][b] for b in BUCKETS}
    fixed, answered, n_fixed, n_open = [], [], 0, 0
    if stage == "delta":
        def stored(b):  # a bucket the card holds, or [] when it is not a list
            return old.get(b) if isinstance(old.get(b), list) else []

        fixed = [f for f in stored("fixed") if isinstance(f, dict)]
        answered = [f for f in stored("answered") if isinstance(f, dict)]
        by_id = {f["id"]: f for b in ("confirmed", "questions") for f in stored(b)
                 if isinstance(f, dict) and isinstance(f.get("id"), str)}
        for e in rep["earlier"]:
            if e["state"] == "fixed":
                fixed.append({**by_id[e["id"]], "fixed_in": head})
                n_fixed += 1
            elif e["state"] == "answered":
                answered.append({**by_id[e["id"]], "fixed_in": head})
            else:
                n_open += 1
    findings.update(fixed=fixed, answered=answered)
    block = {k: rep[k] for k in ("verdict", "tldr", "background", "problem", "solution", "since_last", "diagram")
             if k in rep}
    block.update(ci=rep["checks"]["ci"], notes=rep["checks"]["notes"], angles=rep["angles"], stage=stage,
                 head_sha=head, reviewed_at=rep["reviewed_at"])
    reviews = [r for r in card.get("reviews") or [] if isinstance(r, dict)] if isinstance(card.get("reviews"), list) \
        else []
    reviews.append({"stage": stage, "head_sha": head, "verdict": v, "at": cards.stamp(store.clock())})
    changes = {"verdict": block, "findings": findings, "draft": {"text": rep["reply_draft"], "sent": False},
               "pr": {**pr, "head_sha": head, "reviewed_sha": head}, "reviews": reviews}
    record = (card.get("source") or {}).get("record") if isinstance(card.get("source"), dict) else None
    changes["plain"] = plain_for(record, {**card, **changes}, langs)
    did = f"reviewed {head[:7]}: {v}" if stage == "review" else \
        f"delta review {head[:7]}: {v}, {n_fixed} fixed, {n_open} open"
    new = store.update_checked(card_id, changes, by=by, check=lambda now: now == card, did=did)
    if new is None:
        raise ReviewError(f"card {card_id} changed while the report was being applied; run it again")
    return new


SYNC_DONE = {"merged": "the PR was merged", "closed": "the PR was closed",
             "reviewed": "you reviewed it on GitHub", "request_removed": "the review request was removed"}


def sync(data, card_id: str, by: str = "ap2 review", cfg=None, run=None) -> dict:
    """Where the PR stands on GitHub (design 6.2 points 5-6): merged, closed, reviewed (the user has a review),
    request_removed (neither the user nor a team is asked any more), not_reviewed, new_commits (a delta is due) or
    current. The first four move the card to done; every state records the head. Only the card changes."""
    data = Path(data)
    cfg = cfg if cfg is not None else config.load(data)
    store = Store(data, cfg=cfg)
    card = store.get(card_id)
    pr = pr_of(card)
    reviewed = _reviewed_sha(card)
    view = _json(gh_read(["pr", "view", str(pr["number"]), "--repo", pr["repo"], "--json", SYNC_FIELDS], run),
                 "pr view")
    me = gh_read(["api", "user", "--jq", ".login"], run).strip()
    if not re.match(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}\Z", me):
        raise ReviewError(f"api user: not a GitHub login: {me[:80]!r}")
    if not isinstance(view, dict):
        raise ReviewError("pr view: unexpected reply")
    head = _sha(view.get("headRefOid"), "pr view headRefOid")
    requests = [r for r in view.get("reviewRequests") or [] if isinstance(r, dict)]
    reviews = [r for r in view.get("latestReviews") or [] if isinstance(r, dict)]
    asked = any(r.get("login") == me for r in requests)
    team = any(r.get("__typename") == "Team" or ("slug" in r and "login" not in r) for r in requests)
    mine = any(isinstance(r.get("author"), dict) and r["author"].get("login") == me for r in reviews)
    state = str(view.get("state") or "").upper()
    if state == "MERGED":
        out = "merged"
    elif state == "CLOSED":
        out = "closed"
    elif mine:
        out = "reviewed"
    elif not asked and not team:
        out = "request_removed"
    elif reviewed is None:
        out = "not_reviewed"
    elif head != reviewed:
        out = "new_commits"
    else:
        out = "current"
    result = {"card": card_id, "state": out, "head_sha": head, "reviewed_sha": reviewed, "lane": card.get("lane"),
              "changed": False}
    if card.get("lane") == "done":
        return result
    old_pr = card.get("pr") if isinstance(card.get("pr"), dict) else {}
    changes = {"pr": {**pr, **old_pr, "head_sha": head}}
    did = f"sync: {out}"
    if out in SYNC_DONE:
        changes["lane"] = "done"
        did = SYNC_DONE[out]
    new = store.update_checked(card_id, changes, by=by, check=lambda now: now == card, did=did)
    if new is None:
        raise ReviewError(f"card {card_id} changed while it was being synced; run it again")
    result.update(lane=new.get("lane"), changed=new.get("updated_at") != card.get("updated_at"))
    return result
