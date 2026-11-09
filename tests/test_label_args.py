from __future__ import annotations

import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from tests.helpers import ROOT, TempData, write_ap
from autopilot import cli, ops, snapshot, state, text, paths


class Base(unittest.TestCase):
    def setUp(self):
        self.td = TempData()
        self.data = self.td.__enter__()
        write_ap(self.data, "checkout")

    def tearDown(self):
        self.td.__exit__(None, None, None)


class LabelArgsTest(Base):
    def card(self, title, body=None):
        return {"kind": "ask", "title": title, "body": body or {"en": "b"}, "options": [], "links": []}

    def test_missing_args_are_rejected_with_a_clear_error(self):
        for title in ({"key": "worker_blocked"}, {"key": "worker_blocked", "args": {}},
                      {"key": "worker_blocked", "args": {"name": "x"}}, {"key": "worker_died_twice", "args": None}):
            with self.assertRaises(ValueError) as cm:
                ops.decision_add("checkout", self.card(title))
            self.assertIn("session", str(cm.exception), title)
        self.assertEqual(state.load("checkout")["decisions"], [])

    def test_the_body_is_checked_too(self):
        with self.assertRaises(ValueError) as cm:
            ops.decision_add("checkout", self.card({"en": "t"}, {"key": "two_answers"}))
        self.assertIn("body", str(cm.exception))
        self.assertIn("id", str(cm.exception))

    def test_filled_and_placeholder_free_labels_pass(self):
        ops.decision_add("checkout", self.card({"key": "worker_blocked", "args": {"session": "checkout · T · pr"}}))
        ops.decision_add("checkout", self.card({"key": "dispatcher_stuck"}))
        ops.decision_add("checkout", self.card({"en": "plain"}))
        self.assertEqual(len(state.load("checkout")["decisions"]), 3)

    def test_the_command_exits_1_and_says_why(self):
        f = Path(tempfile.mkdtemp()) / "card.json"
        f.write_text(json.dumps(self.card({"key": "worker_blocked"})))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["decision", "add", "checkout", str(f)]), 1)
        self.assertIn("ap decision add: title: label 'worker_blocked' needs args session", err.getvalue())

    def test_a_card_saved_before_the_rule_still_loads(self):
        st = state.empty()
        st["decisions"] = [dict(self.card({"key": "worker_blocked"}), id="D1", status="open", created_at="x")]
        (paths.ap_dir("checkout") / "state.json").write_text(json.dumps(st))
        self.assertEqual(len(state.load("checkout")["decisions"]), 1)

    def test_the_snapshot_fills_the_placeholders(self):
        self.assertEqual(snapshot._txt({"key": "worker_blocked", "args": {"session": "s1"}}),
                         "A worker is waiting for a permission nobody can give: s1.")

    def test_every_runbook_card_title_fills_its_label(self):
        # The producers of label-key titles are RUNBOOK instructions: each must show its args.
        rb = (ROOT / "RUNBOOK.md").read_text()
        labels = text.labels()
        found = re.findall(r'\{"key": "(\w+)"([^`]*?)\}`', rb)
        self.assertTrue(found)
        for key, rest in found:
            need = set(re.findall(r"\{(\w+)\}", labels[key]))
            for n in need:
                self.assertIn(f'"{n}"', rest, f"{key} needs {n}")
        self.assertIn("worker_died_twice", {k for k, _ in found})
