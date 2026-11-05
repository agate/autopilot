from __future__ import annotations

import unittest

from autopilot import mergecheck

HEAD = "abc123"


def pr(checks=("SUCCESS",), reviews=(("APPROVED", HEAD),), decision="APPROVED", who="teammate"):
    return {"headRefOid": HEAD, "reviewDecision": decision,
            "statusCheckRollup": [{"name": f"c{i}", "conclusion": c, "status": "COMPLETED"} for i, c in enumerate(checks)],
            "reviews": [{"state": s, "commit": {"oid": oid}, "author": {"login": who}} for s, oid in reviews]}


# What `gh pr view` shows when only a review bot has looked at the head commit.
BOT_ONLY = (("COMMENTED", HEAD), ("APPROVED", HEAD))


def thread(resolved=False, typename="User"):
    return {"isResolved": resolved, "comments": {"nodes": [{"author": {"__typename": typename, "login": "x"}}]}}


class MergeCheckTest(unittest.TestCase):
    def test_all_good(self):
        self.assertEqual(mergecheck.evaluate(pr(), [thread(True)], []), (True, []))

    def test_old_approval_refused(self):
        ok, why = mergecheck.evaluate(pr(reviews=(("APPROVED", "old999"),)), [], [])
        self.assertFalse(ok)
        self.assertIn("approval_not_on_head", why)

    def test_approval_missing(self):
        # GitHub says approved but no approval is in the list: fail closed.
        ok, why = mergecheck.evaluate(pr(reviews=()), [], [])
        self.assertEqual((ok, why), (False, ["approval_missing"]))

    def test_review_required_despite_an_approval_on_head(self):
        # Branch protection wants a review the bot cannot give (for example a code owner's).
        p = pr(reviews=BOT_ONLY, decision="REVIEW_REQUIRED", who="review-bot")
        self.assertEqual(mergecheck.evaluate(p, [], []), (False, ["review_required"]))

    def test_review_required_without_reviews(self):
        p = pr(reviews=(), decision="REVIEW_REQUIRED")
        self.assertEqual(mergecheck.evaluate(p, [], []), (False, ["review_required"]))

    def test_review_required_with_an_old_approval(self):
        p = pr(reviews=(("APPROVED", "old999"),), decision="REVIEW_REQUIRED")
        self.assertEqual(mergecheck.evaluate(p, [], []), (False, ["review_required"]))

    def test_bot_approval_on_head_passes_when_github_says_approved(self):
        p = pr(reviews=BOT_ONLY, decision="APPROVED", who="review-bot")
        self.assertEqual(mergecheck.evaluate(p, [], []), (True, []))

    def test_unresolved_human_refused(self):
        ok, why = mergecheck.evaluate(pr(), [thread(False, "User")], [])
        self.assertEqual((ok, why), (False, ["unresolved_human_thread"]))

    def test_unresolved_bot_ok(self):
        self.assertTrue(mergecheck.evaluate(pr(), [thread(False, "Bot")], [])[0])

    def test_failing_check_refused(self):
        ok, why = mergecheck.evaluate(pr(checks=("SUCCESS", "FAILURE")), [], [])
        self.assertIn("checks_not_green", why)

    def test_pending_check_refused(self):
        p = pr()
        p["statusCheckRollup"].append({"name": "slow", "conclusion": "", "status": "IN_PROGRESS"})
        self.assertIn("checks_not_green", mergecheck.evaluate(p, [], [])[1])

    def test_overlap_refused(self):
        ok, why = mergecheck.evaluate(pr(), [], ["src/a.py"])
        self.assertIn("rerun_ci_on_current_main", why)

    def test_no_approval_needed_ok(self):
        self.assertEqual(mergecheck.evaluate(pr(reviews=(), decision=""), [], []), (True, []))

    def test_no_review_decision_at_all_ok(self):
        self.assertEqual(mergecheck.evaluate(pr(reviews=(), decision=None), [], []), (True, []))


if __name__ == "__main__":
    unittest.main()
