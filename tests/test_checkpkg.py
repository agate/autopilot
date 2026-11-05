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

    def test_svg_namespace_is_not_a_leak(self):
        # Every SVG file names this standards namespace; it says nothing about who wrote the package.
        d = self._tree('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"></svg>\n')
        self.assertEqual(checkpkg.scan(d, ["nobody-here"]), [])

    def test_package_version_is_not_an_email(self):
        # A pinned CDN package reads like an address (name@1.7.0) but its "domain" is all digits.
        d = self._tree('<link href="https://cdn.jsdelivr.net/npm/some-webfont@1.7.0/regular.css">\n')
        self.assertEqual(checkpkg.scan(d, ["nobody-here"]), [])

    def test_worktree_git_file_is_skipped(self):
        # In a git worktree, .git is a file naming the main checkout's path; it is never part of the package.
        d = self._tree("clean\n")
        (d / ".git").write_text("gitdir: /home/Jane Roe/repo/.git/worktrees/x\n")  # checkpkg: ignore
        self.assertEqual(checkpkg.scan(d, ["Jane Roe"]), [])  # checkpkg: ignore

    def test_package_itself_clean(self):
        found = checkpkg.scan(ROOT, checkpkg.identities())
        self.assertEqual(found, [], found[:5])


if __name__ == "__main__":
    unittest.main()
