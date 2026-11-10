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

    def run_ap2(self, *args, data=None, env_extra=None, env_drop=(), encoding=None):
        env = {k: v for k, v in os.environ.items() if k != "AP2_DATA" and k not in env_drop}
        env["HOME"] = str(self.home)  # the real home folder is never reached
        env["AP2_DATA"] = str(self.data if data is None else data)
        env.update(env_extra or {})
        return subprocess.run([sys.executable, str(AP2), *args], capture_output=True, text=True, env=env,
                              encoding=encoding, errors="replace", timeout=30)

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

    @unittest.skipIf(os.geteuid() == 0, "root ignores folder permissions")
    def test_unreadable_data_folder_exits_1_without_a_traceback(self):
        np = self.tmp / "np"
        (np / "inner").mkdir(parents=True)
        np.chmod(0)
        self.addCleanup(np.chmod, 0o755)
        r = self.run_ap2("config", "--data", str(np / "inner" / "x"))
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, "")
        self.assertIn("ap2:", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def assert_non_ascii_data_prints_parseable_json(self, env_extra):
        target = self.tmp / "\u52a9\u624b"
        r = self.run_ap2("config", "--data", str(target), env_extra=env_extra,
                         env_drop=("PYTHONUTF8", "PYTHONIOENCODING"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(json.loads(r.stdout)["data_dir"], str(target))

    def test_non_ascii_data_folder_under_latin1_output_prints_parseable_json(self):
        self.assert_non_ascii_data_prints_parseable_json({"PYTHONIOENCODING": "latin-1"})

    def test_non_ascii_data_folder_under_latin1_locale_prints_parseable_json(self):
        try:
            names = subprocess.run(["locale", "-a"], capture_output=True, text=True, errors="replace",
                                   timeout=30).stdout.split()
        except OSError:
            names = []
        # an en_US Latin-1 locale; macOS and glibc spell its name differently, so compare letters and digits only
        latin1 = [n for n in names
                  if n.startswith("en_US.") and "".join(filter(str.isalnum, n)).lower().endswith("iso88591")]
        if not latin1:
            self.skipTest("no en_US Latin-1 locale is installed")
        self.assert_non_ascii_data_prints_parseable_json({"LC_ALL": latin1[0]})

    def test_non_ascii_data_folder_under_utf8_output_prints_the_name_as_is(self):
        target = self.tmp / "\u52a9\u624b"
        r = self.run_ap2("config", "--data", str(target), env_extra={"PYTHONIOENCODING": "utf-8"}, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("\u52a9\u624b", r.stdout)
        self.assertNotIn("\\u52a9", r.stdout)
        self.assertEqual(json.loads(r.stdout)["data_dir"], str(target))

    def test_config_with_stdout_closed_exits_0_without_a_traceback(self):
        env = {k: v for k, v in os.environ.items() if k != "AP2_DATA"}
        env.update(HOME=str(self.home), AP2_DATA=str(self.data))
        # '>&-' closes stdout, so Python starts with sys.stdout set to None
        r = subprocess.run(["sh", "-c", '"$0" "$1" config >&-', sys.executable, str(AP2)], stdin=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, text=True, env=env, errors="replace", timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue((self.data / "config.yaml").is_file())

    def test_non_ascii_error_under_latin1_output_has_no_traceback(self):
        r = self.run_ap2("config", data=self.home / ".claude" / "\u52a9\u624b",
                         env_extra={"PYTHONIOENCODING": "latin-1"}, env_drop=("PYTHONUTF8",))
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, "")
        self.assertIn("ap2:", r.stderr)
        self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
