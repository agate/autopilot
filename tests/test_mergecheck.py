from __future__ import annotations

import unittest

from autopilot import mergecheck

HEAD = "abc123"


def pr(checks=("SUCCESS",), reviews=(("APPROVED", HEAD),), decision="APPROVED"):
    return {"headRefOid": HEAD, "reviewDecision": decision,
            "statusCheckRollup": [{"name": f"c{i}", "conclusion": c, "status": "COMPLETED"} for i, c in enumerate(checks)],
            "reviews": [{"state": s, "commit": {"oid": oid}, "author": {"login": "teammate"}} for s, oid in reviews]}


def thread(resolved=False, typename="User"):
    return {"isResolved": resolved, "comments": {"nodes": [{"author": {"__typename": typename, "login": "x"}}]}}


class MergeCheckTest(unittest.TestCase):
    def test_all_good(self):
        self.assertEqual(mergecheck.evaluate(pr(), [thread(True)], [], True), (True, []))

    def test_old_approval_refused(self):
        ok, why = mergecheck.evaluate(pr(reviews=(("APPROVED", "old999"),)), [], [], True)
        self.assertFalse(ok)
        self.assertIn("approval_not_on_head", why)

    def test_approval_missing(self):
        ok, why = mergecheck.evaluate(pr(reviews=(), decision="REVIEW_REQUIRED"), [], [], True)
        self.assertIn("approval_missing", why)

    def test_unresolved_human_refused(self):
        ok, why = mergecheck.evaluate(pr(), [thread(False, "User")], [], True)
        self.assertEqual((ok, why), (False, ["unresolved_human_thread"]))

    def test_unresolved_bot_ok(self):
        self.assertTrue(mergecheck.evaluate(pr(), [thread(False, "Bot")], [], True)[0])

    def test_failing_check_refused(self):
        ok, why = mergecheck.evaluate(pr(checks=("SUCCESS", "FAILURE")), [], [], True)
        self.assertIn("checks_not_green", why)

    def test_pending_check_refused(self):
        p = pr()
        p["statusCheckRollup"].append({"name": "slow", "conclusion": "", "status": "IN_PROGRESS"})
        self.assertIn("checks_not_green", mergecheck.evaluate(p, [], [], True)[1])

    def test_overlap_refused(self):
        ok, why = mergecheck.evaluate(pr(), [], ["src/a.py"], True)
        self.assertIn("rerun_ci_on_current_main", why)

    def test_no_approval_needed_ok(self):
        self.assertTrue(mergecheck.evaluate(pr(reviews=(), decision=""), [], [], False)[0])


if __name__ == "__main__":
    unittest.main()
