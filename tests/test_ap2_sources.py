from __future__ import annotations

import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT
from assistant import form, sources
from assistant.config import ConfigError
from assistant.sources import Loader, SourceError

FIXTURES = ROOT / "tests" / "fixtures" / "ap2" / "sources"

# A small valid source; each bad case below changes one thing.
GOOD = """\
source: s
brings: messages
read:
  via: command
  run: echo []
fill: paths
form:
  id: .id
  at: .at
  text: .text
link: .url
key: "{id}"
sort: { all: alert }
"""


class SourceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-sources-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.plugin = self.tmp / "plugin"
        self.data = self.tmp / "data"
        self.plugin.mkdir()
        (self.data / "sources").mkdir(parents=True)

    def builtin(self, name, text=None):
        p = self.plugin / f"{name}.yaml"
        if text is None:
            shutil.copy(FIXTURES / "plugin" / f"{name}.yaml", p)
        else:
            p.write_text(textwrap.dedent(text), encoding="utf-8")
        return p

    def user(self, name, text=None):
        p = self.data / "sources" / f"{name}.yaml"
        if text is None:
            shutil.copy(FIXTURES / "user" / f"{name}.yaml", p)
        else:
            p.write_text(textwrap.dedent(text), encoding="utf-8")
        return p

    def loader(self):
        return Loader(self.data, plugin_dir=self.plugin)


class LoadingTest(SourceCase):
    def test_a_source_file_past_the_size_limit_is_refused(self):
        # Read with a byte limit, so a huge file (or a link to a device that never ends) costs no more than that.
        good = GOOD.replace("\n", "\r\n")  # read with its line ends made \n, as before
        cap = len(good.encode())
        self.user("s", good)
        with mock.patch("assistant.sources.MAX_SOURCE", cap):
            self.assertEqual(self.loader().load("s").data["read"]["run"], "echo []")
            big = self.user("s", good + "#")
            with self.assertRaises(SourceError) as e:
                self.loader().load("s")
        self.assertEqual(str(e.exception), f"{big}: larger than {cap} bytes")

    def test_builtin_only(self):
        self.builtin("slack-mentions")
        self.builtin("gh-mine")
        src = self.loader().load("slack-mentions")
        self.assertEqual(src.origin, "built-in")
        self.assertEqual(src.files, [self.plugin / "slack-mentions.yaml"])
        self.assertEqual(src.data["read"]["every"], "15m")
        self.assertEqual(src.sort, {"question": "question", "ask_to_do": "errand", "alert": "alert", "fyi": "none"})
        self.assertEqual([s.name for s in self.loader().load_all()], ["gh-mine", "slack-mentions"])

    def test_a_user_file_with_the_same_name_replaces_the_builtin(self):
        self.builtin("gh-mine")
        self.user("gh-mine", GOOD.replace("source: s", "source: gh-mine"))
        src = self.loader().load("gh-mine")
        self.assertEqual(src.origin, "user")
        self.assertEqual(src.files, [self.data / "sources" / "gh-mine.yaml"])
        self.assertEqual(src.data["read"]["run"], "echo []")  # nothing of the built-in is left
        self.assertNotIn("author", src.data["form"])
        self.assertEqual([s.name for s in self.loader().load_all()], ["gh-mine"])

    def test_extends_changes_read_every_and_adds_skip_channels(self):
        # Appendix A: the user's file holds only the lines it changes.
        self.builtin("slack-mentions")
        self.user("slack-mentions")
        src = self.loader().load("slack-mentions")
        self.assertEqual(src.origin, "user, extends slack-mentions")
        self.assertEqual(src.files, [self.plugin / "slack-mentions.yaml", self.data / "sources" / "slack-mentions.yaml"])
        self.assertEqual(src.data["read"], {"via": "connector", "tools": ["slack_search_public", "slack_read_thread"],
                                            "query": "<@{me}>", "every": "10m",
                                            "skip_channels": ["#random", "#social"]})
        self.assertNotIn("extends", src.data)
        self.assertEqual(src.data["source"], "slack-mentions")
        self.assertEqual(src.data["form"]["id"], "Message_ts")  # the rest comes from the built-in file

    def test_extends_replaces_lists_and_scalars_and_merges_maps(self):
        self.builtin("slack-mentions")
        self.user("slack-mentions", """\
            extends: slack-mentions
            read:
              tools: [slack_search_public]
            sort:
              fyi: question
            """)
        src = self.loader().load("slack-mentions")
        self.assertEqual(src.data["read"]["tools"], ["slack_search_public"])
        self.assertEqual(src.data["read"]["every"], "15m")
        self.assertEqual(src.sort["fyi"], "question")
        self.assertEqual(src.sort["alert"], "alert")

    def test_extends_merges_nested_maps_key_by_key(self):
        self.builtin("deep", """\
            source: deep
            brings: messages
            read:
              via: mcp
              tool: search
              args:
                a: one
                b: two
            fill: ai
            form: { id: Id, at: At, text: Text }
            link: none
            sort: { all: alert }
            """)
        self.user("deep", """\
            extends: deep
            read:
              args:
                a: changed
            """)
        src = self.loader().load("deep")
        self.assertEqual(src.data["read"], {"via": "mcp", "tool": "search", "args": {"a": "changed", "b": "two"}})

    def test_extends_another_name_makes_a_new_source(self):
        self.user("vendor-status")
        self.user("other-status", """\
            extends: vendor-status
            read:
              every: 1m
            """)
        src = self.loader().load("other-status")
        self.assertEqual(src.data["source"], "other-status")
        self.assertEqual(src.data["read"]["every"], "1m")
        self.assertEqual(src.data["form"]["id"], ".incidents[].id")
        self.assertEqual(self.loader().load("vendor-status").data["read"]["every"], "5m")

    def test_bad_extends_is_refused_naming_the_file(self):
        cases = {
            "no such source": ("extends: nosuch\n", "extends nosuch, but there is no source named nosuch"),
            "own name, no built-in": ("extends: s\n", "extends s, but there is no built-in s"),
            "not a name": ("extends: [a]\n", "extends must name a source, not ['a']"),
        }
        for label, (text, reason) in cases.items():
            with self.subTest(label):
                p = self.user("s", text)
                with self.assertRaises(SourceError) as cm:
                    self.loader().load("s")
                self.assertEqual(str(cm.exception), f"{p}: line 1: {reason}")
                self.assertIsInstance(cm.exception, ConfigError)

    def test_an_extends_cycle_is_refused(self):
        a = self.user("a", "extends: b\n")
        self.user("b", "extends: a\n")
        with self.assertRaises(SourceError) as cm:
            self.loader().load("a")
        self.assertEqual(str(cm.exception), f"{a}: extends makes a cycle (a.yaml -> b.yaml -> a.yaml)")

    def test_source_must_match_the_file_name(self):
        p = self.user("vendor-status", GOOD.replace("source: s", "source: vendor"))
        with self.assertRaises(SourceError) as cm:
            self.loader().load("vendor-status")
        self.assertEqual(str(cm.exception), f"{p}: line 1: source is 'vendor', but the file is named "
                                            "vendor-status.yaml; they must match")

    def test_an_extending_file_that_names_its_source_must_match_too(self):
        self.builtin("slack-mentions")
        p = self.user("slack-mentions", "extends: slack-mentions\nsource: slack\n")
        with self.assertRaisesRegex(SourceError, f"^{p}: line 2: source is 'slack'"):
            self.loader().load("slack-mentions")

    def test_a_file_without_source_or_extends(self):
        p = self.user("s", "brings: messages\n")
        with self.assertRaises(SourceError) as cm:
            self.loader().load("s")
        self.assertEqual(str(cm.exception), f"{p}: source missing (write 'source: s', or 'extends: <name>')")

    def test_a_yaml_error_names_the_file_and_line(self):
        p = self.user("s", "source: s\nread: &x\n")
        with self.assertRaises(SourceError) as cm:
            self.loader().load("s")
        self.assertEqual(str(cm.exception), f"{p}: line 2: anchors ('&') are not supported")

    def test_a_bad_file_name_is_refused(self):
        p = self.user("Vendor Status", GOOD)
        with self.assertRaisesRegex(SourceError, f"^{p}: a source's file name must be lowercase"):
            self.loader().load("Vendor Status")

    def test_unknown_source(self):
        with self.assertRaisesRegex(SourceError, "^no source named 'nosuch'"):
            self.loader().load("nosuch")

    def test_missing_folders_mean_no_sources(self):
        self.assertEqual(Loader(self.tmp / "nowhere", plugin_dir=self.tmp / "none").load_all(), [])

    def test_only_yaml_files_are_sources(self):
        self.user("s", GOOD)
        (self.data / "sources" / "notes.md").write_text("not a source\n")
        (self.data / "sources" / ".hidden.yaml").write_text("not: a source\n")
        (self.data / "sources" / ".raw").mkdir()
        self.assertEqual([s.name for s in self.loader().load_all()], ["s"])

    def test_load_reads_only_the_files_it_needs(self):
        self.user("s", GOOD)
        self.user("broken", "source: broken\nread: &x\n")
        self.assertEqual(self.loader().load("s").name, "s")
        with self.assertRaisesRegex(SourceError, "broken.yaml: line 2"):
            self.loader().load_all()

    def test_the_fixture_files_all_load(self):
        for p in sorted((FIXTURES / "plugin").glob("*.yaml")):
            self.builtin(p.stem)
        for p in sorted((FIXTURES / "user").glob("*.yaml")):
            self.user(p.stem)
        names = [s.name for s in self.loader().load_all()]
        self.assertEqual(names, ["gh-mine", "slack-mentions", "vendor-status"])

    def test_the_default_builtin_folder_is_the_plugins_sources(self):
        self.assertEqual(sources.PLUGIN_DIR, ROOT / "sources")


class ValidationTest(SourceCase):
    """Every bad file is refused with its file name and the reason."""

    CASES = [
        # (what, the change to GOOD as (old, new) or extra text, the expected reason)
        ("unknown top-level key", ("sort:", "raed: x\nsort:"),
         "line 13: unknown key 'raed' (known keys: source, brings, read, fill, form, link, key, sort, key_for, "
         "instructions, status_map, writes, never, plan)"),
        ("brings", ("brings: messages", "brings: tasks"), "line 2: brings must be one of messages, work, not 'tasks'"),
        ("read missing", ("read:\n  via: command\n  run: echo []\n", ""), "read missing"),
        ("read not a map", ("read:\n  via: command\n  run: echo []\n", "read: command\n"),
         "line 3: read must be a map with via, not 'command'"),
        ("via", ("via: command", "via: http"),
         "line 4: read.via must be one of command, mcp, connector, skill, not 'http'"),
        ("via command without run", ("  run: echo []\n", ""), "read.run must be the command to run (via: command)"),
        ("every", ("  run: echo []\n", "  run: echo []\n  every: [5m]\n"),
         "line 6: read.every must be a text such as 15m, not ['5m']"),
        ("timeout", ("  run: echo []\n", "  run: echo []\n  timeout: 0\n"),
         "line 6: read.timeout must be a whole number of seconds from 1 to 600, not 0"),
        ("timeout too long", ("  run: echo []\n", "  run: echo []\n  timeout: 601\n"),
         "line 6: read.timeout must be a whole number of seconds from 1 to 600, not 601"),
        ("fill", ("fill: paths", "fill: magic"), "line 6: fill must be one of paths, ai, not 'magic'"),
        ("form missing", ("form:\n  id: .id\n  at: .at\n  text: .text\n", ""), "form missing"),
        ("form without at", ("  at: .at\n", ""), "line 7: form must name id, at, text; missing: at"),
        ("form without id and text", ("  id: .id\n  at: .at\n  text: .text\n", "  at: .at\n"),
         "line 7: form must name id, at, text; missing: id, text"),
        ("unknown form field", ("  text: .text\n", "  text: .text\n  title: .title\n"),
         "line 11: form: unknown field 'title' (fields: id, thread_id, author, at, text, place, place_id, status, "
         "depends_on, pr)"),
        ("bad path", ("  at: .at\n", "  at: at\n"), "line 9: form.at: a path starts with '.', not 'at'"),
        ("path with an index", ("  at: .at\n", "  at: .times[0]\n"),
         "line 9: form.at: '.times[0]': [0] looks like an index; indexes are not supported"),
        ("path outside the item path", ("  id: .id\n  at: .at\n  text: .text\n",
                                        "  id: .items[].id\n  at: .at\n  text: .items[].text\n"),
         "line 9: form.at: every path must start with the item path of form.id (.items[])"),
        ("a scalar field that fans out", ("  text: .text\n", "  text: .texts[]\n"),
         "line 10: form.text: only depends_on may take several values ([] or a filter after the item path)"),
        ("link missing", ("link: .url\n", ""), "link missing"),
        ("link null", ("link: .url", "link:"), "line 11: link must be a {field} pattern, a path such as .url, or "
                                                "none; not None"),
        ("link pattern brace", ("link: .url", 'link: "https://x.example.com/{id"'),
         "line 11: link: 'https://x.example.com/{id': a brace that is not a {field}"),
        ("key path", ('key: "{id}"', "key: .id"), "line 12: key must be a {field} pattern such as \"{id}\", not '.id'"),
        ("key without a field", ('key: "{id}"', "key: all"),
         "line 12: key must name at least one {field}, or every item would be one card: 'all'"),
        ("key names a list", ('key: "{id}"', 'key: "{depends_on}"'),
         "line 12: key: '{depends_on}': {depends_on} is a list and cannot be named in a pattern"),
        ("sort missing", ("sort: { all: alert }\n", ""), "sort missing"),
        ("sort empty", ("sort: { all: alert }", "sort: {}"),
         "line 13: sort must map each category to a template, update_card or none; not {}"),
        ("sort target", ("sort: { all: alert }", "sort: { all: Big Alert }"),
         "line 13: sort.all must be a template name, update_card or none, not 'Big Alert'"),
        ("work key on a messages source", ("sort:", "never: [comment]\nsort:"),
         "line 13: never is only for brings: work sources"),
        ("work source without status", ("brings: messages", "brings: work"),
         "line 7: form must name id, at, text, status; missing: status"),
        ("instructions not text", ("sort:", "instructions: [a]\nsort:"), "line 13: instructions must be a text"),
        ("source not a name", ("source: s", "source: 5"), None),
    ]

    def test_bad_files(self):
        for what, change, reason in self.CASES:
            with self.subTest(what):
                if isinstance(change, tuple):
                    old, new = change
                    self.assertIn(old, GOOD, what)
                    text = GOOD.replace(old, new, 1)
                p = self.user("s", text)
                with self.assertRaises(SourceError) as cm:
                    self.loader().load("s")
                if reason is None:  # `source: 5` is read as a number; it cannot match the file name
                    self.assertIn("source is 5, but the file is named s.yaml", str(cm.exception))
                else:
                    self.assertEqual(str(cm.exception), f"{p}: {reason}")

    WORK = """\
        source: w
        brings: work
        read:
          via: mcp
          server: tracker
          tool: search_issues
        fill: paths
        form:
          id: .key
          at: .fields.updated
          text: .fields.summary
          status: .fields.status.name
          depends_on: .fields.issuelinks[is blocked by].key
        link: "https://tracker.example.com/browse/{id}"
        key: "{id}"
        status_map: { "To Do": todo, "In Progress": in_progress, "Done": done }
        sort: { all: code }
        writes:
          on_start: { transition: "In Progress", assign: me }
          on_merge: { transition: "Done" }
        never: [create_ticket, comment]
        plan: split_here
        instructions: "Split a big ticket into PR-sized tasks."
        """

    def test_work_keys_and_instructions_pass_on_a_work_source(self):
        self.user("w", self.WORK)
        src = self.loader().load("w")
        self.assertEqual(src.brings, "work")
        self.assertEqual(src.data["never"], ["create_ticket", "comment"])
        self.assertEqual(src.paths["depends_on"],
                         (("key", "fields"), ("key", "issuelinks"), ("match", None, "is blocked by"), ("key", "key")))

    def test_bad_work_keys(self):
        cases = [
            ("on_merge:", "on_close:", "line 20: writes: unknown event 'on_close' (events: on_start, on_pr, on_merge)"),
            ("never: [create_ticket, comment]", "never: comment", "line 21: never must be a list of what the engine "
                                                                  "must not do"),
            ('status_map: { "To Do": todo, "In Progress": in_progress, "Done": done }', "status_map: [todo]",
             "line 16: status_map must map each status there to a card status"),
            ("plan: split_here", "plan: [a]", "line 22: plan must be a text such as split_here"),
        ]
        for old, new, reason in cases:
            with self.subTest(new):
                text = textwrap.dedent(self.WORK)
                self.assertIn(old, text)
                p = self.user("w", text.replace(old, new))
                with self.assertRaises(SourceError) as cm:
                    self.loader().load("w")
                self.assertEqual(str(cm.exception), f"{p}: {reason}")

    def test_fill_ai_needs_text_hints_and_no_link_path(self):
        self.builtin("slack-mentions")
        p = self.user("slack-mentions", "extends: slack-mentions\nlink: .permalink\n")
        with self.assertRaises(SourceError) as cm:
            self.loader().load("slack-mentions")
        self.assertEqual(str(cm.exception), f"{p} (merged over {self.plugin / 'slack-mentions.yaml'}): line 2: link: "
                                            "a path needs fill: paths (a fill: ai record has no JSON to read it from)")
        self.user("slack-mentions", "extends: slack-mentions\nform:\n  text: ''\n")
        with self.assertRaisesRegex(SourceError, r"form\.text must say what fills it, not ''"):
            self.loader().load("slack-mentions")

    def test_key_defaults_to_the_source_and_id(self):
        self.user("s", GOOD.replace('key: "{id}"\n', ""))
        src = self.loader().load("s")
        self.assertEqual(src.key, form.parse_pattern("{source}:{id}"))

    def test_link_kinds(self):
        self.user("s", GOOD)
        self.assertEqual(self.loader().load("s").link, ("path", (("key", "url"),)))
        self.user("s", GOOD.replace("link: .url", "link: none"))
        self.assertEqual(self.loader().load("s").link, ("none",))
        self.user("s", GOOD.replace("link: .url", 'link: "https://x.example.com/{id}"'))
        self.assertEqual(self.loader().load("s").link,
                         ("pattern", (("text", "https://x.example.com/"), ("field", "id"))))


class KeyForTest(SourceCase):
    """`key_for: {<category>: <pattern>}`: the records of one category key on their own pattern (design 6.2)."""

    KEYED = GOOD.replace("sort: { all: alert }\n",
                         'sort: { all: alert, review_request: pr-review }\nkey_for: { review_request: "{pr}" }\n')

    def test_key_for_loads_into_parsed_patterns(self):
        self.user("s", self.KEYED)
        src = self.loader().load("s")
        self.assertEqual(src.key_for, {"review_request": form.parse_pattern("{pr}")})
        self.assertEqual(src.key, form.parse_pattern("{id}"))  # every other category keys as before

    def test_a_source_without_key_for_gets_an_empty_map(self):
        self.user("s", GOOD)
        self.assertEqual(self.loader().load("s").key_for, {})

    def test_bad_key_for_is_refused_naming_its_path(self):
        cases = [
            ('key_for: { review_request: "{pr}" }', 'key_for: { other: "{pr}" }',
             "line 14: key_for.other: 'other' is not one of sort's categories (all, review_request)"),
            ('key_for: { review_request: "{pr}" }', "key_for: { review_request: pr }",
             "line 14: key_for.review_request must name at least one {field}, or every such item would be one "
             "card: 'pr'"),
            ('key_for: { review_request: "{pr}" }', "key_for: { review_request: .pr }",
             "line 14: key_for.review_request must be a {field} pattern such as \"{pr}\", not '.pr'"),
            ('key_for: { review_request: "{pr}" }', "key_for: { review_request: \"\" }",
             "line 14: key_for.review_request must be a {field} pattern such as \"{pr}\", not ''"),
            ('key_for: { review_request: "{pr}" }', 'key_for: { review_request: "{pr" }',
             "line 14: key_for.review_request: '{pr': a brace that is not a {field}"),
            ('key_for: { review_request: "{pr}" }', "key_for: [review_request]",
             "line 14: key_for must map a category of sort to a {field} pattern, not ['review_request']"),
            # Block style: the entry's own line, not the key_for line above it.
            ('key_for: { review_request: "{pr}" }', 'key_for:\n  other: "{pr}"',
             "line 15: key_for.other: 'other' is not one of sort's categories (all, review_request)"),
        ]
        for old, new, reason in cases:
            with self.subTest(new):
                self.assertIn(old, self.KEYED)
                p = self.user("s", self.KEYED.replace(old, new))
                with self.assertRaises(SourceError) as cm:
                    self.loader().load("s")
                self.assertEqual(str(cm.exception), f"{p}: {reason}")

    def test_the_builtin_slack_mentions_keys_a_review_request_on_its_pr(self):
        # The real built-in file (sources.PLUGIN_DIR), not the test copy.
        src = Loader(self.data, plugin_dir=sources.PLUGIN_DIR).load("slack-mentions")
        self.assertEqual(src.origin, "built-in")
        self.assertIn("pr", src.data["form"])
        self.assertIn("github.com/<owner>/<repo>/pull/<number>", src.data["form"]["pr"])
        self.assertEqual(src.sort, {"question": "question", "ask_to_do": "errand", "alert": "alert", "fyi": "none",
                                    "review_request": "pr-review"})
        self.assertEqual(src.key_for, {"review_request": form.parse_pattern("{pr}")})
        self.assertEqual(src.data["key"], "{place_id}:{thread_or_id}")  # the thread key is unchanged
        self.assertIn("review_request", src.data["instructions"])

    def test_a_user_null_turns_a_key_for_category_off(self):
        self.user("slack-mentions", "extends: slack-mentions\nsettings: { me: U1 }\n"
                                    "key_for: { review_request: null }\n")
        src = Loader(self.data, plugin_dir=sources.PLUGIN_DIR).load("slack-mentions")
        self.assertEqual(src.key_for, {})  # a review ask keys on `key`, its thread, again

    def test_an_empty_setting_named_in_key_for_refuses_the_source(self):
        p = self.user("slack-mentions", "extends: slack-mentions\nsettings: { me: U1, org: '' }\n"
                                        "key_for: { review_request: \"{org}/{pr}\" }\n")
        src = Loader(self.data, plugin_dir=sources.PLUGIN_DIR).load("slack-mentions")
        with self.assertRaises(SourceError) as cm:
            sources.unset_settings(src)
        self.assertEqual(str(cm.exception), f"slack-mentions: settings.org is not set; add it in {p}")


if __name__ == "__main__":
    unittest.main()
