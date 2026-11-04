from __future__ import annotations

import re
import unittest

from tests.helpers import ROOT
from autopilot import checkpkg, cli

DOCS = [p for p in ROOT.rglob("*.md") if ".superpowers" not in p.parts and "tests" not in p.parts]


def front(path):
    text = path.read_text()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return dict(l.split(":", 1) for l in m.group(1).splitlines() if ":" in l) if m else {}


class DocsTest(unittest.TestCase):
    def test_skill_frontmatter(self):
        f = front(ROOT / "SKILL.md")
        self.assertEqual(f["name"].strip(), "autopilot")
        self.assertEqual(f["disable-model-invocation"].strip(), "true")
        self.assertTrue(f["description"].strip())

    def test_skill_under_limit(self):
        self.assertLess(len((ROOT / "SKILL.md").read_text().splitlines()), 150)

    def test_every_ap_command_mentioned_exists(self):
        cli.load_all()
        used = set()
        for p in DOCS:
            used |= set(re.findall(r"`ap ([a-z][a-z-]*)", p.read_text()))
        self.assertTrue(used)
        self.assertEqual(sorted(used - set(cli.COMMANDS)), [])

    def test_agents_frontmatter_names(self):
        self.assertEqual(front(ROOT / "agents" / "implementer.md")["name"].strip(), "implementer")
        self.assertEqual(front(ROOT / "agents" / "reviewer.md")["name"].strip(), "reviewer")

    def test_runbook_names_the_key_steps(self):
        rb = (ROOT / "RUNBOOK.md").read_text()
        for cmd in ("ap budget", "ap worker list", "ap worker start", "ap merge-check", "ap next --json",
                    "ap publish diff", "ap publish mark", "ap pass end", "ap report due", "ap tasks sync"):
            self.assertIn(cmd, rb, cmd)

    def test_init_asks_then_installs(self):
        text = (ROOT / "references" / "init.md").read_text()
        self.assertIn("`ap install --check`", text)
        self.assertIn("run `ap install` with the Bash tool", text)
        self.assertIn("`ap install --print`", text)
        self.assertNotIn("First init only: run `ap install --print`", text)
        self.assertNotIn("Install the launchd job yourself", (ROOT / "SKILL.md").read_text())

    def test_presets_exist(self):
        for name in ("jira-epic", "jira-ticket", "idea", "custom"):
            self.assertTrue((ROOT / "presets" / f"{name}.md").exists(), name)

    def test_docs_clean(self):
        self.assertEqual(checkpkg.scan(ROOT, checkpkg.identities()), [])


if __name__ == "__main__":
    unittest.main()
