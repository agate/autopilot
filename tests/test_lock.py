from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.helpers import LIB, TempData, write_ap
from autopilot import lock, state


class LockTest(unittest.TestCase):
    def test_lock_exclusive(self):
        p = Path(tempfile.mkdtemp()) / "lock"
        self.assertTrue(lock.acquire(p, "one"))
        self.assertFalse(lock.acquire(p, "two"))
        self.assertEqual(lock.held(p)["label"], "one")
        lock.release(p)
        self.assertIsNone(lock.held(p))
        self.assertTrue(lock.acquire(p, "three"))
        lock.release(p)

    def test_lock_freed_when_holder_dies(self):
        p = Path(tempfile.mkdtemp()) / "lock"
        code = (f"import sys,time; sys.path.insert(0,{str(LIB)!r}); from autopilot import lock; "
                f"lock.acquire({str(p)!r}, 'child'); print('held', flush=True); time.sleep(30)")
        child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        self.assertEqual(child.stdout.readline().strip(), "held")
        self.assertEqual(lock.held(p)["label"], "child")
        self.assertFalse(lock.acquire(p, "parent"))
        child.kill()
        child.wait()
        self.assertIsNone(lock.held(p))
        self.assertTrue(lock.acquire(p, "parent"))
        lock.release(p)

    def test_old_pid_file_is_not_a_lock(self):
        p = Path(tempfile.mkdtemp()) / "lock"
        p.write_text('{"pid": 1, "label": "left over after a reboot", "at": "x"}')
        self.assertIsNone(lock.held(p))
        self.assertTrue(lock.acquire(p, "fresh"))
        lock.release(p)

    def test_state_mutate_serializes_writers(self):
        with TempData() as d:
            write_ap(d, "checkout")
            with state.mutate("checkout") as st:
                st["last_report"] = None
            code = (f"import os,sys; os.environ['AUTOPILOT_DATA']={str(d)!r}; sys.path.insert(0,{str(LIB)!r}); "
                    f"from autopilot import state; import time\n"
                    f"with state.mutate('checkout') as st:\n    print('in', flush=True); time.sleep(1.5)")
            child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
            self.assertEqual(child.stdout.readline().strip(), "in")
            t0 = time.monotonic()
            with state.mutate("checkout"):
                waited = time.monotonic() - t0
            child.wait()
            self.assertGreater(waited, 0.5)


if __name__ == "__main__":
    unittest.main()
