from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest import mock

from tests.helpers import TempData, write_ap
from autopilot import cli, events, ops, paths, snapshot


def dec(title):
    return {"kind": "ask", "title": {"en": title}, "body": {"en": "b"},
            "options": [{"id": "a", "label": {"en": "A"}}], "links": [{"label": "x", "url": "https://example.com"}]}


def P(prompt, sid="s1"):
    return {"session_id": sid, "prompt": prompt, "hook_event_name": "UserPromptSubmit"}


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.td = TempData()
        self.data = self.td.__enter__()
        write_ap(self.data, "checkout")

    def tearDown(self):
        self.td.__exit__(None, None, None)

    def test_bind_by_name(self):
        out = snapshot.handle(P("/autopilot checkout what is going on"))
        self.assertIn("checkout", out)
        self.assertEqual(snapshot.binding("s1")["name"], "checkout")

    def test_off_unbinds(self):
        snapshot.handle(P("/autopilot checkout"))
        self.assertEqual(snapshot.handle(P("/autopilot off")), "autopilot: unbound")
        self.assertIsNone(snapshot.binding("s1"))

    def test_unbound_plain_none(self):
        self.assertIsNone(snapshot.handle(P("hello there")))

    def test_other_plugin_command_ignored(self):
        self.assertIsNone(snapshot.handle(P("/autopilot:pr-loop 12")))

    def test_bound_snapshot_counts(self):
        ops.decision_add("checkout", dec("Pick the first thing"))
        ops.decision_add("checkout", dec("Pick the second thing"))
        snapshot.handle(P("/autopilot checkout"))
        out = snapshot.handle(P("hello"))
        self.assertIn("Waiting on you (2)", out)
        self.assertIn("Pick the first thing", out)
        self.assertIn("Pick the second thing", out)

    def test_self_decisions_show_as_one_line_of_counts(self):
        # A talk session sees how many of its own calls wait to be read, and which want a look, never their bodies.
        def mine(title, attention):
            return dict(dec(title), kind="self", options=[], attention=attention)
        ops.decision_add("checkout", mine("Picked the wider retry", True))
        ops.decision_add("checkout", mine("Renamed a helper", False))
        ops.decision_add("checkout", mine("Kept the old flag", False))
        ops.decision_keep("checkout", ["D3"])
        snapshot.handle(P("/autopilot checkout"))
        out = snapshot.handle(P("hello"))
        self.assertIn("Decided by itself, unread (2): want a look: D1 Picked the wider retry; routine: 1", out)
        self.assertNotIn("Renamed a helper", out)

    def test_events_not_repeated(self):
        snapshot.handle(P("/autopilot checkout"))
        events.append("checkout", "worker_started", {"task": "PROJ-123"})
        first = snapshot.handle(P("hi"))
        second = snapshot.handle(P("hi again"))
        self.assertIn("worker_started", first)
        self.assertNotIn("worker_started", second)

    def test_rules_change_included_once(self):
        (self.data / "checkout" / "RULES.md").write_text("- 2026-01-01 — \"be careful\" — why: test\n")
        first = snapshot.handle(P("/autopilot checkout"))
        second = snapshot.handle(P("again"))
        self.assertIn("be careful", first)
        self.assertNotIn("be careful", second)

    def test_snapshot_size_cap(self):
        snapshot.handle(P("/autopilot checkout"))
        for i in range(400):
            events.append("checkout", "noise", {"i": i, "pad": "x" * 200})
        self.assertLessEqual(len(snapshot.handle(P("hi"))), 6000)

    def test_no_name_gives_session_hint(self):
        out = snapshot.handle(P("/autopilot what is waiting", sid="abc"))
        self.assertIn("session_id=abc", out)
        self.assertIsNone(snapshot.binding("abc"))

    def test_hook_swallows_errors(self):
        buf = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO("not json")), contextlib.redirect_stdout(buf):
            rc = cli.main(["hook-prompt"])
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue(), "")

    def test_hook_output_shape(self):
        buf = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(P("/autopilot checkout")))), contextlib.redirect_stdout(buf):
            cli.main(["hook-prompt"])
        out = json.loads(buf.getvalue())
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn("checkout", out["hookSpecificOutput"]["additionalContext"])


if __name__ == "__main__":
    unittest.main()
