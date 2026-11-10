"""Slice 5: routine files, due slots, missed runs, the run card before last_slot, watches, heartbeat, prune, lock.

Every test injects the time; none sleeps or reads the machine's time zone (each routine names its own `tz`).
"""
from __future__ import annotations

import builtins
import errno
import json
import os
import shutil
import socket
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT  # noqa: F401  (puts lib/ on sys.path)
from assistant import cards, gate, restore, routines
from assistant.config import Config, ConfigError
from assistant.store import Store, write_json

UTC = timezone.utc
LA = "America/Los_Angeles"


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


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
        self.assertEqual(sorted(heartbeat), ["errors", "last_run_at", "pruned", "routines", "watches_due"])
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

class NoOutsideEffectsTest(GateCase):
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


if __name__ == "__main__":
    unittest.main()
