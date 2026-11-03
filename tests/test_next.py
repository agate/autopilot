from __future__ import annotations

import unittest

from autopilot import nextwork, state


def t(id, status="todo", deps=(), needs=(), critical=False, phase=1):
    return {"id": id, "title": {"en": id}, "depends_on": list(deps), "status": status, "link": "",
            "needs": list(needs), "critical": critical, "phase": phase, "origin": "source"}


def st(*tasks, workers=(), decisions=()):
    s = state.empty()
    s["tasks"] = list(tasks)
    s["workers"] = list(workers)
    s["decisions"] = list(decisions)
    return s


def w(task, needs=()):
    return {"task": task, "role": "implement", "needs": list(needs)}


class NextTest(unittest.TestCase):
    def test_dep_not_done_not_ready(self):
        s = st(t("PROJ-1", status="in_progress"), t("PROJ-2", deps=["PROJ-1"]))
        self.assertEqual([x["id"] for x in nextwork.ready(s)], [])

    def test_inflight_excluded(self):
        s = st(t("PROJ-1"), workers=[w("PROJ-1")])
        self.assertEqual(nextwork.ready(s), [])

    def test_open_decision_blocks_task(self):
        s = st(t("PROJ-1"), decisions=[{"id": "D1", "task": "PROJ-1", "status": "open"}])
        self.assertEqual(nextwork.ready(s), [])

    def test_order_critical_phase_id(self):
        s = st(t("PROJ-3", phase=1), t("PROJ-2", phase=2, critical=True), t("PROJ-1", phase=1), t("PROJ-4", phase=None))
        self.assertEqual([x["id"] for x in nextwork.ready(s)], ["PROJ-2", "PROJ-1", "PROJ-3", "PROJ-4"])

    def test_round_robin(self):
        a = ("alpha", {}, st(t("PROJ-1"), t("PROJ-2"), t("PROJ-3")))
        b = ("beta", {}, st(t("PROJ-4"), t("PROJ-5")))
        got = nextwork.plan_starts([a, b], [], 4, None)
        self.assertEqual(got, [("alpha", "PROJ-1"), ("beta", "PROJ-4"), ("alpha", "PROJ-2"), ("beta", "PROJ-5")])

    def test_resource_once(self):
        a = ("alpha", {}, st(t("PROJ-1", needs=["emulator"]), t("PROJ-2", needs=["emulator"])))
        self.assertEqual(nextwork.plan_starts([a], [], 5, None), [("alpha", "PROJ-1")])

    def test_resource_held_by_live(self):
        a = ("alpha", {}, st(t("PROJ-1", needs=["emulator"])))
        self.assertEqual(nextwork.plan_starts([a], [w("PROJ-9", needs=["emulator"])], 5, None), [])

    def test_allow_one(self):
        a = ("alpha", {}, st(t("PROJ-1"), t("PROJ-2")))
        self.assertEqual(nextwork.plan_starts([a], [], 5, 1), [("alpha", "PROJ-1")])

    def test_allow_zero(self):
        a = ("alpha", {}, st(t("PROJ-1")))
        self.assertEqual(nextwork.plan_starts([a], [], 5, 0), [])

    def test_cap_reached(self):
        a = ("alpha", {}, st(t("PROJ-1")))
        live = [w("PROJ-7"), w("PROJ-8")]
        self.assertEqual(nextwork.plan_starts([a], live, 2, None), [])


if __name__ == "__main__":
    unittest.main()
