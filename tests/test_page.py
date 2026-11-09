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

    def test_an_answer_being_handled_sits_outside_the_lists_that_need_you(self):
        p = Ids()
        p.feed(PAGE)
        self.assertIn("handling", p.ids)
        self.assertIn("handling", LABELS)
        # Open asks, gates and systems only count open decisions; a handled one never shows in them or in "answered".
        self.assertIn('d.status !== "open" && !d.handling', PAGE)
        self.assertIn("d.handling", PAGE)

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


    def test_system_access_cards_sit_above_everything_else(self):
        p = Ids()
        p.feed(PAGE)
        self.assertIn("systems", p.ids)
        self.assertLess(PAGE.index('id="systems"'), PAGE.index('id="hero"'))
        body = PAGE[PAGE.index("function systemCard"):PAGE.index("function selfRow")]
        for key in ("sys_tag", "sys_since", "sys_blocks", "sys_fix", "sys_checked"):
            self.assertIn(f'L("{key}"', body)
            self.assertIn(key, LABELS)
        self.assertIn('open("system")', PAGE)
        for key in ("sys_title", "sys_hint", "hero_systems", "access_broken", "access_body", "sys_restored",
                    "blocks_gh", "blocks_page", "blocks_mcp", "blocks_check"):
            self.assertIn(key, LABELS)


def js_fn(name: str) -> str:
    """One top-level function of the page's script, to run on its own in node."""
    src = PAGE[PAGE.index(f"function {name}("):]
    return src[:src.index("\n}") + 2]


def node(js: str) -> str:
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    if out.returncode:
        raise AssertionError(out.stderr)
    return out.stdout.strip()


class SelfDecisionsTest(unittest.TestCase):
    """Decisions the autopilot made by itself: the few worth a look on top, the routine rest folded, the read ones
    in the history and on their task's evidence page."""

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_groups_split_worth_a_look_routine_and_read(self):
        js = js_fn("selfGroups") + """
const d = (id, status, attention, at) => ({ id, kind: "self", status, attention, created_at: at });
const ds = [d("D1", "open", true, "2026-01-01"), d("D2", "open", false, "2026-01-02"), d("D3", "answered", false, "2026-01-03"),
  d("D4", "open", undefined, "2026-01-04"), d("D5", "undone", true, "2026-01-05"), d("D6", "open", true, "2026-01-06"),
  { id: "D7", kind: "ask", status: "open", created_at: "2026-01-07" }];
const g = selfGroups(ds);
console.log(JSON.stringify({ look: g.look.map((x) => x.id), routine: g.routine.map((x) => x.id), read: g.read.map((x) => x.id) }));
"""
        # Worth a look: oldest first, like asks. Routine: newest first. A card saved before the flag existed is routine.
        self.assertEqual(json.loads(node(js)), {"look": ["D1", "D6"], "routine": ["D4", "D2"], "read": ["D5", "D3"]})

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_reminds_at_the_count_or_the_age(self):
        js = js_fn("selfRemind") + """
const cfg = { remind_at: 3, remind_days: 2 }, now = Date.parse("2026-01-10T12:00:00Z");
const r = (...ats) => ats.map((created_at) => ({ created_at }));
console.log(JSON.stringify([
  selfRemind([], cfg, now),
  selfRemind(r("2026-01-10T10:00:00Z", "2026-01-10T11:00:00Z"), cfg, now),
  selfRemind(r("2026-01-10T10:00:00Z", "2026-01-10T11:00:00Z", "2026-01-10T11:30:00Z"), cfg, now),
  selfRemind(r("2026-01-08T11:00:00Z"), cfg, now),
  selfRemind(r("2026-01-08T11:00:00Z"), {}, now),
  selfRemind(r("2026-01-01T00:00:00Z"), { remind_at: 0, remind_days: 0 }, now),
]));
"""
        # The defaults are 10 and 3 days; 0 turns that half of the reminder off.
        self.assertEqual(json.loads(node(js)), [False, False, True, True, False, False])

    def test_the_routine_list_is_folded_and_stays_as_you_left_it(self):
        p = Ids()
        p.feed(PAGE)
        self.assertIn("self-look", p.ids)
        self.assertLess(PAGE.index('id="asks"'), PAGE.index('id="self-look"'))
        self.assertLess(PAGE.index('id="gates"'), PAGE.index('id="self-look"'))
        body = PAGE[PAGE.index("function renderSelfs"):PAGE.index("function renderNeeds")]
        self.assertIn('el("details", { class: "fold self-fold"', body)
        self.assertIn("open: selfOpen", body)
        self.assertIn("selfOpen = e.target.open", body)
        # Each routine item shows its title; the body opens on a click and stays open across a re-render.
        row = js_fn("selfRow")
        self.assertIn("openBodies", row)

    def test_ok_and_undo_go_out_as_replies(self):
        row = js_fn("selfButtons")
        self.assertIn('sendReply(key, { option: "keep" })', row)
        self.assertIn('sendReply(key, { option: "undo" })', row)
        # "Mark all read" sends one reply per autopilot, listing its cards.
        allr = js_fn("keepAll")
        self.assertIn('db.collection("replies").add(', allr)
        self.assertIn("decisions: keys", allr)
        self.assertIn('option: "keep"', allr)
        self.assertIn("by: myId", allr)
        self.assertIn("(r.decisions || []).includes(key)", PAGE[PAGE.index("const replyFor"):][:200])

    def test_evidence_page_lists_the_task_s_own_decisions(self):
        body = js_fn("renderDetail")
        self.assertIn("taskSelfs(", body)
        self.assertIn('L("self_task"', body)
        ts = js_fn("taskSelfs")
        self.assertIn('values("decisions")', ts)
        self.assertIn('values("archive")', ts)   # an OK older than the archive window still shows, with its Undo
        # A task with decisions but no evidence still opens its page.
        self.assertIn("taskSelfs(t.ap, t.id).length", js_fn("taskRow"))

    def test_reminder_is_one_line_never_a_card(self):
        body = PAGE[PAGE.index("function renderSelfs"):PAGE.index("function renderNeeds")]
        self.assertIn('L("self_remind"', body)
        self.assertIn("S.meta.self_review", body)
        self.assertIn('class: "remind', body)
        self.assertNotIn('class: "card', body[body.index('L("self_remind"') - 200:body.index('L("self_remind"')])

    def test_an_undo_after_ok_says_when_it_is_picked_up(self):
        # A kept card does not keep the reply check running, so its undo waits for the forced pass.
        self.assertIn('L("pickup_later"', js_fn("selfButtons"))


if __name__ == "__main__":
    unittest.main()
