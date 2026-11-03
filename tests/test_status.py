from __future__ import annotations

import unittest

from tests.helpers import TempData, write_ap
from autopilot import cli, config, status


class StatusTest(unittest.TestCase):
    def test_cap_warning(self):
        with TempData() as d:
            for n in ("a", "b", "c", "e"):
                write_ap(d, n)
            g = config.load_global()
            g["max_workers"] = 3
            config.save_global(g)
            self.assertIn("4 autopilots are running but at most 3 workers", status.render(config.list_aps()))

    def test_pause_resume_flag(self):
        with TempData() as d:
            write_ap(d, "checkout")
            cli.main(["pause", "checkout"])
            self.assertEqual(config.status_of("checkout"), "paused")
            cli.main(["resume", "checkout"])
            self.assertEqual(config.status_of("checkout"), "active")

    def test_stop_needs_yes(self):
        with TempData() as d:
            write_ap(d, "checkout")
            self.assertEqual(cli.main(["stop", "checkout"]), 1)
            self.assertEqual(config.status_of("checkout"), "active")
            self.assertEqual(cli.main(["stop", "checkout", "--yes"]), 0)
            self.assertEqual(config.status_of("checkout"), "stopped")


if __name__ == "__main__":
    unittest.main()
