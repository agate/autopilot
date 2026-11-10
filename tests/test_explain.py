from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

from tests.helpers import TempData, write_ap
from autopilot import cli, config, ops, paths, state


def t(s):
    return {"en": s, "zh": "中文：" + s}


def card(kind="ask", **kw):
    """A card that explains itself: every section the runbook asks for, in English and Chinese."""
    d = {
        "kind": kind, "title": t("Pick the cleanup fix"),
        "body": t("Cleaning up a bad build puts the same bad build back."),
        "explain": {
            "terms": [{"term": t("build"), "means": t("Turns the code into an app you can install; takes minutes.")}],
            "analogy": t("You throw out spoiled food, and someone puts the same dish back from the shared fridge."),
            "example": t("bin/cleanup lines 8-14 offer the two-step way; the reviewer reproduced it in case 1."),
            "steps": [t("cleanup deletes the build"), t("the next build installs the shared copy again")],
            "why": t("A: cleanup exists to throw away what is suspect."),
            "next": t("Pick A or B. The next pass restarts the PR loop with the fix."),
        },
        "options": [
            {"id": "A", "label": t("A: leave a marker"), "does": t("The next build compiles."),
             "cost": t("One small fix round."), "recommended": True},
            {"id": "B", "label": t("B: words only"), "does": t("Only the docs change."), "cost": t("The trap stays.")},
        ],
        "links": [{"label": "PR", "url": "https://example.com/pr/1"}],
    }
    if kind == "self":
        d["attention"] = True
    d.update(kw)
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self.td = TempData()
        self.data = self.td.__enter__()
        write_ap(self.data, "checkout")
        g = config.load_global()
        g["reader_lang"] = "zh"
        config.save_global(g)

    def tearDown(self):
        self.td.__exit__(None, None, None)

    def add(self, d):
        """Run `ap decision add` as a pass does; return (exit code, its error line or "")."""
        f = Path(tempfile.mkdtemp()) / "card.json"
        f.write_text(json.dumps(d, ensure_ascii=False))
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            code = cli.main(["decision", "add", "checkout", str(f)])
        return code, "".join(l for l in err.getvalue().splitlines(True) if l.startswith("ap decision add:"))

    def refused(self, d, *words):
        before = len(state.load("checkout")["decisions"])
        code, err = self.add(d)
        self.assertEqual(code, 1, err)
        self.assertIn("does not explain itself", err)
        for w in words:
            self.assertIn(w, err)
        self.assertEqual(len(state.load("checkout")["decisions"]), before)
        return err


class ExplainedCardTest(Base):
    def test_a_full_card_goes_in_whole(self):
        self.assertEqual(self.add(card()), (0, ""))
        got = state.load("checkout")["decisions"][0]
        self.assertEqual(got["explain"], card()["explain"])
        self.assertEqual(got["options"][0]["does"], t("The next build compiles."))

    def test_an_ask_or_self_card_without_the_walk_through_is_refused(self):
        for kind in ("ask", "self"):
            d = card(kind)
            del d["explain"]
            self.refused(d, "missing explain")

    def test_each_missing_section_is_named(self):
        for k in ("terms", "analogy", "example", "steps", "why", "next"):
            d = card()
            del d["explain"][k]
            self.refused(d, f"explain: missing {k}")

    def test_a_missing_language_is_named(self):
        d = card()
        del d["explain"]["analogy"]["zh"]
        self.refused(d, "explain.analogy: missing zh text")
        d = card()
        del d["explain"]["terms"][0]["means"]["zh"]
        self.refused(d, "explain.terms[0].means: missing zh text")

    def test_terms_and_steps_need_enough_entries(self):
        d = card()
        d["explain"]["terms"] = []
        self.refused(d, "explain.terms: must be a list of at least 1")
        d = card()
        d["explain"]["steps"] = [t("only one")]
        self.refused(d, "explain.steps: must be a list of at least 2")
        d = card()
        d["explain"]["terms"] = [{"term": t("x")}]
        self.refused(d, "explain.terms[0]: missing means")

    def test_every_option_says_what_it_does_and_costs(self):
        d = card()
        del d["options"][1]["does"]
        self.refused(d, "options[1]: missing does")
        d = card("self")
        d["options"] = [{"id": "keep", "label": t("OK"), "does": t("Nothing changes.")}]
        self.refused(d, "options[0]: missing cost")

    def test_an_unknown_section_is_refused(self):
        d = card()
        d["explain"]["summary"] = t("x")
        self.refused(d, "explain: unknown key summary")

    def test_related_cards_must_exist(self):
        d = card(explain=dict(card()["explain"], related=[{"id": "D7", "why": t("B is safe only because D7 chose A")}]))
        self.refused(d, "has no card D7")
        self.assertEqual(self.add(card()), (0, ""))  # D1
        self.assertEqual(self.add(d | {"explain": dict(d["explain"], related=[{"id": "D1", "why": t("x")}])}), (0, ""))
        d2 = card(explain=dict(card()["explain"], related=[{"id": "1", "why": t("x")}]))
        self.refused(d2, "must be a card id like D12")

    def test_a_related_card_in_the_archive_counts(self):
        (paths.ap_dir("checkout") / "archive.jsonl").write_text(json.dumps({"id": "D3", "kind": "self"}) + "\n")
        d = card(explain=dict(card()["explain"], related=[{"id": "D3", "why": t("x")}]))
        self.assertEqual(self.add(d), (0, ""))
        self.assertEqual(state.load("checkout")["decisions"][0]["id"], "D4")

    def test_a_gate_may_skip_it_but_a_broken_one_is_refused(self):
        gate = {"kind": "gate", "title": t("Needs a code owner's approval"), "body": t("b"), "options": [], "links": []}
        self.assertEqual(self.add(gate), (0, ""))
        bad = dict(gate, explain={"terms": "x"})
        self.refused(bad, "explain.terms: must be a list")

    def test_the_gates_own_cards_go_in_without_one(self):
        # gate.py's dispatcher_stuck card and access.py's system cards are fixed labels, added in code.
        ops.decision_add("checkout", {"kind": "ask", "title": {"key": "dispatcher_stuck"},
                                      "body": {"key": "dispatcher_stuck"}, "options": [], "links": []})
        self.assertEqual(len(state.load("checkout")["decisions"]), 1)

    def test_a_card_saved_before_the_rule_still_loads(self):
        st = state.empty()
        old = card()
        del old["explain"]
        for o in old["options"]:
            del o["does"]
        st["decisions"] = [dict(old, id="D1", status="open", created_at="x")]
        (paths.ap_dir("checkout") / "state.json").write_text(json.dumps(st))
        self.assertEqual(len(state.load("checkout")["decisions"]), 1)

    def test_state_json_with_a_broken_walk_through_does_not_load(self):
        st = state.empty()
        bad = copy.deepcopy(card())
        bad["explain"]["steps"] = "one string"
        st["decisions"] = [dict(bad, id="D1", status="open", created_at="x")]
        (paths.ap_dir("checkout") / "state.json").write_text(json.dumps(st))
        with self.assertRaises(state.StateError) as cm:
            state.load("checkout")
        self.assertIn("explain.steps", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
