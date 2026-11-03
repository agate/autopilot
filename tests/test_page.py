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
    def test_parses_and_has_tabs(self):
        p = Ids()
        p.feed(PAGE)
        for i in ("tab-dashboard", "tab-tickets", "tab-decisions", "detail"):
            self.assertIn(i, p.ids)

    def test_every_label_key_defined(self):
        keys = set(re.findall(r'data-l="(\w+)"', PAGE)) | set(re.findall(r'\bL\("(\w+)"[,)]', PAGE))
        self.assertTrue(keys)
        self.assertEqual(sorted(k for k in keys if k not in LABELS), [])
        for prefix, names in (("role_", ("implement", "review", "pr")),
                              ("status_", ("todo", "in_progress", "in_review", "done", "blocked", "skipped")),
                              ("group_", ("running", "ready", "waiting"))):
            for n in names:
                self.assertIn(prefix + n, LABELS)

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
