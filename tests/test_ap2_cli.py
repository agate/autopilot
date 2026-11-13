from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT
from tests.test_ap2_config import TempHome
from assistant import cli, datadir

AP2 = ROOT / "bin" / "ap2"


def temp_inside_the_accounts_claude() -> bool:
    """True when the temp folder is inside ~/.claude of the account's home in the user database. ap2 then refuses
    every temp data folder, and a subprocess cannot be patched to see another account home."""
    if datadir.pwd is None:
        return False
    try:
        pw_dir = datadir.pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        return False
    if not os.path.isabs(pw_dir):
        return False
    parts = [p.casefold() for p in Path(tempfile.gettempdir()).resolve().parts]
    claude = [p.casefold() for p in (Path(pw_dir) / ".claude").resolve().parts]
    return parts[:len(claude)] == claude


class Ap2CommandTest(unittest.TestCase):
    def setUp(self):
        if temp_inside_the_accounts_claude():
            self.skipTest("the temp folder is inside the account's ~/.claude, so ap2 refuses every temp data folder")
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

    def test_config_into_a_pipe_nobody_reads_exits_1_without_a_traceback(self):
        # `ap2 config | true`: the reader is gone before ap2 writes. The read end is closed before ap2 starts, so
        # every write hits a closed pipe (no race).
        env = {k: v for k, v in os.environ.items() if k != "AP2_DATA"}
        env.update(HOME=str(self.home), AP2_DATA=str(self.data))
        r, w = os.pipe()
        os.close(r)
        try:
            proc = subprocess.Popen([sys.executable, str(AP2), "config"], stdin=subprocess.DEVNULL, stdout=w,
                                    stderr=subprocess.PIPE, env=env)
        finally:
            os.close(w)
        _, err = proc.communicate(timeout=30)
        err = err.decode("utf-8", "replace")
        self.assertEqual(proc.returncode, 1, err)
        self.assertEqual(err, "")
        self.assertTrue((self.data / "config.yaml").is_file())

    @unittest.skipUnless(os.path.exists("/dev/full"), "needs /dev/full (Linux)")
    def test_config_into_a_full_disk_exits_1_with_one_line(self):
        # /dev/full fails every write with ENOSPC (no space left on device).
        env = {k: v for k, v in os.environ.items() if k != "AP2_DATA"}
        env.update(HOME=str(self.home), AP2_DATA=str(self.data))
        with open("/dev/full", "wb") as full:
            r = subprocess.run([sys.executable, str(AP2), "config"], stdin=subprocess.DEVNULL, stdout=full,
                               stderr=subprocess.PIPE, text=True, env=env, errors="replace", timeout=30)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("ap2: cannot write the output", r.stderr)
        self.assertNotIn("Exception ignored", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue((self.data / "config.yaml").is_file())

    def test_non_ascii_error_under_latin1_output_has_no_traceback(self):
        r = self.run_ap2("config", data=self.home / ".claude" / "\u52a9\u624b",
                         env_extra={"PYTHONIOENCODING": "latin-1"}, env_drop=("PYTHONUTF8",))
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, "")
        self.assertIn("ap2:", r.stderr)
        self.assertNotIn("Traceback", r.stderr)


def use_temp_home(test: unittest.TestCase) -> None:
    """Run the test under TempHome, so the real home's ~/.claude (which may hold the temp folder) is never checked."""
    home = TempHome()
    home.__enter__()
    test.addCleanup(home.__exit__)


class ClosedStderrTest(unittest.TestCase):
    # With stderr closed, sys.stderr is None, and print(file=None) would write to stdout instead.
    def setUp(self):
        use_temp_home(self)

    def run_main(self, argv):
        out = io.StringIO()
        with mock.patch.object(sys, "stderr", None), contextlib.redirect_stdout(out):
            code = cli.main(argv)
        return code, out.getvalue()

    def test_error_is_not_printed_to_stdout(self):
        data = Path(tempfile.mkdtemp(prefix="ap2-cli-")).resolve()
        self.addCleanup(shutil.rmtree, data, True)
        (data / "config.yaml").write_text("mode: loud\n")
        self.assertEqual(self.run_main(["config", "--data", str(data)]), (1, ""))

    def test_usage_is_not_printed_to_stdout(self):
        self.assertEqual(self.run_main([]), (2, ""))


class OutputWriteErrorTest(unittest.TestCase):
    # In-process, so it runs where /dev/full does not exist (macOS). The fake stdout's fileno() is a file this
    # test owns, so pointing it at devnull cannot touch the test runner's real stdout.
    class FullStdout:
        encoding = "utf-8"

        def __init__(self, fd):
            self.fd = fd

        def write(self, _text):
            raise OSError(28, "No space left on device")

        def flush(self):
            raise OSError(28, "No space left on device")

        def fileno(self):
            return self.fd

    def setUp(self):
        use_temp_home(self)

    def test_disk_full_returns_1_with_one_line_and_points_stdout_at_devnull(self):
        tmp = Path(tempfile.mkdtemp(prefix="ap2-cli-")).resolve()
        self.addCleanup(shutil.rmtree, tmp, True)
        fd = os.open(tmp / "out", os.O_WRONLY | os.O_CREAT)
        self.addCleanup(os.close, fd)
        err = io.StringIO()
        with mock.patch.object(sys, "stdout", self.FullStdout(fd)), mock.patch.object(sys, "stderr", err):
            code = cli.main(["config", "--data", str(tmp / "data")])
        self.assertEqual(code, 1)
        self.assertEqual(err.getvalue(), "ap2: cannot write the output: [Errno 28] No space left on device\n")
        self.assertEqual(os.fstat(fd).st_rdev, os.stat(os.devnull).st_rdev)
        self.assertTrue((tmp / "data" / "config.yaml").is_file())

    def test_disk_full_on_a_stream_without_a_file_descriptor_still_returns_1(self):
        tmp = Path(tempfile.mkdtemp(prefix="ap2-cli-")).resolve()
        self.addCleanup(shutil.rmtree, tmp, True)
        out = self.FullStdout(None)
        out.fileno = mock.Mock(side_effect=io.UnsupportedOperation("fileno"))
        err = io.StringIO()
        with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
            code = cli.main(["config", "--data", str(tmp / "data")])
        self.assertEqual(code, 1)
        self.assertIn("ap2: cannot write the output", err.getvalue())


class HelpAndUsageOutputTest(unittest.TestCase):
    """argparse's own output (help, usage, argument errors) behaves like `ap2 config`'s output when its reader
    goes away, a write fails, or the stream is closed."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="ap2-cli-")).resolve()
        self.addCleanup(shutil.rmtree, tmp, True)
        self.home = tmp / "home"
        self.home.mkdir()
        self.env = {k: v for k, v in os.environ.items() if k != "AP2_DATA"}
        self.env.update(HOME=str(self.home), AP2_DATA=str(tmp / "data"))
        # Python 3.14 argparse colors help and usage when FORCE_COLOR is set; PYTHON_COLORS=0 wins over it.
        self.env["PYTHON_COLORS"] = "0"

    def run_into_a_pipe_nobody_reads(self, stream, *args):
        """Run ap2 with `stream` ("stdout" or "stderr") writing into a pipe whose read end is closed before ap2
        starts, so every write hits a closed pipe (no race). Returns (exit code, the other stream's text)."""
        r, w = os.pipe()
        os.close(r)
        kw = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, stream: w}
        try:
            proc = subprocess.Popen([sys.executable, str(AP2), *args], stdin=subprocess.DEVNULL, env=self.env, **kw)
        finally:
            os.close(w)
        out, err = proc.communicate(timeout=30)
        other = err if stream == "stdout" else out
        return proc.returncode, other.decode("utf-8", "replace")

    def test_help_into_a_pipe_nobody_reads_exits_0_with_nothing_on_stderr(self):
        for args in (["--help"], ["-h"], ["config", "--help"]):
            with self.subTest(args=args):
                code, err = self.run_into_a_pipe_nobody_reads("stdout", *args)
                self.assertEqual((code, err), (0, ""))

    def test_usage_into_a_pipe_nobody_reads_exits_2_with_nothing_on_stdout(self):
        # `ap2` prints the usage to stderr; stderr's reader is gone.
        code, out = self.run_into_a_pipe_nobody_reads("stderr")
        self.assertEqual((code, out), (2, ""))

    def test_argument_errors_into_a_pipe_nobody_reads_exit_2_with_nothing_on_stdout(self):
        for args in (["nosuch"], ["config", "--bad"]):
            with self.subTest(args=args):
                code, out = self.run_into_a_pipe_nobody_reads("stderr", *args)
                self.assertEqual((code, out), (2, ""))

    def test_argument_errors_keep_argparse_message_and_exit_2(self):
        for args, says in ((["nosuch"], "invalid choice: 'nosuch'"), (["config", "--bad"], "unrecognized arguments: --bad")):
            with self.subTest(args=args):
                r = subprocess.run([sys.executable, str(AP2), *args], stdin=subprocess.DEVNULL, capture_output=True,
                                   text=True, env=self.env, errors="replace", timeout=30)
                self.assertEqual(r.returncode, 2)
                self.assertEqual(r.stdout, "")
                self.assertTrue(r.stderr.startswith("usage: ap2"), r.stderr)
                self.assertIn("ap2: error: ", r.stderr)
                self.assertIn(says, r.stderr)

    def test_help_prints_to_stdout_and_exits_0(self):
        r = subprocess.run([sys.executable, str(AP2), "config", "--help"], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, env=self.env, errors="replace", timeout=30)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertIn("usage: ap2 config", r.stdout)
        self.assertIn("--data PATH", r.stdout)

    @unittest.skipUnless(os.path.exists("/dev/full"), "needs /dev/full (Linux)")
    def test_help_into_a_full_disk_exits_1_with_one_line(self):
        for args in (["--help"], ["config", "--help"]):
            with self.subTest(args=args), open("/dev/full", "wb") as full:
                r = subprocess.run([sys.executable, str(AP2), *args], stdin=subprocess.DEVNULL, stdout=full,
                                   stderr=subprocess.PIPE, text=True, env=self.env, errors="replace", timeout=30)
                self.assertEqual(r.returncode, 1, r.stderr)
                self.assertRegex(r.stderr, r"\Aap2: cannot write the output: .+\n\Z")

    def test_help_and_usage_with_stdout_or_stderr_closed_exit_normally_without_a_traceback(self):
        # '>&-' / '2>&-' close the stream, so Python starts with sys.stdout / sys.stderr set to None.
        for redirect, args, code in ((">&-", "--help", 0), (">&-", "config --help", 0), ("2>&-", "", 2),
                                     ("2>&-", "nosuch", 2)):
            with self.subTest(redirect=redirect, args=args):
                # The shell warns on stderr when LC_ALL names a locale that is not installed, so leave LC_ALL out.
                r = subprocess.run(["sh", "-c", f'"$0" "$1" {args} {redirect}', sys.executable, str(AP2)],
                                   stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
                                   env={k: v for k, v in self.env.items() if k != "LC_ALL"}, timeout=30)
                self.assertEqual(r.returncode, code, r.stderr)
                self.assertEqual(r.stdout + r.stderr, "")


class HelpAndUsageInProcessTest(unittest.TestCase):
    # In-process, so they run where /dev/full does not exist (macOS). The fake stream's fileno() is a file this
    # test owns, so pointing it at devnull cannot touch the test runner's real streams.
    FullStream = OutputWriteErrorTest.FullStdout

    def setUp(self):
        use_temp_home(self)
        tmp = Path(tempfile.mkdtemp(prefix="ap2-cli-")).resolve()
        self.addCleanup(shutil.rmtree, tmp, True)
        self.fd = os.open(tmp / "out", os.O_WRONLY | os.O_CREAT)
        self.addCleanup(os.close, self.fd)

    def assert_exits(self, argv, code):
        with self.assertRaises(SystemExit) as cm:
            cli.main(argv)
        self.assertEqual(cm.exception.code, code)

    def test_help_on_a_full_disk_returns_1_with_one_line_and_points_stdout_at_devnull(self):
        for argv in (["--help"], ["config", "--help"]):
            with self.subTest(argv=argv):
                err = io.StringIO()
                with mock.patch.object(sys, "stdout", self.FullStream(self.fd)), mock.patch.object(sys, "stderr", err):
                    code = cli.main(argv)
                self.assertEqual(code, 1)
                self.assertEqual(err.getvalue(), "ap2: cannot write the output: [Errno 28] No space left on device\n")
                self.assertEqual(os.fstat(self.fd).st_rdev, os.stat(os.devnull).st_rdev)

    def test_usage_on_a_full_stderr_returns_1_without_raising(self):
        for argv in ([], ["nosuch"]):
            with self.subTest(argv=argv):
                with mock.patch.object(sys, "stdout", io.StringIO()) as out, \
                        mock.patch.object(sys, "stderr", self.FullStream(self.fd)):
                    code = cli.main(argv)
                self.assertEqual((code, out.getvalue()), (1, ""))
                self.assertEqual(os.fstat(self.fd).st_rdev, os.stat(os.devnull).st_rdev)

    def test_help_with_stdout_closed_prints_nothing_and_exits_0(self):
        for argv in (["--help"], ["config", "--help"]):
            with self.subTest(argv=argv):
                err = io.StringIO()
                with mock.patch.object(sys, "stdout", None), mock.patch.object(sys, "stderr", err):
                    self.assert_exits(argv, 0)
                self.assertEqual(err.getvalue(), "")

    def test_usage_and_errors_with_stderr_closed_print_nothing(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stderr", None), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main([]), 2)
            self.assert_exits(["nosuch"], 2)
            self.assert_exits(["config", "--bad"], 2)
        self.assertEqual(out.getvalue(), "")

    def test_serve_help_names_every_write_and_what_it_may_run(self):
        out = io.StringIO()
        # A wide fixed width: argparse wraps at the terminal's width and may break "Read-only" at its hyphen.
        with mock.patch.dict(os.environ, {"COLUMNS": "1000", "PYTHON_COLORS": "0"}), \
                    contextlib.redirect_stdout(out):
            self.assert_exits(["serve", "--help"], 0)
        words = " ".join(out.getvalue().split())
        for want in ("Read-only, but for saving a question on a card, answering today's cap card and switching the "
                     "access profile;",
                     "in live mode a pause answer stops the running workers,",
                     "and in both modes a switch to a gateway profile may run its models command."):
            with self.subTest(want):
                self.assertIn(want, words)

    def test_install_help_names_v2s_own_job_and_who_runs_it(self):
        def words(argv):
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"COLUMNS": "1000", "PYTHON_COLORS": "0"}), \
                    contextlib.redirect_stdout(out):
                self.assert_exits(argv, 0)
            return " ".join(out.getvalue().split())

        install_help = words(["install", "--help"])
        for want in ("Write and load v2's own launchd job, com.assistant.gate, which runs `ap2 gate` every 60 seconds",
                     "It never touches v1's or secretary's job.", "Only the user runs it, never a worker.",
                     "a new config.yaml says mode: shadow"):
            with self.subTest(want):
                self.assertIn(want, install_help)
        uninstall_help = words(["uninstall", "--help"])
        for want in ("com.assistant.gate", "never touches v1's or secretary's job", "the data folder is kept",
                     "Only the user runs it."):
            with self.subTest(want):
                self.assertIn(want, uninstall_help)
        self.assertIn("Read-only: creates nothing.", words(["doctor", "--help"]))
        listing = words(["--help"])
        for command in ("install", "uninstall", "doctor"):
            with self.subTest(command):
                self.assertIn(command, cli.COMMANDS)
                self.assertRegex(listing, rf"\b{command} \w")

    def test_help_with_both_streams_closed_exits_0(self):
        with mock.patch.object(sys, "stdout", None), mock.patch.object(sys, "stderr", None):
            self.assert_exits(["--help"], 0)
            self.assert_exits(["nosuch"], 2)


class ConfigErrorLineOutputTest(unittest.TestCase):
    """The `ap2: <message>` line for a refused config or data folder follows the output rules: when stderr's
    reader is gone or its write fails, ap2 still exits 1, with no traceback and no `Exception ignored` line."""

    run_into_a_pipe_nobody_reads = HelpAndUsageOutputTest.run_into_a_pipe_nobody_reads

    def setUp(self):
        if temp_inside_the_accounts_claude():
            self.skipTest("the temp folder is inside the account's ~/.claude, so ap2 refuses every temp data folder")
        tmp = Path(tempfile.mkdtemp(prefix="ap2-cli-")).resolve()
        self.addCleanup(shutil.rmtree, tmp, True)
        self.home = tmp / "home"
        self.home.mkdir()
        self.bad = tmp / "bad"  # a data folder whose config.yaml is invalid
        self.bad.mkdir()
        (self.bad / "config.yaml").write_text("mode: loud\n")
        self.refused = self.home / ".claude" / "x"  # a data folder ap2 refuses
        self.env = {k: v for k, v in os.environ.items() if k != "AP2_DATA"}
        self.env.update(HOME=str(self.home), PYTHON_COLORS="0")

    def cases(self):
        return (("invalid config", ["config", "--data", str(self.bad)]),
                ("refused data folder", ["config", "--data", str(self.refused)]))

    def test_error_line_into_a_pipe_nobody_reads_exits_1_with_nothing_on_stdout(self):
        for name, args in self.cases():
            with self.subTest(name):
                code, out = self.run_into_a_pipe_nobody_reads("stderr", *args)
                self.assertEqual((code, out), (1, ""))
        self.assertFalse((self.home / ".claude").exists())

    @unittest.skipUnless(os.path.exists("/dev/full"), "needs /dev/full (Linux)")
    def test_error_line_into_a_full_disk_exits_1_with_nothing_on_stdout(self):
        for name, args in self.cases():
            with self.subTest(name), open("/dev/full", "wb") as full:
                r = subprocess.run([sys.executable, str(AP2), *args], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=full, text=True, env=self.env, errors="replace",
                                   timeout=30)
                self.assertEqual((r.returncode, r.stdout), (1, ""))


class ConfigErrorLineInProcessTest(unittest.TestCase):
    # In-process, so they run where /dev/full does not exist (macOS), and an exception out of main fails the test
    # instead of being lost in a closed pipe. The fake stream's fileno() is a fresh file each subtest owns, so pointing
    # it at devnull cannot touch the test runner's real streams, and one subtest's devnull cannot pass another's check.
    FullStream = OutputWriteErrorTest.FullStdout

    class GoneStream(FullStream):
        """A stderr whose reader went away."""

        def write(self, _text):
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self):
            raise BrokenPipeError(32, "Broken pipe")

    def setUp(self):
        home = TempHome()
        self.home = home.__enter__()
        self.addCleanup(home.__exit__)
        self.tmp = tmp = Path(tempfile.mkdtemp(prefix="ap2-cli-")).resolve()
        self.addCleanup(shutil.rmtree, tmp, True)
        bad = tmp / "bad"
        bad.mkdir()
        (bad / "config.yaml").write_text("mode: loud\n")
        self.cases = (("invalid config", ["config", "--data", str(bad)]),
                      ("refused data folder", ["config", "--data", str(self.home / ".claude" / "x")]))

    def fresh_stderr_file(self):
        fd, _ = tempfile.mkstemp(dir=self.tmp)
        self.addCleanup(os.close, fd)  # registered after rmtree, so it closes first
        self.assertNotEqual(os.fstat(fd).st_rdev, os.stat(os.devnull).st_rdev)
        return fd

    def run_main(self, argv, stderr):
        with mock.patch.object(sys, "stdout", io.StringIO()) as out, mock.patch.object(sys, "stderr", stderr):
            code = cli.main(argv)
        return code, out.getvalue()

    def test_error_line_with_stderr_reader_gone_returns_1_without_raising(self):
        for name, argv in self.cases:
            with self.subTest(name):
                fd = self.fresh_stderr_file()
                self.assertEqual(self.run_main(argv, self.GoneStream(fd)), (1, ""))
                self.assertEqual(os.fstat(fd).st_rdev, os.stat(os.devnull).st_rdev)

    def test_error_line_on_a_full_stderr_returns_1_without_raising_and_points_it_at_devnull(self):
        for name, argv in self.cases:
            with self.subTest(name):
                fd = self.fresh_stderr_file()
                self.assertEqual(self.run_main(argv, self.FullStream(fd)), (1, ""))
                self.assertEqual(os.fstat(fd).st_rdev, os.stat(os.devnull).st_rdev)

    def test_error_line_on_a_full_stderr_without_a_file_descriptor_returns_1(self):
        err = self.FullStream(None)
        err.fileno = mock.Mock(side_effect=io.UnsupportedOperation("fileno"))
        for name, argv in self.cases:
            with self.subTest(name):
                self.assertEqual(self.run_main(argv, err), (1, ""))

    def test_error_line_on_a_working_stderr_is_one_line(self):
        for name, argv in self.cases:
            with self.subTest(name):
                err = io.StringIO()
                self.assertEqual(self.run_main(argv, err), (1, ""))
                self.assertRegex(err.getvalue(), r"\Aap2: [^\n]+\n\Z")
                self.assertNotIn("cannot write the output", err.getvalue())


class ToDevnullTest(unittest.TestCase):
    """Pointing a failed stream at devnull never raises and never leaks the devnull fd it opens."""
    FullStream = OutputWriteErrorTest.FullStdout
    GoneStream = ConfigErrorLineInProcessTest.GoneStream

    def setUp(self):
        use_temp_home(self)
        self.tmp = tmp = Path(tempfile.mkdtemp(prefix="ap2-cli-")).resolve()
        self.addCleanup(shutil.rmtree, tmp, True)
        self.fd = os.open(tmp / "out", os.O_WRONLY | os.O_CREAT)
        self.addCleanup(os.close, self.fd)
        self.real_open = os.open
        self.opened = []  # every fd os.open returned for devnull

    def spy_open(self, path, *args, **kwargs):
        fd = self.real_open(path, *args, **kwargs)
        if path == os.devnull:
            self.opened.append(fd)
        return fd

    def emfile_on_devnull(self, path, *args, **kwargs):
        # only devnull fails, so the data folder setup in `main` still opens its files
        if path == os.devnull:
            raise OSError(errno.EMFILE, "Too many open files")
        return self.real_open(path, *args, **kwargs)

    def assert_closed(self, fd):
        with self.assertRaises(OSError) as cm:
            os.fstat(fd)
        self.assertEqual(cm.exception.errno, errno.EBADF)

    def test_closes_the_devnull_fd_it_opens(self):
        with mock.patch.object(os, "open", side_effect=self.spy_open), \
                mock.patch.object(os, "close", wraps=os.close) as close:
            cli._to_devnull(self.FullStream(self.fd))
        self.assertEqual(len(self.opened), 1)
        close.assert_called_once_with(self.opened[0])
        self.assert_closed(self.opened[0])
        self.assertEqual(os.fstat(self.fd).st_rdev, os.stat(os.devnull).st_rdev)

    def test_does_not_close_the_stream_fd_when_devnull_comes_back_as_it(self):
        with mock.patch.object(os, "open", return_value=self.fd), \
                mock.patch.object(os, "close", wraps=os.close) as close:
            cli._to_devnull(self.FullStream(self.fd))
        close.assert_not_called()
        os.fstat(self.fd)  # still open

    def test_returns_quietly_when_devnull_cannot_be_opened(self):
        with mock.patch.object(os, "open", side_effect=self.emfile_on_devnull), \
                mock.patch.object(os, "dup2") as dup2:
            self.assertIsNone(cli._to_devnull(self.FullStream(self.fd)))
        dup2.assert_not_called()
        self.assertNotEqual(os.fstat(self.fd).st_rdev, os.stat(os.devnull).st_rdev)

    def test_returns_quietly_and_closes_the_devnull_fd_when_dup2_fails(self):
        with mock.patch.object(os, "open", side_effect=self.spy_open), \
                mock.patch.object(os, "dup2", side_effect=OSError(errno.EBADF, "Bad file descriptor")):
            self.assertIsNone(cli._to_devnull(self.FullStream(self.fd)))
        self.assertEqual(len(self.opened), 1)
        self.assert_closed(self.opened[0])

    def test_returns_quietly_when_closing_the_devnull_fd_fails(self):
        with mock.patch.object(os, "open", side_effect=self.spy_open), \
                mock.patch.object(os, "close", side_effect=OSError(errno.EIO, "Input/output error")) as close:
            result = cli._to_devnull(self.FullStream(self.fd))
        for fd in self.opened:
            os.close(fd)  # the patched close did not close it; do not leak it in the test
        self.assertIsNone(result)
        self.assertEqual(len(self.opened), 1)
        close.assert_called_once_with(self.opened[0])

    def test_write_still_raises_the_write_error_when_devnull_cannot_be_opened(self):
        with mock.patch.object(os, "open", side_effect=self.emfile_on_devnull):
            with self.assertRaises(BrokenPipeError):
                cli._write("x", self.GoneStream(self.fd))
            with self.assertRaises(cli._OutputError) as cm:
                cli._write("x", self.FullStream(self.fd))
        self.assertEqual(cm.exception.__cause__.errno, errno.ENOSPC)

    def test_write_still_raises_the_write_error_when_dup2_fails(self):
        with mock.patch.object(os, "open", side_effect=self.spy_open), \
                mock.patch.object(os, "dup2", side_effect=OSError(errno.EBADF, "Bad file descriptor")):
            with self.assertRaises(BrokenPipeError):
                cli._write("x", self.GoneStream(self.fd))
            with self.assertRaises(cli._OutputError):
                cli._write("x", self.FullStream(self.fd))
        self.assertEqual(len(self.opened), 2)
        for fd in self.opened:
            self.assert_closed(fd)

    def test_config_with_reader_gone_and_devnull_unopenable_returns_1_with_nothing_on_stderr(self):
        err = io.StringIO()
        with mock.patch.object(sys, "stdout", self.GoneStream(self.fd)), mock.patch.object(sys, "stderr", err), \
                mock.patch.object(os, "open", side_effect=self.emfile_on_devnull):
            code = cli.main(["config", "--data", str(self.tmp / "data")])
        self.assertEqual((code, err.getvalue()), (1, ""))
        self.assertTrue((self.tmp / "data" / "config.yaml").is_file())

    def test_config_on_a_full_disk_and_devnull_unopenable_returns_1_with_one_line(self):
        err = io.StringIO()
        with mock.patch.object(sys, "stdout", self.FullStream(self.fd)), mock.patch.object(sys, "stderr", err), \
                mock.patch.object(os, "open", side_effect=self.emfile_on_devnull):
            code = cli.main(["config", "--data", str(self.tmp / "data")])
        self.assertEqual(code, 1)
        self.assertEqual(err.getvalue(), "ap2: cannot write the output: [Errno 28] No space left on device\n")


if __name__ == "__main__":
    unittest.main()
