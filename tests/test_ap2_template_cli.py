from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import ROOT
from tests.test_ap2_cli import temp_inside_the_accounts_claude
from tests.test_ap2_templates import BUILTIN_KINDS, FIXTURES
from assistant import roles, templates

AP2 = ROOT / "bin" / "ap2"


class TemplateCommandTest(unittest.TestCase):
    """Criterion 7: `ap2 template list|show|check`, JSON on stdout, `--data` / `AP2_DATA`, one-line errors."""

    def setUp(self):
        if temp_inside_the_accounts_claude():
            self.skipTest("the temp folder is inside the account's ~/.claude, so ap2 refuses every temp data folder")
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-template-cli-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.data = self.tmp / "data"

    def run_ap2(self, *args, data=None):
        # PYTHON_COLORS=0: Python 3.14 argparse colors its usage when FORCE_COLOR is set. LC_ALL is left as it is.
        env = {k: v for k, v in os.environ.items() if k != "AP2_DATA"}
        env.update(HOME=str(self.home), AP2_DATA=str(self.data if data is None else data), PYTHON_COLORS="0",
                   PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, str(AP2), *args], capture_output=True, text=True, env=env,
                              encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=30)

    def ok(self, *args, **kw):
        r = self.run_ap2(*args, **kw)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        return json.loads(r.stdout)

    def fails(self, *args, **kw):
        r = self.run_ap2(*args, **kw)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(r.stdout, "")
        lines = r.stderr.splitlines()
        self.assertEqual(len(lines), 1, r.stderr)
        self.assertTrue(lines[0].startswith("ap2: "), r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        return lines[0]

    def install(self, name):
        (self.data / "templates").mkdir(parents=True, exist_ok=True)
        return Path(shutil.copy(FIXTURES / name, self.data / "templates" / name))

    def test_list_prints_the_builtins(self):
        out = self.ok("template", "list")
        self.assertEqual([t["kind"] for t in out], sorted(BUILTIN_KINDS))
        self.assertEqual({t["origin"] for t in out}, {"builtin"})
        code = next(t for t in out if t["kind"] == "code")
        self.assertEqual(code, {"kind": "code", "title": "Code task", "origin": "builtin",
                                "file": str(templates.BUILTIN_DIR / "code.yaml"), "replaces_builtin": False})
        self.assertFalse(self.data.exists())  # reading templates never creates the data folder

    def test_list_shows_a_user_override_and_a_new_kind(self):
        self.install("question.yaml")
        self.install("parcel.yaml")
        out = {t["kind"]: t for t in self.ok("template", "list")}
        self.assertEqual(set(out), BUILTIN_KINDS | {"parcel"})
        self.assertEqual((out["question"]["origin"], out["question"]["replaces_builtin"]), ("user", True))
        self.assertEqual(out["question"]["file"], str(self.data / "templates" / "question.yaml"))
        self.assertEqual((out["parcel"]["origin"], out["parcel"]["replaces_builtin"]), ("user", False))

    def test_data_flag_wins_over_env(self):
        other = self.tmp / "other"
        (other / "templates").mkdir(parents=True)
        shutil.copy(FIXTURES / "parcel.yaml", other / "templates" / "parcel.yaml")
        kinds = [t["kind"] for t in self.ok("template", "list", "--data", str(other))]
        self.assertIn("parcel", kinds)
        self.assertNotIn("parcel", [t["kind"] for t in self.ok("template", "list")])

    def test_show_prints_the_template_with_each_roles_shipped_tools(self):
        self.install("parcel.yaml")
        out = self.ok("template", "show", "parcel")
        self.assertEqual(out["kind"], "parcel")
        self.assertEqual(out["origin"], "user")
        self.assertEqual(out["template"]["view"], ["plan", "activity"])
        self.assertEqual(out["roles"], [{"role": "general", "when": "delivery day 08:00",
                                         "brief": "Check the carrier page for {tracking}. If it says out for "
                                                  "delivery, nudge me.",
                                         "tools": list(roles.ROLES["general"].tools),
                                         "floor": roles.ROLES["general"].floor}])

    def test_show_a_builtin(self):
        out = self.ok("template", "show", "code")
        self.assertEqual([r["role"] for r in out["roles"]], ["implement", "review", "pr-loop"])
        self.assertEqual(out["template"]["view"][0], "source_block")

    def test_show_an_unknown_kind_fails_on_one_line(self):
        line = self.fails("template", "show", "crate")
        self.assertIn("'crate'", line)
        self.assertIn("question", line)  # the kinds there are

    def test_check_validates_without_installing(self):
        path = self.tmp / "parcel.yaml"
        shutil.copy(FIXTURES / "parcel.yaml", path)
        out = self.ok("template", "check", str(path))
        self.assertEqual(out, {"ok": True, "kind": "parcel", "file": str(path), "replaces": None,
                               "installed": False})
        self.assertFalse(self.data.exists())  # nothing installed, not even the folder
        self.assertNotIn("parcel", [t["kind"] for t in self.ok("template", "list")])

    def test_check_says_it_would_replace_the_builtin(self):
        path = self.tmp / "question.yaml"
        shutil.copy(FIXTURES / "question.yaml", path)
        self.assertEqual(self.ok("template", "check", str(path))["replaces"], "builtin")

    def test_check_reads_the_data_folder_from_env_and_from_the_flag(self):
        other = self.tmp / "other"
        (other / "templates").mkdir(parents=True)
        shutil.copy(FIXTURES / "question.yaml", other / "templates" / "question.yaml")
        path = self.tmp / "question.yaml"
        shutil.copy(FIXTURES / "question.yaml", path)
        self.assertEqual(self.ok("template", "check", str(path), data=other)["replaces"], "user")  # AP2_DATA
        self.assertEqual(self.ok("template", "check", str(path), "--data", str(other))["replaces"], "user")
        self.assertEqual(self.ok("template", "check", str(path))["replaces"], "builtin")  # AP2_DATA has none

    def test_check_refuses_a_template_that_adds_tools(self):
        path = self.tmp / "parcel.yaml"
        path.write_text((FIXTURES / "parcel.yaml").read_text() + "    tools: [Bash, Write]\n", encoding="utf-8")
        line = self.fails("template", "check", str(path))
        self.assertEqual(line, f"ap2: {path}: line 16: 'tools' is not allowed: a template cannot add tools or "
                               "permissions; each role keeps the tools and permission floor it ships with")

    def test_check_refuses_a_bad_file_with_its_line(self):
        path = self.tmp / "parcel.yaml"
        path.write_text((FIXTURES / "parcel.yaml").read_text().replace("role: general", "role: wizard"),
                        encoding="utf-8")
        line = self.fails("template", "check", str(path))
        self.assertTrue(line.startswith(f"ap2: {path}: line 13: unknown role 'wizard'"), line)

    def test_check_a_missing_file(self):
        self.assertIn("cannot read", self.fails("template", "check", str(self.tmp / "nope.yaml")))

    def test_a_bad_user_template_fails_list_and_show_naming_it(self):
        bad = self.install("parcel.yaml")
        bad.write_text(bad.read_text() + "permissions: {allow: [Bash]}\n", encoding="utf-8")
        for args in (("template", "list"), ("template", "show", "code")):
            with self.subTest(args):
                line = self.fails(*args)
                self.assertIn(str(bad), line)
                self.assertIn("'permissions' is not allowed", line)

    def test_a_refused_data_folder_is_refused(self):
        line = self.fails("template", "list", "--data", str(self.home / ".claude" / "x"))
        self.assertIn("pick a folder outside ~/.claude", line)

    def test_usage_errors_exit_2(self):
        for args in (("template",), ("template", "show"), ("template", "check"), ("template", "nosuch")):
            with self.subTest(args):
                r = self.run_ap2(*args)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertEqual(r.stdout, "")
                self.assertIn("usage: ap2 template", r.stderr)

    def test_help_lists_the_template_command(self):
        r = self.run_ap2("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("template", r.stdout)


if __name__ == "__main__":
    unittest.main()
