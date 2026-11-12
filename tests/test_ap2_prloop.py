"""How the PR loop ends (design 6.3): the record, the decision, the one check of the fix, and the card."""
from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT  # noqa: F401  (puts lib/ on sys.path)
from assistant import angles, prloop, sources, workers
from assistant.config import Config
from assistant.prloop import PrLoopError
from assistant.store import Store

# The angles a PR that changes code, a test and a doc gets: Correctness, Test value, Flakiness & isolation, Accuracy.
PICKED = angles.pick(angles.load(), ["lib/pay.py", "tests/test_pay.py", "docs/pay.md"], "loop")
NAMES = [a["name"] for a in PICKED]
SHA1 = "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d"
SHA2 = "2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e"
SHA3 = "3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f"
T0 = datetime(2026, 11, 10, 14, 30, 5, tzinfo=timezone.utc)


def finding(id_, angle, bucket="fix", **more):
    return dict({"id": id_, "angle": angle, "bucket": bucket}, **more)


def rnd(n, findings=(), commit=None, ci="green", stop=None):
    return {"n": n, "angles": copy.deepcopy(PICKED), "findings": [copy.deepcopy(f) for f in findings],
            "commit": commit, "ci": ci, "stop": stop}


def record(*rounds, **top):
    out = {"rounds": list(rounds)}
    out.update(top)
    return out


# Round 3's findings at a limit of 3: Correctness has two fixes (one is CI's), Test value one, Accuracy only invalid.
R1_FINDING = finding("correctness-1", "Correctness", where="lib/pay.py:10", text="round one only: charge twice")
R2_FINDING = finding("test-value-1", "Test value", where="tests/test_pay.py:4", text="round two only: no retry test")
CI_FINDING = finding("ci-1", None, text="unit tests fail on the retry path")
CORR_FINDING = finding("correctness-2", "Correctness", where="lib/pay.py:31", text="refund skips the zero amount",
                       reason="verified: amount 0 returns early")
TEST_FINDING = finding("test-value-2", "Test value", where="tests/test_pay.py:40", text="mocks away the retry")
ACC_FINDING = finding("accuracy-1", "Accuracy", "invalid", where="docs/pay.md:3", reason="the doc is right")


def at_limit(**top):
    """A loop with a limit of 3 at round 3, with fixes from Correctness and Test value: the check comes next."""
    top.setdefault("limit", 3)
    return record(rnd(1, [R1_FINDING], commit=SHA1), rnd(2, [R2_FINDING], commit=SHA2),
                  rnd(3, [CI_FINDING, CORR_FINDING, TEST_FINDING, ACC_FINDING], commit=SHA3, ci="red"), **top)


def deep(n):
    """A list nested `n` deep."""
    value = []
    for _ in range(n):
        value = [value]
    return value


def no_findings():
    return {"Correctness": [], "Test value": []}


def plain():
    return {k: {"en": f"{k} line"} for k in ("what", "where", "do")}


def card(**over):
    c = {"kind": "code", "lane": "running", "feed": "github-pr", "links": [], "pri": "P2", "origin": "source",
         "plain": plain(), "title": "Retry failed payments"}
    c.update(over)
    return c


class NoSubprocess:
    """Patch subprocess so any call fails the test."""

    def __init__(self, test):
        self.test = test

    def __enter__(self):
        def fail(*a, **k):
            self.test.fail(f"a subprocess ran: {a!r}")
        self.patches = [mock.patch.object(subprocess, "run", side_effect=fail),
                        mock.patch.object(subprocess, "Popen", side_effect=fail)]
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()


class BelowLimitTest(unittest.TestCase):
    """Below the limit, the loop ends as v1's does."""

    def test_decide_cases(self):
        cases = [
            ("round 1 with a fix", record(rnd(1, [R1_FINDING], commit=SHA1)), "round", None, None),
            ("only invalid, CI green", record(rnd(1, [ACC_FINDING])), "end", "clean", "round"),
            ("two rounds of non-behavior fixes",
             record(rnd(1, [finding("c-1", "Correctness", behavior=False)], commit=SHA1),
                    rnd(2, [finding("c-2", "Correctness", behavior=False)], commit=SHA2)),
             "end", "converged", "converged"),
            ("an owner finding", record(rnd(1, [finding("c-1", "Correctness", "owner", text="which currency?")])),
             "end", "stopped", "stop"),
            ("a round stop", record(rnd(1, [R1_FINDING], commit=SHA1, stop="the branch moved")),
             "end", "stopped", "stop"),
            ("no rounds yet", record(), "round", None, None),
            ("CI red with no fix", record(rnd(1, [ACC_FINDING], ci="red")), "round", None, None),
            ("one non-behavior round after a behavior round",
             record(rnd(1, [R1_FINDING], commit=SHA1),
                    rnd(2, [finding("c-2", "Correctness", behavior=False)], commit=SHA2)), "round", None, None),
        ]
        for name, rec, nxt, result, ended_by in cases:
            with self.subTest(name):
                d = prloop.decide(rec)
                self.assertEqual(set(d), {"next", "result", "ended_by", "reason", "plan"})
                self.assertEqual((d["next"], d["result"], d["ended_by"]), (nxt, result, ended_by))
                self.assertIsNone(d["plan"])
                if nxt == "end":
                    self.assertTrue(d["reason"])

    def test_stop_reasons(self):
        d = prloop.decide(record(rnd(1, [R1_FINDING], commit=SHA1, stop="the same finding twice: correctness-1")))
        self.assertIn("the same finding twice: correctness-1", d["reason"])
        d = prloop.decide(record(rnd(1, [finding("c-1", "Correctness", "owner")])))
        self.assertIn("c-1", d["reason"])

    def test_converged_has_no_ask(self):
        rec = record(rnd(1, [finding("c-1", "Correctness", behavior=False)], commit=SHA1),
                     rnd(2, [finding("c-2", "Correctness", behavior=False)], commit=SHA2))
        self.assertEqual(prloop.decide(rec)["result"], "converged")
        self.assertIsNone(prloop.ask(rec))

    def test_fix_that_failed_may_have_no_commit(self):
        d = prloop.decide(record(rnd(1, [R1_FINDING], stop="the fixer could not fix correctness-1")))
        self.assertEqual((d["next"], d["result"]), ("end", "stopped"))


class AtLimitTest(unittest.TestCase):
    """At the limit, a check, not a round."""

    def test_check_with_its_plan(self):
        for name, limit in [("limit in the record", None), ("the same limit in the record and the call", 3)]:
            with self.subTest(name):
                d = prloop.decide(at_limit(), limit=limit)
                self.assertEqual((d["next"], d["result"], d["ended_by"]), ("check", None, None))
                self.assertEqual([e["angle"]["name"] for e in d["plan"]], ["Correctness", "Test value"])
                self.assertEqual([e["commit"] for e in d["plan"]], [SHA3, SHA3])
                self.assertEqual(d["plan"][0]["answers"], [CI_FINDING, CORR_FINDING])
                self.assertEqual(d["plan"][1]["answers"], [TEST_FINDING])
                self.assertEqual(d["plan"][0]["angle"], PICKED[0])
                self.assertEqual(d["plan"], prloop.plan(at_limit()))

    def test_plan_skips_angles_with_no_fix(self):
        names = [e["angle"]["name"] for e in prloop.plan(at_limit())]
        self.assertNotIn("Accuracy", names)
        self.assertNotIn("Flakiness & isolation", names)

    def test_plan_refuses_a_record_with_no_check_due(self):
        cases = [
            ("below the limit", record(rnd(1, [R1_FINDING], commit=SHA1), limit=3),
             r"^the record: rounds\[0\]: no check of the fix: round 1 is below the round limit \(3\)$"),
            ("stopped, fix with no commit", record(rnd(1, [R1_FINDING], stop="the fixer could not fix it"), limit=1),
             r"^the record: rounds\[0\]: no check of the fix: the loop ended after round 1 \(stopped: "),
            ("the check already ran", prloop.record_check(at_limit(), no_findings(), "green"),
             r"^the record: fix_check: the record already has a check of the fix; it runs at most once per loop$"),
            ("no rounds yet", record(), r"^the record: rounds: no check of the fix: the loop has no rounds yet$"),
        ]
        for name, rec, says in cases:
            with self.subTest(name):
                self.assertNotEqual(prloop.decide(rec)["next"], "check")
                with self.assertRaisesRegex(PrLoopError, says):
                    prloop.plan(rec)
        with self.subTest("at the limit: the same plan as decide"):
            rec = at_limit()
            self.assertEqual(prloop.plan(rec), prloop.decide(rec)["plan"])

    def test_plan_sends_a_ci_finding_to_correctness(self):
        rec = at_limit()
        ci = finding("ci-1", "Test value", text="unit tests fail on the retry path")
        rec["rounds"][2]["findings"] = [ci, CORR_FINDING]
        got = prloop.plan(rec)
        self.assertEqual([e["angle"]["name"] for e in got], ["Correctness"])
        self.assertEqual(got[0]["answers"], [ci, CORR_FINDING])

    def test_plan_follows_the_rounds_angle_order(self):
        rec = record(rnd(1, [TEST_FINDING, CORR_FINDING], commit=SHA1), limit=1)
        self.assertEqual([e["angle"]["name"] for e in prloop.plan(rec)], ["Correctness", "Test value"])

    def test_never_a_round_past_the_limit(self):
        rec = at_limit()
        for _ in range(3):
            self.assertNotEqual(prloop.decide(rec)["next"], "round")

    def test_default_limit_is_twenty(self):
        self.assertEqual(prloop.ROUND_LIMIT, 20)
        rec = at_limit(limit=None)
        rec.pop("limit")
        self.assertEqual(prloop.decide(rec)["next"], "round")
        rounds = [rnd(n, [finding(f"c-{n}", "Correctness")], commit=SHA1) for n in range(1, 21)]
        d = prloop.decide(record(*rounds))
        self.assertEqual(d["next"], "check")
        self.assertEqual(prloop.summary(record(*rounds))["limit"], 20)

    def test_bad_limits_refused(self):
        for bad in (0, 21, True, -1, 3.0, "3"):
            with self.subTest(limit=bad):
                with self.assertRaisesRegex(PrLoopError, "limit"):
                    prloop.decide(at_limit(limit=None), limit=bad)
                with self.assertRaisesRegex(PrLoopError, "limit"):
                    prloop.decide(at_limit(limit=bad))
        one = record(rnd(1, [R1_FINDING], commit=SHA1), limit=1)
        whole = "limit: must be a whole number from 1 to 20"
        for name, call in [
                ("record limit 0", lambda: prloop.decide({"rounds": [], "limit": 0})),
                ("record limit True", lambda: prloop.decide(record(rnd(1, [R1_FINDING], commit=SHA1), limit=True))),
                ("call limit True", lambda: prloop.decide(one, limit=True)),
                ("call limit 0", lambda: prloop.decide(one, limit=0)),
                ("call limit 21", lambda: prloop.decide(dict(one, limit=20), limit=21))]:
            with self.subTest(name):
                with self.assertRaisesRegex(PrLoopError, whole):
                    call()

    def test_call_limit_must_match_the_record(self):
        limitless = at_limit()
        limitless.pop("limit")
        with self.subTest("a call limit that differs from the record's"):
            with self.assertRaisesRegex(PrLoopError, "limit"):
                prloop.decide(at_limit(), limit=5)
        with self.subTest("a lower call limit on a record with no limit"):
            with self.assertRaisesRegex(PrLoopError, "the record has no limit, so it is 20, not 3"):
                prloop.decide(limitless, limit=3)
        with self.subTest("the default limit in the call on a record with no limit"):
            self.assertEqual(prloop.decide(limitless, limit=20)["next"], "round")

    def test_decide_and_record_check_read_the_same_limit(self):
        limitless = at_limit()
        limitless.pop("limit")
        with self.subTest("no limit in the record: 20"):
            self.assertEqual(prloop.decide(limitless)["next"], "round")
            with self.assertRaisesRegex(PrLoopError, "below the round limit"):
                prloop.record_check(limitless, no_findings(), "green")
        with self.subTest("limit 3 in the record"):
            rec = dict(limitless, limit=3)
            self.assertEqual(prloop.decide(rec)["next"], "check")
            self.assertEqual(prloop.record_check(rec, no_findings(), "green")["result"], "clean")

    def test_limit_round_with_no_fix_and_red_ci_ends(self):
        rec = record(rnd(1, [R1_FINDING], commit=SHA1), rnd(2, [ACC_FINDING], ci="red"), limit=2)
        d = prloop.decide(rec)
        self.assertEqual((d["next"], d["result"], d["ended_by"]), ("end", "stopped", "round"))
        self.assertIn("red", d["reason"])


class CheckBriefTest(unittest.TestCase):
    """The check reviewer reads only the fix."""

    def test_brief_holds_only_the_fix(self):
        entry = prloop.plan(at_limit())[0]
        text = prloop.check_brief(entry, 42, "/tmp/wt/pay")
        corr = PICKED[0]
        for part in (corr["name"], corr["question"], corr["brief"], SHA3, f"git show {SHA3}", "PR #42",
                     "/tmp/wt/pay", "NO FINDINGS", "severity: blocker | should-fix | nit", "trigger:", "fix:",
                     "Read-only"):
            self.assertIn(part, text)
        for f in (CI_FINDING, CORR_FINDING):
            self.assertIn(json.dumps(f, ensure_ascii=False, sort_keys=True), text)
        for other in (R1_FINDING, R2_FINDING, TEST_FINDING, ACC_FINDING):
            self.assertNotIn(other["id"], text)
            self.assertNotIn(other.get("text") or other["reason"], text)

    def test_bad_inputs_refused(self):
        entry = prloop.plan(at_limit())[0]
        for name, args in [("pr 0", (entry, 0, "/tmp/wt")), ("pr bool", (entry, True, "/tmp/wt")),
                           ("no worktree", (entry, 1, "")), ("bad commit", (dict(entry, commit="HEAD"), 1, "/w")),
                           ("no answers", (dict(entry, answers=[]), 1, "/w"))]:
            with self.subTest(name):
                with self.assertRaises(PrLoopError):
                    prloop.check_brief(*args)


class CheckOutcomeTest(unittest.TestCase):
    """Nothing found ends clean; something found asks."""

    def test_nothing_found_ends_clean(self):
        rec = prloop.record_check(at_limit(), no_findings(), "green")
        d = prloop.decide(rec)
        self.assertEqual((d["next"], d["result"], d["ended_by"]), ("end", "clean", "fix_check"))
        self.assertEqual((rec["result"], rec["ended_by"]), ("clean", "fix_check"))
        self.assertEqual(rec["fix_check"]["commit"], SHA3)
        self.assertIsNone(prloop.ask(rec))

    def test_invalid_alone_is_still_clean_and_listed(self):
        bad = finding("check-1", "Test value", "invalid", reason="the mock is the HTTP client, not the retry")
        rec = prloop.record_check(at_limit(), {"Correctness": [], "Test value": [bad]}, "green")
        self.assertEqual(prloop.decide(rec)["result"], "clean")
        s = prloop.summary(rec)
        listed = [f for r in s["fix_check"]["reviewers"] for f in r["findings"]]
        self.assertEqual(listed, [bad])
        self.assertIsNone(prloop.ask(rec))

    def test_end_fields_come_from_the_check(self):
        for top_ci, check_ci in (("green", "red"), ("red", "green")):
            with self.subTest(record_ci=top_ci, check_ci=check_ci):
                new = prloop.record_check(at_limit(ci=top_ci, head=SHA2), no_findings(), check_ci)
                self.assertEqual(new["head"], SHA3)
                self.assertEqual(new["ci"], check_ci)
                self.assertIsNotNone(new["reason"])
                self.assertEqual(new["reason"], prloop.decide(new)["reason"])

    def test_input_record_is_not_changed(self):
        rec = at_limit()
        before = copy.deepcopy(rec)
        prloop.record_check(rec, no_findings(), "green")
        self.assertEqual(rec, before)

    def test_something_found_asks(self):
        fix = finding("check-1", "Test value", where="tests/test_pay.py:41", text="the retry test still mocks it")
        cases = [("a fix finding", {"Correctness": [], "Test value": [{k: v for k, v in fix.items() if k != "angle"}]},
                  "green", [fix]),
                 ("CI red, no findings", no_findings(), "red", []),
                 ("an owner finding", {"Correctness": [finding("check-2", "Correctness", "owner", text="which?")],
                                       "Test value": [finding("check-3", "Test value", "out_of_scope")]}, "green",
                  [finding("check-2", "Correctness", "owner", text="which?")])]
        for name, results, ci, found in cases:
            with self.subTest(name):
                rec = prloop.record_check(at_limit(), results, ci)
                d = prloop.decide(rec)
                self.assertEqual((d["next"], d["result"], d["ended_by"]), ("end", "stopped", "fix_check"))
                self.assertIn("1", d["reason"])
                a = prloop.ask(rec)
                self.assertEqual({k: a[k] for k in ("type", "from", "role", "reason", "commit")},
                                 {"type": "approve", "from": "prloop", "role": "pr", "reason": "fix_check",
                                  "commit": SHA3})
                self.assertEqual(a["findings"], found)
                self.assertIn(SHA3[:7], a["text"])
                self.assertIn("nothing more is fixed until you decide", a["text"])
                self.assertIn("found 1 thing in", a["text"])
                self.assertIsNone(workers.Workers._worker_ask_kind(a, "pr"))


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.data = Path(tempfile.mkdtemp(prefix="ap2-prloop-")).resolve()
        self.addCleanup(shutil.rmtree, self.data, True)
        self.store = Store(self.data, cfg=Config(), clock=lambda: T0)
        self.card = self.store.add(card(), by="test")


class ApplyTest(StoreCase):
    """The card and the report say how it ended."""

    def test_found_sets_the_approve_ask_and_summary(self):
        results = {"Correctness": [], "Test value": [finding("check-1", "Test value", text="still mocked")]}
        rec = prloop.record_check(at_limit(), results, "green")
        with NoSubprocess(self):
            self.assertTrue(prloop.apply(self.store, self.card["id"], rec, by="ap2 prloop"))
        got = self.store.get(self.card["id"])
        self.assertEqual(got["ask"], prloop.ask(rec))
        self.assertEqual(got["ask"]["from"], "prloop")
        self.assertEqual(got["prloop"], prloop.summary(rec))
        self.assertEqual(got["prloop"]["result"], "stopped")
        self.assertEqual(got["prloop"]["ended_by"], "fix_check")
        self.assertEqual(got["prloop"]["rounds"], 3)
        self.assertIsNotNone(got["prloop"]["reason"])
        self.assertEqual(got["prloop"]["reason"], prloop.decide(rec)["reason"])
        self.assertEqual(got["prloop"]["limit"], 3)
        self.assertEqual(got["prloop"]["fix_check"]["reviewers"][1]["findings"][0]["id"], "check-1")
        # Applying it again changes nothing and still says the ask is the loop's.
        self.assertTrue(prloop.apply(self.store, self.card["id"], rec, by="ap2 prloop"))

    def test_clean_writes_summary_and_no_ask(self):
        rec = prloop.record_check(at_limit(), no_findings(), "green")
        with NoSubprocess(self):
            self.assertTrue(prloop.apply(self.store, self.card["id"], rec, by="ap2 prloop"))
        got = self.store.get(self.card["id"])
        self.assertIsNone(got.get("ask"))
        self.assertEqual(got["prloop"]["result"], "clean")
        self.assertEqual(got["prloop"]["ended_by"], "fix_check")

    def test_users_own_ask_is_kept(self):
        mine = {"type": "confirm", "text": "Ship it on Friday?"}
        self.store.update(self.card["id"], {"ask": mine}, by="user")
        rec = prloop.record_check(at_limit(), no_findings(), "red")
        with NoSubprocess(self):
            self.assertFalse(prloop.apply(self.store, self.card["id"], rec, by="ap2 prloop"))
        got = self.store.get(self.card["id"])
        self.assertEqual(got["ask"], mine)
        self.assertEqual(got["prloop"], prloop.summary(rec))

    def test_other_stop_sets_an_answer_ask(self):
        rec = record(rnd(1, [R1_FINDING], commit=SHA1, stop="the branch moved without our push"))
        with NoSubprocess(self):
            self.assertTrue(prloop.apply(self.store, self.card["id"], rec, by="ap2 prloop"))
        got = self.store.get(self.card["id"])
        self.assertEqual(got["ask"]["type"], "answer")
        self.assertEqual(got["ask"]["from"], "prloop")
        self.assertIn("the branch moved without our push", got["ask"]["text"])
        self.assertIsNone(workers.Workers._worker_ask_kind(got["ask"], "pr"))
        self.assertEqual(got["prloop"]["ended_by"], "stop")


class ReportTest(unittest.TestCase):
    def test_check_lines(self):
        with NoSubprocess(self):
            clean = prloop.report(prloop.record_check(at_limit(), no_findings(), "green"))
            found = prloop.report(prloop.record_check(
                at_limit(), {"Correctness": [finding("check-1", "Correctness")], "Test value": []}, "red"))
        for text in (clean, found):
            self.assertIn(f"Check of the fix · commit {SHA3[:7]} · reviewers: Correctness, Test value · ", text)
            self.assertIn("Findings: prloop.json fix_check.reviewers", text)
        self.assertIn("found nothing: clean", clean)
        self.assertIn("found 2: asking you", found)

    def test_round_lines(self):
        self.assertIn(f"Fix: {SHA1}\n", prloop.report(record(rnd(1, [R1_FINDING], commit=SHA1))))
        self.assertIn(f"Fix: {SHA3}\n", prloop.report(at_limit()))
        self.assertIn("Fix: none\n", prloop.report(record(rnd(1, []))))
        self.assertIn("Next: round 2", prloop.report(record(rnd(1, [R1_FINDING], commit=SHA1))))
        self.assertIn("Next: check of the fix", prloop.report(at_limit()))
        self.assertIn("Next: stop, because", prloop.report(record(rnd(1, [R1_FINDING], commit=SHA1, stop="x y"))))
        self.assertIn("Next: success", prloop.report(record(rnd(1, []))))


class AtMostOnceTest(unittest.TestCase):
    """The check runs at most once."""

    def drive(self, check_results, check_ci):
        """Run a loop with a limit of 3 whose every round fixes something, calling decide until it ends."""
        rec, checks = {"limit": 3, "rounds": []}, 0
        planned = {1: rnd(1, [R1_FINDING], commit=SHA1), 2: rnd(2, [R2_FINDING], commit=SHA2),
                   3: rnd(3, [CI_FINDING, CORR_FINDING, TEST_FINDING, ACC_FINDING], commit=SHA3)}
        for _ in range(10):
            d = prloop.decide(rec)
            if d["next"] == "end":
                return rec, d, checks
            if d["next"] == "round":
                rec["rounds"].append(planned[len(rec["rounds"]) + 1])
            else:
                checks += 1
                rec = prloop.record_check(rec, check_results, check_ci)
        self.fail("the loop never ended")

    def test_one_check_each_way(self):
        for name, results, ci, result in [
                ("clean", no_findings(), "green", "clean"),
                ("found", {"Correctness": [finding("check-1", "Correctness")], "Test value": []}, "green",
                 "stopped")]:
            with self.subTest(name):
                rec, d, checks = self.drive(results, ci)
                self.assertEqual(checks, 1)
                self.assertEqual((d["result"], d["ended_by"]), (result, "fix_check"))
                self.assertEqual(len(rec["rounds"]), 3)

    def test_refusals(self):
        checked = prloop.record_check(at_limit(), no_findings(), "green")
        round2 = record(rnd(1, [R1_FINDING], commit=SHA1), rnd(2, [R2_FINDING], commit=SHA2), limit=3)
        no_fix = record(rnd(1, [R1_FINDING], commit=SHA1), rnd(2, [R2_FINDING], commit=SHA2),
                        rnd(3, [ACC_FINDING], ci="red"), limit=3)
        cases = [
            ("already checked", checked, no_findings(), "fix_check"),
            ("round 2 of 3", round2, {"Test value": []}, "below the round limit"),
            ("limit round with no fixes", no_fix, {}, "fixed nothing"),
            ("a missed angle", at_limit(), {"Correctness": []}, "Test value"),
            ("an added angle", at_limit(), dict(no_findings(), Accuracy=[]), "Accuracy"),
            ("bad ci", at_limit(), no_findings(), "ci"),
            ("a stop at the limit", record(rnd(1, [R1_FINDING], commit=SHA1),
                                           rnd(2, [R2_FINDING], commit=SHA2, stop="the branch moved"), limit=2),
             {"Test value": []}, "ended"),
            ("a bad bucket", at_limit(), {"Correctness": [finding("check-1", "Correctness", "maybe")],
                                          "Test value": []}, "bucket"),
        ]
        for name, rec, results, says in cases:
            with self.subTest(name):
                ci = "blue" if name == "bad ci" else "green"
                with self.assertRaisesRegex(PrLoopError, says):
                    prloop.record_check(rec, results, ci)

    def test_round_after_check_refused(self):
        checked = prloop.record_check(at_limit(), no_findings(), "green")
        checked["rounds"].append(rnd(4, [R1_FINDING], commit=SHA1))
        with self.assertRaisesRegex(PrLoopError, r"rounds\[3\].*after the check of the fix"):
            prloop.decide(checked)
        d = Path(tempfile.mkdtemp(prefix="ap2-prloop-"))
        self.addCleanup(shutil.rmtree, d, True)
        path = d / "prloop.json"
        path.write_text(json.dumps(checked))
        with self.assertRaisesRegex(PrLoopError, r"prloop\.json: rounds\[3\].*after the check of the fix"):
            prloop.read(path)


class ReadTest(unittest.TestCase):
    """A broken record is refused, naming the file and the field."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ap2-prloop-"))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write(self, data, name="prloop.json") -> Path:
        path = self.dir / name
        path.write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode())
        return path

    def test_good_record_round_trips(self):
        rec = prloop.record_check(at_limit(), no_findings(), "green")
        self.assertEqual(prloop.read(self.write(rec)), rec)
        self.assertEqual(prloop.read(self.write(record(), "empty.json")), record())

    def test_broken_records(self):
        def change(fn, base=None):
            rec = copy.deepcopy(base or at_limit())
            fn(rec)
            return json.dumps(rec).encode()

        four = at_limit()
        four["rounds"].append(rnd(4, [R1_FINDING], commit=SHA1))
        unended = at_limit(limit=None)
        unended.pop("limit")
        checked = prloop.record_check(at_limit(), no_findings(), "green")
        cases = [
            ("too large", b" " * (sources.MAX_SOURCE + 1), "larger than"),
            ("not JSON", b"{rounds: [", "not JSON"),
            ("not UTF-8", b'{"rounds": [], "reason": "\xff"}', "UTF-8"),
            ("not an object", b"[]", "a JSON object"),
            ("rounds not a list", b'{"rounds": {}}', "rounds"),
            ("rounds 1, 3", change(lambda r: r["rounds"][1].update(n=3)), r"rounds\[1\]\.n"),
            ("rounds start at 2", change(lambda r: r["rounds"][0].update(n=2)), r"rounds\[0\]\.n"),
            ("unknown bucket", change(lambda r: r["rounds"][2]["findings"][1].update(bucket="maybe")),
             r"rounds\[2\]\.findings\[1\]\.bucket"),
            ("angle not run", change(lambda r: r["rounds"][0]["findings"][0].update(angle="Portability")),
             r"rounds\[0\]\.findings\[0\]\.angle"),
            ("null angle on a non-CI finding", change(lambda r: r["rounds"][0]["findings"][0].update(angle=None)),
             r"rounds\[0\]\.findings\[0\]\.angle"),
            ("fix with no commit", change(lambda r: r["rounds"][0].update(commit=None)), r"rounds\[0\]\.commit"),
            ("bad sha", change(lambda r: r["rounds"][1].update(commit="HEAD~1")), r"rounds\[1\]\.commit"),
            ("upper-case sha", change(lambda r: r["rounds"][1].update(commit=SHA2.upper())), r"rounds\[1\]\.commit"),
            ("short sha", change(lambda r: r["rounds"][1].update(commit="abc12")), r"rounds\[1\]\.commit"),
            ("more rounds than the limit", json.dumps(four).encode(), r"rounds\[3\].*past the round limit"),
            ("limit 21", change(lambda r: r.update(limit=21)), "limit"),
            ("bad ci", change(lambda r: r["rounds"][0].update(ci="yellow")), r"rounds\[0\]\.ci"),
            ("behavior not a bool", change(lambda r: r["rounds"][0]["findings"][0].update(behavior="no")),
             r"rounds\[0\]\.findings\[0\]\.behavior"),
            ("unknown round key", change(lambda r: r["rounds"][0].update(roles=[])), r"rounds\[0\]"),
            ("unknown top key", change(lambda r: r.update(colour="red")), "colour"),
            ("duplicate finding id", change(lambda r: r["rounds"][2]["findings"][1].update(id="ci-1")),
             r"rounds\[2\]\.findings\[1\]\.id"),
            ("no Correctness", change(lambda r: r["rounds"][0].update(angles=r["rounds"][0]["angles"][1:])),
             r"rounds\[0\]\.angles"),
            ("a round after an end", change(lambda r: r["rounds"][0].update(stop="the branch moved")),
             r"rounds\[1\]"),
            ("check below the limit", change(lambda r: r.update(limit=4), prloop.record_check(
                at_limit(), no_findings(), "green")), "fix_check"),
            ("check of another commit", change(lambda r: r["fix_check"].update(commit=SHA2), prloop.record_check(
                at_limit(), no_findings(), "green")), r"fix_check\.commit"),
            ("check missing an angle", change(lambda r: r["fix_check"]["reviewers"].pop(), prloop.record_check(
                at_limit(), no_findings(), "green")), r"fix_check\.reviewers"),
            ("result says clean, loop goes on", change(lambda r: r.update(result="clean"), unended), "result"),
            ("sha with trailing junk", change(lambda r: r["rounds"][1].update(commit="abcdef0zz")),
             r"rounds\[1\]\.commit"),
            ("null angle on a ci-x finding", change(lambda r: r["rounds"][0]["findings"][0].update(
                id="ci-x", angle=None)), r"rounds\[0\]\.findings\[0\]\.angle"),
            ("round number a bool", change(lambda r: r["rounds"][0].update(n=True)), r"rounds\[0\]\.n"),
            ("check ci blue", change(lambda r: r["fix_check"].update(ci="blue"), checked), r": fix_check\.ci: "),
            ("check finding bucket maybe", change(lambda r: r["fix_check"]["reviewers"][0]["findings"].append(
                {"id": "check-1", "angle": "Correctness", "bucket": "maybe"}), checked),
             r"fix_check\.reviewers\[0\]\.findings\[0\]\.bucket"),
            ("check answers emptied", change(lambda r: r["fix_check"]["reviewers"][0].update(answers=[]), checked),
             r"fix_check\.reviewers\[0\]\.answers"),
            ("checked clean, result says stopped", change(lambda r: r.update(result="stopped"), checked),
             r": result: is 'stopped'"),
            ("checked clean, ended_by says round", change(lambda r: r.update(ended_by="round"), checked),
             r": ended_by: is 'round'"),
        ]
        for name, data, field in cases:
            with self.subTest(name):
                path = self.write(data)
                with self.assertRaises(PrLoopError) as cm:
                    prloop.read(path)
                self.assertIn(str(path), str(cm.exception))
                self.assertRegex(str(cm.exception), field)

    def test_missing_file(self):
        with self.assertRaisesRegex(PrLoopError, "nosuch.json"):
            prloop.read(self.dir / "nosuch.json")

    def test_deep_nesting_is_refused(self):
        # json.loads may refuse this itself (RecursionError) or parse it and leave the checker to hit the limit:
        # either way it is a PrLoopError naming the file.
        path = self.write(b'{"rounds": ' + b"[" * 100000 + b"]" * 100000 + b"}")
        with self.subTest("as this Python parses it"):
            with self.assertRaisesRegex(PrLoopError, re.escape(str(path))):
                prloop.read(path)
        with self.subTest("parsed, so the checker hits the limit"):
            with mock.patch.object(prloop.json, "loads", return_value={"rounds": deep(100000)}):
                with self.assertRaisesRegex(PrLoopError, re.escape(str(path)) + ": .*nested too deep"):
                    prloop.read(path)

    def test_deep_extra_key_on_an_angle_is_refused(self):
        rec = at_limit()
        rec["rounds"][2]["angles"][0]["extra"] = deep(900)
        path = self.write(rec, "deep.json")
        with self.assertRaises(PrLoopError) as cm:
            prloop.read(path)
        self.assertIn(str(path), str(cm.exception))
        self.assertIn("nested too deep", str(cm.exception))

    def test_error_is_a_value_error(self):
        self.assertTrue(issubclass(PrLoopError, ValueError))
        self.assertEqual(prloop.BUCKETS, ("fix", "out_of_scope", "invalid", "owner"))


class DeepRecordTest(unittest.TestCase):
    """A record in memory nested too deep is a PrLoopError, not a RecursionError."""

    def test_every_function_refuses_it(self):
        rec = at_limit()
        rec["rounds"][2]["angles"][0]["extra"] = deep(5000)
        calls = [("decide", lambda: prloop.decide(rec)), ("plan", lambda: prloop.plan(rec)),
                 ("record_check", lambda: prloop.record_check(rec, no_findings(), "green")),
                 ("report", lambda: prloop.report(rec)), ("summary", lambda: prloop.summary(rec)),
                 ("ask", lambda: prloop.ask(rec))]
        for name, call in calls:
            with self.subTest(name):
                with self.assertRaisesRegex(PrLoopError, "^the record: nested too deep$"):
                    call()

    def test_deep_check_results_are_refused(self):
        # Only a repr walks this value, and some Pythons repr 5000 deep: 100000 is past every one's limit.
        results = {"Correctness": [{"id": "check-1", "bucket": "fix", "text": deep(100000)}], "Test value": []}
        with self.assertRaisesRegex(PrLoopError, "^record_check: results: nested too deep$"):
            prloop.record_check(at_limit(), results, "green")


class PurityTest(unittest.TestCase):
    def test_imports_only_stdlib_and_assistant(self):
        text = (ROOT / "lib" / "assistant" / "prloop.py").read_text()
        imports = [line.split()[1] for line in text.splitlines() if line.startswith(("import ", "from "))]
        allowed = {"__future__", "copy", "json", "os", "re", "typing", "assistant"}
        self.assertTrue(set(imports) <= allowed, imports)
        self.assertNotIn("subprocess", text)
        self.assertNotIn("socket", text)


if __name__ == "__main__":
    unittest.main()
