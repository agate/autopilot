"""Review angles (design 6.1): the built-in table, the user's and the repo's layers, kinds, globs and the picker."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests.helpers import ROOT
from assistant import angles, sources, templates
from assistant.angles import AngleError, AngleWidenError
from assistant.config import ConfigError

BUILTIN_NAMES = [
    "Correctness",
    "Data correctness & backfill", "Performance & locking",
    "Blast radius & rollback", "Security & least privilege",
    "API contract & backward compatibility", "Failure modes & observability",
    "Portability", "Security of secrets & permissions",
    "Cross-platform parity", "Accessibility & UX",
    "Test value", "Flakiness & isolation",
    "Accuracy", "Reader clarity",
]

# The user's file of criterion 2: Test value replaced as a whole angle.
USER_TEST_VALUE = """\
- name: Test value
  question: "Would a broken handler still pass these tests?"
  when: { kinds: [tests] }
  brief: "Break the code on purpose in your head and say which test catches it."
"""

# The repo's file of criterion 2 (design 6.1's example).
REPO_FILE = """\
# <repo>/.autopilot/review.yaml
- name: Payments safety
  question: "Can this change charge a customer twice, or not at all?"
  when: { paths: ["billing/**", "payments/**"] }
  brief: "Read every caller of the changed charge functions. Idempotency keys must survive retries."
- name: Accessibility & UX
  off: true            # this repo has no UI
"""

GIT = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false",
       "-c", "core.hooksPath=/dev/null", "-c", "init.defaultBranch=main"]


class AngleCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-angles-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()

    # --- helpers --------------------------------------------------------------------------------------------

    def user(self, text: str) -> Path:
        path = self.data / "review" / "angles.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
        return path

    def git(self, *args) -> str:
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        return subprocess.run(GIT + ["-C", str(self.repo)] + list(args), check=True, capture_output=True,
                              text=True, env=env).stdout.strip()

    def commit_repo_file(self, text: str) -> str:
        """Commit `.autopilot/review.yaml` holding `text` in the temp repo; return the commit."""
        if not (self.repo / ".git").exists():
            self.git("init", "-q")
        path = self.repo / ".autopilot" / "review.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "review angles")
        return self.git("rev-parse", "HEAD")

    def commit_without_file(self) -> str:
        self.git("init", "-q")
        (self.repo / "README.md").write_text("hello\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "first")
        return self.git("rev-parse", "HEAD")

    def load(self, **kw):
        return angles.load(self.data, **kw)

    def by_name(self, table):
        return {a.name: a for a in table.angles}


class BuiltinTableTest(AngleCase):
    """Criterion 1: the built-in table loads with exactly its 15 angles."""

    def test_builtin_file_is_next_to_sources(self):
        self.assertEqual(angles.PLUGIN_FILE, ROOT / "review" / "angles.yaml")
        self.assertEqual(angles.PLUGIN_FILE.parent.parent, sources.PLUGIN_DIR.parent)

    def test_fifteen_names_in_order(self):
        table = self.load()
        self.assertEqual([a.name for a in table.angles], BUILTIN_NAMES)
        for a in table.angles:
            with self.subTest(angle=a.name):
                self.assertTrue(a.question.strip())
                self.assertTrue(a.brief.strip())
                self.assertEqual(a.origin, "built-in")
                self.assertEqual(Path(a.file), angles.PLUGIN_FILE)
        self.assertEqual(table.get("Correctness").when, "always")
        self.assertEqual(table.unknown_off, [])

    def test_builtin_kinds(self):
        want = {"Data correctness & backfill": "data", "Performance & locking": "data",
                "Blast radius & rollback": "infra", "Security & least privilege": "infra",
                "API contract & backward compatibility": "api", "Failure modes & observability": "api",
                "Portability": "ci", "Security of secrets & permissions": "ci",
                "Cross-platform parity": "ui", "Accessibility & UX": "ui",
                "Test value": "tests", "Flakiness & isolation": "tests",
                "Accuracy": "docs", "Reader clarity": "docs"}
        table = self.load()
        for name, kind in want.items():
            with self.subTest(angle=name):
                self.assertEqual(table.get(name).when, {"kinds": [kind]})

    def test_folded_harness_row(self):
        # v1's "A tool or harness other teams use" row folds into these two.
        table = self.load()
        self.assertIn("who else runs", table.get("Blast radius & rollback").question.lower())
        self.assertIn("timing", table.get("Flakiness & isolation").question)

    def test_errors_are_config_errors(self):
        self.assertTrue(issubclass(AngleError, ConfigError))
        self.assertTrue(issubclass(AngleWidenError, AngleError))


class LayerTest(AngleCase):
    """Criterion 2: the user's and the repo's layers override by name; the repo file comes from the commit."""

    def test_user_replaces_and_repo_adds_and_turns_off(self):
        user = self.user(USER_TEST_VALUE)
        commit = self.commit_repo_file(REPO_FILE)
        table = self.load(repo=self.repo, commit=commit)
        got = self.by_name(table)
        tv = got["Test value"]
        self.assertEqual(tv.question, "Would a broken handler still pass these tests?")
        self.assertEqual(tv.brief, "Break the code on purpose in your head and say which test catches it.")
        self.assertEqual(tv.when, {"kinds": ["tests"]})
        self.assertEqual(tv.origin, "user")
        self.assertEqual(Path(tv.file), user)
        pay = got["Payments safety"]
        self.assertEqual(pay.origin, "repo")
        self.assertEqual(pay.when, {"paths": ["billing/**", "payments/**"]})
        self.assertIn(".autopilot/review.yaml", pay.file)
        self.assertNotIn("Accessibility & UX", got)
        # A replaced angle keeps its place; a new one comes last.
        names = [a.name for a in table.angles]
        self.assertEqual(names.index("Test value"), BUILTIN_NAMES.index("Test value") - 1)
        self.assertEqual(names[-1], "Payments safety")

    def test_repo_file_is_read_from_the_commit(self):
        commit = self.commit_repo_file(REPO_FILE)
        # A different, uncommitted file in the working tree is ignored.
        (self.repo / ".autopilot" / "review.yaml").write_text(
            '- name: Working tree only\n  question: "q?"\n  when: always\n  brief: "b"\n')
        got = self.by_name(self.load(repo=self.repo, commit=commit))
        self.assertIn("Payments safety", got)
        self.assertNotIn("Working tree only", got)
        self.assertNotIn("Accessibility & UX", got)

    def test_later_commit_is_not_used(self):
        first = self.commit_repo_file(REPO_FILE)
        self.commit_repo_file('- name: Later\n  question: "q?"\n  when: always\n  brief: "b"\n')
        got = self.by_name(self.load(repo=self.repo, commit=first))
        self.assertIn("Payments safety", got)
        self.assertNotIn("Later", got)

    def test_missing_repo_file_is_no_layer(self):
        commit = self.commit_without_file()
        table = self.load(repo=self.repo, commit=commit)
        self.assertEqual([a.name for a in table.angles], BUILTIN_NAMES)

    def test_missing_user_file_is_no_layer(self):
        self.assertEqual([a.name for a in self.load().angles], BUILTIN_NAMES)

    def test_repo_wins_over_user(self):
        self.user(USER_TEST_VALUE)
        commit = self.commit_repo_file('- name: Test value\n  question: "Repo q?"\n  when: always\n'
                                       '  brief: "Repo brief."\n')
        tv = self.load(repo=self.repo, commit=commit).get("Test value")
        self.assertEqual((tv.question, tv.origin), ("Repo q?", "repo"))

    def test_git_failures_are_angle_errors(self):
        commit = self.commit_without_file()
        cases = {"not a repo": (self.tmp / "nowhere", commit),
                 "unknown commit": (self.repo, "0" * 40),
                 "an option": (self.repo, "--output=/tmp/x"),
                 "commit without repo": (None, commit),
                 "repo without commit": (self.repo, None)}
        for label, (repo, rev) in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(AngleError):
                    angles.load(self.data, repo=repo, commit=rev)

    def test_repo_path_that_is_a_folder_is_refused(self):
        self.git("init", "-q")
        (self.repo / ".autopilot" / "review.yaml").mkdir(parents=True)
        (self.repo / ".autopilot" / "review.yaml" / "x").write_text("x\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "folder")
        with self.assertRaises(AngleError) as e:
            self.load(repo=self.repo, commit=self.git("rev-parse", "HEAD"))
        self.assertIn(".autopilot/review.yaml", str(e.exception))

    def test_repo_path_that_is_a_link_is_refused(self):
        self.git("init", "-q")
        (self.repo / "angles.yaml").write_text('- name: A\n  question: "q?"\n  when: always\n  brief: "b"\n')
        (self.repo / ".autopilot").mkdir()
        os.symlink("../angles.yaml", self.repo / ".autopilot" / "review.yaml")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "link")
        with self.assertRaises(AngleError) as e:
            self.load(repo=self.repo, commit=self.git("rev-parse", "HEAD"))
        self.assertIn("not a regular file", str(e.exception))

    def test_repo_file_past_the_cap_is_refused(self):
        big = '- name: Big\n  question: "q?"\n  when: always\n  brief: "%s"\n' % ("x" * sources.MAX_SOURCE)
        commit = self.commit_repo_file(big)
        with self.assertRaises(AngleError) as e:
            self.load(repo=self.repo, commit=commit)
        self.assertIn(".autopilot/review.yaml", str(e.exception))
        self.assertIn(str(sources.MAX_SOURCE), str(e.exception))


class ExtendsOffTest(AngleCase):
    """Criterion 3: extends and off."""

    def test_cases(self):
        base = angles.load(self.data).get("Failure modes & observability")

        with self.subTest(case="extends with only brief"):
            path = self.user('- extends: Failure modes & observability\n  brief: "Read the retry loop first."\n')
            fm = self.load().get("Failure modes & observability")
            self.assertEqual(fm.brief, "Read the retry loop first.")
            self.assertEqual(fm.question, base.question)
            self.assertEqual(fm.when, base.when)
            self.assertEqual(fm.origin, "user, extends Failure modes & observability")
            self.assertEqual(Path(fm.file), path)

        with self.subTest(case="extends with the name written"):
            self.user('- name: Failure modes & observability\n  extends: Failure modes & observability\n'
                      '  brief: "New brief."\n')
            fm = self.load().get("Failure modes & observability")
            self.assertEqual((fm.brief, fm.question), ("New brief.", base.question))

        with self.subTest(case="extends under a new name keeps the base"):
            self.user('- name: Queue failures\n  extends: Failure modes & observability\n'
                      '  when: { paths: ["queue/**"] }\n')
            table = self.load()
            q = table.get("Queue failures")
            self.assertEqual((q.question, q.when), (base.question, {"paths": ["queue/**"]}))
            self.assertEqual(q.origin, "user, extends Failure modes & observability")
            self.assertIsNotNone(table.get("Failure modes & observability"))

        with self.subTest(case="repo extends what the user left"):
            self.user('- extends: Failure modes & observability\n  brief: "User brief."\n')
            commit = self.commit_repo_file('- extends: Failure modes & observability\n'
                                           '  question: "Repo question?"\n')
            fm = self.load(repo=self.repo, commit=commit).get("Failure modes & observability")
            self.assertEqual((fm.brief, fm.question), ("User brief.", "Repo question?"))
            self.assertEqual(fm.origin, "repo, extends Failure modes & observability")

        with self.subTest(case="extends an unknown name"):
            path = self.user('- name: A\n  question: "q?"\n  when: always\n  brief: "b"\n'
                             '- extends: No such angle\n  brief: "b"\n')
            with self.assertRaises(AngleError) as e:
                self.load()
            self.assertIn(str(path), str(e.exception))
            self.assertIn("line 5", str(e.exception))
            self.assertIn("No such angle", str(e.exception))

        with self.subTest(case="extends a name added in the same file"):
            self.user('- name: A\n  question: "q?"\n  when: always\n  brief: "b"\n'
                      '- name: B\n  extends: A\n')
            with self.assertRaises(AngleError):
                self.load()

        with self.subTest(case="off an unknown name"):
            path = self.user('- name: Gone angle\n  off: true\n')
            table = self.load()
            self.assertEqual([a.name for a in table.angles], BUILTIN_NAMES)
            self.assertEqual(table.unknown_off, [{"name": "Gone angle", "file": str(path), "line": 1}])

        with self.subTest(case="off on Correctness"):
            path = self.user('- name: Correctness\n  off: true\n')
            with self.assertRaises(AngleError) as e:
                self.load()
            self.assertIn(str(path), str(e.exception))
            self.assertIn("Correctness", str(e.exception))

        with self.subTest(case="Correctness can be extended"):
            self.user('- extends: Correctness\n  brief: "Check against the ticket."\n')
            c = self.load().get("Correctness")
            self.assertEqual((c.brief, c.when), ("Check against the ticket.", "always"))

        with self.subTest(case="Correctness can be replaced"):
            self.user('- name: Correctness\n  question: "Is it right?"\n  when: always\n  brief: "Mine."\n')
            c = self.load().get("Correctness")
            self.assertEqual((c.question, c.origin), ("Is it right?", "user"))

        with self.subTest(case="off with another key"):
            self.user('- name: Portability\n  off: true\n  brief: "b"\n')
            with self.assertRaises(AngleError):
                self.load()

        with self.subTest(case="off false"):
            self.user('- name: Portability\n  off: false\n')
            with self.assertRaises(AngleError):
                self.load()


class RefusalTest(AngleCase):
    """Criterion 4: an angle cannot add tools; bad files raise AngleError naming the file."""

    def test_widen_keys(self):
        for key in ("tools", "allowed-tools", "allowedTools", "permissions", "hooks"):
            for where in ("top", "when"):
                with self.subTest(key=key, where=where):
                    if where == "top":
                        text = (f'- name: A\n  question: "q?"\n  when: always\n  brief: "b"\n'
                                f'  {key}: [Bash]\n')
                        line = 5
                    else:
                        text = (f'- name: A\n  question: "q?"\n  when:\n    paths: ["a/**"]\n'
                                f'    {key}: [Bash]\n  brief: "b"\n')
                        line = 5
                    path = self.user(text)
                    with self.assertRaises(AngleWidenError) as e:
                        self.load()
                    msg = str(e.exception)
                    self.assertIn(str(path), msg)
                    self.assertIn(f"line {line}", msg)
                    self.assertIn(key, msg)
                    self.assertIn("cannot add tools or permissions", msg)

    def test_widen_in_extends_and_in_repo(self):
        with self.subTest(layer="user extends"):
            self.user('- extends: Portability\n  permission_mode: bypass\n')
            with self.assertRaises(AngleWidenError):
                self.load()
        with self.subTest(layer="repo"):
            self.user("")
            commit = self.commit_repo_file('- name: A\n  question: "q?"\n  when: always\n  brief: "b"\n'
                                           '  mcp_servers: {}\n')
            with self.assertRaises(AngleWidenError) as e:
                self.load(repo=self.repo, commit=commit)
            self.assertIn(".autopilot/review.yaml", str(e.exception))
            self.assertIn("line 5", str(e.exception))

    def test_widen_set_is_the_templates_one(self):
        for key in templates.WIDEN:
            with self.subTest(key=key):
                self.user(f'- extends: Portability\n  {key}: x\n')
                with self.assertRaises(AngleWidenError):
                    self.load()

    def test_bad_files(self):
        good = '- name: A\n  question: "q?"\n  when: always\n  brief: "b"\n'
        cases = {
            "unknown key": (good + '  owner: me\n', "line 5", "owner"),
            "duplicate name": (good + good, "line 5", "A"),
            "missing field": ('- name: A\n  question: "q?"\n  when: always\n', "line 1", "brief"),
            "empty name": ('- name: ""\n  question: "q?"\n  when: always\n  brief: "b"\n', "line 1", "name"),
            "empty question": ('- name: A\n  question: " "\n  when: always\n  brief: "b"\n', "line 2", "question"),
            "when a word": ('- name: A\n  question: "q?"\n  when: sometimes\n  brief: "b"\n', "line 3", "when"),
            "when empty map": ('- name: A\n  question: "q?"\n  when: {}\n  brief: "b"\n', "line 3", "when"),
            "when unknown key": ('- name: A\n  question: "q?"\n  when: { files: [a] }\n  brief: "b"\n',
                                 "line 3", "files"),
            "when unknown kind": ('- name: A\n  question: "q?"\n  when: { kinds: [mobile] }\n  brief: "b"\n',
                                  "line 3", "mobile"),
            "when paths not a list": ('- name: A\n  question: "q?"\n  when: { paths: "a/**" }\n  brief: "b"\n',
                                      "line 3", "paths"),
            "when empty paths": ('- name: A\n  question: "q?"\n  when: { paths: [] }\n  brief: "b"\n',
                                 "line 3", "paths"),
            "when absolute glob": ('- name: A\n  question: "q?"\n  when: { paths: ["/a/**"] }\n  brief: "b"\n',
                                   "line 3", "/a/**"),
            "a map, not a list": ('name: A\nquestion: "q?"\nwhen: always\nbrief: "b"\n', None, "list"),
            "a list of words": ('- A\n- B\n', None, "map"),
            "bad yaml": ('- name: A\n  question: "q?\n', "line 2", None),
            "extends not text": ('- extends: [A]\n', "line 1", "extends"),
        }
        for label, (text, line, word) in cases.items():
            with self.subTest(case=label):
                path = self.user(text)
                with self.assertRaises(AngleError) as e:
                    self.load()
                self.assertNotIsInstance(e.exception, AngleWidenError)
                msg = str(e.exception)
                self.assertTrue(msg.startswith(str(path)), msg)
                if line:
                    self.assertIn(line, msg)
                if word:
                    self.assertIn(word, msg)

    def test_file_past_the_cap(self):
        path = self.user('- name: A\n  question: "q?"\n  when: always\n  brief: "%s"\n' % ("x" * sources.MAX_SOURCE))
        with self.assertRaises(AngleError) as e:
            self.load()
        self.assertTrue(str(e.exception).startswith(str(path)))
        self.assertIn(str(sources.MAX_SOURCE), str(e.exception))

    def test_user_path_that_is_a_folder(self):
        (self.data / "review" / "angles.yaml").mkdir(parents=True)
        with self.assertRaises(AngleError) as e:
            self.load()
        self.assertIn("angles.yaml", str(e.exception))

    def test_empty_file_is_no_angles(self):
        self.user("# nothing here yet\n")
        self.assertEqual([a.name for a in self.load().angles], BUILTIN_NAMES)

    def test_builtin_without_correctness_is_refused(self):
        plugin = self.tmp / "angles.yaml"
        plugin.write_text('- name: A\n  question: "q?"\n  when: always\n  brief: "b"\n')
        with self.assertRaises(AngleError) as e:
            angles.load(self.data, plugin_file=plugin)
        self.assertIn("Correctness", str(e.exception))


class KindsGlobsTest(AngleCase):
    """Criterion 5: kinds from paths (secretary's patterns) and globs."""

    def test_each_kind_from_one_path(self):
        examples = {"data": "db/migrations/0001_add.py", "infra": "terraform/main.tf",
                    "api": "proto/user.proto", "ci": ".github/workflows/ci.yml",
                    "ui": "web/components/Button.tsx", "tests": "tests/test_charge.py",
                    "docs": "docs/guide.md"}
        for kind, path in examples.items():
            with self.subTest(kind=kind):
                self.assertEqual(list(angles.kinds([path])), [kind])

    def test_more_kind_paths(self):
        examples = {"data": "schema.sql", "infra": "deploy/prod/app.yaml", "api": "server/main.go",
                    "ci": "scripts/run.sh", "ui": "app/style.css", "tests": "src/charge_test.go",
                    "docs": "README.MD"}
        for kind, path in examples.items():
            with self.subTest(kind=kind, path=path):
                self.assertEqual(list(angles.kinds([path])), [kind])

    def test_tests_and_docs_are_not_code_kinds(self):
        # A test under api/ is a test, not an API change (secretary's rule).
        self.assertEqual(list(angles.kinds(["api/test_handlers.py"])), ["tests"])
        self.assertEqual(list(angles.kinds(["api/README.md"])), ["docs"])

    def test_kinds_order_and_files(self):
        got = angles.kinds(["web/a.tsx", "schema.sql", "web/b.tsx", "lib/x.py"])
        self.assertEqual(list(got), ["data", "ui"])
        self.assertEqual(got["ui"], ["web/a.tsx", "web/b.tsx"])

    def test_globs(self):
        cases = [("billing/*.py", "billing/charge.py", True),
                 ("billing/*.py", "billing/a/b.py", False),
                 ("billing/**", "billing/a/b.py", True),
                 ("billing/**", "billing/charge.py", True),
                 ("billing/**", "billingx/charge.py", False),
                 ("billing/**/*.py", "billing/charge.py", True),
                 ("billing/**/*.py", "billing/a/b/c.py", True),
                 ("**/*.py", "a.py", True),
                 ("**/*.py", "a/b/c.py", True),
                 ("*.py", "a/b.py", False),
                 ("src/?.go", "src/a.go", True),
                 ("src/?.go", "src/ab.go", False),
                 ("src/?.go", "src//.go", False),
                 ("docs/[a].md", "docs/[a].md", True),
                 ("docs/[a].md", "docs/a.md", False),
                 ("a.py", "x/a.py", False),
                 ("**", "any/thing/at/all.txt", True)]
        for pattern, path, want in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertIs(angles.glob_match(pattern, path), want)


class PickerTest(AngleCase):
    """Criterion 6: the picker's order and limits."""

    PR = ["billing/charge.py", "web/components/Button.tsx"]

    def repo_table(self, user: str = ""):
        self.user(user)
        commit = self.commit_repo_file(REPO_FILE)
        return self.load(repo=self.repo, commit=commit)

    def names(self, picked):
        return [p["name"] for p in picked]

    def test_cases(self):
        table = self.repo_table()

        with self.subTest(case="pr loop"):
            picked = angles.pick(table, self.PR, "loop")
            self.assertEqual(self.names(picked), ["Correctness", "Payments safety", "Cross-platform parity"])
            self.assertLessEqual(len(picked), 4)
            self.assertEqual(picked[1]["origin"], "repo")
            self.assertIn("billing/charge.py", picked[1]["why"])
            self.assertIn("ui", picked[2]["why"])
            self.assertIn("web/components/Button.tsx", picked[2]["why"])
            self.assertEqual(picked[0]["why"], "always")

        with self.subTest(case="pr review"):
            picked = angles.pick(table, self.PR, "review", changed_lines=120)
            self.assertEqual(self.names(picked), ["Correctness", "Payments safety", "Cross-platform parity"])

        with self.subTest(case="small docs-only review"):
            picked = angles.pick(table, ["docs/guide.md", "README.md"], "review", changed_lines=10)
            self.assertEqual(self.names(picked), ["Correctness", "Accuracy"])

        with self.subTest(case="large docs-only review"):
            picked = angles.pick(table, ["docs/guide.md"], "review", changed_lines=300)
            self.assertEqual(self.names(picked), ["Correctness", "Accuracy"])

        with self.subTest(case="small code review"):
            picked = angles.pick(table, self.PR, "review", changed_lines=29)
            self.assertEqual(self.names(picked), ["Correctness", "Payments safety"])

        with self.subTest(case="30 lines is not small"):
            picked = angles.pick(table, self.PR, "review", changed_lines=30)
            self.assertEqual(len(picked), 3)

        with self.subTest(case="nothing matches"):
            for mode in ("loop", "review"):
                picked = angles.pick(table, ["lib/util.py"], mode, changed_lines=100)
                self.assertEqual(self.names(picked), ["Correctness", "Failure modes & observability"])
                self.assertEqual(picked[1]["why"], "fallback")

        with self.subTest(case="every picked angle has its fields"):
            for p in angles.pick(table, self.PR, "loop"):
                self.assertEqual(set(p) >= {"name", "question", "brief", "origin", "why"}, True, p)
                for k in ("name", "question", "brief", "origin", "why"):
                    self.assertTrue(p[k].strip(), (p["name"], k))

    def test_layer_priority_when_more_match_than_fit(self):
        user = ('- name: Billing style\n  question: "Does billing code follow the house style?"\n'
                '  when: { paths: ["billing/*.py"] }\n  brief: "b"\n'
                '- name: Second user angle\n  question: "q?"\n  when: always\n  brief: "b"\n')
        table = self.repo_table(user)
        files = self.PR + ["schema.sql"]
        with self.subTest(mode="loop"):
            picked = angles.pick(table, files, "loop")
            self.assertEqual(self.names(picked),
                             ["Correctness", "Payments safety", "Billing style", "Second user angle"])
            self.assertEqual([p["origin"] for p in picked[1:]], ["repo", "user", "user"])
            self.assertEqual(picked[3]["why"], "always")
        with self.subTest(mode="review"):
            picked = angles.pick(table, files, "review", changed_lines=200)
            self.assertEqual(self.names(picked), ["Correctness", "Payments safety", "Billing style"])

    def test_builtin_order_is_riskiest_first(self):
        table = self.load()
        picked = angles.pick(table, ["web/a.tsx", "schema.sql", "proto/x.proto"], "loop")
        self.assertEqual(self.names(picked), ["Correctness", "Data correctness & backfill", "Performance & locking",
                                              "API contract & backward compatibility"])

    def test_fallback_uses_test_value_when_failure_modes_is_off(self):
        self.user('- name: Failure modes & observability\n  off: true\n')
        picked = angles.pick(self.load(), ["lib/util.py"], "loop")
        self.assertEqual(self.names(picked), ["Correctness", "Test value"])
        self.assertEqual(picked[1]["why"], "fallback")

    def test_fallback_is_the_loaded_version(self):
        self.user('- extends: Failure modes & observability\n  brief: "Mine."\n')
        picked = angles.pick(self.load(), ["lib/util.py"], "loop")
        self.assertEqual((picked[1]["brief"], picked[1]["origin"]),
                         ("Mine.", "user, extends Failure modes & observability"))

    def test_correctness_first_whatever_its_when(self):
        self.user('- extends: Correctness\n  when: { kinds: [docs] }\n')
        picked = angles.pick(self.load(), ["lib/util.py"], "loop")
        self.assertEqual(picked[0]["name"], "Correctness")

    def test_bad_mode_or_lines(self):
        table = self.load()
        with self.assertRaises(ValueError):
            angles.pick(table, ["a.py"], "nosuch")
        with self.assertRaises(ValueError):
            angles.pick(table, ["a.py"], "review")

    def test_small_line_constant(self):
        self.assertEqual(angles.SMALL_LINES, 30)


if __name__ == "__main__":
    unittest.main()
