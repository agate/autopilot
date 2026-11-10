from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT
from assistant import datadir, sources
from assistant.cards import CardError
from assistant.config import Config, ShadowModeError
from assistant.engine import Engine, plain_words, snippet
from assistant.sources import Loader, SourceError
from assistant.store import Store

FIXTURES = ROOT / "tests" / "fixtures" / "ap2" / "sources"
REPLIES = FIXTURES / "replies"

# A fake command: prints the file in argv[1], and writes its working folder to argv[2] when given.
FAKE = """\
import os, sys
sys.stdout.buffer.write(open(sys.argv[1], "rb").read())
if len(sys.argv) > 2:
    open(sys.argv[2], "w").write(os.getcwd())
if len(sys.argv) > 3:
    sys.stderr.write("it went wrong\\n")
    sys.exit(int(sys.argv[3]))
"""


class Clock:
    def __init__(self):
        self.at = datetime(2026, 11, 10, 14, 30, 5, tzinfo=timezone.utc)

    def __call__(self):
        return self.at

    def tick(self, seconds=60):
        self.at += timedelta(seconds=seconds)


def tree(folder: Path) -> dict:
    """Every file under `folder` with its bytes."""
    return {str(p.relative_to(folder)): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


class EngineCase(unittest.TestCase):
    cfg = Config()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-engine-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.plugin = self.tmp / "plugin"
        shutil.copytree(FIXTURES / "plugin", self.plugin)
        self.data = datadir.ensure(self.tmp / "data")
        self.fake = self.tmp / "bin" / "fake.py"
        self.fake.parent.mkdir()
        self.fake.write_text(FAKE)
        self.clock = Clock()

    def engine(self, cfg=None):
        return Engine(self.data, cfg=cfg or self.cfg, plugin_dir=self.plugin, clock=self.clock)

    def store(self):
        return Store(self.data, cfg=self.cfg, clock=self.clock)

    def command(self, reply: Path, *extra) -> str:
        return " ".join(shlex.quote(str(a)) for a in (sys.executable, self.fake, reply, *extra))

    def user_source(self, name: str, text: str) -> Path:
        p = self.data / "sources" / f"{name}.yaml"
        p.write_text(textwrap.dedent(text), encoding="utf-8")
        return p

    def status_source(self, reply: Path, *extra, name="vendor-status"):
        text = (FIXTURES / "user" / "vendor-status.yaml").read_text()
        run = self.command(reply, *extra)
        text = text.replace("run: curl -s https://status.example.com/api/v2/incidents/unresolved.json",
                            f"run: '{run}'")
        self.user_source(name, text.replace("source: vendor-status", f"source: {name}"))

    def reply_copy(self, name: str) -> Path:
        p = self.tmp / name
        shutil.copy(REPLIES / name, p)
        return p


class CommandReadTest(EngineCase):
    def test_runs_the_command_and_saves_its_raw_stdout(self):
        reply = self.reply_copy("status-incidents.json")
        cwd_file = self.tmp / "cwd.txt"
        self.status_source(reply, cwd_file)
        result = self.engine().read("vendor-status")
        self.assertEqual(Path(cwd_file.read_text()).resolve(), self.data)  # the data folder is the working folder
        raw = self.data / result["raw"]
        self.assertRegex(result["raw"], r"^sources/\.raw/vendor-status/20261110T143005Z-[0-9a-f]{6}\.json$")
        self.assertEqual(raw.read_bytes(), reply.read_bytes())
        report = json.loads(raw.with_name(raw.stem + ".report.json").read_text())
        self.assertEqual(report, result)
        self.assertEqual([i["key"] for i in result["items"]], ["inc01abcd", "inc02efgh"])
        self.assertEqual(result["dropped"], [{"item": 2, "reason": "form.text missing"}])

    def test_the_raw_file_is_written_before_the_records_are_filled(self):
        reply = self.reply_copy("status-incidents.json")
        self.status_source(reply)
        seen = []
        real = Engine.fill

        def fill(src, data, **kw):
            seen.extend(p.read_bytes() for p in (self.data / "sources" / ".raw" / "vendor-status").glob("*.json"))
            return real(src, data, **kw)

        with mock.patch.object(Engine, "fill", staticmethod(fill)):
            self.engine().read("vendor-status")
        self.assertEqual(seen, [reply.read_bytes()])

    def test_output_that_is_not_json_is_still_saved(self):
        bad = self.tmp / "bad.json"
        bad.write_text("<html>maintenance</html>")
        self.status_source(bad)
        with self.assertRaisesRegex(SourceError, r"^vendor-status: the output is not JSON \(saved as sources/\.raw/"):
            self.engine().read("vendor-status")
        raws = list((self.data / "sources" / ".raw" / "vendor-status").glob("*.json"))
        self.assertEqual([p.read_text() for p in raws], ["<html>maintenance</html>"])
        self.assertEqual(self.store().list(), [])

    def test_a_failing_command_saves_its_stdout_and_fails(self):
        reply = self.reply_copy("status-incidents.json")
        self.status_source(reply, self.tmp / "cwd.txt", 3)
        with self.assertRaises(SourceError) as cm:
            self.engine().read("vendor-status")
        [raw] = (self.data / "sources" / ".raw" / "vendor-status").glob("*.json")
        self.assertEqual(str(cm.exception), "vendor-status: read.run exited with 3: it went wrong (output saved as "
                                            f"{raw.relative_to(self.data).as_posix()})")
        self.assertEqual(self.store().list(), [])

    def test_no_shell_runs_the_command(self):
        marker = self.tmp / "shell-ran"
        self.user_source("echo", f"""\
            source: echo
            brings: messages
            read:
              via: command
              run: "echo '[]' ; touch {marker}"
            fill: paths
            form: {{ id: .id, at: .at, text: .text }}
            link: none
            sort: {{ all: alert }}
            """)
        with self.assertRaisesRegex(SourceError, "^echo: the output is not JSON"):
            self.engine().read("echo")
        [raw] = (self.data / "sources" / ".raw" / "echo").glob("*.json")
        self.assertEqual(raw.read_text(), f"[] ; touch {marker}\n")  # echo got every word, ';' included
        self.assertFalse(marker.exists())

    def test_a_missing_program_and_a_timeout(self):
        self.user_source("gone", """\
            source: gone
            brings: messages
            read: { via: command, run: no-such-program-ap2 --json }
            fill: paths
            form: { id: .id, at: .at, text: .text }
            link: none
            sort: { all: alert }
            """)
        with self.assertRaisesRegex(SourceError, "^gone: cannot run 'no-such-program-ap2'"):
            self.engine().read("gone")
        slow = self.tmp / "slow.py"
        slow.write_text("import sys, time; sys.stdout.write('[1, 2'); sys.stdout.flush(); time.sleep(30)\n")
        self.user_source("slow", f"""\
            source: slow
            brings: messages
            read: {{ via: command, run: '{sys.executable} {slow}', timeout: 1 }}
            fill: paths
            form: {{ id: .id, at: .at, text: .text }}
            link: none
            sort: {{ all: alert }}
            """)
        with self.assertRaisesRegex(SourceError, "^slow: read.run took longer than 1 seconds; stopped$"):
            self.engine().read("slow", dry_run=True)
        self.assertFalse((self.data / "sources" / ".raw" / "slow").exists())  # a dry run keeps nothing
        with self.assertRaisesRegex(SourceError, r"^slow: read.run took longer than 1 seconds; stopped \(partial "
                                                 r"output saved as sources/\.raw/slow/[0-9TZ]+-[0-9a-f]{6}\.json\)$"):
            self.engine().read("slow")
        [raw] = (self.data / "sources" / ".raw" / "slow").glob("*.json")
        self.assertEqual(raw.read_text(), "[1, 2")

    def test_a_source_without_a_timeout_gets_the_default(self):
        self.status_source(self.reply_copy("status-incidents.json"))
        with mock.patch("assistant.engine.subprocess.Popen") as popen:
            proc = popen.return_value
            proc.communicate.return_value = (b'{"incidents": []}', b"")
            proc.returncode = 0
            result = self.engine().read("vendor-status")
        self.assertEqual((result["items"], result["dropped"]), ([], []))
        popen.assert_called_once()
        self.assertIs(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
        proc.communicate.assert_called_once()
        self.assertEqual(proc.communicate.call_args.kwargs["timeout"], sources.DEFAULT_TIMEOUT)

    def test_a_timeout_stops_the_children_of_the_command_too(self):
        pid_file = self.tmp / "grandchild.pid"
        script = self.tmp / "wrapper.py"
        script.write_text(textwrap.dedent("""\
            import subprocess, sys, time
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            with open(sys.argv[1], "w") as f:
                f.write(str(child.pid))
            time.sleep(30)
            """))

        def kill_left_over():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except (OSError, ValueError):
                pass

        self.addCleanup(kill_left_over)
        self.user_source("wrapper", f"""\
            source: wrapper
            brings: messages
            read: {{ via: command, run: '{sys.executable} {script} {pid_file}', timeout: 1 }}
            fill: paths
            form: {{ id: .id, at: .at, text: .text }}
            link: none
            sort: {{ all: alert }}
            """)
        started = time.monotonic()
        with self.assertRaisesRegex(SourceError, "^wrapper: read.run took longer than 1 seconds; stopped"):
            self.engine().read("wrapper")
        self.assertLess(time.monotonic() - started, 10)
        pid = int(pid_file.read_text())
        deadline = time.monotonic() + 2
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break  # gone
            if time.monotonic() > deadline:
                self.fail(f"the command's child {pid} is still running after the timeout")
            time.sleep(0.05)

    def test_an_interrupted_read_stops_the_command(self):
        script = self.tmp / "sleeper.py"
        script.write_text("import time; time.sleep(30)\n")
        started = []
        real = subprocess.Popen.communicate

        def interrupt(proc, *args, **kwargs):
            if not started:
                started.append(proc.pid)
                raise KeyboardInterrupt
            return real(proc, *args, **kwargs)

        def kill_left_over():
            for pid in started:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except OSError:
                    pass

        self.addCleanup(kill_left_over)
        self.user_source("sleeper", f"""\
            source: sleeper
            brings: messages
            read: {{ via: command, run: '{sys.executable} {script}', timeout: 20 }}
            fill: paths
            form: {{ id: .id, at: .at, text: .text }}
            link: none
            sort: {{ all: alert }}
            """)
        with mock.patch.object(subprocess.Popen, "communicate", autospec=True, side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.engine().read("sleeper")
        [pid] = started
        deadline = time.monotonic() + 2
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break  # gone
            if time.monotonic() > deadline:
                self.fail(f"the command {pid} is still running after the interrupt")
            time.sleep(0.05)

    def test_a_helper_that_keeps_the_output_open_does_not_hold_the_read_past_the_drain_limit(self):
        pid_file = self.tmp / "helper.pid"
        script = self.tmp / "forker.py"
        script.write_text(textwrap.dedent("""\
            import os, sys, time
            sys.stdout.write("[1"); sys.stdout.flush()
            if os.fork() == 0:
                os.setsid()  # leaves the command's group, so the timeout kill does not reach it
                with open(sys.argv[1] + ".tmp", "w") as f:
                    f.write(str(os.getpid()))
                os.rename(sys.argv[1] + ".tmp", sys.argv[1])
                time.sleep(15)
                os._exit(0)
            time.sleep(15)
            """))

        def kill_left_over():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except (OSError, ValueError):
                pass

        self.addCleanup(kill_left_over)
        self.user_source("forker", f"""\
            source: forker
            brings: messages
            read: {{ via: command, run: '{sys.executable} {script} {pid_file}', timeout: 1 }}
            fill: paths
            form: {{ id: .id, at: .at, text: .text }}
            link: none
            sort: {{ all: alert }}
            """)
        started = time.monotonic()
        with mock.patch("assistant.engine.DRAIN", 1):
            with self.assertRaisesRegex(SourceError, r"^forker: read.run took longer than 1 seconds; stopped "
                                                     r"\(partial output saved as "):
                self.engine().read("forker")
        self.assertLess(time.monotonic() - started, 1 + 1 + 3)
        [raw] = (self.data / "sources" / ".raw" / "forker").glob("*.json")
        self.assertEqual(raw.read_text(), "[1")

    def test_values_in_a_link_pattern_are_percent_encoded(self):
        reply = self.tmp / "odd-id.json"
        reply.write_text(json.dumps([{"id": "a/b?c d", "at": "2026-11-10T14:00:00Z", "text": "Odd id"}]))
        self.user_source("odd", f"""\
            source: odd
            brings: messages
            read: {{ via: command, run: '{self.command(reply)}' }}
            fill: paths
            form: {{ id: .id, at: .at, text: .text }}
            link: "https://status.example.com/i/{{id}}"
            key: "{{id}}"
            sort: {{ all: alert }}
            """)
        result = self.engine().read("odd")
        self.assertEqual(result["dropped"], [])
        [card] = self.store().list()
        self.assertEqual(card["key"], "a/b?c d")  # the key keeps the value as it is
        self.assertEqual(card["source"]["link"], "https://status.example.com/i/a%2Fb%3Fc%20d")

    def test_a_fill_ai_command_source_is_refused_before_its_command_runs(self):
        marker = self.tmp / "ran"
        script = self.tmp / "mark.py"
        script.write_text("import sys; open(sys.argv[1], 'w').write('ran'); print('[]')\n")
        self.user_source("cmd-ai", f"""\
            source: cmd-ai
            brings: messages
            read: {{ via: command, run: '{sys.executable} {script} {marker}' }}
            fill: ai
            form: {{ id: Message_ts, at: Message_ts, text: Text }}
            link: none
            sort: {{ all: alert }}
            """)
        with self.assertRaisesRegex(SourceError, "^cmd-ai: fill: ai records are filled by a scan session"):
            self.engine().read("cmd-ai")
        self.assertFalse(marker.exists())
        self.assertFalse((self.data / "sources" / ".raw").exists())

    def test_other_vias_are_not_run_here(self):
        with self.assertRaisesRegex(SourceError, r"read\.via is connector; only a scan session can make that read"):
            self.engine().read("slack-mentions")


class SavedReadTest(EngineCase):
    def test_read_saved_fills_from_a_file_and_keeps_a_copy(self):
        # An mcp-style source: the scan session saved the reply; the engine fills it with code.
        self.user_source("tracker", f"""\
            source: tracker
            brings: work
            read: {{ via: mcp, server: tracker, tool: search_issues }}
            fill: paths
            form:
              id: .key
              at: .fields.updated
              text: .fields.summary
              status: .fields.status.name
              depends_on: .fields.issuelinks[is blocked by].key
            link: "https://tracker.example.com/browse/{{id}}"
            key: "{{id}}"
            status_map: {{ "To Do": todo, "In Progress": in_progress }}
            sort: {{ all: code }}
            """)
        saved = self.reply_copy("jira-search.json")
        result = self.engine().read_saved("tracker", saved)
        self.assertEqual((self.data / result["raw"]).read_bytes(), saved.read_bytes())
        self.assertTrue(result["raw"].startswith("sources/.raw/tracker/"))
        cards = {c["key"]: c for c in self.store().list()}
        self.assertEqual(sorted(cards), ["PROJ-123", "PROJ-124"])
        first = cards["PROJ-123"]
        self.assertEqual((first["kind"], first["lane"], first["status"]), ("code", "planned", "in_progress"))
        self.assertEqual(first["source"]["record"]["depends_on"], ["PROJ-120", "PROJ-121"])
        self.assertEqual(first["source"]["link"], "https://tracker.example.com/browse/PROJ-123")
        self.assertEqual(cards["PROJ-124"]["status"], "todo")
        # a file already under .raw/<source>/ is not copied again
        again = self.engine().read_saved("tracker", self.data / result["raw"])
        self.assertEqual(again["raw"], result["raw"])

    SLACK_RAW = ("Message_ts: 1730000000.123456 thread_ts: 1729999000.000100 From: U0EXAMPLE1 Channel: C0123ABCD\n"
                 "Text: Can you look at the retry change?\n"
                 "Message_ts: 1730000001.000200 thread_ts: 1730000001.000200 Text: lunch?\n"
                 "Message_ts: 1730000002.000300 Text: x\n")

    def test_fill_ai_takes_the_scan_sessions_records(self):
        raw = self.tmp / "slack.txt"
        raw.write_text(self.SLACK_RAW)
        records = [
            {"id": "1730000000.123456", "thread_id": "1729999000.000100", "author": "U0EXAMPLE1",
             "at": "1730000000.123456", "text": "Can you look at the retry change?", "place": "C0123ABCD",
             "category": "question"},
            {"id": "1730000001.000200", "thread_id": "1730000001.000200", "at": "1730000001.000200",
             "text": "lunch?", "place": "C0123ABCD", "category": "fyi"},
            {"id": "1730000002.000300", "at": "1730000002.000300", "text": "x", "category": "gossip"},
            {"id": "", "at": "1", "text": "x", "category": "question"},
            # Invented by the model: not in what the tool returned.
            {"id": "9999999999.000001", "thread_id": "1729999000.000100", "at": "1730000000.123456",
             "text": "made up", "place": "C0123ABCD", "category": "question"},
            {"id": "1730000002.000300", "thread_id": "1111111111.000001", "at": "1730000002.000300",
             "text": "x", "place": "C0123ABCD", "category": "question"},
            {"id": "1730000002.000300", "at": "2026-11-10T14:00:00Z", "text": "x", "category": "question"},
            {"id": "1730000002.000300", "thread_id": "1730000002.000300", "at": "1730000002.000300",
             "text": "x", "place": "C0123ABCD", "category": ["question"]},
        ]
        result = self.engine().read_saved("slack-mentions", raw, records=records)
        self.assertEqual([(i["category"], i["becomes"], i["action"]) for i in result["items"]],
                         [("question", "question", "add"), ("fyi", "none", "none")])
        self.assertEqual(result["dropped"], [
            {"item": 2, "reason": "category 'gossip' is not one of sort's (question, ask_to_do, alert, fyi)"},
            {"item": 3, "reason": "form.id is empty"},
            {"item": 4, "reason": "form.id '9999999999.000001' is not in the raw output"},
            {"item": 5, "reason": "form.thread_id '1111111111.000001' is not in the raw output"},
            {"item": 6, "reason": "form.at '2026-11-10T14:00:00Z' is not in the raw output"},
            {"item": 7, "reason": "category ['question'] is not one of sort's (question, ask_to_do, alert, fyi)"}])
        [card] = self.store().list()
        self.assertEqual(card["source"]["link"], "https://slack.com/archives/C0123ABCD/p1730000000123456"  # checkpkg: ignore
                                                 "?thread_ts=1729999000.000100&cid=C0123ABCD")
        self.assertEqual(card["key"], "C0123ABCD:1729999000.000100")
        with self.assertRaisesRegex(SourceError, "fill: ai records are filled by a scan session"):
            self.engine().read_saved("slack-mentions", raw)

    def test_an_empty_thread_id_drops_the_record_instead_of_merging_items(self):
        # Two different messages with an empty thread_id would both get the key "C0123ABCD:" and become one card.
        raw = self.tmp / "slack.txt"
        raw.write_text(self.SLACK_RAW)
        records = [{"id": "1730000000.123456", "thread_id": "", "at": "1730000000.123456",
                    "text": "Can you look at the retry change?", "place": "C0123ABCD", "category": "question"},
                   {"id": "1730000002.000300", "thread_id": "", "at": "1730000002.000300", "text": "x",
                    "place": "C0123ABCD", "category": "question"}]
        result = self.engine().read_saved("slack-mentions", raw, records=records)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["dropped"], [{"item": 0, "reason": "key: {thread_id} is empty"},
                                             {"item": 1, "reason": "key: {thread_id} is empty"}])
        self.assertEqual(self.store().list(), [])

    def test_an_invented_id_makes_no_card(self):
        raw = self.tmp / "slack.txt"
        raw.write_text(self.SLACK_RAW)
        made_up = {"id": "9999999999.000001", "thread_id": "9999999999.000001", "at": "9999999999.000001",
                   "text": "made up", "place": "C0123ABCD", "category": "question"}
        result = self.engine().read_saved("slack-mentions", raw, records=[made_up])
        self.assertEqual(result["items"], [])
        self.assertEqual([d["item"] for d in result["dropped"]], [0])
        self.assertEqual(self.store().list(), [])

    def test_fill_paths_refuses_records(self):
        with self.assertRaisesRegex(SourceError, "fill: paths fills its own records"):
            self.engine().read_saved("gh-mine", self.reply_copy("gh-pr-list.json"), records=[])
        self.assertFalse((self.data / "sources" / ".raw").exists())  # refused before the copy

    def test_fill_ai_without_records_is_refused_before_the_copy(self):
        raw = self.tmp / "slack.txt"
        raw.write_text(self.SLACK_RAW)
        with self.assertRaisesRegex(SourceError, "^slack-mentions: fill: ai records are filled by a scan session"):
            self.engine().read_saved("slack-mentions", raw)
        self.assertFalse((self.data / "sources" / ".raw").exists())

    def test_a_missing_saved_file(self):
        with self.assertRaisesRegex(SourceError, "^gh-mine: cannot read the saved output"):
            self.engine().read_saved("gh-mine", self.tmp / "nope.json")


class SortToCardTest(EngineCase):
    def test_a_read_makes_cards_and_a_second_read_updates_them(self):
        reply = self.reply_copy("status-incidents.json")
        self.status_source(reply)
        first = self.engine().read("vendor-status")
        self.assertEqual([(i["action"], i["becomes"], i["category"]) for i in first["items"]],
                         [("add", "alert", "all"), ("add", "alert", "all")])
        cards = self.store().list()
        self.assertEqual(len(cards), 2)
        card = next(c for c in cards if c["key"] == "inc01abcd")
        self.assertEqual({k: card[k] for k in ("kind", "lane", "feed", "pri", "origin", "links", "title")},
                         {"kind": "alert", "lane": "needs", "feed": "vendor-status", "pri": "P2", "origin": "source",
                          "links": [], "title": "Elevated error rates on the payments API"})
        self.assertEqual(card["source"], {
            "name": "vendor-status", "link": "https://status.example.com/incidents/inc01abcd", "raw": first["raw"],
            "record": {"id": "inc01abcd", "at": "2026-11-10T13:40:05.000Z",
                       "text": "Elevated error rates on the payments API"}})
        self.assertEqual(card["plain"]["what"]["en"],
                         "New from vendor-status: Elevated error rates on the payments API")
        self.assertEqual(card["audit"][0]["by"], "source:vendor-status")

        # Same items again: nothing changes, so nothing is written (no audit entry, no restore point).
        self.clock.tick()
        again = self.engine().read("vendor-status")
        self.assertEqual([i["action"] for i in again["items"]], ["unchanged", "unchanged"])
        self.assertEqual(self.store().list(), cards)

        # The incident is renamed: the same key updates the same card instead of adding one.
        changed = json.loads(reply.read_text())
        changed["incidents"][0]["name"] = "Payments API errors are falling"
        reply.write_text(json.dumps(changed))
        self.clock.tick()
        third = self.engine().read("vendor-status")
        self.assertEqual([i["action"] for i in third["items"]], ["update", "unchanged"])
        self.assertEqual(third["items"][0]["card"], card["id"])
        after = self.store().list()
        self.assertEqual(len(after), 2)
        updated = self.store().get(card["id"])
        self.assertEqual(updated["title"], "Payments API errors are falling")
        self.assertEqual(updated["source"]["raw"], third["raw"])
        self.assertEqual(updated["audit"][-1]["did"], "read vendor-status")
        self.assertEqual(updated["audit"][-1]["by"], "source:vendor-status")
        self.assertIsNotNone(updated["audit"][-1]["restore"])
        self.assertEqual(len(updated["audit"]), 2)

    def test_a_same_key_update_leaves_kind_lane_pri_and_ask_alone(self):
        reply = self.reply_copy("status-incidents.json")
        self.status_source(reply)
        self.engine().read("vendor-status")
        card = next(c for c in self.store().list() if c["key"] == "inc01abcd")
        ask = {"type": "answer", "text": "Is this still happening?"}
        self.clock.tick()
        moved = self.store().update(card["id"], {"lane": "done", "pri": "P0", "ask": ask, "kind": "question"},
                                    by="tester")
        changed = json.loads(reply.read_text())
        changed["incidents"][0]["name"] = "Payments API errors are falling"
        reply.write_text(json.dumps(changed))
        self.clock.tick()
        result = self.engine().read("vendor-status")
        self.assertEqual(result["items"][0]["action"], "update")
        after = self.store().get(card["id"])
        self.assertEqual({k: after[k] for k in ("kind", "lane", "pri", "ask")},
                         {"kind": "question", "lane": "done", "pri": "P0", "ask": ask})
        for k in ("title", "plain", "source"):
            self.assertNotEqual(after[k], moved[k], k)
        self.assertEqual(after["title"], "Payments API errors are falling")

    def test_the_same_key_twice_in_one_read_is_one_card(self):
        reply = self.tmp / "dup.json"
        incidents = json.loads((REPLIES / "status-incidents.json").read_text())
        incidents["incidents"][1]["id"] = "inc01abcd"
        reply.write_text(json.dumps(incidents))
        self.status_source(reply)
        dry = self.engine().read("vendor-status", dry_run=True)
        self.assertEqual([i["action"] for i in dry["items"]], ["add", "update"])
        self.assertEqual(self.store().list(), [])
        result = self.engine().read("vendor-status")
        self.assertEqual([i["action"] for i in result["items"]], ["add", "update"])
        self.assertEqual(len(self.store().list()), 1)

    def test_a_bad_stored_card_is_dropped_and_the_read_goes_on(self):
        reply = self.tmp / "three.json"
        incidents = json.loads((REPLIES / "status-incidents.json").read_text())
        incidents["incidents"][2]["name"] = "Slow logins"
        reply.write_text(json.dumps(incidents))
        self.status_source(reply)
        self.engine().read("vendor-status")
        ids = {c["key"]: c["id"] for c in self.store().list()}
        self.assertEqual(sorted(ids), ["inc01abcd", "inc02efgh", "inc03ijkl"])
        # The middle card was edited by hand into one the store refuses.
        path = self.store().path(ids["inc02efgh"])
        bad = json.loads(path.read_text())
        bad["pri"] = "P9"
        path.write_text(json.dumps(bad))
        for n, inc in enumerate(incidents["incidents"]):
            inc["name"] = f"Renamed {n}"
        reply.write_text(json.dumps(incidents))
        self.clock.tick()
        result = self.engine().read("vendor-status")
        self.assertEqual([(i["item"], i["action"], i["card"]) for i in result["items"]],
                         [(0, "update", ids["inc01abcd"]), (2, "update", ids["inc03ijkl"])])
        [dropped] = result["dropped"]
        self.assertEqual(dropped["item"], 1)
        self.assertRegex(dropped["reason"], rf"^card {ids['inc02efgh']}: pri must be one of ")
        self.assertEqual(self.store().get(ids["inc03ijkl"])["title"], "Renamed 2")
        raw = self.data / result["raw"]
        self.assertEqual(json.loads(raw.with_name(raw.stem + ".report.json").read_text()), result)

    def test_a_card_the_store_will_not_add_is_dropped_and_the_read_goes_on(self):
        self.status_source(self.reply_copy("status-incidents.json"))
        with mock.patch.object(Store, "add", side_effect=CardError("refused")):
            result = self.engine().read("vendor-status")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["dropped"], [{"item": 2, "reason": "form.text missing"},
                                             {"item": 0, "reason": "new card with key inc01abcd: refused"},
                                             {"item": 1, "reason": "new card with key inc02efgh: refused"}])
        raw = self.data / result["raw"]
        self.assertTrue(raw.with_name(raw.stem + ".report.json").exists())

    STORE_FAILURES = (OSError("boom"), sqlite3.DatabaseError("broken"))

    def test_a_card_store_that_cannot_add_at_all_stops_the_read(self):
        for n, cause in enumerate(self.STORE_FAILURES):
            with self.subTest(type(cause).__name__):
                self.data = datadir.ensure(self.tmp / f"data-add-{n}")
                self.status_source(self.reply_copy("status-incidents.json"))

                def fail(*args, **kwargs):
                    raise CardError("cannot use the card store in X: failed") from cause

                with mock.patch.object(Store, "add", side_effect=fail):
                    with self.assertRaisesRegex(CardError, "^cannot use the card store in X: failed$"):
                        self.engine().read("vendor-status")

    def test_a_card_store_that_cannot_update_at_all_stops_the_read(self):
        for n, cause in enumerate(self.STORE_FAILURES):
            with self.subTest(type(cause).__name__):
                self.data = datadir.ensure(self.tmp / f"data-update-{n}")
                reply = self.reply_copy("status-incidents.json")
                self.status_source(reply)
                self.engine().read("vendor-status")
                self.assertEqual(len(self.store().list()), 2)
                incidents = json.loads(reply.read_text())
                incidents["incidents"][0]["name"] = "Renamed"
                reply.write_text(json.dumps(incidents))
                self.clock.tick()

                def fail(*args, **kwargs):
                    raise CardError("search index X: failed") from cause

                with mock.patch.object(Store, "update", side_effect=fail):
                    with self.assertRaisesRegex(CardError, "^search index X: failed$"):
                        self.engine().read("vendor-status")

    def test_update_card_only_updates_and_none_makes_nothing(self):
        reply = self.reply_copy("status-incidents.json")
        self.status_source(reply)
        self.engine().read("vendor-status")
        before = self.store().list()
        text = (self.data / "sources" / "vendor-status.yaml").read_text()
        self.user_source("vendor-updates", text.replace("source: vendor-status", "source: vendor-updates")
                         .replace("sort: { all: alert }", "sort: { all: update_card }"))
        changed = json.loads(reply.read_text())
        changed["incidents"][0]["name"] = "Fixed"
        changed["incidents"][1]["id"] = "inc09new"
        reply.write_text(json.dumps(changed))
        result = self.engine().read("vendor-updates")
        self.assertEqual([(i["action"], i.get("reason")) for i in result["items"]],
                         [("update", None), ("none", "no card has this key")])
        self.assertEqual(len(self.store().list()), len(before))
        self.user_source("vendor-quiet", text.replace("source: vendor-status", "source: vendor-quiet")
                         .replace("sort: { all: alert }", "sort: { all: none }"))
        quiet = self.engine().read("vendor-quiet")
        self.assertEqual({i["action"] for i in quiet["items"]}, {"none"})
        self.assertEqual(len(self.store().list()), len(before))

    def test_several_categories_on_fill_paths_make_no_card(self):
        reply = self.reply_copy("status-incidents.json")
        text = (FIXTURES / "user" / "vendor-status.yaml").read_text()
        self.user_source("vendor-status", text.replace("run: curl -s https://status.example.com/api/v2/incidents/"
                                                       "unresolved.json", f"run: '{self.command(reply)}'")
                         .replace("sort: { all: alert }", "sort: { alert: alert, fyi: none }"))
        result = self.engine().read("vendor-status")
        self.assertEqual({(i["action"], i["reason"]) for i in result["items"]},
                         {("none", "sort has 2 categories; fill: paths cannot pick one")})
        self.assertEqual(self.store().list(), [])

    def test_plain_words_in_both_languages(self):
        cfg = Config(secondary_language="zh-Hans")
        reply = self.reply_copy("gh-pr-list.json")
        text = (self.plugin / "gh-mine.yaml").read_text().replace(
            "run: gh pr list --author @me --json number,title,url,author,createdAt,headRepository",
            f"run: '{self.command(reply)}'")
        (self.plugin / "gh-mine.yaml").write_text(text)
        self.engine(cfg).read("gh-mine")
        card = next(c for c in Store(self.data, cfg=cfg).list() if c["key"] == "widgets#42")
        self.assertEqual(card["plain"], {
            "what": {"en": "New from gh-mine by octo-dev in widgets: Retry failed card payments once, 2 seconds later",
                     "zh-Hans": "来自 gh-mine 的新内容，作者 octo-dev，在 widgets：Retry failed card payments once, 2 seconds later"},
            "where": {"en": "It was written at 2026-11-09T17:02:11Z. Nothing has been done with it yet.",
                      "zh-Hans": "写于 2026-11-09T17:02:11Z。还没有人处理它。"},
            "do": {"en": "Read it at the link and decide what it needs.",
                   "zh-Hans": "看一下（点链接看全文），再决定要怎么处理。"}})
        self.assertEqual(card["kind"], "pr_review")

    def test_a_language_without_code_made_words_is_refused_before_anything(self):
        reply = self.reply_copy("status-incidents.json")
        self.status_source(reply)
        with self.assertRaisesRegex(SourceError, r"no code-made plain words for fr yet \(built in: en, zh\)"):
            self.engine(Config(secondary_language="fr")).read("vendor-status")
        self.assertEqual(self.store().list(), [])
        self.assertFalse((self.data / "sources" / ".raw").exists())  # refused before the read ran

    def test_a_long_first_line_makes_an_80_character_title(self):
        reply = self.tmp / "long.json"
        incidents = json.loads((REPLIES / "status-incidents.json").read_text())
        incidents["incidents"][0]["name"] = "y" * 100 + "\nsecond line"
        reply.write_text(json.dumps(incidents))
        self.status_source(reply)
        self.engine().read("vendor-status")
        card = next(c for c in self.store().list() if c["key"] == "inc01abcd")
        self.assertEqual(len(card["title"]), 80)
        self.assertEqual(card["title"], "y" * 79 + "…")

    def test_snippet(self):
        self.assertEqual(snippet("\n  first   line \nsecond"), "first line")
        self.assertEqual(snippet("x" * 200, 10), "xxxxxxxxx…")

    def test_work_plain_words_name_the_status(self):
        self.user_source("w", ShadowTest.WORK.format(run="echo []"))
        src = Loader(self.data, plugin_dir=self.plugin)
        words = plain_words(src.load("w"), {"id": "P", "at": "t", "text": "x", "status": "To Do"}, None, ("en",))
        self.assertEqual(words["where"]["en"], 'It was written at t. Its status there is "To Do". Nothing has been '
                                               "done with it yet.")
        self.assertEqual(words["do"]["en"], "Nothing yet; it waits in Planned until it is started.")


class DryRunTest(EngineCase):
    def test_a_dry_run_saves_nothing_and_says_what_each_item_would_become(self):
        reply = self.reply_copy("status-incidents.json")
        self.status_source(reply)
        before = tree(self.data)
        result = self.engine().read("vendor-status", dry_run=True)
        self.assertEqual(tree(self.data), before)
        self.assertTrue(result["dry_run"])
        self.assertIsNone(result["raw"])
        self.assertEqual([(i["action"], i["card"], i["would_add"]["kind"]) for i in result["items"]],
                         [("add", None, "alert"), ("add", None, "alert")])
        self.engine().read("vendor-status")
        changed = json.loads(reply.read_text())
        changed["incidents"][0]["name"] = "Renamed"
        reply.write_text(json.dumps(changed))
        before = tree(self.data)
        result = self.engine().read("vendor-status", dry_run=True)
        self.assertEqual(tree(self.data), before)
        self.assertEqual([(i["action"], i.get("would_change")) for i in result["items"]],
                         [("update", ["plain", "source", "title"]), ("unchanged", None)])

    def test_a_dry_run_drops_a_stored_card_the_store_would_refuse_as_the_read_does(self):
        reply = self.reply_copy("status-incidents.json")
        self.status_source(reply)
        self.engine().read("vendor-status")
        card = next(c for c in self.store().list() if c["key"] == "inc01abcd")
        # The card was edited by hand into one the store refuses.
        path = self.store().path(card["id"])
        bad = json.loads(path.read_text())
        bad["lane"] = "bogus"
        path.write_text(json.dumps(bad))
        changed = json.loads(reply.read_text())
        changed["incidents"][0]["name"] = "Renamed"
        reply.write_text(json.dumps(changed))
        self.clock.tick()
        before = tree(self.data)
        dry = self.engine().read("vendor-status", dry_run=True)
        self.assertEqual(tree(self.data), before)
        real = self.engine().read("vendor-status")
        for result in (dry, real):
            self.assertEqual([(i["item"], i["action"]) for i in result["items"]], [(1, "unchanged")])
            self.assertEqual(result["dropped"][0], {"item": 2, "reason": "form.text missing"})
            self.assertEqual(result["dropped"][1]["item"], 0)
            self.assertRegex(result["dropped"][1]["reason"], rf"^card {card['id']}: lane must be one of ")
        self.assertEqual(dry["dropped"], real["dropped"])


class ReplyShapeTest(EngineCase):
    def test_a_reply_without_the_list_of_the_item_path_is_refused(self):
        bad = self.tmp / "bad.json"
        self.status_source(bad)
        for reply in ({"message": "API rate limit exceeded"}, {"incidents": None}, {"incidents": {}}):
            with self.subTest(reply):
                bad.write_text(json.dumps(reply))
                with self.assertRaisesRegex(SourceError, r"^vendor-status: the reply has no list at \.incidents "
                                                         r"\(saved as sources/\.raw/vendor-status/[0-9TZ]+-"
                                                         r"[0-9a-f]{6}\.json\)$"):
                    self.engine().read("vendor-status")
                with self.assertRaisesRegex(SourceError, r"^vendor-status: the reply has no list at \.incidents$"):
                    self.engine().read("vendor-status", dry_run=True)
        self.assertEqual(self.store().list(), [])

    def test_an_empty_list_is_zero_items(self):
        empty = self.tmp / "empty.json"
        empty.write_text('{"incidents": []}')
        self.status_source(empty)
        result = self.engine().read("vendor-status")
        self.assertEqual((result["items"], result["dropped"]), ([], []))

    def test_text_that_is_not_valid_unicode_drops_only_that_record(self):
        incidents = json.loads((REPLIES / "status-incidents.json").read_text())
        incidents["incidents"][1]["name"] = "Truncated \ud800"
        incidents["incidents"][2]["name"] = "Third"
        reply = self.tmp / "surrogate.json"
        reply.write_text(json.dumps(incidents))  # ensure_ascii writes the lone surrogate as \ud800
        self.status_source(reply)
        result = self.engine().read("vendor-status")
        self.assertEqual([(i["item"], i["action"]) for i in result["items"]], [(0, "add"), (2, "add")])
        self.assertEqual(result["dropped"], [{"item": 1, "reason": "form.text is not valid Unicode"}])
        self.assertEqual(len(self.store().list()), 2)
        raw = self.data / result["raw"]
        self.assertEqual(json.loads(raw.with_name(raw.stem + ".report.json").read_text()), result)

    def test_fill_ai_text_that_is_not_valid_unicode_drops_only_that_record(self):
        raw = self.tmp / "slack.txt"
        raw.write_text(SavedReadTest.SLACK_RAW)
        good = {"id": "1730000000.123456", "thread_id": "1729999000.000100", "at": "1730000000.123456",
                "text": "Can you look at the retry change?", "place": "C0123ABCD", "category": "question"}
        other = {**good, "id": "1730000002.000300", "at": "1730000002.000300", "thread_id": "1730000002.000300"}
        records = [good, {**good, "text": "Truncated \ud800"}, other]
        result = self.engine().read_saved("slack-mentions", raw, records=records)
        self.assertEqual([(i["item"], i["action"]) for i in result["items"]], [(0, "add"), (2, "add")])
        self.assertEqual(result["dropped"], [{"item": 1, "reason": "form.text is not valid Unicode"}])
        self.assertEqual(len(self.store().list()), 2)


class WorkStatusTest(EngineCase):
    def test_fill_paths_drops_a_work_record_without_a_status(self):
        reply = self.tmp / "issues.json"
        issue = {"key": "P-1", "fields": {"updated": "t", "summary": "x", "status": {"name": "To Do"}}}
        reply.write_text(json.dumps([issue, {**issue, "key": "P-2", "fields": {"updated": "t", "summary": "y"}},
                                     {**issue, "key": "P-3", "fields": {**issue["fields"], "status": {"name": None}}}]))
        self.user_source("w", ShadowTest.WORK.format(run=self.command(reply)))
        result = self.engine().read("w", dry_run=True)
        self.assertEqual([i["key"] for i in result["items"]], ["P-1"])
        self.assertEqual(result["dropped"], [{"item": 1, "reason": "form.status missing"},
                                             {"item": 2, "reason": "form.status missing"}])

    def test_fill_ai_drops_a_work_record_without_a_status(self):
        self.user_source("wa", """\
            source: wa
            brings: work
            read: { via: mcp, server: tracker, tool: search }
            fill: ai
            form: { id: Key, at: Updated, text: Summary, status: Status }
            link: "https://tracker.example.com/browse/{id}"
            key: "{id}"
            sort: { all: code }
            """)
        raw = self.tmp / "issues.txt"
        raw.write_text("Key: P-1 Updated: t1\nKey: P-2 Updated: t2\nKey: P-3 Updated: t3\n")
        records = [{"id": "P-1", "at": "t1", "text": "x", "status": "To Do", "category": "all"},
                   {"id": "P-2", "at": "t2", "text": "y", "category": "all"},
                   {"id": "P-3", "at": "t3", "text": "z", "status": None, "category": "all"}]
        result = self.engine().read_saved("wa", raw, records=records, dry_run=True)
        self.assertEqual([i["key"] for i in result["items"]], ["P-1"])
        self.assertEqual(result["dropped"], [{"item": 1, "reason": "form.status missing"},
                                             {"item": 2, "reason": "form.status missing"}])


class LinkKindsTest(EngineCase):
    def status_text(self, reply: Path, link: str) -> str:
        text = (FIXTURES / "user" / "vendor-status.yaml").read_text()
        return text.replace("run: curl -s https://status.example.com/api/v2/incidents/unresolved.json",
                            f"run: '{self.command(reply)}'").replace("link: .incidents[].shortlink", link)

    def test_link_none_makes_cards_without_a_link(self):
        reply = self.reply_copy("status-incidents.json")
        self.user_source("vendor-status", self.status_text(reply, "link: none"))
        result = self.engine().read("vendor-status", dry_run=True)
        self.assertEqual([i["action"] for i in result["items"]], ["add", "add"])
        for item in result["items"]:
            self.assertIsNone(item["link"])
            self.assertIsNone(item["would_add"]["source"]["link"])
            self.assertEqual(item["would_add"]["plain"]["do"]["en"], "Read it and decide what it needs.")

    def test_a_link_pattern_naming_a_missing_field_drops_the_record(self):
        reply = self.reply_copy("status-incidents.json")
        self.user_source("vendor-status", self.status_text(reply, 'link: "https://x.example.com/{place}/{id}"'))
        result = self.engine().read("vendor-status", dry_run=True)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["dropped"], [{"item": 2, "reason": "form.text missing"},
                                             {"item": 0, "reason": "link: {place} missing"},
                                             {"item": 1, "reason": "link: {place} missing"}])


class SavedRawTest(EngineCase):
    def test_a_raw_file_already_in_the_source_folder_keeps_its_report_beside_it(self):
        folder = self.data / "sources" / ".raw" / "gh-mine"
        folder.mkdir(parents=True)
        for name, report in (("abc", "abc.report.json"), ("r.txt", "r.report.json")):
            with self.subTest(name):
                saved = folder / name
                shutil.copy(REPLIES / "gh-pr-list.json", saved)
                result = self.engine().read_saved("gh-mine", saved)
                self.assertEqual(result["raw"], f"sources/.raw/gh-mine/{name}")
                self.assertEqual(json.loads((folder / report).read_text()), result)
        self.assertFalse((self.data / "sources" / ".raw" / ".report.json").exists())

    def test_a_dry_run_of_a_saved_reply_saves_nothing(self):
        saved = self.reply_copy("gh-pr-list.json")
        before = tree(self.data)
        result = self.engine().read_saved("gh-mine", saved, dry_run=True)
        self.assertEqual(tree(self.data), before)
        self.assertTrue(result["dry_run"])
        self.assertIsNone(result["raw"])
        self.assertEqual([(i["action"], i["would_add"]["kind"]) for i in result["items"]],
                         [("add", "pr_review"), ("add", "pr_review")])


class ShadowTest(EngineCase):
    WORK = """\
        source: w
        brings: work
        read:
          via: command
          run: '{run}'
        fill: paths
        form:
          id: .key
          at: .fields.updated
          text: .fields.summary
          status: .fields.status.name
        link: "https://tracker.example.com/browse/{{id}}"
        key: "{{id}}"
        sort: {{ all: code }}
        writes:
          on_start: {{ transition: "In Progress", assign: me }}
          on_merge: {{ transition: "Done" }}
        never: [create_ticket, comment]
        """

    def test_writes_are_refused_in_shadow_mode_and_nothing_outside_the_data_folder_changes(self):
        reply = self.reply_copy("jira-search.json")
        self.user_source("w", self.WORK.format(run=self.command(reply)))
        outside = {p: tree(p) for p in (self.tmp / "bin", self.plugin)}
        outside_files = sorted(p.name for p in self.tmp.iterdir())
        engine = self.engine()
        engine.read("w")  # reading is not a write: shadow mode allows it
        cards = self.store().list()
        self.assertEqual(len(cards), 2)
        for event in ("on_start", "on_merge"):
            with self.subTest(event):
                with mock.patch("subprocess.run") as run, mock.patch("subprocess.Popen") as popen:
                    with self.assertRaises(ShadowModeError) as cm:
                        engine.apply_writes("w", event, cards[0]["id"])
                run.assert_not_called()
                popen.assert_not_called()
                self.assertIn(f"shadow mode: refusing to apply w's writes.{event} for {cards[0]['id']}",
                              str(cm.exception))
        self.assertIsNone(engine.apply_writes("w", "on_pr"))  # nothing to write for that event
        self.assertEqual({p: tree(p) for p in (self.tmp / "bin", self.plugin)}, outside)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), outside_files)
        self.assertEqual(self.store().list(), cards)  # and no card changed either

    def test_require_live_is_called_first_and_live_mode_still_runs_nothing(self):
        reply = self.reply_copy("jira-search.json")
        self.user_source("w", self.WORK.format(run=self.command(reply)))
        with mock.patch("assistant.config.require_live", side_effect=ShadowModeError("stop")) as live:
            with self.assertRaises(ShadowModeError):
                self.engine(Config(mode="live")).apply_writes("w", "on_start")
        live.assert_called_once()
        with mock.patch("subprocess.run") as run:
            with self.assertRaisesRegex(SourceError, "writes.on_start is not run by this version of ap2"):
                self.engine(Config(mode="live")).apply_writes("w", "on_start")
        run.assert_not_called()
        with self.assertRaisesRegex(SourceError, "unknown write event 'on_close'"):
            self.engine().apply_writes("w", "on_close")


if __name__ == "__main__":
    unittest.main()
