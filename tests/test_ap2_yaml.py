from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest

from tests.helpers import LIB, ROOT
from assistant import yamlish
from assistant.yamlish import YamlError, load

DESIGN = ROOT / "docs" / "v2" / "design.md"


def design_blocks(after: str) -> list:
    text = DESIGN.read_text()
    return re.findall(r"```yaml\n(.*?)```", text[text.index(after):], re.S)


# What each appendix file in docs/v2/design.md must parse to, in order: A (built in), A (the user's change), B, C, D.
APPENDIX = [
    {
        "source": "slack-mentions",
        "brings": "messages",
        "read": {"via": "connector", "tools": ["slack_search_public", "slack_read_thread"], "query": "<@{me}>",
                 "every": "15m"},
        "fill": "ai",
        "form": {"id": "Message_ts", "thread_id": "thread_ts", "author": "From", "at": "Message_ts", "text": "Text",
                 "place": "Channel"},
        "link": "https://slack.com/archives/{place_id}/p{id_digits}?thread_ts={thread_id}&cid={place_id}",  # checkpkg: ignore
        "key": "{place_id}:{thread_id}",
        "sort": {"question": "question", "ask_to_do": "errand", "alert": "alert", "fyi": "none"},
    },
    {"extends": "slack-mentions", "read": {"every": "10m", "skip_channels": ["#random", "#social"]}},
    {
        "source": "jira-epic",
        "brings": "work",
        "read": {"via": "mcp", "server": "atlassian", "tool": "search_issues",
                 "jql": "parent = {epic} AND (assignee = currentUser() OR assignee is EMPTY)", "every": "each pass"},
        "fill": "paths",
        "form": {"id": ".key", "text": ".fields.summary", "status": ".fields.status.name",
                 "depends_on": ".fields.issuelinks[is blocked by].key"},
        "link": "https://{site}/browse/{id}",
        "key": "{id}",
        "status_map": {"To Do": "todo", "In Progress": "in_progress", "In Review": "in_review", "Done": "done"},
        "sort": {"code_work": "code", "discussion": "question"},
        "writes": {"on_start": {"transition": "In Progress", "assign": "me"},
                   "on_pr": {"transition": "In Review", "remote_link": "pr"},
                   "on_merge": {"transition": "Done"}},
        "never": ["create_ticket", "comment"],
    },
    {
        "source": "vendor-status",
        "brings": "messages",
        "read": {"via": "command", "run": "curl -s https://status.example.com/api/v2/incidents/unresolved.json",
                 "every": "5m"},
        "fill": "paths",
        "form": {"id": ".incidents[].id", "at": ".incidents[].created_at", "text": ".incidents[].name"},
        "link": ".incidents[].shortlink",
        "key": "{id}",
        "sort": {"all": "alert"},
    },
    {
        "source": "team-standup",
        "brings": "messages",
        "read": {"via": "skill", "skill": "team-standup", "every": "weekdays 09:30"},
        "fill": "ai",
        "form": {"id": "required", "at": "required", "text": "required"},
        "link": "none",
        "sort": {"all": "question"},
    },
]


class DesignFilesTest(unittest.TestCase):
    def test_appendix_files_parse_straight_from_the_design(self):
        blocks = design_blocks("## Appendix: example files")
        self.assertEqual(len(blocks), len(APPENDIX))
        for n, (block, want) in enumerate(zip(blocks, APPENDIX)):
            with self.subTest(block=n):
                self.assertEqual(load(block), want)

    def test_language_example_in_section_3_1(self):
        block = design_blocks("### 3.1 Language")[0]
        self.assertEqual(load(block), {"primary_language": "en", "secondary_language": "zh"})


class ScalarTest(unittest.TestCase):
    def one(self, text):
        return load("v: " + text + "\n")["v"]

    def test_plain_strings(self):
        self.assertEqual(self.one("hello world"), "hello world")
        self.assertEqual(self.one("15m"), "15m")
        self.assertEqual(self.one(".fields.status.name"), ".fields.status.name")
        self.assertEqual(self.one("a#b"), "a#b")               # '#' only starts a comment after a space
        self.assertEqual(self.one("don't stop"), "don't stop")  # an inner quote is part of a plain string
        self.assertEqual(self.one("yes"), "yes")               # YAML 1.2: yes/no/on/off are strings
        self.assertEqual(self.one("well-'known # c"), "well-'known")  # '-' then a quote, no space: plain text
        self.assertEqual(self.one("b-'c # d'"), "b-'c")
        self.assertEqual(self.one("rock 'n roll"), "rock 'n roll")
        self.assertEqual(self.one("it's, 'ok' # c"), "it's, 'ok'")
        self.assertEqual(self.one("?x"), "?x")                 # '?' with no space or tab after it is plain text

    def test_a_quote_mid_value_stays_plain_text(self):
        # Outside brackets a quote opens a string only at a value's start, not after ', ', ' - ' or '? ' later on.
        self.assertEqual(self.one("Bob, 'the builder # nick"), "Bob, 'the builder")
        self.assertEqual(self.one("Why? 'cause it broke"), "Why? 'cause it broke")
        self.assertEqual(self.one("x - 'a # b'"), "x - 'a")
        self.assertEqual(self.one("x [y, 'z # w"), "x [y, 'z")  # a '[' mid-value opens no flow collection

    def test_quoted_strings(self):
        self.assertEqual(self.one('"a # not a comment"'), "a # not a comment")
        self.assertEqual(self.one("'it''s'"), "it's")
        self.assertEqual(self.one(r'"tab\there \"q\" \\ \u00e9"'), 'tab\there "q" \\ \u00e9')
        self.assertEqual(self.one('"true"'), "true")
        self.assertEqual(self.one("'12'"), "12")
        self.assertEqual(self.one('""'), "")

    def test_numbers(self):
        self.assertEqual(self.one("42"), 42)
        self.assertEqual(self.one("-7"), -7)
        self.assertEqual(self.one("3.5"), 3.5)
        self.assertEqual(self.one("1e3"), 1000.0)
        self.assertIsInstance(self.one("1e3"), float)
        self.assertEqual(self.one("09:30"), "09:30")

    def test_quoted_non_decimal_numbers_stay_strings(self):
        self.assertEqual(self.one("'0x1F'"), "0x1F")
        self.assertEqual(self.one('".inf"'), ".inf")
        self.assertEqual(load("a: ['0o17', \".nan\"]\n"), {"a": ["0o17", ".nan"]})

    def test_only_space_and_tab_are_white_space(self):
        # YAML white space is only space and tab: a no-break space (U+00A0) or an ideographic space (U+3000)
        # is part of the text.
        self.assertEqual(load("k: 1\u00a0\n"), {"k": "1\xa0"})
        self.assertEqual(load("k: x\u3000\n"), {"k": "x\u3000"})
        self.assertEqual(load("k: [a\u00a0, b]\n"), {"k": ["a\xa0", "b"]})
        self.assertEqual(load("mode: x\u00a0#c\n"), {"mode": "x\xa0#c"})  # a no-break space does not start a comment

    def test_booleans_and_null(self):
        self.assertIs(self.one("true"), True)
        self.assertIs(self.one("false"), False)
        self.assertIsNone(self.one("null"))
        self.assertIsNone(self.one("~"))
        self.assertIsNone(load("v:\n")["v"])
        self.assertIsNone(load("v:   # nothing here\n")["v"])


class StructureTest(unittest.TestCase):
    def test_empty_document(self):
        self.assertEqual(load(""), {})
        self.assertEqual(load("# only a comment\n\n"), {})

    def test_comments(self):
        text = "# head\na: 1   # trailing\n  # indented full-line comment\nb: 'x # y'  # real one\n"
        self.assertEqual(load(text), {"a": 1, "b": "x # y"})

    def test_nested_maps(self):
        self.assertEqual(load("a:\n  b:\n    c: 1\n  d: 2\ne: 3\n"), {"a": {"b": {"c": 1}, "d": 2}, "e": 3})

    def test_block_list(self):
        self.assertEqual(load("a:\n  - x\n  - 2\n  -\n"), {"a": ["x", 2, None]})

    def test_block_list_at_parent_indent(self):
        self.assertEqual(load("a:\n- x\n- y\nb: 1\n"), {"a": ["x", "y"], "b": 1})

    def test_list_of_maps(self):
        text = "steps:\n  - name: build\n    run: make\n  - name: test\n    args: [a, b]\n  - plain\n"
        self.assertEqual(load(text), {"steps": [{"name": "build", "run": "make"},
                                                {"name": "test", "args": ["a", "b"]}, "plain"]})

    def test_nested_lists(self):
        self.assertEqual(load("a:\n  - - 1\n    - 2\n  - - 3\n"), {"a": [[1, 2], [3]]})

    def test_list_item_holding_a_nested_block(self):
        self.assertEqual(load("a:\n  -\n    b: 1\n"), {"a": [{"b": 1}]})

    def test_flow_collections(self):
        self.assertEqual(load("a: [1, two, 'three', \"f, our\"]\n"), {"a": [1, "two", "three", "f, our"]})
        self.assertEqual(load("a: {x: 1, 'y z': [true, null], w: {}}\n"),
                         {"a": {"x": 1, "y z": [True, None], "w": {}}})
        self.assertEqual(load("a: []\nb: {}\nc: [a, b,]\n"), {"a": [], "b": {}, "c": ["a", "b"]})

    def test_quotes_at_a_value_start_inside_flow_collections(self):
        self.assertEqual(load('a: {"a":"b # c"}\n'), {"a": {"a": "b # c"}})  # JSON style: ':' right after a quote
        self.assertEqual(load('a: {"a":"b"}\n'), {"a": {"a": "b"}})
        self.assertEqual(load("a: {k: 'v # w', n: [1, 'p, q']}  # c\n"), {"a": {"k": "v # w", "n": [1, "p, q"]}})
        self.assertEqual(load("a: ['x # y', [1], 'p # q']\n"), {"a": ["x # y", [1], "p # q"]})
        self.assertEqual(load('k: {"a" :"b # c"}\n'), {"k": {"a": "b # c"}})  # spaces before the ':' after a quote
        self.assertEqual(load('k: {"a"\t:"b # c"}\n'), {"k": {"a": "b # c"}})  # a tab before the ':' after a quote
        self.assertEqual(load("k: [x:'y, z]  # it's here\n"), {"k": ["x:'y", "z"]})  # a quote after 'x:' is plain

    def test_tab_after_a_colon_in_a_flow_map(self):
        self.assertEqual(load("a: {k:\tv}\n"), {"a": {"k": "v"}})

    def test_tab_after_a_colon_in_a_block_map(self):
        self.assertEqual(load("k: \tx\n"), {"k": "x"})  # only a tab after a block indicator is refused
        self.assertEqual(load("k:\t'x # y'\n"), {"k": "x # y"})

    def test_quotes_after_block_list_indicators(self):
        self.assertEqual(load("a:\n  - 'x # y'\n  - - 'z # w'\n"), {"a": ["x # y", ["z # w"]]})
        self.assertEqual(load("a:\n  - -'x # y'\n"), {"a": ["-'x"]})

    def test_minus_without_a_space_in_a_flow_list(self):
        self.assertEqual(load("a: [-1, -x]\n"), {"a": [-1, "-x"]})

    def test_only_cr_and_lf_end_a_line(self):
        data, lines = yamlish.parse("a: x\x0cy\x85z\u2028w\nb: 1\n")
        self.assertEqual(data, {"a": "x\x0cy\x85z\u2028w", "b": 1})
        self.assertEqual(lines, {("a",): 1, ("b",): 2})
        self.assertEqual(yamlish.parse("a: 1\rb: 2\r\nc: 3")[1], {("a",): 1, ("b",): 2, ("c",): 3})

    def test_tabs_inside_list_item_text(self):
        self.assertEqual(load("k:\n  - a\tb\n  - name: c\td\n"), {"k": ["a\tb", {"name": "c\td"}]})

    def test_deep_nesting_is_refused(self):
        with self.assertRaises(YamlError) as ctx:
            load("k: " + "[" * 2000 + "]" * 2000 + "\n")
        self.assertEqual(ctx.exception.line, 1)
        self.assertIn("nested too deeply", str(ctx.exception))
        text = "".join("  " * n + "a:\n" for n in range(2000))
        with self.assertRaises(YamlError) as ctx:
            load(text)
        self.assertIn("nested too deeply", str(ctx.exception))
        self.assertGreater(ctx.exception.line, 1)  # the line depends on the recursion limit, but it is past line 1
        with self.assertRaises(YamlError) as ctx:
            load("a: 1\nk: " + "[" * 2000 + "]" * 2000 + "\n")
        self.assertIn("nested too deeply", str(ctx.exception))
        self.assertEqual(ctx.exception.line, 2)

    def test_quoted_keys(self):
        self.assertEqual(load('"To Do": 1\n\'a: b\': 2\n'), {"To Do": 1, "a: b": 2})
        self.assertEqual(load("'k':\tv\n"), {"k": "v"})  # a tab after the ':' like a plain key

    def test_crlf_lines(self):
        self.assertEqual(load("a: 1\r\nb: x\r\n"), {"a": 1, "b": "x"})

    def test_key_lines(self):
        data, lines = yamlish.parse("# c\na: 1\nb:\n  c: 2\n")
        self.assertEqual(data, {"a": 1, "b": {"c": 2}})
        self.assertEqual(lines, {("a",): 2, ("b",): 3, ("b", "c"): 4})


# One row per thing outside the subset: (name, text, line the message must name, words the message must hold).
ERRORS = [
    ("tab indent", "a:\n\tb: 1\n", 2, "tab"),
    ("anchor", "a: &x 1\n", 1, "anchor"),
    ("alias", "a: 1\nb: *x\n", 2, "alias"),
    ("tag", "a: !!str 1\n", 1, "tag"),
    ("anchor in a flow list", "a: [&x 1]\n", 1, "anchor"),
    ("literal block scalar", "a: |\n  text\n", 1, "multi-line"),
    ("folded block scalar", "a: >-\n  text\n", 1, "multi-line"),
    ("document marker", "a: 1\n---\nb: 2\n", 2, "document"),
    ("document end marker", "a: 1\n...\n", 2, "document"),
    ("document marker then tab", "a: 1\n---\tb: 2\n", 2, "document"),
    ("document marker then tab on the first line", "---\ta: 1\n", 1, "document"),
    ("document end marker then tab", "a: 1\n...\tb: 2\n", 2, "document"),
    ("dedent to no level", "a:\n    b: 1\n  c: 2\n", 3, "indentation"),
    ("unexpected indent", "a: 1\n  b: 2\n", 2, "indentation"),
    ("multi-line plain value", "a: one\n   two\n", 2, "indentation"),
    ("unclosed double quote", 'a: "abc\n', 1, "unclosed quote"),
    ("unclosed single quote", "a:\n  b: 'abc\n", 2, "unclosed quote"),
    ("unclosed bracket", "a: [1, 2\n", 1, "unclosed"),
    ("unclosed brace", "a: {b: 1\n", 1, "unclosed"),
    ("stray closing bracket", "a: [1]]\n", 1, "after"),
    ("duplicate key", "a: 1\nb: 2\na: 3\n", 3, "duplicate key 'a'"),
    ("duplicate nested key", "x:\n  a: 1\n  a: 2\n", 3, "duplicate key 'a'"),
    ("duplicate flow key", "a: {b: 1, b: 2}\n", 1, "duplicate key 'b'"),
    ("not key: value", "a: 1\njust text\n", 2, "key: value"),
    ("list item among keys", "a: 1\n- b\n", 2, "key: value"),
    ("key among list items", "a:\n  - x\n  y: 1\n", 3, "list item"),
    ("top level list", "- a\n", 1, "top level"),
    ("text after a quoted string", 'a: "x" y\n', 1, "after"),
    ("colon-space in a plain value", "a: b: c\n", 1, "quote"),
    ("complex key", "? a\n", 1, "key"),
    ("empty flow entry", "a: [1, , 2]\n", 1, "empty"),
    ("unknown escape", 'a: "\\q"\n', 1, "escape"),
    ("escape past the last code point", 'a: "\\U00110000"\n', 1, "bad escape"),
    ("quoted key without ': '", '"a":b\n', 1, "a quoted key must be followed by ': '"),
    ("complex key in a flow map", "a: {? b: 1}\n", 1, "complex keys"),
    ("block list item in a flow list", "a: [- x]\n", 1, "block list item"),
    ("bare '-' in a flow list", "a: [-]\n", 1, "block list item"),
    ("bare '?' key in a flow map", "a: {?: 1}\n", 1, "complex keys"),
    ("plain value starting with ']'", "a: ]x\n", 1, "']' cannot start a plain value"),
    ("plain value starting with '}'", "a: }x\n", 1, "'}' cannot start a plain value"),
    ("plain value starting with ','", "a: ,x\n", 1, "',' cannot start a plain value"),
    ("plain value starting with '? '", "a: ? x\n", 1, "complex keys"),
    ("plain value that is only '?'", "a: ?\n", 1, "complex keys"),
    ("tab after ':' in a flow list", "a: [b:\tc]\n", 1, "expected ',' or ']'"),
    ("tab after ':' before a quote in a flow list", "a: [b:\t'x # y']\n", 1, "expected ',' or ']'"),
    ("tab after '?' in a value", "k: ?\tx\n", 1, "a tab after '-' or '?'"),
    ("tab after '?' in a key", "a: 1\n?\tx: 1\n", 2, "a tab after '-' or '?'"),
    ("tab after '-' in a value", "k: -\tx\n", 1, "a tab after '-' or '?'"),
    ("tab after '?' in a flow list", "k: [?\tx]\n", 1, "a tab after '-' or '?'"),
    ("tab after '-' in a flow list", "k: [-\tx]\n", 1, "a tab after '-' or '?'"),
    ("tab after '?' in a flow map key", "k: {?\tx: 1}\n", 1, "a tab after '-' or '?'"),
    ("tab after a nested '-' in a block list", "k:\n  - -\tx\n", 2, "a tab after '-' or '?'"),
    ("tab after '-' starting a block list item", "k:\n  -\tx\n", 2, "a tab after '-' or '?'"),
    ("space then tab after '-' in a block list", "k:\n  - \t3\n", 2, "a tab after '-' or '?'"),
    ("spaces then tab after '-' in a block list", "k:\n  -   \t3\n", 2, "a tab after '-' or '?'"),
    ("space then tab before a key in a block list", "k:\n  - \tname: a\n", 2, "a tab after '-' or '?'"),
    ("space then tab after a nested '-' in a block list", "k:\n  - - \tx\n", 2, "a tab after '-' or '?'"),
    ("form feed is not a line break", "a: x\x0cy\nc: [\n", 2, "unclosed"),
    ("line separator is not a line break", "a: x\u2028y\nc: [\n", 2, "unclosed"),
    ("no-break space after ':' is not white space", "mode:\u00a0live\n", 1, "key: value"),
    ("a line holding only a no-break space", "a: 1\n\u00a0\nb: 2\n", 2, "key: value"),
    ("no-break space after a quoted value", 'mode: "live"\u00a0\n', 1, "unexpected text after a quoted string"),
    ("hex number", "a: 0x1F\n", 1, "'0x1F' is a YAML number form this reader does not support"),
    ("octal number", "a: 1\nb: 0o17\n", 2, "number form"),
    ("infinity", "a: .inf\n", 1, "number form"),
    ("negative infinity", "a: -.Inf\n", 1, "number form"),
    ("not a number", "a: .NaN\n", 1, "number form"),
    ("hex number in a flow list", "a: [1, 0X1f]\n", 1, "'0X1f' is a YAML number form"),
    ("infinity in a flow map", "a: {b: +.INF}\n", 1, "number form"),
]


class ErrorTest(unittest.TestCase):
    def test_each_unsupported_form_names_its_line_and_reason(self):
        for name, text, line, words in ERRORS:
            with self.subTest(name):
                with self.assertRaises(YamlError) as ctx:
                    load(text)
                msg = str(ctx.exception)
                self.assertIn(f"line {line}:", msg)
                self.assertIn(words, msg)
                self.assertEqual(ctx.exception.line, line)

    def test_one_error_class(self):
        self.assertTrue(issubclass(YamlError, ValueError))


class LongLineTest(unittest.TestCase):
    """The reader's time grows in step with a line's length: a 1 MiB line of quote characters reads in a second or
    two. Each runs in a child process, so a slow reader fails at the timeout instead of holding the suite."""

    N = 1024 * 1024  # sources.MAX_SOURCE
    CHILD = "import json, sys; from assistant import yamlish; print(json.dumps(yamlish.load(sys.stdin.read())))"

    def load_in_a_child(self, text: str):
        env = {k: v for k, v in os.environ.items() if k != "LC_ALL"}
        env.update(PYTHONPATH=str(LIB), PYTHON_COLORS="0", PYTHONIOENCODING="utf-8")
        r = subprocess.run([sys.executable, "-c", self.CHILD], input=text, capture_output=True, text=True,
                           encoding="utf-8", env=env, timeout=15)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        return json.loads(r.stdout)

    def test_a_long_line_of_quotes_reads_in_linear_time(self):
        n = self.N
        cases = [
            ("single quotes", "k: x" + "'" * n, {"k": "x" + "'" * n}),
            ("double quotes", "k: x" + '"' * n, {"k": "x" + '"' * n}),
            ("quote and space", "k: x" + "' " * (n // 2), {"k": "x" + ("' " * (n // 2)).rstrip(" ")}),
            ("in a flow list", "k: [a" + '"' * n + "]", {"k": ["a" + '"' * n]}),
            ("in a block list item", "k:\n  - x" + "'" * n, {"k": ["x" + "'" * n]}),
        ]
        for name, text, want in cases:
            with self.subTest(name):
                self.assertEqual(self.load_in_a_child(text), want)


if __name__ == "__main__":
    unittest.main()
