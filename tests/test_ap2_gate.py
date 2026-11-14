"""Slice 5: routine files, due slots, missed runs, the run card before last_slot, watches, heartbeat, prune, lock.

Every test injects the time; none sleeps or reads the machine's time zone (each routine names its own `tz`).
"""
from __future__ import annotations

import builtins
import dataclasses
import errno
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT  # noqa: F401  (puts lib/ on sys.path)
from assistant import cards, cap, engine, gate, resources, restore, routines, scan, transcripts
from assistant.config import Config, ConfigError
from assistant.store import Store, write_json
from assistant.workers import IDLE_MIN, Workers

FAKE = str(ROOT / "tests" / "bin" / "fake-claude-v2")
LIVE = Config(mode="live")

UTC = timezone.utc
LA = "America/Los_Angeles"


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def too_deep(path: Path):
    """Make every json.loads of `path`'s present text raise the RecursionError a file nested too deep gives. Nesting
    depth alone does not do it on every Python (3.14's decoder parses 100k levels), so the error is forced; any
    other text is decoded as usual, so the rest of the run reads its files."""
    real, text = json.loads, path.read_text(encoding="utf-8")

    def loads(s, *a, **kw):
        if s == text:
            raise RecursionError("maximum recursion depth exceeded while decoding a JSON array")
        return real(s, *a, **kw)
    return mock.patch.object(json, "loads", loads)


def routine(**over) -> dict:
    r = {"title": "Daily digest", "schedule": {"days": "daily", "at": ["09:00"]}, "tz": "UTC",
         "missed_run": "once_within: 3h", "last_slot": None, "paused": False}
    r.update(over)
    return r


class GateCase(unittest.TestCase):
    cfg = Config()

    def setUp(self):
        self.data = Path(tempfile.mkdtemp(prefix="ap2-gate-")).resolve()
        self.addCleanup(shutil.rmtree, self.data, True)
        (self.data / "routines").mkdir()
        (self.data / "config.yaml").write_text(
            "secondary_language: zh\n" if self.cfg.secondary_language == "zh" else "", encoding="utf-8")

    def put(self, name="digest", **over) -> Path:
        p = self.data / "routines" / f"{name}.json"
        p.write_text(json.dumps(routine(**over)), encoding="utf-8")
        return p

    def file(self, name="digest") -> dict:
        return json.loads((self.data / "routines" / f"{name}.json").read_text(encoding="utf-8"))

    def gate(self, now, **kw) -> dict:
        return gate.run(self.data, now, **kw)

    def store(self, now=None) -> Store:
        return Store(self.data, clock=lambda: now or utc(2026, 11, 10))

    def routine_cards(self) -> list:
        return self.store().list(kind="routine")


# -- the loop (phase 2): workers, watches from the user's sources, reads, the scan session, pass due ---------------

def card_fields(**over) -> dict:
    c = {"kind": "code", "lane": "planned", "feed": "x", "links": [], "pri": "P2", "origin": "found",
         "plain": {k: {"en": "w"} for k in cards.PLAIN_LINES}}
    c.update(over)
    return c


def item(n: int) -> dict:
    return {"id": f"i{n}", "at": "2026-11-10T13:00:00Z", "text": f"item {n}"}


class LoopCase(GateCase):
    """The gate with the fake claude (tests/bin/fake-claude-v2) and a temp home: no real claude, gh or launchctl."""
    T = utc(2026, 11, 10, 14, 0)

    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-gate-loop-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fake_home = self.tmp / "fake-claude"
        self.fake_home.mkdir()
        self.log = self.tmp / "claude.log"
        (self.tmp / "home").mkdir()
        patch = mock.patch.dict(os.environ, {"FAKE_CLAUDE_HOME": str(self.fake_home), "FAKE_CLAUDE_LOG": str(self.log),
                                             "HOME": str(self.tmp / "home")})
        patch.start()
        self.addCleanup(patch.stop)
        self.wt = self.tmp / "worktree"
        self.wt.mkdir()
        (self.data / "sources").mkdir()

    def gate(self, now, **kw) -> dict:
        kw.setdefault("claude", FAKE)
        kw.setdefault("transcripts_dir", self.fake_home / "projects")
        return gate.run(self.data, now, **kw)

    def calls(self) -> list:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def argvs(self) -> list:
        return [c["argv"] for c in self.calls()]

    def bg_calls(self) -> list:
        return [c for c in self.calls() if c["argv"][:1] == ["--bg"]]

    def set_agent(self, short, **fields):
        f = self.fake_home / "agents.json"
        rows = json.loads(f.read_text())
        for a in rows:
            if a["id"] == short:
                a.update(fields)
        f.write_text(json.dumps(rows))

    def store(self, now=None, cfg=None) -> Store:
        return Store(self.data, cfg, clock=lambda: now or self.T)

    def add_card(self, **over) -> str:
        return self.store().add(card_fields(**over), by="test")["id"]

    def workers(self, at=None) -> Workers:
        return Workers(self.data, cfg=LIVE, claude=FAKE, transcripts_dir=self.fake_home / "projects",
                       clock=lambda: at or self.T, store=self.store(at, LIVE))

    def start_worker(self, card_id=None) -> dict:
        return self.workers().start(card_id or self.add_card(), "implement", cwd=self.wt, prompt="Do the work.")

    def run_file(self, card_id, name, body=None) -> Path:
        d = transcripts.run_dir(self.data, card_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(json.dumps(body if body is not None else {"summary": "done"}))
        return d / name

    def source(self, name, text) -> Path:
        p = self.data / "sources" / f"{name}.yaml"
        p.write_text(textwrap.dedent(text), encoding="utf-8")
        return p

    def command_source(self, name, items=(), every="5m", fail=False) -> Path:
        script = self.tmp / f"{name}.py"
        script.write_text("import sys; sys.stderr.write('boom\\n'); sys.exit(3)\n" if fail else
                          f"import json; print(json.dumps({json.dumps(list(items))}))\n")
        every_line = f"\n{' ' * 14}every: {every}" if every is not None else ""  # the template's own indent
        return self.source(name, f"""\
            source: {name}
            brings: messages
            read:
              via: command
              run: '{sys.executable} {script}'{every_line}
            fill: paths
            form: {{ id: .id, at: .at, text: .text }}
            link: none
            sort: {{ all: alert }}
            """)

    def mcp_source(self, name, tool="list_incidents", every="5m") -> Path:
        return self.source(name, f"""\
            source: {name}
            brings: messages
            read:
              via: mcp
              server: vendor
              tool: {tool}
              every: {every}
            fill: paths
            form: {{ id: .id, at: .at, text: .text }}
            link: none
            sort: {{ all: alert }}
            """)

    def scan_record(self) -> dict:
        return json.loads((self.data / "scan-session.json").read_text())

    def gate_log(self) -> str:
        return (self.data / "gate.log").read_text(encoding="utf-8")


# -- criterion 1: routine files -----------------------------------------------------------------------------------

class RoutineFileTest(GateCase):
    BAD = [
        # (what the file holds, the field the error must name)
        ({"schedule": {"days": "daily", "at": ["09:00"]}, "tz": "UTC"}, "title"),
        (routine(title=""), "title"),
        (routine(title=7), "title"),
        (routine(schedule="daily"), "schedule"),
        (routine(schedule={"at": ["09:00"]}), "schedule.days"),
        (routine(schedule={"days": "daily"}), "schedule.at"),
        (routine(schedule={"days": "daily", "at": ["09:00"], "every": "1h"}), "schedule.'every'"),
        (routine(schedule={"days": "weekend", "at": ["09:00"]}), "schedule.days"),
        (routine(schedule={"days": ["mon", "funday"], "at": ["09:00"]}), "schedule.days"),
        (routine(schedule={"days": [], "at": ["09:00"]}), "schedule.days"),
        (routine(schedule={"days": ["mon", "mon"], "at": ["09:00"]}), "schedule.days"),
        (routine(schedule={"days": "daily", "at": []}), "schedule.at"),
        (routine(schedule={"days": "daily", "at": ["9:00"]}), "schedule.at"),
        (routine(schedule={"days": "daily", "at": ["24:00"]}), "schedule.at"),
        (routine(schedule={"days": "daily", "at": ["09:60"]}), "schedule.at"),
        (routine(schedule={"days": "daily", "at": "09:00"}), "schedule.at"),
        (routine(schedule={"days": "daily", "at": ["09:00", "09:00"]}), "schedule.at"),
        (routine(tz="Mars/Olympus_Mons"), "tz"),
        (routine(tz="../../etc/passwd"), "tz"),
        (routine(tz=""), "tz"),
        (routine(tz=None), "tz"),
        (routine(missed_run="catch_up"), "missed_run"),
        (routine(missed_run="once_within: 0h"), "missed_run"),
        (routine(missed_run="once_within: 3"), "missed_run"),
        (routine(missed_run=3), "missed_run"),
        (routine(last_slot="yesterday"), "last_slot"),
        (routine(last_slot="2026-13-01T09:00"), "last_slot"),
        (routine(last_slot="2026-11-07T09:00:00Z"), "last_slot"),
        (routine(paused="no"), "paused"),
        (dict(routine(), owner="me"), "'owner'"),
        ([1, 2], "(file)"),
    ]

    def test_bad_files_are_refused_naming_the_file_and_the_field(self):
        for obj, field in self.BAD:
            with self.subTest(field=field, obj=obj):
                p = self.data / "routines" / "digest.json"
                p.write_text(json.dumps(obj), encoding="utf-8")
                with self.assertRaises(routines.RoutineError) as cm:
                    routines.load(p)
                self.assertIn(f"{p}: {field}:", str(cm.exception))
                self.assertEqual(len(str(cm.exception).splitlines()), 1)

    def test_not_json_and_bad_name_are_refused(self):
        p = self.data / "routines" / "digest.json"
        p.write_text("{nope", encoding="utf-8")
        with self.assertRaises(routines.RoutineError) as cm:
            routines.load(p)
        self.assertIn(f"{p}: (file): not JSON", str(cm.exception))
        q = self.data / "routines" / "Daily Digest.json"
        q.write_text(json.dumps(routine()), encoding="utf-8")
        with self.assertRaises(routines.RoutineError) as cm:
            routines.load(q)
        self.assertIn(f"{q}: file name:", str(cm.exception))

    def test_good_file_reads_with_defaults(self):
        p = self.put(schedule={"days": ["mon", "fri"], "at": ["17:30", "09:00"]}, tz=LA)
        obj = json.loads(p.read_text())
        del obj["missed_run"], obj["last_slot"], obj["paused"]
        p.write_text(json.dumps(obj))
        r = routines.load(p)
        self.assertEqual((r.name, r.title, r.tz_name), ("digest", "Daily digest", LA))
        self.assertEqual(r.days, frozenset({0, 4}))
        self.assertEqual(r.at, ((9, 0), (17, 30)))
        self.assertEqual(r.missed_run, "once_within: 3h")  # the design's recommendation is the default
        self.assertEqual((r.last_slot, r.paused), (None, False))
        for value, days in (("daily", 7), ("weekdays", 5)):
            self.assertEqual(len(routines.parse(p, routine(schedule={"days": value, "at": ["09:00"]})).days), days)

    def test_one_bad_routine_never_stops_the_others(self):
        self.put("good", last_slot="2026-11-09T09:00")
        self.put("bad", tz="Nowhere/Land")
        (self.data / "routines" / "broken.json").write_text("{", encoding="utf-8")
        (self.data / "routines" / ".good.json.x.tmp").write_text("{", encoding="utf-8")  # a writer's temp file
        (self.data / "routines" / "notes.txt").write_text("ignored", encoding="utf-8")
        out = self.gate(utc(2026, 11, 10, 9, 1))
        self.assertEqual([f["routine"] for f in out["routines"]], ["good"])
        self.assertEqual(sorted(e["routine"] for e in out["errors"]), ["bad", "broken"])
        for e in out["errors"]:
            self.assertIn(e["file"], e["error"])
        self.assertIn("tz", next(e for e in out["errors"] if e["routine"] == "bad")["error"])
        self.assertEqual(self.file("good")["last_slot"], "2026-11-10T09:00")
        self.assertEqual(len(self.routine_cards()), 1)

    def test_a_lone_surrogate_key_is_refused_and_the_heartbeat_is_still_written(self):
        # JSON can hold "\ud800" (half of a surrogate pair); the error must not carry it raw, or gate.json cannot be
        # written as UTF-8.
        self.put("good", last_slot="2026-11-09T09:00")
        top = json.dumps(routine()).replace('"paused"', '"\\ud800": 1, "paused"')
        nested = json.dumps(routine()).replace('"at"', '"\\udfff": 1, "at"')
        (self.data / "routines" / "top.json").write_text(top, encoding="utf-8")
        (self.data / "routines" / "nested.json").write_text(nested, encoding="utf-8")
        out = self.gate(utc(2026, 11, 10, 9, 1))
        self.assertEqual([f["routine"] for f in out["routines"]], ["good"])
        self.assertEqual(sorted(e["routine"] for e in out["errors"]), ["nested", "top"])
        self.assertIn("'\\ud800': unknown key", next(e for e in out["errors"] if e["routine"] == "top")["error"])
        self.assertIn("schedule.'\\udfff': unknown key",
                      next(e for e in out["errors"] if e["routine"] == "nested")["error"])
        self.assertEqual(json.loads((self.data / "gate.json").read_text(encoding="utf-8")), out)

    def test_a_lone_surrogate_title_is_refused_and_the_others_still_run(self):
        a = self.data / "routines" / "a.json"
        a.write_text(json.dumps({"title": "Bad \ud800", "schedule": {"days": "daily", "at": ["06:00"]},
                                 "tz": "UTC"}), encoding="utf-8")
        self.put("b", schedule={"days": "daily", "at": ["06:00"]}, last_slot="2026-11-08T06:00")
        out = self.gate(utc(2026, 11, 10, 6, 30))
        self.assertEqual([(f["routine"], f["action"]) for f in out["routines"]], [("b", "late")])
        self.assertEqual(len(out["errors"]), 1)
        self.assertEqual(out["errors"][0]["routine"], "a")
        self.assertIn(f"{a}: title: title is not valid Unicode", out["errors"][0]["error"])
        self.assertEqual(len(self.routine_cards()), 1)
        self.assertEqual(json.loads((self.data / "gate.json").read_text(encoding="utf-8")), out)

    def test_any_value_utf8_cannot_write_is_one_error_for_its_routine(self):
        a = self.put("a", last_slot="2026-11-09T09:00")
        self.put("b", last_slot="2026-11-09T09:00")
        real = routines.write_last_slot

        def write_last_slot(path, slot, write):
            if Path(path) == a:
                raise UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates not allowed")
            return real(path, slot, write)

        with mock.patch.object(routines, "write_last_slot", write_last_slot):
            out = self.gate(utc(2026, 11, 10, 9, 1))
        self.assertEqual([f["routine"] for f in out["routines"]], ["b"])
        self.assertEqual([e["routine"] for e in out["errors"]], ["a"])
        self.assertEqual(self.file("b")["last_slot"], "2026-11-10T09:00")
        self.assertEqual(json.loads((self.data / "gate.json").read_text(encoding="utf-8")), out)


# -- criterion 2: due slots ---------------------------------------------------------------------------------------

class DueSlotTest(GateCase):
    def r(self, **over):
        return routines.parse(self.data / "routines" / "digest.json", routine(**over))

    def test_a_late_tick_still_sees_the_slot(self):
        for now in (utc(2026, 11, 10, 9, 0, 0), utc(2026, 11, 10, 9, 0, 40), utc(2026, 11, 10, 9, 3)):
            with self.subTest(now=now):
                d = routines.due(self.r(last_slot="2026-11-09T09:00"), now)
                self.assertEqual((d.slot, d.action), ("2026-11-10T09:00", "run"))

    def test_before_the_slot_and_after_it_was_handled_nothing_is_due(self):
        self.assertIsNone(routines.due(self.r(last_slot="2026-11-09T09:00"), utc(2026, 11, 10, 8, 59, 59)))
        self.assertIsNone(routines.due(self.r(last_slot="2026-11-10T09:00"), utc(2026, 11, 10, 9, 3)))

    def test_the_gate_starts_a_slot_once_across_ticks(self):
        self.put(last_slot="2026-11-09T09:00")
        first = self.gate(utc(2026, 11, 10, 9, 0, 40))
        second = self.gate(utc(2026, 11, 10, 9, 1, 40))
        self.assertEqual([f["slot"] for f in first["routines"]], ["2026-11-10T09:00"])
        self.assertEqual(second["routines"], [])
        self.assertEqual(len(self.routine_cards()), 1)

    def test_a_weekday_routine_has_no_saturday_slot(self):
        r = self.r(schedule={"days": "weekdays", "at": ["09:00"]}, tz=LA, last_slot="2026-11-13T09:00")
        sat = datetime(2026, 11, 14, 9, 30, tzinfo=routines.ZoneInfo(LA))  # Saturday 09:30 in LA
        self.assertEqual(sat.weekday(), 5)
        self.assertIsNone(routines.due(r, sat.astimezone(UTC)))
        self.assertEqual(routines.latest_slot(r, sat.astimezone(UTC))[0], "2026-11-13T09:00")  # Friday's
        mon = datetime(2026, 11, 16, 9, 1, tzinfo=routines.ZoneInfo(LA))
        self.assertEqual(routines.due(r, mon.astimezone(UTC)).slot, "2026-11-16T09:00")

    def test_slots_are_read_in_the_routines_time_zone(self):
        r = self.r(tz=LA, last_slot="2026-11-09T09:00")
        self.assertIsNone(routines.due(r, utc(2026, 11, 10, 9, 1)))  # 01:01 in LA
        self.assertEqual(routines.due(r, utc(2026, 11, 10, 17, 1)).slot, "2026-11-10T09:00")  # 09:01 PST
        tokyo = self.r(tz="Asia/Tokyo", last_slot="2026-11-09T09:00")
        self.assertEqual(routines.due(tokyo, utc(2026, 11, 10, 0, 2)).slot, "2026-11-10T09:00")

    def test_the_latest_of_several_times_in_a_day(self):
        r = self.r(schedule={"days": "daily", "at": ["18:00", "09:00", "13:00"]}, last_slot="2026-11-10T09:00")
        self.assertEqual(routines.due(r, utc(2026, 11, 10, 13, 2)).slot, "2026-11-10T13:00")
        self.assertEqual(routines.latest_slot(r, utc(2026, 11, 10, 8, 0))[0], "2026-11-09T18:00")

    def test_clocks_going_back_give_each_slot_once(self):
        # 2026-11-01 in Los Angeles: 01:00-02:00 happens twice (PDT 08:00-09:00 UTC, then PST 09:00-10:00 UTC).
        self.put(schedule={"days": "daily", "at": ["01:30"]}, tz=LA, last_slot="2026-10-31T01:30")
        made = []
        for minute in range(0, 4 * 60, 7):  # a tick every 7 minutes from 07:00 to 11:00 UTC
            made += self.gate(utc(2026, 11, 1, 7) + timedelta(minutes=minute))["routines"]
        self.assertEqual([m["slot"] for m in made], ["2026-11-01T01:30"])
        self.assertEqual(self.routine_cards()[0]["slot_at"], "2026-11-01T08:30:00Z")  # the first 01:30

    def test_clocks_going_forward_give_each_slot_once(self):
        # 2026-03-08 in Los Angeles: 02:00-03:00 does not happen. 02:30 runs once, at the jump (03:00 PDT, 10:00 UTC).
        self.put(schedule={"days": "daily", "at": ["02:30"]}, tz=LA, last_slot="2026-03-07T02:30")
        made = []
        for minute in range(0, 4 * 60, 7):  # a tick every 7 minutes from 08:02 UTC: one lands at 10:01
            made += self.gate(utc(2026, 3, 8, 8, 2) + timedelta(minutes=minute))["routines"]
        self.assertEqual([(m["slot"], m["action"]) for m in made], [("2026-03-08T02:30", "run")])
        self.assertEqual(self.routine_cards()[0]["slot_at"], "2026-03-08T10:00:00Z")

    def test_a_time_that_does_not_exist_is_the_moment_the_clocks_jump(self):
        tz = routines.ZoneInfo(LA)
        self.assertEqual(routines.slot_time(datetime(2026, 3, 8, 2, 30), tz), utc(2026, 3, 8, 10))
        self.assertEqual(routines.slot_time(datetime(2026, 3, 8, 2, 0), tz), utc(2026, 3, 8, 10))
        self.assertEqual(routines.slot_time(datetime(2026, 3, 8, 3, 0), tz), utc(2026, 3, 8, 10))
        self.assertEqual(routines.slot_time(datetime(2026, 3, 8, 1, 59), tz), utc(2026, 3, 8, 9, 59))

    def tick_every_minute(self, start, hours) -> list:
        made = []
        for minute in range(hours * 60):
            made += self.gate(start + timedelta(minutes=minute))["routines"]
        return made

    def test_two_slots_in_the_gap_run_once_as_the_later_one(self):
        # 02:30 does not exist on 2026-03-08 in LA and 03:00 is the moment the clocks jump: both are 10:00 UTC.
        self.put(schedule={"days": "daily", "at": ["02:30", "03:00"]}, tz=LA, last_slot="2026-03-07T03:00")
        made = self.tick_every_minute(utc(2026, 3, 8, 8), 4)
        self.assertEqual([(m["slot"], m["action"]) for m in made], [("2026-03-08T03:00", "run")])
        self.assertEqual([(c["key"], c["slot_at"]) for c in self.routine_cards()],
                         [("digest@2026-03-08T03:00", "2026-03-08T10:00:00Z")])
        self.assertEqual(self.file()["last_slot"], "2026-03-08T03:00")

    def test_a_slot_in_the_gap_is_not_lost_behind_a_later_slot(self):
        # Read with the offset before the change, 02:30 would be 10:30 UTC, after 03:15 (10:15 UTC); 03:15 would
        # run first and 02:30 would then be dropped as older than last_slot.
        self.put(schedule={"days": "daily", "at": ["02:30", "03:15"]}, tz=LA, last_slot="2026-03-07T03:15")
        made = self.tick_every_minute(utc(2026, 3, 8, 8), 4)
        self.assertEqual([(m["slot"], m["action"]) for m in made],
                         [("2026-03-08T02:30", "run"), ("2026-03-08T03:15", "run")])
        self.assertEqual([c["slot_at"] for c in self.routine_cards()],
                         ["2026-03-08T10:00:00Z", "2026-03-08T10:15:00Z"])
        self.assertEqual(self.file()["last_slot"], "2026-03-08T03:15")

    def test_a_new_routine_counts_from_its_next_slot(self):
        # last_slot null: a slot inside the grace runs; an older one is not a missed run (the routine did not exist).
        self.put(missed_run="once")
        out = self.gate(utc(2026, 11, 10, 9, 30))
        self.assertEqual([(f["action"], f["card"]) for f in out["routines"]], [("start", None)])
        self.assertEqual(self.file()["last_slot"], "2026-11-10T09:00")
        self.assertEqual(self.routine_cards(), [])
        self.assertEqual(self.gate(utc(2026, 11, 11, 9, 1))["routines"][0]["action"], "run")
        self.put("fresh")
        self.assertEqual(self.gate(utc(2026, 11, 11, 9, 4))["routines"][0]["action"], "run")
        self.assertEqual(len(self.routine_cards()), 2)

    def test_paused_routine_is_never_due(self):
        self.put(paused=True)
        self.assertEqual(self.gate(utc(2026, 11, 10, 9, 1))["routines"], [])
        self.assertIsNone(self.file()["last_slot"])
        self.assertEqual(self.routine_cards(), [])


# -- criterion 3: missed runs -------------------------------------------------------------------------------------

class MissedRunTest(GateCase):
    def run_at(self, now, **over):
        self.put(last_slot="2026-11-09T09:00", **over)
        out = self.gate(now)
        return out["routines"], self.routine_cards()

    def test_within_the_grace_is_on_time(self):
        found, made = self.run_at(utc(2026, 11, 10, 9, 5), missed_run="skip")
        self.assertEqual(found[0]["action"], "run")
        self.assertEqual((made[0]["lane"], made[0]["late"], made[0]["skipped"]), ("running", False, False))

    def test_once_within_3h_runs_a_missed_slot_inside_3_hours(self):
        found, made = self.run_at(utc(2026, 11, 10, 11, 59))
        self.assertEqual((found[0]["action"], found[0]["late_minutes"]), ("late", 179))
        self.assertEqual((made[0]["lane"], made[0]["late"], made[0]["skipped"]), ("running", True, False))
        self.assertEqual(self.file()["last_slot"], "2026-11-10T09:00")

    def test_once_within_3h_skips_a_missed_slot_outside_3_hours(self):
        found, made = self.run_at(utc(2026, 11, 10, 12, 1))
        self.assertEqual(found[0]["action"], "skip")
        self.assertEqual((made[0]["lane"], made[0]["skipped"]), ("done", True))
        self.assertIn("Skipped", made[0]["plain"]["where"]["en"])
        self.assertEqual(self.file()["last_slot"], "2026-11-10T09:00")

    def test_the_default_is_once_within_3h(self):
        self.put(last_slot="2026-11-09T09:00")
        obj = self.file()
        del obj["missed_run"]
        write_json(self.data / "routines" / "digest.json", obj)
        self.assertEqual(self.gate(utc(2026, 11, 10, 11, 0))["routines"][0]["action"], "late")
        self.assertNotIn("missed_run", self.file())  # writing last_slot does not add the default
        self.assertEqual(self.gate(utc(2026, 11, 11, 12, 30))["routines"][0]["action"], "skip")

    def test_skip_files_a_skipped_run(self):
        found, made = self.run_at(utc(2026, 11, 10, 9, 6), missed_run="skip")
        self.assertEqual(found[0]["action"], "skip")
        self.assertEqual((made[0]["lane"], made[0]["skipped"], made[0]["key"]),
                         ("done", True, "digest@2026-11-10T09:00"))

    def test_once_runs_it_late_however_late(self):
        found, made = self.run_at(utc(2026, 11, 10, 23, 0), missed_run="once")
        self.assertEqual(found[0]["action"], "late")
        self.assertEqual(made[0]["lane"], "running")

    def test_only_the_latest_missed_slot_never_a_burst(self):
        # Asleep from Monday 09:00 to Wednesday 10:00: only Wednesday's slot is considered.
        found, made = self.run_at(utc(2026, 11, 11, 10, 0), missed_run="once",
                                  schedule={"days": "daily", "at": ["09:00", "09:30"]})
        self.assertEqual([(f["slot"], f["action"]) for f in found], [("2026-11-11T09:30", "late")])
        self.assertEqual(len(made), 1)
        self.assertEqual(self.gate(utc(2026, 11, 11, 10, 1))["routines"], [])


# -- criterion 4: the card first, then last_slot ------------------------------------------------------------------

class CardFirstTest(GateCase):
    def test_run_card_shape(self):
        self.put(last_slot="2026-11-09T09:00")
        out = self.gate(utc(2026, 11, 10, 9, 2))
        card = self.store().get(out["routines"][0]["card"])
        self.assertEqual((card["kind"], card["lane"], card["key"], card["routine"], card["slot"]),
                         ("routine", "running", "digest@2026-11-10T09:00", "digest", "2026-11-10T09:00"))
        self.assertEqual(card["audit"][0]["by"], "gate")
        self.assertEqual(card["created_at"], "2026-11-10T09:02:00Z")  # the injected time, not the real one
        for line in cards.PLAIN_LINES:
            self.assertTrue(card["plain"][line]["en"].strip())

    def test_a_crash_between_the_card_and_last_slot_makes_one_card(self):
        self.put(last_slot="2026-11-09T09:00")
        with mock.patch.object(routines, "write_last_slot", side_effect=RuntimeError("killed")):
            with self.assertRaises(RuntimeError):
                self.gate(utc(2026, 11, 10, 9, 1))
        self.assertEqual(self.file()["last_slot"], "2026-11-09T09:00")
        self.assertEqual(len(self.routine_cards()), 1)
        out = self.gate(utc(2026, 11, 10, 9, 2))
        self.assertEqual(out["routines"][0]["action"], "exists")
        self.assertEqual(self.file()["last_slot"], "2026-11-10T09:00")
        made = self.routine_cards()
        self.assertEqual([c["key"] for c in made], ["digest@2026-11-10T09:00"])
        self.assertEqual(out["routines"][0]["card"], made[0]["id"])

    def test_card_is_written_before_last_slot(self):
        self.put(last_slot="2026-11-09T09:00")
        order = []
        real_add, real_write = Store.add, routines.write_last_slot

        def add(store, card, by):
            order.append("card")
            return real_add(store, card, by)

        def write(path, slot, w):
            order.append("last_slot")
            return real_write(path, slot, w)

        with mock.patch.object(Store, "add", add), mock.patch.object(routines, "write_last_slot", write):
            self.gate(utc(2026, 11, 10, 9, 1))
        self.assertEqual(order, ["card", "last_slot"])

    def test_last_slot_write_keeps_other_fields_and_is_atomic(self):
        p = self.put(last_slot="2026-11-09T09:00", title="Digest 摘要")
        with mock.patch("assistant.store.os.replace", wraps=os.replace) as rep:
            self.gate(utc(2026, 11, 10, 9, 1))
        self.assertIn(str(p), [str(c.args[1]) for c in rep.call_args_list])
        obj = self.file()
        self.assertEqual((obj["title"], obj["last_slot"], obj["tz"]), ("Digest 摘要", "2026-11-10T09:00", "UTC"))

    def test_a_card_store_error_is_reported_and_last_slot_kept(self):
        self.put(last_slot="2026-11-09T09:00")
        bad = self.data / "items" / "2026-11" / "c-20261101-000000-abcdef.json"
        bad.parent.mkdir(parents=True)
        bad.write_text("{", encoding="utf-8")
        out = self.gate(utc(2026, 11, 10, 9, 1))
        self.assertEqual(out["routines"], [])
        self.assertIn("c-20261101-000000-abcdef", out["errors"][0]["error"])
        self.assertEqual(self.file()["last_slot"], "2026-11-09T09:00")

    def test_a_routine_card_whose_key_is_not_text_is_passed_over(self):
        r = routines.parse(self.data / "routines" / "other.json", routine())
        d = routines.Due("2026-11-09T09:00", utc(2026, 11, 9, 9), "run", timedelta(0))
        odd = dict(gate.run_card(r, d, ("en",)), key=["a", "b"])
        self.store().add(odd, by="test")
        self.put(last_slot="2026-11-09T09:00")
        out = self.gate(utc(2026, 11, 10, 9, 1))
        self.assertEqual([(f["routine"], f["action"]) for f in out["routines"]], [("digest", "run")])
        self.assertEqual(out["errors"], [])
        self.assertIn("digest@2026-11-10T09:00", [c["key"] for c in self.routine_cards()])
        self.assertEqual(self.file()["last_slot"], "2026-11-10T09:00")


class TwoLanguageCardTest(GateCase):
    cfg = Config(secondary_language="zh")

    def test_the_title_has_one_slot_per_language(self):
        self.put(last_slot="2026-11-09T09:00", title="Digest 摘要")
        self.gate(utc(2026, 11, 10, 9, 2))
        [card] = self.routine_cards()
        self.assertEqual(card["title"], {"en": "Digest 摘要", "zh": "Digest 摘要"})

    def test_run_card_writes_the_routine_title_in_each_language(self):
        r = routines.parse(self.data / "routines" / "x.json", routine())
        d = routines.Due("2026-11-10T09:00", utc(2026, 11, 10, 9), "run", timedelta(0))
        self.assertEqual(gate.run_card(r, d, ("en",))["title"], {"en": "Daily digest"})
        self.assertEqual(gate.run_card(r, d, ("ja", "zh-Hans"))["title"],
                         {"ja": "Daily digest", "zh-Hans": "Daily digest"})

    def test_plain_words_in_every_configured_language(self):
        self.put(last_slot="2026-11-09T09:00", missed_run="skip")
        self.put("cleanup", last_slot="2026-11-09T09:00", title="Worktree cleanup")  # 30 min late: within 3h
        self.gate(utc(2026, 11, 10, 9, 30))
        made = self.routine_cards()
        self.assertEqual(len(made), 2)
        for card in made:
            for line in cards.PLAIN_LINES:
                self.assertEqual(sorted(card["plain"][line]), ["en", "zh"])
                self.assertTrue(all(t.strip() for t in card["plain"][line].values()))
        skipped = next(c for c in made if c["skipped"])
        self.assertIn("已跳过", skipped["plain"]["where"]["zh"])  # 已跳过

    def test_a_language_without_built_in_words_gets_the_english_lines(self):
        r = routines.parse(self.data / "routines" / "x.json", routine())
        d = routines.Due("2026-11-10T09:00", utc(2026, 11, 10, 9), "run", timedelta(0))
        c = gate.run_card(r, d, ("ja", "zh-Hans"))
        self.assertEqual(c["plain"]["do"]["ja"], gate._words("en", r, d)["do"])
        self.assertEqual(c["plain"]["do"]["zh-Hans"], gate._words("zh", r, d)["do"])


# -- criterion 5: watches -----------------------------------------------------------------------------------------

class WatchTest(GateCase):
    T = utc(2026, 11, 10, 14, 0)

    def test_due_when_the_interval_has_passed_and_the_time_is_recorded(self):
        watches = [{"name": "slack", "every": "5m", "last_check_at": "2026-11-10T13:55:00Z"},
                   {"name": "github", "every": "15m", "last_check_at": "2026-11-10T13:50:00Z"},
                   {"name": "jira", "every": "1h", "last_check_at": None}]
        out = self.gate(self.T, watches=watches)
        self.assertEqual(out["watches_due"], ["slack", "jira"])
        saved = json.loads((self.data / "watches.json").read_text())
        self.assertEqual(saved, {"slack": {"last_check_at": "2026-11-10T14:00:00Z"},
                                 "jira": {"last_check_at": "2026-11-10T14:00:00Z"}})
        heartbeat = json.loads((self.data / "gate.json").read_text())
        self.assertEqual(heartbeat["watches_due"], ["slack", "jira"])

    def test_the_recorded_time_is_used_by_the_next_run(self):
        watches = [{"name": "slack", "every": "5m"}]
        self.assertEqual(self.gate(self.T, watches=watches)["watches_due"], ["slack"])
        self.assertEqual(self.gate(self.T + timedelta(minutes=4, seconds=59), watches=watches)["watches_due"], [])
        self.assertEqual(self.gate(self.T + timedelta(minutes=5), watches=watches)["watches_due"], ["slack"])

    def test_a_bad_watch_is_reported_and_the_others_still_run(self):
        watches = [{"name": "bad", "every": "5 minutes"}, {"every": "5m"}, {"name": "x", "every": "5m",
                   "last_check_at": "noon"}, {"name": "ok", "every": "1h"}]
        out = self.gate(self.T, watches=watches)
        self.assertEqual(out["watches_due"], ["ok"])
        self.assertEqual([e["watch"] for e in out["errors"]], ["bad", None, "x"])

    def test_a_damaged_watches_file_is_reported_and_the_run_goes_on(self):
        for text in ("{", "[1]", "\xff"):
            with self.subTest(text=text):
                path = self.data / "watches.json"
                path.write_bytes(text.encode("latin-1"))
                (self.data / "gate.json").unlink(missing_ok=True)
                (self.data / "gate.log").unlink(missing_ok=True)
                out = self.gate(self.T, watches=[{"name": "slack", "every": "5m"}])
                self.assertEqual(out["watches_due"], ["slack"])
                self.assertEqual(len(out["errors"]), 1)
                self.assertEqual(out["errors"][0]["watches"], str(path))
                self.assertIn(str(path), out["errors"][0]["error"])
                self.assertEqual(json.loads((self.data / "gate.json").read_text()), out)
                self.assertIn(f"watches: error: {path}", (self.data / "gate.log").read_text())
                self.assertEqual(json.loads(path.read_text()), {"slack": {"last_check_at": "2026-11-10T14:00:00Z"}})

    def test_a_watches_file_nested_too_deep_is_a_damaged_file(self):
        path = self.data / "watches.json"
        path.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
        with too_deep(path):
            out = self.gate(self.T, watches=[{"name": "slack", "every": "5m"}])
        self.assertEqual(out["watches_due"], ["slack"])
        self.assertEqual([e.get("watches") for e in out["errors"]], [str(path)])
        self.assertIn("recursion", out["errors"][0]["error"])
        self.assertEqual(json.loads((self.data / "gate.json").read_text(encoding="utf-8")), out)
        self.assertIn(f"watches: error: {path}", (self.data / "gate.log").read_text())
        self.assertEqual(json.loads(path.read_text()), {"slack": {"last_check_at": "2026-11-10T14:00:00Z"}})

    def test_a_lone_surrogate_in_watches_json_is_a_damaged_file(self):
        path = self.data / "watches.json"
        path.write_text('{"\\ud800": {"last_check_at": null}}', encoding="utf-8")
        out = self.gate(self.T, watches=[{"name": "slack", "every": "5m"}])
        self.assertEqual(out["watches_due"], ["slack"])
        self.assertEqual(len(out["errors"]), 1)
        self.assertEqual(out["errors"][0]["watches"], str(path))
        self.assertIn("not valid Unicode", out["errors"][0]["error"])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")),
                         {"slack": {"last_check_at": "2026-11-10T14:00:00Z"}})
        self.assertEqual(json.loads((self.data / "gate.json").read_text(encoding="utf-8")), out)

    def test_a_watch_name_utf8_cannot_write_is_refused(self):
        out = self.gate(self.T, watches=[{"name": "bad\ud800", "every": "5m"}, {"name": "ok", "every": "5m"}])
        self.assertEqual(out["watches_due"], ["ok"])
        self.assertEqual([e["watch"] for e in out["errors"]], ["bad\\ud800"])  # escaped, so it can be written
        self.assertIn("not valid Unicode", out["errors"][0]["error"])
        self.assertEqual(json.loads((self.data / "watches.json").read_text(encoding="utf-8")),
                         {"ok": {"last_check_at": "2026-11-10T14:00:00Z"}})
        self.assertEqual(json.loads((self.data / "gate.json").read_text(encoding="utf-8")), out)

    def test_a_look_is_not_logged_as_a_card(self):
        self.gate(self.T, watches=[{"name": "slack", "every": "5m"}])
        self.assertEqual(self.store().list(), [])


# -- criterion 6: heartbeat, log lines, prune ---------------------------------------------------------------------

class HeartbeatTest(GateCase):
    def test_every_run_writes_gate_json_atomically(self):
        with mock.patch("assistant.store.os.replace", wraps=os.replace) as rep:
            out = self.gate(utc(2026, 11, 10, 14, 30, 5))
        self.assertIn(str(self.data / "gate.json"), [str(c.args[1]) for c in rep.call_args_list])
        heartbeat = json.loads((self.data / "gate.json").read_text())
        self.assertEqual(heartbeat, out)
        self.assertEqual(heartbeat["last_run_at"], "2026-11-10T14:30:05Z")
        self.assertEqual(sorted(heartbeat), ["errors", "last_run_at", "pass_due", "pruned", "reads", "routines",
                                             "watches_due", "workers"])
        self.gate(utc(2026, 11, 10, 14, 31, 5))
        self.assertEqual(json.loads((self.data / "gate.json").read_text())["last_run_at"], "2026-11-10T14:31:05Z")

    def test_a_file_name_that_is_not_utf8_is_escaped_in_gate_json(self):
        # On Linux a file name with the byte 0xe9 lists as "caf\udce9.json"; it is refused, and its error text
        # must still be written.
        bad = self.data / "routines" / "caf\udce9.json"
        with mock.patch.object(routines, "files", return_value=[bad]):
            out = self.gate(utc(2026, 11, 10, 9, 1))
        self.assertEqual(len(out["errors"]), 1)
        self.assertEqual(out["errors"][0]["routine"], "caf\\udce9")
        self.assertIn("caf\\udce9.json", out["errors"][0]["file"])
        self.assertIn("caf\\udce9.json", out["errors"][0]["error"])
        self.assertEqual(json.loads((self.data / "gate.json").read_text(encoding="utf-8")), out)

    def test_a_non_utc_now_is_written_as_utc(self):
        la = datetime(2026, 11, 10, 6, 30, 5, tzinfo=routines.ZoneInfo(LA))
        self.assertEqual(self.gate(la)["last_run_at"], "2026-11-10T14:30:05Z")

    def test_every_log_line_starts_with_a_utc_time(self):
        self.put(last_slot="2026-11-09T09:00")
        self.put("bad", tz="Nowhere/Land")
        self.gate(utc(2026, 11, 10, 9, 1), watches=[{"name": "slack", "every": "5m"}])
        self.gate(utc(2026, 11, 10, 9, 2))
        lines = (self.data / "gate.log").read_text(encoding="utf-8").splitlines()
        self.assertGreaterEqual(len(lines), 5)
        for line in lines:
            stamp, _, message = line.partition(" ")
            self.assertEqual(cards.parse_stamp(stamp).tzinfo, UTC, line)
            self.assertTrue(message.strip(), line)
        self.assertTrue(lines[0].startswith("2026-11-10T09:01:00Z "))
        self.assertTrue(lines[-1].startswith("2026-11-10T09:02:00Z run:"))

    def test_a_multi_line_message_stays_one_line(self):
        log = gate.Log(self.data, utc(2026, 11, 10))
        log("first\nsecond\r\nthird")
        self.assertEqual((self.data / "gate.log").read_text(), "2026-11-10T00:00:00Z first second third\n")

    def test_a_big_log_is_rolled_over(self):
        (self.data / "gate.log").write_text("x" * (gate.LOG_MAX_BYTES + 1))
        self.gate(utc(2026, 11, 10))
        self.assertTrue((self.data / "gate.log.1").stat().st_size > gate.LOG_MAX_BYTES)
        self.assertTrue((self.data / "gate.log").read_text().startswith("2026-11-10T00:00:00Z "))

    def test_each_run_prunes_restore_points_older_than_30_days(self):
        t0 = utc(2026, 10, 1, 12)
        s = Store(self.data, clock=lambda: t0)
        c = s.add({"kind": "code", "lane": "planned", "feed": "x", "links": [], "pri": "P2", "origin": "found",
                   "plain": {k: {"en": "words"} for k in cards.PLAIN_LINES}}, by="test")
        s.update(c["id"], {"pri": "P1"}, by="test")
        point = s.get(c["id"])["audit"][-1]["restore"]
        self.assertTrue(restore.path(self.data, point).exists())
        self.assertEqual(self.gate(t0 + timedelta(days=30))["pruned"], [])  # kept at exactly 30 days
        out = self.gate(t0 + timedelta(days=31))
        self.assertEqual(out["pruned"], [point])
        self.assertFalse(restore.path(self.data, point).exists())


# -- criterion 7: no AI, no network, no writes outside the data folder, one run at a time -------------------------

class NoOutsideEffectsTest(LoopCase):
    def test_no_process_no_socket_no_exec_and_writes_only_inside_data(self):
        self.put(last_slot="2026-11-09T09:00")
        self.put("late", last_slot="2026-11-09T09:00", missed_run="skip")
        t0 = utc(2026, 10, 1)
        s = Store(self.data, clock=lambda: t0)
        c = s.add({"kind": "code", "lane": "planned", "feed": "x", "links": [], "pri": "P2", "origin": "found",
                   "plain": {k: {"en": "w"} for k in cards.PLAIN_LINES}}, by="test")
        s.update(c["id"], {"pri": "P1"}, by="test")

        def refuse(*a, **k):
            raise AssertionError(f"the gate must not start a process or open a socket: {a!r}")

        written = []
        real_open, real_os_open = builtins.open, os.open

        def watch_open(file, mode="r", *a, **k):
            if any(ch in mode for ch in "wax+"):
                written.append(os.fspath(file))
            return real_open(file, mode, *a, **k)

        def watch_os_open(path, flags, *a, **k):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
                written.append(os.fspath(path))
            return real_os_open(path, flags, *a, **k)

        patches = [mock.patch.object(subprocess, "Popen", side_effect=refuse),
                   mock.patch.object(subprocess, "run", side_effect=refuse),
                   mock.patch.object(socket, "socket", side_effect=refuse),
                   mock.patch.object(socket, "create_connection", side_effect=refuse),
                   mock.patch.object(builtins, "open", watch_open),
                   mock.patch.object(os, "open", watch_os_open)]
        patches += [mock.patch.object(os, name, side_effect=refuse)
                    for name in ("system", "execv", "execve", "execvp", "execvpe", "execl", "execlp", "spawnv",
                                 "posix_spawn", "posix_spawnp", "fork") if hasattr(os, name)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        out = self.gate(utc(2026, 11, 10, 9, 30), watches=[{"name": "slack", "every": "5m"}])
        for p in reversed(patches):
            p.stop()
        self.assertEqual(len(out["routines"]), 2)
        self.assertEqual(len(out["pruned"]), 1)
        self.assertTrue(written)
        for path in written:
            self.assertTrue(Path(path).resolve().is_relative_to(self.data), path)

    def test_in_shadow_mode_with_a_worker_and_due_watches_every_effect_stays_inside(self):
        self.start_worker()  # started in live mode, before anything is watched; the gate runs in shadow mode
        self.command_source("feed", items=[item(1)])
        self.mcp_source("vendor-a")
        started, written = [], []
        real_popen, real_open, real_os_open = subprocess.Popen, builtins.open, os.open

        class Watched(real_popen):
            def __init__(self, args, *a, **k):
                started.append(list(args))
                super().__init__(args, *a, **k)

        def watch_open(file, mode="r", *a, **k):
            if any(ch in mode for ch in "wax+"):
                written.append(os.fspath(file))
            return real_open(file, mode, *a, **k)

        def watch_os_open(path, flags, *a, **k):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
                written.append(os.fspath(path))
            return real_os_open(path, flags, *a, **k)

        patches = [mock.patch.object(subprocess, "Popen", Watched), mock.patch.object(builtins, "open", watch_open),
                   mock.patch.object(os, "open", watch_os_open),
                   mock.patch.object(engine.Engine, "apply_writes", side_effect=AssertionError("no writes"))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        out = self.gate(self.T)
        for p in reversed(patches):
            p.stop()
        self.assertEqual(out["watches_due"], ["feed", "vendor-a"])
        self.assertEqual([r["watch"] for r in out["reads"]], ["feed"])
        self.assertEqual(out["errors"], [])
        self.assertTrue(written)
        for path in written:
            if path != os.devnull:  # subprocess opens it for a child's stdin (DEVNULL); nothing is written there
                self.assertTrue(Path(path).resolve().is_relative_to(self.data), path)
        script = [sys.executable, str(self.tmp / "feed.py")]
        self.assertIn(script, started)
        bg = [a for a in started if a[1:2] == ["--bg"]]
        self.assertEqual(len(bg), 1)
        self.assertTrue(bg[0][bg[0].index("-n") + 1].startswith("ap2 scan · "), bg[0])
        for argv in started:
            with self.subTest(argv=argv):
                self.assertTrue(argv == script or (argv[0] == FAKE and argv[1] in ("agents", "stop", "--bg")), argv)

    def test_a_held_lock_makes_a_second_run_do_nothing(self):
        import fcntl
        self.put(last_slot="2026-11-09T09:00")
        fd = os.open(str(self.data / ".gate.lock"), os.O_CREAT | os.O_RDWR, 0o644)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        before = sorted(p.relative_to(self.data) for p in self.data.rglob("*"))
        out = self.gate(utc(2026, 11, 10, 9, 1))
        self.assertEqual(out, {"locked": True, "lock": str(self.data / ".gate.lock")})
        self.assertEqual(sorted(p.relative_to(self.data) for p in self.data.rglob("*")), before)
        self.assertEqual(self.file()["last_slot"], "2026-11-09T09:00")
        fcntl.flock(fd, fcntl.LOCK_UN)
        self.assertEqual(len(self.gate(utc(2026, 11, 10, 9, 1))["routines"]), 1)

    def test_a_lock_that_cannot_be_taken_is_a_config_error_naming_the_lock(self):
        self.put(last_slot="2026-11-09T09:00")
        no_locks = OSError(errno.ENOLCK, "No locks available")
        with mock.patch.object(gate.fcntl, "flock", side_effect=no_locks), \
                mock.patch.object(gate.os, "close", wraps=os.close) as close:
            with self.assertRaises(ConfigError) as cm:
                self.gate(utc(2026, 11, 10, 9, 1))
        self.assertIn(f"cannot lock the gate {self.data / '.gate.lock'}", str(cm.exception))
        self.assertEqual(close.call_count, 1)  # the lock file is closed again
        self.assertEqual(self.file()["last_slot"], "2026-11-09T09:00")
        self.assertFalse((self.data / "gate.json").exists())

    def test_a_bad_config_is_a_config_error(self):
        (self.data / "config.yaml").write_text("mode: loud\n")
        with self.assertRaises(ConfigError):
            self.gate(utc(2026, 11, 10))

    def test_now_must_be_aware(self):
        with self.assertRaises(ValueError):
            self.gate(datetime(2026, 11, 10, 9))


# -- follow-ups: a clock that went back, a changed routine tz, fatal errors in gate.log ----------------------------

class ClockWentBackTest(GateCase):
    NOW = utc(2026, 11, 10, 9, 1)

    def test_a_routine_last_slot_later_than_now_is_reported_and_the_file_kept(self):
        p = self.put(last_slot="2026-11-11T09:01")  # one day after now (the routine's tz is UTC)
        before = p.read_bytes()
        out = self.gate(self.NOW)
        self.assertEqual(out["routines"], [])
        msg = (f"{p}: last_slot 2026-11-11T09:01 is later than now (2026-11-10T09:01:00Z); the clock may have gone "
               f"back. Set last_slot to null or an earlier slot to run it again.")
        self.assertEqual(out["errors"], [{"routine": "digest", "file": str(p), "error": msg}])
        self.assertEqual(json.loads((self.data / "gate.json").read_text(encoding="utf-8")), out)
        log = (self.data / "gate.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual([line for line in log if "digest" in line],
                         [f"2026-11-10T09:01:00Z routine digest: error: {msg}"])
        self.assertEqual(p.read_bytes(), before)
        self.assertEqual(self.routine_cards(), [])

    def test_the_slot_is_read_in_the_routines_tz(self):
        # 2026-11-10T06:00 in Los Angeles is 14:00 UTC, five hours after now.
        p = self.put(tz=LA, last_slot="2026-11-10T06:00", last_slot_tz=LA)
        out = self.gate(self.NOW)
        self.assertEqual(len(out["errors"]), 1)
        self.assertIn(f"{p}: last_slot 2026-11-10T06:00 is later than now", out["errors"][0]["error"])

    def test_a_last_slot_at_now_or_inside_the_grace_is_no_error(self):
        for slot in ("2026-11-10T09:01", "2026-11-10T09:06"):
            with self.subTest(last_slot=slot):
                p = self.put(last_slot=slot)
                before = p.read_bytes()
                out = self.gate(self.NOW)
                self.assertEqual((out["routines"], out["errors"]), ([], []))
                self.assertEqual(p.read_bytes(), before)
        self.put(last_slot="2026-11-10T09:07")  # past the 5-minute grace
        self.assertEqual(len(self.gate(self.NOW)["errors"]), 1)

    def test_a_slot_at_the_ends_of_the_calendar_never_stops_the_run(self):
        # Year 9999 in Los Angeles and year 1 in Tokyo have no UTC time (Python cannot hold it).
        far = self.put("far", tz=LA, last_slot="9999-12-31T23:59")
        self.put("old", tz="Asia/Tokyo", last_slot="0001-01-01T00:00")
        self.put("ok", last_slot="2026-11-09T09:00")
        out = self.gate(self.NOW)
        self.assertEqual([e["routine"] for e in out["errors"]], ["far"])
        self.assertIn(f"{far}: last_slot 9999-12-31T23:59 is later than now", out["errors"][0]["error"])
        self.assertEqual(sorted(f["routine"] for f in out["routines"]), ["ok", "old"])

    def test_a_watch_time_later_than_now_is_reported_and_repaired(self):
        T = utc(2026, 11, 10, 14, 0)
        write_json(self.data / "watches.json", {"slack": {"last_check_at": "2026-11-11T14:00:00Z"}})
        out = self.gate(T, watches=[{"name": "slack", "every": "5m"}])
        self.assertEqual(out["watches_due"], ["slack"])
        self.assertEqual(out["errors"], [{"watch": "slack", "error": "last_check_at 2026-11-11T14:00:00Z is later "
                                          "than now (2026-11-10T14:00:00Z); the clock may have gone back"}])
        self.assertEqual(json.loads((self.data / "watches.json").read_text()),
                         {"slack": {"last_check_at": "2026-11-10T14:00:00Z"}})
        self.assertIn("watch slack: error: last_check_at 2026-11-11T14:00:00Z is later than now",
                      (self.data / "gate.log").read_text())
        again = self.gate(T + timedelta(minutes=1), watches=[{"name": "slack", "every": "5m"}])
        self.assertEqual((again["watches_due"], again["errors"]), ([], []))  # recovered on its own

    def test_a_given_watch_time_later_than_now_is_reported_too(self):
        T = utc(2026, 11, 10, 14, 0)
        out = self.gate(T, watches=[{"name": "slack", "every": "5m", "last_check_at": "2026-11-10T15:00:00Z"}])
        self.assertEqual(out["watches_due"], ["slack"])
        self.assertEqual([e["watch"] for e in out["errors"]], ["slack"])
        self.assertIn("the clock may have gone back", out["errors"][0]["error"])

    def test_a_watch_time_inside_the_grace_is_no_error(self):
        T = utc(2026, 11, 10, 14, 0)
        out = self.gate(T, watches=[{"name": "slack", "every": "5m", "last_check_at": "2026-11-10T14:05:00Z"}])
        self.assertEqual((out["watches_due"], out["errors"]), ([], []))

    def test_a_saved_watch_time_inside_the_grace_is_no_error(self):
        T = utc(2026, 11, 10, 14, 0)
        for saved in ("2026-11-10T14:03:00Z", "2026-11-10T14:05:00Z"):
            with self.subTest(saved=saved):
                write_json(self.data / "watches.json", {"slack": {"last_check_at": saved}})
                out = self.gate(T, watches=[{"name": "slack", "every": "5m"}])
                self.assertEqual((out["watches_due"], out["errors"]), ([], []))
                self.assertEqual(json.loads((self.data / "watches.json").read_text()),
                                 {"slack": {"last_check_at": saved}})

    def test_after_a_tz_change_the_error_quotes_the_files_slot_and_its_old_zone(self):
        # Tokyo 09:00 on Nov 11 is 00:00 UTC, which is 19:00 on Nov 10 in New York: later than now.
        p = self.put(tz="America/New_York", last_slot="2026-11-11T09:00", last_slot_tz="Asia/Tokyo")
        before = p.read_bytes()
        out = self.gate(self.NOW)
        msg = (f"{p}: last_slot 2026-11-11T09:00 in Asia/Tokyo (2026-11-10T19:00 in America/New_York) is later than "
               f"now (2026-11-10T09:01:00Z); the clock may have gone back. Set last_slot to null or an earlier slot "
               f"to run it again.")
        self.assertEqual(out["errors"], [{"routine": "digest", "file": str(p), "error": msg}])
        self.assertIn(f"routine digest: error: {msg}", (self.data / "gate.log").read_text(encoding="utf-8"))
        self.assertEqual(p.read_bytes(), before)

    def test_a_given_watch_time_ahead_on_every_run_still_waits_for_every(self):
        watches = [{"name": "slack", "every": "1h", "last_check_at": "2026-11-11T17:00:00Z"}]
        error = {"watch": "slack", "error": "last_check_at 2026-11-11T17:00:00Z is later than now "
                                            "(2026-11-10T17:01:00Z); the clock may have gone back"}
        T = utc(2026, 11, 10, 17, 1)
        out = self.gate(T, watches=watches)
        self.assertEqual((out["watches_due"], out["errors"]), (["slack"], [error]))
        for later in (T + timedelta(minutes=5), T + timedelta(minutes=10)):
            with self.subTest(now=later):
                out = self.gate(later, watches=watches)
                self.assertEqual(out["watches_due"], [])  # the saved time counts, and 1h has not passed
                self.assertEqual(out["errors"], [{"watch": "slack", "error": "last_check_at 2026-11-11T17:00:00Z is "
                                                  f"later than now ({cards.stamp(later)}); the clock may have gone "
                                                  "back"}])
                self.assertEqual(json.loads((self.data / "watches.json").read_text()),
                                 {"slack": {"last_check_at": "2026-11-10T17:01:00Z"}})
        self.assertEqual(self.gate(T + timedelta(hours=1), watches=watches)["watches_due"], ["slack"])

    def test_a_saved_watch_time_ahead_is_repaired_even_when_a_given_time_counts(self):
        T = utc(2026, 11, 10, 14, 0)
        write_json(self.data / "watches.json", {"w": {"last_check_at": "2026-11-10T16:00:00Z"}})  # 2h ahead
        watches = [{"name": "w", "every": "24h", "last_check_at": "2026-11-10T13:00:00Z"}]
        out = self.gate(T, watches=watches)
        self.assertEqual(out["watches_due"], ["w"])
        self.assertEqual(out["errors"], [{"watch": "w", "error": "last_check_at 2026-11-10T16:00:00Z is later than "
                                          "now (2026-11-10T14:00:00Z); the clock may have gone back"}])
        self.assertEqual(json.loads((self.data / "watches.json").read_text()),
                         {"w": {"last_check_at": "2026-11-10T14:00:00Z"}})
        again = self.gate(T + timedelta(hours=1), watches=watches)
        self.assertEqual((again["watches_due"], again["errors"]), ([], []))

    def test_a_saved_and_a_given_time_both_ahead_are_one_error_quoting_the_later(self):
        write_json(self.data / "watches.json", {"slack": {"last_check_at": "2026-11-11T14:00:00Z"}})
        out = self.gate(utc(2026, 11, 10, 14, 0),
                        watches=[{"name": "slack", "every": "5m", "last_check_at": "2026-11-12T00:00:00Z"}])
        self.assertEqual(out["errors"], [{"watch": "slack", "error": "last_check_at 2026-11-12T00:00:00Z is later "
                                          "than now (2026-11-10T14:00:00Z); the clock may have gone back"}])
        self.assertEqual(out["watches_due"], ["slack"])

    def test_a_now_at_the_end_of_the_calendar_does_not_overflow(self):
        end = datetime(9999, 12, 31, 23, 58, tzinfo=UTC)
        names, errors = gate.due_watches(
            self.data, [{"name": "w", "every": "1h", "last_check_at": "9999-12-31T23:00:00Z"}], end)
        self.assertEqual((names, errors), ([], []))  # 58 minutes is not 1h
        self.put(last_slot="2026-11-10T09:00")
        out = self.gate(end)
        self.assertEqual([e for e in out["errors"] if "routine" in e], [])

    def test_a_refused_watch_is_logged_refused_when_another_watch_of_its_name_runs(self):
        T = utc(2026, 11, 10, 14, 0)
        out = self.gate(T, watches=[{"name": "a", "every": "5x"}, {"name": "a", "every": "1h"}])
        self.assertEqual(out["watches_due"], ["a"])
        self.assertEqual(len(out["errors"]), 1)
        log = (self.data / "gate.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual([line for line in log if line.startswith("2026-11-10T14:00:00Z watch a:")],
                         [f"2026-11-10T14:00:00Z watch a: refused: {out['errors'][0]['error']}"])


class ZoneNextToLastSlotTest(GateCase):
    def test_a_run_writes_last_slot_and_its_zone(self):
        for name, tz, now in (("utc", "UTC", utc(2026, 11, 10, 9, 1)), ("pacific", LA, utc(2026, 11, 10, 17, 1))):
            with self.subTest(tz=tz):
                self.put(name, tz=tz, last_slot="2026-11-09T09:00")
                out = self.gate(now)
                self.assertEqual([(f["routine"], f["action"]) for f in out["routines"]], [(name, "run")])
                obj = self.file(name)
                slot = "2026-11-10T09:00"
                self.assertEqual((obj["last_slot"], obj["last_slot_tz"]), (slot, tz))

    def test_a_new_routine_start_writes_the_zone_too(self):
        self.put(tz=LA, last_slot=None)
        out = self.gate(utc(2026, 11, 10, 20, 0))  # 12:00 in Los Angeles: the 09:00 slot is older than the routine
        self.assertEqual([f["action"] for f in out["routines"]], ["start"])
        self.assertEqual((self.file()["last_slot"], self.file()["last_slot_tz"]), ("2026-11-10T09:00", LA))

    def test_an_old_file_without_the_zone_loads_and_behaves_as_before(self):
        p = self.put(tz=LA, last_slot="2026-11-09T09:00")
        self.assertNotIn("last_slot_tz", self.file())
        r = routines.load(p)
        self.assertEqual(r.last_slot, "2026-11-09T09:00")
        self.assertIsNone(routines.due(r, utc(2026, 11, 10, 16, 59)))
        self.assertEqual(routines.due(r, utc(2026, 11, 10, 17, 1)).action, "run")
        out = self.gate(utc(2026, 11, 10, 17, 1))
        self.assertEqual([(f["slot"], f["action"]) for f in out["routines"]], [("2026-11-10T09:00", "run")])
        self.assertEqual(out["errors"], [])

    def test_the_zone_is_text_or_null(self):
        p = self.put(last_slot_tz=None)
        self.assertIsNone(routines.load(p).last_slot)
        for bad in (7, ["UTC"], True):
            with self.subTest(last_slot_tz=bad):
                p = self.put(last_slot="2026-11-09T09:00", last_slot_tz=bad)
                with self.assertRaises(routines.RoutineError) as cm:
                    routines.load(p)
                self.assertIn(f"{p}: last_slot_tz:", str(cm.exception))


class ChangedZoneTest(GateCase):
    def test_a_changed_tz_converts_last_slot_so_no_slot_is_skipped(self):
        # Eastern 09:00 is Pacific 06:00, so the Pacific 09:00 slot has not run yet.
        p = self.put(tz=LA, last_slot="2026-11-10T09:00", last_slot_tz="America/New_York")
        self.assertEqual(routines.load(p).last_slot, "2026-11-10T06:00")
        before = p.read_bytes()
        routines.load(p)
        self.assertEqual(p.read_bytes(), before)  # only write_last_slot rewrites the file
        now = datetime(2026, 11, 10, 9, 1, tzinfo=routines.ZoneInfo(LA))
        out = self.gate(now)
        self.assertEqual([(f["slot"], f["action"]) for f in out["routines"]], [("2026-11-10T09:00", "run")])
        self.assertEqual([c["key"] for c in self.routine_cards()], ["digest@2026-11-10T09:00"])
        obj = self.file()
        self.assertEqual((obj["last_slot"], obj["last_slot_tz"]), ("2026-11-10T09:00", LA))
        self.assertEqual(self.gate(now + timedelta(minutes=1))["routines"], [])

    def test_the_same_zone_changes_nothing(self):
        p = self.put(tz=LA, last_slot="2026-11-10T09:00", last_slot_tz=LA)
        self.assertEqual(routines.load(p).last_slot, "2026-11-10T09:00")
        out = self.gate(datetime(2026, 11, 10, 9, 1, tzinfo=routines.ZoneInfo(LA)))
        self.assertEqual((out["routines"], out["errors"]), ([], []))

    def test_a_slot_that_cannot_be_moved_to_the_new_zone_is_one_error(self):
        # The old zone's time is past the calendar's end, the new zone's time is, and the old one is before its start.
        for last, old, tz in (("9999-12-31T23:59", LA, "Asia/Tokyo"), ("9999-12-31T20:00", "UTC", "Asia/Tokyo"),
                              ("0001-01-01T00:00", "Asia/Tokyo", LA)):
            with self.subTest(last_slot=last, last_slot_tz=old, tz=tz):
                p = self.put(tz=tz, last_slot=last, last_slot_tz=old)
                with self.assertRaises(routines.RoutineError) as cm:
                    routines.load(p)
                self.assertIn(f"{p}: last_slot:", str(cm.exception))
        self.put("ok", last_slot="2026-11-09T09:00")
        out = self.gate(utc(2026, 11, 10, 9, 1))
        self.assertEqual([f["routine"] for f in out["routines"]], ["ok"])
        self.assertEqual([e["routine"] for e in out["errors"]], ["digest"])

    def test_an_unknown_old_zone_is_refused_on_last_slot_tz(self):
        for bad in ("Mars/Olympus_Mons", "", "../../etc/passwd"):
            with self.subTest(last_slot_tz=bad):
                p = self.put(tz=LA, last_slot="2026-11-10T09:00", last_slot_tz=bad)
                with self.assertRaises(routines.RoutineError) as cm:
                    routines.load(p)
                self.assertIn(f"{p}: last_slot_tz:", str(cm.exception))
                self.assertEqual(len(str(cm.exception).splitlines()), 1)

    def test_an_unknown_old_zone_is_refused_with_no_last_slot_too(self):
        p = self.put(tz=LA, last_slot=None, last_slot_tz="Mars/Olympus_Mons")
        with self.assertRaises(routines.RoutineError) as cm:
            routines.load(p)
        self.assertTrue(str(cm.exception).startswith(f"{p}: last_slot_tz:"), str(cm.exception))


class MovedIntoRepeatedHourTest(GateCase):
    """A `tz` change whose moved slot lands in the second pass of the hour the clocks go back. Chicago 01:30 (CDT)
    on 2026-11-01 is 06:30 UTC, which is New York 01:30 (EST), the second time New York reads 01:30 that night.
    The 01:45 slot first happens at 05:45 UTC, before that run, so it must not run; 02:00 (07:00 UTC) must."""
    NY = "America/New_York"
    AT = ["01:30", "01:45", "02:00"]
    MOMENT = utc(2026, 11, 1, 6, 30)

    def put_moved(self, name="digest", **over) -> Path:
        fields = dict(tz=self.NY, schedule={"days": "daily", "at": self.AT}, last_slot="2026-11-01T01:30",
                      last_slot_tz="America/Chicago")
        fields.update(over)
        return self.put(name, **fields)

    # criterion 1: the moved moment is kept

    def test_a_moved_slot_keeps_its_exact_moment(self):
        r = routines.load(self.put_moved())
        self.assertEqual(r.last_slot, "2026-11-01T01:30")
        self.assertEqual(r.moved_at, self.MOMENT)
        self.assertEqual(r.moved_at.utcoffset(), timedelta(0))
        self.assertEqual(r.moved_from, "2026-11-01T01:30 in America/Chicago")
        west = routines.load(self.put("west", tz=LA, last_slot="2026-11-10T09:00", last_slot_tz=self.NY))
        self.assertEqual((west.last_slot, west.moved_at), ("2026-11-10T06:00", utc(2026, 11, 10, 14, 0)))

    def test_a_move_into_the_first_pass_keeps_the_first_pass_moment(self):
        # Chicago 00:30 (CDT) is 05:30 UTC, which is New York 01:30 (EDT), the first pass. The moved text reads the
        # same as on the second pass, so the moment must come from the old zone, not from the text. The 01:45 slot
        # (05:45 UTC) came after that run, so it must still run.
        r = routines.load(self.put_moved(last_slot="2026-11-01T00:30"))
        self.assertEqual((r.last_slot, r.moved_at), ("2026-11-01T01:30", utc(2026, 11, 1, 5, 30)))
        d = routines.due(r, utc(2026, 11, 1, 6, 46))
        self.assertEqual((d.slot, d.action), ("2026-11-01T01:45", "late"))

    def test_a_move_from_the_old_zones_skipped_hour_keeps_the_jump_moment(self):
        # New York 02:30 on 2026-03-08 does not happen: the clocks jump from 02:00 to 03:00 (EDT), 07:00 UTC. The old
        # slot is in the hour New York skips, so its moment is the jump, London 07:00 (GMT). The London 07:15 slot
        # after it must still run on time.
        r = routines.load(self.put(tz="Europe/London", schedule={"days": "daily", "at": ["07:15"]},
                                   last_slot="2026-03-08T02:30", last_slot_tz=self.NY))
        self.assertEqual((r.last_slot, r.moved_at), ("2026-03-08T07:00", utc(2026, 3, 8, 7, 0)))
        d = routines.due(r, utc(2026, 3, 8, 7, 16))
        self.assertEqual((d.slot, d.action), ("2026-03-08T07:15", "run"))

    def test_a_slot_that_was_not_moved_has_no_moment(self):
        for label, over in (("no last_slot_tz", {"last_slot_tz": None}), ("no key", {}),
                            ("same zone", {"last_slot_tz": self.NY}),
                            ("no last_slot", {"last_slot": None})):
            with self.subTest(label):
                fields = dict(tz=self.NY, schedule={"days": "daily", "at": self.AT}, last_slot="2026-11-01T01:30")
                if label == "no last_slot":
                    fields["last_slot_tz"] = "America/Chicago"
                fields.update(over)
                r = routines.load(self.put(**fields))
                self.assertIsNone(r.moved_at)
                self.assertIsNone(r.moved_from)

    # criterion 2: due skips a slot whose moment is not after the moved moment

    def test_a_slot_whose_first_pass_came_before_the_move_is_not_due(self):
        r = routines.load(self.put_moved())
        self.assertIsNone(routines.due(r, utc(2026, 11, 1, 6, 46)))
        d = routines.due(r, utc(2026, 11, 1, 7, 1))
        self.assertEqual((d.slot, d.at, d.action, d.late_by),
                         ("2026-11-01T02:00", utc(2026, 11, 1, 7, 0), "run", timedelta(minutes=1)))

    def test_a_slot_at_the_moved_moment_is_not_due_and_one_after_it_is(self):
        # The 01:45 slot is 05:45 UTC. A moment at it means the slot ran; a moment a minute before it means it did not.
        r = routines.load(self.put_moved())
        now = utc(2026, 11, 1, 6, 46)
        self.assertIsNone(routines.due(dataclasses.replace(r, moved_at=utc(2026, 11, 1, 5, 45)), now))
        d = routines.due(dataclasses.replace(r, moved_at=utc(2026, 11, 1, 5, 44)), now)
        self.assertEqual((d.slot, d.action), ("2026-11-01T01:45", "late"))

    def test_an_on_time_slot_that_came_before_the_move_is_not_due(self):
        # Chicago 01:01 (CDT) is 06:01 UTC, which is New York 01:01 (EST), the second pass. At 06:02 UTC the latest
        # slot is 01:57, whose first pass was 05:57 UTC: 5 minutes ago, inside the grace, so it reads as on time.
        # It came before the run, so it must not run.
        p = self.put_moved(schedule={"days": "daily", "at": ["01:01", "01:57", "02:30"]},
                           last_slot="2026-11-01T01:01")
        r = routines.load(p)
        self.assertEqual((r.last_slot, r.moved_at), ("2026-11-01T01:01", utc(2026, 11, 1, 6, 1)))
        self.assertIsNone(routines.due(r, utc(2026, 11, 1, 6, 2)))
        before = p.read_bytes()
        out = self.gate(utc(2026, 11, 1, 6, 2))
        self.assertEqual((out["routines"], out["errors"]), ([], []))
        self.assertEqual(self.routine_cards(), [])
        self.assertEqual(p.read_bytes(), before)

    # criterion 3: nothing changes without a move

    def test_the_same_schedule_with_no_move_gives_the_base_result(self):
        want = routines.Due("2026-11-01T01:45", utc(2026, 11, 1, 5, 45), "late", timedelta(minutes=61))
        for label, over in (("no last_slot_tz", {"last_slot_tz": None}), ("same zone", {"last_slot_tz": self.NY})):
            with self.subTest(label):
                r = routines.load(self.put_moved(**over))
                self.assertEqual(routines.due(r, utc(2026, 11, 1, 6, 46)), want)
                self.assertEqual(routines.slot_at(r), utc(2026, 11, 1, 5, 30))  # fold 0, as before

    def test_a_move_west_still_runs_the_new_zones_slot(self):
        # Eastern 09:00 is Pacific 06:00: the moment is 14:00 UTC and the Pacific 09:00 slot (17:00 UTC) is after it.
        r = routines.load(self.put(tz=LA, last_slot="2026-11-10T09:00", last_slot_tz=self.NY))
        self.assertEqual(routines.due(r, utc(2026, 11, 10, 17, 1)).action, "run")

    # criterion 4: the clock-went-back check uses the real moment

    def test_slot_at_is_the_moved_moment(self):
        self.assertEqual(routines.slot_at(routines.load(self.put_moved())), self.MOMENT)

    def test_the_clock_went_back_check_compares_the_moved_moment(self):
        p = self.put_moved()
        before = p.read_bytes()
        out = self.gate(utc(2026, 11, 1, 6, 28))  # the moment is 2 minutes ahead: inside the grace
        self.assertEqual((out["routines"], out["errors"]), ([], []))
        self.assertEqual(p.read_bytes(), before)
        for now in (utc(2026, 11, 1, 5, 30), utc(2026, 11, 1, 5, 0)):  # 60 and 90 minutes ahead
            with self.subTest(now=now):
                out = self.gate(now)
                self.assertEqual(out["routines"], [])
                msg = (f"{p}: last_slot 2026-11-01T01:30 in America/Chicago (2026-11-01T01:30 in {self.NY}) is "
                       f"later than now ({cards.stamp(now)}); the clock may have gone back. Set last_slot to null "
                       f"or an earlier slot to run it again.")
                self.assertEqual(out["errors"], [{"routine": "digest", "file": str(p), "error": msg}])
                self.assertEqual(p.read_bytes(), before)

    # criterion 5: the gate run agrees

    def test_the_gate_runs_no_slot_twice_across_the_move(self):
        p = self.put_moved()
        before = p.read_bytes()
        out = self.gate(utc(2026, 11, 1, 6, 46))
        self.assertEqual((out["routines"], out["errors"]), ([], []))
        self.assertEqual(self.routine_cards(), [])
        self.assertEqual(p.read_bytes(), before)
        out = self.gate(utc(2026, 11, 1, 7, 1))
        self.assertEqual([(f["slot"], f["action"]) for f in out["routines"]], [("2026-11-01T02:00", "run")])
        self.assertEqual(out["errors"], [])
        self.assertEqual([c["key"] for c in self.routine_cards()], ["digest@2026-11-01T02:00"])
        obj = self.file()
        self.assertEqual((obj["last_slot"], obj["last_slot_tz"]), ("2026-11-01T02:00", self.NY))
        self.assertEqual(self.gate(utc(2026, 11, 1, 7, 2))["routines"], [])


class FatalLineTest(GateCase):
    def test_a_config_that_does_not_parse_writes_a_fatal_line_and_raises(self):
        (self.data / "config.yaml").write_text("mode: [\n", encoding="utf-8")
        with self.assertRaises(ConfigError) as cm:
            self.gate(utc(2026, 11, 10, 9, 1))
        lines = (self.data / "gate.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[-1], f"2026-11-10T09:01:00Z fatal: {cm.exception}")
        self.assertIn("config.yaml", str(cm.exception))

    def test_an_unwritable_log_gives_the_same_error_and_nothing_else(self):
        (self.data / "config.yaml").write_text("mode: [\n", encoding="utf-8")
        with self.assertRaises(ConfigError) as first:
            self.gate(utc(2026, 11, 10, 9, 1))
        (self.data / "gate.log").unlink()
        (self.data / "gate.log").mkdir()  # a folder where the log should be
        with self.assertRaises(ConfigError) as cm:
            self.gate(utc(2026, 11, 10, 9, 1))
        self.assertIs(type(cm.exception), ConfigError)
        self.assertEqual(str(cm.exception), str(first.exception))
        self.assertNotIsInstance(cm.exception.__context__, OSError)
        self.assertEqual(list((self.data / "gate.log").iterdir()), [])

    def test_a_lock_that_cannot_be_taken_writes_a_fatal_line(self):
        no_locks = OSError(errno.ENOLCK, "No locks available")
        with mock.patch.object(gate.fcntl, "flock", side_effect=no_locks):
            with self.assertRaises(ConfigError) as cm:
                self.gate(utc(2026, 11, 10, 9, 1))
        self.assertEqual((self.data / "gate.log").read_text(encoding="utf-8"),
                         f"2026-11-10T09:01:00Z fatal: {cm.exception}\n")

    def test_a_now_with_no_utc_stamp_still_gives_the_same_config_error(self):
        # Year 1, 00:30 at UTC+1 is before the start of the calendar in UTC, so the log line has no time stamp.
        (self.data / ".gate.lock").mkdir()  # the lock cannot be opened: a ConfigError
        now = datetime(1, 1, 1, 0, 30, tzinfo=timezone(timedelta(hours=1)))
        with self.assertRaises(ConfigError) as cm:
            self.gate(now)
        self.assertIs(type(cm.exception), ConfigError)
        self.assertIn("cannot open the gate lock", str(cm.exception))
        self.assertFalse((self.data / "gate.log").exists())

    def test_no_data_folder_writes_nothing(self):
        missing = self.data / "nowhere"
        with self.assertRaises(ConfigError):
            gate.run(missing, utc(2026, 11, 10, 9, 1))
        self.assertFalse(missing.exists())

    def test_an_os_error_from_the_data_folder_writes_a_fatal_line(self):
        (self.data / "gate.json").mkdir()  # a folder where the heartbeat should be: writing it fails
        with self.assertRaises(ConfigError) as cm:
            self.gate(utc(2026, 11, 10, 9, 1))
        self.assertIsInstance(cm.exception.__cause__, OSError)
        self.assertIn("cannot use the data folder", str(cm.exception))
        lines = (self.data / "gate.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[-1], f"2026-11-10T09:01:00Z fatal: {cm.exception}")

    def test_a_held_lock_is_not_fatal(self):
        import fcntl
        fd = os.open(str(self.data / ".gate.lock"), os.O_CREAT | os.O_RDWR, 0o644)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.assertTrue(self.gate(utc(2026, 11, 10, 9, 1))["locked"])
        self.assertFalse((self.data / "gate.log").exists())


# -- invisible text: a routine title, a tz and a watch name follow cards.blank -------------------------------------

INVISIBLE = ("\ufeff", "\ufeff \x1c")


class InvisibleTextTest(GateCase):
    T = utc(2026, 11, 10, 14, 0)

    def test_a_title_of_only_invisible_characters_is_refused(self):
        for title in INVISIBLE:
            with self.subTest(title=title):
                p = self.put(title=title)
                with self.assertRaises(routines.RoutineError) as cm:
                    routines.load(p)
                self.assertIn(f"{p}: title: title must be a non-empty text", str(cm.exception))

    def test_a_tz_of_only_invisible_characters_gets_the_tz_words(self):
        for tz in INVISIBLE:
            with self.subTest(tz=tz):
                p = self.put(tz=tz)
                with self.assertRaises(routines.RoutineError) as cm:
                    routines.load(p)
                self.assertIn(f"{p}: tz: tz must be an IANA time zone name", str(cm.exception))

    def test_a_title_with_words_next_to_an_invisible_character_is_kept(self):
        for title in ("\ufeffT1", "a\ufeff"):
            with self.subTest(title=title):
                self.assertEqual(routines.load(self.put(title=title)).title, title)

    def test_a_watch_named_only_invisible_characters_is_refused(self):
        for name in INVISIBLE:
            with self.subTest(name=name):
                (self.data / "watches.json").unlink(missing_ok=True)
                out = self.gate(self.T, watches=[{"name": name, "every": "5m"}, {"name": "ok", "every": "5m"}])
                self.assertEqual(out["watches_due"], ["ok"])
                self.assertEqual(len(out["errors"]), 1)
                self.assertIn("a watch needs a name", out["errors"][0]["error"])

    def test_a_watch_name_with_words_next_to_an_invisible_character_is_kept(self):
        for name in ("\ufeffT1", "a\ufeff"):
            with self.subTest(name=name):
                (self.data / "watches.json").unlink(missing_ok=True)
                self.assertEqual(self.gate(self.T, watches=[{"name": name, "every": "5m"}])["watches_due"], [name])


class GateWorkersTest(LoopCase):
    def test_a_done_worker_is_ended(self):
        a = self.start_worker()
        self.run_file(a["card"], "handback.json")
        out = self.gate(self.T)
        self.assertEqual([(e["card"], e["role"], e["did"]) for e in out["workers"]], [(a["card"], "implement", "ended")])
        self.assertEqual(resources.Registry(self.data).read(), [])
        self.assertEqual(self.store().get(a["card"])["sessions"]["implement"]["state"], "ended")
        self.assertIn(["stop", a["short_id"]], self.argvs())
        self.assertIn(f"worker {a['card']} implement: ended", self.gate_log())

    def test_a_question_becomes_the_cards_answer_ask(self):
        a = self.start_worker()
        self.run_file(a["card"], "question.json", {"text": "Which port should it use?"})
        out = self.gate(self.T)
        self.assertEqual(out["workers"][0]["did"], "asked")
        ask = self.store().get(a["card"])["ask"]
        self.assertEqual(ask["type"], "answer")
        self.assertIn("Which port should it use?", json.dumps(ask))

    def test_a_dead_worker_in_shadow_mode_keeps_its_record_and_is_not_resumed(self):
        a = self.start_worker()
        (self.fake_home / "agents.json").write_text("[]")
        out = self.gate(self.T)
        [entry] = out["workers"]
        self.assertEqual((entry["state"], entry["did"]), ("dead", "nothing"))
        self.assertIn("shadow mode", entry["note"])
        self.assertEqual(len(resources.Registry(self.data).read()), 1)
        self.assertEqual(len(self.bg_calls()), 1)  # only the start, no resume
        self.assertEqual(out["errors"], [])

    def test_an_idle_worker_is_stopped_after_idle_min(self):
        a = self.start_worker()
        self.set_agent(a["short_id"], state="done", status="idle")
        self.assertEqual(self.gate(self.T)["workers"][0]["did"], "nothing")
        self.assertNotIn(["stop", a["short_id"]], self.argvs())
        out = self.gate(self.T + timedelta(minutes=IDLE_MIN))
        self.assertEqual(out["workers"][0]["did"], "paused")
        self.assertIn(["stop", a["short_id"]], self.argvs())
        self.assertEqual(self.store().get(a["card"])["sessions"]["implement"]["state"], "paused")

    def test_with_no_worker_on_record_no_process_runs(self):
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("no process")), \
                mock.patch.object(subprocess, "Popen", side_effect=AssertionError("no process")):
            out = self.gate(self.T)
        self.assertEqual((out["workers"], out["errors"]), ([], []))

    def test_a_check_that_raises_is_one_error_and_the_run_goes_on(self):
        self.start_worker()
        with mock.patch.object(Workers, "check", side_effect=RuntimeError("boom")):
            out = self.gate(self.T)
        self.assertEqual(out["errors"], [{"workers": True, "error": "RuntimeError: boom"}])
        self.assertEqual(json.loads((self.data / "gate.json").read_text()), out)


class WatchListTest(LoopCase):
    def test_a_user_extends_file_is_a_watch_and_a_builtin_alone_is_not(self):
        self.assertEqual(gate.watch_list(self.data), ([], []))  # the built-in sources alone
        self.source("github-my-prs", "extends: github-my-prs\n")
        self.assertEqual(gate.watch_list(self.data),
                         ([{"name": "github-my-prs", "every": "5m", "via": "command"}], []))

    def test_each_pass_and_no_every_are_left_out_and_a_bad_every_is_one_error(self):
        self.command_source("each", every="each pass")
        self.command_source("never", every=None)
        self.command_source("bad", every="5 minutes")
        self.command_source("ok", every="1h")
        watches, errors = gate.watch_list(self.data)
        self.assertEqual(watches, [{"name": "ok", "every": "1h", "via": "command"}])
        self.assertEqual([e["watch"] for e in errors], ["bad"])
        self.assertIn("5 minutes", errors[0]["error"])

    def test_a_file_that_does_not_load_is_one_error_and_the_others_still_run(self):
        self.source("broken", "source: broken\nbrings: messages\n")
        self.command_source("ok", items=[item(1)])
        watches, errors = gate.watch_list(self.data)
        self.assertEqual([w["name"] for w in watches], ["ok"])
        self.assertEqual([e["source"] for e in errors], ["broken"])
        out = self.gate(self.T)
        self.assertEqual([e.get("source") for e in out["errors"]], ["broken"])
        self.assertEqual([r["watch"] for r in out["reads"]], ["ok"])


class CommandWatchTest(LoopCase):
    def test_a_due_command_watch_is_read_and_makes_its_cards(self):
        self.command_source("feed", items=[item(1), item(2)])
        out = self.gate(self.T)
        self.assertEqual(out["watches_due"], ["feed"])
        self.assertEqual(out["reads"], [{"watch": "feed", "via": "command", "made": 2, "updated": 0, "unchanged": 0,
                                         "dropped": 0}])
        self.assertEqual(sorted(c["key"] for c in self.store().list()), ["feed:i1", "feed:i2"])
        again = self.gate(self.T + timedelta(minutes=4))
        self.assertEqual((again["watches_due"], again["reads"]), ([], []))
        third = self.gate(self.T + timedelta(minutes=5))
        self.assertEqual(third["reads"][0]["unchanged"], 2)

    def test_a_failing_command_is_one_error_and_the_next_watch_is_still_read(self):
        self.command_source("afail", fail=True)
        self.command_source("bok", items=[item(1)])
        out = self.gate(self.T)
        self.assertEqual([e["watch"] for e in out["errors"]], ["afail"])
        self.assertIn("exited with 3", out["errors"][0]["error"])
        self.assertEqual([(r["watch"], r["made"]) for r in out["reads"]], [("bok", 1)])
        self.assertIn("watch afail: error:", self.gate_log())

    def test_the_look_itself_makes_no_card(self):
        self.command_source("quiet")
        out = self.gate(self.T)
        self.assertEqual(out["reads"][0]["made"], 0)
        self.assertEqual(self.store().list(), [])

    def test_once_the_read_budget_is_spent_a_due_watch_is_not_read_and_waits_for_its_next_time(self):
        self.command_source("a1", items=[item(1)])
        self.command_source("b2", items=[item(2)])
        marker = self.tmp / "b2-ran"
        (self.tmp / "b2.py").write_text(f"open({str(marker)!r}, 'w').close(); print('[]')\n")
        clock = iter([0.0, 0.0])  # the start, then before a1; before b2 the budget is spent
        with mock.patch("assistant.gate.monotonic", lambda: next(clock, gate.READ_BUDGET + 1.0)):
            out = self.gate(self.T)
        self.assertEqual(out["watches_due"], ["a1", "b2"])
        self.assertEqual([r["watch"] for r in out["reads"]], ["a1"])
        self.assertEqual(out["errors"], [{"watch": "b2", "error": "not read: this run's 120 seconds for command "
                                                                  "reads were spent; it is read at its next due "
                                                                  "time"}])
        self.assertFalse(marker.exists())
        self.assertIn("watch b2: error: not read", self.gate_log())
        self.assertIn("b2", json.loads((self.data / "watches.json").read_text()))  # its time stays written
        self.assertEqual(self.gate(self.T + timedelta(minutes=4))["watches_due"], [])

    def test_each_read_may_use_what_is_left_less_a_floor_for_each_read_still_to_go(self):
        for name in ("a1", "b2", "c3", "d4"):
            self.command_source(name)
        given = []
        real = engine.Engine.read

        def read(eng, name, dry_run=False, max_timeout=None):
            given.append(max_timeout)
            return real(eng, name, dry_run=dry_run, max_timeout=max_timeout)

        # the start, then before each read: a1 took 35, b2 took 50, c3 took 30 (115 of 120 used)
        clock = iter([0.0, 0.0, 35.0, 85.0, 115.0])
        with mock.patch("assistant.gate.monotonic", lambda: next(clock)), \
                mock.patch.object(engine.Engine, "read", read):
            self.gate(self.T)
        self.assertEqual(gate.READ_FLOOR, 10)
        self.assertEqual(given, [120 - 3 * 10, 85 - 2 * 10, 35 - 10, 5])

    def test_when_the_floors_are_more_than_what_is_left_each_read_gets_an_even_share(self):
        for name in ("a1", "b2", "c3"):
            self.command_source(name)
        given = []
        real = engine.Engine.read

        def read(eng, name, dry_run=False, max_timeout=None):
            given.append(max_timeout)
            return real(eng, name, dry_run=dry_run, max_timeout=max_timeout)

        clock = iter([0.0, 0.0, 0.0, 0.0])  # nothing used
        with mock.patch("assistant.gate.monotonic", lambda: next(clock)), \
                mock.patch.object(gate, "READ_BUDGET", 12), mock.patch.object(engine.Engine, "read", read):
            self.gate(self.T)
        self.assertEqual(given, [4.0, 6.0, 12.0])  # 12 - 20 is less than 12 / 3, then 12 - 10 is less than 12 / 2

    def test_a_slow_first_read_is_not_cut_by_its_place_in_the_order(self):
        self.command_source("a1")
        (self.tmp / "a1.py").write_text(f"import json, time; time.sleep(1.5); "
                                        f"print(json.dumps({json.dumps([item(1)])}))\n")
        for n, name in enumerate(("b2", "c3", "d4"), start=2):
            self.command_source(name, items=[item(n)])
        with mock.patch.object(gate, "READ_BUDGET", 4), mock.patch.object(gate, "READ_FLOOR", 0.5):
            out = self.gate(self.T)  # an even split would cut a1 at 1 second
        self.assertEqual(out["errors"], [])
        self.assertEqual([(r["watch"], r["made"]) for r in out["reads"]], [("a1", 1), ("b2", 1), ("c3", 1), ("d4", 1)])

    def test_a_read_cut_by_its_place_in_the_order_is_read_again_with_what_is_left(self):
        names = ("a1", "b2", "c3", "d4", "e5", "f6", "g7", "h8", "i9")
        self.command_source("a1")
        (self.tmp / "a1.py").write_text(f"import json, time; time.sleep(1); "
                                        f"print(json.dumps({json.dumps([item(1)])}))\n")
        for n, name in enumerate(names[1:], start=2):
            self.command_source(name, items=[item(n)])
        given = []
        real = engine.Engine.read

        def read(eng, name, dry_run=False, max_timeout=None):
            given.append((name, max_timeout))
            return real(eng, name, dry_run=dry_run, max_timeout=max_timeout)

        with mock.patch.object(gate, "READ_BUDGET", 4), mock.patch.object(gate, "READ_FLOOR", 0.5), \
                mock.patch.object(engine.Engine, "read", read):
            out = self.gate(self.T)  # a1's first cap is 4 / 9 seconds
        self.assertEqual(out["errors"], [])
        self.assertEqual(sorted(r["watch"] for r in out["reads"]), list(names))
        self.assertEqual([n for n, _ in given], list(names) + ["a1"])
        self.assertAlmostEqual(given[0][1], 4 / 9, places=2)
        self.assertGreater(given[-1][1], 2)  # all that the eight others left
        self.assertIn("a1:i1", [c["key"] for c in self.store().list()])

    def test_a_read_that_fails_for_its_own_reason_is_not_read_again(self):
        marker = self.tmp / "a1-runs"
        self.command_source("a1")
        (self.tmp / "a1.py").write_text(f"import sys; open({str(marker)!r}, 'a').write('ran\\n'); sys.exit(1)\n")
        self.command_source("b2", items=[item(2)])
        with mock.patch.object(gate, "READ_BUDGET", 4), mock.patch.object(gate, "READ_FLOOR", 0.5):
            out = self.gate(self.T)
        self.assertEqual([e["watch"] for e in out["errors"]], ["a1"])
        self.assertIn("exited with 1", out["errors"][0]["error"])
        self.assertEqual(marker.read_text(), "ran\n")
        self.assertEqual([r["watch"] for r in out["reads"]], ["b2"])

    def test_a_hanging_read_is_one_error_and_does_not_use_the_time_of_the_next_watch(self):
        self.command_source("a1")
        (self.tmp / "a1.py").write_text("import time; time.sleep(30)\n")
        self.command_source("b2", items=[item(2)])
        marker = self.tmp / "b2-ran"
        (self.tmp / "b2.py").write_text(f"import json; open({str(marker)!r}, 'w').close(); "
                                        f"print(json.dumps({json.dumps([item(2)])}))\n")
        with mock.patch.object(gate, "READ_BUDGET", 3):
            out = self.gate(self.T)
        self.assertEqual(out["watches_due"], ["a1", "b2"])
        self.assertEqual([e["watch"] for e in out["errors"]], ["a1"])  # what b2 left is less than its first try had
        self.assertRegex(out["errors"][0]["error"], r"took longer than [0-9.]+ seconds \(its read.timeout of 60 was")
        self.assertEqual([(r["watch"], r["made"]) for r in out["reads"]], [("b2", 1)])
        self.assertTrue(marker.exists())
        self.assertEqual([c["key"] for c in self.store().list()], ["b2:i2"])


    def reads_given(self):
        """Patch Engine.read to note each read's name; returns the list it fills."""
        given = []
        real = engine.Engine.read

        def read(eng, name, dry_run=False, max_timeout=None):
            given.append(name)
            return real(eng, name, dry_run=dry_run, max_timeout=max_timeout)

        patch = mock.patch.object(engine.Engine, "read", read)
        patch.start()
        self.addCleanup(patch.stop)
        return given

    def saved_watches(self) -> dict:
        return json.loads((self.data / "watches.json").read_text())

    def test_a_cut_read_whose_second_try_would_get_less_time_is_not_run_again(self):
        marker = self.tmp / "a1-runs"
        self.command_source("a1")
        (self.tmp / "a1.py").write_text(f"import time; open({str(marker)!r}, 'a').write('ran\\n'); time.sleep(30)\n")
        self.command_source("b2", items=[item(2)])
        with mock.patch.object(gate, "READ_BUDGET", 2), mock.patch.object(gate, "READ_FLOOR", 0.5):
            out = self.gate(self.T)  # a1's first cap is 1.5 seconds; b2 leaves less than that
        self.assertEqual([e["watch"] for e in out["errors"]], ["a1"])
        self.assertRegex(out["errors"][0]["error"], r"^a1: read.run took longer than 1.5 seconds \(its read.timeout "
                                                    r"of 60 was cut")
        self.assertEqual(marker.read_text(), "ran\n")
        self.assertEqual(len(list((self.data / "sources" / ".raw" / "a1").glob("*.json"))), 1)
        self.assertEqual([r["watch"] for r in out["reads"]], ["b2"])

    def test_a_command_that_exits_not_0_with_its_output_held_open_is_not_read_again(self):
        marker = self.tmp / "a1-runs"
        self.command_source("a1")
        (self.tmp / "a1.py").write_text(f"import subprocess, sys; open({str(marker)!r}, 'a').write('ran\\n'); "
                                        f"subprocess.Popen(['sleep', '30']); sys.exit(1)\n")
        for n, name in enumerate(("b2", "c3", "d4", "e5"), start=2):
            self.command_source(name, items=[item(n)])
        with mock.patch.object(gate, "READ_BUDGET", 4), mock.patch.object(gate, "READ_FLOOR", 1):
            out = self.gate(self.T)  # a1's first cap is 0.8 seconds; the others leave far more
        self.assertEqual([e["watch"] for e in out["errors"]], ["a1"])
        self.assertIn("exited with 1, but something it started still held its output open", out["errors"][0]["error"])
        self.assertEqual(marker.read_text(), "ran\n")
        self.assertEqual(sorted(r["watch"] for r in out["reads"]), ["b2", "c3", "d4", "e5"])

    def test_a_hanging_read_cut_while_it_held_more_than_its_share_is_read_last_next_time(self):
        self.command_source("a1")
        (self.tmp / "a1.py").write_text("import time; time.sleep(30)\n")
        self.command_source("b2")  # needs 1 second: more than its floor, far less than its own timeout of 60
        (self.tmp / "b2.py").write_text(f"import json, time; time.sleep(1); "
                                        f"print(json.dumps({json.dumps([item(2)])}))\n")
        given = self.reads_given()
        with mock.patch.object(gate, "READ_BUDGET", 3), mock.patch.object(gate, "READ_FLOOR", 0.5):
            first = self.gate(self.T)  # a1 gets 2.5 seconds, b2 the 0.5 left; neither has more for a second try
            self.assertEqual(given, ["a1", "b2"])
            self.assertEqual([e["watch"] for e in first["errors"]], ["a1", "b2"])
            self.assertEqual(self.saved_watches(), {"a1": {"last_check_at": "2026-11-10T14:00:00Z",
                                                           "cut_at": "2026-11-10T14:00:00Z"},
                                                    "b2": {"last_check_at": "2026-11-10T14:00:00Z"}})
            second = self.gate(self.T + timedelta(minutes=5))
        self.assertEqual(given[2:], ["b2", "a1"])
        self.assertEqual([(r["watch"], r["made"]) for r in second["reads"]], [("b2", 1)])
        self.assertEqual([e["watch"] for e in second["errors"]], ["a1"])
        self.assertEqual(self.saved_watches(), {"a1": {"last_check_at": "2026-11-10T14:05:00Z",  # cut at an even
                                                       "cut_at": "2026-11-10T14:00:00Z"},  # share: the old stamp
                                                "b2": {"last_check_at": "2026-11-10T14:05:00Z"}})

    def test_a_first_try_that_exits_not_0_at_a_cap_over_its_share_is_marked_but_not_read_again(self):
        marker = self.tmp / "a1-runs"
        self.command_source("a1")  # exits 1 at once, but the child it started holds its output past the cap
        (self.tmp / "a1.py").write_text(f"import subprocess, sys; open({str(marker)!r}, 'a').write('ran\\n'); "
                                        f"subprocess.Popen(['sleep', '30']); sys.exit(1)\n")
        self.command_source("b2")  # needs 1 second
        (self.tmp / "b2.py").write_text(f"import json, time; time.sleep(1); "
                                        f"print(json.dumps({json.dumps([item(2)])}))\n")
        given = self.reads_given()
        with mock.patch.object(gate, "READ_BUDGET", 3), mock.patch.object(gate, "READ_FLOOR", 0.5):
            first = self.gate(self.T)  # a1 holds 2.5 seconds, b2 gets the 0.5 left
            self.assertEqual(given, ["a1", "b2"])
            self.assertEqual([e["watch"] for e in first["errors"]], ["a1", "b2"])
            self.assertIn("exited with 1, but something it started still held its output open",
                          first["errors"][0]["error"])
            self.assertEqual(marker.read_text(), "ran\n")  # its own failure: not read again
            self.assertEqual(self.saved_watches(), {"a1": {"last_check_at": "2026-11-10T14:00:00Z",
                                                           "cut_at": "2026-11-10T14:00:00Z"},
                                                    "b2": {"last_check_at": "2026-11-10T14:00:00Z"}})
            second = self.gate(self.T + timedelta(minutes=5))
        self.assertEqual(given[2:], ["b2", "a1"])
        self.assertEqual([(r["watch"], r["made"]) for r in second["reads"]], [("b2", 1)])
        self.assertEqual([e["watch"] for e in second["errors"]], ["a1"])
        self.assertEqual(marker.read_text(), "ran\nran\n")

    def test_a_healthy_watch_that_stalled_once_behind_a_hanging_one_is_not_stuck_behind_it(self):
        self.command_source("a1")
        (self.tmp / "a1.py").write_text("import time; time.sleep(30)\n")
        stall = self.tmp / "b2-stalls"
        self.command_source("b2")  # needs 1 second, but 30 while `stall` is there
        (self.tmp / "b2.py").write_text(f"import json, os, time; time.sleep(30 if os.path.exists({str(stall)!r}) "
                                        f"else 1); print(json.dumps({json.dumps([item(2)])}))\n")
        given = self.reads_given()
        runs = []
        with mock.patch.object(gate, "READ_BUDGET", 3), mock.patch.object(gate, "READ_FLOOR", 0.5):
            for n in range(5):
                if n == 1:
                    stall.touch()
                elif stall.exists():
                    stall.unlink()
                at = len(given)
                out = self.gate(self.T + timedelta(minutes=5 * n))
                runs.append((given[at:], [r["watch"] for r in out["reads"]],
                             {k: v.get("cut_at") for k, v in self.saved_watches().items()}))
        self.assertEqual(runs, [
            (["a1", "b2"], [], {"a1": "2026-11-10T14:00:00Z", "b2": None}),
            (["b2", "a1"], [], {"a1": "2026-11-10T14:00:00Z", "b2": "2026-11-10T14:05:00Z"}),  # b2 stalled
            (["a1", "b2"], [], {"a1": "2026-11-10T14:10:00Z", "b2": "2026-11-10T14:05:00Z"}),  # a1's stamp is older
            (["b2", "a1"], ["b2"], {"a1": "2026-11-10T14:10:00Z", "b2": None}),  # b2's is older: it goes first
            (["b2", "a1"], ["b2"], {"a1": "2026-11-10T14:10:00Z", "b2": None}),
        ])

    def test_a_first_try_that_ends_inside_its_share_clears_cut_at_and_keeps_the_other_keys(self):
        self.command_source("a1", items=[item(1)])
        self.command_source("b2", fail=True)  # failed by itself, inside its share
        write_json(self.data / "watches.json", {
            "a1": {"last_check_at": "2026-11-10T13:00:00Z", "cut_at": "2026-11-10T13:00:00Z", "note": 1},
            "b2": {"last_check_at": "2026-11-10T13:00:00Z", "cut_at": "2026-11-10T13:00:00Z"},
            "z9": {"last_check_at": "2026-11-10T13:00:00Z", "cut_at": "2026-11-10T13:00:00Z"}})  # not a watch now
        given = self.reads_given()
        out = self.gate(self.T)
        self.assertEqual(given, ["a1", "b2"])  # both were cut last time: by name
        self.assertEqual([e["watch"] for e in out["errors"]], ["b2"])
        self.assertEqual(self.saved_watches(), {
            "a1": {"last_check_at": "2026-11-10T14:00:00Z", "note": 1},
            "b2": {"last_check_at": "2026-11-10T14:00:00Z"},
            "z9": {"last_check_at": "2026-11-10T13:00:00Z", "cut_at": "2026-11-10T13:00:00Z"}})

    def test_a_damaged_cut_at_means_not_cut_and_the_run_goes_on(self):
        for n, name in enumerate(("a0", "b1", "c2", "d3")):
            self.command_source(name, items=[item(n)])
        write_json(self.data / "watches.json", {
            "a0": {"last_check_at": "2026-11-10T13:00:00Z", "cut_at": "2026-11-10T13:00:00Z"},
            "b1": {"last_check_at": "2026-11-10T13:00:00Z", "cut_at": 5},
            "c2": {"last_check_at": "2026-11-10T13:00:00Z", "cut_at": "x"},
            "d3": {"last_check_at": "2026-11-10T13:00:00Z", "cut_at": [1]}})
        given = self.reads_given()
        out = self.gate(self.T)
        self.assertEqual(given, ["b1", "c2", "d3", "a0"])  # only a0's cut_at is a stamp
        self.assertEqual(out["errors"], [])
        self.assertEqual(sorted(r["watch"] for r in out["reads"]), ["a0", "b1", "c2", "d3"])
        self.assertEqual(self.saved_watches(), {n: {"last_check_at": "2026-11-10T14:00:00Z"}
                                                for n in ("a0", "b1", "c2", "d3")})

    def test_a_failed_write_of_cut_at_is_one_error_and_the_run_goes_on(self):
        self.command_source("a1", items=[item(1)])
        write_json(self.data / "watches.json",
                   {"a1": {"last_check_at": "2026-11-10T13:00:00Z", "cut_at": "2026-11-10T13:00:00Z"}})
        real = gate.write_json
        calls = []

        def write(path, obj):
            calls.append(Path(path).name)
            if Path(path).name == "watches.json" and calls.count("watches.json") == 2:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(path, obj)

        with mock.patch.object(gate, "write_json", write):
            out = self.gate(self.T)
        self.assertEqual([(r["watch"], r["made"]) for r in out["reads"]], [("a1", 1)])
        self.assertEqual(len(out["errors"]), 1)
        self.assertIn("cannot save which command reads were cut", out["errors"][0]["error"])
        self.assertIn("watches: error:", self.gate_log())
        self.assertEqual(self.saved_watches(), {"a1": {"last_check_at": "2026-11-10T14:00:00Z",
                                                       "cut_at": "2026-11-10T13:00:00Z"}})


class ScanWatchTest(LoopCase):
    def setUp(self):
        super().setUp()
        self.mcp_source("vendor-a", "list_incidents")
        self.mcp_source("vendor-b", "list_changes")

    def test_two_due_scan_watches_share_one_session(self):
        out = self.gate(self.T)
        self.assertEqual(out["watches_due"], ["vendor-a", "vendor-b"])
        [bg] = self.bg_calls()
        argv = bg["argv"]
        rec = self.scan_record()
        folder = Path(rec["plan"]).parent
        self.assertEqual(sorted(rec), ["plan", "profile", "session_id", "short_id", "started_at"])
        self.assertEqual((rec["short_id"], rec["profile"], rec["started_at"]), ("s0001", "subscription",
                                                                               "2026-11-10T14:00:00Z"))
        self.assertTrue(rec["session_id"])
        self.assertEqual(argv[argv.index("-n") + 1], f"ap2 scan · {folder.name}")
        self.assertEqual(argv[argv.index("--model") + 1], "haiku")
        self.assertEqual(bg["cwd"], str(folder))
        self.assertEqual([c["source"] for c in scan.load_plan(rec["plan"])[0]["calls"]], ["vendor-a", "vendor-b"])
        self.assertEqual(argv[-1], scan.load_plan(rec["plan"])[0]["brief"])
        settings = Path(argv[argv.index("--settings") + 1])
        self.assertEqual(settings, folder / "settings.json")
        s = json.loads(settings.read_text())
        [pre], [post] = s["hooks"]["PreToolUse"], s["hooks"]["PostToolUse"]
        self.assertEqual(pre["matcher"], "*")
        self.assertIn(f"scan hook-allow --plan {rec['plan']}", pre["hooks"][0]["command"])
        self.assertIn(f"scan save-raw --plan {rec['plan']}", post["hooks"][0]["command"])
        self.assertEqual(s["env"]["AP2_PROFILE"], "subscription")  # the profile's overlay
        launches = [json.loads(line) for line in (self.data / "spend" / "launches.jsonl").read_text().splitlines()]
        self.assertEqual([(r["short"], r["kind"]) for r in launches], [("s0001", "scan")])
        self.assertIn("scan: started s0001 for vendor-a, vendor-b", self.gate_log())

    def test_a_spend_launch_row_that_was_not_written_is_one_scan_error(self):
        with mock.patch("assistant.spend.record_launch", side_effect=OSError("disk full")):
            out = self.gate(self.T)
        self.assertEqual(out["errors"], [{"scan": True, "error": "the spend launch row was not written: disk full"}])
        self.assertEqual(self.scan_record()["short_id"], "s0001")  # the session still runs, on record
        self.assertIn("scan: the spend launch row was not written: disk full", self.gate_log())

    def test_a_second_run_while_it_is_open_starts_none_and_writes_no_watch_time(self):
        self.gate(self.T)
        times = (self.data / "watches.json").read_text()
        out = self.gate(self.T + timedelta(minutes=10))
        self.assertEqual(len(self.bg_calls()), 1)
        self.assertEqual(out["watches_due"], [])
        self.assertEqual((self.data / "watches.json").read_text(), times)

    def test_the_record_is_on_file_before_the_listing_is_asked_for_the_session_id(self):
        from autopilot import sessions
        real, seen = sessions.list_all, []

        def listing(claude):
            if self.bg_calls():  # the listing start asks after the launch
                seen.append(self.scan_record())
                return None  # the listing fails: the record must still be there
            return real(claude)

        with mock.patch("autopilot.sessions.list_all", listing):
            out = self.gate(self.T)
        self.assertEqual([r["session_id"] for r in seen], [""])
        self.assertEqual((self.scan_record()["short_id"], self.scan_record()["session_id"]), ("s0001", ""))
        self.assertEqual([e for e in out["errors"] if e.get("scan")], [])
        self.assertNotIn(["stop", "s0001"], self.argvs())

    def test_a_record_that_cannot_be_written_stops_the_session(self):
        real = scan.write_json

        def write(path, obj):
            if Path(path).name == scan.SESSION:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(path, obj)

        with mock.patch("assistant.scan.write_json", write):
            out = self.gate(self.T)
        self.assertEqual(len(self.bg_calls()), 1)
        self.assertIn(["stop", "s0001"], self.argvs())
        self.assertFalse((self.data / "scan-session.json").exists())
        [err] = [e for e in out["errors"] if e.get("scan")]
        self.assertIn("No space left on device", err["error"])
        self.assertTrue(err["error"].endswith("the session s0001 was stopped"), err["error"])

    def test_a_second_record_write_that_fails_keeps_the_first(self):
        real, writes = scan.write_json, []

        def write(path, obj):
            if Path(path).name == scan.SESSION:
                writes.append(dict(obj))
                if len(writes) > 1:
                    raise OSError(errno.ENOSPC, "No space left on device")
            return real(path, obj)

        with mock.patch("assistant.scan.write_json", write):
            out = self.gate(self.T)
        self.assertEqual([bool(w["session_id"]) for w in writes], [False, True])
        self.assertEqual((self.scan_record()["short_id"], self.scan_record()["session_id"]), ("s0001", ""))
        self.assertNotIn(["stop", "s0001"], self.argvs())
        self.assertEqual([e for e in out["errors"] if e.get("scan")], [])

    def test_hook_allow_allows_only_the_planned_tools(self):
        self.gate(self.T)
        plan = self.scan_record()["plan"]

        def decide(tool, tool_input=None):
            out = scan.hook_allow(plan, json.dumps({"tool_name": tool, "tool_input": tool_input or {}}).encode())
            return out["hookSpecificOutput"]["permissionDecision"]

        self.assertEqual(decide("mcp__vendor__list_incidents"), "allow")
        self.assertEqual(decide("mcp__vendor__list_changes"), "allow")
        for tool, tool_input in (("mcp__slack__slack_send_message", {"text": "hi"}), ("Bash", {"command": "ls"}),
                                 ("Write", {"file_path": str(self.tmp / "x.json"), "content": "[]"}),
                                 ("mcp__other__list_incidents", {}), ("Read", {"file_path": "/etc/hosts"})):
            with self.subTest(tool=tool):
                self.assertEqual(decide(tool, tool_input), "deny")

    def test_a_paused_cap_starts_nothing_and_the_watch_times_stay(self):
        cap.pause(self.data, self.T)
        out = self.gate(self.T)
        self.assertEqual(self.bg_calls(), [])
        self.assertEqual(len([e for e in out["errors"] if e.get("scan")]), 1)
        self.assertFalse((self.data / "scan-session.json").exists())
        self.assertEqual(sorted(json.loads((self.data / "watches.json").read_text())), ["vendor-a", "vendor-b"])

    def test_an_active_profile_that_does_not_load_starts_nothing(self):
        (self.data / "active-profile.json").write_text('{"name": "nosuch"}')
        out = self.gate(self.T)
        self.assertEqual(self.bg_calls(), [])
        [err] = [e for e in out["errors"] if e.get("scan")]
        self.assertIn("nosuch", err["error"])

    def test_plan_errors_go_in_errors(self):
        self.source("vendor-b", """\
            source: vendor-b
            brings: messages
            read: { via: mcp, server: vendor, every: 5m }
            fill: paths
            form: { id: .id, at: .at, text: .text }
            link: none
            sort: { all: alert }
            """)
        out = self.gate(self.T)
        self.assertEqual([e["source"] for e in out["errors"]], ["vendor-b"])
        self.assertEqual(len(self.bg_calls()), 1)  # vendor-a still runs


class ScanFinishTest(LoopCase):
    def setUp(self):
        super().setUp()
        self.mcp_source("vendor-a", "list_incidents", every="1h")
        self.gate(self.T)
        self.rec = self.scan_record()

    def test_a_finished_scan_is_taken_into_cards(self):
        hook = {"tool_name": "mcp__vendor__list_incidents", "tool_input": {},
                "tool_response": json.dumps([item(1)])}
        scan.save_raw(self.rec["plan"], json.dumps(hook).encode())
        self.set_agent("s0001", state="done", status="idle")
        out = self.gate(self.T + timedelta(minutes=2))
        self.assertIn(["stop", "s0001"], self.argvs())
        self.assertFalse((self.data / "scan-session.json").exists())
        self.assertEqual(out["reads"], [{"watch": "vendor-a", "via": "mcp", "made": 1, "updated": 0, "unchanged": 0,
                                         "dropped": 0}])
        self.assertEqual([c["key"] for c in self.store().list()], ["vendor-a:i1"])
        self.assertEqual(out["errors"], [])
        self.assertIn("scan: s0001 ended", self.gate_log())

    def test_a_session_still_working_past_15_minutes_is_stopped_with_one_error(self):
        self.assertIsNone(self.gate(self.T + timedelta(minutes=14))["errors"] or None)
        self.assertNotIn(["stop", "s0001"], self.argvs())
        out = self.gate(self.T + timedelta(minutes=16))
        self.assertIn(["stop", "s0001"], self.argvs())
        self.assertFalse((self.data / "scan-session.json").exists())
        late = [e for e in out["errors"] if e.get("scan")]
        self.assertEqual(len(late), 1)
        self.assertIn(f"{gate.scan.SCAN_MAX_MIN} minutes", late[0]["error"])

    def test_a_failed_listing_changes_nothing(self):
        with mock.patch("autopilot.sessions.list_all", return_value=None):
            soon = self.gate(self.T + timedelta(minutes=2))
            out = self.gate(self.T + timedelta(minutes=30))
        self.assertNotIn(["stop", "s0001"], self.argvs())
        self.assertEqual(self.scan_record(), self.rec)
        self.assertEqual((soon["reads"], soon["errors"]), ([], []))
        self.assertEqual(out["reads"], [])
        # Past SCAN_MAX_MIN the wait is reported, once per run; the record and the scan watches still wait.
        self.assertEqual(out["errors"], [{"scan": True, "error": "cannot list the sessions; the scan session s0001 "
                                                                  "has been open since 2026-11-10T14:00:00Z, and the "
                                                                  "scan watches wait"}])
        self.assertEqual(out["watches_due"], [])

    def test_a_session_that_started_later_than_now_is_stopped_with_one_error(self):
        self.assertEqual(self.gate(self.T - timedelta(minutes=2))["errors"], [])  # inside routines.GRACE
        self.assertNotIn(["stop", "s0001"], self.argvs())
        out = self.gate(self.T - timedelta(hours=1))
        self.assertIn(["stop", "s0001"], self.argvs())
        self.assertNotEqual(self.scan_record()["short_id"], "s0001")  # taken; the due watch started a new scan
        [err] = [e for e in out["errors"] if e.get("scan")]
        self.assertIn("started at 2026-11-10T14:00:00Z, later than now (2026-11-10T13:00:00Z)", err["error"])
        self.assertTrue(err["error"].endswith(gate.WENT_BACK), err["error"])

    def test_a_failed_listing_for_a_session_that_started_later_than_now_says_the_clock(self):
        with mock.patch("autopilot.sessions.list_all", return_value=None):
            out = self.gate(self.T - timedelta(hours=1))
        self.assertEqual(self.scan_record(), self.rec)
        [err] = [e for e in out["errors"] if e.get("scan")]
        self.assertTrue(err["error"].startswith("cannot list the sessions"), err["error"])
        self.assertTrue(err["error"].endswith(gate.WENT_BACK), err["error"])

    def test_a_take_that_fails_on_the_store_keeps_the_record_and_the_next_run_takes_again(self):
        hook = {"tool_name": "mcp__vendor__list_incidents", "tool_input": {},
                "tool_response": json.dumps([item(1)])}
        scan.save_raw(self.rec["plan"], json.dumps(hook).encode())
        self.set_agent("s0001", state="done", status="idle")
        times = (self.data / "watches.json").read_text()
        with mock.patch("assistant.scan.take", side_effect=cards.CardError("the store is broken")) as take:
            out = self.gate(self.T + timedelta(hours=2))  # vendor-a would be due again by its time
        take.assert_called_once()
        self.assertEqual(self.scan_record(), dict(self.rec, stopped_at="2026-11-10T16:00:00Z"))  # stopped, settled
        [err] = out["errors"]
        self.assertTrue(err["scan"])
        self.assertIn("the store is broken", err["error"])
        self.assertEqual((out["reads"], out["watches_due"]), ([], []))  # the scan watches are not timed meanwhile
        self.assertEqual((self.data / "watches.json").read_text(), times)
        again = self.gate(self.T + timedelta(hours=2, minutes=1))
        self.assertNotEqual(self.scan_record()["short_id"], "s0001")  # taken; the due watch started a new scan
        self.assertEqual([r["made"] for r in again["reads"] if r["watch"] == "vendor-a"], [1])
        self.assertEqual([c["key"] for c in self.store().list()], ["vendor-a:i1"])

    def test_while_the_card_store_cannot_be_read_a_retry_takes_nothing_and_stops_and_settles_once(self):
        hook = {"tool_name": "mcp__vendor__list_incidents", "tool_input": {},
                "tool_response": json.dumps([item(1)])}
        scan.save_raw(self.rec["plan"], json.dumps(hook).encode())
        self.set_agent("s0001", state="done", status="idle")
        bad = self.data / "items" / "2026-11" / "c-20261110-130000-abcdef.json"
        bad.parent.mkdir(parents=True)
        bad.write_text('{"id": "c-2026', encoding="utf-8")
        raw = self.data / "sources" / ".raw" / "vendor-a"
        settled = self.data / "spend" / "settled.jsonl"

        def rows() -> int:
            return len(settled.read_text().splitlines()) if settled.exists() else 0
        for minutes in (2, 3, 4):
            out = self.gate(self.T + timedelta(minutes=minutes))
            [err] = [e for e in out["errors"] if e.get("scan")]
            self.assertTrue(err["error"].startswith("the card store cannot be read ("), err["error"])
            self.assertIn(str(bad), err["error"])
            self.assertTrue(err["error"].endswith("; the scan session s0001 is taken once it can"), err["error"])
            self.assertEqual(self.scan_record(), dict(self.rec, stopped_at="2026-11-10T14:02:00Z"))
            self.assertEqual(sorted(raw.iterdir()) if raw.exists() else [], [])  # no raw copy is saved
            self.assertEqual(out["watches_due"], [])
        self.assertEqual(self.argvs().count(["stop", "s0001"]), 1)
        self.assertEqual(rows(), 1)
        bad.unlink()  # the store is repaired
        out = self.gate(self.T + timedelta(minutes=5))
        self.assertFalse((self.data / "scan-session.json").exists())
        self.assertEqual([(r["watch"], r["made"]) for r in out["reads"]], [("vendor-a", 1)])
        self.assertEqual([c["key"] for c in self.store().list()], ["vendor-a:i1"])
        self.assertEqual([e for e in out["errors"] if e.get("scan")], [])
        self.assertEqual((self.argvs().count(["stop", "s0001"]), rows()), (1, 1))

    def list_as_stopped(self, short):
        """The CLI keeps a stopped session on its listing with no pid and no status."""
        f = self.fake_home / "agents.json"
        rows = [a for a in json.loads(f.read_text()) if a["id"] != short]
        f.write_text(json.dumps(rows + [{"id": short, "state": "stopped"}]))

    def test_a_session_the_listing_shows_stopped_is_taken_with_no_late_error(self):
        hook = {"tool_name": "mcp__vendor__list_incidents", "tool_input": {},
                "tool_response": json.dumps([item(1)])}
        scan.save_raw(self.rec["plan"], json.dumps(hook).encode())
        self.list_as_stopped("s0001")
        out = self.gate(self.T + timedelta(minutes=3))
        self.assertFalse((self.data / "scan-session.json").exists())
        self.assertEqual([(r["watch"], r["made"]) for r in out["reads"]], [("vendor-a", 1)])
        self.assertEqual([c["key"] for c in self.store().list()], ["vendor-a:i1"])
        self.assertEqual(out["errors"], [])

    def test_after_a_failed_take_a_session_listed_stopped_is_taken_on_the_next_run(self):
        hook = {"tool_name": "mcp__vendor__list_incidents", "tool_input": {},
                "tool_response": json.dumps([item(1)])}
        scan.save_raw(self.rec["plan"], json.dumps(hook).encode())
        self.set_agent("s0001", state="done", status="idle")
        with mock.patch("assistant.scan.take", side_effect=cards.CardError("the store is broken")):
            first = self.gate(self.T + timedelta(minutes=2))
        self.assertIn("the store is broken", first["errors"][0]["error"])
        self.assertEqual(self.scan_record(), dict(self.rec, stopped_at="2026-11-10T14:02:00Z"))
        self.list_as_stopped("s0001")  # the first run stopped it; the CLI still lists it
        out = self.gate(self.T + timedelta(minutes=3))  # well before SCAN_MAX_MIN
        self.assertFalse((self.data / "scan-session.json").exists())
        self.assertEqual([(r["watch"], r["made"]) for r in out["reads"]], [("vendor-a", 1)])
        self.assertEqual(out["errors"], [])

    def test_a_session_that_is_gone_is_taken(self):
        (self.fake_home / "agents.json").write_text("[]")
        out = self.gate(self.T + timedelta(minutes=2))
        self.assertFalse((self.data / "scan-session.json").exists())
        self.assertEqual([e["source"] for e in out["errors"]], ["vendor-a"])  # nothing was logged
        self.assertIn("no logged call matches", out["errors"][0]["error"])


class PassDueTest(LoopCase):
    def pass_file(self) -> dict:
        return json.loads((self.data / "pass.json").read_text())

    def test_an_empty_folder_is_not_due(self):
        out = self.gate(self.T)
        self.assertIs(out["pass_due"], False)
        p = self.pass_file()
        self.assertEqual(sorted(p), ["checked_at", "due", "seen", "since", "why"])
        self.assertEqual((p["due"], p["since"], p["why"], p["checked_at"]), (False, None, [], "2026-11-10T14:00:00Z"))

    def test_a_planned_card_is_due_until_a_pass_starts(self):
        cid = self.add_card()
        out = self.gate(self.T)
        self.assertIs(out["pass_due"], True)
        self.assertEqual(self.pass_file()["why"], [{"why": "ready", "card": cid}])
        self.assertEqual(self.pass_file()["since"], "2026-11-10T14:00:00Z")
        self.assertIn("pass due: 1 reason(s)", self.gate_log())
        self.gate(self.T + timedelta(minutes=1))
        self.assertEqual(self.pass_file()["since"], "2026-11-10T14:00:00Z")  # kept from the first run that found it
        gate.pass_started(self.data, self.T + timedelta(minutes=2))
        self.assertIs(self.pass_file()["due"], False)
        out = self.gate(self.T + timedelta(minutes=3))
        self.assertIs(out["pass_due"], False)
        self.assertIsNone(self.pass_file()["since"])
        self.assertEqual(self.bg_calls(), [])  # nothing is started

    def test_a_new_result_file_is_due(self):
        cid = self.add_card(lane="running")
        self.gate(self.T)
        gate.pass_started(self.data, self.T)
        self.run_file(cid, "handback.json")
        out = self.gate(self.T + timedelta(minutes=1))
        self.assertIs(out["pass_due"], True)
        self.assertEqual(self.pass_file()["why"], [{"why": "result", "card": cid, "file": "handback.json"}])

    def test_an_ask_that_closed_is_due(self):
        cid = self.add_card(lane="needs", ask={"type": "confirm", "text": "Ship it?"})
        self.assertIs(self.gate(self.T)["pass_due"], False)  # an open ask is not ready
        gate.pass_started(self.data, self.T)
        self.store().update(cid, {"ask": None}, by="test")
        self.assertIs(self.gate(self.T + timedelta(minutes=1))["pass_due"], True)
        self.assertEqual(self.pass_file()["why"], [{"why": "answered", "card": cid}])

    def test_an_ask_closed_by_worker_answer_is_due(self):
        a = self.start_worker()
        self.run_file(a["card"], "question.json", {"text": "Which port?"})
        self.gate(self.T)
        gate.pass_started(self.data, self.T)
        self.workers(self.T + timedelta(minutes=1)).answer(a["card"], "Use 7130.")
        out = self.gate(self.T + timedelta(minutes=2))
        self.assertIs(out["pass_due"], True)
        self.assertIn({"why": "answered", "card": a["card"]}, self.pass_file()["why"])

    def test_an_open_ask_a_worker_on_record_and_an_imported_card_are_not_ready(self):
        self.add_card(ask={"type": "confirm", "text": "Ship it?"})
        self.add_card(feed="v1-import")
        self.start_worker()
        out = self.gate(self.T)
        self.assertIs(out["pass_due"], False)
        self.assertEqual(self.pass_file()["seen"]["ready"], [])

    def test_a_routine_run_card_with_no_worker_is_ready(self):
        self.put(last_slot="2026-11-09T09:00")
        out = self.gate(utc(2026, 11, 10, 9, 1))
        self.assertEqual(self.pass_file()["why"], [{"why": "ready", "card": out["routines"][0]["card"]}])

    def test_a_workers_file_nested_too_deep_is_one_pass_error_and_the_run_completes(self):
        (self.data / "workers.json").write_text('{"workers": ' + "[" * 100_000 + "]" * 100_000 + "}")
        with too_deep(self.data / "workers.json"):
            out = self.gate(self.T)
        self.assertEqual([e for e in out["errors"] if "pass" in e],
                         [{"pass": True, "error": out["errors"][-1]["error"]}])
        self.assertIn("recursion", out["errors"][-1]["error"])
        self.assertIn(f"{self.data / 'workers.json'}: cannot read the worker list", out["errors"][-1]["error"])
        self.assertIs(out["pass_due"], False)
        self.assertEqual(json.loads((self.data / "gate.json").read_text(encoding="utf-8")), out)
        self.assertIn("pass: error:", self.gate_log())

    def test_a_damaged_pass_file_is_one_error(self):
        cid = self.add_card()
        for text in ("{", "[]", '{"seen": {"ready": [[1]], "results": {}, "asks": {}}}', '{"seen": {"ready": []}}'):
            with self.subTest(text=text):
                (self.data / "pass.json").write_text(text)
                out = self.gate(self.T)
                self.assertEqual([e.get("pass") for e in out["errors"]], [str(self.data / "pass.json")])
                self.assertIs(out["pass_due"], True)  # from an empty seen
                self.assertEqual(self.pass_file()["why"], [{"why": "ready", "card": cid}])


if __name__ == "__main__":
    unittest.main()
