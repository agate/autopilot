from __future__ import annotations

import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.helpers import ROOT
from assistant import form
from assistant.config import Config
from assistant.engine import Engine
from assistant.form import PathError, evaluate, parse_path
from assistant.sources import Loader

FIXTURES = ROOT / "tests" / "fixtures" / "ap2" / "sources"


def reply(name):
    return json.loads((FIXTURES / "replies" / name).read_text(encoding="utf-8"))


class PathLanguageTest(unittest.TestCase):
    DOC = {"a": {"b": 1, "c d": "x", "list": [{"n": 1, "t": "k"}, {"n": 2, "t": "m"}, 3]},
           "links": [{"type": "is blocked by", "key": "P-1"}, {"type": "blocks", "key": "P-2"},
                     {"type": {"inward": "is blocked by"}, "inwardIssue": {"key": "P-3"}}]}

    def test_parse(self):
        self.assertEqual(parse_path("."), ())
        self.assertEqual(parse_path(".a.b"), (("key", "a"), ("key", "b")))
        self.assertEqual(parse_path('.a."c d"'), (("key", "a"), ("key", "c d")))
        self.assertEqual(parse_path(".a.list[].n"), (("key", "a"), ("key", "list"), ("each",), ("key", "n")))
        self.assertEqual(parse_path(".[].n"), (("each",), ("key", "n")))
        self.assertEqual(parse_path(".[]"), (("each",),))
        self.assertEqual(parse_path(".links[is blocked by].key"),
                         (("key", "links"), ("match", None, "is blocked by"), ("key", "key")))
        self.assertEqual(parse_path(".links[type.inward=is blocked by].inwardIssue.key"),
                         (("key", "links"), ("match", ("type", "inward"), "is blocked by"), ("key", "inwardIssue"),
                          ("key", "key")))
        self.assertEqual(parse_path(".links[a b=c]"), (("key", "links"), ("match", None, "a b=c")))

    def test_parse_refuses(self):
        cases = {"a.b": "a path starts with '.'", "": "a path starts with '.'", ".a..b": "expected a name after '.'",
                 ".a[": "unclosed '['", '."a': "unclosed quote", '.""': "empty quoted name",
                 ".a[0]": "looks like an index", ".a[ x]": "starts or ends with a space", ".a b": "unexpected ' '",
                 ".a.": "expected a name after '.'"}
        for text, reason in cases.items():
            with self.subTest(text):
                with self.assertRaisesRegex(PathError, re.escape(reason)):
                    parse_path(text)
        with self.assertRaises(PathError):
            parse_path(5)

    def test_evaluate(self):
        doc = self.DOC
        self.assertEqual(evaluate(parse_path("."), doc), doc)
        self.assertEqual(evaluate(parse_path(".a.b"), doc), 1)
        self.assertEqual(evaluate(parse_path('.a."c d"'), doc), "x")
        self.assertIs(evaluate(parse_path(".a.nope"), doc), form.MISSING)
        self.assertIs(evaluate(parse_path(".a.b.c"), doc), form.MISSING)  # a key on a number gives nothing
        self.assertEqual(evaluate(parse_path(".a.list[].n"), doc), [1, 2])  # 3 has no .n
        self.assertEqual(evaluate(parse_path(".a.b[]"), doc), [])  # [] on a number gives nothing
        self.assertEqual(evaluate(parse_path(".links[is blocked by].key"), doc), ["P-1"])
        self.assertEqual(evaluate(parse_path(".links[blocks].key"), doc), ["P-2"])
        self.assertEqual(evaluate(parse_path(".links[type.inward=is blocked by].inwardIssue.key"), doc), ["P-3"])
        self.assertEqual(evaluate(parse_path(".a.list[t=m].n"), doc), [2])

    def test_a_filter_matches_text_word_for_word_at_any_depth(self):
        doc = [{"x": {"y": ["is blocked by"]}}, {"x": "is blocked by now"}, {"x": "IS BLOCKED BY"}]
        self.assertEqual(evaluate(parse_path(".[is blocked by]"), doc), [doc[0]])

    def test_patterns(self):
        parts = form.parse_pattern("https://x.example.com/{place}/p{id_digits}")
        self.assertEqual(parts, (("text", "https://x.example.com/"), ("field", "place"), ("text", "/p"),
                                 ("field", "id_digits")))
        self.assertEqual(form.fill_pattern(parts, {"place": "a b/c", "id_digits": "1"}, url=True),
                         "https://x.example.com/a%20b%2Fc/p1")
        self.assertEqual(form.fill_pattern(form.parse_pattern("{place}#{id}"), {"place": "a b", "id": "7"}), "a b#7")
        with self.assertRaisesRegex(ValueError, r"^\{id\} missing$"):
            form.fill_pattern(form.parse_pattern("{id}"), {})
        for bad in ("{", "}", "a{b", "{Id}", "{ id}", "{a}}", ""):
            with self.subTest(bad), self.assertRaises(PathError):
                form.parse_pattern(bad)

    def test_derived_fields(self):
        rec = {"id": "1730000000.123456", "place": "C0123ABCD", "text": "t", "at": "x", "depends_on": ["a"]}
        values = form.derived(rec, "slack-mentions")
        self.assertEqual(values["id_digits"], "1730000000123456")
        self.assertEqual(values["place_id"], "C0123ABCD")
        self.assertEqual(values["source"], "slack-mentions")
        self.assertNotIn("depends_on", values)
        self.assertEqual(form.derived({**rec, "place": "#general", "place_id": "C0999"}, "s")["place_id"], "C0999")
        self.assertNotIn("place_id", form.derived({"id": "1"}, "s"))


class FillCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-form-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.plugin = self.tmp / "plugin"
        self.data = self.tmp / "data"
        shutil.copytree(FIXTURES / "plugin", self.plugin)
        shutil.copytree(FIXTURES / "user", self.data / "sources")
        self.loader = Loader(self.data, plugin_dir=self.plugin)
        self.engine = Engine(self.data, cfg=Config(), plugin_dir=self.plugin)


class FillPathsTest(FillCase):
    def test_gh_pr_list_reply(self):
        src = self.loader.load("gh-mine")
        entries, dropped = Engine.fill(src, reply("gh-pr-list.json"))
        self.assertEqual([e["record"] for e in entries], [
            {"id": "42", "at": "2026-11-09T17:02:11Z", "text": "Retry failed card payments once, 2 seconds later",
             "author": "octo-dev", "place": "widgets"},
            {"id": "7", "at": "2026-11-10T08:45:00Z", "text": "Show the delivery date on the order page",
             "author": "octo-dev", "place": "gadgets"}])
        self.assertEqual([e["link"] for e in entries], ["https://github.com/example/widgets/pull/42",
                                                        "https://github.com/example/gadgets/pull/7"])
        # the third PR has an empty title: dropped, and the reason kept
        self.assertEqual(dropped, [{"item": 2, "reason": "form.text is empty"}])
        self.assertEqual([self.engine.link_and_key(src, e["record"], e["link"])[1] for e in entries],
                         ["widgets#42", "gadgets#7"])

    def test_status_page_reply(self):
        # Appendix C: every path starts with .incidents[], one record per incident.
        src = self.loader.load("vendor-status")
        self.assertEqual(src.prefix, (("key", "incidents"), ("each",)))
        entries, dropped = Engine.fill(src, reply("status-incidents.json"))
        self.assertEqual([e["record"] for e in entries], [
            {"id": "inc01abcd", "at": "2026-11-10T13:40:05.000Z", "text": "Elevated error rates on the payments API"},
            {"id": "inc02efgh", "at": "2026-11-10T12:10:00.000Z", "text": "Delayed webhooks"}])
        self.assertEqual([e["link"] for e in entries], ["https://status.example.com/incidents/inc01abcd",
                                                        "https://status.example.com/incidents/inc02efgh"])
        self.assertEqual(dropped, [{"item": 2, "reason": "form.text missing"}])

    def test_issuelinks_filter_form(self):
        # Appendix B's depends_on path, and the explicit [field=text] form for links shaped like Jira's own.
        paths = {"id": parse_path(".key"), "at": parse_path(".fields.updated"), "text": parse_path(".fields.summary"),
                 "status": parse_path(".fields.status.name"),
                 "depends_on": parse_path(".fields.issuelinks[is blocked by].key")}
        items = form.items((), reply("jira-search.json"))
        first = form.fill_item(paths, (), items[0])
        self.assertEqual(first, {"id": "PROJ-123", "at": "2026-11-10T10:00:00.000+0000",
                                 "text": "Retry failed card payments", "status": "In Progress",
                                 "depends_on": ["PROJ-120", "PROJ-121"]})
        self.assertNotIn("depends_on", form.fill_item(paths, (), items[1]))  # nothing blocks it: left out
        paths["depends_on"] = parse_path(".fields.issuelinks[type.inward=is blocked by].inwardIssue.key")
        self.assertEqual(form.fill_item(paths, (), items[1])["depends_on"], ["PROJ-123"])

    def test_a_field_of_the_wrong_type_drops_the_record(self):
        paths = {"id": parse_path(".id"), "at": parse_path(".at"), "text": parse_path(".text"),
                 "author": parse_path(".author")}
        cases = [({"id": 1, "at": "t", "text": "x", "author": {"login": "a"}}, "form.author is an object, not a text"),
                 ({"id": True, "at": "t", "text": "x"}, "form.id is true/false, not a text"),
                 ({"id": 1.5, "at": "t", "text": "x"}, "form.id is a decimal number, not a text"),
                 ({"id": 1, "at": None, "text": "x"}, "form.at missing"),
                 ({"id": 1, "at": "t", "text": "  "}, "form.text is empty")]
        for item, reason in cases:
            with self.subTest(reason), self.assertRaises(ValueError) as cm:
                form.fill_item(paths, (), item)
            self.assertEqual(str(cm.exception), reason)
        # a missing optional field is simply left out
        self.assertEqual(form.fill_item(paths, (), {"id": 1, "at": "t", "text": "x"}), {"id": "1", "at": "t", "text": "x"})

    def test_a_reply_that_is_one_object_is_one_item(self):
        self.assertEqual(form.items((), {"a": 1}), [{"a": 1}])
        self.assertEqual(form.items((), [1, 2]), [1, 2])
        self.assertEqual(form.items(parse_path(".x[]"), {"y": 1}), [])

    def test_no_model_decides_any_field(self):
        # The same reply always fills the same records: plain code, no session, no network.
        src = self.loader.load("vendor-status")
        self.assertEqual(Engine.fill(src, reply("status-incidents.json")),
                         Engine.fill(src, reply("status-incidents.json")))


class LinkAndKeyTest(FillCase):
    SLACK = {"id": "1730000000.123456", "thread_id": "1729999000.000100", "author": "U0EXAMPLE1",
             "at": "1730000000.123456", "text": "Can you look at the retry change?", "place": "C0123ABCD"}

    def test_slack_link_of_appendix_a(self):
        src = self.loader.load("slack-mentions")
        link, key = self.engine.link_and_key(src, self.SLACK)
        self.assertEqual(link, "https://slack.com/archives/C0123ABCD/p1730000000123456"  # checkpkg: ignore
                               "?thread_ts=1729999000.000100&cid=C0123ABCD")
        self.assertEqual(key, "C0123ABCD:1729999000.000100")

    def test_a_pattern_naming_an_empty_value_is_refused(self):
        for value in ("", "  "):
            with self.subTest(repr(value)), self.assertRaises(ValueError) as cm:
                form.fill_pattern(form.parse_pattern("{place_id}:{thread_id}"), {"place_id": "C1", "thread_id": value})
            self.assertEqual(str(cm.exception), "{thread_id} is empty")

    def test_a_pattern_naming_a_missing_field_drops_the_record(self):
        src = self.loader.load("slack-mentions")
        rec = {k: v for k, v in self.SLACK.items() if k != "thread_id"}
        with self.assertRaises(ValueError) as cm:
            self.engine.link_and_key(src, rec)
        self.assertEqual(str(cm.exception), "key: {thread_id} missing")
        result = self.engine.build(src, [{"item": 0, "record": rec, "link": None, "category": "question"}],
                                   dry_run=True)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["dropped"], [{"item": 0, "reason": "key: {thread_id} missing"}])

    def test_a_link_path_must_give_an_http_link(self):
        src = self.loader.load("vendor-status")
        incidents = reply("status-incidents.json")
        incidents["incidents"][0]["shortlink"] = "javascript:alert(1)"
        del incidents["incidents"][1]["shortlink"]
        entries, dropped = Engine.fill(src, incidents)
        self.assertEqual(dropped[0], {"item": 1, "reason": "link missing"})
        result = self.engine.build(src, entries, dropped, dry_run=True)
        self.assertEqual(result["dropped"][-1], {"item": 0, "reason": "link 'javascript:alert(1)' is not an http(s) link"})
        self.assertEqual(result["items"], [])


if __name__ == "__main__":
    unittest.main()
