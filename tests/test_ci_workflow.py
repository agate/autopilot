from __future__ import annotations

import json
import re
import unittest

from tests.helpers import ROOT

WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
RUN_COMMAND = "python -m unittest discover -s tests -v"
VERSIONS = ["3.12", "3.14"]


def indent(line):
    return len(line) - len(line.lstrip(" "))


def job_block(text, name):
    """The lines of one job under `jobs:`, read as plain text (the suite uses no YAML library)."""
    lines = text.splitlines()
    try:
        start = lines.index(f"  {name}:")
    except ValueError:
        return None
    block = []
    for line in lines[start + 1:]:
        if line.strip() and indent(line) <= 2:
            break
        block.append(line)
    return block


def job_keys(block):
    """Keys set directly on the job (4 spaces in), comments skipped."""
    keys = []
    for line in block:
        m = re.match(r"^    ([A-Za-z][\w-]*):(.*)$", line)
        if m:
            keys.append((m.group(1), m.group(2).split("#", 1)[0].strip()))
    return keys


def steps(block):
    """Each step of the job as its own list of lines, split on the `- ` that opens it."""
    out = []
    for line in block:
        if re.match(r"^      - ", line):
            out.append([line])
        elif out and (not line.strip() or indent(line) > 6):
            out[-1].append(line)
    return out


class CiWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.block = job_block(WORKFLOW.read_text(), "unittest")
        self.assertIsNotNone(self.block, "no `unittest:` job in the tests workflow")

    def test_job_has_a_timeout(self):
        # Without it GitHub waits 6 hours on a hung test before it fails the check.
        found = [v for k, v in job_keys(self.block) if k == "timeout-minutes"]
        self.assertEqual(len(found), 1, f"want one job-level timeout-minutes, got {found}")
        self.assertRegex(found[0], r"^\d+$")
        self.assertTrue(5 <= int(found[0]) <= 30, found[0])

    def test_job_runs_the_suite_with_an_empty_user(self):
        # The package check treats $USER as a private name, so CI must clear it for the run.
        runs = [s for s in steps(self.block)
                if any(l.strip() in (f"run: {RUN_COMMAND}", f"- run: {RUN_COMMAND}") for l in s)]
        self.assertEqual(len(runs), 1, f"want one step running `{RUN_COMMAND}`")
        step = runs[0]
        env = [i for i, l in enumerate(step) if l.strip() == "env:"]
        self.assertEqual(len(env), 1, "the run step has no `env:` map")
        under_env = []
        for line in step[env[0] + 1:]:
            if line.strip() and indent(line) <= indent(step[env[0]]):
                break
            under_env.append(line.strip())
        self.assertIn('USER: ""', under_env)

    def under(self, key):
        """The lines nested under one job-level key, for example `strategy:`."""
        lines = [i for i, l in enumerate(self.block) if re.match(rf"^    {key}:", l)]
        self.assertEqual(len(lines), 1, f"want one job-level `{key}:`")
        out = []
        for line in self.block[lines[0] + 1:]:
            if line.strip() and indent(line) <= 4:
                break
            out.append(line.split("#", 1)[0].strip())
        return [l for l in out if l]

    def test_job_runs_on_both_python_versions(self):
        # A bug that only one version shows (Path.is_file swallows PermissionError on 3.14, not on 3.12)
        # must still turn CI red, so the suite runs once per version.
        strategy = self.under("strategy")
        self.assertIn(f"python-version: {json.dumps(VERSIONS)}", strategy)

    def test_both_versions_always_report(self):
        # With fail-fast on, a failure on one version cancels the other and hides its result.
        self.assertIn("fail-fast: false", self.under("strategy"))

    def test_job_name_shows_the_version(self):
        found = [v for k, v in job_keys(self.block) if k == "name"]
        self.assertEqual(len(found), 1, f"want one job-level name, got {found}")
        self.assertIn("${{ matrix.python-version }}", found[0])

    def test_job_sets_up_the_matrix_python(self):
        setup = [s for s in steps(self.block) if any("actions/setup-python@" in l for l in s)]
        self.assertEqual(len(setup), 1, "want one actions/setup-python step")
        self.assertTrue(any(l.strip() == "python-version: ${{ matrix.python-version }}" for l in setup[0]),
                        "the setup-python step does not use the matrix version")

    def test_job_prints_the_python_version_before_the_tests(self):
        # The CI log then shows which interpreter really ran the suite.
        all_steps = steps(self.block)
        names = [i for i, s in enumerate(all_steps)
                 if any(l.strip() in ("run: python --version", "- run: python --version") for l in s)]
        runs = [i for i, s in enumerate(all_steps) if any(RUN_COMMAND in l for l in s)]
        self.assertEqual(len(names), 1, "want one step running `python --version`")
        self.assertEqual(len(runs), 1)
        self.assertLess(names[0], runs[0])


if __name__ == "__main__":
    unittest.main()
