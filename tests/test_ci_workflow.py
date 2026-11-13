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
        # A comment line, at any indent, does not end the job.
        if line.strip() and not line.strip().startswith("#") and indent(line) <= 2:
            break
        block.append(line)
    return block


# One mapping key and the rest of its line: the key may be quoted (`'if':`) and may have spaces before its colon.
KEY = r"""(?P<q>["']?)(?P<key>[A-Za-z][\w-]*)(?P=q)\s*:(?P<rest>.*)"""


def job_keys(block):
    """Keys set directly on the job (4 spaces in), comments skipped."""
    keys = []
    for line in block:
        m = re.match(rf"^    {KEY}$", line)
        if m:
            keys.append((m["key"], m["rest"].split("#", 1)[0].strip()))
    return keys


def steps(block):
    """Each step of the job as its own list of lines, split on the `- ` that opens it.

    A job-level key after the steps (a line at 6 spaces or less, not a `- ` or a comment) ends the last step."""
    out = []
    in_step = False
    for line in block:
        text = line.strip()
        if re.match(r"^      - ", line):
            out.append([line])
            in_step = True
        elif not text or indent(line) > 6:
            if in_step:
                out[-1].append(line)
        elif not text.startswith("#"):
            in_step = False
    return out


def step_keys(step):
    """Keys set directly on one step: the one on its `- ` line and those at that key's indent, comments skipped.

    Keys nested deeper (under `with:` or `env:`) are not the step's own and are left out.
    None when the `- ` line does not open with a key (a flow mapping, a comment, an anchor), so the keys cannot be read."""
    m = re.match(rf"^(?P<dash> *- *){KEY}$", step[0])
    if not m:
        return None
    keys = [(m["key"], m["rest"].split("#", 1)[0].strip())]
    pad = " " * len(m["dash"])
    for line in step[1:]:
        m = re.match(rf"^{pad}{KEY}$", line)
        if m:
            keys.append((m["key"], m["rest"].split("#", 1)[0].strip()))
    return keys


def unreadable_lines(lines, pad):
    """The lines at exactly `pad` spaces that `KEY` cannot read, blank and comment lines skipped.

    At a mapping's key indent such a line is still a key to GitHub: an anchor (`&a if: false`), a tag
    (`!!str if: false`), an explicit key (`? if` with `: false` below it), a merge key (`<<: *a`) or a `- ` item.
    Lines nested deeper (under `with:`, `env:`, inside a `run: |` block) are not the mapping's own keys."""
    out = []
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or indent(line) != pad:
            continue
        if not re.match(rf"^ {{{pad}}}{KEY}$", line):
            out.append(text)
    return out


# Either key turns a red run into a green check: `continue-on-error` lets the job or step fail quietly,
# and `if:` can skip it.
SWITCHES = ("if", "continue-on-error")
JOB = "unittest"
SETUP_PYTHON = "actions/setup-python@"


def job_switch_problems(text):
    """Problems with the job's own `if:` / `continue-on-error:` keys in the workflow `text`; [] when clean."""
    block = job_block(text, JOB)
    if block is None:
        return [f"no `{JOB}:` job"]
    # A key line the check cannot read must fail it, not pass it.
    return ([f"the `{JOB}` job sets `{k}: {v}`" for k, v in job_keys(block) if k in SWITCHES]
            + [f"cannot read the job-level line '{l}'" for l in unreadable_lines(block, 4)])


def step_switch_problems(text, marker):
    """Problems with the `if:` / `continue-on-error:` keys of the one step containing `marker`; [] when clean."""
    block = job_block(text, JOB)
    if block is None:
        return [f"no `{JOB}:` job"]
    found = [s for s in steps(block) if any(marker in l for l in s)]
    if len(found) != 1:
        return [f"want one step containing `{marker}`, got {len(found)}"]
    keys = step_keys(found[0])
    if keys is None:
        # Keys the check cannot read must fail it, not pass it.
        return [f"cannot read the keys of the `{marker}` step"]
    pad = len(re.match(r"^ *- *", found[0][0])[0])
    return ([f"the `{marker}` step sets `{k}: {v}`" for k, v in keys if k in SWITCHES]
            + [f"cannot read the step-level line '{l}' of the `{marker}` step"
               for l in unreadable_lines(found[0][1:], pad)])


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

    def test_job_sets_up_python_then_prints_the_version_then_runs_the_tests(self):
        # The version step runs after setup-python, so the log shows the interpreter that really ran the suite.
        all_steps = steps(self.block)
        setups = [i for i, s in enumerate(all_steps) if any("actions/setup-python@" in l for l in s)]
        names = [i for i, s in enumerate(all_steps)
                 if any(l.strip() in ("run: python --version", "- run: python --version") for l in s)]
        runs = [i for i, s in enumerate(all_steps) if any(RUN_COMMAND in l for l in s)]
        self.assertEqual(len(setups), 1, "want one actions/setup-python step")
        self.assertEqual(len(names), 1, "want one step running `python --version`")
        self.assertEqual(len(runs), 1)
        self.assertLess(setups[0], names[0], "setup-python must run before the version print")
        self.assertLess(names[0], runs[0], "the version print must run before the tests")

    # A red run must never show as a green check: `continue-on-error` lets a job or step fail quietly, and an
    # `if:` can skip the step that sets up or runs the suite.

    def test_job_has_no_skip_or_allow_failure_switch(self):
        self.assertEqual(job_switch_problems(WORKFLOW.read_text()), [])

    def test_setup_python_step_has_no_skip_or_allow_failure_switch(self):
        self.assertEqual(step_switch_problems(WORKFLOW.read_text(), SETUP_PYTHON), [])

    def test_run_step_has_no_skip_or_allow_failure_switch(self):
        self.assertEqual(step_switch_problems(WORKFLOW.read_text(), RUN_COMMAND), [])


def insert_after(text, needle, new_line):
    """A copy of `text` with `new_line` added after the first line that contains `needle`."""
    lines = text.splitlines()
    at = next((i for i, l in enumerate(lines) if needle in l), None)
    if at is None:
        raise AssertionError(f"no line contains {needle!r}")
    return "\n".join(lines[:at + 1] + [new_line] + lines[at + 1:]) + "\n"


class SwitchCheckTest(unittest.TestCase):
    """The switch checks above, fed modified copies of the real workflow text (never the file on disk)."""

    def setUp(self):
        self.text = WORKFLOW.read_text()

    def test_job_level_continue_on_error_true_is_a_problem(self):
        copy = insert_after(self.text, "  unittest:", "    continue-on-error: true")
        self.assertEqual(len(job_switch_problems(copy)), 1)

    def test_job_level_continue_on_error_for_one_version_is_a_problem(self):
        copy = insert_after(self.text, "  unittest:",
                            "    continue-on-error: ${{ matrix.python-version == '3.14' }}")
        self.assertEqual(len(job_switch_problems(copy)), 1)

    def test_job_level_if_is_a_problem(self):
        copy = insert_after(self.text, "  unittest:", "    if: false")
        self.assertEqual(len(job_switch_problems(copy)), 1)

    def test_if_false_on_the_setup_python_step_is_a_problem(self):
        copy = insert_after(self.text, SETUP_PYTHON, "        if: false")
        self.assertEqual(len(step_switch_problems(copy, SETUP_PYTHON)), 1)

    def test_if_as_the_first_key_of_the_setup_python_step_is_a_problem(self):
        copy = re.sub(r"(?m)^      - (uses: actions/setup-python@)", r"      - if: false\n        \1", self.text)
        self.assertNotEqual(copy, self.text)
        self.assertEqual(len(step_switch_problems(copy, SETUP_PYTHON)), 1)

    def test_continue_on_error_on_the_setup_python_step_is_a_problem(self):
        copy = insert_after(self.text, SETUP_PYTHON, "        continue-on-error: true")
        self.assertEqual(len(step_switch_problems(copy, SETUP_PYTHON)), 1)

    def test_continue_on_error_true_on_the_run_step_is_a_problem(self):
        copy = insert_after(self.text, RUN_COMMAND, "        continue-on-error: true")
        self.assertEqual(len(step_switch_problems(copy, RUN_COMMAND)), 1)

    def test_if_on_the_run_step_is_a_problem(self):
        copy = insert_after(self.text, RUN_COMMAND, "        if: matrix.python-version == '3.12'")
        self.assertEqual(len(step_switch_problems(copy, RUN_COMMAND)), 1)

    def test_a_switch_on_another_step_is_not_blamed_on_the_run_step(self):
        copy = insert_after(self.text, "run: python --version", "        continue-on-error: true")
        self.assertEqual(step_switch_problems(copy, RUN_COMMAND), [])
        self.assertEqual(step_switch_problems(copy, SETUP_PYTHON), [])

    def test_a_commented_out_switch_is_not_a_problem(self):
        copy = insert_after(self.text, "  unittest:", "    # continue-on-error: true")
        copy = insert_after(copy, SETUP_PYTHON, "        # if: false")
        copy = insert_after(copy, RUN_COMMAND, "        # continue-on-error: true")
        self.assertEqual(job_switch_problems(copy), [])
        self.assertEqual(step_switch_problems(copy, SETUP_PYTHON), [])
        self.assertEqual(step_switch_problems(copy, RUN_COMMAND), [])

    def test_a_switch_nested_under_with_or_env_is_not_a_problem(self):
        copy = insert_after(self.text, "python-version: ${{ matrix.python-version }}",
                            "          continue-on-error: true")
        copy = insert_after(copy, 'USER: ""', '          GATE: "if: false"')
        copy = insert_after(copy, 'USER: ""', "          if: false")
        self.assertEqual(step_switch_problems(copy, SETUP_PYTHON), [])
        self.assertEqual(step_switch_problems(copy, RUN_COMMAND), [])

    def test_a_quoted_if_on_the_setup_python_step_is_a_problem(self):
        copy = insert_after(self.text, SETUP_PYTHON, "        'if': false")
        self.assertEqual(step_switch_problems(copy, SETUP_PYTHON), [f"the `{SETUP_PYTHON}` step sets `if: false`"])

    def test_a_quoted_job_level_continue_on_error_is_a_problem(self):
        copy = insert_after(self.text, "  unittest:", '    "continue-on-error": true')
        self.assertEqual(job_switch_problems(copy), ["the `unittest` job sets `continue-on-error: true`"])

    def test_a_space_before_the_colon_is_still_read(self):
        copy = insert_after(self.text, SETUP_PYTHON, "        if : false")
        self.assertEqual(step_switch_problems(copy, SETUP_PYTHON), [f"the `{SETUP_PYTHON}` step sets `if: false`"])

    def test_a_flow_mapping_setup_python_step_is_a_problem(self):
        # Keys the check cannot read must fail the check, not pass it.
        copy = re.sub(r"(?m)^      - uses: actions/setup-python@v5\n        with:\n          python-version: .*$",
                      "      - {uses: actions/setup-python@v5, if: false}", self.text)
        self.assertNotEqual(copy, self.text)
        self.assertEqual(step_switch_problems(copy, SETUP_PYTHON),
                         [f"cannot read the keys of the `{SETUP_PYTHON}` step"])

    def test_a_setup_python_step_that_opens_with_a_comment_is_a_problem(self):
        copy = re.sub(r"(?m)^      - (uses: actions/setup-python@)", r"      - # note\n        \1", self.text)
        copy = insert_after(copy, SETUP_PYTHON, "        if: false")
        self.assertNotEqual(copy, self.text)
        self.assertEqual(step_switch_problems(copy, SETUP_PYTHON),
                         [f"cannot read the keys of the `{SETUP_PYTHON}` step"])

    def test_a_comment_does_not_end_the_job(self):
        for comment in ("#    timeout-minutes: 30", "  # x"):
            with self.subTest(comment=comment):
                copy = insert_after(self.text, "runs-on: ubuntu-latest", comment)
                copy = insert_after(copy, comment, "    continue-on-error: true")
                self.assertEqual(job_switch_problems(copy), ["the `unittest` job sets `continue-on-error: true`"])

    def test_a_blank_line_does_not_end_the_job(self):
        copy = self.text.rstrip("\n") + "\n\n    continue-on-error: true\n"
        self.assertEqual(job_switch_problems(copy), ["the `unittest` job sets `continue-on-error: true`"])

    def test_text_inside_a_run_block_is_not_a_problem(self):
        copy = self.text.replace(f"        run: {RUN_COMMAND}",
                                 "        run: |\n"
                                 "          if: false\n"
                                 "          continue-on-error: true\n"
                                 f"          {RUN_COMMAND}")
        self.assertNotEqual(copy, self.text)
        self.assertEqual(step_switch_problems(copy, RUN_COMMAND), [])

    def test_job_level_switch_after_the_steps_is_a_problem(self):
        copy = insert_after(self.text, RUN_COMMAND, "    continue-on-error: true")
        self.assertEqual(job_switch_problems(copy), ["the `unittest` job sets `continue-on-error: true`"])

    def test_a_job_level_key_after_the_steps_is_not_blamed_on_the_run_step(self):
        # The lines under a job-level key that follows `steps:` belong to that key, not to the last step.
        for lines in (["    env:", "        &a X: 1"], ["    env:", "        if: x"]):
            with self.subTest(lines=lines):
                copy = self.text.rstrip("\n") + "\n" + "\n".join(lines) + "\n"
                self.assertEqual(step_switch_problems(copy, RUN_COMMAND), [])
                self.assertEqual(job_switch_problems(copy), [])

    # A key line the check cannot read (an anchor, a tag, an explicit `? ` key, a merge key) can still carry a
    # switch, so it must fail the check, not be skipped.

    def test_an_unreadable_job_level_line_is_a_problem(self):
        for lines in (["    &a if: false"], ["    !!str if: false"], ["    ? if", "    : false"],
                      ["    <<: *defaults"], ["    - if: false"]):
            with self.subTest(lines=lines):
                copy = self.text
                for at, line in zip(["  unittest:"] + lines, lines):
                    copy = insert_after(copy, at, line)
                problems = job_switch_problems(copy)
                self.assertGreaterEqual(len(problems), 1)
                self.assertIn(f"cannot read the job-level line '{lines[0].strip()}'", problems)

    def test_an_unreadable_line_on_the_setup_python_step_is_a_problem(self):
        for lines in (["        &a if: false"], ["        !!str continue-on-error: true"],
                      ["        ? if", "        : false"]):
            with self.subTest(lines=lines):
                copy = self.text
                for at, line in zip([SETUP_PYTHON] + lines, lines):
                    copy = insert_after(copy, at, line)
                problems = step_switch_problems(copy, SETUP_PYTHON)
                self.assertGreaterEqual(len(problems), 1)
                self.assertIn(f"cannot read the step-level line '{lines[0].strip()}' of the `{SETUP_PYTHON}` step",
                              problems)

    def test_an_unreadable_line_on_the_run_step_is_a_problem(self):
        copy = insert_after(self.text, RUN_COMMAND, "        &a if: false")
        self.assertEqual(step_switch_problems(copy, RUN_COMMAND),
                         [f"cannot read the step-level line '&a if: false' of the `{RUN_COMMAND}` step"])

    def test_an_unreadable_line_nested_deeper_is_not_a_problem(self):
        copy = insert_after(self.text, "python-version: ${{ matrix.python-version }}", "          &v x: 1")
        copy = insert_after(copy, 'USER: ""', "          !!str if: false")
        copy = insert_after(copy, "fail-fast: false", "      &m x: 1")
        self.assertEqual(step_switch_problems(copy, SETUP_PYTHON), [])
        self.assertEqual(step_switch_problems(copy, RUN_COMMAND), [])
        self.assertEqual(job_switch_problems(copy), [])

    def test_an_unreadable_line_on_another_step_is_not_blamed_on_the_checked_steps(self):
        copy = insert_after(self.text, "run: python --version", "        &a if: false")
        self.assertEqual(step_switch_problems(copy, RUN_COMMAND), [])
        self.assertEqual(step_switch_problems(copy, SETUP_PYTHON), [])
        self.assertEqual(job_switch_problems(copy), [])

    def test_a_missing_job_or_step_is_a_problem(self):
        # A renamed job or step must not make the check pass with nothing to look at.
        self.assertEqual(len(job_switch_problems(self.text.replace("  unittest:", "  tests:"))), 1)
        self.assertEqual(len(step_switch_problems(self.text, "no-such-step")), 1)


if __name__ == "__main__":
    unittest.main()
