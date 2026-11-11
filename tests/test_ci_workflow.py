from __future__ import annotations

import re
import unittest

from tests.helpers import ROOT

WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
RUN_COMMAND = "python3 -m unittest discover -s tests -v"


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


if __name__ == "__main__":
    unittest.main()
