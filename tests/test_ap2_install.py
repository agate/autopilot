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
# v1 works these out at import, in the real home. HomeTest points them at sentinels in the temp home, so a call that
# falls back to v1's defaults (`build_launcher` without its `dest`) writes there and a test sees it.
V1_PLIST, V1_LAUNCHER = v1_install.PLIST, v1_install.LAUNCHER


class FakeLaunchctl:
    """Records every call; `fail` maps a launchctl verb to the (code, output) it returns. Otherwise it acts like
    launchctl: bootstrap loads the job, bootout unloads it, and `print` exits 0 only while it is loaded."""

    def __init__(self, fail=None, loaded=False):
        self.calls = []
        self.fail = dict(fail or {})
        self.loaded = loaded
        self.bootstrapped = []  # the plist's bytes as bootstrap found them on disk

    def __call__(self, *args):
        self.calls.append(list(args))
        if args[0] in self.fail:
            return self.fail[args[0]]
        if args[0] == "bootstrap":
            plist = Path(args[2])
            if not plist.is_file():
                raise AssertionError(f"bootstrap was asked to load {plist}, which is not on disk")
            self.bootstrapped.append(plist.read_bytes())
            self.loaded = True
        elif args[0] == "bootout":
            self.loaded = False
        elif args[0] == "print":
            return (0, "") if self.loaded else (113, "Could not find service")
        return 0, ""


def fake_which(folder: Path):
    return lambda tool: str(folder / tool)


class HomeTest(unittest.TestCase):
    def setUp(self):
        home = TempHome()
        self.home = home.__enter__()
        self.addCleanup(home.__exit__)
        # Not the defaults, so a command that drops one of these arguments writes somewhere else and a test sees it.
        self.agents = self.home / "agents"
        self.data = self.home / "data"
        self.lhome = self.home / "h"  # the `home` argument: the launcher and the err log go under it
        self.tools = self.home / "opt" / "tools" / "bin"
        self.v1_sentinel = self.home / "v1-must-not-exist"
        for name, path in (("LAUNCHER", self.v1_sentinel / V1_LAUNCHER.name),
                           ("PLIST", self.v1_sentinel / V1_PLIST.name)):
            patch = mock.patch.object(v1_install, name, path)
            patch.start()
            self.addCleanup(patch.stop)

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
        self.assertNotEqual(install.plist_path().name, V1_PLIST.name)
        self.assertNotEqual(install.launcher_path(), V1_LAUNCHER)
        self.assertNotEqual(install.launcher_path().name, V1_LAUNCHER.name)
        self.assertEqual(install.plist_path(), self.home / "Library" / "LaunchAgents" / "com.assistant.gate.plist")
        self.assertEqual(install.launcher_path(),
                         self.home / "Library" / "Application Support" / "assistant" / "assistant-gate")
        for p in (install.plist_path(), install.launcher_path()):
            self.assertTrue(p.is_relative_to(self.home), p)

    def test_paths_take_the_folder_given(self):
        self.assertEqual(install.plist_path(self.agents / "x"), self.agents / "x" / "com.assistant.gate.plist")
        self.assertEqual(install.launcher_path(self.home / "h"),
                         self.home / "h" / "Library" / "Application Support" / "assistant" / "assistant-gate")
        self.assertEqual(install.err_log_path(self.home / "h"),
                         self.home / "h" / "Library" / "Logs" / "assistant" / "gate.err.log")

    def test_the_err_log_defaults_into_the_homes_logs_not_the_data_folder(self):
        self.assertEqual(install.err_log_path(), self.home / "Library" / "Logs" / "assistant" / "gate.err.log")


class PlistTest(unittest.TestCase):
    def test_fields(self):
        pl = plistlib.loads(install.render_plist(["/launcher"], Path("/data/a"), ["/opt/tools/bin", "/usr/bin"],
                                                 Path("/logs/gate.err.log")))
        self.assertEqual(pl["Label"], "com.assistant.gate")
        self.assertEqual(pl["ProgramArguments"], ["/launcher"])
        self.assertEqual(pl["StartInterval"], 60)
        self.assertIs(pl["RunAtLoad"], True)
        self.assertEqual(pl["StandardOutPath"], "/dev/null")
        self.assertEqual(pl["StandardErrorPath"], "/logs/gate.err.log")
        env = pl["EnvironmentVariables"]
        self.assertEqual(set(env), {"PATH", "AP2_DATA"})
        self.assertEqual(env["AP2_DATA"], "/data/a")
        self.assertNotIn("AUTOPILOT_DATA", env)

    def test_path_is_tool_folders_then_system_folders_each_once(self):
        pl = plistlib.loads(install.render_plist(["/l"], Path("/d"), ["/opt/b", "/opt/a", "/opt/b", "/bin"],
                                                 Path("/e")))
        path = pl["EnvironmentVariables"]["PATH"].split(":")
        self.assertEqual(path, ["/opt/b", "/opt/a", "/bin"] + [d for d in v1_install.SYSTEM_PATH if d != "/bin"])
        self.assertEqual(len(path), len(set(path)))


class InstallTest(HomeTest):
    def do_install(self, launcher="built", fail=None, data="self", fake=None, sleep=None):
        fake = fake or FakeLaunchctl(fail)
        built = []

        def build(ap, dest):
            built.append((ap, dest))
            if launcher is None:
                return None
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_text("launcher")
            return Path(dest)

        out = install.install(self.data if data == "self" else data, agents_dir=self.agents, home=self.lhome,
                              launchctl=fake, which=fake_which(self.tools), build=build, sleep=sleep or self.fail)
        return out, fake, built

    def test_writes_the_plist_under_the_agents_folder_then_bootout_print_and_bootstrap(self):
        out, fake, built = self.do_install()
        plist = self.agents / "com.assistant.gate.plist"
        self.assertTrue(plist.is_file())
        uid = os.getuid()
        self.assertEqual(fake.calls, [["bootout", f"gui/{uid}/com.assistant.gate"],
                                      ["print", f"gui/{uid}/com.assistant.gate"],
                                      ["bootstrap", f"gui/{uid}", str(plist)]])
        self.assertEqual(fake.bootstrapped, [plist.read_bytes()])  # the final plist was on disk before bootstrap
        pl = plistlib.loads(plist.read_bytes())
        launcher = install.launcher_path(self.lhome)
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
        self.assertFalse((self.home / "Library").exists())  # nothing went to the default places
        self.assertFalse((self.home / "Documents").exists())

    def test_the_err_log_is_under_the_homes_logs_folder_which_install_creates(self):
        self.do_install()
        pl = plistlib.loads((self.agents / "com.assistant.gate.plist").read_bytes())
        err_log = self.lhome / "Library" / "Logs" / "assistant" / "gate.err.log"
        self.assertEqual(pl["StandardErrorPath"], str(err_log))
        self.assertTrue(err_log.parent.is_dir())
        self.assertFalse(err_log.is_relative_to(self.data))

    def test_a_logs_folder_that_cannot_be_made_is_one_line_and_writes_no_job(self):
        blocker = self.lhome / "Library" / "Logs"
        blocker.parent.mkdir(parents=True)
        blocker.write_text("a file where the logs folder goes")
        with self.assertRaises(install.InstallError) as cm:
            self.do_install()
        self.assertTrue(str(cm.exception).startswith(f"cannot create {blocker / 'assistant'}: "), cm.exception)
        self.assertEqual(len(str(cm.exception).splitlines()), 1)
        self.assertFalse(self.agents.exists())

    def test_a_data_folder_macos_guards_says_so_in_the_note(self):
        for parts in (("Documents", "Assistant"), ("Desktop", "a"), ("Library", "Mobile Documents", "x")):
            with self.subTest(parts):
                folder = self.home.joinpath(*parts)
                out, _, _ = self.do_install(data=str(folder))
                self.assertIn(f"macOS will ask to let assistant-gate into {folder}", out["note"])
                self.assertIn("cannot run until it is allowed", out["note"])
        out, _, _ = self.do_install(launcher=None, data=str(self.home / "Documents" / "Assistant"))
        self.assertIn("no C compiler", out["note"])
        self.assertIn("macOS will ask", out["note"])

    def test_downloads_and_another_letter_case_get_the_privacy_note(self):
        for folder in (self.home / "documents" / "x", self.home / "Downloads" / "x", self.home / "DESKTOP" / "y"):
            with self.subTest(folder):
                out, _, _ = self.do_install(data=str(folder))
                self.assertIn("macOS will ask to let assistant-gate into", out["note"])
        out, _, _ = self.do_install(data=str(self.home / "data"))
        self.assertEqual(out["note"], "")

    @unittest.skipIf(os.geteuid() == 0, "root can write anywhere")
    def test_a_plist_that_cannot_be_written_is_one_line_and_leaves_no_temp_file(self):
        self.agents.mkdir(parents=True)
        self.agents.chmod(0o555)
        self.addCleanup(self.agents.chmod, 0o755)
        with self.assertRaises(install.InstallError) as cm:
            self.do_install()
        plist = self.agents / "com.assistant.gate.plist"
        self.assertTrue(str(cm.exception).startswith(f"cannot write {plist}: "), cm.exception)
        self.assertEqual(len(str(cm.exception).splitlines()), 1)
        self.assertFalse(plist.exists())
        self.assertEqual(list(self.agents.glob("*.new")), [])

    def test_a_data_folder_elsewhere_has_no_privacy_note(self):
        for folder in (self.data, self.home / "Documents2", self.home / "Library" / "Other"):
            with self.subTest(folder):
                out, _, _ = self.do_install(data=str(folder))
                self.assertEqual(out["note"], "")

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
        self.assertEqual(pl["StandardErrorPath"], str(install.err_log_path(self.lhome)))

    def test_a_missing_tool_is_null_and_left_off_path(self):
        fake = FakeLaunchctl()
        out = install.install(self.data, agents_dir=self.agents, home=self.lhome, launchctl=fake,
                              which=lambda t: None if t == "gh" else f"/opt/{t}/bin/{t}", build=lambda a, d: None)
        self.assertIsNone(out["tools"]["gh"])
        path = plistlib.loads((self.agents / "com.assistant.gate.plist").read_bytes())["EnvironmentVariables"]["PATH"]
        self.assertNotIn("/opt/gh/bin", path.split(":"))
        self.assertIn("/opt/claude/bin", path.split(":"))

    def test_a_failed_bootstrap_raises_install_error(self):
        with self.assertRaises(install.InstallError) as cm:
            self.do_install(fail={"bootstrap": (5, "Bootstrap failed: 5:\n Input/output error")})
        self.assertEqual(str(cm.exception), "launchctl bootstrap failed: Bootstrap failed: 5: Input/output error; "
                                            "run `ap2 install` again")
        self.assertIsInstance(cm.exception, install.ConfigError)

    def test_a_running_job_that_takes_a_moment_to_go_is_waited_for_before_bootstrap(self):
        calls = []
        real = FakeLaunchctl(loaded=True)

        def slow(*args):  # bootout returns at once; `print` still finds the old job twice while the gate exits
            calls.append(args[0])
            if args[0] == "print":
                return (0, "") if calls.count("print") <= 2 else (113, "Could not find service")
            return real(*args)

        sleeps = []
        out, _, _ = self.do_install(fake=slow, sleep=sleeps.append)
        self.assertEqual(calls, ["bootout", "print", "print", "print", "bootstrap"])
        self.assertEqual(sleeps, [install.UNLOAD_WAIT] * 2)
        self.assertEqual(out["label"], "com.assistant.gate")

    def test_a_job_still_loaded_after_bootout_stops_install_before_bootstrap(self):
        fake = FakeLaunchctl(fail={"bootout": (0, "")}, loaded=True)  # bootout says yes, the job stays
        sleeps = []
        with self.assertRaises(install.InstallError) as cm:
            self.do_install(fake=fake, sleep=sleeps.append)
        self.assertEqual(str(cm.exception), "launchctl bootout did not finish: com.assistant.gate is still loaded "
                                            "after 10 checks; run `ap2 install` again")
        self.assertEqual([c[0] for c in fake.calls], ["bootout"] + ["print"] * install.UNLOAD_TRIES)
        self.assertEqual(sleeps, [install.UNLOAD_WAIT] * (install.UNLOAD_TRIES - 1))

    def test_a_bad_config_yaml_is_refused_before_the_job_is_written(self):
        self.data.mkdir(parents=True)
        (self.data / "config.yaml").write_text("mode: loud\n")
        fake = FakeLaunchctl()
        with self.assertRaises(install.ConfigError):
            install.install(self.data, agents_dir=self.agents, home=self.lhome, launchctl=fake,
                            which=fake_which(self.tools), build=lambda a, d: None)
        self.assertEqual(fake.calls, [])
        self.assertFalse(self.agents.exists())

    def test_a_data_folder_inside_claude_is_refused(self):
        fake = FakeLaunchctl()
        with self.assertRaises(install.ConfigError):
            install.install(str(self.home / ".claude" / "data"), agents_dir=self.agents, home=self.lhome,
                            launchctl=fake, which=fake_which(self.tools), build=lambda a, d: None)
        self.assertEqual(fake.calls, [])
        self.assertFalse((self.home / ".claude").exists())

    @unittest.skipUnless(shutil.which("clang"), "needs clang")
    def test_the_default_build_compiles_v1s_launcher_to_v2s_path(self):
        out = install.install(self.data, agents_dir=self.agents, home=self.lhome, launchctl=FakeLaunchctl(),
                              which=fake_which(self.tools))
        launcher = install.launcher_path(self.lhome)
        self.assertFalse(self.v1_sentinel.exists())  # nothing fell back to v1's own launcher path
        self.assertEqual(out["program"], [str(launcher)])
        self.assertTrue(os.access(launcher, os.X_OK))


class UninstallTest(HomeTest):
    def test_removes_the_plist_and_launcher_and_keeps_the_data_folder(self):
        install.install(self.data, agents_dir=self.agents, home=self.lhome, launchctl=FakeLaunchctl(),
                        which=fake_which(self.tools),
                        build=lambda a, d: (Path(d).parent.mkdir(parents=True, exist_ok=True),
                                            Path(d).write_text("x"), Path(d))[2])
        (self.data / "gate.json").write_text('{"last_run_at": "2026-11-13T00:00:00Z"}')
        (self.data / "gate.log").write_text("2026-11-13T00:00:00Z run\n")
        err_log = install.err_log_path(self.lhome)
        err_log.write_text("Traceback ...\n")
        data_before = self.files(self.data)
        plist, launcher = install.plist_path(self.agents), install.launcher_path(self.lhome)
        self.assertTrue(plist.exists() and launcher.exists())
        fake = FakeLaunchctl(loaded=True)
        out = install.uninstall(agents_dir=self.agents, home=self.lhome, launchctl=fake)
        self.assertFalse(plist.exists())
        self.assertFalse(launcher.exists())
        self.assertEqual(self.files(self.data), data_before)
        self.assertEqual(err_log.read_text(), "Traceback ...\n")
        target = f"gui/{os.getuid()}/com.assistant.gate"
        self.assertEqual(fake.calls, [["bootout", target], ["print", target]])
        self.assertEqual(out, {"label": "com.assistant.gate", "removed": [str(plist), str(launcher)]})
        self.assertFalse((self.home / "Library").exists())

    def test_nothing_installed_is_fine(self):
        fake = FakeLaunchctl(fail={"bootout": (113, "Could not find service")})
        out = install.uninstall(agents_dir=self.agents, home=self.lhome, launchctl=fake)
        self.assertEqual(out["removed"], [])
        self.assertEqual([c[0] for c in fake.calls], ["bootout", "print"])

    def test_a_job_still_loaded_after_bootout_is_an_error_and_keeps_the_files(self):
        plist, launcher = install.plist_path(self.agents), install.launcher_path(self.lhome)
        for f in (plist, launcher):
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x")
        fake = FakeLaunchctl(fail={"bootout": (5, "Boot-out failed: 5:\n Input/output error")}, loaded=True)
        sleeps = []
        with self.assertRaises(install.InstallError) as cm:
            install.uninstall(agents_dir=self.agents, home=self.lhome, launchctl=fake, sleep=sleeps.append)
        self.assertEqual(str(cm.exception), "launchctl bootout failed: com.assistant.gate is still loaded after 10 "
                                            "checks (Boot-out failed: 5: Input/output error); run `ap2 uninstall` "
                                            "again")
        self.assertTrue(plist.exists() and launcher.exists())
        self.assertEqual(install.UNLOAD_TRIES, 10)
        self.assertEqual([c[0] for c in fake.calls], ["bootout"] + ["print"] * install.UNLOAD_TRIES)
        self.assertEqual(sleeps, [install.UNLOAD_WAIT] * (install.UNLOAD_TRIES - 1))
        self.assertEqual(install.UNLOAD_WAIT, 0.5)

    def test_a_quiet_bootout_with_the_job_still_loaded_says_so_not_no_output(self):
        fake = FakeLaunchctl(fail={"bootout": (0, "")}, loaded=True)
        with self.assertRaises(install.InstallError) as cm:
            install.uninstall(agents_dir=self.agents, home=self.lhome, launchctl=fake, sleep=lambda s: None)
        self.assertEqual(str(cm.exception), "launchctl bootout failed: com.assistant.gate is still loaded after 10 "
                                            "checks (no output); run `ap2 uninstall` again")

    def test_a_print_that_cannot_ask_launchctl_is_an_error_and_keeps_the_files(self):
        plist, launcher = install.plist_path(self.agents), install.launcher_path(self.lhome)
        for f in (plist, launcher):
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x")
        fake = FakeLaunchctl(fail={"bootout": (127, "cannot run launchctl: no such file"),
                                   "print": (127, "cannot run launchctl:\n no such file")}, loaded=True)
        with self.assertRaises(install.InstallError) as cm:
            install.uninstall(agents_dir=self.agents, home=self.lhome, launchctl=fake, sleep=self.fail)
        self.assertEqual(str(cm.exception), "cannot confirm com.assistant.gate is gone: cannot run launchctl: no such "
                                            "file")
        self.assertTrue(plist.exists() and launcher.exists())
        self.assertEqual([c[0] for c in fake.calls], ["bootout", "print"])

    def test_a_job_that_takes_a_moment_to_go_is_waited_for(self):
        plist, launcher = install.plist_path(self.agents), install.launcher_path(self.lhome)
        for f in (plist, launcher):
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x")
        calls = []

        def slow(*args):  # bootout returns at once; `print` still finds the job twice while the gate exits
            calls.append(args[0])
            if args[0] == "print":
                return (0, "") if calls.count("print") <= 2 else (113, "Could not find service")
            return 0, ""

        sleeps = []
        out = install.uninstall(agents_dir=self.agents, home=self.lhome, launchctl=slow, sleep=sleeps.append)
        self.assertEqual(calls, ["bootout", "print", "print", "print"])
        self.assertEqual(sleeps, [install.UNLOAD_WAIT] * 2)
        self.assertFalse(plist.exists() or launcher.exists())
        self.assertEqual(out["removed"], [str(plist), str(launcher)])

    @unittest.skipIf(os.geteuid() == 0, "root can remove anything")
    def test_a_file_that_cannot_be_removed_is_one_line(self):
        plist, launcher = install.plist_path(self.agents), install.launcher_path(self.lhome)
        for f in (plist, launcher):
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x")
        launcher.parent.chmod(0o555)
        self.addCleanup(launcher.parent.chmod, 0o755)
        fake = FakeLaunchctl()  # not loaded: `print` exits non-zero, so uninstall gets to the files
        with self.assertRaises(install.InstallError) as cm:
            install.uninstall(agents_dir=self.agents, home=self.lhome, launchctl=fake, sleep=self.fail)
        self.assertTrue(str(cm.exception).startswith(f"cannot remove {launcher}: "), cm.exception)
        self.assertEqual(len(str(cm.exception).splitlines()), 1)
        self.assertTrue(launcher.exists())


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
        other = self.home / "other"  # not the default data folder, so a dropped --data shows
        code, out, err = self.main(["install", "--data", str(other)])
        self.assertEqual((code, err), (0, ""))
        printed = json.loads(out)
        self.assertEqual(printed["label"], "com.assistant.gate")
        self.assertEqual(printed["mode"], "shadow")
        self.assertEqual(printed["data_dir"], str(other))
        plist = self.home / "Library" / "LaunchAgents" / "com.assistant.gate.plist"  # the defaults, in the temp home
        self.assertTrue(plist.is_file())
        pl = plistlib.loads(plist.read_bytes())
        self.assertEqual(pl["EnvironmentVariables"]["AP2_DATA"], str(other))
        err_log = self.home / "Library" / "Logs" / "assistant" / "gate.err.log"
        self.assertEqual(pl["StandardErrorPath"], str(err_log))
        self.assertTrue(err_log.parent.is_dir())
        self.assertFalse((self.home / "Documents").exists())
        code, out, _ = self.main(["doctor", "--data", str(other)])
        self.assertIn("ok   launchd job:", out)
        code, out, err = self.main(["uninstall"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["label"], "com.assistant.gate")
        self.assertFalse(plist.exists())
        self.assertTrue(err_log.parent.is_dir())
        verbs = [c[0] for c in self.fake.calls]
        self.assertEqual(verbs, ["bootout", "print", "bootstrap", "print", "bootout", "print"])
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
        self.assertEqual(err, "ap2: launchctl bootstrap failed: Bootstrap failed: 5: Input/output error; run "
                              "`ap2 install` again\n")

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
        doctor.checks(str(self.data), agents_dir=install.plist_path().parent, launchctl=fake)
        self.assertEqual(fake.calls, [["print", f"gui/{os.getuid()}/com.assistant.gate"]])
        self.assertEqual(self.fake.calls, [])


if __name__ == "__main__":
    unittest.main()
