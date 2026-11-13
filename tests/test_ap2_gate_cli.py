"""`ap2 gate [--data PATH] [--now ISO]`, run as a subprocess against a temp data folder (slice 5)."""
from __future__ import annotations

import fcntl
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
from tests.test_ap2_cli import temp_inside_the_accounts_claude

AP2 = ROOT / "bin" / "ap2"


class Ap2GateCommandTest(unittest.TestCase):
    def setUp(self):
        if temp_inside_the_accounts_claude():
            self.skipTest("the temp folder is inside the account's ~/.claude, so ap2 refuses every temp data folder")
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-gate-cli-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.data = self.tmp / "data"

    def run_ap2(self, *args):
        env = {k: v for k, v in os.environ.items() if k not in ("AP2_DATA", "LC_ALL")}
        # HOME points at an empty folder, so the real home is never reached; PYTHON_COLORS=0 keeps Python 3.14's
        # argparse from coloring its usage and errors.
        env.update(HOME=str(self.home), AP2_DATA=str(self.data), PYTHON_COLORS="0", TZ="Pacific/Kiritimati")
        return subprocess.run([sys.executable, str(AP2), *args], capture_output=True, text=True, env=env,
                              errors="replace", timeout=30)

    def put_routine(self, name="digest", **over):
        r = {"title": "Daily digest", "schedule": {"days": "weekdays", "at": ["09:00"]},
             "tz": "America/Los_Angeles", "last_slot": "2025-11-10T09:00"}
        r.update(over)
        (self.data / "routines").mkdir(parents=True, exist_ok=True)
        (self.data / "routines" / f"{name}.json").write_text(json.dumps(r), encoding="utf-8")

    def test_a_run_in_an_empty_folder_prints_the_new_keys(self):
        r = self.run_ap2("gate", "--now", "2025-11-11T09:01:30-08:00")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(sorted(out), ["errors", "last_run_at", "pass_due", "pruned", "reads", "routines",
                                       "watches_due", "workers"])
        self.assertEqual((out["workers"], out["reads"], out["pass_due"], out["errors"]), ([], [], False, []))
        self.assertIs(json.loads((self.data / "pass.json").read_text())["due"], False)

    def test_the_help_says_what_a_run_does(self):
        r = self.run_ap2("gate", "--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        text = " ".join(r.stdout.split())
        for words in ("check every worker", "due watches", "scan session", "pass.json", "starts no dispatcher pass"):
            self.assertIn(words, text)

    def test_gate_prints_the_run_and_writes_the_heartbeat(self):
        self.put_routine()
        r = self.run_ap2("gate", "--now", "2025-11-11T09:01:30-08:00")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        out = json.loads(r.stdout)
        self.assertEqual(out["last_run_at"], "2025-11-11T17:01:30Z")
        self.assertEqual([(f["routine"], f["slot"], f["action"]) for f in out["routines"]],
                         [("digest", "2025-11-11T09:00", "run")])
        self.assertEqual(json.loads((self.data / "gate.json").read_text()), out)
        self.assertTrue((self.data / "gate.log").read_text().startswith("2025-11-11T17:01:30Z "))
        cards = json.loads(self.run_ap2("card", "list", "--kind", "routine").stdout)
        self.assertEqual([c["key"] for c in cards], ["digest@2025-11-11T09:00"])
        again = json.loads(self.run_ap2("gate", "--now", "2025-11-11T17:02:30Z").stdout)
        self.assertEqual(again["routines"], [])

    def test_data_flag_and_no_now_use_the_real_time(self):
        other = self.tmp / "other"
        r = self.run_ap2("gate", "--data", str(other))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(json.loads(r.stdout)["last_run_at"].endswith("Z"))
        self.assertTrue((other / "gate.json").is_file())
        self.assertFalse(self.data.exists())

    def test_writes_nothing_outside_the_data_folder(self):
        self.put_routine()
        r = self.run_ap2("gate", "--now", "2025-11-11T17:01:00Z")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["data", "home"])
        self.assertEqual(list(self.home.iterdir()), [])

    def test_now_without_a_zone_or_not_a_time_is_a_usage_error(self):
        for bad in ("2025-11-11T09:00", "tomorrow"):
            with self.subTest(now=bad):
                r = self.run_ap2("gate", "--now", bad)
                self.assertEqual(r.returncode, 2)
                self.assertEqual(r.stdout, "")
                self.assertIn("--now", r.stderr)
                self.assertNotIn("Traceback", r.stderr)
        self.assertFalse(self.data.exists())

    def test_a_now_later_than_the_real_time_is_a_usage_error_and_writes_nothing(self):
        # A run in the future would write last_slot, make cards and prune restore points as if it were then.
        real = datetime.now(timezone.utc)
        for later in (timedelta(minutes=10), timedelta(days=400)):
            now = (real + later).strftime("%Y-%m-%dT%H:%M:%SZ")
            with self.subTest(now=now):
                r = self.run_ap2("gate", "--now", now)
                self.assertEqual(r.returncode, 2)
                self.assertEqual(r.stdout, "")
                self.assertEqual([line for line in r.stderr.splitlines() if "error" in line],
                                 [f"ap2 gate: error: argument --now: {now!r} cannot be later than the real time: "
                                  f"a run writes last_slot, makes cards and prunes restore points"])
                self.assertNotIn("Traceback", r.stderr)
        self.assertFalse(self.data.exists())

    def test_a_now_out_of_range_is_a_usage_error(self):
        for bad in ("0001-01-02T00:00:00Z", "0001-01-01T00:00:00+01:00", "1970-12-31T23:59:59Z",
                    "9999-12-31T23:59:59-01:00"):
            with self.subTest(now=bad):
                r = self.run_ap2("gate", "--now", bad)
                self.assertEqual(r.returncode, 2)
                self.assertEqual(r.stdout, "")
                self.assertEqual([line for line in r.stderr.splitlines() if "error" in line],
                                 [f"ap2 gate: error: argument --now: {bad!r} is out of range (years 1971 to now)"])
                self.assertNotIn("Traceback", r.stderr)
        self.assertFalse(self.data.exists())

    def test_a_bad_config_is_one_error_line_and_exit_1(self):
        self.data.mkdir()
        (self.data / "config.yaml").write_text("mode: loud\n")
        r = self.run_ap2("gate", "--now", "2025-11-11T17:01:00Z")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, "")
        self.assertEqual(len(r.stderr.splitlines()), 1)
        self.assertTrue(r.stderr.startswith("ap2: "), r.stderr)
        self.assertIn("mode", r.stderr)

    def test_a_config_that_does_not_parse_leaves_a_fatal_line_in_gate_log(self):
        self.data.mkdir()
        (self.data / "config.yaml").write_text("mode: [\n")
        r = self.run_ap2("gate", "--now", "2025-11-11T17:01:00Z")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, "")
        self.assertEqual(len(r.stderr.splitlines()), 1)
        self.assertTrue(r.stderr.startswith("ap2: "), r.stderr)
        error = r.stderr.strip()[len("ap2: "):]
        lines = (self.data / "gate.log").read_text().splitlines()
        self.assertEqual(lines[-1], f"2025-11-11T17:01:00Z fatal: {error}")

    def test_a_bad_routine_is_reported_and_the_run_still_exits_0(self):
        self.put_routine()
        self.put_routine("broken", tz="Nowhere/Land")
        r = self.run_ap2("gate", "--now", "2025-11-11T17:01:00Z")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual([f["routine"] for f in out["routines"]], ["digest"])
        self.assertEqual([e["routine"] for e in out["errors"]], ["broken"])

    def test_a_second_run_while_the_lock_is_held_exits_0_and_does_nothing(self):
        self.put_routine()
        self.data.mkdir(exist_ok=True)
        r = self.run_ap2("config")  # the layout exists first, so the locked run has nothing to set up
        self.assertEqual(r.returncode, 0, r.stderr)
        fd = os.open(str(self.data / ".gate.lock"), os.O_CREAT | os.O_RDWR, 0o644)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        r = self.run_ap2("gate", "--now", "2025-11-11T17:01:00Z")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"locked": True, "lock": str(self.data / ".gate.lock")})
        self.assertFalse((self.data / "gate.json").exists())
        self.assertFalse((self.data / "gate.log").exists())
        self.assertFalse((self.data / "items").exists() and any((self.data / "items").iterdir()))

    def test_help_lists_gate(self):
        r = self.run_ap2("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("gate", r.stdout)
        r = self.run_ap2("gate", "--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("--now", r.stdout)


if __name__ == "__main__":
    unittest.main()
