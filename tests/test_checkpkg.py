from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import ROOT
from autopilot import checkpkg


class CheckPkgTest(unittest.TestCase):
    def _tree(self, text):
        d = Path(tempfile.mkdtemp())
        (d / "a.md").write_text(text)
        return d

    def test_scan_flags_email_ticket_identity(self):
        d = self._tree("mail alice@corp.test\nsee ABC-9\nby Jane Roe\nhttps://wiki.corp.test/x\n")  # checkpkg: ignore
        kinds = sorted({f["kind"] for f in checkpkg.scan(d, ["Jane Roe"])})  # checkpkg: ignore
        self.assertEqual(kinds, ["email", "identity", "ticket", "url"])

    def test_scan_allows_examples(self):
        d = self._tree("PROJ-123 PROJ-129 noreply@anthropic.com autopilot@example.com https://example.com/x "
                       "https://claude.ai/artifact https://github.com/org/repo UTF-8 CLAUDE_CODE_MAX-1\n")
        self.assertEqual(checkpkg.scan(d, ["nobody-here"]), [])

    def test_package_itself_clean(self):
        found = checkpkg.scan(ROOT, checkpkg.identities())
        self.assertEqual(found, [], found[:5])


if __name__ == "__main__":
    unittest.main()
