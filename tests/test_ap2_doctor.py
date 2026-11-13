"""`ap2 doctor`: one test per check, pass and fail, under a temp home with a fake launchctl."""
from __future__ import annotations

import contextlib
import io
import json
import os
import plistlib
import shlex
from datetime import timedelta
from unittest import mock
import unittest

from tests.helpers import ROOT  # noqa: F401  (puts lib/ on sys.path)
from tests.test_ap2_config import TempHome
from assistant import cards, cli, config, datadir, doctor, install

NOW = cards.parse_stamp("2026-11-13T12:00:00Z")


class FakeLaunchctl:
    def __init__(self, rc=0):
        self.rc, self.calls = rc, []

    def __call__(self, *args):
        self.calls.append(list(args))
        return self.rc, "" if self.rc == 0 else "Could not find service \"com.assistant.gate\" in domain for user"


class DoctorTest(unittest.TestCase):
    def setUp(self):
        home = TempHome()
        self.home = home.__enter__()
        self.addCleanup(home.__exit__)
        # Not the defaults, so a check that drops one of these arguments reads somewhere else and a test sees it.
        self.agents = self.home / "agents"
        self.data = datadir.ensure(self.home / "data")
        self.tools = self.home / "tools"
        self.tools.mkdir()
        for t in install.TOOLS:
            stub = self.tools / t
            stub.write_text("#!/bin/sh\nexit 0\n")
            stub.chmod(0o755)

    def write_plist(self, label=install.LABEL, data=None, path=None, err_log=None):
        self.agents.mkdir(parents=True, exist_ok=True)
        pl = plistlib.loads(install.render_plist(["/x/assistant-gate"], data or self.data, [],
                                                 err_log or install.err_log_path()))
        pl["Label"] = label
        pl["EnvironmentVariables"]["PATH"] = path if path is not None else str(self.tools)
        (self.agents / f"{install.LABEL}.plist").write_bytes(plistlib.dumps(pl))

    def write_gate(self, minutes_ago=0, seconds=0):
        at = NOW - timedelta(minutes=minutes_ago, seconds=seconds)
        (self.data / "gate.json").write_text(json.dumps({"last_run_at": cards.stamp(at)}))

    def checks(self, rc=0, data_flag="self"):
        data_flag = str(self.data) if data_flag == "self" else data_flag
        self.fake = FakeLaunchctl(rc)
        res = doctor.checks(data_flag, agents_dir=self.agents, launchctl=self.fake, now=NOW)
        for c in res:
            self.assertEqual(set(c), {"name", "ok", "detail", "fix"})
            if not c["ok"]:
                self.assertTrue(c["fix"], c)
            else:
                self.assertEqual(c["fix"], "", c)
        return {c["name"]: c for c in res}

    def test_names_in_order(self):
        names = [c["name"] for c in doctor.checks(agents_dir=self.agents, launchctl=FakeLaunchctl(), now=NOW)]
        self.assertEqual(names, ["launchd job", "gate ran recently", "data folder", "tool claude", "tool gh",
                                 "tool git", "tool python3", "active profile", "mode"])

    # -- launchd job

    def test_job_passes_when_written_loaded_and_on_this_data_folder(self):
        self.write_plist()
        c = self.checks()["launchd job"]
        self.assertTrue(c["ok"], c)
        self.assertEqual(self.fake.calls, [["print", f"gui/{os.getuid()}/com.assistant.gate"]])

    def test_job_fails_when_not_loaded(self):
        self.write_plist()
        c = self.checks(rc=113)["launchd job"]
        self.assertFalse(c["ok"])
        self.assertIn("113", c["detail"])
        self.assertIn("ap2 install", c["fix"])

    def test_job_fails_without_a_plist_and_runs_no_launchctl(self):
        c = self.checks()["launchd job"]
        self.assertFalse(c["ok"])
        self.assertIn("not installed", c["detail"])
        self.assertEqual(self.fake.calls, [])

    def test_job_fails_on_an_unreadable_plist(self):
        self.agents.mkdir(parents=True)
        (self.agents / f"{install.LABEL}.plist").write_text("not a plist")
        c = self.checks()["launchd job"]
        self.assertFalse(c["ok"])
        self.assertIn("cannot read", c["detail"])

    def test_job_fails_on_another_label(self):
        self.write_plist(label="com.autopilot.gate")
        c = self.checks()["launchd job"]
        self.assertFalse(c["ok"])
        self.assertIn("com.autopilot.gate", c["detail"])

    def test_job_fails_on_another_data_folder_and_the_fix_names_the_flag(self):
        self.write_plist(data=self.home / "other")
        c = self.checks(data_flag=str(self.data))["launchd job"]
        self.assertFalse(c["ok"])
        self.assertIn(str(self.home / "other"), c["detail"])
        self.assertIn(f"ap2 install --data {shlex.quote(str(self.data))}", c["fix"])

    def test_job_passes_when_the_plist_names_the_same_folder_another_way(self):
        link = self.home / "link"
        link.symlink_to(self.data, target_is_directory=True)
        self.write_plist(data=link)
        c = self.checks()["launchd job"]
        self.assertTrue(c["ok"], c)

    def test_job_reads_the_plist_from_the_agents_folder_given(self):
        self.write_plist()
        self.assertTrue(self.checks()["launchd job"]["ok"])
        self.assertFalse((self.home / "Library").exists())

    # -- gate ran recently

    def test_gate_one_minute_ago_passes(self):
        self.write_gate(1)
        c = self.checks()["gate ran recently"]
        self.assertTrue(c["ok"], c)
        self.assertIn("60 s ago", c["detail"])

    def test_gate_two_and_a_half_minutes_ago_passes_and_three_fails(self):
        self.write_gate(seconds=150)
        c = self.checks()["gate ran recently"]
        self.assertTrue(c["ok"], c)
        self.assertIn("150 s ago", c["detail"])
        self.write_gate(seconds=180)
        c = self.checks()["gate ran recently"]
        self.assertFalse(c["ok"], c)
        self.assertIn("180 s ago", c["detail"])

    def test_gate_ten_minutes_ago_fails_and_points_at_the_logs(self):
        self.write_gate(10)
        c = self.checks()["gate ran recently"]
        self.assertFalse(c["ok"])
        self.assertIn(str(self.data / "gate.log"), c["fix"])
        self.assertIn(str(install.err_log_path()), c["fix"])  # no plist: where install puts it
        self.assertNotIn(str(self.data / "gate.err.log"), c["fix"])

    def test_gate_fix_names_the_err_log_the_plist_gives(self):
        err_log = self.home / "logs" / "mine.err.log"
        self.write_plist(err_log=err_log)
        self.write_gate(10)
        c = self.checks()["gate ran recently"]
        self.assertFalse(c["ok"])
        self.assertIn(f"look at {self.data / 'gate.log'} and {err_log}", c["fix"])

    def test_gate_never_ran_fails(self):
        c = self.checks()["gate ran recently"]
        self.assertFalse(c["ok"])
        self.assertIn("never", c["detail"])

    def test_gate_bad_json_or_stamp_fails(self):
        for text in ("{", '{"last_run_at": "yesterday"}', "[]", '{"last_run_at": 5}'):
            with self.subTest(text):
                (self.data / "gate.json").write_text(text)
                self.assertFalse(self.checks()["gate ran recently"]["ok"])

    # -- data folder

    def test_data_folder_passes(self):
        c = self.checks()["data folder"]
        self.assertTrue(c["ok"], c)
        self.assertEqual(c["detail"], str(self.data))

    def test_data_folder_inside_claude_fails(self):
        res = self.checks(data_flag=str(self.home / ".claude" / "assistant"))
        c = res["data folder"]
        self.assertFalse(c["ok"])
        self.assertIn(".claude", c["detail"])
        self.assertIn("outside ~/.claude", c["fix"])
        self.assertFalse(res["mode"]["ok"])
        self.assertFalse(res["active profile"]["ok"])

    def test_data_folder_missing_fails_with_install(self):
        c = self.checks(data_flag=str(self.home / "nowhere"))["data folder"]
        self.assertFalse(c["ok"])
        self.assertIn("does not exist", c["detail"])
        self.assertIn("ap2 install", c["fix"])

    def test_data_folder_that_is_a_file_fails(self):
        f = self.home / "afile"
        f.write_text("x")
        self.assertFalse(self.checks(data_flag=str(f))["data folder"]["ok"])

    @unittest.skipIf(os.geteuid() == 0, "root can write anywhere")
    def test_data_folder_not_writable_fails(self):
        self.data.chmod(0o555)
        self.addCleanup(self.data.chmod, 0o755)
        c = self.checks()["data folder"]
        self.assertFalse(c["ok"])
        self.assertIn("not writable", c["detail"])

    def assert_privacy_fix(self, res):
        for name in ("data folder", "mode"):
            self.assertFalse(res[name]["ok"], res[name])
            fix = res[name]["fix"]
            self.assertIn("System Settings > Privacy & Security > Files and Folders", fix)
            self.assertIn("Full Disk Access", fix)
            self.assertIn("assistant-gate", fix)
            self.assertIn("terminal", fix)
            self.assertNotIn("fix ", fix)

    def test_config_yaml_macos_will_not_let_us_read_gives_the_privacy_fix(self):
        def blocked(data_dir):
            path = data_dir / config.FILE
            try:
                raise PermissionError(1, "Operation not permitted", str(path))
            except PermissionError as e:
                raise config.ConfigError(f"{path}: cannot read: {e}") from e
        with mock.patch.object(doctor.config, "load", side_effect=blocked):
            self.assert_privacy_fix(self.checks())

    @unittest.skipIf(os.geteuid() == 0, "root can read anywhere")
    def test_an_unreadable_config_yaml_gives_the_privacy_fix(self):
        f = self.data / "config.yaml"
        f.chmod(0o000)
        self.addCleanup(f.chmod, 0o644)
        self.assert_privacy_fix(self.checks())

    def test_bad_config_yaml_fails_data_folder_and_mode(self):
        (self.data / "config.yaml").write_text("mode: loud\n")
        res = self.checks()
        self.assertFalse(res["data folder"]["ok"])
        self.assertIn("config.yaml", res["data folder"]["fix"])
        self.assertFalse(res["mode"]["ok"])
        self.assertIn("config.yaml", res["mode"]["fix"])
        self.assertNotIn("Privacy", res["data folder"]["fix"] + res["mode"]["fix"])

    # -- tools

    def test_tools_found_on_the_plists_path(self):
        self.write_plist()
        res = self.checks()
        for t in install.TOOLS:
            self.assertTrue(res[f"tool {t}"]["ok"], res[f"tool {t}"])
            self.assertIn(str(self.tools / t), res[f"tool {t}"]["detail"])
            self.assertIn("the job's PATH", res[f"tool {t}"]["detail"])

    def test_a_tool_missing_from_the_plists_path_fails(self):
        (self.tools / "gh").unlink()
        self.write_plist()
        res = self.checks()
        self.assertFalse(res["tool gh"]["ok"])
        self.assertIn(f"then run `ap2 install --data {shlex.quote(str(self.data))}` again", res["tool gh"]["fix"])
        self.assertIn("PATH", res["tool gh"]["fix"])
        self.assertTrue(res["tool claude"]["ok"])

    def test_the_shells_path_does_not_count_when_there_is_a_plist(self):
        self.write_plist(path="/nonexistent-folder")
        with mock.patch.dict(os.environ, {"PATH": str(self.tools)}):
            res = self.checks()
        self.assertFalse(res["tool claude"]["ok"])

    def test_without_a_plist_the_current_path_is_used_and_said(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.tools)}):
            res = self.checks()
        self.assertTrue(res["tool git"]["ok"])
        self.assertIn("current PATH", res["tool git"]["detail"])
        with mock.patch.dict(os.environ, {"PATH": str(self.home / "empty")}):
            self.assertFalse(self.checks()["tool git"]["ok"])

    # -- active profile

    def test_active_profile_passes_with_the_builtin(self):
        c = self.checks()["active profile"]
        self.assertTrue(c["ok"], c)
        self.assertEqual(c["detail"], "subscription (subscription)")

    def test_a_broken_active_profile_fails_with_the_profiles_fix(self):
        (self.data / "active-profile.json").write_text("{nope")
        c = self.checks()["active profile"]
        self.assertFalse(c["ok"])
        self.assertIn("active-profile.json", c["detail"])
        self.assertEqual(c["fix"], doctor.profiles.FIX)

    def test_no_key_or_models_command_runs(self):
        with mock.patch.object(doctor.profiles, "fetch_models", side_effect=AssertionError("models ran")), \
                mock.patch("subprocess.run", side_effect=AssertionError("a command ran")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("a command ran")):
            self.assertTrue(self.checks()["active profile"]["ok"])

    # -- mode

    def test_mode_is_reported_not_judged(self):
        self.assertEqual(self.checks()["mode"], {"name": "mode", "ok": True, "detail": "shadow", "fix": ""})
        (self.data / "config.yaml").write_text("mode: live\n")
        self.assertEqual(self.checks()["mode"], {"name": "mode", "ok": True, "detail": "live", "fix": ""})

    # -- never raises

    def test_a_check_that_breaks_is_a_failed_line_not_a_traceback(self):
        self.write_plist()
        with mock.patch.object(doctor.profiles, "active", side_effect=RuntimeError("boom")):
            c = self.checks()["active profile"]
        self.assertFalse(c["ok"])
        self.assertIn("boom", c["detail"])

        def broken(*args):
            raise OSError("no launchctl here")
        res = doctor.checks(agents_dir=self.agents, launchctl=broken, now=NOW)
        self.assertFalse({c["name"]: c for c in res}["launchd job"]["ok"])


class DoctorCommandTest(unittest.TestCase):
    def setUp(self):
        home = TempHome()
        self.home = home.__enter__()
        self.addCleanup(home.__exit__)
        # Fail-safe: the cli commands below use the default plist path, so it must land in the temp home.
        self.assertTrue(install.plist_path().is_relative_to(self.home),
                        f"default path {install.plist_path()} is outside the temp home; refusing to run cli commands")
        self.fake = FakeLaunchctl()
        patch = mock.patch.object(install, "_launchctl", self.fake)
        patch.start()
        self.addCleanup(patch.stop)

    def main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def tree(self):
        return sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*"))

    def test_a_missing_data_folder_creates_nothing_and_exits_1(self):
        before = self.tree()
        code, out, err = self.main(["doctor"])
        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        self.assertEqual(self.tree(), before)
        self.assertFalse((self.home / "Documents").exists())
        self.assertIn("FAIL data folder:", out)

    def test_printout_is_v1s_shape(self):
        data = datadir.ensure(self.home / "Documents" / "Assistant")
        code, out, _ = self.main(["doctor", "--data", str(data)])
        self.assertEqual(code, 1)  # no launchd job, the gate never ran
        lines = out.splitlines()
        self.assertIn(f"ok   data folder: {data}", lines)
        self.assertIn("ok   mode: shadow", lines)
        i = next(n for n, line in enumerate(lines) if line.startswith("FAIL launchd job: "))
        self.assertTrue(lines[i + 1].startswith("     fix: run `ap2 install --data "), lines[i + 1])
        for n, line in enumerate(lines):
            if line.startswith("ok   "):
                self.assertFalse(lines[n + 1:n + 2] and lines[n + 1].startswith("     fix:"), line)

    def test_all_ok_exits_0(self):
        ok = [{"name": "x", "ok": True, "detail": "d", "fix": ""}]
        with mock.patch.object(doctor, "checks", return_value=ok):
            self.assertEqual(self.main(["doctor"]), (0, "ok   x: d\n", ""))


if __name__ == "__main__":
    unittest.main()
