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
from pathlib import Path

from tests.helpers import ROOT
from tests.test_ap2_cli import temp_inside_the_accounts_claude
from tests.test_ap2_store import card

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
        self.fake_home = self.tmp / "fake-claude"
        self.fake_home.mkdir()
        self.log = self.tmp / "claude.log"
        self.wt = self.tmp / "worktree"
        self.wt.mkdir()
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("AP2_DATA", "LC_ALL") and not k.startswith(("AUTOPILOT_", "FAKE_"))}
        self.env.update(HOME=str(self.home), AP2_DATA=str(self.data), PYTHON_COLORS="0", PYTHONIOENCODING="utf-8",
                        PATH=f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
                        FAKE_CLAUDE_HOME=str(self.fake_home), FAKE_CLAUDE_LOG=str(self.log))

    def run_ap2(self, *args, data=None):
        env = dict(self.env)
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
