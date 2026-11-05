from __future__ import annotations

import json
import re
import shutil
import subprocess
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

    def test_logs_load_as_text_never_as_images(self):
        self.assertIn("(ev.logs || []).length", PAGE)
        self.assertIn('L("logs")', PAGE)
        body = PAGE[PAGE.index("function logItem"):PAGE.index("function renderDetail")]
        self.assertIn('fetch("/_blob/"', body)
        self.assertIn(".textContent = await", body)
        self.assertNotIn('"img"', body)
        self.assertNotIn("innerHTML", body)

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


    def test_children_are_set_only_through_fill(self):
        # DOM replaceChildren turns a null child into the text "null"; renderDetail returns null for every
        # missing card. fill() drops null and false, like el() does.
        self.assertEqual(PAGE.count("replaceChildren("), 1)
        body = PAGE[PAGE.index("function fill("):]
        self.assertIn("replaceChildren(", body[:body.index("\n}")])
        self.assertIn("fill(box, ...renderDetail())", PAGE)

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_fill_drops_missing_cards(self):
        src = PAGE[PAGE.index("function fill("):]
        src = src[:src.index("\n}") + 2]
        js = src + """
const got = [];
fill({ replaceChildren: (...k) => got.push(...k) }, "a", null, ["b", null, false], false, "c");
console.log(JSON.stringify(got));
"""
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.stdout.strip(), '["a","b","c"]', out.stderr)


    def test_a_missing_card_says_why_it_is_empty(self):
        # Every card the detail view can leave out keeps its heading and says, in one muted line, what
        # it would show and why it is empty for now.
        body = PAGE[PAGE.index("function renderDetail"):PAGE.index("async function exportOne")]
        for key in ("none_criteria", "none_screenshots", "none_logs", "none_rounds", "none_links"):
            self.assertIn(f'L("{key}")', body)
            self.assertTrue(LABELS.get(key), key)
        self.assertIn("function emptyCard(", PAGE)
        self.assertEqual(LABELS["criteria_title"], "Acceptance criteria")
        self.assertNotIn(") : null,\n    (ev.logs", body)


if __name__ == "__main__":
    unittest.main()
