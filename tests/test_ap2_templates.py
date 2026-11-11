from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.helpers import ROOT
from assistant import cards, roles, templates, yamlish
from assistant.config import ConfigError
from assistant.templates import BriefError, TemplateError, TemplateWidenError

FIXTURES = ROOT / "tests" / "fixtures" / "ap2" / "templates"
# Design 4.2: the kinds the plugin ships, by file name.
BUILTIN_KINDS = {"question", "errand", "alert", "code", "project", "research", "routine", "login", "step",
                 "pr-review"}
# Design 6: the roles v2 ships. The PR review slice adds more (pr-review), so the tests check these are there, not that no
# other role is.
SHIPPED_ROLES = {"implement", "review", "pr-loop", "investigate", "analyze", "reviewer-angle", "general"}
# Design 12: each shipped role's tools and permission floor, copied from the design, not from roles.py.
EXPECTED_ROLES = {
    "implement": (("Agent", "Bash", "Edit", "Glob", "Grep", "NotebookEdit", "Read", "Skill", "WebFetch", "WebSearch",
                   "Write"), "worker"),
    "review": (("Agent", "Bash", "Glob", "Grep", "Read", "Skill", "Write"), "worker"),
    "pr-loop": (("Agent", "Bash", "Edit", "Glob", "Grep", "Read", "Skill", "Write"), "worker"),
    "investigate": (("Bash", "Glob", "Grep", "Read", "WebFetch", "WebSearch", "Write"), "read-only"),
    "analyze": (("Bash", "Glob", "Grep", "Read", "WebFetch", "WebSearch", "Write"), "read-only"),
    "reviewer-angle": (("Bash", "Glob", "Grep", "Read"), "read-only"),
    "general": (("Bash", "Edit", "Glob", "Grep", "Read", "WebFetch", "WebSearch", "Write"), "worker"),
}

GOOD = """\
kind: parcel
view: [plan, activity]
buttons:
  - id: arrived
    label: It arrived
    lane: done
roles:
  - role: general
    brief: "Check {tracking}."
"""


class TempFolder(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-templates-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "data"
        (self.data / "templates").mkdir(parents=True)

    def write(self, name, text, folder=None):
        path = (folder or self.tmp) / name
        path.write_text(text, encoding="utf-8")
        return path


class BuiltinTemplatesTest(unittest.TestCase):
    """Criterion 1: one file per kind, each with kind, view, buttons and roles, and the source rule."""

    def test_one_file_per_shipped_kind(self):
        files = sorted(templates.BUILTIN_DIR.glob("*.yaml"))
        self.assertEqual({f.stem for f in files}, BUILTIN_KINDS)
        self.assertFalse(templates.BUILTIN_DIR.resolve().is_relative_to((ROOT / "templates").resolve()))

    def test_every_builtin_file_follows_the_rules(self):
        cat = templates.catalog()
        self.assertEqual(set(cat), BUILTIN_KINDS)
        for path in sorted(templates.BUILTIN_DIR.glob("*.yaml")):
            with self.subTest(path.name):
                raw = yamlish.load(path.read_text(encoding="utf-8"))  # read with yamlish, as written
                self.assertEqual(raw["kind"], path.stem)
                for key in ("kind", "view", "buttons", "roles"):
                    self.assertIn(key, raw)
                self.assertTrue(raw["view"], "the view shows something")
                self.assertTrue(set(raw["view"]) <= set(templates.BLOCKS), raw["view"])
                self.assertTrue(raw["buttons"], "every built-in card has buttons")
                for b in raw["buttons"]:
                    actions = [a for a in templates.ACTIONS if a in b]
                    self.assertEqual(len(actions), 1, b)  # a lane change, a restore point, or a child card
                    if "lane" in b:
                        self.assertIn(b["lane"], cards.LANES)
                    if "child" in b:
                        self.assertIn(b["child"], BUILTIN_KINDS)
                self.assertTrue(raw["roles"], "every built-in card names the roles that do its work")
                for r in raw["roles"]:
                    self.assertIn(r["role"], roles.ROLES)
                    self.assertTrue(r["brief"].strip())
                    self.assertTrue(set(r) <= {"role", "when", "brief"}, r)
                if raw.get("source") == "any":
                    self.assertEqual(raw["view"][0], "source_block")
                else:
                    self.assertNotIn("source_block", raw["view"])
                t = cat[path.stem]
                self.assertEqual((t.origin, t.path), ("builtin", path))

    def test_kinds_that_take_outside_input_say_so(self):
        cat = templates.catalog()
        # Design 5.5 and 6.2: a source sorts items into these.
        for kind in ("question", "errand", "alert", "code", "pr-review"):
            with self.subTest(kind):
                self.assertEqual(cat[kind].data.get("source"), "any")


class RolesTest(unittest.TestCase):
    """Criterion 2: the shipped roles, each with its tools and permission floor, fixed in code."""

    def test_the_shipped_roles_are_there(self):
        self.assertTrue(SHIPPED_ROLES <= set(roles.ROLES), set(roles.ROLES))

    def test_each_role_has_its_tools_and_floor(self):
        for name, role in roles.ROLES.items():
            with self.subTest(name):
                self.assertEqual(role.name, name)
                self.assertIsInstance(role.tools, tuple)  # fixed: nobody appends to it
                self.assertTrue(role.tools)
                self.assertEqual(len(role.tools), len(set(role.tools)))
                self.assertIn(role.floor, roles.FLOORS)
                self.assertTrue(role.does.strip())

    def test_each_shipped_role_has_the_designs_tools_and_floor(self):
        for name, (tools, floor) in EXPECTED_ROLES.items():
            with self.subTest(name):
                role = roles.ROLES[name]
                self.assertEqual(tuple(sorted(role.tools)), tuple(sorted(tools)))
                self.assertEqual(role.floor, floor)

    def test_read_only_roles_have_no_file_editing_tools(self):
        self.assertEqual(set(roles.READ_ONLY_DENIED), {"Edit", "NotebookEdit"})
        for name in ("reviewer-angle", "investigate", "analyze"):
            with self.subTest(name):
                self.assertEqual(roles.ROLES[name].floor, "read-only")
        for name, role in roles.ROLES.items():
            if role.floor != "read-only":
                continue
            with self.subTest(name):
                self.assertFalse(set(role.tools) & {"Edit", "NotebookEdit"}, role.tools)

    def test_read_only_roles_write_only_their_result_file(self):
        # The copied secretary agents: the investigator and analyst write their report; the reviewer returns
        # its findings to the loop that started it, so it has no Write.
        for name in ("investigate", "analyze"):
            with self.subTest(name):
                self.assertIn("Write", roles.ROLES[name].tools)
        self.assertNotIn("Write", roles.ROLES["reviewer-angle"].tools)
        self.assertIn("result file", roles.FLOORS["read-only"])
        self.assertIn("reviewer-angle", roles.FLOORS["read-only"])

    def test_code_roles_run_under_the_worker_floor(self):
        for name in ("implement", "pr-loop"):
            with self.subTest(name):
                self.assertEqual(roles.ROLES[name].floor, "worker")
                self.assertTrue({"Edit", "Write", "Bash"} <= set(roles.ROLES[name].tools))

    def test_the_worker_floor_protects_the_users_templates_and_sources(self):
        # Design 3.6: changing a template or a source asks first, so a worker cannot rewrite one.
        for folder in ("templates/", "sources/"):
            with self.subTest(folder):
                self.assertIn(folder, roles.FLOORS["worker"])

    def test_the_role_table_cannot_be_changed(self):
        with self.assertRaises(TypeError):
            roles.ROLES["general"] = roles.ROLES["implement"]  # type: ignore[index]
        with self.assertRaises(AttributeError):
            roles.ROLES["general"].tools = ("Bash",)  # type: ignore[misc]

    def test_a_template_must_name_a_shipped_role(self):
        with self.assertRaises(TemplateError) as e:
            templates.parse(GOOD.replace("role: general", "role: wizard"), "parcel.yaml")
        self.assertIn("line 8", str(e.exception))
        self.assertIn("wizard", str(e.exception))
        self.assertIn("general", str(e.exception))  # the known roles are listed

    def test_every_builtin_role_reference_resolves(self):
        for kind, t in templates.catalog().items():
            for run in t.runs({"id": "c-x", "title": "a title"}):
                with self.subTest(kind=kind, role=run["role"]):
                    self.assertEqual(run["tools"], list(roles.ROLES[run["role"]].tools))
                    self.assertEqual(run["floor"], roles.ROLES[run["role"]].floor)

    def test_every_builtin_brief_fills_from_a_card_with_no_title(self):
        # title is optional (cards.OPTIONAL); a card the store accepts has only the required keys and its id.
        self.assertIn("title", cards.OPTIONAL)
        plain = {line: {"en": "x"} for line in cards.PLAIN_LINES}
        for kind, t in templates.catalog().items():
            with self.subTest(kind):
                card = {"id": "c-1", "kind": kind, "lane": "needs", "feed": "f", "links": [], "pri": "P2",
                        "origin": "source", "plain": plain}
                self.assertEqual(set(card) - {"id"}, set(cards.REQUIRED))
                runs = t.runs(card)
                self.assertEqual([r["role"] for r in runs], [e["role"] for e in t.data["roles"]])
                for run in runs:
                    self.assertIn("c-1", run["brief"])


class UserTemplatesTest(TempFolder):
    """Criterion 3: <data>/templates/*.yaml wins on the same kind, and adds new kinds."""

    def test_a_user_file_replaces_the_builtin_kind(self):
        shutil.copy(FIXTURES / "question.yaml", self.data / "templates" / "question.yaml")
        cat = templates.catalog(self.data)
        q = cat["question"]
        self.assertEqual(q.origin, "user")
        self.assertEqual(q.path, self.data / "templates" / "question.yaml")
        self.assertEqual(q.data["title"], "Question (mine)")
        self.assertEqual([b["id"] for b in q.data["buttons"]], ["remind", "sent"])
        self.assertTrue(q.replaces_builtin)
        self.assertEqual(set(cat), BUILTIN_KINDS)  # replaced, nothing added
        self.assertEqual(templates.catalog()["question"].origin, "builtin")  # the plugin's file is untouched

    def test_an_unreadable_templates_folder_is_a_template_error(self):
        folder = self.data / "templates"
        shutil.rmtree(folder)
        folder.write_text("not a folder\n", encoding="utf-8")
        with self.assertRaises(TemplateError) as e:
            templates.catalog(self.data)
        self.assertIn(str(folder), str(e.exception))
        self.assertIn("cannot read", str(e.exception))

    def test_a_new_kind_is_added(self):
        shutil.copy(FIXTURES / "parcel.yaml", self.data / "templates" / "parcel.yaml")
        cat = templates.catalog(self.data)
        self.assertEqual(set(cat), BUILTIN_KINDS | {"parcel"})
        self.assertEqual(cat["parcel"].origin, "user")
        self.assertFalse(cat["parcel"].replaces_builtin)
        self.assertEqual(cat["code"].origin, "builtin")

    def test_no_templates_folder_means_builtins_only(self):
        shutil.rmtree(self.data / "templates")
        self.assertEqual(set(templates.catalog(self.data)), BUILTIN_KINDS)
        self.assertFalse((self.data / "templates").exists())  # reading never creates anything

    def test_other_files_are_ignored(self):
        self.write("notes.txt", "kind: nope\n", self.data / "templates")
        self.write(".parcel.yaml", "not: a template\n", self.data / "templates")
        (self.data / "templates" / "sub.yaml").mkdir()
        self.assertEqual(set(templates.catalog(self.data)), BUILTIN_KINDS)

    def test_a_bad_user_file_fails_the_load_naming_it(self):
        bad = self.write("parcel.yaml", GOOD + "colour: red\n", self.data / "templates")
        with self.assertRaises(TemplateError) as e:
            templates.catalog(self.data)
        self.assertIn(str(bad), str(e.exception))
        self.assertIn("colour", str(e.exception))

    def test_a_broken_link_is_reported_not_skipped(self):
        link = self.data / "templates" / "question.yaml"
        link.symlink_to(self.tmp / "missing.yaml")
        with self.assertRaises(TemplateError) as e:
            templates.catalog(self.data)
        self.assertIn(str(link), str(e.exception))
        self.assertIn("cannot read", str(e.exception))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores folder modes")
    def test_a_folder_that_cannot_be_searched_is_a_template_error(self):
        folder = self.data / "templates"
        shutil.copy(templates.BUILTIN_DIR / "question.yaml", folder / "question.yaml")
        folder.chmod(0o444)
        self.addCleanup(folder.chmod, 0o755)  # runs before the temp folder is removed
        with self.assertRaises(TemplateError) as e:
            templates.catalog(self.data)
        self.assertIn(str(folder), str(e.exception))
        self.assertIn("cannot read", str(e.exception))

    def test_a_templates_folder_link_to_a_missing_folder_is_a_template_error(self):
        folder = self.data / "templates"
        folder.rmdir()
        target = self.tmp / "mine"
        target.mkdir()
        shutil.copy(FIXTURES / "parcel.yaml", target / "parcel.yaml")
        folder.symlink_to(target)
        self.assertIn("parcel", templates.catalog(self.data))
        target.rename(self.tmp / "moved")  # the link now points at nothing
        outside = self.write("question.yaml", (FIXTURES / "question.yaml").read_text())
        runs = {"catalog": lambda: templates.catalog(self.data), "check": lambda: templates.check(outside, self.data)}
        for name, run in runs.items():
            with self.subTest(name):
                with self.assertRaises(TemplateError) as e:
                    run()
                self.assertIn(str(folder), str(e.exception))
                self.assertIn("links to a folder that does not exist", str(e.exception))

    def test_a_user_child_button_may_name_a_user_kind(self):
        self.write("parcel.yaml", GOOD + "", self.data / "templates")
        self.write("errand.yaml", (templates.BUILTIN_DIR / "errand.yaml").read_text().replace(
            "child: step", "child: parcel"), self.data / "templates")
        self.assertEqual(templates.catalog(self.data)["errand"].origin, "user")

    def test_a_child_button_naming_no_kind_is_refused(self):
        self.write("parcel.yaml", GOOD.replace("lane: done", "child: crate"), self.data / "templates")
        with self.assertRaises(TemplateError) as e:
            templates.catalog(self.data)
        self.assertIn("line 6", str(e.exception))
        self.assertIn("crate", str(e.exception))

    def test_a_user_template_keeps_the_roles_shipped_tools(self):
        shutil.copy(FIXTURES / "parcel.yaml", self.data / "templates" / "parcel.yaml")
        runs = templates.catalog(self.data)["parcel"].runs({"tracking": "1Z999"})
        self.assertEqual(runs, [{"role": "general", "when": "delivery day 08:00",
                                 "brief": "Check the carrier page for 1Z999. If it says out for delivery, nudge me.",
                                 "tools": list(roles.ROLES["general"].tools),
                                 "floor": roles.ROLES["general"].floor}])


# Design 12: the keys that would widen a role, copied from the design, not from templates.py.
WIDEN_KEYS = ("tools", "tool", "allowed_tools", "disallowed_tools", "permissions", "permission", "permission_mode",
              "settings", "setting_sources", "mcp", "mcp_servers", "mcp_config", "hooks", "env", "add_dir",
              "add_dirs", "dangerously_skip_permissions", "floor", "cwd", "folder", "sandbox", "agents",
              "additional_directories", "default_mode")

# Criterion 4: every key that would widen a role, at the top level and inside a roles entry.
WIDEN_CASES = [
    ("tools at the top", GOOD + "tools: [Bash]\n", "tools", 10),
    ("permissions at the top", GOOD + "permissions: {allow: [Bash]}\n", "permissions", 10),
    ("allowed_tools at the top", GOOD + "allowed_tools: [WebFetch]\n", "allowed_tools", 10),
    ("allowedTools, CLI spelling", GOOD + "allowedTools: [WebFetch]\n", "allowedTools", 10),
    ("allowed-tools, dash spelling", GOOD + "allowed-tools: [WebFetch]\n", "allowed-tools", 10),
    ("settings at the top", GOOD + "settings: {x: 1}\n", "settings", 10),
    ("mcp_servers at the top", GOOD + "mcp_servers: [slack]\n", "mcp_servers", 10),
    ("permission_mode at the top", GOOD + "permission_mode: bypassPermissions\n", "permission_mode", 10),
    ("hooks at the top", GOOD + "hooks: {}\n", "hooks", 10),
    ("env at the top", GOOD + "env: {A: b}\n", "env", 10),
    ("add_dir at the top", GOOD + "add_dir: /\n", "add_dir", 10),
    ("a working folder at the top", GOOD + "cwd: /\n", "cwd", 10),
    ("floor at the top", GOOD + "floor: worker\n", "floor", 10),
    ("TOOLS in capitals", GOOD + "TOOLS: [Bash]\n", "TOOLS", 10),
    ("tools in roles[0]", GOOD + "    tools: [Bash, Write]\n", "tools", 10),
    ("permissions in roles[0]", GOOD + "    permissions: {allow: [Bash]}\n", "permissions", 10),
    ("allowed_tools in roles[0]", GOOD + "    allowed_tools: [Edit]\n", "allowed_tools", 10),
    ("settings in roles[0]", GOOD + "    settings: {}\n", "settings", 10),
    ("dangerously_skip_permissions in roles[0]", GOOD + "    dangerously_skip_permissions: true\n",
     "dangerously_skip_permissions", 10),
    ("tools in roles[1]", GOOD + "  - role: implement\n    brief: x\n    tools: [Bash]\n", "tools", 12),
    ("tools in a button", GOOD.replace("    lane: done\n", "    lane: done\n    tools: [Bash]\n"), "tools", 7),
    ("tools in a flow map", GOOD.replace("roles:\n  - role: general\n    brief: \"Check {tracking}.\"\n",
                                         "roles: [{role: general, brief: x, tools: [Bash]}]\n"), "tools", 7),
    ("a widening key beside other errors", GOOD + "colour: red\ntools: [Bash]\n", "tools", 11),
    ("defaultMode at the top", GOOD + "defaultMode: bypassPermissions\n", "defaultMode", 10),
    ("additionalDirectories in roles[0]", GOOD + "    additionalDirectories: [/]\n", "additionalDirectories", 10),
    ("allowed tools, space spelling", GOOD + "allowed tools: [x]\n", "allowed tools", 10),
]


class NoAddedToolsTest(TempFolder):
    def test_widening_keys_are_refused_naming_the_file_and_key(self):
        for name, text, key, line in WIDEN_CASES:
            with self.subTest(name):
                path = self.write("parcel.yaml", text)
                with self.assertRaises(TemplateWidenError) as e:
                    templates.load_file(path)
                msg = str(e.exception)
                self.assertTrue(msg.startswith(f"{path}: line {line}: "), msg)
                self.assertIn(f"'{key}'", msg)
                self.assertEqual((e.exception.file, e.exception.key), (str(path), key))
                self.assertIsInstance(e.exception, TemplateError)

    def test_every_widening_key_is_refused_at_the_top_and_in_a_role(self):
        for k in dict.fromkeys(WIDEN_KEYS + tuple(templates._WIDEN)):
            for where, text in (("top", GOOD + f"{k}: x\n"), ("roles[0]", GOOD + f"    {k}: x\n")):
                with self.subTest(key=k, where=where):
                    with self.assertRaises(TemplateWidenError) as e:
                        templates.parse(text, "parcel.yaml")
                    self.assertEqual(e.exception.key, k)

    def test_a_user_file_with_tools_is_refused_and_the_builtin_stays(self):
        shipped = roles.ROLES["investigate"].tools
        text = (templates.BUILTIN_DIR / "question.yaml").read_text() + "tools: [Bash, Write, Edit]\n"
        path = self.write("question.yaml", text, self.data / "templates")
        with self.assertRaises(TemplateWidenError) as e:
            templates.catalog(self.data)
        self.assertEqual(e.exception.file, str(path))
        self.assertEqual(roles.ROLES["investigate"].tools, shipped)
        self.assertNotIn("Edit", shipped)  # the file asked for Edit; the role still does not have it
        path.unlink()
        runs = templates.catalog(self.data)["question"].runs({"id": "c-1", "title": "t"})
        self.assertEqual([r["tools"] for r in runs], [list(shipped)])

    def test_check_refuses_a_widening_template_too(self):
        path = self.write("parcel.yaml", GOOD + "    tools: [Bash]\n")
        with self.assertRaises(TemplateWidenError):
            templates.check(path, self.data)
        self.assertEqual(list((self.data / "templates").iterdir()), [])

    def test_the_widening_error_is_one_class(self):
        self.assertTrue(issubclass(TemplateWidenError, TemplateError))
        self.assertTrue(issubclass(TemplateError, ConfigError))  # ap2 prints it as one `ap2: ...` line


# Criterion 5: bad files, each refused naming the file and the line or the key.
BAD_CASES = [
    ("unknown top-level key", GOOD + "colour: red\n", "line 10", "unknown key 'colour'"),
    ("unknown role", GOOD.replace("role: general", "role: wizard"), "line 8", "unknown role 'wizard'"),
    ("a view entry that is not a block", GOOD.replace("[plan, activity]", "[plan, sidebar]"), "line 2",
     "unknown block 'sidebar'"),
    ("a view entry that is not text", GOOD.replace("[plan, activity]", "[plan, [a]]"), "line 2",
     "view entries must be block names"),
    ("a view block twice", GOOD.replace("[plan, activity]", "[plan, plan]"), "line 2", "'plan' twice"),
    ("an empty view", GOOD.replace("[plan, activity]", "[]"), "line 2", "view"),
    ("a button with no action", GOOD.replace("    lane: done\n", ""), "line 4", "no action"),
    ("a button with two actions", GOOD.replace("    lane: done\n", "    lane: done\n    restore: last\n"), "line 4",
     "more than one action"),
    ("a button with an unknown lane", GOOD.replace("lane: done", "lane: later"), "line 6", "lane"),
    ("a button with an unknown key", GOOD.replace("    lane: done\n", "    lane: done\n    color: red\n"),
     "line 7", "unknown key 'color'"),
    ("a button with no label", GOOD.replace("    label: It arrived\n", ""), "line 4", "label"),
    ("a button with no id", GOOD.replace("  - id: arrived\n    label", "  - label"), "line 4", "id"),
    ("two buttons with one id", GOOD.replace("roles:", "  - id: arrived\n    label: Again\n    lane: needs\nroles:"),
     "line 7", "'arrived' twice"),
    ("a restore that is not last", GOOD.replace("lane: done", "restore: first"), "line 6", "restore"),
    ("kind is not the file name", GOOD.replace("kind: parcel", "kind: crate"), "line 1",
     "does not match the file name"),
    ("no kind", GOOD.replace("kind: parcel\n", ""), "parcel.yaml: ", "missing key 'kind'"),
    ("no view", GOOD.replace("view: [plan, activity]\n", ""), "parcel.yaml: ", "missing key 'view'"),
    ("no buttons", GOOD.replace("buttons:\n  - id: arrived\n    label: It arrived\n    lane: done\n", ""),
     "parcel.yaml: ", "missing key 'buttons'"),
    ("no roles", GOOD.split("roles:")[0], "parcel.yaml: ", "missing key 'roles'"),
    ("a role with no brief", GOOD.replace("    brief: \"Check {tracking}.\"\n", ""), "line 8", "brief"),
    ("a role with no role", GOOD.replace("  - role: general\n    brief", "  - brief"), "line 8",
     "roles[0] has no role"),
    ("a role entry with an unknown key", GOOD + "    after: implement\n", "line 10", "unknown key 'after'"),
    ("an empty brief", GOOD.replace("\"Check {tracking}.\"", "\"  \""), "line 9", "brief"),
    ("a brief with a lone brace", GOOD.replace("{tracking}.", "{tracking."), "line 9", "brace"),
    ("a brief with a bad placeholder", GOOD.replace("{tracking}", "{0}"), "line 9", "{0}"),
    ("roles is not a list", GOOD.split("roles:")[0] + "roles: 3\n", "line 7", "roles must be a list"),
    ("buttons is not a list", GOOD.replace("buttons:\n  - id: arrived\n    label: It arrived\n    lane: done\n",
                                           "buttons: 3\n"), "line 3", "buttons must be a list"),
    ("source other than any or none", GOOD + "source: slack\n", "line 10", "source"),
    ("source any without source_block first", GOOD + "source: any\n", "line 2", "source_block"),
    ("source_block without source any", GOOD.replace("[plan, activity]", "[source_block, plan]"), "line 2",
     "source: any"),
    ("title that is not text", GOOD + "title: 3\n", "line 10", "title"),
    ("a kind with capitals", GOOD.replace("kind: parcel", "kind: Parcel"), "line 1", "kind must be lowercase"),
    ("not the YAML subset", GOOD + "x: &a 1\n", "line 10", "anchors"),
    ("an empty file", "", "parcel.yaml: ", "missing key 'kind'"),
    ("a kind that is not text", GOOD.replace("kind: parcel", "kind: 3"), "line 1", "kind must be"),
    ("a view that is not a list", GOOD.replace("[plan, activity]", "3"), "line 2", "view must be a non-empty list"),
    ("a button that is not a map", GOOD.replace("buttons:\n  - id: arrived\n    label: It arrived\n    lane: done\n",
                                                "buttons: [3]\n"), "line 3", "buttons[0] must be a map"),
    ("a button id that is not text", GOOD.replace("id: arrived", "id: [a]"), "line 4", "button id must be"),
    ("a button id that breaks the pattern", GOOD.replace("id: arrived", "id: Remind Me!"), "line 4",
     "button id must be"),
    ("an empty label", GOOD.replace("label: It arrived", 'label: ""'), "line 5", "label must be"),
    ("a child that is not text", GOOD.replace("lane: done", "child: [step]"), "line 6",
     "child names no template kind"),
    ("a role entry that is not a map", GOOD.split("roles:")[0] + "roles: [3]\n", "line 7", "roles[0] must be a map"),
    ("a role that is not text", GOOD.replace("role: general", "role: [general]"), "line 8", "unknown role"),
    ("a blank when", GOOD.replace("  - role: general\n", '  - role: general\n    when: "  "\n'), "line 9",
     "when must be"),
    ("a placeholder name with a dash", GOOD.replace("{tracking}", "{tracking-id}"), "line 9",
     "{tracking-id} is not a card field name"),
]


class ValidationTest(TempFolder):
    def test_bad_files_are_refused_with_the_file_and_line_or_key(self):
        for name, text, where, why in BAD_CASES:
            with self.subTest(name):
                path = self.write("parcel.yaml", text)
                with self.assertRaises(TemplateError) as e:
                    templates.load_file(path)
                msg = str(e.exception)
                self.assertNotIsInstance(e.exception, TemplateWidenError)
                self.assertTrue(msg.startswith(f"{path}: "), msg)
                self.assertIn(where, msg)
                self.assertIn(why, msg)

    def test_a_kind_with_capitals_is_refused_even_when_the_file_name_matches(self):
        path = self.write("Parcel.yaml", GOOD.replace("kind: parcel", "kind: Parcel"))
        with self.assertRaises(TemplateError) as e:
            templates.load_file(path)
        msg = str(e.exception)
        self.assertTrue(msg.startswith(f"{path}: line 1: "), msg)
        self.assertIn("kind must be lowercase", msg)

    def test_the_good_file_loads(self):
        t = templates.load_file(self.write("parcel.yaml", GOOD))
        self.assertEqual((t.kind, t.origin), ("parcel", "user"))

    def test_a_file_that_is_not_utf8_is_refused_naming_it(self):
        path = self.tmp / "parcel.yaml"
        path.write_bytes(b"kind: parcel\ntitle: \xff\n")
        with self.assertRaises(TemplateError) as e:
            templates.load_file(path)
        self.assertIn(str(path), str(e.exception))

    def test_a_missing_file_is_refused_naming_it(self):
        with self.assertRaises(TemplateError) as e:
            templates.load_file(self.tmp / "nope.yaml")
        self.assertIn("nope.yaml", str(e.exception))

    def test_a_file_that_is_not_yaml_named(self):
        with self.assertRaises(TemplateError) as e:
            templates.load_file(self.write("parcel.yml", GOOD))
        self.assertIn(".yaml", str(e.exception))


class BriefTest(unittest.TestCase):
    """Criterion 6: {placeholder} fields are filled from the card; a missing one is an error naming it."""

    def test_placeholders_are_filled_from_the_card(self):
        self.assertEqual(templates.fill_brief("Check the carrier page for {tracking}.", {"tracking": "1Z999"}),
                         "Check the carrier page for 1Z999.")

    def test_dotted_names_and_numbers(self):
        card = {"id": "c-1", "source": {"place": "#deploys"}, "pr": {"number": 42}}
        self.assertEqual(templates.fill_brief("{id} in {source.place}: PR {pr.number}", card),
                         "c-1 in #deploys: PR 42")

    def test_doubled_braces_are_literal(self):
        self.assertEqual(templates.fill_brief("{{not}} {x}", {"x": "y"}), "{not} y")

    def test_a_missing_placeholder_is_an_error_naming_it(self):
        with self.assertRaises(BriefError) as e:
            templates.fill_brief("Check {tracking} for {who}.", {"who": "me"})
        self.assertIn("{tracking}", str(e.exception))
        self.assertEqual(e.exception.missing, ["tracking"])

    def test_a_dotted_name_inside_text_is_missing(self):
        with self.assertRaises(BriefError) as e:
            templates.fill_brief("{title.a}", {"title": "a title"})
        self.assertEqual(e.exception.missing, ["title.a"])

    def test_every_missing_placeholder_is_named(self):
        with self.assertRaises(BriefError) as e:
            templates.fill_brief("{a} {b} {c.d}", {"b": "x", "c": {}})
        self.assertEqual(e.exception.missing, ["a", "c.d"])

    def test_an_empty_value_is_never_filled(self):
        for value in ("", "   ", None):
            with self.subTest(repr(value)):
                with self.assertRaises(BriefError) as e:
                    templates.fill_brief("Check {tracking}.", {"tracking": value})
                self.assertEqual(e.exception.missing, ["tracking"])

    def test_a_value_that_is_not_text_is_an_error(self):
        for value in (True, ["a"], {"a": 1}):
            with self.subTest(repr(value)):
                with self.assertRaises(BriefError) as e:
                    templates.fill_brief("Check {tracking}.", {"tracking": value})
                self.assertIn("{tracking}", str(e.exception))

    def test_a_filled_value_is_not_read_again(self):
        self.assertEqual(templates.fill_brief("{a}", {"a": "{b}", "b": "no"}), "{b}")

    def test_runs_name_the_template_and_role_on_a_missing_field(self):
        t = templates.parse(GOOD, "parcel.yaml")
        tpl = templates.Template(kind="parcel", data=t, path=Path("parcel.yaml"), origin="user")
        with self.assertRaises(BriefError) as e:
            tpl.runs({"title": "x"})
        msg = str(e.exception)
        self.assertIn("parcel", msg)
        self.assertIn("general", msg)
        self.assertIn("{tracking}", msg)

    def test_the_design_example_fills_from_a_card(self):
        t = templates.load_file(FIXTURES / "parcel.yaml")
        runs = t.runs({"kind": "parcel", "tracking": "1Z999AA10123456784"})
        self.assertEqual(runs[0]["brief"],
                         "Check the carrier page for 1Z999AA10123456784. If it says out for delivery, nudge me.")


class CheckTest(TempFolder):
    def test_check_validates_without_installing(self):
        path = self.write("parcel.yaml", (FIXTURES / "parcel.yaml").read_text())
        out = templates.check(path, self.data)
        self.assertEqual(out, {"ok": True, "kind": "parcel", "file": str(path), "replaces": None,
                               "installed": False})
        self.assertEqual(list((self.data / "templates").iterdir()), [])
        self.assertNotIn("parcel", templates.catalog(self.data))

    def test_check_says_what_it_would_replace(self):
        path = self.write("question.yaml", (FIXTURES / "question.yaml").read_text())
        self.assertEqual(templates.check(path, self.data)["replaces"], "builtin")
        shutil.copy(path, self.data / "templates" / "question.yaml")
        self.assertEqual(templates.check(path, self.data)["replaces"], "user")
        out = templates.check(self.data / "templates" / "question.yaml", self.data)
        self.assertEqual((out["replaces"], out["installed"]), ("builtin", True))

    def test_check_knows_the_installed_file_by_another_spelling(self):
        installed = self.data / "templates" / "question.yaml"
        shutil.copy(FIXTURES / "question.yaml", installed)
        old_cwd = os.getcwd()
        self.addCleanup(os.chdir, old_cwd)
        os.chdir(self.data / "templates")
        out = templates.check("question.yaml", self.data)  # relative to the templates folder
        self.assertEqual((out["replaces"], out["installed"]), ("builtin", True))
        os.chdir(old_cwd)
        link_dir = self.tmp / "links"
        link_dir.mkdir()
        link = link_dir / "question.yaml"
        link.symlink_to(installed)
        out = templates.check(link, self.data)  # a symlink to the installed file
        self.assertEqual((out["replaces"], out["installed"]), ("builtin", True))

    def test_check_ignores_other_bad_user_files(self):
        self.write("errand.yaml", "kind: errand\ncolour: red\n", self.data / "templates")
        path = self.write("parcel.yaml", GOOD)
        self.assertTrue(templates.check(path, self.data)["ok"])

    def test_check_does_not_stat_other_user_entries(self):
        folder = self.data / "templates"
        os.symlink("other.yaml", folder / "other.yaml")  # a link to itself: stat fails with a loop error
        path = self.write("parcel.yaml", GOOD)
        self.assertTrue(templates.check(path, self.data)["ok"])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores folder modes")
    def test_check_with_a_templates_folder_that_cannot_be_searched_is_a_template_error(self):
        folder = self.data / "templates"  # empty, so only the installed-file check touches it
        path = self.write("question.yaml", (FIXTURES / "question.yaml").read_text())
        folder.chmod(0o444)
        self.addCleanup(folder.chmod, 0o755)  # runs before the temp folder is removed
        with self.assertRaises(TemplateError) as e:
            templates.check(path, self.data)
        self.assertIn(str(folder / "question.yaml"), str(e.exception))
        self.assertIn("cannot read", str(e.exception))

    def test_check_accepts_a_child_of_its_own_or_a_user_kind(self):
        self.write("crate.yaml", "anything\n", self.data / "templates")
        path = self.write("parcel.yaml", GOOD.replace("lane: done", "child: crate"))
        self.assertTrue(templates.check(path, self.data)["ok"])
        path = self.write("parcel.yaml", GOOD.replace("lane: done", "child: parcel"))
        self.assertTrue(templates.check(path, self.data)["ok"])
        path = self.write("parcel.yaml", GOOD.replace("lane: done", "child: barrel"))
        with self.assertRaises(TemplateError):
            templates.check(path, self.data)

    def test_check_does_not_take_a_folder_for_a_kind(self):
        path = self.write("parcel.yaml", GOOD.replace("lane: done", "child: crate"))
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        crate = self.data / "templates" / "crate.yaml"
        rows = [("a folder named crate.yaml", crate.mkdir, crate.rmdir, False),
                ("a link to a folder", lambda: crate.symlink_to(elsewhere), crate.unlink, False),
                # a link to itself: stat fails with a loop error, so it stays a kind and nothing raises
                ("a link loop", lambda: os.symlink("crate.yaml", crate), crate.unlink, True)]
        for name, make, remove, accepted in rows:
            with self.subTest(name):
                make()
                try:
                    if accepted:
                        self.assertTrue(templates.check(path, self.data)["ok"])
                        continue
                    with self.assertRaisesRegex(TemplateError, "crate"):
                        templates.check(path, self.data)
                    installed = self.data / "templates" / "parcel.yaml"
                    shutil.copy(path, installed)
                    try:
                        with self.assertRaisesRegex(TemplateError, "crate"):  # catalog agrees: no kind
                            templates.catalog(self.data)
                    finally:
                        installed.unlink()
                finally:
                    remove()

    def test_check_knows_an_installed_file_that_is_a_link(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        target = self.write("parcel.yaml", GOOD, elsewhere)
        (self.data / "templates" / "parcel.yaml").symlink_to(target)
        out = templates.check(target, self.data)
        self.assertEqual((out["replaces"], out["installed"]), (None, True))
        copy = self.write("parcel.yaml", GOOD)  # the same text, another file
        out = templates.check(copy, self.data)
        self.assertEqual((out["replaces"], out["installed"]), ("user", False))

    def test_check_does_not_take_a_folder_for_the_installed_file(self):
        (self.data / "templates" / "question.yaml").mkdir()
        path = self.write("question.yaml", (FIXTURES / "question.yaml").read_text())
        out = templates.check(path, self.data)
        self.assertEqual((out["replaces"], out["installed"]), ("builtin", False))


if __name__ == "__main__":
    unittest.main()
