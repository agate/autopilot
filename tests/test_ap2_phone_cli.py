"""`ap2 phone build [--data PATH] [--out PATH]`, run as a subprocess against a temp data folder."""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT
from tests.test_ap2_cli import temp_inside_the_accounts_claude, use_temp_home
from tests.test_ap2_phone import FIELDS, tree

from assistant import cards, cli, datadir
from assistant.config import Config
from assistant.store import Store

AP2 = ROOT / "bin" / "ap2"


class Ap2PhoneCommandTest(unittest.TestCase):
    def setUp(self):
        if temp_inside_the_accounts_claude():
            self.skipTest("the temp folder is inside the account's ~/.claude, so ap2 refuses every temp data folder")
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-phone-cli-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.data = self.tmp / "data"

    def run_ap2(self, *args):
        env = {k: v for k, v in os.environ.items() if k not in ("AP2_DATA", "LC_ALL")}
        # HOME points at an empty folder, so the real home is never reached; PYTHON_COLORS=0 keeps Python 3.14's
        # argparse from coloring its usage and errors.
        env.update(HOME=str(self.home), AP2_DATA=str(self.data), PYTHON_COLORS="0")
        return subprocess.run([sys.executable, str(AP2), *args], capture_output=True, env=env, timeout=30)

    def add(self, **over) -> dict:
        card = {"kind": "errand", "lane": "needs", "feed": "chat", "links": [], "pri": "P2", "origin": "source",
                "plain": {line: {"en": f"{line} — café"} for line in cards.PLAIN_LINES}, "title": "Café order"}
        card.update(over)
        return Store(datadir.ensure(self.data), cfg=Config()).add(card, by="test")

    def test_prints_the_phone_data_as_json(self):
        card = self.add()
        r = self.run_ap2("phone", "build")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, b"")
        out = json.loads(r.stdout.decode("utf-8"))
        self.assertEqual(set(out), FIELDS)
        self.assertEqual([row["id"] for row in out["cards"]], [card["id"]])
        self.assertEqual(out["mode"], "shadow")
        self.assertIn("Café order".encode("utf-8"), r.stdout)  # UTF-8, not \\u escapes
        self.assertEqual(r.stdout.decode("utf-8"), json.dumps(out, sort_keys=True, ensure_ascii=False) + "\n")

    def test_sets_up_the_data_folder_like_serve(self):
        r = self.run_ap2("phone", "build")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.data / "config.yaml").is_file())
        self.assertTrue((self.data / "items").is_dir())
        self.assertEqual(json.loads(r.stdout)["cards"], [])

    def test_data_flag(self):
        other = self.tmp / "other"
        r = self.run_ap2("phone", "build", "--data", str(other))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((other / "config.yaml").is_file())
        self.assertFalse(self.data.exists())

    def test_out_writes_the_same_bytes(self):
        self.add()
        shown = self.run_ap2("phone", "build")
        out = self.tmp / "phone.json"
        r = self.run_ap2("phone", "build", "--out", str(out))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, b"")
        # The two runs read the clock separately and may straddle a second, so mask the one clock field first.
        clock = re.compile(rb'"generated_at": "[^"]*"')
        written = out.read_bytes()
        self.assertEqual(len(clock.findall(shown.stdout)), 1, shown.stdout)
        self.assertEqual(len(clock.findall(written)), 1, written)
        self.assertEqual(clock.sub(b'"generated_at": "X"', written), clock.sub(b'"generated_at": "X"', shown.stdout))
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["data", "home", "phone.json"])  # no temp left

    def test_out_replaces_an_old_file_and_may_sit_in_the_data_folder(self):
        self.add()
        out = self.data / "phone.json"
        out.write_text("old", encoding="utf-8")
        r = self.run_ap2("phone", "build", "--out", str(out))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(set(json.loads(out.read_text(encoding="utf-8"))), FIELDS)
        self.assertEqual([p.name for p in self.data.iterdir() if "phone" in p.name], ["phone.json"])

    def test_out_in_a_missing_folder_is_one_line_and_exit_1(self):
        r = self.run_ap2("phone", "build", "--out", str(self.tmp / "nowhere" / "phone.json"))
        self.assertEqual(r.returncode, 1)
        err = r.stderr.decode("utf-8")
        self.assertTrue(err.startswith("ap2: "), err)
        self.assertEqual(len(err.splitlines()), 1, err)
        self.assertIn("nowhere", err)
        self.assertEqual(r.stdout, b"")

    def test_out_that_is_a_folder_is_one_line_and_exit_1(self):
        r = self.run_ap2("phone", "build", "--out", str(self.tmp))
        self.assertEqual(r.returncode, 1)
        err = r.stderr.decode("utf-8")
        self.assertTrue(err.startswith("ap2: "), err)
        self.assertEqual(len(err.splitlines()), 1, err)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["data", "home"])  # no temp left

    def test_a_damaged_card_is_one_line_and_exit_1(self):
        card = self.add()
        Store(self.data, cfg=Config()).path(card["id"]).write_text("{oops", encoding="utf-8")
        r = self.run_ap2("phone", "build")
        self.assertEqual(r.returncode, 1)
        err = r.stderr.decode("utf-8")
        self.assertTrue(err.startswith("ap2: "), err)
        self.assertEqual(len(err.splitlines()), 1, err)
        self.assertIn(card["id"], err)
        self.assertEqual(r.stdout, b"")

    def test_a_bad_config_is_one_line_and_exit_1(self):
        datadir.ensure(self.data)
        (self.data / "config.yaml").write_text("phone_history_days: -2\n", encoding="utf-8")
        r = self.run_ap2("phone", "build")
        self.assertEqual(r.returncode, 1)
        err = r.stderr.decode("utf-8")
        self.assertEqual(len(err.splitlines()), 1, err)
        self.assertIn("line 1:", err)
        self.assertIn("phone_history_days", err)

    def test_a_stdout_build_leaves_the_data_folder_byte_for_byte(self):
        self.add()
        self.add(lane="done")
        for name in (".store.lock", "index.sqlite", ".index.stale"):
            if (self.data / name).exists():
                (self.data / name).unlink()
        before = tree(self.data)
        r = self.run_ap2("phone", "build")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(tree(self.data), before)
        self.assertEqual(tree(self.home), {})

    def test_there_is_no_publish_option_and_the_help_says_so(self):
        r = self.run_ap2("phone", "build", "--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        text = r.stdout.decode("utf-8")
        self.assertIn("--out", text)
        self.assertNotIn("--publish", text)
        self.assertIn("dispatcher", text)
        self.assertIn("publish", text.lower())
        r = self.run_ap2("phone", "build", "--publish")
        self.assertEqual(r.returncode, 2)
        self.assertIn("phone", self.run_ap2("--help").stdout.decode("utf-8"))


class Ap2PhoneOutAtomicTest(unittest.TestCase):
    """In-process, so the rename can be made to fail: `--out` writes a temp file next to the target and renames
    it over the target, so a failed write leaves the old file whole and no temp file behind."""

    def setUp(self):
        use_temp_home(self)
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-phone-cli-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_failed_rename_keeps_the_old_file_and_leaves_no_temp_file(self):
        data = datadir.ensure(self.tmp / "data")  # set up first, so only the --out rename is made to fail
        out = self.tmp / "phone.json"
        out.write_text("old", encoding="utf-8")
        real_replace = os.replace
        renamed = []

        def replace(src, dst, *args, **kwargs):
            if Path(dst) == out:
                renamed.append(Path(src))
                raise OSError("disk full")
            return real_replace(src, dst, *args, **kwargs)

        err = io.StringIO()
        with mock.patch("os.replace", side_effect=replace), mock.patch.object(sys, "stderr", err):
            code = cli.main(["phone", "build", "--data", str(data), "--out", str(out)])
        self.assertEqual(code, 1)
        lines = err.getvalue().splitlines()
        self.assertEqual(len(lines), 1, err.getvalue())
        self.assertTrue(lines[0].startswith("ap2:"), lines[0])
        self.assertIn("disk full", lines[0])
        self.assertEqual(out.read_text(encoding="utf-8"), "old")
        self.assertEqual(len(renamed), 1)
        self.assertEqual(renamed[0].parent, self.tmp)  # the temp file was next to the target
        self.assertEqual(sorted(self.tmp.glob(".phone.json.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
