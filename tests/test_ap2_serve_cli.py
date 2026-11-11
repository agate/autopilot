"""`ap2 serve [--port N] [--data PATH]`, run as a subprocess against a temp data folder (the local page)."""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from tests.helpers import ROOT
from tests.test_ap2_cli import temp_inside_the_accounts_claude

AP2 = ROOT / "bin" / "ap2"
# No proxy: an http_proxy in the environment would otherwise route requests for 127.0.0.1 elsewhere.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Ap2ServeCommandTest(unittest.TestCase):
    def setUp(self):
        if temp_inside_the_accounts_claude():
            self.skipTest("the temp folder is inside the account's ~/.claude, so ap2 refuses every temp data folder")
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-serve-cli-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.data = self.tmp / "data"

    def env(self):
        env = {k: v for k, v in os.environ.items() if k not in ("AP2_DATA", "LC_ALL")}
        # HOME points at an empty folder, so the real home is never reached; PYTHON_COLORS=0 keeps Python 3.14's
        # argparse from coloring its usage and errors.
        env.update(HOME=str(self.home), AP2_DATA=str(self.data), PYTHON_COLORS="0")
        return env

    def run_ap2(self, *args):
        return subprocess.run([sys.executable, str(AP2), *args], capture_output=True, text=True, env=self.env(),
                              errors="replace", timeout=30)

    def start(self, *args) -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, str(AP2), "serve", *args], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=self.env(), errors="replace")

        def stop():
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=30)
        self.addCleanup(stop)
        return proc

    def first_line(self, proc) -> dict:
        line = proc.stdout.readline()
        self.assertTrue(line, proc.stderr.read() if proc.poll() is not None else "no output")
        return json.loads(line)

    def test_serve_prints_one_line_serves_and_stops_on_sigterm(self):
        proc = self.start("--port", "0")
        out = self.first_line(proc)
        self.assertEqual(set(out), {"url", "data_dir", "mode"})
        self.assertRegex(out["url"], "^http://" + r"127\.0\.0\.1:[1-9][0-9]*/$")
        self.assertEqual(out["data_dir"], str(self.data))
        self.assertEqual(out["mode"], "shadow")
        self.assertTrue((self.data / "config.yaml").is_file())  # set up like ap2 card
        with OPENER.open(out["url"] + "api/state", timeout=10) as r:
            state = json.loads(r.read())
        self.assertEqual(state["url"], out["url"])
        self.assertEqual(state["lanes"], {"needs": 0, "running": 0, "planned": 0, "done": 0})
        with OPENER.open(out["url"], timeout=10) as r:
            self.assertEqual(r.headers["Content-Type"], "text/html; charset=utf-8")
        proc.send_signal(signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, stderr)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")  # 2xx replies are not logged

    def test_ctrl_c_exits_0(self):
        proc = self.start("--port", "0", "--data", str(self.tmp / "other"))
        out = self.first_line(proc)
        self.assertEqual(out["data_dir"], str(self.tmp / "other"))
        proc.send_signal(signal.SIGINT)
        _, stderr = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, stderr)
        self.assertNotIn("Traceback", stderr)

    def test_an_error_reply_is_logged_with_a_utc_time_first(self):
        proc = self.start("--port", "0")
        out = self.first_line(proc)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            OPENER.open(out["url"] + "api/nope", timeout=10)
        self.assertEqual(cm.exception.code, 404)
        cm.exception.close()
        proc.send_signal(signal.SIGTERM)
        _, stderr = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0)
        lines = stderr.splitlines()
        self.assertEqual(len(lines), 1, stderr)
        self.assertRegex(lines[0], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ 127\.0\.0\.1 \"GET /api/nope HTTP/1\.1\" 404$")

    def test_there_is_no_host_option(self):
        r = self.run_ap2("serve", "--host", "0.0.0.0")
        self.assertEqual(r.returncode, 2)
        self.assertIn("unrecognized arguments: --host", r.stderr)
        self.assertFalse(self.data.exists())

    def test_a_port_outside_0_to_65535_is_an_argument_error(self):
        for port in ("65536", "-1", "ten", "", "+80", "1e3"):
            with self.subTest(port):
                r = self.run_ap2("serve", "--port", port)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn("--port", r.stderr)
        self.assertFalse(self.data.exists())

    def test_a_port_in_use_is_one_line_and_exit_1(self):
        held = socket.socket()
        self.addCleanup(held.close)
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        r = self.run_ap2("serve", "--port", str(port))
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, "")
        lines = r.stderr.splitlines()
        self.assertEqual(len(lines), 1, r.stderr)
        self.assertTrue(lines[0].startswith(f"ap2: cannot listen on 127.0.0.1:{port}: "), lines[0])

    def test_a_bad_config_is_refused_before_listening(self):
        self.data.mkdir()
        (self.data / "config.yaml").write_text("mode: loud\n", encoding="utf-8")
        r = self.run_ap2("serve", "--port", "0")
        self.assertEqual(r.returncode, 1)
        self.assertRegex(r.stderr, r"^ap2: .*config\.yaml: line 1: mode must be shadow or live")

    def test_help_lists_serve(self):
        r = self.run_ap2("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("serve", r.stdout)
        r = self.run_ap2("serve", "--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("--port", r.stdout)
        self.assertIn("4317", r.stdout)


if __name__ == "__main__":
    unittest.main()
