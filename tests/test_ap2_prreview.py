"""PR review cards (design 6.2): the pr-review role, the plan, the verdict, the report check, apply, sync, and
that nothing is ever posted. Every gh reply is recorded (tests/fixtures/ap2/prreview/); nothing reads the network,
the real home or ~/.claude."""
from __future__ import annotations

import copy
import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT
from tests.test_ap2_cli import temp_inside_the_accounts_claude
from assistant import angles, cli, prreview, roles, templates
from assistant.config import Config
from assistant.engine import Engine
from assistant.store import Store, write_json

AP2 = ROOT / "bin" / "ap2"
FAKE_GH = ROOT / "tests" / "bin" / "fake-gh-replay"
FIX = ROOT / "tests" / "fixtures" / "ap2" / "prreview"
SCAN = ROOT / "tests" / "fixtures" / "ap2" / "scan"
PROCESS = ROOT / "commands" / "pr-review.md"

A = "a1b2c3d4" * 5
B = "b2c3d4e5" * 5
BASE = "c3d4e5f6" * 5
MB = "d4e5f6a7" * 5
REPO = "example/widgets"
KEY = "example/widgets#42"
LANGS = ("en", "zh")
DIAGRAM = '<svg viewBox="0 0 10 10"><rect width="10" height="10"/></svg>'
TITLE = "Retry failed card payments once, 2 seconds later"
# The gh calls as recorded, copied by hand (not read from prreview), so a change to what prreview asks GitHub
# no longer matches the recorded replies and fails a test instead of being served the old answer.
GH_PR_FILES = """query($owner:String!, $name:String!, $n:Int!, $after:String){ repository(owner:$owner, name:$name){
 pullRequest(number:$n){ headRefOid baseRefOid additions deletions
  files(first:100, after:$after){ pageInfo{ hasNextPage endCursor } nodes{ path } } } } }"""
GH_SINCE_JQ = ("{status: .status, merges: [.commits[] | select(.parents | length > 1) | .sha], "
               "more: (if (.total_commits | type) == \"number\" then .total_commits > (.commits | length) "
               "else null end)}")
GH_SYNC_FIELDS = "state,headRefOid,reviewRequests,latestReviews,author"


def fixture(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def compare(a, b):
    return f"repos/{REPO}/compare/{a}...{b}"


def graphql_argv(after=None, owner="example", name="widgets", n=42):
    argv = ["api", "graphql", "-f", "query=" + GH_PR_FILES, "-f", f"owner={owner}", "-f", f"name={name}",
            "-F", f"n={n}"]
    return argv + (["-f", f"after={after}"] if after else [])


def table(pages, head=A, since=None, reviewed=A, view=None):
    """The recorded replies of one GitHub state: the file pages, the merge base, and a delta's compare."""
    out, after = [], None
    for page in pages:
        out.append({"argv": graphql_argv(after), "stdout": page})
        after = page["data"]["repository"]["pullRequest"]["files"]["pageInfo"]["endCursor"]
    cmp = fixture("compare.json")
    out.append({"argv": ["api", compare(BASE, head), "--jq", ".merge_base_commit.sha"], "stdout": cmp["merge_base"]})
    if since:
        reply = cmp[since]
        entry = {"argv": ["api", compare(reviewed, head), "--jq", GH_SINCE_JQ]}
        entry.update(reply if isinstance(reply, dict) else {"stdout": reply})
        out.append(entry)
    if view:
        views = fixture("pr-view.json")
        out.append({"argv": ["pr", "view", "42", "--repo", REPO, "--json", GH_SYNC_FIELDS],
                    "stdout": view if isinstance(view, dict) else views[view]})
        out.append({"argv": ["api", "user", "--jq", ".login"], "stdout": views["user"]})
    return out


def with_head(pages, head):
    pages = copy.deepcopy(pages)
    for p in pages:
        p["data"]["repository"]["pullRequest"]["headRefOid"] = head
    return pages


class Replay:
    """An injected gh runner: answers from a table like fake-gh-replay, and logs every argv."""

    def __init__(self, entries):
        self.entries, self.log = entries, []

    def __call__(self, argv):
        assert argv[0] == "gh", argv
        self.log.append(argv[1:])
        for e in self.entries:
            if e["argv"] == argv[1:]:
                out = e.get("stdout", "")
                return subprocess.CompletedProcess(argv, e.get("exit", 0),
                                                   out if isinstance(out, str) else json.dumps(out),
                                                   e.get("stderr", ""))
        return subprocess.CompletedProcess(argv, 1, "", f"no recorded reply for {argv[1:]}\n")


# Whether and how deep json.loads raises RecursionError depends on the Python version and platform (3.14 stops only
# when the C stack runs out), so no real depth under the size limit fails everywhere. The tests fake it instead, for
# this one text only: `json` is the shared module, so every other read still parses for real.
DEEP_TEXT = '[[["nested too deeply"]]]'
TOO_DEEP = RecursionError("maximum recursion depth exceeded while decoding a JSON array from a unicode string")


def too_deep_to_read(module: str):
    """Make `json.loads`, as `assistant.<module>` calls it, raise the RecursionError a too-deep text raises when it
    is given DEEP_TEXT (as str or bytes)."""
    real = json.loads

    def loads(s, *a, **kw):
        text = bytes(s).decode("utf-8", "replace") if isinstance(s, (bytes, bytearray)) else s
        if text == DEEP_TEXT:
            raise TOO_DEEP
        return real(s, *a, **kw)

    return mock.patch(f"assistant.{module}.json.loads", side_effect=loads)


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-prreview-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "data"
        self.data.mkdir()
        (self.data / "config.yaml").write_text("secondary_language: zh\n")
        self.cfg = Config(secondary_language="zh")

    def store(self):
        return Store(self.data, cfg=self.cfg)

    def read_request(self, reply=None, source="github-review-requests"):
        raw = self.tmp / "gh-search-prs.json"
        shutil.copy(reply or SCAN / "gh-search-prs.json", raw)
        result = Engine(self.data, cfg=self.cfg).read_saved(source, raw)
        self.assertEqual(result["dropped"], [])
        return result

    def card(self, source="github-review-requests"):
        self.read_request(source=source)
        return next(c for c in self.store().list() if c.get("key") == KEY)

    def plan(self, card_id, entries, stage="review", checkout=None):
        run = Replay(entries)
        return prreview.plan(self.data, card_id, stage, checkout, cfg=self.cfg, run=run), run

    def put_report(self, card_id, rep):
        write_json(prreview.run_dir(self.data, card_id) / prreview.REPORT_FILE, rep)

    def report(self, name, card_id):
        rep = fixture(name)
        rep["card"] = card_id
        return rep

    def reviewed_card(self, source="github-review-requests"):
        """A card with the recorded full review on it (head A, correctness-1 open)."""
        cid = self.card(source)["id"]
        self.plan(cid, table(fixture("graphql-head-a.json")))
        self.put_report(cid, self.report("report-review.json", cid))
        prreview.apply(self.data, cid, cfg=self.cfg)
        return cid


# --- criterion 1: the role and its template ----------------------------------------------------------------------

class RoleTest(Case):
    def test_the_role_has_its_six_tools_and_the_read_only_floor(self):
        role = roles.ROLES["pr-review"]
        self.assertEqual(sorted(role.tools), ["Agent", "Bash", "Glob", "Grep", "Read", "Write"])
        self.assertEqual(role.floor, "read-only")
        self.assertIn("commands/pr-review.md", role.from_v1)
        self.assertFalse(set(role.tools) & set(roles.READ_ONLY_DENIED))

    def test_the_builtin_template_runs_only_pr_review(self):
        t = templates.load_file(templates.BUILTIN_DIR / "pr-review.yaml", origin="built-in")
        self.assertEqual([r["role"] for r in t.data["roles"]], ["pr-review"])
        self.assertIn("commands/pr-review.md", t.data["roles"][0]["brief"])
        self.assertIn("Post nothing", t.data["roles"][0]["brief"])
        self.assertEqual(t.data["view"], ["source_block", "verdict", "findings", "draft", "activity"])

    def test_template_show_lists_the_role_with_its_tools(self):
        if temp_inside_the_accounts_claude():
            self.skipTest("the temp folder is inside the account's ~/.claude, so ap2 refuses every temp data folder")
        env = {k: v for k, v in os.environ.items() if k != "AP2_DATA"}
        env.update(HOME=str(self.tmp), AP2_DATA=str(self.data), PYTHON_COLORS="0", PYTHONIOENCODING="utf-8")
        r = subprocess.run([sys.executable, str(AP2), "template", "show", "pr-review"], capture_output=True,
                           text=True, env=env, encoding="utf-8", stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        shown = json.loads(r.stdout)["roles"]
        self.assertEqual([(x["role"], sorted(x["tools"]), x["floor"]) for x in shown],
                         [("pr-review", ["Agent", "Bash", "Glob", "Grep", "Read", "Write"], "read-only")])

    def test_the_process_file(self):
        text = PROCESS.read_text(encoding="utf-8")
        front = re.match(r"---\n(.*?)\n---\n", text, re.S).group(1)
        meta = dict(line.split(":", 1) for line in front.splitlines())
        self.assertEqual(meta["disable-model-invocation"].strip(), "true")
        self.assertEqual(meta["argument-hint"].strip(), '"<card id> <review|delta>"')
        self.assertTrue(meta["description"].strip())
        self.assertNotIn("`ap ", text)  # tests/test_docs.py would read that as a v1 command

        commands = re.findall(r"<package>/bin/ap2 ([^`]*)`", text)
        subs = {c.split()[1] for c in commands if c.startswith("review ")}
        self.assertEqual(subs, {"plan", "verdict", "check"})
        fill = {"<card id>": "c-20261111-093000-abcdef", "<stage>": "delta", "<folder>": "/tmp/x"}
        for c in commands:
            with self.subTest(c):
                c = c.split("<<")[0]
                for k, v in fill.items():
                    c = c.replace(k, v)
                cli._parser().parse_args(shlex.split(c))  # an argparse error exits, failing the test

        rules = re.search(r"^## Rules\n(.*?)^## ", text, re.S | re.M).group(1)
        outside = text.replace(rules, "")
        for banned in ("gh pr review", "gh pr comment", "gh pr merge", "gh issue comment", "-X", "--method",
                       "--input", "mutation"):
            with self.subTest(banned=banned):
                self.assertIn(banned, rules)
                self.assertNotIn(banned, outside)

    def test_the_process_file_names_every_whole_pr_reason(self):
        # whole_pr_reason gives three reasons; the role's own file must name all three, with the compare limit
        # read from the constant so changing it without the doc fails here.
        text = " ".join(PROCESS.read_text(encoding="utf-8").split())
        sentence = re.search(r"`whole_pr` is set when (.*?);", text)
        self.assertIsNotNone(sentence, "no `whole_pr` is set when ... sentence in step 2")
        sentence = sentence.group(1)
        for reason in ("new commits include a merge commit", f"({prreview.COMPARE_LISTS})", "rewritten"):
            with self.subTest(reason=reason):
                self.assertIn(reason, sentence)
        # Step 3 points back at the list rather than naming fewer reasons.
        step3 = re.search(r"When `whole_pr` is set, `compare` is the whole PR \((.*?)\)", text)
        self.assertIsNotNone(step3, "no `whole_pr` parenthesis in step 3")
        self.assertEqual(step3.group(1), "for one of the reasons in step 2")


# --- criterion 2: a review request becomes a card in plain words -------------------------------------------------

class PlainWordsTest(Case):
    def test_a_review_request_becomes_a_card_in_plain_words(self):
        card = self.card()
        self.assertEqual(card["kind"], "pr-review")
        self.assertEqual(card["plain"], {
            "what": {"en": f"octo-dev asked you to review PR #42: {TITLE}",
                     "zh": f"octo-dev 请你审查 PR #42：{TITLE}"},
            "where": {"en": "Not reviewed yet.", "zh": "还没有审查。"},
            "do": {"en": "Nothing yet; open it again once its review has run.",
                   "zh": "现在不用做什么；审查跑完后再打开它。"}})

    def test_a_reread_keeps_the_tldr(self):
        cid = self.reviewed_card()
        tldr = fixture("report-review.json")["tldr"]
        card = self.store().get(cid)
        self.assertEqual(card["plain"]["where"], tldr)

        again = self.read_request()
        item = next(i for i in again["items"] if i["key"] == KEY)
        self.assertEqual((item["action"], item["card"]), ("unchanged", cid))
        self.assertEqual(self.store().get(cid)["plain"]["where"], tldr)

        reply = json.loads((SCAN / "gh-search-prs.json").read_text())
        reply[0]["title"] = "Retry failed card payments once, 5 seconds later"
        changed = self.tmp / "changed.json"
        changed.write_text(json.dumps(reply))
        again = self.read_request(changed)
        item = next(i for i in again["items"] if i["key"] == KEY)
        self.assertEqual(item["action"], "update")
        card = self.store().get(cid)
        self.assertEqual(card["plain"]["what"]["en"],
                         "octo-dev asked you to review PR #42: Retry failed card payments once, 5 seconds later")
        self.assertEqual(card["plain"]["where"], tldr)
        self.assertEqual(card["plain"]["do"]["en"],
                         "Read the 1 finding, then approve or ask for changes on GitHub yourself.")

    def test_the_do_line_counts_the_open_findings(self):
        f = fixture("report-review.json")["findings"]["confirmed"][0]
        base = {"verdict": {"tldr": {"en": "Approve: fine.", "zh": "Approve：没问题。"}}}
        for n, want in ((0, "Read the review, then"), (1, "Read the 1 finding, then"), (3, "Read the 3 findings, then")):
            with self.subTest(n=n):
                card = dict(base, findings={"confirmed": [f] * min(n, 2), "questions": [f] * max(n - 2, 0)})
                words = prreview.plain_for({"id": "42", "text": TITLE}, card, LANGS)
                self.assertTrue(words["do"]["en"].startswith(want), words["do"]["en"])
                self.assertEqual(words["what"]["en"], f"Someone asked you to review PR #42: {TITLE}")
        with self.subTest("rejected, out of scope and fixed are not open"):
            card = dict(base, findings={"confirmed": [f], "rejected": [f], "out_of_scope": [f], "fixed": [f]})
            words = prreview.plain_for({"id": "42", "text": TITLE}, card, LANGS)
            self.assertTrue(words["do"]["en"].startswith("Read the 1 finding, then"), words["do"]["en"])
        with self.subTest("a confirmed nit is open"):
            card = dict(base, findings={"confirmed": [dict(f, severity="nit")]})
            words = prreview.plain_for({"id": "42", "text": TITLE}, card, LANGS)
            self.assertTrue(words["do"]["en"].startswith("Read the 1 finding, then"), words["do"]["en"])

    def test_a_regional_language_uses_its_base_language_words(self):
        words = prreview.plain_for({"id": "42", "text": TITLE}, {}, ("en", "zh-TW"))
        self.assertEqual(words["what"]["zh-TW"], f"有人 请你审查 PR #42：{TITLE}")
        self.assertEqual(words["where"]["zh-TW"], "还没有审查。")

    def test_a_tldr_missing_a_language_falls_back_to_another(self):
        card = {"verdict": {"tldr": {"en": "Changes: x."}}}
        words = prreview.plain_for({"id": "42", "text": TITLE}, card, LANGS)
        self.assertEqual(words["where"]["zh"], "Changes: x.")
        self.assertNotEqual(words["do"]["zh"], prreview.PHRASES["zh"]["do_none"])
        self.assertEqual(words["do"]["zh"], prreview.PHRASES["zh"]["do_zero"])

    def test_with_no_source_record_the_title_is_the_cards(self):
        for title, zh in (("Fix it", "Fix it"), ({"en": "Fix it", "zh": "修好它"}, "修好它")):
            with self.subTest(title=title):
                card = {"kind": "pr-review", "key": "o/r#5", "title": title}
                words = prreview.plain_for(None, card, LANGS)
                self.assertEqual(words["what"], {"en": "Someone asked you to review PR #5: Fix it",
                                                 "zh": f"有人 请你审查 PR #5：{zh}"})

    def test_with_no_title_at_all_the_what_line_names_the_card_key(self):
        for title in (None, "", "  "):
            with self.subTest(title=title):
                what = prreview.plain_for({}, {"key": "example/widgets#42", "title": title}, ("en",))["what"]["en"]
                self.assertFalse(what.rstrip().endswith(":"), what)
                self.assertIn("example/widgets#42", what)

    def test_an_own_pr_has_its_own_words(self):
        """A card from github-my-prs is the user's own PR: it reads as "Your PR", and its do line says to fix
        findings on the branch, not to approve or ask for changes."""
        f = fixture("report-review.json")["findings"]["confirmed"][0]
        tldr = {"en": "Changes: one retry is missing.", "zh": "Changes：少了一次重试。"}
        own = {"feed": prreview.OWN_FEED}
        self.assertEqual(prreview.OWN_FEED, "github-my-prs")
        with self.subTest("before a review"):
            words = prreview.plain_for({"id": "42", "author": "octo-me", "text": TITLE}, own, LANGS)
            self.assertEqual(words, {
                "what": {"en": f"Your PR #42: {TITLE}", "zh": f"你的 PR #42：{TITLE}"},
                "where": {"en": "Not reviewed yet.", "zh": "还没有审查。"},
                "do": {"en": "Nothing yet; open it again once its review has run.",
                       "zh": "现在不用做什么；审查跑完后再打开它。"}})
        for n, en, zh in (
                (0, "Read the review; nothing needs fixing.", "看一下审查结果；没有要修的。"),
                (1, "Read the 1 finding, then fix it on your branch if you agree.",
                 "看一下这 1 条发现，你认同的话就在你的分支上修好它。"),
                (3, "Read the 3 findings, then fix the ones you agree with on your branch.",
                 "看一下这 3 条发现，然后在你的分支上修好你认同的那些。")):
            with self.subTest("after a review", n=n):
                card = dict(own, verdict={"tldr": tldr},
                            findings={"confirmed": [f] * min(n, 2), "questions": [f] * max(n - 2, 0)})
                words = prreview.plain_for({"id": "42", "author": "octo-me", "text": TITLE}, card, LANGS)
                self.assertEqual(words, {"what": {"en": f"Your PR #42: {TITLE}", "zh": f"你的 PR #42：{TITLE}"},
                                         "where": tldr, "do": {"en": en, "zh": zh}})
        with self.subTest("with no source record the title is the card's"):
            card = {"feed": prreview.OWN_FEED, "key": "o/r#5", "title": {"en": "Fix it", "zh": "修好它"}}
            self.assertEqual(prreview.plain_for(None, card, LANGS)["what"],
                             {"en": "Your PR #5: Fix it", "zh": "你的 PR #5：修好它"})
        with self.subTest("a regional language uses its base language's own words"):
            record = {"id": "42", "text": TITLE}
            self.assertEqual(prreview.plain_for(record, own, ("en", "zh-TW"))["what"]["zh-TW"],
                             f"你的 PR #42：{TITLE}")
            card = dict(own, verdict={"tldr": tldr}, findings={"confirmed": [f]})
            self.assertEqual(prreview.plain_for(record, card, ("en", "zh-TW"))["do"]["zh-TW"],
                             "看一下这 1 条发现，你认同的话就在你的分支上修好它。")

    def test_any_other_feed_keeps_the_review_request_words(self):
        f = fixture("report-review.json")["findings"]["confirmed"][0]
        tldr = {"en": "Changes: one retry is missing.", "zh": "Changes：少了一次重试。"}
        record = {"id": "42", "author": "octo-dev", "text": TITLE}
        for feed in ("github-review-requests", "slack-mentions", None, 7):
            with self.subTest(feed=feed):
                before = prreview.plain_for(record, {"feed": feed}, LANGS)
                self.assertEqual(before, {
                    "what": {"en": f"octo-dev asked you to review PR #42: {TITLE}",
                             "zh": f"octo-dev 请你审查 PR #42：{TITLE}"},
                    "where": {"en": "Not reviewed yet.", "zh": "还没有审查。"},
                    "do": {"en": "Nothing yet; open it again once its review has run.",
                           "zh": "现在不用做什么；审查跑完后再打开它。"}})
                after = prreview.plain_for(record, {"feed": feed, "verdict": {"tldr": tldr},
                                                    "findings": {"confirmed": [f]}}, LANGS)
                self.assertEqual(after["do"], {
                    "en": "Read the 1 finding, then approve or ask for changes on GitHub yourself.",
                    "zh": "看一下这 1 条发现，然后自己在 GitHub 上批准或要求修改。"})

    def test_the_engine_gives_an_own_pr_card_its_words(self):
        card = self.card("github-my-prs")
        self.assertEqual((card["kind"], card["feed"]), ("pr-review", "github-my-prs"))
        self.assertEqual(card["plain"]["what"], {"en": f"Your PR #42: {TITLE}", "zh": f"你的 PR #42：{TITLE}"})
        gadgets = next(c for c in self.store().list() if c.get("key") == "example/gadgets#7")
        self.assertEqual(gadgets["plain"]["what"]["en"], "Your PR #7: Show the delivery date on the order page")

    def test_a_reread_of_an_own_pr_keeps_its_words(self):
        cid = self.reviewed_card("github-my-prs")
        card = self.store().get(cid)
        self.assertEqual(card["plain"]["do"], {"en": "Read the 1 finding, then fix it on your branch if you agree.",
                                               "zh": "看一下这 1 条发现，你认同的话就在你的分支上修好它。"})
        reply = json.loads((SCAN / "gh-search-prs.json").read_text())
        reply[0]["title"] = "Retry failed card payments once, 5 seconds later"
        changed = self.tmp / "changed.json"
        changed.write_text(json.dumps(reply))
        item = next(i for i in self.read_request(changed, "github-my-prs")["items"] if i["key"] == KEY)
        self.assertEqual((item["action"], item["card"]), ("update", cid))
        card = self.store().get(cid)
        self.assertEqual(card["plain"]["what"]["en"], "Your PR #42: Retry failed card payments once, 5 seconds later")
        self.assertEqual(card["plain"]["where"], fixture("report-review.json")["tldr"])
        self.assertEqual(card["plain"]["do"]["en"], "Read the 1 finding, then fix it on your branch if you agree.")

    def assert_reread_keeps_words(self, first, second, en):
        """Both GitHub sources key a PR as place#id, so one PR read by both shares one card. Its words follow the
        card's own feed (the source that made it), so they do not flip from read to read."""
        card = self.card(first)
        self.assertEqual((card["feed"], card["plain"]["what"]["en"]), (first, en))
        self.read_request(source=second)
        again = self.store().get(card["id"])
        self.assertEqual(again["feed"], first)
        self.assertEqual(again["plain"]["what"], card["plain"]["what"])

    def test_a_my_prs_reread_of_a_review_request_keeps_its_words(self):
        self.assert_reread_keeps_words("github-review-requests", "github-my-prs",
                                       f"octo-dev asked you to review PR #42: {TITLE}")

    def test_a_review_requests_reread_of_an_own_pr_keeps_its_words(self):
        self.assert_reread_keeps_words("github-my-prs", "github-review-requests", f"Your PR #42: {TITLE}")

    def test_a_long_title_is_cut_at_120_characters(self):
        words = prreview.plain_for({"id": "7", "author": "octo-dev", "text": "x" * 300 + "\nsecond line"}, {},
                                   ("en",))
        self.assertEqual(len(words["what"]["en"]) - len("octo-dev asked you to review PR #7: "), 120)


    def test_the_pr_number_comes_from_the_card_key_not_a_slack_message_id(self):
        # A Slack record's id is a message ts; the card's key says which PR it is.
        record = {"id": "1730000000.123456", "author": "U0EXAMPLE2", "text": "PTAL"}
        words = prreview.plain_for(record, {"kind": "pr-review", "key": KEY}, ("en",))
        self.assertEqual(words["what"]["en"], "U0EXAMPLE2 asked you to review PR #42: PTAL")
        with self.subTest("a key that is not owner/repo#number falls back to the record's id"):
            words = prreview.plain_for({"id": "7", "text": "x"}, {"key": "C0123:1.2"}, ("en",))
            self.assertEqual(words["what"]["en"], "Someone asked you to review PR #7: x")
        with self.subTest("no PR number in the key and no record id"):
            words = prreview.plain_for({"text": "x"}, {"key": "C0123:1.2"}, ("en",))
            self.assertEqual(words["what"]["en"], "Someone asked you to review PR #?: x")


class PrOfTest(unittest.TestCase):
    def pr(self, link):
        card = {"id": "c1", "kind": "pr-review", "key": KEY, "source": {"name": "s", "link": link}}
        return prreview.pr_of(card)

    def test_the_source_link_is_the_pr_url_only_when_it_is_that_pr(self):
        github = f"https://github.com/{REPO}/pull/42"
        cases = [
            (github, github),
            (github + "/", github + "/"),
            (github + "/files", github + "/files"),
            (github + "?w=1", github + "?w=1"),
            (github + "#discussion_r1", github + "#discussion_r1"),
            (f"https://git.example.com/{REPO}/pull/42", f"https://git.example.com/{REPO}/pull/42"),
            ("https://slack.com/archives/C0123ABCD/p1730000000123456?thread_ts=1730000000.123456",  # checkpkg: ignore
             github),
            (f"https://github.com/{REPO}/pull/420", github),
            (f"https://github.com/{REPO}/pull/4", github),
            (f"https://github.com/example/gadgets/pull/42", github),
            (f"https://github.com/{REPO}/issues/42", github),
            (f"http://github.com/{REPO}/pull/42", github),
            (f"https://github.com/{REPO}/pull/42 x", github),
            (f"https://evil.example/x/{REPO}/pull/42", github),  # checkpkg: ignore
            (f"https://a b/{REPO}/pull/42", github),  # checkpkg: ignore
            (github + "/ x", github),
            (None, github),
            (7, github),
        ]
        for link, url in cases:
            with self.subTest(link=link):
                self.assertEqual(self.pr(link), {"repo": REPO, "number": 42, "url": url})

    def test_a_source_that_is_not_a_map_gives_the_url_from_the_key(self):
        github = f"https://github.com/{REPO}/pull/42"
        missing = object()
        cases = [github, [github], 7, True, None, {}, missing]
        for source in cases:
            with self.subTest(source="no source key" if source is missing else source):
                card = {"id": "c1", "kind": "pr-review", "key": KEY}
                if source is not missing:
                    card["source"] = source
                self.assertEqual(prreview.pr_of(card), {"repo": REPO, "number": 42, "url": github})

    def test_a_dot_in_the_repo_name_is_a_dot_not_any_character(self):
        card = {"id": "c1", "kind": "pr-review", "key": "acme/my.repo#3",
                "source": {"name": "s", "link": "https://github.com/acme/myXrepo/pull/3"}}
        self.assertEqual(prreview.pr_of(card),
                         {"repo": "acme/my.repo", "number": 3, "url": "https://github.com/acme/my.repo/pull/3"})

# --- criterion 3: the plan ---------------------------------------------------------------------------------------

def git(repo, *args):
    return subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "-C", str(repo),
                           *args], check=True, capture_output=True, text=True).stdout.strip()


class PlanTest(Case):
    def set_reviewed(self, cid, sha=A, names=("Correctness", "Test value", "Flakiness & isolation")):
        block = {"angles": [{"name": n, "origin": "built-in", "why": {"en": "x", "zh": "x"}} for n in names]}
        self.store().update(cid, {"pr": {"repo": REPO, "number": 42, "reviewed_sha": sha}, "verdict": block},
                            by="test")

    def test_full_reviews(self):
        cid = self.card()["id"]
        with self.subTest("two pages, 40 lines, 3 angles"):
            out, run = self.plan(cid, table(fixture("graphql-head-a.json")))
            self.assertEqual((out["stage"], out["head_sha"], out["base_sha"], out["merge_base"]),
                             ("review", A, BASE, MB))
            self.assertEqual((out["file_count"], out["changed_lines"], out["docs_only"]), (2, 40, False))
            self.assertEqual(sum(1 for a in run.log if a[:2] == ["api", "graphql"]), 2)
            self.assertEqual([a["name"] for a in out["angles"]], ["Correctness", "Test value", "Flakiness & isolation"])
            self.assertEqual(out["pr"], "https://github.com/example/widgets/pull/42")
            self.assertEqual(out["report"], str(self.data / "sessions" / cid / "prreview.json"))
            self.assertEqual(out["languages"], ["en", "zh"])
            self.assertEqual(out["repo_layer"], "no checkout given")
            self.assertIsNone(out["reviewed_sha"])
            self.assertNotIn("compare", out)
            saved = json.loads((self.data / "sessions" / cid / "prreview-plan.json").read_text())
            self.assertEqual(saved, out)
        with self.subTest("docs only: 2 angles"):
            out, _ = self.plan(cid, table(fixture("graphql-docs.json")))
            self.assertTrue(out["docs_only"])
            self.assertEqual([a["name"] for a in out["angles"]], ["Correctness", "Accuracy"])

    def test_the_repo_layer(self):
        cid = self.card()["id"]
        repo = self.tmp / "checkout"
        repo.mkdir()
        git(repo, "init", "-q")
        (repo / ".autopilot").mkdir()
        (repo / ".autopilot" / "review.yaml").write_text(
            '- name: Payments safety\n  question: "Can this change charge a customer twice?"\n'
            '  when: { paths: ["billing/**"] }\n  brief: "Read every caller of the charge functions."\n')
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "init")
        head = git(repo, "rev-parse", "HEAD")
        # A later commit renames the angle, so the checkout's HEAD is not the PR head: the plan reads the PR head.
        (repo / ".autopilot" / "review.yaml").write_text(
            '- name: Billing safety\n  question: "Can this change charge a customer twice?"\n'
            '  when: { paths: ["billing/**"] }\n  brief: "Read every caller of the charge functions."\n')
        git(repo, "commit", "-q", "-am", "rename the angle")
        self.assertNotEqual(git(repo, "rev-parse", "HEAD"), head)
        real = subprocess.run
        calls, envs = [], []

        def spy(argv, *a, **kw):
            calls.append(list(argv))
            if "cat-file" in argv:
                envs.append(kw.get("env"))
            return real(argv, *a, **kw)

        with self.subTest("the checkout has the head"), mock.patch("subprocess.run", spy):
            out, _ = self.plan(cid, table(with_head(fixture("graphql-head-a.json"), head), head=head),
                               checkout=str(repo))
            self.assertEqual([(a["name"], a["origin"]) for a in out["angles"]][:2],
                             [("Correctness", "built-in"), ("Payments safety", "repo")])
            self.assertEqual(len(out["angles"]), 3)
            self.assertNotIn("Billing safety", [a["name"] for a in out["angles"]])
            self.assertEqual(out["repo_layer"], f"{repo} at {head[:7]}")
            self.assertTrue(envs)
            for env in envs:
                self.assertEqual((env["GIT_NO_LAZY_FETCH"], env["GIT_TERMINAL_PROMPT"]), ("1", "0"))
        with self.subTest("the checkout lacks the head"), mock.patch("subprocess.run", spy):
            out, _ = self.plan(cid, table(fixture("graphql-head-a.json")), checkout=str(repo))
            self.assertEqual(out["repo_layer"], f"{repo} does not have {A[:7]}")
            self.assertNotIn("repo", {a["origin"] for a in out["angles"]})
        with self.subTest("no checkout"):
            out, _ = self.plan(cid, table(fixture("graphql-head-a.json")))
            self.assertEqual(out["repo_layer"], "no checkout given")
            self.assertNotIn("repo", {a["origin"] for a in out["angles"]})
        self.assertTrue(calls)
        self.assertFalse([c for c in calls if "fetch" in c or "pull" in c], calls)

    def write_user_angles(self):
        """The user's layer (`<data>/review/angles.yaml`, design 6.1): one angle for the PR's billing/ file."""
        (self.data / "review").mkdir(exist_ok=True)
        (self.data / angles.USER_FILE).write_text(
            '- name: Payments safety\n  question: "Can this change charge a customer twice?"\n'
            '  when: { paths: ["billing/**"] }\n  brief: "Read every caller of the charge functions."\n')

    def checkout(self, review_yaml=None):
        """A git checkout with one commit (holding `.autopilot/review.yaml` when given); returns (path, head)."""
        repo = self.tmp / "checkout"
        repo.mkdir()
        git(repo, "init", "-q")
        if review_yaml is not None:
            (repo / ".autopilot").mkdir()
            (repo / ".autopilot" / "review.yaml").write_text(review_yaml)
        else:
            (repo / "README").write_text("x\n")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "init")
        return repo, git(repo, "rev-parse", "HEAD")

    def test_the_user_layer(self):
        cid = self.card()["id"]
        self.write_user_angles()
        with self.subTest("no checkout"):
            out, _ = self.plan(cid, table(fixture("graphql-head-a.json")))
            self.assertEqual(out["repo_layer"], "no checkout given")
            self.assertIn(("Payments safety", "user"), [(a["name"], a["origin"]) for a in out["angles"]])
        repo, _ = self.checkout()
        with self.subTest("a checkout that lacks the head"):
            out, _ = self.plan(cid, table(fixture("graphql-head-a.json")), checkout=str(repo))
            self.assertEqual(out["repo_layer"], f"{repo} does not have {A[:7]}")
            self.assertIn(("Payments safety", "user"), [(a["name"], a["origin"]) for a in out["angles"]])

    def test_a_delta_keeps_user_and_repo_angles(self):
        cid = self.card()["id"]
        self.write_user_angles()

        def set_reviewed(pairs):
            block = {"angles": [{"name": n, "origin": o, "why": {"en": "x", "zh": "x"}} for n, o in pairs]}
            self.store().update(cid, {"pr": {"repo": REPO, "number": 42, "reviewed_sha": A}, "verdict": block},
                                by="test")

        with self.subTest("a user angle, no checkout"):
            stored = [("Correctness", "built-in"), ("Accuracy", "built-in"), ("Payments safety", "user")]
            set_reviewed(stored)
            out, _ = self.plan(cid, table(fixture("graphql-head-b.json"), head=B, since="since_clean"),
                               stage="delta")
            self.assertEqual(out["stage"], "delta")
            self.assertEqual([(a["name"], a["origin"]) for a in out["angles"]], stored)
        repo, head = self.checkout(
            '- name: Billing safety\n  question: "Can a retry run after the order was refunded?"\n'
            '  when: { paths: ["billing/**"] }\n  brief: "Follow the refund path."\n')
        with self.subTest("a repo angle, the checkout has the head"):
            stored = [("Correctness", "built-in"), ("Accuracy", "built-in"), ("Billing safety", "repo"),
                      ("Payments safety", "user")]
            set_reviewed(stored)
            out, _ = self.plan(cid, table(with_head(fixture("graphql-head-b.json"), head), head=head,
                                          since="since_clean"), stage="delta", checkout=str(repo))
            self.assertEqual((out["stage"], out["repo_layer"]), ("delta", f"{repo} at {head[:7]}"))
            self.assertEqual([(a["name"], a["origin"]) for a in out["angles"]], stored)

    def test_deltas(self):
        cid = self.card()["id"]
        b_pages = fixture("graphql-head-b.json")
        with self.subTest("no earlier review: a full review"):
            out, _ = self.plan(cid, table(b_pages, head=B), stage="delta")
            self.assertEqual(out["stage"], "review")
            self.assertNotIn("compare", out)
        self.set_reviewed(cid)
        with self.subTest("the commits since the review"):
            out, _ = self.plan(cid, table(b_pages, head=B, since="since_clean"), stage="delta")
            self.assertEqual((out["stage"], out["reviewed_sha"], out["head_sha"]), ("delta", A, B))
            self.assertEqual(out["compare"], compare(A, B))
            self.assertIsNone(out["whole_pr"])
        for since, words in (("since_merge", "merge commit (e5f6a7b)"), ("since_diverged", "rewritten"),
                             ("since_behind", "rewritten since the last review (compare status behind)"),
                             ("since_missing", "no longer on GitHub"),
                             ("since_more", "more commits since the last review than GitHub's compare lists (250), "
                                            "so a merge commit among them cannot be ruled out"),
                             ("since_merge_more", "merge commit (e5f6a7b)")):
            with self.subTest(since):
                out, _ = self.plan(cid, table(b_pages, head=B, since=since), stage="delta")
                self.assertEqual(out["compare"], compare(BASE, B))
                self.assertIn(words, out["whole_pr"])
        for since in ("since_no_more", "since_more_text"):
            with self.subTest(f"{since}: a reply without a boolean more is an error, not a silent delta"):
                with self.assertRaisesRegex(prreview.ReviewError, "unexpected reply"):
                    self.plan(cid, table(b_pages, head=B, since=since), stage="delta")
        with self.subTest("a compare that fails for another reason is an error, not a whole-PR review"):
            with self.assertRaisesRegex(prreview.ReviewError, "HTTP 502") as cm:
                self.plan(cid, table(b_pages, head=B, since="since_error"), stage="delta")
            self.assertNotIsInstance(cm.exception, prreview.NotFound)
            self.assertNotIn("no longer on GitHub", str(cm.exception))
        with self.subTest("nothing new"):
            with self.assertRaisesRegex(prreview.ReviewError, f"nothing new since {A[:7]}"):
                self.plan(cid, table(fixture("graphql-head-a.json")), stage="delta")

    def test_a_delta_keeps_the_earlier_angles(self):
        cid = self.card()["id"]
        b_pages = fixture("graphql-head-b.json")
        with self.subTest("kept, in their order"):
            self.set_reviewed(cid, names=("Correctness", "Accuracy", "Gone angle"))
            out, _ = self.plan(cid, table(b_pages, head=B, since="since_clean"), stage="delta")
            self.assertEqual([a["name"] for a in out["angles"]], ["Correctness", "Accuracy"])
            self.assertEqual(out["angles"][1]["origin"], "built-in")
            # Each reviewer's prompt is built from the kept angle, so it carries the table's question, brief, file
            # and origin, not just its name.
            now = angles.load(self.data)
            for a in out["angles"]:
                want = {k: v for k, v in now.get(a["name"]).summary().items() if k != "when"}
                self.assertEqual({k: v for k, v in a.items() if k != "why"}, want)
        with self.subTest("fewer than 2 left: picked anew"):
            self.set_reviewed(cid, names=("Correctness", "Gone angle"))
            out, _ = self.plan(cid, table(b_pages, head=B, since="since_clean"), stage="delta")
            self.assertEqual([a["name"] for a in out["angles"]], ["Correctness", "Test value", "Flakiness & isolation"])

    def test_a_full_review_picks_its_angles_anew(self):
        cid = self.card()["id"]
        self.set_reviewed(cid, names=("Correctness", "Accuracy"))
        out, _ = self.plan(cid, table(fixture("graphql-head-a.json")), stage="review")
        self.assertEqual(out["stage"], "review")
        self.assertEqual([a["name"] for a in out["angles"]], ["Correctness", "Test value", "Flakiness & isolation"])

    def test_cards_that_are_not_a_pr_review_are_refused(self):
        base = {"lane": "needs", "feed": "test", "links": [], "pri": "P2", "origin": "source",
                "plain": {line: {"en": "x", "zh": "x"} for line in ("what", "where", "do")}}
        for name, card, words in (
                ("another kind", dict(base, kind="question", key=KEY), "not a pr-review card"),
                ("a bad key", dict(base, kind="pr-review", key="widgets#42"), "is not <owner>/<repo>#<number>"),
                ("no key", dict(base, kind="pr-review"), "is not <owner>/<repo>#<number>"),
                ("number 0", dict(base, kind="pr-review", key="example/widgets#0"), "is not <owner>/")):
            with self.subTest(name):
                cid = self.store().add(card, by="test")["id"]
                with self.assertRaisesRegex(prreview.ReviewError, re.escape(words)):
                    self.plan(cid, table(fixture("graphql-head-a.json")))

    def test_a_sha_that_is_not_40_hex_is_refused(self):
        cid = self.card()["id"]
        with self.subTest("a path for a head"):
            pages = with_head(fixture("graphql-head-a.json"), "../../etc")
            with self.assertRaisesRegex(prreview.ReviewError, "headRefOid: not a 40-character commit SHA"):
                self.plan(cid, table(pages))
        with self.subTest("40 hex and one more character"):
            pages = with_head(fixture("graphql-head-a.json"), A + "x")
            with self.assertRaisesRegex(prreview.ReviewError,
                                        re.escape(f"headRefOid: not a 40-character commit SHA: {A + 'x'!r}")):
                self.plan(cid, table(pages, head=A + "x"))
        with self.subTest("a merge base of null"):
            entries = table(fixture("graphql-head-a.json"))
            for e in entries:
                if e["argv"][-1] == ".merge_base_commit.sha":
                    e["stdout"] = "null\n"
            with self.assertRaisesRegex(prreview.ReviewError, re.escape(
                    f"the merge base of {BASE[:7]}...{A[:7]}: not a 40-character commit SHA: 'null'")):
                self.plan(cid, entries)
        with self.subTest("a stored reviewed_sha that is a path"):
            self.store().update(cid, {"pr": {"repo": REPO, "number": 42, "reviewed_sha": "../../etc"}}, by="test")
            with self.assertRaisesRegex(prreview.ReviewError, re.escape(
                    f"card {cid}: pr.reviewed_sha: not a 40-character commit SHA: '../../etc'")):
                self.plan(cid, table(fixture("graphql-head-b.json"), head=B, since="since_clean"), stage="delta")
        with self.subTest("sync: a pr view head that is a path"):
            self.store().update(cid, {"pr": {"repo": REPO, "number": 42}}, by="test")
            view = dict(fixture("pr-view.json")["open_head_a"], headRefOid="../../etc")
            entries = [{"argv": ["pr", "view", "42", "--repo", REPO, "--json", GH_SYNC_FIELDS], "stdout": view},
                       {"argv": ["api", "user", "--jq", ".login"], "stdout": "octo-me\n"}]
            before = self.store().get(cid)
            with self.assertRaisesRegex(prreview.ReviewError,
                                        re.escape("pr view headRefOid: not a 40-character commit SHA: '../../etc'")):
                prreview.sync(self.data, cid, cfg=self.cfg, run=Replay(entries))
            self.assertEqual(self.store().get(cid), before)


# --- criterion 4: the verdict follows the findings ---------------------------------------------------------------

def good_finding(**over):
    f = copy.deepcopy(fixture("report-review.json")["findings"]["confirmed"][0])
    f.update(over)
    return f


def buckets(**over):
    out = {"confirmed": [], "out_of_scope": [], "rejected": [], "questions": []}
    out.update(over)
    return out


# The word each verdict's en TL;DR starts with, as commands/pr-review.md tells the role, written here rather than
# read from prreview so a changed word fails.
TLDR_WORD = {"approve": "Approve", "changes": "Changes", "question": "Question"}


def en_only(obj):
    """A copy of `obj` with every {en, zh} text reduced to its en text."""
    if isinstance(obj, dict):
        if "en" in obj and set(obj) <= {"en", "zh"}:
            return {"en": obj["en"]}
        return {k: en_only(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [en_only(v) for v in obj]
    return obj


class Ap2Case(Case):
    """Runs bin/ap2 in a subprocess with a temp HOME and data folder."""

    def setUp(self):
        super().setUp()
        if temp_inside_the_accounts_claude():
            self.skipTest("the temp folder is inside the account's ~/.claude, so ap2 refuses every temp data folder")
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.env = {k: v for k, v in os.environ.items() if k not in ("AP2_DATA", "FAKE_GH_REPLIES", "FAKE_GH_LOG")}
        self.env.update(HOME=str(self.home), AP2_DATA=str(self.data), PYTHON_COLORS="0", PYTHONIOENCODING="utf-8")

    def ap2(self, *args, stdin=None):
        return subprocess.run([sys.executable, str(AP2), *args], capture_output=True, text=True, env=self.env,
                              encoding="utf-8", input=stdin, timeout=60)


class CheckCliTest(Ap2Case):
    def test_check_leaves_a_missing_data_folder_alone(self):
        missing = self.tmp / "nope"
        r = self.ap2("review", "check", "c-20261111-000000-abcdef", "--data", str(missing))
        self.assertEqual((r.returncode, r.stdout), (1, ""))
        self.assertEqual(r.stderr, f"ap2: no data folder at {missing}\n")
        self.assertFalse(missing.exists())


class VerdictCliDataTest(Ap2Case):
    def test_verdict_leaves_a_missing_data_folder_alone(self):
        missing = self.tmp / "nope"
        r = self.ap2("review", "verdict", "--file", "-", "--data", str(missing), stdin=json.dumps(buckets()))
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertEqual(json.loads(r.stdout)["verdict"], "approve")
        self.assertFalse(missing.exists())


class VerdictTest(Ap2Case):
    def verdict(self, obj):
        r = self.ap2("review", "verdict", "--file", "-", stdin=json.dumps(obj))
        return r.returncode, json.loads(r.stdout)

    def test_the_verdict(self):
        q = {k: v for k, v in good_finding(id="q-1", severity="nit").items() if k != "checked"}
        q["question"] = {"en": "Should a retry wait longer?", "zh": "重试要不要等更久？"}
        unchecked = {k: v for k, v in good_finding().items() if k != "checked"}
        rejected = dict(unchecked, reason={"en": "The retry already waits.", "zh": "重试已经会等。"})
        out_of_scope = dict(unchecked, severity="blocker")
        for name, obj, want in (
                ("a blocker", buckets(confirmed=[good_finding(severity="blocker")]), "changes"),
                ("a rejected should-fix", buckets(rejected=[rejected]), "approve"),
                ("an out-of-scope blocker", buckets(out_of_scope=[out_of_scope]), "approve"),
                ("a should-fix", buckets(confirmed=[good_finding()]), "changes"),
                ("only nits", buckets(confirmed=[good_finding(severity="nit")]), "approve"),
                ("nothing", buckets(), "approve"),
                ("an open question", buckets(confirmed=[good_finding(severity="nit")], questions=[q]), "question"),
                ("a whole report", {"findings": buckets(questions=[q])}, "question")):
            with self.subTest(name):
                code, out = self.verdict(obj)
                self.assertEqual((code, out["verdict"], out["errors"]), (0, want, []))
        code, out = self.verdict(buckets(confirmed=[good_finding()], questions=[q]))
        self.assertEqual(out["counts"], {"confirmed": 1, "out_of_scope": 0, "rejected": 0, "questions": 1})

    def test_broken_findings_name_the_field(self):
        no_fix = {k: v for k, v in good_finding().items() if k != "fix"}
        no_checked = {k: v for k, v in good_finding().items() if k != "checked"}
        rejected = {k: v for k, v in good_finding(id="r-1").items() if k != "checked"}
        no_zh = good_finding()
        del no_zh["impact"]["zh"]
        for name, obj, words in (
                ("a missing field", buckets(confirmed=[no_fix]), "findings.confirmed[0].fix: missing"),
                ("an unknown severity", buckets(confirmed=[good_finding(severity="major")]),
                 "findings.confirmed[0].severity: must be one of blocker|should-fix|nit"),
                ("a duplicate id", buckets(confirmed=[good_finding(), good_finding(severity="nit")]),
                 "findings.confirmed[1].id: correctness-1 is used twice"),
                ("a duplicate id across buckets",
                 buckets(confirmed=[good_finding()], rejected=[dict(rejected, id="correctness-1",
                                                                     reason={"en": "no", "zh": "不"})]),
                 "findings.rejected[0].id: correctness-1 is used twice"),
                ("confirmed with no checked", buckets(confirmed=[no_checked]),
                 "findings.confirmed[0].checked: missing"),
                ("rejected with no reason", buckets(rejected=[rejected]), "findings.rejected[0].reason: missing"),
                ("a text missing zh", buckets(confirmed=[no_zh]), "findings.confirmed[0].impact.zh: missing"),
                ("an extra key", buckets(confirmed=[good_finding(score=3)]),
                 "findings.confirmed[0].score: not a field in confirmed"),
                ("a text in a language not configured",
                 buckets(confirmed=[good_finding(impact={"en": "x", "zh": "x", "fr": "x"})]),
                 "findings.confirmed[0].impact.fr: not a configured language (en, zh)"),
                ("not four buckets", {"confirmed": []}, "findings: must be an object"),
                ("an extra bucket", {**buckets(), "nits": []}, "findings: must be an object")):
            with self.subTest(name):
                code, out = self.verdict(obj)
                self.assertEqual(code, 1)
                self.assertIsNone(out["verdict"])
                self.assertTrue(any(words in e for e in out["errors"]), out["errors"])

    def test_a_file_that_is_not_json(self):
        r = self.ap2("review", "verdict", "--file", "-", stdin="{nope")
        self.assertEqual(r.returncode, 1)
        self.assertTrue(r.stderr.startswith("ap2: cannot read '-'"), r.stderr)

    def test_a_file_nested_too_deeply(self):
        # In-process, so json.loads can be made to raise: a subprocess cannot be patched.
        path = self.tmp / "deep.json"
        path.write_text(DEEP_TEXT)
        with too_deep_to_read("cli"), mock.patch.object(sys, "stdout", io.StringIO()) as out, \
                mock.patch.object(sys, "stderr", io.StringIO()) as err:
            code = cli.main(["review", "verdict", "--file", str(path), "--data", str(self.data)])
        self.assertEqual((code, out.getvalue()), (1, ""))
        self.assertEqual(err.getvalue(), f"ap2: cannot read {str(path)!r}: nested too deeply\n")


# --- criterion 5: the report is checked --------------------------------------------------------------------------

class CheckTest(Case):
    def setUp(self):
        super().setUp()
        self.cid = self.card()["id"]
        self.plan(self.cid, table(fixture("graphql-head-a.json")))

    def errors(self, rep):
        self.put_report(self.cid, rep)
        out = prreview.check(self.data, self.cid, cfg=self.cfg)
        self.assertEqual(out["ok"], not out["errors"])
        return out["errors"]

    def test_a_good_report_passes(self):
        self.assertEqual(self.errors(self.report("report-review.json", self.cid)), [])

    def test_a_missing_data_folder_is_refused_not_created(self):
        missing = self.tmp / "nope"
        with self.assertRaisesRegex(prreview.ReviewError, re.escape(f"no data folder at {missing}")):
            prreview.check(missing, self.cid, cfg=self.cfg)
        self.assertFalse(missing.exists())

    def test_a_pending_ci_passes(self):
        rep = self.report("report-review.json", self.cid)
        rep["checks"]["ci"] = "pending"
        self.assertEqual(self.errors(rep), [])

    def test_a_tldr_of_200_characters_passes(self):
        rep = self.report("report-review.json", self.cid)
        rep["tldr"]["en"] = "Changes: " + "x" * 191
        self.assertEqual(self.errors(rep), [])
        rep["tldr"]["en"] += "x"
        self.assertEqual(self.errors(rep), ["tldr.en: 201 characters; at most 200"])
        # zh counts characters, not bytes: 200 of them is 600 bytes and still passes.
        rep["tldr"] = {"en": "Changes: x.", "zh": "一" * 200}
        self.assertEqual(self.errors(rep), [])
        rep["tldr"]["zh"] += "一"
        self.assertEqual(self.errors(rep), ["tldr.zh: 201 characters; at most 200"])

    def test_a_question_report_starts_with_question(self):
        q = {k: v for k, v in good_finding(id="q-1", severity="nit").items() if k != "checked"}
        q["question"] = {"en": "Should a retry wait longer?", "zh": "重试要不要等更久？"}
        rep = self.report("report-review.json", self.cid)
        rep.update(findings=buckets(questions=[q]), verdict="question",
                   tldr={"en": "Question: should a retry wait longer?", "zh": "问题：重试要不要等更久？"})
        self.assertEqual(self.errors(rep), [])

    def test_a_good_delta_report_passes(self):
        self.put_report(self.cid, self.report("report-review.json", self.cid))
        prreview.apply(self.data, self.cid, cfg=self.cfg)
        self.plan(self.cid, table(fixture("graphql-head-b.json"), head=B, since="since_clean"), stage="delta")
        self.assertEqual(self.errors(self.report("report-delta.json", self.cid)), [])

    def test_a_delta_marks_only_the_earlier_confirmed_and_question_ids(self):
        rejected = {k: v for k, v in good_finding(id="r-1", severity="nit").items() if k != "checked"}
        rejected["reason"] = {"en": "Not a bug.", "zh": "不是问题。"}
        out_of_scope = {k: v for k, v in good_finding(id="o-1", severity="nit").items() if k != "checked"}
        rep = self.report("report-review.json", self.cid)
        rep["findings"].update(rejected=[rejected], out_of_scope=[out_of_scope])
        self.assertEqual(self.errors(rep), [])
        prreview.apply(self.data, self.cid, cfg=self.cfg)
        self.assertEqual([f["id"] for f in self.store().get(self.cid)["findings"]["rejected"]], ["r-1"])
        self.assertEqual([f["id"] for f in self.store().get(self.cid)["findings"]["out_of_scope"]], ["o-1"])
        self.plan(self.cid, table(fixture("graphql-head-b.json"), head=B, since="since_clean"), stage="delta")
        delta = self.report("report-delta.json", self.cid)
        self.assertEqual(delta["earlier"], [{"id": "correctness-1", "state": "fixed"}])
        self.assertEqual(self.errors(delta), [])
        card = prreview.apply(self.data, self.cid, cfg=self.cfg)
        self.assertEqual(card["verdict"]["head_sha"], B)

    def test_bad_reports(self):
        good = self.report("report-review.json", self.cid)

        def bad(**over):
            rep = copy.deepcopy(good)
            for k, v in over.items():
                if v is None:
                    rep.pop(k, None)
                else:
                    rep[k] = v
            return rep

        script = '<svg viewBox="0 0 10 10"><script>alert(1)</script></svg>'
        for name, rep, words in (
                ("an unknown verdict", bad(verdict="request_changes"),
                 "verdict: must be one of approve|changes|question"),
                ("a verdict the findings do not give", bad(verdict="approve",
                                                           tldr={"en": "Approve: fine.", "zh": "没问题。"}),
                 "verdict: must be changes (it follows the findings)"),
                ("a two-line TL;DR", bad(tldr={"en": "Changes: one.\nTwo.", "zh": "一。"}), "tldr.en: must be one line"),
                ("a 201-character TL;DR", bad(tldr={"en": "Changes: " + "x" * 192, "zh": "一。"}),
                 "tldr.en: 201 characters; at most 200"),
                ("an en TL;DR not starting with its verdict", bad(tldr={"en": "Looks risky.", "zh": "有风险。"}),
                 "tldr.en: must start with the verdict, 'Changes'"),
                ("an en TL;DR starting with another verdict", bad(tldr={"en": "Approve: fine.", "zh": "没问题。"}),
                 "tldr.en: must start with the verdict, 'Changes'"),
                ("an en TL;DR with its verdict word later",
                 bad(tldr={"en": "Approve the idea, but Changes are needed.", "zh": "一。"}),
                 "tldr.en: must start with the verdict, 'Changes'"),
                ("a two-line zh TL;DR", bad(tldr={"en": "Changes: x.", "zh": "一。\n二。"}),
                 "tldr.zh: must be one line"),
                ("a 201-character zh TL;DR", bad(tldr={"en": "Changes: x.", "zh": "一" * 201}),
                 "tldr.zh: 201 characters; at most 200"),
                ("a carriage return in the TL;DR", bad(tldr={"en": "Changes: a\rb", "zh": "一。"}),
                 "tldr.en: must be one line"),
                ("a line separator in the TL;DR", bad(tldr={"en": "Changes: a\u2028b", "zh": "一。"}),
                 "tldr.en: must be one line"),
                ("angles not the plan's", bad(angles=good["angles"][:2]), "angles: must be the plan's angles"),
                ("the plan's angles out of order", bad(angles=good["angles"][::-1]),
                 "angles: must be the plan's angles"),
                ("an angle from another layer", bad(angles=[dict(good["angles"][0], origin="user")] + good["angles"][1:]),
                 "angles: must be the plan's angles"),
                ("a head not the plan's", bad(head_sha=B), f"head_sha: must be {A}"),
                ("a stage not the plan's", bad(stage="delta"), "stage: must be review"),
                ("a <script> in the diagram", bad(diagram=script), "diagram: must be one inline <svg>"),
                ("an onload in the diagram", bad(diagram='<svg onload="x()"></svg>'), "diagram: must be one"),
                ("a diagram that is not an svg", bad(diagram="<div></div>"), "diagram: must be one inline <svg>"),
                ("a javascript: link in the diagram",
                 bad(diagram='<svg><a href="javascript:alert(1)"><text>x</text></a></svg>'),
                 "diagram: must be one inline <svg>"),
                ("a foreignObject in the diagram", bad(diagram="<svg><foreignObject><div>x</div></foreignObject></svg>"),
                 "diagram: must be one inline <svg>"),
                ("markup after the svg", bad(diagram='<svg></svg><iframe src="https://example.com/x"></iframe>'),
                 "diagram: must be one inline <svg>"),
                ("a missing block", bad(problem=None), "problem: missing"),
                ("no tldr", bad(tldr=None), "tldr: missing"),
                ("no solution", bad(solution=None), "solution: missing"),
                ("no reply_draft", bad(reply_draft=None), "reply_draft: missing"),
                ("a tldr that is a plain string", bad(tldr="Changes: x."),
                 "tldr: must be a text object with en, zh"),
                ("a text missing zh", bad(background={"en": "x"}), "background.zh: missing"),
                ("a bad time", bad(reviewed_at="2026-11-11 09:30"), "reviewed_at: must be a UTC time"),
                ("a bad ci", bad(checks={"ci": "yellow", "notes": good["checks"]["notes"]}), "checks.ci: must be"),
                ("no checks", bad(checks=None), "checks: must be an object with ci and notes"),
                ("no angles", bad(angles=None), "angles: must list {name, origin, why} for each angle that ran"),
                ("an unknown field", bad(score=3), "score: not a field of the report"),
                ("another card", bad(card="c-20200101-000000-000000"), f"card: must be {self.cid}"),
                ("earlier on a full review", bad(earlier=[]), "earlier: only in a delta review"),
                ("since_last on a full review", bad(since_last={"en": "x", "zh": "x"}),
                 "since_last: only in a delta review")):
            with self.subTest(name):
                errors = self.errors(rep)
                self.assertTrue(any(words in e for e in errors), errors)

    def test_texts_that_are_not_text_objects(self):
        good = self.report("report-review.json", self.cid)
        q = {k: v for k, v in good_finding(id="q-1", severity="nit").items() if k != "checked"}
        q["question"] = "Should a retry wait longer?"
        r = {k: v for k, v in good_finding(id="r-1", severity="nit").items() if k != "checked"}
        r["reason"] = {"en": "Not a bug.", "zh": "   "}
        for name, path, value, want in (
                ("a question that is a plain string", ("findings", "questions"), [q],
                 "findings.questions[0].question: must be a text object with en, zh"),
                ("a reason that is only spaces", ("findings", "rejected"), [r],
                 "findings.rejected[0].reason.zh: must be non-empty text"),
                ("an angle's why that is a plain string", ("angles", 0, "why"), "x",
                 "angles[0].why: must be a text object with en, zh"),
                ("notes that are a plain string", ("checks", "notes"), "x",
                 "checks.notes: must be a text object with en, zh")):
            with self.subTest(name):
                rep = copy.deepcopy(good)
                at = rep
                for k in path[:-1]:
                    at = at[k]
                at[path[-1]] = value
                self.assertIn(want, self.errors(rep))

    def test_files_that_cannot_be_read(self):
        path = prreview.run_dir(self.data, self.cid) / prreview.REPORT_FILE
        with self.subTest("too large"):
            path.write_text(" " * (prreview.sources.MAX_SOURCE + 1))
            errors = prreview.check(self.data, self.cid, cfg=self.cfg)["errors"]
            self.assertEqual(errors, [f"prreview.json: larger than {prreview.sources.MAX_SOURCE} bytes"])
        with self.subTest("not JSON"):
            path.write_text("{nope")
            errors = prreview.check(self.data, self.cid, cfg=self.cfg)["errors"]
            self.assertEqual(len(errors), 1)
            self.assertTrue(errors[0].startswith("prreview.json: not JSON"), errors)
        with self.subTest("nested too deeply"):
            path.write_text(DEEP_TEXT)
            with too_deep_to_read("prreview"):
                errors = prreview.check(self.data, self.cid, cfg=self.cfg)["errors"]
            self.assertEqual(errors, ["prreview.json: not JSON (nested too deeply)"])
        with self.subTest("missing"):
            path.unlink()
            errors = prreview.check(self.data, self.cid, cfg=self.cfg)["errors"]
            self.assertTrue(errors[0].startswith("prreview.json: no report at"), errors)
        with self.subTest("no plan"):
            (path.parent / prreview.PLAN_FILE).unlink()
            with self.assertRaisesRegex(prreview.ReviewError, "no review plan"):
                prreview.check(self.data, self.cid, cfg=self.cfg)

    def test_a_plan_nested_too_deeply(self):
        path = prreview.run_dir(self.data, self.cid) / prreview.PLAN_FILE
        path.write_text(DEEP_TEXT)
        with too_deep_to_read("prreview"), \
                self.assertRaisesRegex(prreview.ReviewError, "not a review plan: nested too deeply"):
            prreview.check(self.data, self.cid, cfg=self.cfg)

    def test_bad_delta_reports(self):
        q = {k: v for k, v in good_finding(id="q-1", severity="nit").items() if k != "checked"}
        q["question"] = {"en": "Should a retry wait longer?", "zh": "重试要不要等更久？"}
        rep = self.report("report-review.json", self.cid)
        rep["findings"]["questions"] = [q]
        self.put_report(self.cid, rep)
        prreview.apply(self.data, self.cid, cfg=self.cfg)
        self.plan(self.cid, table(fixture("graphql-head-b.json"), head=B, since="since_clean"), stage="delta")
        good = self.report("report-delta.json", self.cid)
        good["earlier"] = [{"id": "correctness-1", "state": "fixed"}, {"id": "q-1", "state": "answered"}]
        self.assertEqual(self.errors(good), [])
        still = good_finding()
        new_reusing = good_finding(id="q-1", severity="nit")
        moved = {k: v for k, v in good_finding().items() if k != "checked"}
        moved["reason"] = {"en": "Not a bug after all.", "zh": "其实不是问题。"}

        def bad(earlier=None, findings=None, drop=()):
            r = copy.deepcopy(good)
            if earlier is not None:
                r["earlier"] = earlier
            if findings is not None:
                r["findings"] = buckets(**findings)
                r["verdict"] = prreview.verdict(r["findings"])
                word = TLDR_WORD[r["verdict"]]
                r["tldr"] = {"en": f"{word}: see the findings.", "zh": "看发现。"}
            for k in drop:
                r.pop(k)
            return r

        for name, rep, words in (
                ("no since_last", bad(drop=("since_last",)), "since_last: missing"),
                ("a since_last that is a plain string", dict(copy.deepcopy(good), since_last="x"),
                 "since_last: must be a text object with en, zh"),
                ("no earlier", bad(drop=("earlier",)), "earlier: a delta must list"),
                ("an earlier id left out", bad(earlier=[{"id": "correctness-1", "state": "fixed"}]),
                 "earlier: q-1 (questions) is left out"),
                ("open on an id gone from its bucket",
                 bad(earlier=[{"id": "correctness-1", "state": "open"}, {"id": "q-1", "state": "answered"}]),
                 "correctness-1 is open but is not in findings.confirmed"),
                ("open on an id moved to another bucket",
                 bad(earlier=[{"id": "correctness-1", "state": "open"}, {"id": "q-1", "state": "answered"}],
                     findings={"rejected": [moved]}),
                 "(it is in rejected)"),
                ("fixed on an id still listed", bad(findings={"confirmed": [still]}),
                 "correctness-1 is fixed but is still listed in findings.confirmed"),
                ("fixed on an id moved to another bucket", bad(findings={"rejected": [moved]}),
                 "correctness-1 is fixed but is still listed in findings.rejected"),
                ("answered on a confirmed id",
                 bad(earlier=[{"id": "correctness-1", "state": "answered"}, {"id": "q-1", "state": "answered"}]),
                 "correctness-1 is a confirmed finding; mark it fixed"),
                ("an id listed twice",
                 bad(earlier=good["earlier"] + [{"id": "q-1", "state": "answered"}]), "q-1 is listed twice"),
                ("an id that was not on the card",
                 bad(earlier=good["earlier"] + [{"id": "other-9", "state": "fixed"}]), "other-9 is not an earlier"),
                ("a bad state", bad(earlier=[{"id": "correctness-1", "state": "gone"},
                                             {"id": "q-1", "state": "answered"}]), "earlier[0].state: must be one of"),
                ("fixed on a question id",
                 bad(earlier=[{"id": "correctness-1", "state": "fixed"}, {"id": "q-1", "state": "fixed"}]),
                 "q-1 is a question; mark it answered, not fixed")):
            with self.subTest(name):
                errors = self.errors(rep)
                self.assertTrue(any(words in e for e in errors), errors)
        for name, value in (("questions: true", True), ("confirmed: 1", 1)):
            with self.subTest(name):
                rep = copy.deepcopy(good)
                rep["findings"][name.split(":")[0]] = value
                errors = self.errors(rep)
                self.assertTrue(any("findings: must be an object with exactly the lists" in e for e in errors), errors)

        # Once q-1 is answered and on the card's answered list, a later delta's new finding may not take its id.
        self.put_report(self.cid, good)
        prreview.apply(self.data, self.cid, cfg=self.cfg)
        c_pages = with_head(fixture("graphql-head-b.json"), "f" * 40)
        entries = table(c_pages, head="f" * 40, since="since_clean", reviewed=B)
        self.plan(self.cid, entries, stage="delta")
        rep = copy.deepcopy(good)
        rep.update(head_sha="f" * 40, reviewed_at="2026-11-11T12:00:00Z", earlier=[],
                   findings=buckets(confirmed=[new_reusing]))
        rep["tldr"] = {"en": "Approve: one nit.", "zh": "一个小问题。"}
        errors = self.errors(rep)
        self.assertTrue(any("q-1 reuses an earlier id (answered)" in e for e in errors), errors)
        rep["findings"] = buckets(confirmed=[good_finding(severity="nit")])
        errors = self.errors(rep)
        self.assertTrue(any("correctness-1 reuses an earlier id (fixed)" in e for e in errors), errors)


# --- apply -------------------------------------------------------------------------------------------------------

C = "f" * 40


class ApplyTest(Case):
    def question(self):
        q = {k: v for k, v in good_finding(id="q-1", severity="nit").items() if k != "checked"}
        q["question"] = {"en": "Should a retry wait longer?", "zh": "重试要不要等更久？"}
        return q

    def two_finding_card(self):
        """A card with a full review at A holding correctness-1 (confirmed) and q-1 (a question)."""
        cid = self.card()["id"]
        self.plan(cid, table(fixture("graphql-head-a.json")))
        rep = self.report("report-review.json", cid)
        rep["findings"]["questions"] = [self.question()]
        self.put_report(cid, rep)
        prreview.apply(self.data, cid, cfg=self.cfg)
        return cid

    def delta(self, cid, earlier, findings=None, head=B, reviewed=A, at="2026-11-11T11:00:00Z"):
        """Plan a delta at `head` and put a delta report with these marks; return the report."""
        pages = fixture("graphql-head-b.json") if head == B else with_head(fixture("graphql-head-b.json"), head)
        self.plan(cid, table(pages, head=head, since="since_clean", reviewed=reviewed), stage="delta")
        rep = self.report("report-delta.json", cid)
        rep.update(head_sha=head, reviewed_at=at, earlier=earlier, findings=buckets(**(findings or {})))
        rep["verdict"] = prreview.verdict(rep["findings"])
        if rep["verdict"] != "approve":
            word = TLDR_WORD[rep["verdict"]]
            rep["tldr"] = {"en": f"{word}: see the findings.", "zh": "看发现。"}
        self.put_report(cid, rep)
        return rep

    def test_the_applied_verdict_block_and_review_entry(self):
        cid = self.card()["id"]
        self.plan(cid, table(fixture("graphql-head-a.json")))
        rep = self.report("report-review.json", cid)
        rep["diagram"] = DIAGRAM
        rep["checks"]["ci"] = "red"
        self.put_report(cid, rep)
        card = prreview.apply(self.data, cid, cfg=self.cfg)
        self.assertEqual(card["verdict"], {
            "verdict": "changes", "tldr": rep["tldr"], "background": rep["background"], "problem": rep["problem"],
            "solution": rep["solution"], "diagram": DIAGRAM, "ci": "red", "notes": rep["checks"]["notes"],
            "angles": rep["angles"], "stage": "review", "head_sha": A, "reviewed_at": "2026-11-11T09:30:00Z"})
        self.assertEqual(set(card["reviews"][-1]), {"stage", "head_sha", "verdict", "at"})
        self.assertEqual({k: card["reviews"][-1][k] for k in ("stage", "head_sha", "verdict")},
                         {"stage": "review", "head_sha": A, "verdict": "changes"})
        self.assertRegex(card["reviews"][-1]["at"], prreview.STAMP)

        # A delta over docs only would pick Correctness and Accuracy anew; it keeps the applied angles instead.
        fresh, _ = self.plan(cid, table(with_head(fixture("graphql-docs.json"), B), head=B), stage="review")
        self.assertEqual([a["name"] for a in fresh["angles"]], ["Correctness", "Accuracy"])
        out, _ = self.plan(cid, table(with_head(fixture("graphql-docs.json"), B), head=B, since="since_clean"),
                           stage="delta")
        self.assertEqual(out["stage"], "delta")
        self.assertEqual([(a["name"], a["origin"]) for a in out["angles"]],
                         [(a["name"], a["origin"]) for a in rep["angles"]])
        self.assertEqual({a["why"] for a in out["angles"]}, {"same angle as the last review"})

    def test_apply_on_an_own_pr_writes_its_own_words(self):
        cid = self.card("github-my-prs")["id"]
        self.plan(cid, table(fixture("graphql-head-a.json")))
        rep = self.report("report-review.json", cid)
        rep["findings"]["questions"] = [self.question()]
        self.put_report(cid, rep)
        card = prreview.apply(self.data, cid, cfg=self.cfg)
        self.assertEqual(card["plain"], {
            "what": {"en": f"Your PR #42: {TITLE}", "zh": f"你的 PR #42：{TITLE}"},
            "where": rep["tldr"],
            "do": {"en": "Read the 2 findings, then fix the ones you agree with on your branch.",
                   "zh": "看一下这 2 条发现，然后在你的分支上修好你认同的那些。"}})

    def test_fixed_and_answered_are_kept_across_deltas(self):
        cid = self.two_finding_card()
        earlier = self.store().get(cid)["findings"]
        confirmed, question = earlier["confirmed"][0], earlier["questions"][0]
        self.delta(cid, [{"id": "correctness-1", "state": "fixed"}, {"id": "q-1", "state": "answered"}])
        card = prreview.apply(self.data, cid, cfg=self.cfg)
        # An answered question is not counted as fixed.
        self.assertEqual(card["audit"][-1]["did"], f"delta review {B[:7]}: approve, 1 fixed, 0 open")
        self.delta(cid, [], head=C, reviewed=B, at="2026-11-11T12:00:00Z")
        card = prreview.apply(self.data, cid, cfg=self.cfg)
        # The whole finding is kept, as the card had it, with the head it was settled in.
        self.assertEqual(card["findings"]["fixed"], [dict(confirmed, fixed_in=B)])
        self.assertEqual(card["findings"]["answered"], [dict(question, fixed_in=B)])

    def test_a_full_review_starts_fixed_and_answered_empty(self):
        cid = self.two_finding_card()
        self.delta(cid, [{"id": "correctness-1", "state": "fixed"}, {"id": "q-1", "state": "answered"}])
        self.assertTrue(prreview.apply(self.data, cid, cfg=self.cfg)["findings"]["fixed"])
        self.plan(cid, table(with_head(fixture("graphql-head-a.json"), C), head=C))
        rep = self.report("report-review.json", cid)
        rep.update(head_sha=C, reviewed_at="2026-11-11T12:00:00Z")
        self.put_report(cid, rep)
        card = prreview.apply(self.data, cid, cfg=self.cfg)
        self.assertEqual((card["findings"]["fixed"], card["findings"]["answered"]), ([], []))

    def test_a_delta_counts_its_marks_and_keeps_since_last(self):
        cid = self.two_finding_card()
        rep = self.delta(cid, [{"id": "correctness-1", "state": "fixed"}, {"id": "q-1", "state": "open"}],
                         findings={"questions": [self.question()]})
        card = prreview.apply(self.data, cid, cfg=self.cfg)
        self.assertEqual(card["audit"][-1]["did"], f"delta review {B[:7]}: question, 1 fixed, 1 open")
        self.assertEqual(card["verdict"]["since_last"], rep["since_last"])
        self.assertEqual(card["verdict"]["stage"], "delta")

    def test_stored_buckets_that_are_not_lists_do_not_break_a_delta(self):
        cid = self.two_finding_card()
        found = dict(self.store().get(cid)["findings"], fixed=True, answered=True)
        Store(self.data, cfg=self.cfg).update(cid, {"findings": found}, by="test")
        self.delta(cid, [{"id": "correctness-1", "state": "fixed"}, {"id": "q-1", "state": "answered"}])
        card = prreview.apply(self.data, cid, cfg=self.cfg)
        self.assertEqual([(f["id"], f["fixed_in"]) for f in card["findings"]["fixed"]], [("correctness-1", B)])
        self.assertEqual([(f["id"], f["fixed_in"]) for f in card["findings"]["answered"]], [("q-1", B)])

    def test_the_same_head_reviewed_again_at_another_time_is_applied(self):
        cid = self.reviewed_card()
        self.plan(cid, table(fixture("graphql-head-a.json")))
        rep = self.report("report-review.json", cid)
        rep["reviewed_at"] = "2026-11-11T12:00:00Z"
        self.put_report(cid, rep)
        card = prreview.apply(self.data, cid, cfg=self.cfg)
        self.assertEqual(len(card["reviews"]), 2)
        self.assertEqual(card["verdict"]["reviewed_at"], "2026-11-11T12:00:00Z")

    def test_the_same_report_twice_is_refused(self):
        cid = self.reviewed_card()
        before = self.store().get(cid)
        with self.assertRaisesRegex(prreview.ReviewError, "is already on the card"):
            prreview.apply(self.data, cid, cfg=self.cfg)
        self.assertEqual(len(self.store().get(cid)["reviews"]), len(before["reviews"]))
        self.assertEqual(self.store().get(cid), before)

    def test_a_report_that_fails_the_check_is_refused(self):
        cid = self.card()["id"]
        self.plan(cid, table(fixture("graphql-head-a.json")))
        rep = self.report("report-review.json", cid)
        rep.update(verdict="approve", tldr={"en": "Approve: fine.", "zh": "没问题。"})
        self.put_report(cid, rep)
        before = self.store().get(cid)
        with self.assertRaisesRegex(prreview.ReviewError, "does not pass ap2 review check"):
            prreview.apply(self.data, cid, cfg=self.cfg)
        self.assertEqual(self.store().get(cid)["updated_at"], before["updated_at"])
        self.assertEqual(self.store().get(cid), before)

    def test_a_write_between_the_read_and_the_write_is_kept(self):
        cid = self.card()["id"]
        self.plan(cid, table(fixture("graphql-head-a.json")))
        self.put_report(cid, self.report("report-review.json", cid))
        real = prreview.check_report

        def racing(*a, **kw):  # another writer lands while apply checks the report
            Store(self.data, cfg=self.cfg).update(cid, {"pr": {"repo": REPO, "number": 42, "reviewed_sha": B}},
                                                  by="test")
            return real(*a, **kw)

        with mock.patch.object(prreview, "check_report", racing), \
                self.assertRaisesRegex(prreview.ReviewError,
                                       f"card {cid} changed while the report was being applied; run it again"):
            prreview.apply(self.data, cid, cfg=self.cfg)
        card = self.store().get(cid)
        self.assertEqual(card["pr"]["reviewed_sha"], B)
        self.assertNotIn("reviews", card)


# --- the configured languages, not a fixed pair ------------------------------------------------------------------

def english_only(case):
    """Give a Case the default config: English only, no secondary language."""
    (case.data / "config.yaml").write_text("primary_language: en\n")
    case.cfg = Config()


class EnglishOnlyTest(Case):
    def setUp(self):
        super().setUp()
        english_only(self)

    def test_an_english_only_report_is_checked_and_applied(self):
        cid = self.card()["id"]
        out, _ = self.plan(cid, table(fixture("graphql-head-a.json")))
        self.assertEqual(out["languages"], ["en"])
        self.put_report(cid, en_only(self.report("report-review.json", cid)))
        self.assertEqual(prreview.check(self.data, cid, cfg=self.cfg), {"ok": True, "errors": []})
        card = prreview.apply(self.data, cid, cfg=self.cfg)
        self.assertEqual(card["verdict"]["verdict"], "changes")
        self.assertEqual({line: set(words) for line, words in card["plain"].items()},
                         {"what": {"en"}, "where": {"en"}, "do": {"en"}})
        self.assertEqual(card["plain"]["where"]["en"], fixture("report-review.json")["tldr"]["en"])


class EnglishOnlyVerdictCliTest(Ap2Case):
    def setUp(self):
        super().setUp()
        english_only(self)

    def test_an_english_only_finding_gives_its_verdict(self):
        path = self.tmp / "findings.json"
        path.write_text(json.dumps(buckets(confirmed=[en_only(good_finding())])))
        r = self.ap2("review", "verdict", "--file", str(path), "--data", str(self.data))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.loads(r.stdout)["verdict"], "changes")
        self.assertEqual(json.loads(r.stdout)["errors"], [])


# --- criteria 6 and 8: the whole flow through bin/ap2 and fake-gh-replay, in shadow and live mode ----------------

class FlowTest(Ap2Case):
    def setUp(self):
        super().setUp()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "gh").symlink_to(FAKE_GH)
        (self.bin / "python3").symlink_to(sys.executable)
        self.path = str(self.bin)
        self.replies = self.tmp / "replies.json"
        self.log = self.tmp / "gh.log"
        self.env.update(PATH=self.path, FAKE_GH_REPLIES=str(self.replies), FAKE_GH_LOG=str(self.log))

    def ok(self, *args):
        r = self.ap2(*args)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return json.loads(r.stdout)

    def test_the_fake_is_the_only_gh(self):
        self.assertTrue(stat.S_IMODE(FAKE_GH.stat().st_mode) & stat.S_IXUSR)
        found = shutil.which("gh", path=self.path)
        self.assertEqual(Path(found).resolve(), FAKE_GH.resolve())

    def test_from_a_review_request_to_a_delta_that_marks_a_finding_fixed(self):
        for mode in ("shadow", "live"):
            with self.subTest(mode=mode):
                shutil.rmtree(self.data)
                self.data.mkdir()
                (self.data / "config.yaml").write_text(f"mode: {mode}\nsecondary_language: zh\n")
                self.log.write_text("")
                self.flow()
                logged = [json.loads(line) for line in self.log.read_text().splitlines()]
                self.assertTrue(logged)
                self.assertEqual([a for a in logged if prreview.refusal(a) is not None], [])
                self.assertTrue({a[0] for a in logged} <= {"api", "pr"})

    def test_plan_reads_the_repo_layer_from_the_checkout(self):
        repo = self.tmp / "checkout"
        repo.mkdir()
        git(repo, "init", "-q")
        (repo / ".autopilot").mkdir()
        (repo / ".autopilot" / "review.yaml").write_text(
            '- name: Payments safety\n  question: "Can this change charge a customer twice?"\n'
            '  when: { paths: ["billing/**"] }\n  brief: "Read every caller of the charge functions."\n')
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "init")
        head = git(repo, "rev-parse", "HEAD")
        self.env["PATH"] = self.path + os.pathsep + str(Path(shutil.which("git")).parent)
        raw = self.tmp / "gh-search-prs.json"
        shutil.copy(SCAN / "gh-search-prs.json", raw)
        read = self.ok("source", "read", "github-review-requests", "--raw", str(raw))
        cid = next(i["card"] for i in read["items"] if i["key"] == KEY)
        self.replies.write_text(json.dumps(table(with_head(fixture("graphql-head-a.json"), head), head=head)))
        plan = self.ok("review", "plan", cid, "--checkout", str(repo))
        self.assertEqual(plan["repo_layer"], f"{repo} at {head[:7]}")
        self.assertIn(("Payments safety", "repo"), [(a["name"], a["origin"]) for a in plan["angles"]])

    def flow(self):
        raw = self.tmp / "gh-search-prs.json"
        shutil.copy(SCAN / "gh-search-prs.json", raw)
        read = self.ok("source", "read", "github-review-requests", "--raw", str(raw))
        cid = next(i["card"] for i in read["items"] if i["key"] == KEY)

        self.replies.write_text(json.dumps(table(fixture("graphql-head-a.json"), view="open_head_a")))
        self.assertEqual(self.ok("review", "sync", cid)["state"], "not_reviewed")
        plan = self.ok("review", "plan", cid)
        self.assertEqual((plan["stage"], plan["head_sha"]), ("review", A))
        rep = self.report("report-review.json", cid)
        Path(plan["report"]).write_text(json.dumps(dict(rep, verdict="approve")))
        r = self.ap2("review", "check", cid)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIs(json.loads(r.stdout)["ok"], False)
        Path(plan["report"]).write_text(json.dumps(rep))
        self.assertEqual(self.ok("review", "check", cid), {"ok": True, "errors": []})
        card = self.ok("review", "apply", cid)
        self.assertEqual(card["plain"]["where"]["en"], rep["tldr"]["en"])
        self.assertTrue(card["plain"]["do"]["en"].startswith("Read the 1 finding"))
        self.assertIs(card["draft"]["sent"], False)
        self.assertEqual(card["draft"]["text"], rep["reply_draft"])
        self.assertEqual(card["pr"]["reviewed_sha"], A)
        self.assertEqual(card["verdict"]["verdict"], "changes")
        self.assertEqual(card["audit"][-1]["did"], f"reviewed {A[:7]}: changes")
        self.assertEqual(self.ok("review", "sync", cid)["state"], "current")

        self.replies.write_text(json.dumps(table(fixture("graphql-head-b.json"), head=B, since="since_clean",
                                                 view="open_head_b")))
        synced = self.ok("review", "sync", cid)
        self.assertEqual((synced["state"], synced["lane"]), ("new_commits", "needs"))
        plan = self.ok("review", "plan", cid, "--stage", "delta")
        self.assertEqual((plan["stage"], plan["compare"], plan["whole_pr"]), ("delta", compare(A, B), None))
        delta = self.report("report-delta.json", cid)
        Path(plan["report"]).write_text(json.dumps(delta))
        self.assertEqual(self.ok("review", "check", cid), {"ok": True, "errors": []})
        card = self.ok("review", "apply", cid)
        self.assertEqual(card["findings"]["fixed"][0]["id"], "correctness-1")
        self.assertEqual(card["findings"]["fixed"][0]["fixed_in"], B)
        self.assertEqual(card["findings"]["confirmed"], [])
        self.assertEqual(card["plain"]["where"]["en"], delta["tldr"]["en"])
        self.assertEqual(card["plain"]["do"]["en"],
                         "Read the review, then approve or ask for changes on GitHub yourself.")
        self.assertEqual([(r["stage"], r["head_sha"], r["verdict"]) for r in card["reviews"]],
                         [("review", A, "changes"), ("delta", B, "approve")])
        self.assertEqual(card["pr"]["reviewed_sha"], B)
        self.assertEqual(card["audit"][-1]["did"], f"delta review {B[:7]}: approve, 1 fixed, 0 open")
        points = [e["restore"] for e in card["audit"] if e.get("restore")]
        self.assertGreaterEqual(len(points), 2)
        for p in points:
            self.assertEqual(self.ok("card", "show", cid)["id"], cid)
        self.assertTrue(any((self.data / "restore").rglob("*.json")))


# --- criterion 7: the card closes --------------------------------------------------------------------------------

class SyncTest(Case):
    def sync(self, cid, view):
        run = Replay(table([], view=view))
        return prreview.sync(self.data, cid, cfg=self.cfg, run=run)

    def test_the_card_closes(self):
        base = {k: v for k, v in self.card().items() if k not in ("id", "created_at", "updated_at", "audit")}
        for view, state, did in (("merged", "merged", "the PR was merged"), ("closed", "closed", "the PR was closed"),
                                 ("reviewed", "reviewed", "you reviewed it on GitHub"),
                                 ("team_only_changes_requested", "reviewed", "you reviewed it on GitHub"),
                                 ("team_only_commented", "reviewed", "you reviewed it on GitHub"),
                                 ("no_request","request_removed", "the review request was removed"),
                                 ("other_user_only", "request_removed", "the review request was removed")):
            with self.subTest(view):
                cid = self.store().add(base, by="test")["id"]
                out = self.sync(cid, view)
                self.assertEqual((out["state"], out["lane"]), (state, "done"))
                card = self.store().get(cid)
                self.assertEqual(card["lane"], "done")
                self.assertEqual(card["audit"][-1]["did"], did)
                self.assertEqual(card["pr"]["head_sha"], fixture("pr-view.json")[view]["headRefOid"])

    def test_the_card_closes_after_a_review_by_ap2(self):
        """Merged, closed, reviewed and request_removed come before not_reviewed, new_commits and current: a card
        ap2 already reviewed (at the same head, or at an older one) closes the same way a fresh card does."""
        base = {k: v for k, v in self.card().items() if k not in ("id", "created_at", "updated_at", "audit")}
        views = fixture("pr-view.json")
        for view, reviewed, state, did in (
                ("merged", B, "merged", "the PR was merged"), ("merged", A, "merged", "the PR was merged"),
                ("closed", A, "closed", "the PR was closed"),
                ("reviewed", A, "reviewed", "you reviewed it on GitHub"),
                ("team_only_changes_requested", A, "reviewed", "you reviewed it on GitHub"),
                ("team_only_commented", A, "reviewed", "you reviewed it on GitHub"),
                ("reviewed_head_b_team", A, "reviewed", "you reviewed it on GitHub"),
                ("no_request", A, "request_removed", "the review request was removed"),
                ("other_user_only", A, "request_removed", "the review request was removed")):
            with self.subTest(view=view, reviewed=reviewed[:7]):
                cid = self.store().add(base, by="test")["id"]
                self.store().update(cid, {"pr": {"repo": REPO, "number": 42, "reviewed_sha": reviewed}}, by="test")
                out = self.sync(cid, view)
                self.assertEqual((out["state"], out["lane"], out["reviewed_sha"]), (state, "done", reviewed))
                card = self.store().get(cid)
                self.assertEqual(card["lane"], "done")
                self.assertEqual(card["audit"][-1]["did"], did)
                self.assertEqual(card["pr"]["reviewed_sha"], reviewed)
                self.assertEqual(card["pr"]["head_sha"], views[view]["headRefOid"])

    def test_a_write_during_the_sync_is_kept(self):
        cid = self.card()["id"]
        replay = Replay(table([], view="open_head_b"))

        def run(argv):  # an apply lands while sync reads GitHub
            if argv[1:3] == ["pr", "view"]:
                Store(self.data, cfg=self.cfg).update(cid, {"pr": {"repo": REPO, "number": 42, "reviewed_sha": A}},
                                                      by="test")
            return replay(argv)

        with self.assertRaisesRegex(prreview.ReviewError, f"card {cid} changed while it was being synced; run it again"):
            prreview.sync(self.data, cid, cfg=self.cfg, run=run)
        self.assertEqual(self.store().get(cid)["pr"], {"repo": REPO, "number": 42, "reviewed_sha": A})

    def test_the_card_stays_open(self):
        cid = self.card()["id"]
        with self.subTest("a team request alone"):
            out = self.sync(cid, "team_only")
            self.assertEqual(out["state"], "not_reviewed")
            self.assertEqual(self.store().get(cid)["lane"], "needs")
            self.assertEqual(self.store().get(cid)["pr"]["head_sha"], A)
        with self.subTest("a new head"):
            self.store().update(cid, {"pr": {"repo": REPO, "number": 42, "reviewed_sha": A}}, by="test")
            out = self.sync(cid, "open_head_b")
            self.assertEqual((out["state"], out["lane"]), ("new_commits", "needs"))
            self.assertEqual(self.store().get(cid)["pr"], {"repo": REPO, "number": 42, "reviewed_sha": A,
                                                           "head_sha": B, "url": f"https://github.com/{REPO}/pull/42"})
        with self.subTest("current"):
            self.assertEqual(self.sync(cid, "open_head_a")["state"], "current")
        with self.subTest("a card in done is left alone"):
            before = self.store().update(cid, {"lane": "done"}, by="test")
            out = self.sync(cid, "open_head_b")
            self.assertEqual((out["state"], out["changed"]), ("new_commits", False))
            self.assertEqual(self.store().get(cid), before)

    def test_a_card_a_slack_ask_made_is_never_request_removed(self):
        """A Slack ask's card (its link is the message, not the PR's page) was never asked on GitHub, so no review
        request there does not close it; merged, closed and the user's own review still do."""
        base = {k: v for k, v in self.card().items() if k not in ("id", "created_at", "updated_at", "audit")}
        ask = {"id": "1730000000.123456", "author": "U0EXAMPLE2", "at": "1730000000.123456",
               "text": "PTAL <https://github.com/example/widgets/pull/42>", "pr": KEY}
        base["source"] = {"name": "slack-mentions", "record": ask, "raw": None,
                          "link": "https://slack.com/archives/C0123ABCD/p1730000000123456"}  # checkpkg: ignore
        base["feed"] = "slack-mentions"
        nobody = own_view(author="octo-dev")
        for name, view, state, lane in (
                ("no request, no review", nobody, "not_reviewed", "needs"),
                ("the user's review", own_view(author="octo-dev", reviews=[("octo-me", "APPROVED")]), "reviewed",
                 "done"),
                ("merged", own_view(author="octo-dev", state="MERGED"), "merged", "done")):
            with self.subTest(name):
                cid = self.store().add(base, by="test")["id"]
                out = self.sync(cid, view)
                self.assertEqual((out["state"], out["lane"]), (state, lane))
                self.assertEqual(self.store().get(cid)["lane"], lane)


    def test_a_card_whose_link_names_the_pr_or_is_not_text_can_lose_its_request(self):
        """Only a link that is text naming no `/<repo>/pull/<number>` holds back request_removed: a card with no
        link, a non-text one, or one naming this PR on another scheme, host or path still closes."""
        base = {k: v for k, v in self.card().items() if k not in ("id", "created_at", "updated_at", "audit")}
        nobody = own_view(author="octo-dev")
        for name, link in (("no link", None), ("a non-text link", 7),
                           ("http on another host", f"http://ghe.example.com/{REPO}/pull/42"),
                           ("a path before the repo", f"https://corp.example.com/github/{REPO}/pull/42")):
            with self.subTest(name):
                card = dict(base, source=dict(base["source"]))
                if link is None:
                    card["source"].pop("link", None)
                else:
                    card["source"]["link"] = link
                cid = self.store().add(card, by="test")["id"]
                out = self.sync(cid, nobody)
                self.assertEqual((out["state"], out["lane"]), ("request_removed", "done"))
                self.assertEqual(self.store().get(cid)["audit"][-1]["did"], "the review request was removed")

    def test_a_link_that_only_looks_like_this_pr_holds_back_request_removed(self):
        """A link names this PR only as `/<repo>/pull/<number>` exactly: a longer number, a repo whose `.` is
        another character, or a longer owner is another page, so the card keeps waiting (not_reviewed)."""
        base = {k: v for k, v in self.card().items() if k not in ("id", "created_at", "updated_at", "audit", "pr")}
        views = fixture("pr-view.json")
        nobody = own_view(author="octo-dev")
        for name, key, link in (
                ("a longer number", KEY, f"https://github.com/{REPO}/pull/420"),
                ("a dot in the repo is not any character", "acme/my.repo#3", "https://github.com/acme/myXrepo/pull/3"),
                ("a longer owner", KEY, f"https://github.com/my{REPO}/pull/42")):
            with self.subTest(name):
                repo, number = key.split("#")
                card = dict(base, key=key, source=dict(base["source"], link=link))
                cid = self.store().add(card, by="test")["id"]
                run = Replay([{"argv": ["pr", "view", number, "--repo", repo, "--json", GH_SYNC_FIELDS],
                               "stdout": nobody},
                              {"argv": ["api", "user", "--jq", ".login"], "stdout": views["user"]}])
                out = prreview.sync(self.data, cid, cfg=self.cfg, run=run)
                self.assertEqual((out["state"], out["lane"]), ("not_reviewed", "needs"))
                self.assertEqual(self.store().get(cid)["lane"], "needs")

def own_view(head=A, state="OPEN", reviews=(), requests=(), author="octo-me"):
    """A pr view reply for a PR whose author is `author` (the recorded user is octo-me)."""
    return {"state": state, "headRefOid": head, "author": {"login": author} if isinstance(author, str) else author,
            "reviewRequests": [{"__typename": "User", "login": r} for r in requests],
            "latestReviews": [{"author": {"login": who}, "state": how, "submittedAt": "2026-11-11T10:00:00Z"}
                              for who, how in reviews]}


class OwnPrSyncTest(Case):
    """The user's own PR (its author is the `gh api user` login): it is never in its own reviewRequests and a
    comment the user leaves shows in latestReviews, so only merged and closed end it."""

    sync = SyncTest.sync

    def own_card(self, reviewed=None):
        """A fresh github-my-prs card for PR 42 (the store does not hold keys unique, so each row adds one)."""
        if not hasattr(self, "base"):
            self.base = {k: v for k, v in self.card("github-my-prs").items()
                         if k not in ("id", "created_at", "updated_at", "audit", "pr")}
        cid = self.store().add(self.base, by="test")["id"]
        if reviewed:
            self.store().update(cid, {"pr": {"repo": REPO, "number": 42, "reviewed_sha": reviewed}}, by="test")
        return cid

    def test_an_own_pr_with_no_review_request_stays_open(self):
        cid = self.own_card()
        out = self.sync(cid, own_view())
        self.assertEqual((out["state"], out["lane"]), ("not_reviewed", "needs"))
        card = self.store().get(cid)
        self.assertEqual((card["lane"], card["pr"]["head_sha"], card["audit"][-1]["did"]),
                         ("needs", A, "sync: not_reviewed"))

    def test_an_own_pr_with_the_users_own_comment_is_not_reviewed(self):
        mine = [("octo-me", "COMMENTED")]
        for view, reviewed, state in ((own_view(reviews=mine), None, "not_reviewed"),
                                      (own_view(head=B, reviews=mine), A, "new_commits"),
                                      (own_view(reviews=mine), A, "current"),
                                      (own_view(reviews=mine + [("octo-ops", "APPROVED")]), A, "current"),
                                      (own_view(reviews=[("octo-me", "APPROVED")], requests=["octo-me"]), A,
                                       "current")):
            with self.subTest(state=state, reviews=view["latestReviews"]):
                cid = self.own_card(reviewed)
                out = self.sync(cid, view)
                self.assertEqual((out["state"], out["lane"]), (state, "needs"))
                self.assertEqual(self.store().get(cid)["lane"], "needs")

    def test_an_own_pr_ends_when_merged_or_closed(self):
        for state, want, did in (("MERGED", "merged", "the PR was merged"), ("CLOSED", "closed", "the PR was closed")):
            with self.subTest(state):
                cid = self.own_card(A)
                out = self.sync(cid, own_view(state=state, reviews=[("octo-me", "COMMENTED")]))
                self.assertEqual((out["state"], out["lane"]), (want, "done"))
                card = self.store().get(cid)
                self.assertEqual((card["lane"], card["audit"][-1]["did"]), ("done", did))

    def test_the_author_not_the_feed_decides_in_sync(self):
        """A card from github-review-requests whose PR the user wrote is still the user's own PR in sync."""
        base = {k: v for k, v in self.card().items() if k not in ("id", "created_at", "updated_at", "audit")}
        cid = self.store().add(base, by="test")["id"]
        self.assertEqual(self.sync(cid, own_view())["state"], "not_reviewed")

    def test_a_reply_with_no_author_is_not_an_own_pr(self):
        """An older recorded reply (no author field), a null author (a deleted account) or another author:
        sync works as before, so a PR with no review request and the user's review ends."""
        no_field = {k: v for k, v in own_view().items() if k != "author"}
        for name, view, state, did in (
                ("no author field", no_field, "request_removed", "the review request was removed"),
                ("a null author", own_view(author=None), "request_removed", "the review request was removed"),
                ("a login of another", own_view(author="octo-dev"), "request_removed",
                 "the review request was removed"),
                ("an author with no login", own_view(author={}), "request_removed", "the review request was removed"),
                ("an author that is text", own_view(author="octo-me") | {"author": "octo-me"}, "request_removed",
                 "the review request was removed"),
                ("the user's comment, null author", own_view(author=None, reviews=[("octo-me", "COMMENTED")]),
                 "reviewed", "you reviewed it on GitHub")):
            with self.subTest(name):
                cid = self.own_card(A)
                out = self.sync(cid, view)
                self.assertEqual((out["state"], out["lane"]), (state, "done"))
                self.assertEqual(self.store().get(cid)["audit"][-1]["did"], did)


class RecordedCallsTest(unittest.TestCase):
    """What prreview asks GitHub is pinned as text, so a dropped field or a changed filter fails here."""

    def test_the_gh_calls_are_the_recorded_text(self):
        self.assertEqual(prreview.PR_FILES, GH_PR_FILES)
        self.assertEqual(prreview.SINCE_JQ, GH_SINCE_JQ)
        self.assertEqual(prreview.SYNC_FIELDS, GH_SYNC_FIELDS)

    @unittest.skipUnless(shutil.which("jq"), "jq is not on PATH")
    def test_the_since_filter_lists_only_merge_commits(self):
        merge, plain = "e5f6a7b8" * 5, "f6a7b8c9" * 5
        commits = [{"sha": plain, "parents": [{"sha": A}]}, {"sha": merge, "parents": [{"sha": plain}, {"sha": BASE}]}]
        for total, more in ((2, False), (100, True), (300, True)):
            with self.subTest(total_commits=total):
                # GitHub's compare lists at most 250 commits but counts them all in total_commits.
                reply = {"status": "ahead", "ahead_by": total, "total_commits": total, "commits": commits}
                out = subprocess.run([shutil.which("jq"), "-c", GH_SINCE_JQ], input=json.dumps(reply), text=True,
                                     capture_output=True, timeout=30, check=True).stdout
                self.assertEqual(json.loads(out), {"status": "ahead", "merges": [merge], "more": more})
        with self.subTest("a reply without total_commits gives more: null, which whole_pr_reason refuses"):
            reply = {"status": "ahead", "ahead_by": 2, "commits": commits}
            out = subprocess.run([shutil.which("jq"), "-c", GH_SINCE_JQ], input=json.dumps(reply), text=True,
                                 capture_output=True, timeout=30, check=True).stdout
            self.assertEqual(json.loads(out), {"status": "ahead", "merges": [merge], "more": None})


# --- criterion 8: nothing is posted ------------------------------------------------------------------------------

class NothingPostedTest(unittest.TestCase):
    REFUSED = (
        ["pr", "review", "42", "--approve"],
        ["pr", "review", "42", "--request-changes", "-b", "x"],
        ["pr", "comment", "42", "--body", "x"],
        ["pr", "merge", "42"],
        ["pr", "close", "42"],
        ["issue", "comment", "1", "--body", "x"],
        ["api", "-X", "POST", "repos/o/r/issues/1/comments"],
        ["api", "-XPOST", "repos/o/r/issues/1/comments"],
        ["api", "--method", "PATCH", "repos/o/r/pulls/1"],
        ["api", "--method=DELETE", "repos/o/r/pulls/1"],
        ["api", "repos/o/r/issues/1/comments", "-f", "body=x"],
        ["api", "repos/o/r/issues/1/comments", "-fbody=x"],
        ["api", "repos/o/r/issues/1/comments", "-F", "body=x"],
        ["api", "repos/o/r/issues/1/comments", "--field", "body=x"],
        ["api", "repos/o/r/issues/1/comments", "--raw-field=body=x"],
        ["api", "--input", "f", "repos/o/r/issues/1/comments"],
        ["api", "graphql", "-f", "query=mutation { addComment(input: {}) { clientMutationId } }"],
        ["api", "graphql", "-f", "query=query { viewer { login } } mutation x { y }"],
        ["api", "graphql", "-F", "query=@q.graphql"],
        ["api", "graphql", "--input", "q.json"],
        # pflag reads `-f=VALUE` as VALUE: the `=` must not hide a mutation's `query` key.
        ["api", "graphql", "-f", "query=query { viewer { login } }",
         "-f=query=mutation { addComment(input: {}) { clientMutationId } }"],
        ["api", "graphql", "-f", "query=query { a }", "-if=query=mutation { x }"],
        ["api", "graphql", "-F", "query=query { a }", "-F=query=mutation { x }"],
        ["api", "graphql", "-f", "query=query { a }", "-F=query=@q.graphql"],
        # pflag reads `-i=t` as -i with the value t: the argument ends there, so the next one is checked too.
        ["api", "repos/o/r/issues/1/comments", "-i=t", "-fbody=hello"],
        ["api", "-i=t", "-XPOST", "repos/o/r/issues/1/comments"],
        ["api", "graphql", "-i=t", "--input=mut.json", "-f", "query={viewer{login}}"],
        ["api", "-i=f", "--input=x", "repos/o/r/issues/1/comments"],
        # gh fills {owner}, {repo} and {branch} in a typed field, so a query must be a raw field.
        ["api", "graphql", "-F", 'query={branch}{addStar(input:{starrableId:"R_x"}){clientMutationId}}'],
        ["api", "repos/o/r/pulls/1/reviews", "--input=body.json"],
        ["api", "repos/o/r/issues/1/comments", "--field=body=x"],
        ["api", "graphql", "-f", "query=query { a }", "--raw-field=query=mutation { x }"],
        ["api", "graphql", "--field", "query=@q.graphql"],
        ["api", "graphql", "--field", 'query={branch}{addStar(input:{starrableId:"R_x"}){clientMutationId}}'],
        # A short value flag with its value attached takes no next argument, and a long one with `=` neither.
        ["api", "-HAccept: x", "-XPOST", "repos/o/r/issues/1/comments"],
        ["api", "--jq=.id", "-XPOST", "repos/o/r/issues/1/comments"],
        # --include takes no value, so the next argument is still checked.
        ["api", "--include", "-XPOST", "repos/o/r/issues/1/comments"],
        ["--repo", "o/r", "pr", "merge", "1"],
        [],
    )
    WHY = (r"may write|a field makes gh api POST|refusing a mutation|cannot be checked|must be a raw field"
           r"|only pr view|a list of text arguments")
    READS = (
        ["pr", "view", "42", "--repo", "o/r", "--json", "state"],
        ["pr", "diff", "42", "--repo", "o/r"],
        ["pr", "checks", "42", "--repo", "o/r"],
        ["api", "graphql", "-f", "query=" + GH_PR_FILES, "-f", "owner=o", "-F", "n=1"],
        ["api", "repos/o/r/compare/a...b", "--jq", ".merge_base_commit.sha"],
        ["api", "-H", "Accept: application/vnd.github.diff", "repos/o/r/compare/a...b"],
        ["api", "user", "--jq", ".login"],
    )

    def test_writes_are_refused_before_any_process(self):
        def boom(*a, **kw):
            raise AssertionError("a process was started")

        with mock.patch("subprocess.run", boom), mock.patch("subprocess.Popen", boom):
            for args in self.REFUSED:
                with self.subTest(args):
                    # The reason names the write, not a parse slip like "one path only" or "no path".
                    with self.assertRaisesRegex(prreview.PostRefused, self.WHY):
                        prreview.gh_read(args)

    def test_reads_pass(self):
        for args in self.READS:
            with self.subTest(args):
                run = Replay([{"argv": args, "stdout": "ok"}])
                self.assertIsNone(prreview.refusal(args))
                self.assertEqual(prreview.gh_read(args, run), "ok")

    def test_the_default_runner_reads_gh_quietly(self):
        seen = {}

        def fake(argv, **kw):
            seen.update(argv=argv, **kw)
            return subprocess.CompletedProcess(argv, 0, "octo-me\n", "")

        with mock.patch("subprocess.run", fake):
            self.assertEqual(prreview.gh_read(["api", "user", "--jq", ".login"]), "octo-me\n")
        self.assertEqual(seen["argv"], ["gh", "api", "user", "--jq", ".login"])
        self.assertEqual(seen["env"]["GH_PROMPT_DISABLED"], "1")
        self.assertEqual(seen["timeout"], 60)

    def test_a_failure_names_the_call_and_the_first_stderr_line(self):
        run = Replay([{"argv": ["pr", "view", "1"], "exit": 1, "stderr": "\nGraphQL: Could not resolve\nmore\n"}])
        with self.assertRaisesRegex(prreview.ReviewError, r"^gh pr view 1: GraphQL: Could not resolve$"):
            prreview.gh_read(["pr", "view", "1"], run)
        self.assertTrue(issubclass(prreview.PostRefused, prreview.ReviewError))
        self.assertTrue(issubclass(prreview.ReviewError, prreview.ConfigError))


if __name__ == "__main__":
    unittest.main()
