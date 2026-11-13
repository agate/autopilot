"""`ap2 install` and `ap2 uninstall`: v2's own launchd job, written under a temp home with a fake launchctl."""
from __future__ import annotations

import contextlib
import io
import json
import os
import plistlib
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT  # noqa: F401  (puts lib/ on sys.path)
from tests.test_ap2_config import TempHome
from assistant import cli, datadir, doctor, install
from autopilot import install as v1_install

V1_LABEL = "com.autopilot.gate"
SECRETARY_LABEL = "com.secretary.gate"


class FakeLaunchctl:
    """Records every call; `fail` maps a launchctl verb to the (code, output) it returns."""

    def __init__(self, fail=None):
        self.calls = []
        self.fail = dict(fail or {})

    def __call__(self, *args):
        self.calls.append(list(args))
        return self.fail.get(args[0], (0, ""))


def fake_which(folder: Path):
    return lambda tool: str(folder / tool)


class HomeTest(unittest.TestCase):
    def setUp(self):
        home = TempHome()
        self.home = home.__enter__()
        self.addCleanup(home.__exit__)
        self.agents = self.home / "Library" / "LaunchAgents"
        self.data = self.home / "Documents" / "Assistant"
        self.tools = self.home / "opt" / "tools" / "bin"

    def files(self, root=None) -> dict:
        root = Path(root or self.home)
        return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


class LabelAndPathsTest(HomeTest):
    def test_label_is_v2s_own(self):
        self.assertEqual(install.LABEL, "com.assistant.gate")
        self.assertEqual(install.INTERVAL, 60)
        for other in (V1_LABEL, SECRETARY_LABEL, v1_install.LABEL):
            self.assertNotEqual(install.LABEL, other)

    def test_paths_differ_from_v1s_and_default_into_the_home(self):
        self.assertNotEqual(install.plist_path().name, v1_install.PLIST.name)
        self.assertNotEqual(install.launcher_path(), v1_install.LAUNCHER)
        self.assertEqual(install.plist_path(), self.home / "Library" / "LaunchAgents" / "com.assistant.gate.plist")
        self.assertEqual(install.launcher_path(),
                         self.home / "Library" / "Application Support" / "assistant" / "assistant-gate")
        for p in (install.plist_path(), install.launcher_path()):
            self.assertTrue(p.is_relative_to(self.home), p)

    def test_paths_take_the_folder_given(self):
        self.assertEqual(install.plist_path(self.agents / "x"), self.agents / "x" / "com.assistant.gate.plist")
        self.assertEqual(install.launcher_path(self.home / "h"),
                         self.home / "h" / "Library" / "Application Support" / "assistant" / "assistant-gate")


class PlistTest(unittest.TestCase):
    def test_fields(self):
        pl = plistlib.loads(install.render_plist(["/launcher"], Path("/data/a"), ["/opt/tools/bin", "/usr/bin"]))
        self.assertEqual(pl["Label"], "com.assistant.gate")
        self.assertEqual(pl["ProgramArguments"], ["/launcher"])
        self.assertEqual(pl["StartInterval"], 60)
        self.assertIs(pl["RunAtLoad"], True)
        self.assertEqual(pl["StandardOutPath"], "/dev/null")
        self.assertEqual(pl["StandardErrorPath"], "/data/a/gate.err.log")
        env = pl["EnvironmentVariables"]
        self.assertEqual(set(env), {"PATH", "AP2_DATA"})
        self.assertEqual(env["AP2_DATA"], "/data/a")
        self.assertNotIn("AUTOPILOT_DATA", env)

    def test_path_is_tool_folders_then_system_folders_each_once(self):
        pl = plistlib.loads(install.render_plist(["/l"], Path("/d"), ["/opt/b", "/opt/a", "/opt/b", "/bin"]))
        path = pl["EnvironmentVariables"]["PATH"].split(":")
        self.assertEqual(path, ["/opt/b", "/opt/a", "/bin"] + [d for d in v1_install.SYSTEM_PATH if d != "/bin"])
        self.assertEqual(len(path), len(set(path)))


class InstallTest(HomeTest):
    def do_install(self, launcher="built", fail=None, data=None):
        fake = FakeLaunchctl(fail)
        built = []

        def build(ap, dest):
            built.append((ap, dest))
            if launcher is None:
                return None
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_text("launcher")
            return Path(dest)

        out = install.install(data, agents_dir=self.agents, home=self.home, launchctl=fake,
                              which=fake_which(self.tools), build=build)
        return out, fake, built

    def test_writes_the_plist_under_the_agents_folder_then_bootout_and_bootstrap(self):
        out, fake, built = self.do_install()
        plist = self.agents / "com.assistant.gate.plist"
        self.assertTrue(plist.is_file())
        uid = os.getuid()
        self.assertEqual(fake.calls, [["bootout", f"gui/{uid}/com.assistant.gate"],
                                      ["bootstrap", f"gui/{uid}", str(plist)]])
        pl = plistlib.loads(plist.read_bytes())
        launcher = install.launcher_path(self.home)
        self.assertEqual(pl["ProgramArguments"], [str(launcher)])
        self.assertEqual(pl["EnvironmentVariables"]["AP2_DATA"], str(self.data))
        self.assertEqual(pl["EnvironmentVariables"]["PATH"].split(":")[0], str(self.tools))
        self.assertEqual(built, [(str(install.ap2_program()), launcher)])
        self.assertTrue(str(install.ap2_program()).endswith("/bin/ap2"))
        self.assertEqual(out, {"label": "com.assistant.gate", "plist": str(plist), "program": [str(launcher)],
                               "interval": 60, "data_dir": str(self.data), "mode": "shadow",
                               "tools": {t: str(self.tools / t) for t in ("claude", "gh", "git", "python3")},
                               "note": ""})
        self.assertFalse(list(self.agents.glob("*.new")))

    def test_a_second_install_gives_the_same_plist_and_calls(self):
        _, first, _ = self.do_install()
        before = (self.agents / "com.assistant.gate.plist").read_bytes()
        _, second, _ = self.do_install()
        self.assertEqual((self.agents / "com.assistant.gate.plist").read_bytes(), before)
        self.assertEqual(second.calls, first.calls)

    def test_without_a_compiler_runs_bin_ap2_gate_and_says_why(self):
        out, _, _ = self.do_install(launcher=None)
        argv = plistlib.loads((self.agents / "com.assistant.gate.plist").read_bytes())["ProgramArguments"]
        self.assertEqual(argv, [str(install.ap2_program()), "gate"])
        self.assertEqual(out["program"], argv)
        self.assertIn("cloud-synced", out["note"])
        self.assertIn("no C compiler", out["note"])

    def test_a_new_data_folder_says_shadow(self):
        self.do_install()
        self.assertIn("mode: shadow", (self.data / "config.yaml").read_text())
        for name in datadir.FOLDERS:
            self.assertTrue((self.data / name).is_dir(), name)

    def test_an_existing_config_yaml_is_kept_byte_for_byte(self):
        self.data.mkdir(parents=True)
        text = b"# mine\nmode: live\nprimary_language: zh\n"
        (self.data / "config.yaml").write_bytes(text)
        out, _, _ = self.do_install()
        self.assertEqual((self.data / "config.yaml").read_bytes(), text)
        self.assertEqual(out["mode"], "live")

    def test_data_flag_is_written_into_the_plist(self):
        other = self.home / "elsewhere"
        out, _, _ = self.do_install(data=str(other))
        self.assertEqual(out["data_dir"], str(other))
        pl = plistlib.loads((self.agents / "com.assistant.gate.plist").read_bytes())
        self.assertEqual(pl["EnvironmentVariables"]["AP2_DATA"], str(other))
        self.assertEqual(pl["StandardErrorPath"], str(other / "gate.err.log"))

    def test_a_missing_tool_is_null_and_left_off_path(self):
        fake = FakeLaunchctl()
        out = install.install(agents_dir=self.agents, home=self.home, launchctl=fake,
                              which=lambda t: None if t == "gh" else f"/opt/{t}/bin/{t}", build=lambda a, d: None)
        self.assertIsNone(out["tools"]["gh"])
        path = plistlib.loads((self.agents / "com.assistant.gate.plist").read_bytes())["EnvironmentVariables"]["PATH"]
        self.assertNotIn("/opt/gh/bin", path.split(":"))
        self.assertIn("/opt/claude/bin", path.split(":"))

    def test_a_failed_bootstrap_raises_install_error(self):
        with self.assertRaises(install.InstallError) as cm:
            self.do_install(fail={"bootstrap": (5, "Bootstrap failed: 5:\n Input/output error")})
        self.assertEqual(str(cm.exception), "launchctl bootstrap failed: Bootstrap failed: 5: Input/output error")
        self.assertIsInstance(cm.exception, install.ConfigError)

    def test_a_bad_config_yaml_is_refused_before_the_job_is_written(self):
        self.data.mkdir(parents=True)
        (self.data / "config.yaml").write_text("mode: loud\n")
        fake = FakeLaunchctl()
        with self.assertRaises(install.ConfigError):
            install.install(agents_dir=self.agents, home=self.home, launchctl=fake, which=fake_which(self.tools),
                            build=lambda a, d: None)
        self.assertEqual(fake.calls, [])
        self.assertFalse(self.agents.exists())

    def test_a_data_folder_inside_claude_is_refused(self):
        fake = FakeLaunchctl()
        with self.assertRaises(install.ConfigError):
            install.install(str(self.home / ".claude" / "data"), agents_dir=self.agents, home=self.home,
                            launchctl=fake, which=fake_which(self.tools), build=lambda a, d: None)
        self.assertEqual(fake.calls, [])
        self.assertFalse((self.home / ".claude").exists())

    @unittest.skipUnless(shutil.which("clang"), "needs clang")
    def test_the_default_build_compiles_v1s_launcher_to_v2s_path(self):
        out = install.install(agents_dir=self.agents, home=self.home, launchctl=FakeLaunchctl(),
                              which=fake_which(self.tools))
        launcher = install.launcher_path(self.home)
        self.assertEqual(out["program"], [str(launcher)])
        self.assertTrue(os.access(launcher, os.X_OK))


class UninstallTest(HomeTest):
    def test_removes_the_plist_and_launcher_and_keeps_the_data_folder(self):
        install.install(agents_dir=self.agents, home=self.home, launchctl=FakeLaunchctl(),
                        which=fake_which(self.tools),
                        build=lambda a, d: (Path(d).parent.mkdir(parents=True, exist_ok=True),
                                            Path(d).write_text("x"), Path(d))[2])
        (self.data / "gate.json").write_text('{"last_run_at": "2026-11-13T00:00:00Z"}')
        (self.data / "gate.log").write_text("2026-11-13T00:00:00Z run\n")
        data_before = self.files(self.data)
        plist, launcher = install.plist_path(self.agents), install.launcher_path(self.home)
        self.assertTrue(plist.exists() and launcher.exists())
        fake = FakeLaunchctl()
        out = install.uninstall(agents_dir=self.agents, home=self.home, launchctl=fake)
        self.assertFalse(plist.exists())
        self.assertFalse(launcher.exists())
        self.assertEqual(self.files(self.data), data_before)
        self.assertEqual(fake.calls, [["bootout", f"gui/{os.getuid()}/com.assistant.gate"]])
        self.assertEqual(out, {"label": "com.assistant.gate", "removed": [str(plist), str(launcher)]})

    def test_nothing_installed_is_fine(self):
        fake = FakeLaunchctl(fail={"bootout": (113, "Could not find service")})
        out = install.uninstall(agents_dir=self.agents, home=self.home, launchctl=fake)
        self.assertEqual(out["removed"], [])
        self.assertEqual(len(fake.calls), 1)


class DefaultRunnerTest(unittest.TestCase):
    def test_runs_launchctl_with_a_30_second_timeout(self):
        done = subprocess.CompletedProcess(["launchctl"], 0, "out\n", "err\n")
        with mock.patch.object(install.subprocess, "run", return_value=done) as run:
            self.assertEqual(install._launchctl("print", "gui/1/com.assistant.gate"), (0, "out\nerr"))
        self.assertEqual(run.call_args.args[0], ["launchctl", "print", "gui/1/com.assistant.gate"])
        self.assertEqual(run.call_args.kwargs["timeout"], 30)

    def test_a_missing_launchctl_is_127(self):
        with mock.patch.object(install.subprocess, "run", side_effect=FileNotFoundError(2, "No such file")):
            rc, msg = install._launchctl("print", "x")
        self.assertEqual(rc, 127)
        self.assertIn("cannot run launchctl", msg)

    def test_a_timeout_is_127(self):
        with mock.patch.object(install.subprocess, "run", side_effect=subprocess.TimeoutExpired("launchctl", 30)):
            self.assertEqual(install._launchctl("bootstrap", "x"), (127, "launchctl timed out after 30 s"))


class NoRealLaunchctlTest(HomeTest):
    """Every command, run through `cli.main`, reaches launchctl only through the replaced runner."""

    def setUp(self):
        super().setUp()
        # Fail-safe: the cli commands below use the default paths, so they must land in the temp home.
        for p in (install.plist_path(), install.launcher_path()):
            self.assertTrue(p.is_relative_to(self.home), f"default path {p} is outside the temp home; refusing to "
                                                         f"run cli commands")
        real_run, real_popen = subprocess.run, subprocess.Popen

        def guard(argv):
            first = argv[0] if isinstance(argv, (list, tuple)) else str(argv).split()[0]
            if str(first).endswith("launchctl"):
                self.fail(f"the real launchctl was run: {argv}")

        def run(argv, *a, **kw):
            guard(argv)
            return real_run(argv, *a, **kw)

        def popen(argv, *a, **kw):
            guard(argv)
            return real_popen(argv, *a, **kw)

        self.fake = FakeLaunchctl()
        for patch in (mock.patch.object(subprocess, "run", run), mock.patch.object(subprocess, "Popen", popen),
                      mock.patch.object(install, "_launchctl", self.fake),
                      mock.patch.object(install, "_build", lambda ap, dest: None)):
            patch.start()
            self.addCleanup(patch.stop)

    def main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_install_doctor_uninstall_use_only_the_fake_and_only_v2s_label(self):
        code, out, err = self.main(["install", "--data", str(self.data)])
        self.assertEqual((code, err), (0, ""))
        printed = json.loads(out)
        self.assertEqual(printed["label"], "com.assistant.gate")
        self.assertEqual(printed["mode"], "shadow")
        self.assertTrue((self.home / "Library" / "LaunchAgents" / "com.assistant.gate.plist").is_file())
        code, out, _ = self.main(["doctor", "--data", str(self.data)])
        self.assertIn("ok   launchd job:", out)
        code, out, err = self.main(["uninstall"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["label"], "com.assistant.gate")
        verbs = [c[0] for c in self.fake.calls]
        self.assertEqual(verbs, ["bootout", "bootstrap", "print", "bootout"])
        targets = [a for c in self.fake.calls for a in c[1:] if a.startswith("gui/")]
        for t in targets:
            if t.count("/") == 2:
                self.assertTrue(t.endswith("/com.assistant.gate"), t)
        joined = json.dumps(self.fake.calls)
        self.assertNotIn(V1_LABEL, joined)
        self.assertNotIn(SECRETARY_LABEL, joined)
        self.assertIn("com.assistant.gate", " ".join(c[-1] for c in self.fake.calls))

    def test_a_failed_bootstrap_is_one_line_and_exit_1(self):
        self.fake.fail["bootstrap"] = (5, "Bootstrap failed: 5: Input/output error")
        code, out, err = self.main(["install", "--data", str(self.data)])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(err, "ap2: launchctl bootstrap failed: Bootstrap failed: 5: Input/output error\n")

    def test_a_launcher_that_cannot_be_built_is_one_line_and_writes_no_job(self):
        blocker = self.home / "Library" / "Application Support" / "assistant"
        blocker.parent.mkdir(parents=True)
        blocker.write_text("a file where the launcher's folder goes")

        def build(ap, dest):  # the real build when a compiler exists, else the error it would raise
            if shutil.which("clang") or shutil.which("cc"):
                return v1_install.build_launcher(ap, dest)
            raise FileExistsError(17, "File exists", str(blocker))

        with mock.patch.object(install, "_build", build):
            code, out, err = self.main(["install", "--data", str(self.data)])
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(len(err.splitlines()), 1, err)
        self.assertTrue(err.startswith(f"ap2: cannot build the launcher at {install.launcher_path()}: "), err)
        self.assertFalse(install.plist_path().exists())
        self.assertEqual(self.fake.calls, [])

    def test_doctor_checks_take_the_runner(self):
        self.assertEqual(self.main(["install", "--data", str(self.data)])[0], 0)
        self.fake.calls.clear()
        fake = FakeLaunchctl()
        doctor.checks(str(self.data), agents_dir=self.agents, launchctl=fake)
        self.assertEqual(fake.calls, [["print", f"gui/{os.getuid()}/com.assistant.gate"]])
        self.assertEqual(self.fake.calls, [])


if __name__ == "__main__":
    unittest.main()
