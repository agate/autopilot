from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from unittest import mock

from tests.helpers import TempData, write_ap
from autopilot import cli, config, dispatch, events, gate, lock, paths, snapshot, state


def legacy(data, name, **st):
    """An autopilot made before projects/ existed: its folder sits at the top of the data root."""
    d = data / name
    d.mkdir(parents=True)
    (d / "config.json").write_text(json.dumps({"name": name, "source": {"preset": "jira-epic", "key": "PROJ-100"},
                                               "repo": str(data)}))
    (d / "state.json").write_text(json.dumps({**state.empty(), **st}))
    return d


class LayoutTest(unittest.TestCase):
    def test_new_autopilot_lives_under_projects(self):
        with TempData() as d:
            write_ap(d, "alpha")
            self.assertEqual(paths.ap_dir("alpha"), d / "projects" / "alpha")
            self.assertEqual(config.list_aps(), ["alpha"])

    def test_legacy_autopilot_is_found_until_moved(self):
        with TempData() as d:
            old = legacy(d, "alpha")
            write_ap(d, "beta")
            self.assertEqual(paths.ap_dir("alpha"), old)
            self.assertEqual(config.list_aps(), ["alpha", "beta"])

    def test_move_keeps_files_and_rewrites_stored_paths(self):
        with TempData() as d:
            old = d / "alpha"
            legacy(d, "alpha", tasks=[{"id": "T-1", "status": "todo", "spec": str(old / "specs" / "T-1.md")}])
            (old / "paused").write_text("")
            (old / "RULES.md").write_text("- a rule\n")
            self.assertEqual(config.migrate_legacy(), ["alpha"])
            new = d / "projects" / "alpha"
            self.assertFalse(old.exists())
            self.assertEqual(paths.ap_dir("alpha"), new)
            self.assertEqual(config.status_of("alpha"), "paused")
            self.assertEqual((new / "RULES.md").read_text(), "- a rule\n")
            spec = json.loads((new / "state.json").read_text())["tasks"][0]["spec"]
            self.assertEqual(spec, str(new / "specs" / "T-1.md"))
            ev = [json.loads(l) for l in (new / "events.jsonl").read_text().splitlines()]
            self.assertEqual(ev[-1]["type"], "moved")
            self.assertEqual(ev[-1]["data"], {"from": str(old), "to": str(new)})
            self.assertEqual(config.migrate_legacy(), [])

    def test_move_waits_while_a_worker_runs(self):
        with TempData() as d:
            old = legacy(d, "alpha", workers=[{"task": "T-1", "role": "implement"}])
            self.assertEqual(config.migrate_legacy(), [])
            self.assertTrue((old / "config.json").exists())
            self.assertEqual(paths.ap_dir("alpha"), old)

    def test_move_skips_broken_state(self):
        with TempData() as d:
            old = legacy(d, "alpha")
            (old / "state.json").write_text("{broken")
            self.assertEqual(config.migrate_legacy(), [])
            self.assertTrue(old.exists())

    def test_move_leaves_a_folder_whose_new_place_is_taken(self):
        with TempData() as d:
            old = legacy(d, "alpha")
            new = write_ap(d, "alpha")
            self.assertEqual(config.migrate_legacy(), [])
            self.assertTrue((old / "config.json").exists())
            self.assertEqual(paths.ap_dir("alpha"), new)
            self.assertEqual(config.list_aps(), ["alpha"])

    def test_a_stray_new_folder_does_not_hide_the_legacy_autopilot(self):
        # A pass or talk session that writes into projects/<name>/ before the move must not make the
        # autopilot vanish: the new place counts only once it holds config.json.
        with TempData() as d:
            old = legacy(d, "alpha")
            stray = d / "projects" / "alpha" / "runs" / "T-1"
            stray.mkdir(parents=True)
            (stray / "brief-review.md").write_text("brief")
            self.assertEqual(paths.ap_dir("alpha"), old)
            self.assertEqual(config.load_ap("alpha")["name"], "alpha")
            self.assertEqual(config.list_aps(), ["alpha"])

    def test_a_stray_new_folder_is_set_aside_and_the_move_runs(self):
        # Left in place, it would block the move for good. Nothing in it is lost, and the event says where it went.
        with TempData() as d:
            legacy(d, "alpha")
            (d / "projects" / "alpha" / "runs" / "T-1").mkdir(parents=True)
            (d / "projects" / "alpha" / "runs" / "T-1" / "brief-review.md").write_text("brief")
            self.assertEqual(config.migrate_legacy(), ["alpha"])
            self.assertTrue((d / "projects" / "alpha" / "config.json").exists())
            aside = [x for x in (d / "projects").iterdir() if x.name.startswith(".alpha.stray-")]
            self.assertEqual(len(aside), 1)
            self.assertEqual((aside[0] / "runs" / "T-1" / "brief-review.md").read_text(), "brief")
            self.assertEqual(config.list_aps(), ["alpha"])
            moved = [e for e in events.since("alpha", 0)[0] if e["type"] == "moved"]
            self.assertEqual(moved[0]["data"]["stray"], str(aside[0]))

    def test_run_files_with_absolute_paths_are_rewritten_too(self):
        # `ap evidence check` accepts an absolute path inside the run directory (finding 4).
        with TempData() as d:
            old = legacy(d, "alpha")
            run = old / "runs" / "T-1"
            (run / "media").mkdir(parents=True)
            (run / "media" / "a.png").write_bytes(b"png")
            (run / "evidence.json").write_text(json.dumps({"screenshots": [{"path": str(run / "media" / "a.png")}]}))
            (run / "notes.json").write_text("{not json")
            config.migrate_legacy()
            new = d / "projects" / "alpha" / "runs" / "T-1"
            ev = json.loads((new / "evidence.json").read_text())
            self.assertEqual(ev["screenshots"][0]["path"], str(new / "media" / "a.png"))
            self.assertEqual((new / "notes.json").read_text(), "{not json")   # left as it was

    def test_an_interrupted_rewrite_is_finished_by_the_next_run(self):
        # The folder is already moved when the rewrite runs; a failure there must not leave old paths for good.
        with TempData() as d:
            old = legacy(d, "alpha", tasks=[{"id": "T-1", "status": "todo", "spec": str(d / "alpha" / "specs" / "T-1.md")}])
            real = config._write
            with mock.patch.object(config, "_write", side_effect=OSError("disk full")):
                config.migrate_legacy()
            new = d / "projects" / "alpha"
            self.assertTrue((new / "config.json").exists())
            self.assertTrue((new / ".rewrite-pending").exists())
            self.assertFalse([e for e in events.since("alpha", 0)[0] if e["type"] == "moved"])
            self.assertEqual(config.migrate_legacy(), [])
            st = json.loads((new / "state.json").read_text())
            self.assertEqual(st["tasks"][0]["spec"], str(new / "specs" / "T-1.md"))
            self.assertFalse((new / ".rewrite-pending").exists())
            self.assertEqual(len([e for e in events.since("alpha", 0)[0] if e["type"] == "moved"]), 1)
            self.assertIs(config._write, real)

    def test_only_a_movable_autopilot_keeps_the_gate_checking(self):
        # Each check costs one session listing a minute: skip autopilots that cannot move yet.
        with TempData() as d:
            self.assertFalse(config.legacy_waiting())
            legacy(d, "alpha", workers=[{"task": "T-1"}])        # a worker runs
            self.assertFalse(config.legacy_waiting())
            broken = legacy(d, "beta")
            (broken / "state.json").write_text("{broken")
            self.assertFalse(config.legacy_waiting())
            legacy(d, "gamma")
            write_ap(d, "gamma")                                # its new place is taken
            self.assertFalse(config.legacy_waiting())
            legacy(d, "delta")
            self.assertTrue(config.legacy_waiting())

    def test_a_pending_rewrite_keeps_the_gate_checking(self):
        with TempData() as d:
            new = write_ap(d, "alpha")
            (new / config.PENDING).write_text(json.dumps({"from": str(d / "alpha")}))
            self.assertTrue(config.legacy_waiting())

    def test_the_stray_folder_is_named_even_when_the_move_finishes_later(self):
        # The stray was set aside, then the rename failed; the next run finishes and still says where it went.
        with TempData() as d:
            legacy(d, "alpha")
            (d / "projects" / "alpha" / "runs").mkdir(parents=True)
            real, calls = os.rename, []

            def flaky(a, b):
                calls.append(a)
                if len(calls) == 2:
                    raise OSError("busy")
                return real(a, b)
            with mock.patch.object(os, "rename", flaky):
                self.assertEqual(config.migrate_legacy(), [])
            self.assertTrue((d / "alpha" / "config.json").exists())
            self.assertEqual(config.migrate_legacy(), ["alpha"])
            moved = [e for e in events.since("alpha", 0)[0] if e["type"] == "moved"]
            self.assertEqual(len(moved), 1)
            self.assertTrue(moved[0]["data"]["stray"].startswith(str(d / "projects" / ".alpha.stray-")))

    def test_an_event_follows_a_folder_that_moved_meanwhile(self):
        with TempData() as d:
            old = legacy(d, "alpha")
            new = write_ap(d, "alpha")
            calls = []

            def where(name):
                calls.append(name)
                return old if len(calls) == 1 else new
            with mock.patch.object(paths, "ap_dir", side_effect=where):
                events.append("alpha", "x", {})
            self.assertTrue((new / "events.jsonl").exists())
            self.assertFalse((old / "events.jsonl").exists())

    def test_doctor_flags_an_older_autopilot_named_projects(self):
        from autopilot import doctor
        with TempData() as d:
            with mock.patch.object(doctor, "_run", lambda argv: (1, "")):
                names = {c["name"]: c for c in doctor.checks(config.load_global())}
                self.assertNotIn("autopilot named projects", names)
                legacy(d, "projects")
                c = {c["name"]: c for c in doctor.checks(config.load_global())}["autopilot named projects"]
            self.assertFalse(c["ok"])
            self.assertIn("rename", c["fix"])

    def test_the_name_projects_is_reserved(self):
        # It is the folder every autopilot lives in (finding 3).
        self.assertFalse(paths.valid_name("projects"))
        self.assertTrue(paths.valid_name("project-x"))

    def test_state_lock_follows_a_folder_that_moved_while_waiting(self):
        # A command that opened the old lock file just before the move must not slip past it (finding 7).
        with TempData() as d:
            old = legacy(d, "alpha")
            new = d / "projects" / "alpha"
            new.mkdir(parents=True)
            for f in ("config.json", "state.json"):
                (new / f).write_text((old / f).read_text())
            (new / "state.lock").write_text("")
            (old / "state.lock").write_text("")
            calls = []

            def where(name):
                calls.append(name)
                return old if len(calls) == 1 else new
            opened, real_open = [], os.open

            def spy(path, *a, **k):
                if str(path).endswith("state.lock"):
                    opened.append(str(path))
                return real_open(path, *a, **k)
            with mock.patch.object(paths, "ap_dir", side_effect=where), mock.patch.object(os, "open", spy):
                with state.mutate("alpha") as st:
                    st["tasks"] = []
            # It found the folder had moved after taking the old lock, and took the new one instead.
            self.assertEqual(opened, [str(old / "state.lock"), str(new / "state.lock")])

    def test_snapshot_names_the_folder_in_use(self):
        # A long talk session reads the folder from every message, never from a path it saw hours ago (finding 5).
        with TempData() as d:
            legacy(d, "alpha")
            out, _, _ = snapshot.build("alpha", 0, {}, __import__("datetime").datetime.now())
            self.assertIn(f"Folder: {d / 'alpha'}", out)
            config.migrate_legacy()
            out, _, _ = snapshot.build("alpha", 0, {}, __import__("datetime").datetime.now())
            self.assertIn(f"Folder: {d / 'projects' / 'alpha'}", out)

    def test_dir_command_prints_the_folder_in_use(self):
        def run(*argv):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = cli.main(list(argv))
            return rc, out.getvalue().strip()
        with TempData() as d:
            legacy(d, "alpha")
            self.assertEqual(run("dir", "alpha"), (0, str(d / "alpha")))
            config.migrate_legacy()
            self.assertEqual(run("dir", "alpha"), (0, str(d / "projects" / "alpha")))
            self.assertEqual(run("dir", "nope")[0], 1)
            self.assertEqual(run("dir")[0], 2)

    def test_a_legacy_folder_named_projects_is_never_treated_as_an_autopilot(self):
        with TempData() as d:
            legacy(d, "alpha")
            legacy(d, "projects")
            self.assertEqual(config.list_aps(), ["alpha"])
            self.assertEqual(config.migrate_legacy(), ["alpha"])
            self.assertTrue((d / "projects" / "alpha" / "config.json").exists())


class GateMovesTest(unittest.TestCase):
    def test_gate_moves_legacy_autopilots_before_the_pass(self):
        with TempData() as d, \
                mock.patch.object(gate, "_sessions", lambda g: []), \
                mock.patch.object(gate, "notify", lambda msg: None), \
                mock.patch.object(gate, "_allowance", lambda g, now: {"allow": None, "paused_until": None,
                                                                      "reason": "ok"}):
            legacy(d, "alpha")
            seen = []

            def fake(g, aps):
                seen.append(str(paths.ap_dir("alpha")))
                return [{"started": 0, "free": 0, "ready": 0, "allow": None, "ended_at": "x"}]

            self.assertEqual(gate.run(dispatch_fn=fake), 0)
            self.assertEqual(seen, [str(d / "projects" / "alpha")])
            self.assertFalse((d / "alpha").exists())

    def _gate(self, d, rows):
        seen = []

        def fake(g, aps):
            seen.append(str(paths.ap_dir("alpha")))
            return [{"started": 0, "free": 0, "ready": 0, "allow": None, "ended_at": "x"}]
        with mock.patch.object(gate, "_sessions", lambda g: rows), \
                mock.patch.object(gate, "notify", lambda msg: None), \
                mock.patch.object(gate, "_allowance", lambda g, now: {"allow": None, "paused_until": None, "reason": "ok"}):
            gate.run(dispatch_fn=fake)
        return seen

    def test_gate_waits_while_an_earlier_dispatcher_still_works(self):
        # The data-root lock dies with its gate, but the dispatcher session it started may keep running.
        with TempData() as d:
            legacy(d, "alpha")
            live = [{"kind": "background", "name": dispatch.NAME, "status": "busy", "pid": os.getpid(), "startedAt": 1}]
            self._gate(d, live)
            self.assertTrue((d / "alpha" / "config.json").exists())
            self._gate(d, None)                      # the session list could not be read: wait too
            self.assertTrue((d / "alpha" / "config.json").exists())
            idle = [dict(live[0], status="idle")]
            self._gate(d, idle)
            self.assertTrue((d / "projects" / "alpha" / "config.json").exists())

    def test_gate_moves_under_the_data_root_lock(self):
        with TempData() as d:
            legacy(d, "alpha")
            held = []
            real = config.migrate_legacy
            with mock.patch.object(config, "migrate_legacy", lambda: held.append(lock.held(d / "lock")) or real()):
                self._gate(d, [])
            self.assertTrue(held and held[0])
            self.assertFalse(lock.held(d / "lock"))


if __name__ == "__main__":
    unittest.main()
