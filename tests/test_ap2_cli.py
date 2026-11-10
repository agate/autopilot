from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import ROOT

AP2 = ROOT / "bin" / "ap2"


class Ap2CommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-cli-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.data = self.tmp / "data"

    def run_ap2(self, *args, data=None):
        env = {k: v for k, v in os.environ.items() if k != "AP2_DATA"}
        env["HOME"] = str(self.home)  # the real home folder is never reached
        env["AP2_DATA"] = str(self.data if data is None else data)
        return subprocess.run([sys.executable, str(AP2), *args], capture_output=True, text=True, env=env,
                              timeout=30)

    def test_bin_is_executable(self):
        self.assertTrue(os.access(AP2, os.X_OK))

    def test_config_prints_effective_config(self):
        r = self.run_ap2("config")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out, {"data_dir": str(self.data), "keep_transcripts": "always", "mode": "shadow",
                               "primary_language": "en", "secondary_language": None})
        self.assertEqual(list(out), sorted(out))
        self.assertTrue((self.data / "config.yaml").is_file())
        self.assertTrue((self.data / "items").is_dir())
        self.assertFalse((self.home / "Documents").exists())

    def test_config_reads_the_file(self):
        self.data.mkdir()
        (self.data / "config.yaml").write_text("secondary_language: zh\nkeep_transcripts: 90d\n")
        out = json.loads(self.run_ap2("config").stdout)
        self.assertEqual((out["secondary_language"], out["keep_transcripts"]), ("zh", "90d"))

    def test_data_flag_wins_over_env(self):
        other = self.tmp / "other"
        r = self.run_ap2("config", "--data", str(other))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["data_dir"], str(other))
        self.assertTrue((other / "config.yaml").is_file())
        self.assertFalse(self.data.exists())

    def test_no_args_prints_usage_and_exits_2(self):
        r = self.run_ap2()
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage", r.stderr.lower())
        self.assertIn("config", r.stderr)

    def test_invalid_config_exits_1_with_error_on_stderr(self):
        self.data.mkdir()
        (self.data / "config.yaml").write_text("mode: loud\n")
        r = self.run_ap2("config")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, "")
        self.assertIn("mode", r.stderr)
        self.assertIn("line 1:", r.stderr)

    def test_data_inside_dot_claude_exits_1(self):
        r = self.run_ap2("config", data=self.home / ".claude" / "assistant")
        self.assertEqual(r.returncode, 1)
        self.assertIn(".claude", r.stderr)
        self.assertFalse((self.home / ".claude").exists())


if __name__ == "__main__":
    unittest.main()
