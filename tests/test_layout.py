from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest import mock

from tests.helpers import TempData, write_ap
from autopilot import cli, config, gate, paths, state


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
            self.assertEqual(config.migrate_legacy(), [])          # nothing is merged or lost
            self.assertTrue((old / "config.json").exists())
            self.assertTrue((stray / "brief-review.md").exists())

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

    def test_legacy_autopilot_named_projects_moves_first(self):
        with TempData() as d:
            legacy(d, "alpha")
            legacy(d, "projects")
            self.assertEqual(config.migrate_legacy(), ["projects", "alpha"])
            self.assertFalse((d / "projects" / "config.json").exists())
            self.assertTrue((d / "projects" / "projects" / "config.json").exists())
            self.assertTrue((d / "projects" / "alpha" / "config.json").exists())
            self.assertEqual(config.list_aps(), ["alpha", "projects"])


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


if __name__ == "__main__":
    unittest.main()
