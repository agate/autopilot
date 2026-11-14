"""The commands the dispatcher pass runs (`ap2 pass status|end`, `ap2 card ask|lane`) and the v2 runbook, as
subprocesses against a temp data folder."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import ROOT
from tests.test_ap2_cli import temp_inside_the_accounts_claude
from tests.test_ap2_store import card
from assistant import dispatch
from assistant.importv1 import FEED as V1_FEED

AP2 = ROOT / "bin" / "ap2"
RUNBOOK = ROOT / "lib" / "assistant" / "builtin" / "RUNBOOK.md"


class Ap2Case(unittest.TestCase):
    def setUp(self):
        if temp_inside_the_accounts_claude():
            self.skipTest("the temp folder is inside the account's ~/.claude, so ap2 refuses every temp data folder")
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-dispatch-cli-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.data = self.tmp / "data"

    def run_ap2(self, *args, extra=None):
        env = {k: v for k, v in os.environ.items() if k not in ("AP2_DATA", "AP2_PASS", "LC_ALL")}
        env.update(HOME=str(self.home), AP2_DATA=str(self.data), PYTHONIOENCODING="utf-8", PYTHON_COLORS="0")
        env.update(extra or {})
        return subprocess.run([sys.executable, str(AP2), *args], capture_output=True, text=True, env=env,
                              encoding="utf-8", errors="replace", timeout=30)

    def ok(self, *args):
        r = self.run_ap2(*args)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        return json.loads(r.stdout)

    def fails(self, *args, code=1, extra=None) -> str:
        r = self.run_ap2(*args, extra=extra)
        self.assertEqual(r.returncode, code, r.stdout + r.stderr)
        self.assertEqual(r.stdout, "")
        if code == 1:
            self.assertEqual(len(r.stderr.splitlines()), 1, r.stderr)
            self.assertTrue(r.stderr.startswith("ap2: "), r.stderr)
        return r.stderr

    def add(self, **over) -> str:
        f = self.tmp / "card.json"
        f.write_text(json.dumps(card(**over)), encoding="utf-8")
        return self.ok("card", "add", str(f))["id"]

    def text(self, words="Please log in to the tracker again.") -> str:
        f = self.tmp / "ask.txt"
        f.write_text(words, encoding="utf-8")
        return str(f)


class PassCommandTest(Ap2Case):
    def test_status_prints_both_files_or_null(self):
        self.assertEqual(self.ok("pass", "status"), {"pass": None, "dispatch": None, "mode": "shadow"})
        (self.data / "pass.json").write_text(json.dumps({"due": True, "why": []}))
        rec = {"group": None, "projects": [], "short_id": "s0001", "session_id": "", "profile": "subscription",
               "started_at": "2026-11-10T14:00:00Z", "queue": []}
        (self.data / dispatch.DISPATCH).write_text(json.dumps(rec))
        out = self.ok("pass", "status")
        self.assertEqual(out["pass"], {"due": True, "why": []})
        self.assertEqual(out["dispatch"], rec)

    def test_status_of_a_damaged_file_is_one_line(self):
        self.ok("config")
        (self.data / "pass.json").write_text("{")
        self.assertIn("pass.json: cannot read", self.fails("pass", "status"))

    def test_end_marks_the_open_pass_ended(self):
        self.assertEqual(self.fails("pass", "end"), "ap2: no dispatcher pass is open\n")
        self.ok("config")
        (self.data / dispatch.DISPATCH).write_text(json.dumps(
            {"group": None, "projects": [], "short_id": "s0007", "session_id": "", "profile": "subscription",
             "started_at": "2026-11-10T14:00:00Z", "queue": []}))
        out = self.ok("pass", "end")
        self.assertEqual((out["short_id"], out["started_at"]), ("s0007", "2026-11-10T14:00:00Z"))
        self.assertEqual(json.loads((self.data / dispatch.END).read_text()), out)

    def test_end_from_a_session_that_is_not_the_open_pass_ends_nothing(self):
        self.ok("config")
        rec = {"group": None, "projects": [], "short_id": "s0007", "session_id": "", "profile": "subscription",
               "started_at": "2026-11-10T14:00:00Z", "queue": [], "why": [], "token": "ab" * 8}
        (self.data / dispatch.DISPATCH).write_text(json.dumps(rec))
        for other in ("cd" * 8, "x"):
            with self.subTest(token=other):
                line = self.fails("pass", "end", extra={"AP2_PASS": other})
                self.assertEqual(line, "ap2: this session is not the open dispatcher pass (s0007); it ends nothing\n")
                self.assertFalse((self.data / dispatch.END).exists())
        r = self.run_ap2("pass", "end", extra={"AP2_PASS": "ab" * 8})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["token"], "ab" * 8)
        self.assertEqual(json.loads((self.data / dispatch.END).read_text())["token"], "ab" * 8)

    def test_a_bad_pass_command_is_a_usage_error(self):
        self.fails("pass", "start", code=2)


class CardAskTest(Ap2Case):
    def test_ask_sets_the_ask_from_dispatch_and_moves_the_card_to_needs(self):
        cid = self.add(lane="planned")
        out = self.ok("card", "ask", cid, "--type", "confirm", "--text-file", self.text("Go ahead with the plan?\n"))
        self.assertEqual(out["ask"], {"type": "confirm", "from": "dispatch", "text": "Go ahead with the plan?"})
        self.assertEqual(out["lane"], "needs")
        self.assertEqual(out["audit"][-1]["did"], "ask confirm from dispatch")
        self.assertEqual(self.ok("card", "show", cid)["ask"]["type"], "confirm")

    def test_an_open_ask_is_left_as_it_is(self):
        cid = self.add(ask={"type": "answer", "from": "worker"})
        line = self.fails("card", "ask", cid, "--type", "fix", "--text-file", self.text())
        self.assertIn("already has an open ask (answer from worker)", line)
        self.assertEqual(self.ok("card", "show", cid)["ask"], {"type": "answer", "from": "worker"})

    def test_a_v1_copy_is_refused_and_left_as_it_is(self):
        v1 = self.add(feed=V1_FEED)
        before = self.ok("card", "show", v1)
        line = self.fails("card", "ask", v1, "--type", "fix", "--text-file", self.text())
        self.assertIn("imported v1 copy", line)
        self.assertEqual(self.ok("card", "show", v1), before)

    def test_a_bad_card_id_type_or_text_is_refused(self):
        self.assertIn("not a card id", self.fails("card", "ask", "nope", "--type", "fix", "--text-file", self.text()))
        self.assertFalse(self.data.exists())  # refused before the data folder is touched
        cid = self.add()
        self.assertIn("invalid choice", self.fails("card", "ask", cid, "--type", "maybe", "--text-file",
                                                   self.text(), code=2))
        self.assertIn("invalid choice", self.fails("card", "ask", cid, "--type", "fix", "--text-file", self.text(),
                                                   "--from", "someone", code=2))
        self.assertIn("holds no words", self.fails("card", "ask", cid, "--type", "fix", "--text-file",
                                                   self.text("  \n")))
        self.assertIn("cannot read", self.fails("card", "ask", cid, "--type", "fix", "--text-file",
                                                str(self.tmp / "missing.txt")))
        self.assertIn("c-20991231-235959-abcdef", self.fails("card", "ask", "c-20991231-235959-abcdef", "--type",
                                                             "fix", "--text-file", self.text()))
        self.assertIsNone(self.ok("card", "show", cid).get("ask"))


class CardLaneTest(Ap2Case):
    def test_lane_moves_the_card(self):
        cid = self.add(lane="planned")
        out = self.ok("card", "lane", cid, "running")
        self.assertEqual(out["lane"], "running")
        self.assertEqual(out["audit"][-1]["did"], "lane planned -> running")

    def test_a_bad_lane_or_id_and_a_v1_copy_are_refused(self):
        cid = self.add()
        self.assertIn("invalid choice", self.fails("card", "lane", cid, "later", code=2))
        self.assertIn("not a card id", self.fails("card", "lane", "nope", "done"))
        v1 = self.add(feed=V1_FEED)
        self.assertIn("imported v1 copy", self.fails("card", "lane", v1, "done"))
        self.assertEqual(self.ok("card", "show", v1)["lane"], "planned")


# -- criterion 5: the runbook names only ap2 commands that exist ---------------------------------------------------

def runbook_commands() -> list:
    """Each `ap2 <command> [<sub command>]` the runbook names in backticks, as its words."""
    found = []
    for span in re.findall(r"`([^`]+)`", RUNBOOK.read_text(encoding="utf-8")):
        words = span.split()
        if words[:1] != ["ap2"]:
            continue
        verbs = []
        for w in words[1:3]:
            if not re.fullmatch(r"[a-z][a-z0-9-]*", w):
                break
            verbs.append(w)
        if verbs and verbs not in found:
            found.append(verbs)
    return found


class RunbookTest(Ap2Case):
    def test_every_ap2_command_the_runbook_names_parses(self):
        cmds = runbook_commands()
        for needed in (["pass", "status"], ["pass", "end"], ["card", "ask"], ["card", "lane"], ["worker", "start"],
                       ["template", "show"], ["cap"]):
            self.assertIn(needed, cmds)
        for verbs in cmds:
            with self.subTest(command=" ".join(verbs)):
                r = self.run_ap2(*verbs, "--help")
                self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_command_that_does_not_exist_would_fail_the_check(self):
        self.assertNotEqual(self.run_ap2("card", "bogus", "--help").returncode, 0)

    def test_the_runbook_covers_its_steps(self):
        text = " ".join(RUNBOOK.read_text(encoding="utf-8").split())
        for words in ("`ap2 cap --json`", "`mode`", "dispatch.why", "handback.json", "review.json", "prloop.json",
                      "`ap2 template show <kind>`", "`ap2 worker start", "round-robin", "`confirm`", "`approve`",
                      "`answer`", "`fix`", "plain words", "Run `ap2 pass end`", "Shadow mode", "gh pr merge"):
            self.assertIn(words, text)
        self.assertEqual(dispatch.RUNBOOK, RUNBOOK)
        self.assertNotIn("folder above this file", text)
        self.assertNotIn("pass.why", text)  # pass_started empties it as the round starts: the pass reads its record
        self.assertIn("**`prreview.json`.** The gate already applied it (`ap2 worker check`); leave it.", text)
        self.assertIn("the absolute `ap2` path your prompt names", text)


if __name__ == "__main__":
    unittest.main()
