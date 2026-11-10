from __future__ import annotations

import io
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stderr
from unittest import mock

from tests.helpers import TempData
from autopilot import shellenv, workers


def _script(path: Path, body: str) -> str:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


class ShellEnvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ap-shellenv-"))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.proj = self.tmp / "proj"
        self.proj.mkdir()
        # A stand-in for the user's login shell: drops `-lic` and runs the script with plain sh, on a PATH
        # that holds only this test's tools, so the real ~/.zshrc never runs in a test.
        self.shell = _script(self.bin / "fake-shell",
                             f'PATH="{self.bin}:/usr/bin:/bin"; export PATH; shift; exec /bin/sh -c "$@"\n')
        self.old = os.environ.pop("AUTOPILOT_PROJECT_ENV", None)

    def tearDown(self):
        if self.old is not None:
            os.environ["AUTOPILOT_PROJECT_ENV"] = self.old

    def test_reads_the_environment_inside_the_project_folder(self):
        env = shellenv.load(str(self.proj), shell=self.shell)
        self.assertEqual(Path(env["PWD"]).resolve(), self.proj.resolve())
        self.assertTrue(env["PATH"].startswith(str(self.bin)))

    def test_applies_the_project_direnv(self):
        # direnv prints the project's own variables only when the folder has an .envrc.
        _script(self.bin / "direnv",
                'if [ -f .envrc ]; then echo "export PROJ_CA=\'/proj/ca.pem\';"; fi\n')
        self.assertNotIn("PROJ_CA", shellenv.load(str(self.proj), shell=self.shell))
        (self.proj / ".envrc").write_text("export PROJ_CA=/proj/ca.pem\n")
        self.assertEqual(shellenv.load(str(self.proj), shell=self.shell)["PROJ_CA"], "/proj/ca.pem")

    def test_keeps_values_with_newlines_and_equals(self):
        _script(self.bin / "direnv", "echo \"export ODD='a=b\nc';\"\n")
        self.assertEqual(shellenv.load(str(self.proj), shell=self.shell)["ODD"], "a=b\nc")

    def test_banner_output_before_the_environment_is_ignored(self):
        noisy = _script(self.bin / "noisy-shell",
                        f'echo "welcome = banner"; PATH="{self.bin}:/usr/bin:/bin"; export PATH; shift; '
                        f'exec /bin/sh -c "$@"\n')
        env = shellenv.load(str(self.proj), shell=noisy)
        self.assertNotIn("welcome ", env)
        self.assertIn("PATH", env)

    def test_missing_folder_gives_none(self):
        self.assertIsNone(shellenv.load(str(self.tmp / "gone"), shell=self.shell))

    def test_failing_shell_gives_none(self):
        bad = _script(self.bin / "bad-shell", "exit 3\n")
        self.assertIsNone(shellenv.load(str(self.proj), shell=bad))

    def test_slow_shell_gives_none(self):
        slow = _script(self.bin / "slow-shell", "sleep 5\n")
        with mock.patch.object(shellenv, "TIMEOUT_S", 0.5):
            self.assertIsNone(shellenv.load(str(self.proj), shell=slow))

    def test_switch_off(self):
        os.environ["AUTOPILOT_PROJECT_ENV"] = "off"
        try:
            self.assertIsNone(shellenv.load(str(self.proj), shell=self.shell))
        finally:
            os.environ.pop("AUTOPILOT_PROJECT_ENV", None)

    def test_shell_outside_the_sh_family_is_not_used(self):
        fish = self.bin / "fish"
        fish.write_text("")
        with mock.patch.object(shellenv, "_login_shell", return_value=str(fish)):
            self.assertIsNone(shellenv.load(str(self.proj)))


class LaunchUsesProjectEnvTest(unittest.TestCase):
    def setUp(self):
        self.td = TempData()
        self.data = self.td.__enter__()

    def tearDown(self):
        self.td.__exit__(None, None, None)

    def _launch(self, project_env):
        done = subprocess.CompletedProcess([], 0, stdout="backgrounded · abcd1234 · x\n", stderr="")
        with mock.patch.object(workers.shellenv, "enabled", return_value=True), \
                mock.patch.object(workers.shellenv, "load", return_value=project_env) as load, \
                mock.patch.object(workers.subprocess, "run", return_value=done) as run:
            workers._launch(["claude"], "/repo", {"PATH": "/launchd/bin", "AUTOPILOT_ROLE": "worker",
                                                  "AUTOPILOT_DATA": "/data", "KEPT": "1"})
        load.assert_called_once_with("/repo")
        return run.call_args.kwargs["env"]

    def test_session_gets_the_project_environment(self):
        env = self._launch({"PATH": "/proj/bin", "SSL_CERT_FILE": "/proj/ca.pem", "AUTOPILOT_ROLE": "x"})
        self.assertEqual(env["PATH"], "/proj/bin")
        self.assertEqual(env["SSL_CERT_FILE"], "/proj/ca.pem")
        self.assertEqual(env["AUTOPILOT_ROLE"], "worker")  # the role always comes from the autopilot
        self.assertEqual(env["AUTOPILOT_DATA"], "/data")
        self.assertEqual(env["KEPT"], "1")

    def test_falls_back_to_the_given_environment(self):
        err = io.StringIO()
        with redirect_stderr(err):
            env = self._launch(None)
        self.assertEqual(env["PATH"], "/launchd/bin")
        self.assertEqual(env["AUTOPILOT_ROLE"], "worker")
        self.assertIn("could not read the shell environment of /repo", err.getvalue())

    def test_switched_off_launch_is_quiet_and_unchanged(self):
        done = subprocess.CompletedProcess([], 0, stdout="backgrounded · abcd1234 · x\n", stderr="")
        err = io.StringIO()
        with mock.patch.object(workers.subprocess, "run", return_value=done) as run, redirect_stderr(err):
            workers._launch(["claude"], "/repo", {"PATH": "/launchd/bin"})
        self.assertEqual(run.call_args.kwargs["env"], {"PATH": "/launchd/bin"})
        self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
