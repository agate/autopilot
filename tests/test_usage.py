from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.helpers import TempData, write_ap
from autopilot import events, usage


def line(req, inp, out, cr=0, cw=0):
    return json.dumps({"type": "assistant", "requestId": req, "message": {"usage": {
        "input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
        "cache_creation_input_tokens": cw}}})


class UsageTest(unittest.TestCase):
    def test_dedupes_by_request_keeping_last(self):
        p = Path(tempfile.mkdtemp()) / "s.jsonl"
        p.write_text("\n".join([line("r1", 10, 5, 100), line("r1", 10, 40, 100), line("r2", 3, 7), "not json"]))
        self.assertEqual(usage.transcript_tokens(p), {"input": 13, "output": 47, "cache_read": 100, "cache_write": 0})

    def test_per_autopilot_sums_ended_workers(self):
        with TempData() as d:
            write_ap(d, "checkout")
            events.append("checkout", "worker_ended", {"task": "PROJ-1", "role": "implement",
                                                       "tokens": {"input": 1, "output": 2, "cache_read": 3, "cache_write": 4}})
            events.append("checkout", "worker_ended", {"task": "PROJ-2", "role": "review",
                                                       "tokens": {"input": 1, "output": 1, "cache_read": 1, "cache_write": 1}})
            self.assertEqual(usage.autopilot_tokens("checkout", live_sessions=[]),
                             {"input": 2, "output": 3, "cache_read": 4, "cache_write": 5})


if __name__ == "__main__":
    unittest.main()
