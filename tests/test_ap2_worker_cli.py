"""`ap2 worker ...` as a subprocess: JSON on stdout, `--data PATH` / `AP2_DATA`, one `ap2: ...` line and exit 1 on
an error. The fake claude (`tests/bin/fake-claude-v2`) is the `claude` on PATH; HOME and the data folder are temp."""
from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.helpers import ROOT
from tests.test_ap2_apistop import refused_line
from tests.test_ap2_cli import temp_inside_the_accounts_claude
from tests.test_ap2_store import card
from tests.test_ap2_workers import usage_file

AP2 = ROOT / "bin" / "ap2"
FAKE = ROOT / "tests" / "bin" / "fake-claude-v2"


class WorkerCommandTest(unittest.TestCase):
    def setUp(self):
        if temp_inside_the_accounts_claude():
            self.skipTest("the temp folder is inside the account's ~/.claude, so ap2 refuses every temp data folder")
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-worker-cli-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.data = self.tmp / "data"
        bindir = self.tmp / "bin"
        bindir.mkdir()
        (bindir / "claude").symlink_to(FAKE)
        # A made-up `ap` with no v1 package next to it, so no test reads the developer's installed v1.
        (bindir / "ap").write_text("#!/bin/sh\nexit 1\n")
        (bindir / "ap").chmod(0o755)
        self.fake_home = self.tmp / "fake-claude"
        self.fake_home.mkdir()
        self.log = self.tmp / "claude.log"
        self.wt = self.tmp / "worktree"
        self.wt.mkdir()
        # A user at a terminal: no AP2_* (a worker's own session carries AP2_ROLE and AP2_CARD).
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("LC_ALL", "GH_CONFIG_DIR", "XDG_CONFIG_HOME")
                    and not k.startswith(("AUTOPILOT_", "FAKE_", "AP2_"))}
        self.env.update(HOME=str(self.home), AP2_DATA=str(self.data), PYTHON_COLORS="0", PYTHONIOENCODING="utf-8",
                        PATH=f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
                        FAKE_CLAUDE_HOME=str(self.fake_home), FAKE_CLAUDE_LOG=str(self.log))

    def run_ap2(self, *args, data=None, env_extra=None):
        env = dict(self.env, **(env_extra or {}))
        if data is not None:
            env["AP2_DATA"] = str(data)
        return subprocess.run([sys.executable, str(AP2), *args], capture_output=True, text=True, env=env,
                              encoding="utf-8", errors="replace", timeout=60)

    def ok(self, *args, **kw):
        r = self.run_ap2(*args, **kw)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        return json.loads(r.stdout)

    def fails(self, *args, **kw):
        r = self.run_ap2(*args, **kw)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(r.stdout, "")
        lines = r.stderr.splitlines()
        self.assertEqual(len(lines), 1, r.stderr)
        self.assertTrue(lines[0].startswith("ap2: "), r.stderr)
        return lines[0]

    def live(self):
        self.ok("config")
        (self.data / "config.yaml").write_text("mode: live\n")

    def add_card(self) -> str:
        f = self.tmp / "card.json"
        f.write_text(json.dumps(card()), encoding="utf-8")
        return self.ok("card", "add", str(f))["id"]

    def calls(self) -> list:
        return [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_start_is_refused_in_shadow_mode(self):
        cid = self.add_card()
        line = self.fails("worker", "start", cid, "implement", "--cwd", str(self.wt))
        self.assertIn("shadow mode: refusing to start a implement worker", line)
        self.assertEqual([c for c in self.calls() if c["argv"][:1] == ["--bg"]], [])
        self.assertEqual(self.ok("worker", "list"), [])

    def test_start_list_check_and_finish(self):
        self.live()
        cid = self.add_card()
        brief = self.tmp / "brief.md"
        brief.write_text("Build it.\n", encoding="utf-8")
        rec = self.ok("worker", "start", cid, "implement", "--cwd", str(self.wt), "--prompt-file", str(brief),
                      "--needs", "native-dev-loop")
        self.assertEqual((rec["short_id"], rec["ports"], rec["needs"]), ("s0001", [7200, 7209], ["native-dev-loop"]))
        bg = [c for c in self.calls() if c["argv"][:1] == ["--bg"]][0]
        self.assertTrue(bg["argv"][-1].startswith("Build it.\n"))
        self.assertEqual(bg["cwd"], str(self.wt))
        other = self.add_card()
        line = self.fails("worker", "start", other, "implement", "--cwd", str(self.wt), "--needs", "native-dev-loop")
        self.assertIn(f"native-dev-loop is held by {cid} · implement", line)
        listed = self.ok("worker", "list")
        self.assertEqual([(w["card"], w["state"]) for w in listed], [(cid, "running")])
        run = self.data / "sessions" / cid
        (run / "handback.json").write_text("{}")
        out = self.ok("worker", "check", "--transcripts", str(self.fake_home / "projects"))
        self.assertEqual([(o["state"], o["did"]) for o in out], [("done", "ended")])
        self.assertTrue(gzip.decompress(Path(out[0]["archive"]).read_bytes()))
        self.assertEqual(self.ok("card", "show", cid)["sessions"]["implement"]["id"], rec["session_id"])
        fin = self.ok("worker", "finish", cid, "--transcripts", str(self.fake_home / "projects"))
        self.assertEqual(fin["removed"], [rec["session_id"]])

    def bg_calls(self) -> list:
        return [c for c in self.calls() if c["argv"][:1] == ["--bg"]]

    def test_list_says_why_a_stopped_worker_stopped(self):
        self.live()
        cid = self.add_card()
        rec = self.ok("worker", "start", cid, "implement", "--cwd", str(self.wt))
        agents = json.loads((self.fake_home / "agents.json").read_text())
        for a in agents:
            a.update(state="blocked", status="idle")
        (self.fake_home / "agents.json").write_text(json.dumps(agents))
        # `worker list` reads the default projects folder, under this test's own HOME.
        folder = self.home / ".claude" / "projects" / "-worktree"
        folder.mkdir(parents=True)
        at = (datetime.now(timezone.utc) + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        (folder / f"{rec['session_id']}.jsonl").write_text(json.dumps(refused_line(at)) + "\n", encoding="utf-8")
        before = (self.data / "workers.json").read_bytes()
        listed = self.ok("worker", "list")
        self.assertEqual([(w["state"], w["api_stop"]) for w in listed], [("idle", "gateway_refused")])
        self.assertEqual((self.data / "workers.json").read_bytes(), before)

    def usage(self, path: Path, percent: int, resets_in_min: int = 180) -> Path:
        """A usage cache fetched now (the command runs on the real clock) with one window at `percent`."""
        now = datetime.now(timezone.utc)
        return usage_file(path, percent, resets_at=(now + timedelta(minutes=resets_in_min)).isoformat(),
                          fetched=now)

    def test_park_reads_the_home_usage_file_or_the_given_one(self):
        self.live()
        cid = self.add_card()
        self.ok("worker", "start", cid, "implement", "--cwd", str(self.wt))
        home_file = self.usage(self.home / ".claude.json", 95)
        self.assertEqual(self.ok("worker", "park", cid, "implement", "--note", "x")["percent"], 95)
        home_file.unlink()
        other = self.usage(self.tmp / "usage.json", 95)
        self.assertEqual(self.ok("worker", "park", cid, "implement", "--note", "x", "--usage", str(other))["percent"],
                         95)

    def parked(self, cid: str):
        return next(w for w in json.loads((self.data / "workers.json").read_text())["workers"]
                    if w["card"] == cid).get("parked")

    def test_a_worker_parks_only_its_own_card_and_role(self):
        # A worker's own session (AP2_ROLE, AP2_CARD) parking another card would hold that card's slot with no ask.
        self.live()
        mine, other = self.add_card(), self.add_card()
        self.ok("worker", "start", mine, "pr-review", "--cwd", str(self.wt))
        self.ok("worker", "start", other, "pr-review", "--cwd", str(self.wt))
        self.usage(self.home / ".claude.json", 95)
        fake = self.usage(self.tmp / "fake.json", 99)
        worker = {"AP2_ROLE": "pr-review", "AP2_CARD": mine}
        for card_id, role in ((other, "pr-review"), (mine, "implement"), (other, "implement")):
            with self.subTest(card=card_id, role=role):
                line = self.fails("worker", "park", card_id, role, "--note", "x", "--usage", str(fake),
                                  env_extra=worker)
                self.assertIn(f"a worker parks only itself ({mine} · pr-review)", line)
        self.assertIsNone(self.parked(other))
        self.assertIsNone(self.parked(mine))
        self.assertEqual(self.ok("worker", "park", mine, "pr-review", "--note", "x", env_extra=worker)["percent"], 95)

    def test_a_worker_parking_itself_reads_the_real_usage_not_its_usage_flag(self):
        self.live()
        cid = self.add_card()
        self.ok("worker", "start", cid, "pr-review", "--cwd", str(self.wt))
        worker = {"AP2_ROLE": "pr-review", "AP2_CARD": cid}
        fake = usage_file(self.tmp / "fake.json", 99, resets_at="2999-01-01T00:00:00+00:00",
                          fetched=datetime.now(timezone.utc))
        self.usage(self.home / ".claude.json", 3)
        self.assertIn("under the park line", self.fails("worker", "park", cid, "pr-review", "--usage", str(fake),
                                                        env_extra=worker))
        self.assertIsNone(self.parked(cid))
        self.usage(self.home / ".claude.json", 95)
        rec = self.ok("worker", "park", cid, "pr-review", "--usage", str(fake), env_extra=worker)
        self.assertEqual(rec["percent"], 95)
        self.assertFalse(rec["resume_at"].startswith("2999"), rec)

    def test_a_terminal_parks_any_card_with_its_usage_flag(self):
        self.live()
        a, b = self.add_card(), self.add_card()
        self.ok("worker", "start", a, "pr-review", "--cwd", str(self.wt))
        self.ok("worker", "start", b, "implement", "--cwd", str(self.wt))
        given = self.usage(self.tmp / "given.json", 97)
        for cid, role in ((a, "pr-review"), (b, "implement")):
            with self.subTest(card=cid):
                self.assertEqual(self.ok("worker", "park", cid, role, "--usage", str(given))["percent"], 97)
                self.assertEqual(self.parked(cid)["percent"], 97)

    def test_answer_resume_and_a_parked_resume_by_check(self):
        self.live()
        cid = self.add_card()
        rec = self.ok("worker", "start", cid, "implement", "--cwd", str(self.wt), "--needs", "native-dev-loop")
        (self.data / "sessions" / cid / "question.json").write_text(json.dumps({"text": "Three retries or five?"}))
        self.assertEqual([o["did"] for o in self.ok("worker", "check")], ["asked"])
        answered = self.ok("worker", "answer", cid, "Three", "retries")
        last = self.bg_calls()[-1]["argv"]
        self.assertEqual(last[1:3], ["--resume", rec["session_id"]])
        self.assertIn("The answer: Three retries", last[-1])
        self.assertEqual(self.held(), [["native-dev-loop"]])  # the resumed worker still holds its resource
        self.ok("worker", "end", cid, "implement")
        again = self.ok("worker", "start", cid, "implement", "--resume")
        self.assertEqual(self.bg_calls()[-1]["argv"][:3], ["--bg", "--resume", answered["session_id"]])
        self.assertEqual(self.held(), [["native-dev-loop"]])
        self.ok("worker", "park", cid, "implement", "--usage", str(self.usage(self.tmp / "u.json", 95, -60)))
        under = self.usage(self.tmp / "under.json", 3)
        self.assertEqual([o["did"] for o in self.ok("worker", "check", "--usage", str(under))], ["resumed"])
        self.assertEqual(self.bg_calls()[-1]["argv"][1:3], ["--resume", again["session_id"]])

    def held(self) -> list:
        return [r["needs"] for r in json.loads((self.data / "workers.json").read_text())["workers"]]

    def test_empty_needs_hold_no_resource(self):
        # v1's runbook passes `--needs ""` on every start: an empty name is no resource, so two such workers never
        # block each other.
        self.live()
        a = self.ok("worker", "start", self.add_card(), "implement", "--cwd", str(self.wt), "--needs", "")
        self.assertEqual(a["needs"], [])
        b = self.ok("worker", "start", self.add_card(), "implement", "--cwd", str(self.wt), "--needs", ",")
        self.assertEqual(b["needs"], [])
        self.assertEqual(self.held(), [[], []])
        self.assertEqual(len(self.bg_calls()), 2)

    def test_errors_are_one_line(self):
        cid = self.add_card()
        self.assertIn(f"no implement worker on card {cid}", self.fails("worker", "end", cid, "implement"))
        self.assertIn("no card", self.fails("worker", "finish", "c-20260101-000000-abcdef"))
        self.assertIn("not a card id", self.fails("worker", "finish", "nope"))
        self.assertIn("shadow mode: refusing to resume", self.fails("worker", "answer", cid, "yes"))
        self.assertIn("under the park line", self.fails("worker", "park", cid, "implement", "--usage",
                                                        str(self.tmp / "missing.json")))
        self.live()
        self.assertIn("no question from a worker", self.fails("worker", "answer", cid, "yes"))
        self.assertIn("cannot read", self.fails("worker", "start", cid, "implement", "--cwd", str(self.wt),
                                                "--prompt-file", str(self.tmp / "missing.md")))

    def test_data_flag_wins_over_env(self):
        other = self.tmp / "other"
        self.assertEqual(self.ok("worker", "list", "--data", str(other)), [])
        self.assertTrue((other / "config.yaml").is_file())
        self.assertFalse(self.data.exists())

    def test_a_bad_role_is_a_usage_error(self):
        r = self.run_ap2("worker", "start", "c-20260101-000000-abcdef", "nosuch")
        self.assertEqual(r.returncode, 2)
        self.assertIn("invalid choice: 'nosuch'", r.stderr)

    def test_worker_is_listed_in_the_help(self):
        r = self.run_ap2("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("worker", r.stdout)


if __name__ == "__main__":
    unittest.main()
