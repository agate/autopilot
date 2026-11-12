from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT
from tests.test_ap2_cli import temp_inside_the_accounts_claude

AP2 = ROOT / "bin" / "ap2"
FIXTURES = ROOT / "tests" / "fixtures" / "ap2" / "sources"
FAKE = "import sys; sys.stdout.buffer.write(open(sys.argv[1], 'rb').read())\n"


def tree(folder: Path) -> dict:
    return {str(p.relative_to(folder)): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


class SourceCommandTest(unittest.TestCase):
    def setUp(self):
        if temp_inside_the_accounts_claude():
            self.skipTest("the temp folder is inside the account's ~/.claude, so ap2 refuses every temp data folder")
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-source-cli-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.data = self.tmp / "data"
        (self.data / "sources").mkdir(parents=True)
        self.fake = self.tmp / "fake.py"
        self.fake.write_text(FAKE)
        self.reply = self.tmp / "incidents.json"
        shutil.copy(FIXTURES / "replies" / "status-incidents.json", self.reply)
        run = " ".join(shlex.quote(str(a)) for a in (sys.executable, self.fake, self.reply))
        text = (FIXTURES / "user" / "vendor-status.yaml").read_text().replace(
            "run: curl -s https://status.example.com/api/v2/incidents/unresolved.json", f"run: '{run}'")
        # Names no built-in file will take, so the plugin's own sources/ never changes what these tests see.
        (self.data / "sources" / "t-vendor-status.yaml").write_text(
            text.replace("source: vendor-status", "source: t-vendor-status"))
        (self.data / "sources" / "t-vendor-fast.yaml").write_text("extends: t-vendor-status\nread:\n  every: 1m\n")

    def env(self):
        env = {k: v for k, v in os.environ.items() if k not in ("AP2_DATA", "LC_ALL")}
        env.update(HOME=str(self.home), AP2_DATA=str(self.data), PYTHONIOENCODING="utf-8", PYTHON_COLORS="0")
        return env

    def run_ap2(self, *args):
        return subprocess.run([sys.executable, str(AP2), *args], capture_output=True, text=True, env=self.env(),
                              encoding="utf-8", errors="replace", timeout=60)

    def run_ap2_fed(self, offered: int, *args):
        """Run ap2 with `offered` bytes on a pipe to its stdin; return (the run, how many bytes it took in)."""
        proc = subprocess.Popen([sys.executable, str(AP2), *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=self.env())
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        written = [0]

        def feed():
            chunk = b" " * (1024 * 1024)
            try:
                while written[0] < offered:
                    proc.stdin.write(chunk)
                    written[0] += len(chunk)
            except OSError:  # ap2 closed its end: EPIPE
                pass
            finally:
                with contextlib.suppress(OSError):
                    proc.stdin.close()

        writer = threading.Thread(target=feed, daemon=True)
        writer.start()
        with proc.stdout, proc.stderr:
            out, err = proc.stdout.read(), proc.stderr.read()
        proc.wait(timeout=60)
        writer.join(60)
        self.assertFalse(writer.is_alive())
        return subprocess.CompletedProcess(args, proc.returncode, out.decode(), err.decode()), written[0]

    def ok(self, *args):
        r = self.run_ap2(*args)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        return json.loads(r.stdout)

    def fails(self, *args):
        r = self.run_ap2(*args)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(r.stdout, "")
        lines = r.stderr.splitlines()
        self.assertEqual(len(lines), 1, r.stderr)
        self.assertTrue(lines[0].startswith("ap2: "), r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        return lines[0]

    def test_list(self):
        out = {s["source"]: s for s in self.ok("source", "list")}
        self.assertEqual(out["t-vendor-status"], {
            "source": "t-vendor-status", "brings": "messages", "via": "command", "fill": "paths", "every": "5m",
            "origin": "user", "file": str(self.data / "sources" / "t-vendor-status.yaml")})
        self.assertEqual(out["t-vendor-fast"]["origin"], "user, extends t-vendor-status")
        self.assertEqual(out["t-vendor-fast"]["every"], "1m")

    def test_show_prints_the_merged_file(self):
        out = self.ok("source", "show", "t-vendor-fast")
        self.assertEqual(out["source"], "t-vendor-fast")
        self.assertEqual(out["read"]["every"], "1m")
        self.assertEqual(out["form"], {"id": ".incidents[].id", "at": ".incidents[].created_at",
                                       "text": ".incidents[].name"})
        self.assertNotIn("extends", out)

    def test_read_makes_cards_and_saves_the_raw_output(self):
        out = self.ok("source", "read", "t-vendor-status")
        self.assertFalse(out["dry_run"])
        self.assertEqual([i["action"] for i in out["items"]], ["add", "add"])
        self.assertEqual(out["dropped"], [{"item": 2, "reason": "form.text missing"}])
        self.assertEqual((self.data / out["raw"]).read_bytes(), self.reply.read_bytes())
        cards = self.ok("card", "list")
        self.assertEqual(sorted(c["key"] for c in cards), ["inc01abcd", "inc02efgh"])
        again = self.ok("source", "read", "t-vendor-status")
        self.assertEqual([i["action"] for i in again["items"]], ["unchanged", "unchanged"])
        self.assertEqual(self.ok("card", "list"), cards)

    def test_dry_run_saves_nothing(self):
        before = tree(self.data)
        out = self.ok("source", "read", "t-vendor-status", "--dry-run")
        self.assertTrue(out["dry_run"])
        self.assertIsNone(out["raw"])
        self.assertEqual([(i["action"], i["would_add"]["kind"], i["would_add"]["title"]) for i in out["items"]],
                         [("add", "alert", {"en": "Elevated error rates on the payments API"}),
                          ("add", "alert", {"en": "Delayed webhooks"})])
        self.assertEqual(tree(self.data), before)  # not even config.yaml or the folder layout

    def test_read_raw_takes_a_saved_reply(self):
        (self.data / "sources" / "t-tracker.yaml").write_text(
            "source: t-tracker\nbrings: messages\nread: { via: mcp, server: tracker, tool: search }\n"
            "fill: paths\nform: { id: .key, at: .fields.updated, text: .fields.summary }\n"
            'link: "https://tracker.example.com/browse/{id}"\nsort: { all: question }\n')
        out = self.ok("source", "read", "t-tracker", "--raw", str(FIXTURES / "replies" / "jira-search.json"))
        self.assertEqual([i["key"] for i in out["items"]], ["t-tracker:PROJ-123", "t-tracker:PROJ-124"])
        self.assertTrue(out["raw"].startswith("sources/.raw/t-tracker/"))
        line = self.fails("source", "read", "t-tracker")
        self.assertIn("read.via is mcp; only a scan session can make that read", line)

    TRACKER = ("source: t-tracker\nbrings: messages\nread: { via: mcp, server: tracker, tool: search }\n"
               "fill: paths\nform: { id: .key, at: .fields.updated, text: .fields.summary }\n"
               'link: "https://tracker.example.com/browse/{id}"\nsort: { all: question }\n')

    def test_read_raw_dry_run_saves_nothing(self):
        (self.data / "sources" / "t-tracker.yaml").write_text(self.TRACKER)
        before = tree(self.data)
        out = self.ok("source", "read", "t-tracker", "--raw", str(FIXTURES / "replies" / "jira-search.json"),
                      "--dry-run")
        self.assertEqual(tree(self.data), before)
        self.assertTrue(out["dry_run"])
        self.assertIsNone(out["raw"])
        self.assertEqual([(i["action"], i["would_add"]["kind"]) for i in out["items"]],
                         [("add", "question"), ("add", "question")])

    def test_a_reply_nested_too_deeply_is_one_line(self):
        (self.data / "sources" / "t-tracker.yaml").write_text(self.TRACKER)
        deep = self.tmp / "deep.json"
        deep.write_text("[" * 200000 + "]" * 200000)
        self.assertIn("the output is not JSON", self.fails("source", "read", "t-tracker", "--raw", str(deep)))
        self.assertIn(f"cannot read the records {str(deep)!r}",
                      self.fails("source", "read", "t-tracker", "--raw", str(deep), "--records", str(deep)))

    def test_a_records_file_past_the_output_cap_is_refused_after_one_byte_more_is_read(self):
        from assistant.engine import MAX_OUTPUT
        (self.data / "sources" / "t-tracker.yaml").write_text(self.TRACKER)
        raw = str(FIXTURES / "replies" / "jira-search.json")
        records = self.tmp / "records.json"
        with open(records, "wb") as f:
            f.truncate(MAX_OUTPUT + 1)  # sparse: instant and takes no disk
        line = self.fails("source", "read", "t-tracker", "--raw", raw, "--records", str(records))
        self.assertEqual(line, f"ap2: the records file {str(records)!r} is larger than {MAX_OUTPUT} bytes")
        # A pipe's size from stat is 0, so only a read with a byte limit stops it: ap2 takes the cap and one byte
        # more, of far more on offer, and stops.
        r, written = self.run_ap2_fed(MAX_OUTPUT + 32 * 1024 * 1024, "source", "read", "t-tracker", "--raw", raw,
                                      "--records", "/dev/stdin")
        self.assertEqual((r.returncode, r.stdout), (1, ""))
        self.assertEqual(r.stderr, f"ap2: the records file '/dev/stdin' is larger than {MAX_OUTPUT} bytes\n")
        self.assertLess(written, MAX_OUTPUT + 16 * 1024 * 1024)
        self.assertFalse((self.data / "sources" / ".raw").exists())

    def test_a_raw_stream_past_the_output_cap_is_refused(self):
        from assistant.engine import MAX_OUTPUT
        (self.data / "sources" / "t-tracker.yaml").write_text(self.TRACKER)
        r, written = self.run_ap2_fed(MAX_OUTPUT + 32 * 1024 * 1024, "source", "read", "t-tracker", "--raw",
                                      "/dev/stdin")
        self.assertEqual((r.returncode, r.stdout), (1, ""))
        self.assertEqual(r.stderr, f"ap2: t-tracker: the saved output /dev/stdin is larger than {MAX_OUTPUT} "
                                   "bytes\n")
        self.assertLess(written, MAX_OUTPUT + 16 * 1024 * 1024)
        self.assertFalse((self.data / "sources" / ".raw").exists())  # nothing copied

    def test_a_records_file_of_exactly_the_cap_is_taken(self):
        from assistant import cli
        text = (FIXTURES / "plugin" / "slack-mentions.yaml").read_text()
        (self.data / "sources" / "t-slack-mentions.yaml").write_text(
            text.replace("source: slack-mentions", "source: t-slack-mentions"))
        raw = self.tmp / "slack.txt"
        raw.write_text("Message_ts: 1730000000.123456\n")
        cap = 256
        records = self.tmp / "records.json"
        records.write_text("[" + " " * (cap - 2) + "]")
        args = ["source", "read", "t-slack-mentions", "--raw", str(raw), "--records", str(records), "--dry-run",
                "--data", str(self.data)]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("assistant.engine.MAX_OUTPUT", cap), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(args), 0, err.getvalue())
            self.assertEqual((json.loads(out.getvalue())["items"], err.getvalue()), ([], ""))
            records.write_text("[" + " " * (cap - 1) + "]")
            self.assertEqual(cli.main(args), 1)
        self.assertEqual(err.getvalue(), f"ap2: the records file {str(records)!r} is larger than {cap} bytes\n")

    def test_a_records_file_that_is_not_a_list_is_refused_before_anything_runs(self):
        marker = self.tmp / "ran"
        script = self.tmp / "mark.py"
        script.write_text("import sys\nopen(sys.argv[1], 'w').write('ran')\nprint('[]')\n")
        run = " ".join(shlex.quote(str(a)) for a in (sys.executable, script, marker))
        (self.data / "sources" / "t-marker.yaml").write_text(
            f"source: t-marker\nbrings: messages\nread:\n  via: command\n  run: '{run}'\nfill: paths\n"
            "form: { id: .id, at: .at, text: .text }\nlink: none\nsort: { all: alert }\n")
        null = self.tmp / "null.json"
        null.write_text("null")
        for extra in ((), ("--dry-run",)):
            with self.subTest(extra=extra):
                self.assertEqual(self.fails("source", "read", "t-marker", "--records", str(null), *extra),
                                 "ap2: --records needs --raw FILE, the output the records were filled from")
                self.assertFalse(marker.exists())
        self.assertEqual(self.fails("source", "read", "t-marker", "--raw", str(self.reply), "--records", str(null)),
                         f"ap2: the records file {str(null)!r} must hold a JSON list")
        self.assertFalse(marker.exists())
        self.assertFalse((self.data / "sources" / ".raw").exists())

    NEEDS_RAW = "ap2: --records needs --raw FILE, the output the records were filled from"

    def marker_source(self) -> Path:
        marker = self.tmp / "ran"
        script = self.tmp / "mark.py"
        script.write_text("import sys\nopen(sys.argv[1], 'w').write('ran')\nprint('[]')\n")
        run = " ".join(shlex.quote(str(a)) for a in (sys.executable, script, marker))
        (self.data / "sources" / "t-marker.yaml").write_text(
            f"source: t-marker\nbrings: messages\nread:\n  via: command\n  run: '{run}'\nfill: paths\n"
            "form: { id: .id, at: .at, text: .text }\nlink: none\nsort: { all: alert }\n")
        return marker

    def test_records_without_raw_is_refused_before_a_missing_records_file_is_read(self):
        marker = self.marker_source()
        missing = self.tmp / "missing.json"
        self.assertEqual(self.fails("source", "read", "t-marker", "--records", str(missing)), self.NEEDS_RAW)
        self.assertFalse(marker.exists())
        self.assertFalse(missing.exists())
        self.assertFalse((self.data / "sources" / ".raw").exists())

    def test_records_without_raw_is_refused_before_stdin_is_read(self):
        marker = self.marker_source()
        proc = subprocess.Popen([sys.executable, str(AP2), "source", "read", "t-marker", "--records", "/dev/stdin"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env(),
                                text=True, encoding="utf-8", errors="replace")
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        self.addCleanup(proc.stdin.close)
        # stdin is held open and never written, so a read of it would wait until the timeout. Not communicate():
        # with no input it closes stdin first, and ap2 would then read an empty file instead of waiting.
        proc.wait(timeout=20)  # one short line on each pipe, so nothing blocks on a full pipe
        out, err = proc.stdout.read(), proc.stderr.read()
        proc.stdout.close()
        proc.stderr.close()
        self.assertEqual((proc.returncode, out, err), (1, "", self.NEEDS_RAW + "\n"))
        self.assertFalse(marker.exists())
        self.assertFalse((self.data / "sources" / ".raw").exists())

    def test_a_reply_without_the_list_of_the_item_path_is_one_line(self):
        self.reply.write_text('{"message": "API rate limit exceeded"}')
        line = self.fails("source", "read", "t-vendor-status")
        self.assertRegex(line, r"^ap2: t-vendor-status: the reply has no list at \.incidents \(saved as "
                               r"sources/\.raw/t-vendor-status/")

    def test_data_flag_wins_over_the_environment(self):
        other = self.tmp / "other"
        (other / "sources").mkdir(parents=True)
        (other / "sources" / "t-other.yaml").write_text(self.TRACKER.replace("t-tracker", "t-other"))
        names = [s["source"] for s in self.ok("source", "list", "--data", str(other))]
        self.assertIn("t-other", names)
        self.assertNotIn("t-vendor-status", names)

    def test_read_records_with_a_bad_category_drops_the_record(self):
        text = (FIXTURES / "plugin" / "slack-mentions.yaml").read_text()
        (self.data / "sources" / "t-slack-mentions.yaml").write_text(
            text.replace("source: slack-mentions", "source: t-slack-mentions"))
        raw = self.tmp / "slack.txt"
        raw.write_text("Message_ts: 1730000000.123456 thread_ts: 1729999000.000100 Channel: C0123ABCD\n")
        records = self.tmp / "records.json"
        good = {"id": "1730000000.123456", "thread_id": "1729999000.000100", "at": "1730000000.123456",
                "text": "Can you look at the retry change?", "place": "C0123ABCD"}
        records.write_text(json.dumps([{**good, "category": ["question"]},
                                       {**good, "id": "9999999999.000001", "category": "question"},
                                       {**good, "category": "question"}]))
        r = self.run_ap2("source", "read", "t-slack-mentions", "--raw", str(raw), "--records", str(records))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        out = json.loads(r.stdout)
        self.assertEqual(out["dropped"], [
            {"item": 0, "reason": "category ['question'] is not one of sort's (question, ask_to_do, alert, fyi)"},
            {"item": 1, "reason": "form.id '9999999999.000001' is not in the raw output"}])
        self.assertEqual([(i["item"], i["action"]) for i in out["items"]], [(2, "add")])

    def test_errors_are_one_line(self):
        self.assertEqual(self.fails("source", "show", "nosuch"),
                         f"ap2: no source named 'nosuch' (looked in {ROOT / 'sources'} and {self.data / 'sources'})")
        bad = self.data / "sources" / "t-bad.yaml"
        bad.write_text("source: t-bad\nbrings: messages\nraed: x\n")
        self.assertEqual(self.fails("source", "show", "t-bad"),
                         f"ap2: {bad}: line 3: unknown key 'raed' (known keys: source, brings, read, fill, form, link, "
                         "key, sort, key_for, instructions, status_map, writes, never, plan)")
        self.assertTrue(self.fails("source", "list").startswith(f"ap2: {bad}: "))
        self.assertIn("--records needs --raw FILE", self.fails("source", "read", "t-vendor-status", "--records",
                                                               str(self.reply)))

    def test_help_lists_the_source_command(self):
        r = self.run_ap2("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("source", r.stdout)
        r = self.run_ap2("source")
        self.assertEqual(r.returncode, 2)
        self.assertIn("<source command>", r.stderr)


if __name__ == "__main__":
    unittest.main()
