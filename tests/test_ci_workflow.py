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
        # Any non-blank line at 2 spaces or less (the next job, a top-level key) ends the job; a comment line, at any
        # indent, does not. The rest of a quoted string or flow collection may also sit there: `open_values` fails
        # the check on the line that opened it.
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
    """Each step of the job as its own list of lines, split on the `-` that opens it (`- ` or a bare `-` with the
    step's keys on the lines below).

    A readable job-level key at 4 spaces after the steps ends the last step. Any other line at 6 spaces or less
    (a comment, the rest of a quoted string) keeps it open."""
    out = []
    in_step = False
    for line in block:
        text = line.strip()
        if re.match(r"^      -(?:[ \t]|$)", line):
            out.append([line])
            in_step = True
        elif not text or indent(line) > 6:
            if in_step:
                out[-1].append(line)
        elif re.match(rf"^    {KEY}$", line):
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
    (`!!str if: false`), an explicit key (`? if` with `: false` below it) or a merge key (`<<: *a`).
    A `- ` item there (a sequence, or a YAML error) cannot be read either, so it fails too.
    Lines nested deeper (under `with:`, `env:`, inside a `run: |` block) are not the mapping's own keys."""
    out = []
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or indent(line) != pad:
            continue
        if not re.match(rf"^ {{{pad}}}{KEY}$", line):
            out.append(text)
    return out


# Before a value: a `- ` item, a `? ` explicit key or a `: ` explicit value.
INDICATOR = re.compile(r"[-?:](?:[ \t]+|$)")
# Before a value: an anchor (`&name `) or a tag (`!!str `, `!tag `).
PROPERTY = re.compile(r"[&!]\S*(?:[ \t]+|$)")
# A block scalar header (`|`, `>-`, `|2 # c`): its body is the deeper lines that follow.
BLOCK_SCALAR = re.compile(r"[|>][-+0-9]*\s*(?:#.*)?")


def quoted_end(line, i):
    """Where the quoted string opening at `line[i]` ends (just past its closing quote); None when not on this line.

    In a double-quoted string `\\` escapes the next character; in a single-quoted one `''` is a quote."""
    q = line[i]
    i += 1
    while i < len(line):
        if q == '"' and line[i] == "\\":
            i += 2
        elif line[i] == q and q == "'" and line[i + 1:i + 2] == "'":
            i += 2
        elif line[i] == q:
            return i + 1
        else:
            i += 1
    return None


def flow_end(line, i):
    """Where the flow collection opening at `line[i]` (`[` or `{`) ends; None when not on this line.

    Quoted parts are skipped, and a ` #` outside them starts a comment, so the rest of the line does not count.
    A quote opens a string only where libyaml starts one: right after `[`, `{` or `,` (blanks between are fine), or
    after a `:` with a blank before the quote, or with the `:` right after a closing quote or bracket (`{"a":"b"}`).
    Anywhere else (`it's`, `5"`, `label:"bug`) the quote sits inside a plain scalar, which this reader does not
    follow, so the end cannot be read: None. A quote after a `?` is read the same way, blank or not (`why?"x`,
    `why ?"x` and `{? "x": 1}`): libyaml reads `why ?"x` as one plain scalar."""
    depth = 0
    while i < len(line):
        c = line[i]
        if c in "\"'":
            before = line[:i].rstrip(" \t")
            p, prev, gap = before[-1:], before[-2:-1], len(before) < i
            if not (p in ("[", "{", ",")
                    or p == ":" and (gap or prev in ('"', "'", "]", "}"))):
                return None
            i = quoted_end(line, i)
            if i is None:
                return None
            continue
        if c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
            if depth == 0:
                return i + 1
        elif c == "#" and line[i - 1] in " \t":
            return None
        i += 1
    return None


def open_values(lines):
    """The lines (stripped) whose value opens a quoted string or a flow collection (`[` / `{`) that does not close
    on that line, or a flow collection whose end this reader cannot find (see `flow_end`); blank lines, comment
    lines and the body of a `|` / `>` block scalar are skipped.

    Such a value may run onto the next lines at any indent, even shallower than its key, and GitHub still reads
    the keys after it as part of the same mapping. A plain-text reader cannot tell where the step or job ends
    then (the rest of the value can look like a job-level key, or the next job), so a switch after it could be
    dropped: the value itself must fail the check. A plain value cannot run onto a shallower line."""
    out = []
    body = None  # Inside a block scalar body: the column its lines must be deeper than.
    for line in lines:
        text = line.strip()
        if body is not None:
            if not text or indent(line) > body:
                continue
            body = None
        if not text or text.startswith("#"):
            continue
        i = parent = indent(line)
        while True:
            while (m := INDICATOR.match(line, i)) and m.end() > i:
                parent, i = i, m.end()
            start = i
            while (m := PROPERTY.match(line, i)) and m.end() > i:
                i = m.end()
            rest = line[i:].rstrip()
            if not rest or rest.startswith("#"):
                break
            if BLOCK_SCALAR.fullmatch(rest):
                body = parent
                break
            if rest[0] in "\"'[{":
                end = (quoted_end if rest[0] in "\"'" else flow_end)(line, i)
                if end is None:
                    out.append(text)
                    break
                key = re.match(r"[ \t]*:(?:[ \t]+|$)", line[end:])
                if not key:
                    break
                i = end + key.end()
            else:
                # A plain key ends at the first `: `; a plain value (no `: ` before any ` #`) closes on its line.
                key = re.compile(r":(?=[ \t]|$)|[ \t]#").search(line, i)
                if not key or key[0] != ":":
                    break
                i = key.end()
            parent = start
            while i < len(line) and line[i] in " \t":
                i += 1
    return out


# Either key turns a red run into a green check: `continue-on-error` lets the job or step fail quietly,
# and `if:` can skip it.
SWITCHES = ("if", "continue-on-error")
JOB = "unittest"
SETUP_PYTHON = "actions/setup-python@"


def stray_bom(text):
    """[a problem] when a byte order mark (U+FEFF) sits anywhere but at the very start of `text`, else [].

    libyaml skips one at the start of any line, but `job_block` reads it as text at indent 0 and ends the job there,
    so the lines after it (a switch among them) would go unchecked."""
    return ["the workflow has a byte order mark (U+FEFF) after its first character"] if "\ufeff" in text[1:] else []


def job_switch_problems(text):
    """Problems with the job's own `if:` / `continue-on-error:` keys in the workflow `text`; [] when clean."""
    bom = stray_bom(text)
    block = job_block(text, JOB)
    if block is None:
        return bom + [f"no `{JOB}:` job"]
    # A key line the check cannot read must fail it, not pass it.
    return (bom + [f"the `{JOB}` job sets `{k}: {v}`" for k, v in job_keys(block) if k in SWITCHES]
            + [f"cannot read the job-level line '{l}'" for l in unreadable_lines(block, 4)]
            + [f"cannot read where the value on the line '{l}' ends" for l in open_values(block)])


def step_switch_problems(text, marker):
    """Problems with the `if:` / `continue-on-error:` keys of the one step containing `marker`; [] when clean."""
    bom = stray_bom(text)
    block = job_block(text, JOB)
    if block is None:
        return bom + [f"no `{JOB}:` job"]
    found = [s for s in steps(block) if any(marker in l for l in s)]
    if len(found) != 1:
        return bom + [f"want one step containing `{marker}`, got {len(found)}"]
    keys = step_keys(found[0])
    if keys is None:
        # Keys the check cannot read must fail it, not pass it.
        return bom + [f"cannot read the keys of the `{marker}` step"]
    pad = len(re.match(r"^ *- *", found[0][0])[0])
    return (bom + [f"the `{marker}` step sets `{k}: {v}`" for k, v in keys if k in SWITCHES]
            + [f"cannot read the step-level line '{l}' of the `{marker}` step"
               for l in unreadable_lines(found[0][1:], pad)]
            + [f"cannot read where the value on the line '{l}' of the `{marker}` step ends"
               for l in open_values(found[0])])


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


def open_job_value(line):
    return f"cannot read where the value on the line '{line}' ends"


def open_step_value(line, marker):
    return f"cannot read where the value on the line '{line}' of the `{marker}` step ends"


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

    def test_a_string_running_onto_a_shallow_line_does_not_end_the_step(self):
        # The rest of a quoted string may sit at 6 spaces or less; GitHub still reads the keys after it as the step's.
        for marker, after, lines in (
                (SETUP_PYTHON, "python-version: ${{ matrix.python-version }}",
                 ['        name: "Set up', '      Python"', "        continue-on-error: true"]),
                (RUN_COMMAND, RUN_COMMAND,
                 ['        working-directory: "', '      ."', "        continue-on-error: true"])):
            with self.subTest(marker=marker):
                copy = insert_after(self.text, after, "\n".join(lines))
                self.assertEqual(step_switch_problems(copy, marker),
                                 [f"the `{marker}` step sets `continue-on-error: true`",
                                  open_step_value(lines[0].strip(), marker)])
                self.assertEqual(job_switch_problems(copy), [open_job_value(lines[0].strip())])

    def test_a_string_running_onto_a_shallow_line_of_the_job_is_a_problem(self):
        # The rest of a quoted string may sit at 2 spaces or less; GitHub still reads the keys after it as the job's,
        # so the check cannot tell where the job ends and must fail.
        copy = insert_after(self.text, "timeout-minutes: 15", '    name2: "x\n  y"\n    continue-on-error: true')
        self.assertEqual(job_switch_problems(copy), [open_job_value('name2: "x')])

    # A quoted string or a flow collection (`[` / `{`) may run onto the next lines at any indent, even shallower
    # than its key, and GitHub still reads the keys after it. Such a line looks like where the step or job ends,
    # so a switch after it would be dropped: the value itself must fail the check.

    def test_a_string_running_onto_a_key_like_line_of_a_step_is_a_problem(self):
        for marker, after, lines in (
                (SETUP_PYTHON, "python-version: ${{ matrix.python-version }}",
                 ['        name: "Set up the matrix Python;', '    note: slow on 3.14"',
                  "        continue-on-error: true"]),
                (RUN_COMMAND, RUN_COMMAND,
                 ['        working-directory: "./', '    tests: here"', "        continue-on-error: true"])):
            with self.subTest(marker=marker):
                copy = insert_after(self.text, after, "\n".join(lines))
                self.assertEqual(step_switch_problems(copy, marker), [open_step_value(lines[0].strip(), marker)])
                self.assertEqual(job_switch_problems(copy), [open_job_value(lines[0].strip())])

    def test_a_string_running_onto_a_key_like_line_of_the_job_is_a_problem(self):
        copy = (self.text.rstrip("\n")
                + '\n    env:\n      NOTE: "slow on 3.14,\n  see: issue 12"\n    continue-on-error: true\n')
        self.assertEqual(job_switch_problems(copy), [open_job_value('NOTE: "slow on 3.14,')])

    def test_a_string_running_onto_an_odd_indent_is_a_problem(self):
        copy = insert_after(self.text, "python-version: ${{ matrix.python-version }}",
                            '        name: "Set up\n   Python: 3"\n        continue-on-error: true')
        self.assertEqual(step_switch_problems(copy, SETUP_PYTHON),
                         [f"the `{SETUP_PYTHON}` step sets `continue-on-error: true`",
                          open_step_value('name: "Set up', SETUP_PYTHON)])
        self.assertEqual(job_switch_problems(copy), [open_job_value('name: "Set up')])
        copy = self.text.rstrip("\n") + '\n    env:\n      NOTE: "x,\n y: z"\n    continue-on-error: true\n'
        self.assertEqual(job_switch_problems(copy), [open_job_value('NOTE: "x,')])

    def test_a_single_quoted_or_flow_value_running_on_is_a_problem(self):
        for lines, step_problems in (
                (["        name: 'Set up", "    x: y'", "        continue-on-error: true"], []),
                (["        env: {A: 1,", "      B: 2}", "        continue-on-error: true"],
                 [f"the `{SETUP_PYTHON}` step sets `continue-on-error: true`"]),
                (["        env: [1,", "    x: 2]", "        continue-on-error: true"], []),
                (["        env: {A: {B: 1},", "    x: 2}", "        continue-on-error: true"], []),
                (['        env: {A: "}", B: "x,', '    y: z"}', "        continue-on-error: true"], [])):
            with self.subTest(lines=lines):
                copy = insert_after(self.text, "python-version: ${{ matrix.python-version }}", "\n".join(lines))
                self.assertEqual(step_switch_problems(copy, SETUP_PYTHON),
                                 step_problems + [open_step_value(lines[0].strip(), SETUP_PYTHON)])
                self.assertEqual(job_switch_problems(copy), [open_job_value(lines[0].strip())])

    def test_a_string_with_an_escaped_quote_running_on_is_a_problem(self):
        # `\"` inside double quotes and `''` inside single quotes do not close the string.
        for lines in (['        name: "say \\"hi', '    x: y"', "        continue-on-error: true"],
                      ["        name: 'Don''t", "    x: y'", "        continue-on-error: true"]):
            with self.subTest(lines=lines):
                copy = insert_after(self.text, "python-version: ${{ matrix.python-version }}", "\n".join(lines))
                self.assertEqual(step_switch_problems(copy, SETUP_PYTHON),
                                 [open_step_value(lines[0].strip(), SETUP_PYTHON)])
                self.assertEqual(job_switch_problems(copy), [open_job_value(lines[0].strip())])

    def test_an_anchored_or_tagged_string_running_on_is_a_problem(self):
        for first in ('        name: &n "a', '        name: !!str "a', "          - &n 'a"):
            with self.subTest(first=first):
                copy = insert_after(self.text, "python-version: ${{ matrix.python-version }}",
                                    first + '\n    b: c"\n        continue-on-error: true')
                self.assertEqual(step_switch_problems(copy, SETUP_PYTHON),
                                 [open_step_value(first.strip(), SETUP_PYTHON)])
                self.assertEqual(job_switch_problems(copy), [open_job_value(first.strip())])

    def test_a_value_that_closes_on_its_line_is_not_a_problem(self):
        for line in ("        name: 'it''s'", '        name: "a \\" b"', '        name: "a # b"',
                     '        with: {a: "]", b: 1}', "        name: [a, '[', {b: c}] # [", '          "a b": "c"',
                     "        run: echo \"it's", '        name: "a" # "', "        working-directory: 'C:\\'",
                     '        with: {title: "Fix #42", labels: bug}', '        with: {"a": 1}', '        with: {"a":"b"}',
                     "        with: {'a':'b'}", '        with: {a:  "x"}'):
            with self.subTest(line=line):
                copy = insert_after(self.text, "python-version: ${{ matrix.python-version }}", line)
                self.assertEqual(job_switch_problems(copy), [])
                self.assertEqual(step_switch_problems(copy, SETUP_PYTHON), [])

    def test_a_quote_inside_a_plain_scalar_of_a_flow_value_is_a_problem(self):
        # A quote in the middle of a plain scalar (`it's`) does not open a string; read as one, it pairs with a
        # later quote and hides the real end of the value, so the check cannot tell where it ends and must fail.
        for first in ("    env: {GREETING: it's me, CLOSE: '}', MODE: ci,",
                      '    env: {SIZE: 5" disk, Q: "}", MODE: ci,',
                      "    env: {GREETING: say 'hi, CLOSE: '}', MODE: ci,"):
            with self.subTest(first=first):
                copy = self.text.rstrip("\n") + "\n" + first + "\n  }\n    continue-on-error: true\n"
                self.assertEqual(job_switch_problems(copy), [open_job_value(first.strip())])
        # The same quote in a value that does close on its line fails too (known spurious reds: libyaml reads
        # `a :"b"` as one plain key, and `{? "x": 1}` as the key "x", but a quote after `?` is not followed).
        for value in ("with: {msg: it's}", 'with: {a :"b"}', 'with: {? "x": 1}'):
            with self.subTest(value=value):
                copy = insert_after(self.text, "python-version: ${{ matrix.python-version }}", "        " + value)
                self.assertEqual(job_switch_problems(copy), [open_job_value(value)])
                self.assertEqual(step_switch_problems(copy, SETUP_PYTHON), [open_step_value(value, SETUP_PYTHON)])

    def test_a_quote_glued_to_a_colon_or_question_mark_in_a_flow_value_is_a_problem(self):
        # In `label:"bug`, `why?"bug` and `why ?"bug` the `:` and `?` are part of a plain scalar, so the quote does
        # not open a string; read as one, it pairs with a later quote and hides the real end of the value, and the
        # switch after it would be dropped.
        for word in ('label:"bug', 'why?"bug', 'why ?"bug'):
            with self.subTest(word=word):
                first = f'    env: {{QUERY: {word}, NOTE: "}}'
                copy = self.text.rstrip("\n") + "\n" + first + '\n  "}\n    continue-on-error: true\n'
                self.assertEqual(job_switch_problems(copy), [open_job_value(first.strip())])
        for marker, after, lines in (
                (SETUP_PYTHON, "python-version: ${{ matrix.python-version }}",
                 ['        env: {Q: label:"bug, N: "}', '      - name: x"}', "        if: false"]),
                (RUN_COMMAND, RUN_COMMAND,
                 ['        working-directory: {a: b:"c, d: "}', '      - e"}', "        continue-on-error: true"])):
            with self.subTest(marker=marker):
                copy = insert_after(self.text, after, "\n".join(lines))
                self.assertEqual(step_switch_problems(copy, marker), [open_step_value(lines[0].strip(), marker)])
                self.assertEqual(job_switch_problems(copy), [open_job_value(lines[0].strip())])

    def test_a_byte_order_mark_after_the_first_character_is_a_problem(self):
        # libyaml skips a byte order mark at the start of any line; read as text it ends the job early, so the
        # switch after it would be dropped.
        bom = "the workflow has a byte order mark (U+FEFF) after its first character"
        for lines in (["\ufeff   if: false"], ["\ufeff       continue-on-error: true"],
                      ["\ufeff# note", "    continue-on-error: true"]):
            with self.subTest(lines=lines):
                copy = self.text.rstrip("\n") + "\n" + "\n".join(lines) + "\n"
                for problems in (job_switch_problems(copy), step_switch_problems(copy, SETUP_PYTHON),
                                 step_switch_problems(copy, RUN_COMMAND)):
                    self.assertNotEqual(problems, [])
                    self.assertIn(bom, problems)
        # One at the very start of the file is fine.
        self.assertEqual(job_switch_problems("\ufeff" + self.text), [])
        self.assertEqual(step_switch_problems("\ufeff" + self.text, SETUP_PYTHON), [])
        self.assertEqual(step_switch_problems("\ufeff" + self.text, RUN_COMMAND), [])

    def test_a_quoted_key_with_a_value_running_on_is_a_problem(self):
        copy = insert_after(self.text, "python-version: ${{ matrix.python-version }}",
                            '          "a b": "c\n    d: e"\n        continue-on-error: true')
        self.assertEqual(step_switch_problems(copy, SETUP_PYTHON), [open_step_value('"a b": "c', SETUP_PYTHON)])
        self.assertEqual(job_switch_problems(copy), [open_job_value('"a b": "c')])

    def test_a_quote_inside_a_run_block_is_not_a_problem(self):
        copy = self.text.replace(f"        run: {RUN_COMMAND}",
                                 "        run: |\n"
                                 "          echo \"it's\n"
                                 "            [ {\n"
                                 "\n"
                                 f"          {RUN_COMMAND}")
        self.assertNotEqual(copy, self.text)
        self.assertEqual(step_switch_problems(copy, RUN_COMMAND), [])
        self.assertEqual(job_switch_problems(copy), [])

    def test_a_string_opened_right_after_a_run_block_is_a_problem(self):
        for header in ("|", ">-", "|2 # keep", "&r |+"):
            with self.subTest(header=header):
                # The body (a blank line too) is skipped: its open quote and brackets are not read as a value.
                block = (f"        run: {header}\n"
                         "          echo a\n"
                         "\n"
                         "          \"it's [ {\n"
                         f"          {RUN_COMMAND}")
                copy = self.text.replace(f"        run: {RUN_COMMAND}", block)
                self.assertNotEqual(copy, self.text)
                self.assertEqual(step_switch_problems(copy, RUN_COMMAND), [])
                self.assertEqual(job_switch_problems(copy), [])
                copy = self.text.replace(f"        run: {RUN_COMMAND}",
                                         block + "\n"
                                         '        working-directory: "./\n'
                                         '    tests: here"\n'
                                         "        continue-on-error: true")
                self.assertEqual(step_switch_problems(copy, RUN_COMMAND),
                                 [open_step_value('working-directory: "./', RUN_COMMAND)])
                self.assertEqual(job_switch_problems(copy), [open_job_value('working-directory: "./')])

    def test_a_string_opened_right_after_a_dash_run_block_is_a_problem(self):
        # On a `- run: |` line the body must be deeper than the key's column, not the dash's.
        copy = (self.text.rstrip("\n")
                + '\n      - run: |\n          make\n        working-directory: "./\n    tests: here"\n'
                  "        continue-on-error: true\n")
        self.assertEqual(job_switch_problems(copy), [open_job_value('working-directory: "./')])

    def test_a_dash_run_block_body_is_not_read_as_a_value(self):
        copy = self.text.rstrip("\n") + '\n      - run: |\n          "it\'s [ {\n          make\n'
        self.assertEqual(step_switch_problems(copy, "make"), [])
        self.assertEqual(job_switch_problems(copy), [])

    def test_a_job_whose_id_starts_with_an_underscore_ends_the_unittest_job(self):
        copy = (self.text.rstrip("\n")
                + "\n  _docs:\n    if: github.event_name == 'push'\n    runs-on: x\n    steps:\n      - run: echo\n")
        self.assertEqual(job_switch_problems(copy), [])
        self.assertEqual(step_switch_problems(copy, RUN_COMMAND), [])

    def test_a_top_level_key_after_the_jobs_ends_the_job(self):
        copy = self.text.rstrip("\n") + "\nconcurrency:\n    if: false\n"
        self.assertEqual(job_switch_problems(copy), [])

    def test_a_next_job_is_not_blamed_on_the_unittest_job(self):
        copy = (self.text.rstrip("\n")
                + "\n  other:\n    if: false\n    steps:\n      - run: x\n        continue-on-error: true\n")
        self.assertEqual(job_switch_problems(copy), [])
        self.assertEqual(step_switch_problems(copy, RUN_COMMAND), [])

    def test_a_comment_inside_a_step_does_not_end_it(self):
        for comment in ("#        timeout-minutes: 5", "      # note", "    # note"):
            with self.subTest(comment=comment):
                copy = insert_after(self.text, SETUP_PYTHON, comment)
                copy = insert_after(copy, comment, "        if: false")
                self.assertEqual(step_switch_problems(copy, SETUP_PYTHON),
                                 [f"the `{SETUP_PYTHON}` step sets `if: false`"])

    def test_a_blank_line_inside_a_step_does_not_end_it(self):
        copy = insert_after(self.text, SETUP_PYTHON, "")
        copy = insert_after(copy, "python-version: ${{ matrix.python-version }}", "        &a if: false")
        self.assertEqual(step_switch_problems(copy, SETUP_PYTHON),
                         [f"cannot read the step-level line '&a if: false' of the `{SETUP_PYTHON}` step"])

    def test_a_step_opened_by_a_bare_dash_is_a_problem(self):
        # A bare `-` opens a step whose keys sit on the lines below; the check cannot read them, so it must fail.
        for marker, old, new in (
                (RUN_COMMAND, rf"(?ms)^      - name: Run tests\n.*?^        run: {re.escape(RUN_COMMAND)}$",
                 "      -\n         name: Run tests\n         if: matrix.python-version == '3.12'\n"
                 f'         env:\n           USER: ""\n         run: {RUN_COMMAND}'),
                (SETUP_PYTHON, r"(?m)^      - uses: actions/setup-python@v5\n        with:\n          python-version",
                 "      -\n         uses: actions/setup-python@v5\n         if: false\n         with:\n"
                 "           python-version")):
            with self.subTest(marker=marker):
                copy = re.sub(old, lambda _: new, self.text)
                self.assertNotEqual(copy, self.text)
                self.assertEqual(step_switch_problems(copy, marker), [f"cannot read the keys of the `{marker}` step"])

    def test_the_step_pad_follows_the_dash_line(self):
        copy = self.text.replace("      - uses: actions/setup-python@v5\n        with:\n          python-version",
                                 "      -   uses: actions/setup-python@v5\n          with:\n            python-version")
        self.assertNotEqual(copy, self.text)
        copy = insert_after(copy, SETUP_PYTHON, "          &a if: false")
        self.assertEqual(step_switch_problems(copy, SETUP_PYTHON),
                         [f"cannot read the step-level line '&a if: false' of the `{SETUP_PYTHON}` step"])

    def test_a_whitespace_only_line_is_not_a_problem(self):
        copy = insert_after(self.text, "runs-on: ubuntu-latest", "    ")
        copy = insert_after(copy, SETUP_PYTHON, "        ")
        self.assertEqual(job_switch_problems(copy), [])
        self.assertEqual(step_switch_problems(copy, SETUP_PYTHON), [])

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
