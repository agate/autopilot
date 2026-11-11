"""The gate prunes old raw source files: `<data>/sources/.raw/<source>/<UTC stamp>-<6 hex>.json` and its report.

Every test injects the time and uses temp folders only; none reads the machine's time (the one CLI test passes a
`--now` in the past) and none touches the real home.
"""
from __future__ import annotations

import builtins
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT
from assistant import cards, engine, gate
from assistant.cards import CardError
from assistant.store import Store
from tests.test_ap2_cli import temp_inside_the_accounts_claude

UTC = timezone.utc
NOW = datetime(2026, 11, 10, 14, 30, 5, tzinfo=UTC)
RAW = "sources/.raw"


def name(at: datetime, hexid: str = "a1b2c3", report: bool = False) -> str:
    return f"{at.astimezone(UTC).strftime(engine.STAMP)}-{hexid}{'.report' if report else ''}.json"


def no_cards():
    return []


class RawCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-raw-prune-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "data"
        self.data.mkdir()
        (self.data / "config.yaml").write_text("", encoding="utf-8")

    def put(self, source: str, at: datetime, hexid: str = "a1b2c3", report: bool = True) -> Path:
        """One read's raw file (and its report); return the raw file's path."""
        folder = self.data / RAW / source
        folder.mkdir(parents=True, exist_ok=True)
        raw = folder / name(at, hexid)
        raw.write_text("{}", encoding="utf-8")
        if report:
            (folder / name(at, hexid, report=True)).write_text("{}", encoding="utf-8")
        return raw

    @staticmethod
    def report(raw: Path) -> Path:
        return raw.with_name(raw.name[:-len(".json")] + ".report.json")

    def rel(self, p: Path) -> str:
        return p.relative_to(self.data).as_posix()

    def prune(self, now=NOW, linked=no_cards) -> dict:
        return engine.prune_raw(self.data, now, linked)

    def gate(self, now=NOW, **kw) -> dict:
        return gate.run(self.data, now, **kw)

    def log(self) -> list:
        p = self.data / "gate.log"
        return p.read_text(encoding="utf-8").splitlines() if p.exists() else []

    def source_card(self, raw_rel: str) -> dict:
        s = Store(self.data, clock=lambda: NOW)
        return s.add({"kind": "message", "lane": "needs", "feed": "chat", "links": [], "pri": "P2",
                      "origin": "source", "plain": {k: {"en": "words"} for k in cards.PLAIN_LINES},
                      "key": "chat#1", "source": {"name": "chat", "record": {"id": "1"}, "link": None,
                                                  "raw": raw_rel}}, by="test")


# -- criterion 1: what is old ------------------------------------------------------------------------------------

class WhatIsOldTest(RawCase):
    def test_age_comes_from_the_name_and_30_days_is_kept(self):
        d29 = self.put("chat", NOW - timedelta(days=29), "000029")
        d30 = self.put("chat", NOW - timedelta(days=30), "000030")
        d31 = self.put("chat", NOW - timedelta(days=31), "000031")
        just = self.put("chat", NOW - timedelta(days=30, seconds=1), "3d0d01")
        # The mtime does not count: an old copy of a young file stays, a fresh copy of an old file goes.
        long_ago = (NOW - timedelta(days=400)).timestamp()
        os.utime(d29, (long_ago, long_ago))
        os.utime(d31, (NOW.timestamp(), NOW.timestamp()))
        out = self.prune()
        self.assertTrue(d29.exists() and d30.exists())
        self.assertFalse(d31.exists())
        self.assertFalse(just.exists())
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["link"], None)
        self.assertEqual(sorted(out["removed"]), sorted(self.rel(p) for p in (
            d31, self.report(d31), just, self.report(just))))

    def test_a_name_that_does_not_match_and_a_future_stamp_are_kept(self):
        folder = self.data / RAW / "chat"
        folder.mkdir(parents=True)
        old = (NOW - timedelta(days=90)).strftime(engine.STAMP)
        kept = [
            "notes.txt",
            "scan-session-output.json",                # a scan session's own file (read_saved keeps its name)
            f".{old}-a1b2c3.json",                     # hidden
            f"{old}-A1B2C3.json",                      # uppercase hex
            f"{old}-a1b2c.json",                       # 5 hex
            f"{old}-a1b2c3d.json",                     # 7 hex
            f"{old}-a1b2c3.json.bak",
            f"{old}-a1b2c3.txt",
            f"{old}-a1b2c3.report.json.tmp",
            "20261340T000000Z-a1b2c3.json",            # month 13: its age cannot be read
            "20260230T000000Z-a1b2c3.json",            # 30 February
            f"{old[:-1]}-a1b2c3.json",                 # no Z
            "２０２５0101T000000Z-a1b2c3.json",          # digits that are not ASCII
            name(NOW + timedelta(days=3)),             # a stamp later than now
        ]
        for n in kept:
            (folder / n).write_text("{}", encoding="utf-8")
        future_report = folder / name(NOW + timedelta(days=3), report=True)
        future_report.write_text("{}", encoding="utf-8")
        out = self.prune()
        self.assertEqual(out["removed"], [])
        self.assertEqual(sorted(p.name for p in folder.iterdir()), sorted(kept + [future_report.name]))

    def test_no_raw_folder_is_nothing_to_do(self):
        self.assertEqual(self.prune(), {"removed": [], "errors": [], "link": None})
        (self.data / "sources").mkdir()
        self.assertEqual(self.prune(), {"removed": [], "errors": [], "link": None})
        (self.data / RAW).write_text("not a folder", encoding="utf-8")
        self.assertEqual(self.prune(), {"removed": [], "errors": [], "link": None})

    def test_only_regular_files_directly_in_a_source_folder_go(self):
        old = NOW - timedelta(days=60)
        deep = self.data / RAW / "chat" / "nested"
        deep.mkdir(parents=True)
        (deep / name(old)).write_text("{}", encoding="utf-8")
        (self.data / RAW / name(old)).write_text("{}", encoding="utf-8")  # directly in .raw, not in a source
        as_dir = self.data / RAW / "chat" / name(old, "dddddd")
        as_dir.mkdir()                                                       # a folder with a raw file's name
        self.prune()
        self.assertTrue((deep / name(old)).exists())
        self.assertTrue((self.data / RAW / name(old)).exists())
        self.assertTrue(as_dir.is_dir())

    def test_a_hidden_source_folder_is_skipped(self):
        hidden = self.put(".cache", NOW - timedelta(days=60))
        self.prune()
        self.assertTrue(hidden.exists() and self.report(hidden).exists())

    def test_the_constant_is_30_days(self):
        self.assertEqual(engine.RAW_KEEP, timedelta(days=30))


# -- criterion 2: reports go with their raw file -----------------------------------------------------------------

class ReportsTest(RawCase):
    def test_reports_follow_their_raw_file(self):
        old = self.put("chat", NOW - timedelta(days=40), "aaaaaa")
        young = self.put("chat", NOW - timedelta(days=10), "bbbbbb")
        orphan = self.put("chat", NOW - timedelta(days=50), "cccccc")
        orphan.unlink()  # its raw file is already gone
        young_orphan = self.put("chat", NOW - timedelta(days=5), "eeeeee")
        young_orphan.unlink()
        out = self.prune()
        self.assertFalse(old.exists() or self.report(old).exists())
        self.assertTrue(young.exists() and self.report(young).exists())
        self.assertFalse(self.report(orphan).exists())
        self.assertTrue(self.report(young_orphan).exists())
        self.assertEqual(sorted(out["removed"]), sorted(
            [self.rel(old), self.rel(self.report(old)), self.rel(self.report(orphan))]))

    def test_a_report_whose_raw_file_could_not_be_removed_is_kept(self):
        old = self.put("chat", NOW - timedelta(days=40))
        real = os.unlink

        def fail_raw(path, *a, **k):
            if os.fspath(path) == old.name:
                raise PermissionError(13, "Permission denied", old.name)
            return real(path, *a, **k)

        with mock.patch.object(os, "unlink", fail_raw):
            out = self.prune()
        self.assertTrue(old.exists() and self.report(old).exists())
        self.assertEqual(out["removed"], [])
        self.assertEqual(len(out["errors"]), 1)


# -- criterion 3: never a file a card still links to -------------------------------------------------------------

class LinkedTest(RawCase):
    def test_an_old_linked_file_and_its_report_are_kept(self):
        linked = self.put("chat", NOW - timedelta(days=45), "aaaaaa")
        unlinked = self.put("chat", NOW - timedelta(days=45), "bbbbbb")
        orphan = self.put("chat", NOW - timedelta(days=45), "cccccc")
        orphan.unlink()  # the card still names it; its report stays too
        card_list = [{"source": {"raw": self.rel(linked)}}, {"source": {"raw": self.rel(orphan)}},
                     {"source": "not a block"}, {"source": {"raw": 7}}, {"title": "no source"}]
        out = self.prune(linked=lambda: card_list)
        self.assertTrue(linked.exists() and self.report(linked).exists())
        self.assertTrue(self.report(orphan).exists())
        self.assertFalse(unlinked.exists() or self.report(unlinked).exists())
        self.assertEqual(sorted(out["removed"]), sorted([self.rel(unlinked), self.rel(self.report(unlinked))]))

    def test_a_real_card_keeps_its_old_raw_file_through_a_gate_run(self):
        linked = self.put("chat", NOW - timedelta(days=45), "aaaaaa")
        unlinked = self.put("chat", NOW - timedelta(days=45), "bbbbbb")
        self.source_card(self.rel(linked))
        self.gate()
        self.assertTrue(linked.exists() and self.report(linked).exists())
        self.assertFalse(unlinked.exists())

    def test_the_cards_are_not_read_when_every_file_is_young(self):
        young = self.put("chat", NOW - timedelta(days=3))
        never = mock.Mock(side_effect=AssertionError("the cards must not be read"))
        out = self.prune(linked=never)
        never.assert_not_called()
        self.assertEqual(out["removed"], [])
        with mock.patch.object(Store, "list", side_effect=AssertionError("Store.list must not be called")) as lst:
            heartbeat = self.gate()
        lst.assert_not_called()
        self.assertEqual(heartbeat["errors"], [])
        self.assertTrue(young.exists())

    def test_the_cards_are_read_once_when_files_are_old(self):
        for i, src in enumerate(("chat", "mail", "tickets")):
            self.put(src, NOW - timedelta(days=40), f"aaaaa{i}")
        calls = mock.Mock(return_value=[])
        out = self.prune(linked=calls)
        self.assertEqual(calls.call_count, 1)
        self.assertEqual(len(out["removed"]), 6)

    def test_a_card_list_that_fails_removes_nothing_and_is_one_error(self):
        old = self.put("chat", NOW - timedelta(days=40))
        out = self.prune(linked=mock.Mock(side_effect=CardError("items/2026-11/c-x.json: not JSON")))
        self.assertTrue(old.exists() and self.report(old).exists())
        self.assertEqual(out["removed"], [])
        self.assertEqual(len(out["errors"]), 1)
        self.assertIs(out["errors"][0]["prune_raw"], True)
        self.assertIn("not JSON", out["errors"][0]["error"])

    def test_a_damaged_card_file_means_nothing_is_removed_and_one_error(self):
        old = self.put("chat", NOW - timedelta(days=40))
        card = self.source_card("sources/.raw/chat/elsewhere.json")
        Store(self.data).path(card["id"]).write_text("{ damaged", encoding="utf-8")
        out = self.gate()
        self.assertTrue(old.exists() and self.report(old).exists())
        raw_errors = [e for e in out["errors"] if "prune_raw" in e]
        self.assertEqual(len(raw_errors), 1, out["errors"])
        self.assertIs(raw_errors[0]["prune_raw"], True)
        self.assertEqual(len([line for line in self.log() if "prune raw: error:" in line]), 1)
        self.assertFalse(any(line.split(" ", 1)[1].startswith("pruned") and "raw" in line for line in self.log()))


# -- criterion 4: only inside the raw folder, no symlink followed -------------------------------------------------

class SymlinkTest(RawCase):
    def outside(self) -> Path:
        """A folder outside the data folder holding an old read (raw file and report)."""
        out = self.tmp / "outside"
        out.mkdir(exist_ok=True)
        (out / name(NOW - timedelta(days=90))).write_text("{}", encoding="utf-8")
        (out / name(NOW - timedelta(days=90), report=True)).write_text("{}", encoding="utf-8")
        return out

    def assert_outside_intact(self, out: Path):
        self.assertEqual(sorted(p.name for p in out.iterdir()),
                         sorted([name(NOW - timedelta(days=90)), name(NOW - timedelta(days=90), report=True)]))

    def test_a_linked_raw_folder_prunes_nothing_and_logs_one_line(self):
        out = self.tmp / "raw-elsewhere"
        (out / "chat").mkdir(parents=True)
        (out / "chat" / name(NOW - timedelta(days=90))).write_text("{}", encoding="utf-8")
        (self.data / "sources").mkdir()
        os.symlink(out, self.data / RAW)
        result = self.prune()
        self.assertEqual(result, {"removed": [], "errors": [], "link": RAW})
        heartbeat = self.gate()
        self.assertTrue((out / "chat" / name(NOW - timedelta(days=90))).exists())
        self.assertTrue((self.data / RAW).is_symlink())
        self.assertEqual(heartbeat["errors"], [])
        lines = [line for line in self.log() if "is a link" in line]
        self.assertEqual(len(lines), 1, self.log())
        self.assertIn(RAW, lines[0])

    def test_a_linked_sources_folder_prunes_nothing(self):
        out = self.tmp / "sources-elsewhere"
        (out / ".raw" / "chat").mkdir(parents=True)
        old = out / ".raw" / "chat" / name(NOW - timedelta(days=90))
        old.write_text("{}", encoding="utf-8")
        os.symlink(out, self.data / "sources")
        self.assertEqual(self.prune(), {"removed": [], "errors": [], "link": "sources"})
        self.assertEqual(self.gate()["errors"], [])
        self.assertTrue(old.exists())
        self.assertTrue((self.data / "sources").is_symlink())
        self.assertEqual(len([line for line in self.log() if "is a link" in line]), 1)

    def test_a_linked_source_folder_and_a_linked_file_are_skipped(self):
        out = self.outside()
        (self.data / RAW).mkdir(parents=True)
        os.symlink(out, self.data / RAW / "linked-source")
        real = self.put("chat", NOW - timedelta(days=90), "ffffff", report=False)
        link_file = self.data / RAW / "chat" / name(NOW - timedelta(days=90), "eeeeee")
        os.symlink(out / name(NOW - timedelta(days=90)), link_file)
        link_report = self.data / RAW / "chat" / name(NOW - timedelta(days=90), "eeeeee", report=True)
        os.symlink(out / name(NOW - timedelta(days=90), report=True), link_report)
        result = self.prune()
        self.assertEqual(result["removed"], [self.rel(real)])
        self.assert_outside_intact(out)
        self.assertTrue((self.data / RAW / "linked-source").is_symlink())
        self.assertTrue(link_file.is_symlink() and link_report.is_symlink())

    def test_a_folder_swapped_for_a_link_after_the_listing_is_not_followed(self):
        # The source folder is opened once, without following a link, and every remove is relative to it: a
        # swap after the listing cannot send a remove elsewhere.
        out = self.outside()
        raw = self.put("chat", NOW - timedelta(days=90))  # same name as the outside files
        folder = raw.parent
        real_scandir = os.scandir

        def swap_after_listing(target):
            entries = list(real_scandir(target))
            if isinstance(target, int) and any(e.name == raw.name for e in entries):
                shutil.rmtree(self.tmp / "moved", True)
                os.rename(folder, self.tmp / "moved")
                os.symlink(out, folder)
            return iter(entries)

        with mock.patch.object(os, "scandir", swap_after_listing):
            self.prune()
        self.assert_outside_intact(out)
        self.assertTrue(folder.is_symlink())
        self.assertFalse((self.tmp / "moved" / raw.name).exists())  # removed in the folder that was listed


# -- criterion 5: wired into every gate run -----------------------------------------------------------------------

class GateRunTest(RawCase):
    def test_a_gate_run_prunes_and_logs_one_line(self):
        old = self.put("chat", NOW - timedelta(days=40))
        self.put("mail", NOW - timedelta(days=40), report=False)
        young = self.put("chat", NOW - timedelta(days=1), "bbbbbb")
        out = self.gate()
        self.assertFalse(old.exists())
        self.assertTrue(young.exists())
        self.assertEqual(sorted(out), ["errors", "last_run_at", "pruned", "routines", "watches_due"])
        self.assertEqual(out["pruned"], [])  # restore points only
        self.assertEqual(json.loads((self.data / "gate.json").read_text(encoding="utf-8")), out)
        stamp = cards.stamp(NOW)
        self.assertIn(f"{stamp} pruned 3 raw source file(s) older than 30 days", self.log())
        self.assertEqual(self.log()[-1],
                         f"{stamp} run: 0 routine slot(s), 0 watch(es) due, 0 pruned, 0 error(s)")
        self.gate(NOW + timedelta(minutes=1))
        self.assertEqual(len([line for line in self.log() if "raw source file" in line]), 1)  # nothing on N = 0

    def test_the_run_prunes_with_its_own_now(self):
        at = NOW - timedelta(days=30)
        raw = self.put("chat", at)
        self.gate(NOW)
        self.assertTrue(raw.exists())
        self.gate(NOW + timedelta(seconds=1))
        self.assertFalse(raw.exists())

    def test_the_raw_prune_runs_right_after_the_restore_point_prune(self):
        order = []
        real_store_prune, real_raw = Store.prune, engine.prune_raw

        def store_prune(s, now):
            order.append("store")
            return real_store_prune(s, now)

        def raw_prune(data, now, linked):
            order.append(("raw", now))
            return real_raw(data, now, linked)

        real_write = gate.write_json

        def write(path, obj):
            if Path(path).name == gate.HEARTBEAT:
                order.append("heartbeat")
            return real_write(path, obj)

        with mock.patch.object(Store, "prune", store_prune), mock.patch.object(engine, "prune_raw", raw_prune), \
                mock.patch.object(gate, "write_json", write):
            self.gate()
        self.assertEqual(order, ["store", ("raw", NOW), "heartbeat"])

    def test_a_file_that_cannot_be_removed_is_one_error_and_the_rest_still_go(self):
        stuck = self.put("chat", NOW - timedelta(days=40), "aaaaaa", report=False)
        other = self.put("chat", NOW - timedelta(days=40), "bbbbbb")
        real = os.unlink

        def fail_one(path, *a, **k):
            if os.fspath(path) == stuck.name:
                raise PermissionError(13, "Permission denied", stuck.name)
            return real(path, *a, **k)

        with mock.patch.object(os, "unlink", fail_one):
            out = self.gate()
        self.assertTrue(stuck.exists())
        self.assertFalse(other.exists() or self.report(other).exists())
        self.assertEqual(out["errors"], [{"prune_raw": self.rel(stuck), "error": mock.ANY}])
        self.assertIn("Permission denied", out["errors"][0]["error"])
        self.assertEqual(json.loads((self.data / "gate.json").read_text(encoding="utf-8")), out)
        log = self.log()
        self.assertEqual(len([line for line in log if "prune raw: error:" in line]), 1)
        self.assertIn(f"{cards.stamp(NOW)} pruned 2 raw source file(s) older than 30 days", log)
        self.assertTrue(log[-1].endswith("1 error(s)"))

    def test_an_unreadable_source_folder_is_one_error_and_the_others_still_go(self):
        locked = self.put("chat", NOW - timedelta(days=40), report=False)
        other = self.put("mail", NOW - timedelta(days=40), report=False)
        real_scandir = os.scandir
        chat_fd = []

        def deny(target):
            if isinstance(target, int) and chat_fd and target == chat_fd[0]:
                raise PermissionError(13, "Permission denied")
            return real_scandir(target)

        real_open = os.open

        def remember(path, flags, *a, **k):
            fd = real_open(path, flags, *a, **k)
            if os.fspath(path) == "chat":
                chat_fd.append(fd)
            return fd

        with mock.patch.object(os, "scandir", deny), mock.patch.object(os, "open", remember):
            out = self.prune()
        self.assertTrue(locked.exists())
        self.assertFalse(other.exists())
        self.assertEqual(out["errors"], [{"prune_raw": f"{RAW}/chat", "error": mock.ANY}])


# -- criterion 6: still no outside effects -----------------------------------------------------------------------

class NoOutsideEffectsTest(RawCase):
    def test_no_process_no_socket_and_every_remove_is_inside_the_raw_folder(self):
        for src in ("chat", "mail"):
            self.put(src, NOW - timedelta(days=40))
            self.put(src, NOW - timedelta(days=2), "bbbbbb")
        raw_folders = {(st.st_dev, st.st_ino) for st in
                       (os.lstat(p) for p in (self.data / RAW).iterdir())}

        def refuse(*a, **k):
            raise AssertionError(f"the prune must not start a process or open a socket: {a!r}")

        removes = []
        real_unlink, real_open = os.unlink, builtins.open
        written = []

        def watch_unlink(path, *a, dir_fd=None, **k):
            removes.append((os.fspath(path), None if dir_fd is None else os.fstat(dir_fd)))
            return real_unlink(path, *a, dir_fd=dir_fd, **k)

        def watch_open(file, mode="r", *a, **k):
            if any(ch in mode for ch in "wax+"):
                written.append(os.fspath(file))
            return real_open(file, mode, *a, **k)

        patches = [mock.patch.object(subprocess, "Popen", side_effect=refuse),
                   mock.patch.object(subprocess, "run", side_effect=refuse),
                   mock.patch.object(socket, "socket", side_effect=refuse),
                   mock.patch.object(socket, "create_connection", side_effect=refuse),
                   mock.patch.object(os, "unlink", watch_unlink),
                   mock.patch.object(builtins, "open", watch_open)]
        patches += [mock.patch.object(os, n, side_effect=refuse)
                    for n in ("system", "execv", "execve", "execvp", "execvpe", "execl", "execlp", "spawnv",
                              "posix_spawn", "posix_spawnp", "fork", "remove", "rmdir", "removedirs", "rename",
                              "renames") if hasattr(os, n)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        out = self.gate()
        for p in reversed(patches):
            p.stop()
        self.assertEqual(out["errors"], [])
        self.assertEqual(len(removes), 4)
        for path, folder in removes:
            self.assertNotIn("/", path)
            self.assertIsNotNone(folder, path)
            self.assertIn((folder.st_dev, folder.st_ino), raw_folders, path)
        for path in written:
            self.assertTrue(Path(path).resolve().is_relative_to(self.data), path)
        left = sorted(p.relative_to(self.data / RAW).as_posix() for p in (self.data / RAW).rglob("*")
                      if p.is_file())
        self.assertEqual(left, sorted(f"{s}/{name(NOW - timedelta(days=2), 'bbbbbb', r)}"
                                      for s in ("chat", "mail") for r in (False, True)))

    def test_a_run_that_finds_the_lock_held_removes_nothing(self):
        import fcntl
        old = self.put("chat", NOW - timedelta(days=40))
        fd = os.open(str(self.data / gate.LOCK), os.O_CREAT | os.O_RDWR, 0o644)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.assertEqual(self.gate()["locked"], True)
        self.assertTrue(old.exists() and self.report(old).exists())


class GateCommandTest(unittest.TestCase):
    def setUp(self):
        if temp_inside_the_accounts_claude():
            self.skipTest("the temp folder is inside the account's ~/.claude, so ap2 refuses every temp data folder")
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-raw-prune-cli-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.data = self.tmp / "data"

    def test_ap2_gate_with_a_valid_now_prunes_old_raw_files(self):
        now = datetime(2025, 11, 11, 17, 1, 30, tzinfo=UTC)  # in the past, so --now is valid
        folder = self.data / RAW / "chat"
        folder.mkdir(parents=True)
        old, young = name(now - timedelta(days=31)), name(now - timedelta(days=1), "bbbbbb")
        for n in (old, young, name(now - timedelta(days=31), report=True)):
            (folder / n).write_text("{}", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k not in ("AP2_DATA", "LC_ALL")}
        env.update(HOME=str(self.home), AP2_DATA=str(self.data), PYTHON_COLORS="0", TZ="Pacific/Kiritimati")
        r = subprocess.run([sys.executable, str(ROOT / "bin" / "ap2"), "gate", "--now", "2025-11-11T17:01:30Z"],
                           capture_output=True, text=True, env=env, errors="replace", timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertEqual(json.loads(r.stdout)["errors"], [])
        self.assertEqual(sorted(p.name for p in folder.iterdir()), [young])
        self.assertIn("2025-11-11T17:01:30Z pruned 2 raw source file(s) older than 30 days",
                      (self.data / "gate.log").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
