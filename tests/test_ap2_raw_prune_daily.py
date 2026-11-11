"""The gate's raw prune runs on the first gate run of each UTC day, judged by the previous run's `last_run_at`.

Every test injects the time and uses temp folders only; none reads the machine's time (the one CLI test passes a
`--now` in the past) and none touches the real home.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT
from assistant import cards, engine, gate
from assistant.store import Store
from tests.test_ap2_cli import temp_inside_the_accounts_claude
from tests.test_ap2_raw_prune import NOW, RAW, RawCase, name

UTC = timezone.utc
SUMMARY = "run: 0 routine slot(s), 0 watch(es) due, 0 pruned, 0 error(s)"
KEYS = ["errors", "last_run_at", "pruned", "routines", "watches_due"]


class DailyCase(RawCase):
    def heartbeat(self, text) -> None:
        """Write the previous run's gate.json as raw text (or bytes)."""
        p = self.data / gate.HEARTBEAT
        if isinstance(text, bytes):
            p.write_bytes(text)
        else:
            p.write_text(text, encoding="utf-8", errors="surrogatepass")

    def previous_run_at(self, at: datetime) -> None:
        self.heartbeat(json.dumps({"last_run_at": cards.stamp(at), "routines": [], "watches_due": [], "pruned": [],
                                   "errors": []}))

    def lines_at(self, at: datetime) -> list:
        prefix = cards.stamp(at) + " "
        return [line[len(prefix):] for line in self.log() if line.startswith(prefix)]

    def counted(self):
        """Patch `engine.prune_raw` and `Store.list` with wrappers that count their calls (and still run)."""
        calls = {"prune_raw": 0, "list": 0}
        real_prune, real_list = engine.prune_raw, Store.list

        def prune_raw(data, now, linked):
            calls["prune_raw"] += 1
            return real_prune(data, now, linked)

        def store_list(s, *a, **k):
            calls["list"] += 1
            return real_list(s, *a, **k)

        return calls, (mock.patch.object(engine, "prune_raw", prune_raw), mock.patch.object(Store, "list", store_list))


# -- criterion 1: the first run of a UTC day prunes ---------------------------------------------------------------

class FirstRunOfTheDayTest(DailyCase):
    def test_a_fresh_data_folder_prunes_on_its_first_run(self):
        old = self.put("chat", NOW - timedelta(days=45))
        self.assertFalse((self.data / gate.HEARTBEAT).exists())
        out = self.gate()
        self.assertFalse(old.exists() or self.report(old).exists())
        self.assertEqual(out["errors"], [])
        self.assertEqual(self.lines_at(NOW), ["pruned 2 raw source file(s) older than 30 days", SUMMARY])

    def test_a_previous_run_on_an_earlier_utc_day_prunes(self):
        # 30 days old at yesterday's run (kept, exactly 30), 30 days and a day at today's first run (removed).
        raw = self.put("chat", NOW - timedelta(days=31))
        self.gate(NOW - timedelta(days=1))
        self.assertTrue(raw.exists())
        self.gate(NOW)
        self.assertFalse(raw.exists() or self.report(raw).exists())

    def test_the_first_second_of_a_utc_day_prunes(self):
        last = datetime(2026, 11, 9, 23, 59, 59, tzinfo=UTC)
        first = datetime(2026, 11, 10, 0, 0, 0, tzinfo=UTC)
        raw = self.put("chat", first - timedelta(days=40))
        self.previous_run_at(last)
        calls, patches = self.counted()
        with patches[0], patches[1]:
            self.gate(first)
        self.assertEqual(calls["prune_raw"], 1)
        self.assertFalse(raw.exists())

    def test_the_utc_date_counts_not_the_date_where_now_was_given(self):
        raw = self.put("chat", NOW - timedelta(days=40))
        # 09:00Z and 11:00Z are one UTC day, though 11:00Z is the next day at +14:00.
        self.previous_run_at(datetime(2026, 11, 10, 9, 0, tzinfo=UTC))
        ahead = datetime(2026, 11, 10, 11, 0, tzinfo=UTC).astimezone(timezone(timedelta(hours=14)))
        self.assertEqual(ahead.day, 11)
        calls, patches = self.counted()
        with patches[0], patches[1]:
            self.gate(ahead)
        self.assertEqual(calls["prune_raw"], 0)
        self.assertTrue(raw.exists())
        # 20:00Z on the 9th, then 01:00Z on the 10th: a new UTC day, though it is still the 9th at -08:00.
        self.previous_run_at(datetime(2026, 11, 9, 20, 0, tzinfo=UTC))
        behind = datetime(2026, 11, 10, 1, 0, tzinfo=UTC).astimezone(timezone(timedelta(hours=-8)))
        self.assertEqual(behind.day, 9)
        with patches[0], patches[1]:
            self.gate(behind)
        self.assertEqual(calls["prune_raw"], 1)
        self.assertFalse(raw.exists())


# -- criteria 2 and 4: later runs the same UTC day skip it and read no card for it ---------------------------------

class SameDayTest(DailyCase):
    def test_five_runs_on_one_utc_day_read_the_cards_once_and_the_next_day_again(self):
        # The review's reproduction: one card links a 45-day-old raw file, so the file is old on every run.
        linked = self.put("chat", NOW - timedelta(days=45))
        self.source_card(self.rel(linked))
        calls, patches = self.counted()
        with patches[0], patches[1]:
            for i in range(5):
                at = NOW + timedelta(minutes=i)
                out = self.gate(at)
                self.assertEqual(out["errors"], [])
                self.assertEqual(sorted(out), KEYS)
                self.assertEqual(self.lines_at(at), [SUMMARY])  # a skipped day writes no extra line
            self.assertEqual(calls, {"prune_raw": 1, "list": 1})
            self.gate(NOW + timedelta(days=1))
        self.assertEqual(calls, {"prune_raw": 2, "list": 2})
        self.assertTrue(linked.exists() and self.report(linked).exists())

    def test_the_check_reads_the_previous_runs_heartbeat_before_this_run_writes_its_own(self):
        seen = []
        real = gate._raw_prune_due

        def due(data, now):
            p = Path(data) / gate.HEARTBEAT
            seen.append(json.loads(p.read_text(encoding="utf-8"))["last_run_at"] if p.exists() else None)
            return real(data, now)

        with mock.patch.object(gate, "_raw_prune_due", due):
            self.gate(NOW)
            self.gate(NOW + timedelta(minutes=1))
            self.gate(NOW + timedelta(days=1))
        self.assertEqual(seen, [None, cards.stamp(NOW), cards.stamp(NOW + timedelta(minutes=1))])

    def test_a_file_that_turns_old_later_in_the_day_goes_on_the_next_days_first_run(self):
        # The cost of the rule: removed up to a day after its 30 days, never early.
        raw = self.put("chat", NOW - timedelta(days=30))
        self.gate(NOW)
        self.gate(NOW + timedelta(hours=1))
        self.assertTrue(raw.exists())
        self.gate(datetime(2026, 11, 11, 0, 0, 0, tzinfo=UTC))
        self.assertFalse(raw.exists() or self.report(raw).exists())

    def test_a_skipped_run_still_writes_gate_json_with_its_five_keys(self):
        self.gate(NOW)
        old = self.put("chat", NOW - timedelta(days=40))
        later = NOW + timedelta(minutes=1)
        out = self.gate(later)
        self.assertTrue(old.exists())
        self.assertEqual(sorted(out), KEYS)
        self.assertEqual(out["last_run_at"], cards.stamp(later))
        self.assertEqual(json.loads((self.data / gate.HEARTBEAT).read_text(encoding="utf-8")), out)


# -- criterion 3: never a reason to skip forever, never a crash ---------------------------------------------------

CASES = [
    ("not UTF-8", b"\xff\xfe{"),
    ("not JSON", "{nope"),
    ("empty", ""),
    ("nested too deeply", "[" * 100_000 + "]" * 100_000),
    ("a list", '["2026-11-10T14:00:00Z"]'),
    ("null", "null"),
    ("text", '"2026-11-10T14:00:00Z"'),
    ("a number", "7"),
    ("no last_run_at", "{}"),
    ("last_run_at null", '{"last_run_at": null}'),
    ("last_run_at a number", '{"last_run_at": 7}'),
    ("last_run_at a list", '{"last_run_at": ["2026-11-10T14:00:00Z"]}'),
    ("last_run_at empty", '{"last_run_at": ""}'),
    ("last_run_at a date only", '{"last_run_at": "2026-11-10"}'),
    ("last_run_at with an offset", '{"last_run_at": "2026-11-10T14:00:00+00:00"}'),
    ("last_run_at month 13", '{"last_run_at": "2026-13-10T14:00:00Z"}'),
    ("last_run_at a lone surrogate", '{"last_run_at": "\\ud800"}'),
    ("last_run_at later the same day", json.dumps({"last_run_at": cards.stamp(NOW + timedelta(hours=1))})),
    ("last_run_at a later day", json.dumps({"last_run_at": cards.stamp(NOW + timedelta(days=2))})),
]


class SafeDefaultTest(DailyCase):
    def check_prunes(self, label):
        old = self.put("chat", NOW - timedelta(days=40))
        out = self.gate()
        self.assertFalse(old.exists() or self.report(old).exists(), label)
        self.assertEqual(out["errors"], [], label)
        self.assertEqual(sorted(out), KEYS, label)
        self.assertEqual(json.loads((self.data / gate.HEARTBEAT).read_text(encoding="utf-8")), out, label)
        # No error and no log line of its own: the same two lines as a fresh data folder's run.
        self.assertEqual(self.log(), [f"{cards.stamp(NOW)} pruned 2 raw source file(s) older than 30 days",
                                      f"{cards.stamp(NOW)} {SUMMARY}"], label)

    def fresh(self, label):
        self.data = self.tmp / f"case-{len(list(self.tmp.iterdir()))}"
        self.data.mkdir()
        (self.data / "config.yaml").write_text("", encoding="utf-8")

    def test_a_damaged_or_odd_gate_json_makes_the_run_prune(self):
        for label, text in CASES:
            with self.subTest(label):
                self.fresh(label)
                self.heartbeat(text)
                self.check_prunes(label)

    def test_a_gate_json_that_cannot_be_read_makes_the_run_prune(self):
        self.previous_run_at(NOW - timedelta(minutes=1))
        p = self.data / gate.HEARTBEAT
        p.chmod(0)
        self.addCleanup(lambda: p.exists() and p.chmod(0o644))
        if os.access(p, os.R_OK):
            self.skipTest("this account can read a file with no permissions (root)")
        self.check_prunes("unreadable")

    def test_the_check_itself_never_raises(self):
        for label, text in CASES:
            with self.subTest(label):
                self.heartbeat(text)
                self.assertIs(gate._raw_prune_due(self.data, NOW), True)
        self.previous_run_at(NOW - timedelta(seconds=1))
        self.assertIs(gate._raw_prune_due(self.data, NOW), False)
        self.previous_run_at(NOW)  # the same second: not later than now
        self.assertIs(gate._raw_prune_due(self.data, NOW), False)
        (self.data / gate.HEARTBEAT).unlink()
        self.assertIs(gate._raw_prune_due(self.data, NOW), True)


class DailyCommandTest(unittest.TestCase):
    def setUp(self):
        if temp_inside_the_accounts_claude():
            self.skipTest("the temp folder is inside the account's ~/.claude, so ap2 refuses every temp data folder")
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-raw-prune-daily-cli-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.data = self.tmp / "data"

    def run_gate(self, now: str):
        env = {k: v for k, v in os.environ.items() if k not in ("AP2_DATA", "LC_ALL")}
        env.update(HOME=str(self.home), AP2_DATA=str(self.data), PYTHON_COLORS="0", TZ="Pacific/Kiritimati")
        return subprocess.run([sys.executable, str(ROOT / "bin" / "ap2"), "gate", "--now", now],
                              capture_output=True, text=True, env=env, errors="replace", timeout=30)

    def test_ap2_gate_with_a_damaged_gate_json_prunes_and_exits_0(self):
        now = datetime(2025, 11, 11, 9, 59, 30, tzinfo=UTC)  # in the past, so --now is valid
        folder = self.data / RAW / "chat"
        folder.mkdir(parents=True)
        old = name(now - timedelta(days=31))
        (folder / old).write_text("{}", encoding="utf-8")
        (self.data / gate.HEARTBEAT).write_text("{nope", encoding="utf-8")
        r = self.run_gate("2025-11-11T09:59:30Z")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertEqual(json.loads(r.stdout)["errors"], [])
        self.assertFalse((folder / old).exists())
        self.assertEqual(sorted(json.loads((self.data / gate.HEARTBEAT).read_text(encoding="utf-8"))), KEYS)
        # A second run a minute later, the same UTC day though the next day in Kiritimati (the TZ the command
        # runs under), so a check on the machine's local date would prune it: an old file put meanwhile stays
        # until the next UTC day.
        later = name(now - timedelta(days=40), "bbbbbb")
        (folder / later).write_text("{}", encoding="utf-8")
        r = self.run_gate("2025-11-11T10:00:30Z")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((folder / later).exists())


if __name__ == "__main__":
    unittest.main()
