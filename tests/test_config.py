from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest import mock

from tests.helpers import ROOT, TempData, write_ap
from autopilot import cli, config, paths


class ConfigTest(unittest.TestCase):
    def test_defaults_when_no_file(self):
        with TempData():
            g = config.load_global()
            self.assertEqual(g["max_workers"], 5)
            self.assertEqual(g["port_base"], 7100)
            self.assertEqual(g["budget"]["window_pct"], 85)
            self.assertEqual(g["report_times"], ["09:00", "17:00", "21:00"])

    def test_file_overrides_deep_merge(self):
        with TempData() as d:
            (d / "config.json").write_text(json.dumps({"budget": {"window_pct": 70}}))
            g = config.load_global()
            self.assertEqual(g["budget"]["window_pct"], 70)
            self.assertEqual(g["budget"]["stale_min"], 60)

    def test_valid_name(self):
        for ok in ["checkout", "a-b"]:
            self.assertTrue(paths.valid_name(ok), ok)
        for bad in ["Checkout", "1x", "a_b", ""]:
            self.assertFalse(paths.valid_name(bad), bad)

    def test_active_excludes_paused_and_stopped(self):
        with TempData() as d:
            write_ap(d, "alpha")
            write_ap(d, "beta")
            write_ap(d, "gamma", stopped=True)
            (d / "beta" / "paused").write_text("")
            self.assertEqual(config.list_aps(), ["alpha", "beta", "gamma"])
            self.assertEqual(config.status_of("beta"), "paused")
            self.assertEqual(config.status_of("gamma"), "stopped")
            self.assertEqual(config.active_aps(), ["alpha"])


def leaves(d, prefix=""):
    out = []
    for k, v in d.items():
        if isinstance(v, dict) and v:
            out += leaves(v, prefix + k + ".")
        else:
            out.append(prefix + k)
    return out


def run_cli(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(["config", *args])
    return rc, out.getvalue(), err.getvalue()


class SaveOnlyChangesTest(unittest.TestCase):
    """The file keeps only what the user changed, so a new default in the package reaches every install."""

    def test_save_stores_only_overrides(self):
        with TempData() as d:
            g = config.load_global()
            g["max_workers"] = 3
            g["tools"]["claude"] = "/x/claude"
            config.save_global(g)
            self.assertEqual(json.loads((d / "config.json").read_text()),
                             {"max_workers": 3, "tools": {"claude": "/x/claude"}})

    def test_a_new_default_reaches_a_saved_install(self):
        with TempData():
            g = config.load_global()
            g["max_workers"] = 3
            config.save_global(g)
            with mock.patch.dict(config.DEFAULT_GLOBAL, {"forced_pass_min": 15}):
                self.assertEqual(config.load_global()["forced_pass_min"], 15)
                self.assertEqual(config.load_global()["max_workers"], 3)

    def test_old_files_with_every_default_shrink_on_next_save(self):
        with TempData() as d:
            (d / "config.json").write_text(json.dumps(dict(config.DEFAULT_GLOBAL, max_workers=2)))
            config.save_global(config.load_global())
            self.assertEqual(json.loads((d / "config.json").read_text()), {"max_workers": 2})


class ConfigListTest(unittest.TestCase):
    def test_every_setting_is_described(self):
        self.assertEqual(sorted(leaves(config.DEFAULT_GLOBAL)), sorted(config.DESCRIBE))
        self.assertTrue(all(v.strip() for v in config.DESCRIBE.values()))

    def test_list_shows_value_default_and_what_changed(self):
        with TempData():
            self.assertEqual(run_cli("set", "reply_check.every_s=0")[0], 0)
            rc, out, _ = run_cli("list")
            self.assertEqual(rc, 0)
            row = next(l for l in out.splitlines() if "reply_check.every_s" in l)
            self.assertTrue(row.startswith("*"), row)
            self.assertIn("0", row)
            self.assertIn("60", row)
            quiet = next(l for l in out.splitlines() if "reply_check.quiet" in l)
            self.assertFalse(quiet.startswith("*"), quiet)

    def test_list_json(self):
        with TempData():
            rows = json.loads(run_cli("list", "--json")[1])
            row = next(r for r in rows if r["key"] == "gate_interval_s")
            self.assertEqual((row["value"], row["default"], row["changed"]), (60, 60, False))
            self.assertTrue(row["description"])

    def test_set_and_reset_a_nested_key(self):
        with TempData() as d:
            self.assertEqual(run_cli("set", 'reply_check.quiet=["23:00","07:00"]')[0], 0)
            self.assertEqual(config.load_global()["reply_check"]["quiet"], ["23:00", "07:00"])
            self.assertEqual(config.load_global()["reply_check"]["every_s"], 60)
            self.assertEqual(run_cli("reset", "reply_check.quiet")[0], 0)
            self.assertEqual(config.load_global()["reply_check"]["quiet"], ["22:00", "06:00"])
            self.assertEqual(json.loads((d / "config.json").read_text()), {})

    def test_unknown_key_refused(self):
        with TempData():
            self.assertEqual(run_cli("set", "reply_check.colour=red")[0], 2)
            self.assertEqual(run_cli("set", "nope=1")[0], 2)
            self.assertEqual(run_cli("reset", "nope")[0], 2)

    def test_reference_doc_matches_the_defaults(self):
        self.assertEqual((ROOT / "references" / "config.md").read_text(), config.markdown())


if __name__ == "__main__":
    unittest.main()
