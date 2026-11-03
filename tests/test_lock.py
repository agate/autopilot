from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autopilot import lock


class LockTest(unittest.TestCase):
    def test_lock_exclusive(self):
        p = Path(tempfile.mkdtemp()) / "lock"
        self.assertTrue(lock.acquire(p, "one"))
        self.assertFalse(lock.acquire(p, "two"))
        self.assertEqual(lock.held(p)["label"], "one")
        lock.release(p)
        self.assertIsNone(lock.held(p))
        self.assertTrue(lock.acquire(p, "three"))

    def test_lock_stale_replaced(self):
        p = Path(tempfile.mkdtemp()) / "lock"
        p.write_text(json.dumps({"pid": 999999, "label": "dead", "at": "x"}))
        self.assertIsNone(lock.held(p))
        self.assertTrue(lock.acquire(p, "fresh"))
        self.assertEqual(lock.held(p)["label"], "fresh")


if __name__ == "__main__":
    unittest.main()
