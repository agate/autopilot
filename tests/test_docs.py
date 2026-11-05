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

    def test_replies_are_handled_once(self):
        rb = (ROOT / "RUNBOOK.md").read_text()
        step = rb[rb.index("**Replies.**"):rb.index("7. **Sync the source.**")]
        self.assertIn("`unchanged`", step)          # a reply seen twice is not acted on again
        self.assertIn("Exit 3", step)               # a second, different answer becomes a card
        self.assertIn("two_answers", step)
        self.assertIn("--replace", step)
        talk = (ROOT / "references" / "talk.md").read_text()
        self.assertIn("--replace", talk)
        labels = (ROOT / "templates" / "labels.en.json").read_text()
        self.assertIn('"two_answers"', labels)

    def test_a_task_without_a_pr_gets_evidence(self):
        # Only the PR worker wrote evidence.json, so a research task closed with nothing on the page.
        rb = (ROOT / "RUNBOOK.md").read_text()
        self.assertIn("`No PR`", rb[rb.index("8. **Fill every free slot.**"):rb.index("9. **Report.**")])
        review = rb[rb.index("4. **Reviewer done**"):rb.index("5. **PR loop done**")]
        self.assertIn("`No PR`", review)
        self.assertIn("`ap evidence check <ap> <task>`", review)
        impl = (ROOT / "agents" / "implementer.md").read_text()
        self.assertIn("`No PR`", impl)
        self.assertIn("evidence.json", impl)
        self.assertIn("evidence add", impl)

    def test_logs_go_up_under_their_upload_name(self):
        rb = (ROOT / "RUNBOOK.md").read_text()
        self.assertIn("`upload_name`", rb[rb.index("10. **Tidy and publish.**"):])

    def test_publish_records_uploads_before_the_batch(self):
        # Two passes stalled improvising this step: ids missing from the rows, versions read into a refused folder.
        rb = (ROOT / "RUNBOOK.md").read_text()
        step = rb[rb.index("10. **Tidy and publish.**"):rb.index("11. **End.**")]
        for s in ("`ap publish assets", "`ap publish diff` again", "`if_version`", "without `out_dir`", "`ap publish mark`"):
            self.assertIn(s, step, s)
        self.assertLess(step.index("`ap publish assets"), step.index("`ap publish diff` again"))
        self.assertLess(step.index("`ap publish diff` again"), step.index("`ap publish mark`"))
        loop = (ROOT / "commands" / "pr-loop.md").read_text()
        self.assertIn('"logs": [{"path": "media/', loop)

    def test_page_update_is_republished_in_place(self):
        init = (ROOT / "references" / "init.md").read_text()
        step = init[init.index("4. The page"):init.index("5. Sources")]
        self.assertIn("`ap page build`", step[step.index("Later inits"):])
        self.assertIn("without `capabilities`", step)

    def test_only_the_owner_writes_replies(self):
        init = (ROOT / "references" / "init.md").read_text()
        self.assertIn('{"path": "replies", "read": "view", "write": "owner"}', init)

    def test_init_does_not_import_rules(self):
        # Sessions in the repo and its worktrees already load its CLAUDE.md/AGENTS.md, .claude settings and
        # skills, and the user's memory for that repo (checked live 2026-11-04); copying them would drift.
        init = (ROOT / "references" / "init.md").read_text()
        self.assertNotIn("- Offer to import rules", init)
        self.assertIn("Do not offer to import rules", init)

    def test_presets_exist(self):
        for name in ("jira-epic", "jira-ticket", "idea", "custom"):
            self.assertTrue((ROOT / "presets" / f"{name}.md").exists(), name)

    def test_docs_clean(self):
        self.assertEqual(checkpkg.scan(ROOT, checkpkg.identities()), [])


    def test_runbook_end_line_asks_nothing(self):
        # Claude Code labels a background session "blocked" when its last message asks the user for
        # something, so the pass's last line must not; the user's to-dos live on cards.
        end = (ROOT / "RUNBOOK.md").read_text().split("**End.**", 1)[1].split("\n## ", 1)[0]
        self.assertIn("asks nothing of the user", end)
        self.assertIn("card", end)

    def test_runbook_runs_floor_commands_on_their_own(self):
        rb = (ROOT / "RUNBOOK.md").read_text()
        self.assertIn("gh pr ready <n>", rb)
        self.assertIn("authority.open_prs", rb)
        self.assertIn("Never run `claude stop`", rb)
        self.assertIn("written out in full", rb)

    def test_jira_epic_jql_has_the_project_clause(self):
        pre = (ROOT / "presets" / "jira-epic.md").read_text()
        self.assertIn("`project = <project> AND parent = <key>`", pre)
        self.assertIn("before the last `-`", pre)
        self.assertNotIn("JQL `parent = <key>`", pre)

    def test_readme_names_the_dispatcher_allow_rules(self):
        rd = (ROOT / "README.md").read_text()
        for word in ("gh pr ready", "ap worker end", "authority.source_status", "authority.open_prs"):
            self.assertIn(word, rd)


if __name__ == "__main__":
    unittest.main()
