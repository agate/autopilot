from __future__ import annotations

import json
import re
import unittest
from html.parser import HTMLParser

from tests.helpers import ROOT, TempData
from autopilot import publish

PAGE = (ROOT / "templates" / "page.html").read_text()
LABELS = json.loads((ROOT / "templates" / "labels.en.json").read_text())
ALLOWED = ("https://cdnjs.cloudflare.com/", "https://cdn.jsdelivr.net/npm/", "https://unpkg.com/")


class Ids(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids, self.scripts = set(), []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a:
            self.ids.add(a["id"])
        if tag == "script" and a.get("src"):
            self.scripts.append(a["src"])


class PageTest(unittest.TestCase):
    def test_parses_and_has_the_three_views(self):
        # Inbox layout: what needs you, the tasks, the status panel; a bottom tab bar on phones.
        p = Ids()
        p.feed(PAGE)
        for i in ("v-needs", "v-tasks", "v-status", "tabbar", "filters", "lang", "status-note"):
            self.assertIn(i, p.ids)

    def test_every_label_key_defined(self):
        keys = set(re.findall(r'data-l="(\w+)"', PAGE)) | set(re.findall(r'\bL\("(\w+)"[,)]', PAGE))
        self.assertTrue(keys)
        self.assertEqual(sorted(k for k in keys if k not in LABELS), [])
        for prefix, names in (("role_", ("implement", "review", "pr")),
                              ("status_", ("todo", "in_progress", "in_review", "done", "blocked", "skipped")),
                              ("group_", ("running", "ready", "waiting")),
                              ("tab_", ("needs", "tickets", "status"))):
            for n in names:
                self.assertIn(prefix + n, LABELS)
        self.assertEqual(LABELS["lang_short"], "EN")

    def test_english_by_default_and_the_choice_is_remembered(self):
        self.assertIn('let lang = "en"', PAGE)
        self.assertIn('localStorage.getItem("autopilot-lang")', PAGE)
        self.assertIn('localStorage.setItem("autopilot-lang"', PAGE)

    def test_a_reply_names_who_sent_it_and_only_the_owner_answers(self):
        self.assertIn('db.collection("replies").add(', PAGE)
        self.assertIn("by: myId", PAGE)
        self.assertIn("user.isOwner()", PAGE)

    def test_says_when_an_answer_is_picked_up(self):
        self.assertIn("S.meta.reply_check", PAGE)
        for k in ("pickup_minute", "pickup_later", "pickup_paused"):
            self.assertIn(f'L("{k}"', PAGE)
            self.assertIn(k, LABELS)

    def test_no_prototype_leftovers(self):
        for s in ("SAMPLE", "/*@SHARED@*/", "LABELS_EXTRA", "proto_note", "原型", "Prototype"):
            self.assertNotIn(s, PAGE)

    def test_only_allowed_script_hosts(self):
        p = Ids()
        p.feed(PAGE)
        self.assertEqual([s for s in p.scripts if not s.startswith(ALLOWED)], [])

    def test_build_injects_labels(self):
        with TempData() as d:
            out = publish.build_page()
            html = out.read_text()
            self.assertNotIn("/*LABELS_EN*/null", html)
            self.assertIn('"tab_dashboard"', html)
            self.assertEqual(out, d / "page.html")


if __name__ == "__main__":
    unittest.main()
