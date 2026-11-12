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

    def test_profile_and_cap_replies_go_through_reply_kind(self):
        rb = (ROOT / "RUNBOOK.md").read_text()
        step = rb[rb.index("**Replies.**"):rb.index("7. **Sync the source.**")]
        self.assertIn("ap reply-kind", step)
        self.assertIn("never `ap decision resolve` it", step)
        self.assertIn("same `ArtifactData` batch", step)
        tidy = rb[rb.index("**Tidy and publish.**"):rb.index("11. **End.**")]
        self.assertIn("ap reply-kind", tidy)
        for doc in ("README.md", "SKILL.md"):
            self.assertNotIn("up to 30 minutes", (ROOT / doc).read_text(), doc)

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


    def test_access_problems_go_on_system_cards(self):
        rb = (ROOT / "RUNBOOK.md").read_text()
        self.assertIn("ap access fail <ap> <check>", rb)
        self.assertIn("ap access ok <ap> <check>", rb)
        self.assertIn("never an `ask` card", rb)
        cfg = (ROOT / "references" / "access.md").read_text()
        for word in ("health_checks", "mcp_servers", "ap access check <ap>", "exits 0"):
            self.assertIn(word, cfg)
        self.assertIn("System access", (ROOT / "README.md").read_text())


    def test_runbook_runs_the_merge_on_its_own(self):
        rb = (ROOT / "RUNBOOK.md").read_text()
        head = rb[:rb.index("## Authority")]
        for cmd in ("`ap worker end`", "`gh pr ready`", "`gh pr merge`"):
            self.assertIn(cmd, head)
        step = rb[rb.index("**Merge check**"):]
        self.assertIn("as its own command, written out in full", step)
        self.assertIn("`gh pr merge --match-head-commit <head sha> --<merge_method> <n>`", step)
        self.assertNotIn("gh pr merge <n>", rb)
        rd = (ROOT / "README.md").read_text()
        self.assertIn("`gh pr merge --match-head-commit`", rd)
        self.assertIn("`authority.merge`", rd)

    def test_self_decisions_are_flagged_read_and_reminded(self):
        rb = (ROOT / "RUNBOOK.md").read_text()
        auth = rb[rb.index("## Authority"):rb.index("## System access")]
        self.assertIn("`attention`", auth)                       # every self card says whether it wants a look
        begin = rb[rb.index("1. **Begin.**"):rb.index("2. **Check workers.**")]
        self.assertIn("ap decision attention <ap> <D-id> true|false", begin)   # older cards get the flag once
        step = rb[rb.index("**Replies.**"):rb.index("7. **Sync the source.**")]
        self.assertIn('`option: "keep"`', step)
        self.assertIn("ap decision keep <ap> <D-id>", step)
        self.assertIn("`decisions`", step)                        # the page's "mark all read" reply
        report = rb[rb.index("9. **Report.**"):rb.index("10. **Tidy and publish.**")]
        self.assertIn("ap decision unread <ap>", report)
        self.assertIn("`remind`", report)
        late = rb[rb.index("10. **Tidy and publish.**"):rb.index("11. **End.**")]
        self.assertIn("ap decision keep", late)                   # a keep that arrives late is no "being handled"
        talk = (ROOT / "references" / "talk.md").read_text()
        self.assertIn("ap decision keep <name>", talk)

    def test_sessions_get_the_autopilot_folder_from_ap_dir(self):
        # Until the gate moves an older autopilot, its folder is not projects/<name>/: a pass or talk session
        # that wrote there by hand would split it in two. Every path comes from `ap dir`. (init.md makes a new
        # autopilot, which always goes in projects/<name>/.)
        docs = ["RUNBOOK.md", "references/talk.md", "presets/idea.md", "presets/jira-ticket.md",
                "presets/jira-epic.md", "presets/custom.md"]
        for name in docs:
            text = (ROOT / name).read_text()
            for bad in ("$AUTOPILOT_DATA/projects/<", "$AUTOPILOT_DATA/<ap>/", "$AUTOPILOT_DATA/<name>/"):
                self.assertNotIn(bad, text, name)
        rb = (ROOT / "RUNBOOK.md").read_text()
        self.assertIn("`ap dir <ap>`", rb[:rb.index("## Authority")])
        briefs = rb[rb.index("## Briefs"):]
        self.assertIn("older run directory", briefs)
        self.assertIn("`ap dir <name>`", (ROOT / "references" / "talk.md").read_text())

    def test_cards_explain_themselves_and_talk_walks_through_them(self):
        from autopilot import state
        rb = (ROOT / "RUNBOOK.md").read_text()
        card = rb[rb.index("## A decision card"):rb.index("## The pass")]
        for k in state.EXPLAIN_REQ + state.EXPLAIN_OPT + ("does", "cost"):
            self.assertIn(f"`{k}`", card, k)                      # every section the command checks is described
        self.assertIn("refuses", card)
        self.assertIn("(at least 1)", card)                       # the same minimums the command checks
        self.assertIn("(at least 2)", card)
        self.assertIn("word for word", card)                      # a drafted message is shown exactly
        self.assertIn("read every\nline you cite", card)
        talk = (ROOT / "references" / "talk.md").read_text()
        walk = talk[talk.index("## Walking the user through the cards"):talk.index("## Changing things")]
        self.assertIn("card 2 of 5", walk)                        # one card at a time, saying where we are
        self.assertIn("still true", walk)                         # checked against the code before explaining
        self.assertIn("`path:line`", walk)
        self.assertIn("attention: true", walk)
        self.assertIn("`replies`", walk)                          # a card answered on the page is not asked again
        self.assertIn('"A decision card"', talk[talk.index("## Changing things"):])   # a card a talk session adds
        self.assertIn("if it does, every section above", card)    # a gate's walk-through is all or nothing
        self.assertIn("ap decision explain <ap> <D-id> <file>", card)   # an older card gets one in place
        self.assertIn("ap decision explain <name> <D-id> <file> --check", talk)
        for preset in ("jira-ticket.md", "idea.md"):
            self.assertIn('"A decision card"', (ROOT / "presets" / preset).read_text(), preset)
        self.assertIn("walk through them one at a time", (ROOT / "SKILL.md").read_text())


if __name__ == "__main__":
    unittest.main()
