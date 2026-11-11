"""Review angles (design 6.1): the built-in table, the user's and the repo's layers, kinds, globs and the picker."""
from __future__ import annotations

import os
import random
import re
import shutil
import subprocess
import tempfile
import textwrap
import time
import tracemalloc
import unittest
from unittest import mock
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
                 "control characters": (self.repo, "main\x1b]0;owned\x07"),
                 "commit without repo": (None, commit),
                 "repo without commit": (self.repo, None)}
        for label, (repo, rev) in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(AngleError) as e:
                    angles.load(self.data, repo=repo, commit=rev)
                if label in ("an option", "control characters"):
                    self.assertIn("is not a commit name", str(e.exception))
                self.assertNotIn("\x1b", str(e.exception))
                self.assertNotIn("\x07", str(e.exception))

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
        with mock.patch.object(angles, "_git", wraps=angles._git) as git:
            with self.assertRaises(AngleError) as e:
                self.load(repo=self.repo, commit=commit)
        self.assertIn(".autopilot/review.yaml", str(e.exception))
        self.assertIn(str(sources.MAX_SOURCE), str(e.exception))
        # The size listed by ls-tree refuses it; the blob is never read.
        self.assertTrue(git.call_args_list)
        for call in git.call_args_list:
            self.assertNotIn("cat-file", call.args[1])

    def test_repo_file_is_read_from_the_repo_root(self):
        self.git("init", "-q")
        for folder, name in ((self.repo, "Root"), (self.repo / "sub", "Sub")):
            path = folder / ".autopilot" / "review.yaml"
            path.parent.mkdir(parents=True)
            path.write_text(f'- name: {name}\n  question: "q?"\n  when: always\n  brief: "b"\n')
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "two files")
        commit = self.git("rev-parse", "HEAD")
        got = self.by_name(angles.load(self.data, repo=self.repo / "sub", commit=commit))
        self.assertIn("Root", got)
        self.assertNotIn("Sub", got)
        # The file is named from the repo root, where it was read, not from the folder given.
        self.assertEqual(got["Root"].file, f"{self.repo}/.autopilot/review.yaml at {commit[:12]}")
        for label, rev in (("a bad commit name", "-x"), ("an unknown commit", "0" * 40)):
            with self.subTest(case=f"{label} names the root file"):
                with self.assertRaises(AngleError) as e:
                    angles.load(self.data, repo=self.repo / "sub", commit=rev)
                self.assertTrue(str(e.exception).startswith(f"{self.repo}/.autopilot/review.yaml"), str(e.exception))
                self.assertNotIn("/sub/", str(e.exception))
        with self.subTest(case="an error names the root file"):
            (self.repo / ".autopilot" / "review.yaml").write_text(
                '- name: Root\n  question: "q?"\n  when: always\n  brief: "b"\n  bogus: 1\n')
            self.git("commit", "-q", "-am", "bad key")
            commit = self.git("rev-parse", "HEAD")
            with self.assertRaises(AngleError) as e:
                angles.load(self.data, repo=self.repo / "sub", commit=commit)
            self.assertTrue(str(e.exception).startswith(f"{self.repo}/.autopilot/review.yaml at {commit[:12]}: "
                                                        "line 5: "), str(e.exception))
            self.assertNotIn("/sub/", str(e.exception))

    def test_bare_repo_file_is_named_from_the_path_given(self):
        commit = self.commit_repo_file('- name: Bare\n  question: "q?"\n  when: always\n  brief: "b"\n')
        bare = self.tmp / "bare.git"
        self.git("clone", "-q", "--bare", str(self.repo), str(bare))
        got = self.by_name(angles.load(self.data, repo=bare, commit=commit))
        self.assertEqual(got["Bare"].file, f"{bare}/.autopilot/review.yaml at {commit[:12]}")

    def test_files_starting_with_a_bom_load(self):
        text = '\ufeff- name: Bom angle\n  question: "q?"\n  when: always\n  brief: "b"\n'
        with self.subTest(layer="user"):
            path = self.data / "review" / "angles.yaml"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode("utf-8"))
            self.assertIsNotNone(self.load().get("Bom angle"))
        with self.subTest(layer="repo"):
            self.user("")
            commit = self.commit_repo_file(text)
            self.assertIsNotNone(self.load(repo=self.repo, commit=commit).get("Bom angle"))


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

        with self.subTest(case="extends a name an earlier layer turned off"):
            path = self.user('- name: A\n  question: "q?"\n  when: always\n  brief: "b"\n'
                             '- name: Portability\n  off: true\n')
            commit = self.commit_repo_file('- extends: Portability\n  brief: "b"\n')
            with self.assertRaises(AngleError) as e:
                self.load(repo=self.repo, commit=commit)
            self.assertEqual(str(e.exception), f"{self.repo}/{angles.REPO_FILE} at {commit[:12]}: line 1: extends "
                                               f"'Portability', but {path} line 5 turned it off")

    def test_inherited_globs_count_toward_the_cap(self):
        # An extends without its own when takes the base's globs, so they count toward its file's 200.
        globs = ", ".join(f'"g{n}/**"' for n in range(100))
        self.user(f'- name: Mine\n  question: "q?"\n  when: {{ paths: [{globs}] }}\n  brief: "b"\n')
        commit = self.commit_repo_file("".join(f"- {{name: M{k}, extends: Mine}}\n" for k in range(2)))
        table = self.load(repo=self.repo, commit=commit)
        self.assertEqual([len(table.get(f"M{k}").when["paths"]) for k in range(2)], [100, 100])
        commit = self.commit_repo_file("".join(f"- {{name: M{k}, extends: Mine}}\n" for k in range(3)))
        with self.assertRaises(AngleError) as e:
            self.load(repo=self.repo, commit=commit)
        self.assertEqual(str(e.exception), f"{self.repo}/{angles.REPO_FILE} at {commit[:12]}: line 3: the file holds "
                                           "more than 200 globs in all")
        with self.subTest(case="an extends with its own when takes none"):
            commit = self.commit_repo_file("".join(f"- {{name: M{k}, extends: Mine, when: always}}\n"
                                                   for k in range(3)))
            self.assertEqual(self.load(repo=self.repo, commit=commit).get("M2").when, "always")


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
        with self.subTest(where="list in when"):
            self.user('- name: A\n  question: "q?"\n  when:\n    paths:\n      - tools: [Bash]\n  brief: "b"\n')
            with self.assertRaises(AngleWidenError) as e:
                self.load()
            self.assertIn("line 5", str(e.exception))

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
            "duplicate key": ('- name: A\n  name: B\n  question: "q?"\n  when: always\n  brief: "b"\n',
                              "line 2", "first on line 1"),
            "duplicate name written after question": (
                '- question: "q?"\n  name: A\n  when: always\n  brief: "b"\n' * 2, "line 6", "named on line 2"),
            "extends bad when": ('- extends: Portability\n  when: sometimes\n', "line 2", "when"),
            "extends empty brief": ('- extends: Portability\n  brief: ""\n', "line 2", "brief"),
            "a list beside the wrapper's key": ('- name: a\nangles: 1\n', "line 2", "must be a list of angles"),
            "an empty flow list": ('[]\n', "line 1", "must be a list of angles"),
            "a bare word": ('hello\n', "line 1", "must be a list of angles"),
            "glob too long": (f'- name: A\n  question: "q?"\n  when: {{ paths: ["{'a' * 257}"] }}\n  brief: "b"\n',
                              "line 3", "longer than 256"),
            "too many globs": ('- name: A\n  question: "q?"\n  when:\n    paths: [%s]\n  brief: "b"\n'
                               % ", ".join(f"g{n}" for n in range(201)), "line 4", "more than 200 globs in all"),
            "too many globs in all": ("".join('- name: A%d\n  question: "q?"\n  when: { paths: [%s] }\n  brief: "b"\n'
                                              % (a, ", ".join(f"g{a}-{n}" for n in range(101))) for a in range(2)),
                                      "line 7", "the file holds more than 200 globs in all"),
            "a repeated kind": ('- name: A\n  question: "q?"\n  when: { kinds: [data, ui, data] }\n  brief: "b"\n',
                                "line 3", "when.kinds holds 'data' more than once"),
            "a repeated glob": ('- name: A\n  question: "q?"\n  when: { paths: ["a/**", "a/**"] }\n  brief: "b"\n',
                                "line 3", "when.paths holds 'a/**' more than once"),
            "a name with a control character": ('- name: "A\\eB"\n  question: "q?"\n  when: always\n  brief: "b"\n',
                                                "line 1", "name must be one line of text with no control characters, "
                                                "not 'A\\x1bB'"),
            "a name with a newline": ('- name: "A\\nB"\n  question: "q?"\n  when: always\n  brief: "b"\n', "line 1",
                                      "name must be one line of text with no control characters, not 'A\\nB'"),
            "a name with a right-to-left override": ('- name: "A\\u202eB"\n  question: "q?"\n  when: always\n'
                                                     '  brief: "b"\n', "line 1", "not 'A\\u202eB'"),
            "a name with a bidi isolate": ('- name: "A\\u2066B"\n  question: "q?"\n  when: always\n  brief: "b"\n',
                                           "line 1", "not 'A\\u2066B'"),
            "an extends with a control character": ('- extends: "Portability\\a"\n  brief: "b"\n', "line 1",
                                                    "extends must be one line of text with no control characters, "
                                                    "not 'Portability\\x07'"),
            "an extends with a line separator": ('- extends: "Portability\\u2028"\n  brief: "b"\n', "line 1",
                                                 "not 'Portability\\u2028'"),
            "a glob with a newline": ('- name: A\n  question: "q?"\n  when: { paths: ["a\\n/**"] }\n  brief: "b"\n',
                                      "line 3", "when.paths entries must be one line of text with no control "
                                      "characters, not 'a\\n/**'"),
            "a glob with a right-to-left override": ('- name: A\n  question: "q?"\n  when: { paths: ["a\\u202e/**"] }\n'
                                                     '  brief: "b"\n', "line 3", "not 'a\\u202e/**'"),
            "a glob of 340,000 '**' parts": ('- name: A\n  question: "q?"\n  when: { paths: ["%s"] }\n  brief: "b"\n'
                                             % "/".join(["**"] * 340000), "line 3", "longer than 256"),
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
                self.assertNotIn("line 0", msg)

    def test_every_line_breaking_or_bidi_character_is_refused_in_a_name(self):
        # Each end of both bidi ranges and both separators: a range off by one, or a dropped category, lets one in.
        chars = list(range(0x202A, 0x202F)) + list(range(0x2066, 0x206A)) + [0x2028, 0x2029]
        for code in chars:
            with self.subTest(char=f"U+{code:04X}"):
                self.user('- name: "A\\u%04xB"\n  question: "q?"\n  when: always\n  brief: "b"\n' % code)
                with self.assertRaises(AngleError) as e:
                    self.load()
                self.assertIn("line 1", str(e.exception))
                self.assertIn("name must be one line of text with no control characters, not 'A\\u%04xB'" % code,
                              str(e.exception))

    def test_file_text_is_shown_escaped_and_capped(self):
        good = '- name: A\n  question: "q?"\n  when: always\n  brief: "b"\n'
        esc, long = "x\\e[2Jy", "k" * 100000
        cases = {
            "unknown key": good + f'  "{esc}": 1\n',
            "long unknown key": good + f'  {long}: 1\n',
            "duplicate key": good + f'  "{esc}": 1\n  "{esc}": 2\n',
            "long duplicate key": good + f'  {long}: 1\n  {long}: 2\n',
            "when unknown key": f'- name: A\n  question: "q?"\n  when: {{ "{esc}": [a] }}\n  brief: "b"\n',
            "unknown kind": f'- name: A\n  question: "q?"\n  when: {{ kinds: ["{esc}"] }}\n  brief: "b"\n',
            "long kind": f'- name: A\n  question: "q?"\n  when: {{ kinds: [{long}] }}\n  brief: "b"\n',
            "absolute glob": f'- name: A\n  question: "q?"\n  when: {{ paths: ["/{esc}"] }}\n  brief: "b"\n',
            "duplicate name": (f'- name: "{esc}"\n  question: "q?"\n  when: always\n  brief: "b"\n') * 2,
            "long duplicate name": (f'- name: {long}\n  question: "q?"\n  when: always\n  brief: "b"\n') * 2,
            "extends unknown": f'- extends: "{esc}"\n  brief: "b"\n',
            "long extends": f'- extends: {long}\n  brief: "b"\n',
            "long widen key": good + '  "too' + " " * 100000 + 'ls": [Bash]\n',
            "map duplicate key": f'"{esc}": 1\n"{esc}": 2\n',
            "map long duplicate key": f'{long}: 1\n{long}: 2\n',
        }
        for label, text in cases.items():
            with self.subTest(case=label):
                path = self.user(text)
                with self.assertRaises(AngleError) as e:
                    self.load()
                msg = str(e.exception)
                self.assertTrue(msg.startswith(str(path)), msg[:300])
                self.assertNotIn("\x1b", msg)
                self.assertLess(len(msg), len(str(path)) + 400, msg[:300])
        with self.subTest(case="widen keeps the raw key"):
            self.user(good + '  "too' + " " * 1000 + 'ls": [Bash]\n')
            with self.assertRaises(AngleWidenError) as e:
                self.load()
            self.assertEqual(e.exception.key, "too" + " " * 1000 + "ls")

    def test_one_line_text_with_other_unicode_loads(self):
        # Only control characters, line and paragraph separators and bidi overrides or isolates are refused in a
        # name or glob: a joined emoji, a no-break space and a narrow no-break space are text.
        self.user('- name: "\\U0001F469\\u200d\\U0001F4BB Developer experience"\n  question: "q?"\n'
                  '  when: { paths: ["docs/Guide\\u00a0de\\u202fl\'API/**"] }\n  brief: "b"\n'
                  '- name: "Prix\\u00a0: \\u00e9t\\u00e9"\n  question: "q?"\n  when: always\n  brief: "b"\n'
                  '- extends: Portability\n  name: "Portability\\u202fbis"\n')
        table = self.load()
        dev = table.get("\U0001F469\u200d\U0001F4BB Developer experience")
        self.assertEqual(dev.when, {"paths": ["docs/Guide\u00a0de\u202fl'API/**"]})
        self.assertIsNotNone(table.get("Prix\u00a0: \u00e9t\u00e9"))
        self.assertEqual(table.get("Portability\u202fbis").origin, "user, extends Portability")
        picked = angles.pick(table, ["docs/Guide\u00a0de\u202fl'API/x.md"], "loop")
        self.assertIn("\U0001F469\u200d\U0001F4BB Developer experience", [p["name"] for p in picked])

    def test_question_and_brief_may_span_lines(self):
        # The one-line rule is for names and globs only: a question or brief is prose and keeps its newlines and tabs.
        self.user('- name: A\n  question: "First?\\nSecond?"\n  when: always\n  brief: "a\\nb\\tc"\n')
        a = self.load().get("A")
        self.assertEqual((a.question, a.brief), ("First?\nSecond?", "a\nb\tc"))

    def test_many_entries_load_fast(self):
        self.user("".join(f"- {{name: o{n}, off: true}}\n" for n in range(20000)))
        start = time.monotonic()
        table = self.load()
        self.assertLess(time.monotonic() - start, 20)
        self.assertEqual(len(table.unknown_off), 20000)
        self.assertEqual(table.unknown_off[-1]["line"], 20000)

    def test_many_new_angles_merge_fast(self):
        # Pins the merge by name: the files are parsed before the clock starts, so only the merge is timed.
        # Each load takes well under 0.1 s; a merge that scans the table per entry takes 7 s or more.
        new = "".join(f"- {{name: o{n}, question: q, when: always, brief: b}}\n" for n in range(18000))
        off = "".join(f"- {{name: o{n}, off: true}}\n" for n in range(18000))
        self.assertLess(len(new.encode()), angles.MAX_FILE)
        self.user(new)
        commit = self.commit_repo_file(off)
        user, repo = str(self.data / angles.USER_FILE), f"{self.repo}/{angles.REPO_FILE} at {commit[:12]}"
        real = angles._parse
        parsed = {user: real(new, user), repo: real(off, repo)}
        with mock.patch.object(angles, "_parse", lambda text, where: parsed.get(where) or real(text, where)):
            start = time.monotonic()
            table = self.load()
            self.assertLess(time.monotonic() - start, 3)
            self.assertEqual(len(table.angles), 18015)
            self.assertEqual(table.angles[-1].name, "o17999")
            start = time.monotonic()
            table = self.load(repo=self.repo, commit=commit)
            self.assertLess(time.monotonic() - start, 3)
        self.assertEqual(table.names(), BUILTIN_NAMES)

    def test_line_lookup_is_linear_in_the_paths(self):
        # Pins the checker on its own: deep key paths cost one step each, not one per prefix.
        lines = {(0,) + ("a",) * k: k for k in range(1, 2001)}
        start = time.monotonic()
        c = angles._Checker("f", lines)
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertEqual(c.line((0,)), 1)
        self.assertEqual(c.line((0,) + ("a",) * 7), 7)
        self.assertIsNone(c.line((1,)))

    def test_deep_brackets_are_refused_before_parsing(self):
        good = '- name: A\n  question: "q?"\n  when: always\n  brief: "b"\n'
        self.assertEqual(angles.MAX_NESTING, 32)
        deep, close = "[" * 20, "]" * 20
        # Each refused line nests 40 deep as yamlish reads it, with closers inside a quoted string at depth 20.
        cases = {"33 deep": (good + "  x: " + "[" * 33 + "]" * 33 + "\n", True),
                 "33 deep curly": (good + "  x: " + "{a: " * 33 + "1" + "}" * 33 + "\n", True),
                 "closers in double quotes": (good + "  x: " + deep + '"' + close + '", ' + deep + close * 2 + "\n",
                                              True),
                 "closers in single quotes": (good + "  x: " + deep + "'" + close + "', " + deep + close * 2 + "\n",
                                              True),
                 "closers around a doubled single quote": (good + "  x: " + deep + "'" + "]" * 10 + "''" + "]" * 10
                                                           + "', " + deep + close * 2 + "\n", True),
                 "closers around an escaped double quote": (good + "  x: " + deep + '"' + "]" * 10 + '\\"' + "]" * 10
                                                            + '", ' + deep + close * 2 + "\n", True),
                 "a single quote inside double quotes": (good + "  x: " + deep + '"' + "]" * 10 + "'" + "]" * 10
                                                         + '", ' + deep + close * 2 + "\n", True),
                 "a double quote inside single quotes": (good + "  x: " + deep + "'" + "]" * 10 + '"' + "]" * 10
                                                         + "', " + deep + close * 2 + "\n", True),
                 "a flow map value in quotes": (good + "  x: " + "{a: " * 20 + '"' + "}" * 20 + '", b: '
                                                + "{a: " * 20 + "1" + "}" * 40 + "\n", True),
                 "32 deep": (good + "  x: " + "[" * 32 + "]" * 32 + "\n", False),
                 "32 deep curly": (good + "  x: " + "{a: " * 32 + "1" + "}" * 32 + "\n", False),
                 "an apostrophe before brackets": (good + "  x: don't {a} [b] {c}\n", False),
                 # An apostrophe inside a word opens no string, and a '#' inside a word starts no comment.
                 "an apostrophe in a word inside a flow list": (good + "  x: [don't, " + "[" * 33 + "]" * 33 + "]\n",
                                                                True),
                 "a '#' in a word inside a flow list": (good + "  x: [a#b, " + "[" * 33 + "]" * 33 + "]\n", True),
                 "after a list item": (good + "- " + "[" * 33 + "]" * 33 + "\n", True)}
        for label, (text, refused) in cases.items():
            with self.subTest(case=label):
                path = self.user(text)
                with self.assertRaises(AngleError) as e:
                    self.load()
                msg = str(e.exception)
                if refused:
                    self.assertTrue(msg.startswith(f"{path}: line 5: "), msg[:300])
                    self.assertIn("nested more than 32", msg)
                else:
                    self.assertNotIn("nested", msg)
                    self.assertIn("unknown key 'x'", msg)
        with self.subTest(case="after an indented list item"):
            path = self.user(good + "  x:\n    - " + "[" * 33 + "]" * 33 + "\n")
            with self.assertRaises(AngleError) as e:
                self.load()
            self.assertTrue(str(e.exception).startswith(f"{path}: line 6: "), str(e.exception)[:300])
            self.assertIn("nested more than 32", str(e.exception))

    def test_brackets_yamlish_reads_as_text_are_not_counted(self):
        # Brackets in a quoted string, in plain text, or in a comment open no flow collection.
        angle = '- name: A\n  question: "q?"\n  when: always\n'
        cases = {"in a quoted string": angle + '  brief: "%s"\n' % " ".join(f"[ ] item{n}" for n in range(33)),
                 "in plain text after an apostrophe": angle + "  brief: Don't skip any of %s\n"
                 % " ".join(f"[step {n}](docs/{n}.md)" for n in range(33)),
                 "in a comment": angle + '  brief: "b"\n# it\'s %s\n' % " ".join(f"[{n}]" for n in range(33)),
                 "in a trailing comment": angle + '  brief: "b"   # it\'s %s\n' % ("[" * 33),
                 "opened in plain text": angle + "  brief: see %s\n" % ("[" * 33),
                 "after ': ' in a comment": angle + '  brief: "b"   # e.g.: %s\n' % ("[" * 33)}
        for label, text in cases.items():
            with self.subTest(case=label):
                self.user(text)
                self.assertEqual(self.load().get("A").question, "q?")
        with self.subTest(case="depth starts again on each line"):
            self.user("".join(f'- name: A{n}\n  question: "q?"\n  when: {{ paths: ["p{n}/**"] }}\n  brief: "b"\n'
                              for n in range(20)))
            self.assertEqual(self.load().get("A19").when, {"paths": ["p19/**"]})

    def test_deep_list_items_on_one_line_are_refused(self):
        good = '- name: A\n  question: "q?"\n  when: always\n  brief: "b"\n'
        for count, refused in ((33, True), (32, False)):
            with self.subTest(items=count):
                path = self.user(good + "- " * count + "x\n")
                with self.assertRaises(AngleError) as e:
                    self.load()
                msg = str(e.exception)
                if refused:
                    self.assertTrue(msg.startswith(f"{path}: line 5: "), msg[:300])
                    self.assertIn("nested more than 32", msg)
                else:
                    self.assertNotIn("nested", msg)
                    self.assertIn("item 2 must be a map", msg)
        with self.subTest(case="a comment after a tab ends the run"):
            path = self.user(good + "- " * 32 + "-\t# x\n")
            with self.assertRaises(AngleError) as e:
                self.load()
            self.assertIn("nested more than 32", str(e.exception))
        for label, text, line in (("items with two spaces after each '-'", good + "-  " * 33 + "x\n", 5),
                                  ("indented items", good + "  x:\n    " + "- " * 33 + "x\n", 6)):
            with self.subTest(case=label):
                path = self.user(text)
                with self.assertRaises(AngleError) as e:
                    self.load()
                self.assertTrue(str(e.exception).startswith(f"{path}: line {line}: "), str(e.exception)[:300])
                self.assertIn("nested more than 32", str(e.exception))
        with self.subTest(case="a line of the most items the cap allows, refused fast"):
            path = self.user(good + "- " * ((angles.MAX_FILE - 200) // 2) + "x\n")
            start = time.monotonic()
            with self.assertRaises(AngleError) as e:
                self.load()
            self.assertLess(time.monotonic() - start, 5)
            self.assertTrue(str(e.exception).startswith(f"{path}: line 5: "), str(e.exception)[:300])
            self.assertIn("nested more than 32", str(e.exception))

    def test_deep_but_allowed_file_is_checked_fast(self):
        # Flow maps 32 deep (the most allowed) on every line of a file near the cap: refused on its first key, fast.
        line = "- " + "{a: " * angles.MAX_NESTING + "x" + "}" * angles.MAX_NESTING + "\n"
        text = line * ((angles.MAX_FILE - 100) // len(line))
        path = self.user(text)
        start = time.monotonic()
        with self.assertRaises(AngleError) as e:
            self.load()
        self.assertLess(time.monotonic() - start, 20)
        self.assertEqual(str(e.exception), f"{path}: line 1: unknown key 'a' (known keys: {', '.join(angles.KEYS)})")

    def test_glob_limits_at_the_edge_load(self):
        globs = ", ".join(f'"{n:03d}' + "a" * 253 + '"' for n in range(200))
        self.user(f'- name: A\n  question: "q?"\n  when: {{ paths: [{globs}] }}\n  brief: "b"\n')
        self.assertEqual(len(self.load().get("A").when["paths"]), 200)
        # The cap is on the file's globs in all: two angles of 100 each fit.
        self.user("".join('- name: A%d\n  question: "q?"\n  when: { paths: [%s] }\n  brief: "b"\n'
                          % (a, ", ".join(f"g{a}-{n}" for n in range(100))) for a in range(2)))
        table = self.load()
        self.assertEqual([len(table.get(f"A{a}").when["paths"]) for a in range(2)], [100, 100])

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
                 ("**", "any/thing/at/all.txt", True),
                 ("src/*.py", "src/a.pyc", False),
                 ("billing/*", "billing/a/b.py", False),
                 ("billing/**/b.py", "billing/xb.py", False),
                 ("billing/**/b.py", "billing/b.py", True),
                 ("billing/**/b.py", "billing/x/y/b.py", True),
                 ("billing/**/b.py", "billing//b.py", False),
                 ("billing/**", "billing", False),
                 ("billing/**", "billing/", False),
                 ("billing/**", "billing/a/", True),
                 ("billing/**", "billing//", True),
                 ("**", "", False),
                 ("**/x", "x", True),
                 ("a/**/**/x", "a/x", True),
                 ("a/**/**", "a/b", True),
                 ("a//b", "a//b", True),
                 ("billing/", "billing/", True),
                 ("*a*b", "xxaxxbxb", True),
                 ("*a*b", "xxaxxbxa", False),
                 ("a**b", "axxb", True),
                 ("a*", "a", True),
                 ("?", "", False),
                 ("src/*.py", "src/.py", True)]
        for pattern, path, want in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertIs(angles.glob_match(pattern, path), want)

    def test_globs_agree_with_a_regex_reference(self):
        # The rules of test_globs as a regex, run only on short text, where backtracking costs nothing: `**` parts
        # in a run are one, a middle `**/` is any number of named folders, a final `**` is one character or more,
        # `*` is any run inside a name, `?` is one character of it.
        def reference(pattern):
            parts = pattern.split("/")
            parts = [p for i, p in enumerate(parts) if not (p == "**" and i and parts[i - 1] == "**")]
            out = []
            for i, part in enumerate(parts):
                last = i == len(parts) - 1
                if part == "**":
                    out.append(".+" if last else "(?:[^/]+/)*")
                    continue
                out.append("".join("[^/]*" if ch == "*" else "[^/]" if ch == "?" else re.escape(ch) for ch in part))
                if not last:
                    out.append("/")
            return re.compile("".join(out), re.S)

        rnd = random.Random(6)
        tokens = ["a", "b", "*", "?", "/", "**", "/**/", "**/", "/**", "\n"]
        names = ["a", "b", "/", "ab", "//", "\n"]
        for _ in range(20000):
            pattern = "".join(rnd.choice(tokens) for _ in range(rnd.randint(0, 6)))
            path = "".join(rnd.choice(names) for _ in range(rnd.randint(0, 6)))
            want = reference(pattern).fullmatch(path) is not None
            if angles.glob_match(pattern, path) is not want:
                self.fail(f"glob_match({pattern!r}, {path!r}) is not {want}")

    def test_globs_take_linear_time(self):
        cases = [("*a" * 7 + "*b", "a" * 40), ("*a" * 10 + "*b", "a" * 40),
                 ("**/**/**/**/**/**/**/x", "a/" * 60 + "b"), ("*a" * 200 + "*b", "a" * 4000),
                 ("/".join(["**"] * 200) + "/x", "a/" * 2000 + "b")]
        for pattern, path in cases:
            with self.subTest(pattern=pattern[:40], path=path[:40]):
                start = time.monotonic()
                self.assertIs(angles.glob_match(pattern, path), False)
                self.assertLess(time.monotonic() - start, 2)


    def test_glob_runs_and_dead_ends_stop_early(self):
        # A run of '**' parts is matched as one, and a glob stops at the first part that leaves no match.
        cases = {"a run of '**'": ("/".join(["**"] * 300000) + "/x", "a/" * 1000 + "b"),
                 "a dead end": ("z/" + "a/" * 300000 + "b", "a/" * 1000 + "b")}
        for label, (pattern, path) in cases.items():
            with self.subTest(case=label):
                start = time.monotonic()
                self.assertIs(angles.glob_match(pattern, path), False)
                self.assertLess(time.monotonic() - start, 3)


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

    def test_file_order_inside_a_layer(self):
        self.user('- name: New first\n  question: "q?"\n  when: always\n  brief: "b"\n'
                  '- name: Reader clarity\n  question: "Mine?"\n  when: always\n  brief: "b"\n')
        picked = angles.pick(self.load(), ["lib/util.py"], "loop")
        self.assertEqual(self.names(picked), ["Correctness", "New first", "Reader clarity"])

    def test_review_count(self):
        for files in (["docs/a.md", "schema.sql"], ["tests/test_x.py"]):
            with self.subTest(files=files):
                self.assertEqual(angles.count(files, "review", 200), 3)

    def test_paths_or_kinds(self):
        self.user('- name: Billing data\n  question: "q?"\n  when: { paths: ["billing/**"], kinds: [data] }\n'
                  '  brief: "b"\n')
        picked = {p["name"]: p for p in angles.pick(self.load(), ["schema.sql"], "loop")}
        self.assertIn("Billing data", picked)
        self.assertIn("kind data", picked["Billing data"]["why"])
        self.assertNotIn("paths", picked["Billing data"]["why"])

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

    def test_most_globs_pick_fast(self):
        # The most globs one file may hold, each near the longest, against 300 changed files: bounded.
        globs = [("**/" * 82 + f"n{n:03d}.py") if n % 2 else (f"z{n:03d}/" + "a/" * 123 + "b") for n in range(200)]
        self.assertEqual(len(globs), angles.MAX_GLOBS)
        self.assertTrue(all(250 <= len(g) <= angles.MAX_GLOB for g in globs))
        table = angles.Table([angles.Angle("Correctness", "q", "always", "b", "built-in", "f", 0, 0),
                              angles.Angle("Wide", "q", {"paths": globs}, "b", "user", "f", 1, 0)])
        files = [f"src/app/pkg{n % 50}/a/b/c/d/sub{n % 7}/mod{n}.py" for n in range(300)]
        start = time.monotonic()
        picked = angles.pick(table, files, "loop")
        self.assertLess(time.monotonic() - start, 15)
        self.assertEqual(self.names(picked), ["Correctness"])

    def test_worst_globs_pick_fast(self):
        # The worst a PR author can build within the caps: a repo file of 200 globs, each kept alive to the end of
        # every path by its leading '**', against crafted paths of 2,559 characters (ten 255-character names, or
        # 1,280 one-character names). Matching reads each path once per glob, a few integer steps a character, so
        # the shape of the globs does not matter: under 1 s here. A matcher whose cost grows with a glob part times
        # a name (the first 150 globs) or with the parts times the names (the last 50) takes 12 s or more.
        globs = [f'"**/*{"a" * 120}b{n:03d}"' for n in range(150)]
        globs += [f'"{("**/?/" * 50) + f"{n:03d}"}"' for n in range(50)]
        commit = self.commit_repo_file('- name: Wide\n  question: "q?"\n  when:\n    paths: [%s]\n  brief: "b"\n'
                                       % ", ".join(globs))
        table = self.load(repo=self.repo, commit=commit)
        self.assertEqual(len(table.get("Wide").when["paths"]), angles.MAX_GLOBS)
        self.assertTrue(all(len(g) <= angles.MAX_GLOB for g in table.get("Wide").when["paths"]))
        files = ["/".join(["a" * 255] * 9 + ["a" * 254 + str(k)]) for k in range(3)]
        files.append("/".join("ab"[i % 2] for i in range(1280)))
        self.assertTrue(all(len(f) == 2559 for f in files))
        start = time.monotonic()
        picked = angles.pick(table, files, "review", changed_lines=100)
        self.assertLess(time.monotonic() - start, 8)
        self.assertEqual(self.names(picked), ["Correctness", "Failure modes & observability"])

    def test_many_kind_angles_pick_fast(self):
        # No glob cap bounds kinds-only angles, and a PR author picks the path names: each kind's examples are
        # written once per pick, not once per angle. Well under 0.1 s here; writing them per angle takes 5 s or more.
        many = [angles.Angle(f"a{n}", "q", {"kinds": ["docs"]}, "b", "repo", "f", 2, n) for n in range(20000)]
        table = angles.Table([angles.Angle("Correctness", "q", "always", "b", "built-in", "f", 0, 0)] + many)
        files = ["docs/" + "a" * 3995]
        start = time.monotonic()
        picked = angles.pick(table, files, "loop")
        self.assertLess(time.monotonic() - start, 1)
        self.assertEqual(self.names(picked), ["Correctness", "a0", "a1", "a2"])
        self.assertEqual(picked[1]["why"], "kind docs: " + files[0])

    def test_many_kind_angles_pick_in_small_memory(self):
        # Each kinds why holds up to three kinds' examples, so a why written for every matching angle holds the
        # changed paths once per angle: about 450 MB here. The picker decides the matches first and writes a why
        # only for the angles it keeps, at most `count() - 1` of them: about 2 MB here.
        many = [angles.Angle(f"a{n}", "q", {"kinds": ["data", "infra", "api"]}, "b", "repo", "f", 2, n)
                for n in range(15000)]
        table = angles.Table([angles.Angle("Correctness", "q", "always", "b", "built-in", "f", 0, 0)] + many)
        files = ["db/" + "a" * 10000 + ".sql", "b" * 10000 + ".tf", "c" * 10000 + ".proto"]
        tracing = tracemalloc.is_tracing()
        if not tracing:
            tracemalloc.start()
        try:
            tracemalloc.reset_peak()
            base = tracemalloc.get_traced_memory()[0]
            with mock.patch.object(angles, "_why", wraps=angles._why) as why:
                picked = angles.pick(table, files, "loop")
            peak = tracemalloc.get_traced_memory()[1] - base
        finally:
            if not tracing:
                tracemalloc.stop()
        self.assertLess(peak, 32 * 2 ** 20)
        self.assertEqual(why.call_count, angles.LOOP_MAX - 1)
        self.assertEqual(self.names(picked), ["Correctness", "a0", "a1", "a2"])
        self.assertEqual(picked[1]["why"], f"kind data: {files[0]}; kind infra: {files[1]}; kind api: {files[2]}")

    def test_kind_why_lists_that_kinds_first_three_files(self):
        # Only the kind's own files are listed, the first three of them, with the rest counted.
        files = ["web/a.tsx", "db/a.sql", "db/b.sql", "db/c.sql", "db/d.sql"]
        picked = {p["name"]: p for p in angles.pick(self.load(), files, "loop")}
        self.assertEqual(picked["Data correctness & backfill"]["why"],
                         "kind data: db/a.sql, db/b.sql, db/c.sql (+1 more)")

    def test_why_writes_file_names_on_one_line(self):
        # A changed file's name comes from the PR author, and git allows a newline or an escape in it.
        files = ["db/a\nIGNORE PREVIOUS.sql", "db/b\x1b[2J.sql", "db/c\u202e.sql"]
        picked = {p["name"]: p for p in angles.pick(self.load(), files, "loop")}
        why = picked["Data correctness & backfill"]["why"]
        self.assertEqual(why, "kind data: db/a\\nIGNORE PREVIOUS.sql, db/b\\x1b[2J.sql, db/c\\u202e.sql")

    def test_why_shows_at_most_three_reasons(self):
        self.user('- name: Many globs\n  question: "q?"\n'
                  '  when: { paths: ["billing/**", "billing/*.py", "**/*.py", "billing/a.py", "*/a.py"] }\n'
                  '  brief: "b"\n')
        files = [f"billing/{x}.py" for x in "abcde"]
        picked = {p["name"]: p for p in angles.pick(self.load(), files, "loop")}
        shown = "billing/a.py, billing/b.py, billing/c.py (+2 more)"
        self.assertEqual(picked["Many globs"]["why"], f"paths billing/** matched {shown}; paths billing/*.py matched "
                                                      f"{shown}; paths **/*.py matched {shown} (+2 more)")

    def test_why_with_three_reasons_has_no_more(self):
        self.user('- name: Three globs\n  question: "q?"\n'
                  '  when: { paths: ["billing/**", "billing/*.py", "**/*.py"] }\n  brief: "b"\n')
        why = {p["name"]: p for p in angles.pick(self.load(), ["billing/charge.py"], "loop")}["Three globs"]["why"]
        self.assertNotIn("more", why)
        self.assertEqual(why.count("matched billing/charge.py"), 3, why)

    def test_why_counts_reasons_past_three_without_listing_their_files(self):
        # Pins the reasons past the third as counted only: their files are never listed. 'z/**' matches no file,
        # so it is not counted; 'd/**' and the kinds data and ui are, so "+3".
        angle = angles.Angle("Wide", "q", {"paths": ["a/**", "b/**", "c/**", "z/**", "d/**"], "kinds": ["data", "ui"]},
                             "b", "user", "f", 1, 0)
        files = ["a/x.py", "b/x.py", "c/x.py", "d/x.py", "d/y.sql", "e/z.tsx"]
        shown = {k: angles._examples(v) for k, v in angles.kinds(files).items()}
        self.assertEqual(list(shown), ["data", "ui"])
        # Only the three path reasons it writes call `_examples`: the 'd/**' it counts does not.
        with mock.patch.object(angles, "_examples", wraps=angles._examples) as examples:
            why = angles._why(angle, files, shown)
        self.assertEqual(why, "paths a/** matched a/x.py; paths b/** matched b/x.py; paths c/** matched c/x.py "
                              "(+3 more)")
        self.assertEqual(examples.call_count, 3)

    def test_small_line_constant(self):
        self.assertEqual(angles.SMALL_LINES, 30)


if __name__ == "__main__":
    unittest.main()
