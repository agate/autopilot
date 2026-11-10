from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import ROOT
from tests.test_ap2_cli import temp_inside_the_accounts_claude
from tests.test_ap2_store import card, plain

AP2 = ROOT / "bin" / "ap2"


class CardCommandTest(unittest.TestCase):
    def setUp(self):
        if temp_inside_the_accounts_claude():
            self.skipTest("the temp folder is inside the account's ~/.claude, so ap2 refuses every temp data folder")
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-card-cli-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.data = self.tmp / "data"

    def run_ap2(self, *args, data=None, stdin=None):
        env = {k: v for k, v in os.environ.items() if k != "AP2_DATA"}
        env["HOME"] = str(self.home)  # the real home folder is never reached
        env["AP2_DATA"] = str(self.data if data is None else data)
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run([sys.executable, str(AP2), *args], capture_output=True, text=True, env=env,
                              encoding="utf-8", errors="replace", input=stdin, timeout=30)

    def card_file(self, name="card.json", **over):
        path = self.tmp / name
        path.write_text(json.dumps(card(**over)), encoding="utf-8")
        return path

    def ok(self, *args, **kw):
        r = self.run_ap2(*args, **kw)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        return json.loads(r.stdout)

    def fails(self, *args, **kw):
        r = self.run_ap2(*args, **kw)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(r.stdout, "")
        lines = r.stderr.splitlines()
        self.assertEqual(len(lines), 1, r.stderr)
        self.assertTrue(lines[0].startswith("ap2: "), r.stderr)
        return lines[0]

    def test_add_show_list(self):
        added = self.ok("card", "add", str(self.card_file()), "--by", "tester")
        self.assertRegex(added["id"], r"^c-\d{8}-\d{6}-[0-9a-f]{6}$")
        self.assertEqual(added["audit"][0]["by"], "tester")
        self.assertTrue(list((self.data / "items").glob(f"*/{added['id']}.json")))
        self.assertEqual(self.ok("card", "show", added["id"]), added)
        self.assertEqual(self.ok("card", "list"), [added])
        self.assertEqual(self.ok("card", "list", "--lane", "planned"), [added])
        self.assertEqual(self.ok("card", "list", "--lane", "done"), [])

    def test_add_reads_stdin(self):
        added = self.ok("card", "add", "-", stdin=json.dumps(card(title="From stdin")))
        self.assertEqual(added["title"], "From stdin")
        self.assertEqual(added["audit"][0]["by"], "ap2")

    def test_add_from_a_closed_stdin_is_one_line(self):
        env = {k: v for k, v in os.environ.items() if k != "AP2_DATA"}
        env.update(HOME=str(self.home), AP2_DATA=str(self.data))
        # '<&-' closes stdin, so Python starts with sys.stdin set to None
        r = subprocess.run(["sh", "-c", '"$0" "$1" card add - <&-', sys.executable, str(AP2)],
                           capture_output=True, text=True, env=env, errors="replace", timeout=30)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(r.stderr.strip(), "ap2: cannot read -: stdin is closed")
        self.assertNotIn("Traceback", r.stderr)

    def run_latin1_stdin(self, data: bytes):
        """`ap2 card add -` with the given stdin bytes, under a locale encoding that is not UTF-8."""
        env = {k: v for k, v in os.environ.items() if k != "AP2_DATA"}
        env.update(HOME=str(self.home), AP2_DATA=str(self.data), PYTHONIOENCODING="latin-1")
        return subprocess.run([sys.executable, str(AP2), "card", "add", "-"], capture_output=True, env=env,
                              input=data, timeout=30)

    def test_add_reads_stdin_as_utf8(self):
        r = self.run_latin1_stdin(json.dumps(card(title="完成"), ensure_ascii=False).encode("utf-8"))
        self.assertEqual(r.returncode, 0, r.stderr)
        stored = list((self.data / "items").glob("*/*.json"))
        self.assertEqual(len(stored), 1)
        self.assertEqual(json.loads(stored[0].read_text(encoding="utf-8"))["title"], "完成")

    def test_add_from_stdin_that_is_not_utf8_is_one_line(self):
        r = self.run_latin1_stdin(b"\xff")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(r.stdout, b"")
        lines = r.stderr.decode("utf-8", "replace").splitlines()
        self.assertEqual(len(lines), 1, r.stderr)
        self.assertTrue(lines[0].startswith("ap2: cannot read '-': "), r.stderr)
        self.assertFalse(list(self.data.glob("items/*/*.json")))

    def test_a_file_name_with_a_newline_is_one_line(self):
        line = self.fails("card", "add", str(self.tmp / "bad\nname.json"))
        self.assertIn("cannot read", line)
        (self.tmp / "bad\nnot-json.json").write_text("{not json")
        self.assertIn("not JSON", self.fails("card", "add", str(self.tmp / "bad\nnot-json.json")))

    def test_list_filters_by_kind_and_parent(self):
        parent = self.ok("card", "add", str(self.card_file(kind="question")))
        child = self.ok("card", "add", str(self.card_file("child.json", parent=parent["id"])))
        self.assertEqual([c["id"] for c in self.ok("card", "list", "--kind", "question")], [parent["id"]])
        self.assertEqual([c["id"] for c in self.ok("card", "list", "--parent", parent["id"])], [child["id"]])

    def test_search(self):
        added = self.ok("card", "add", str(self.card_file(plain=plain(what="the nightly vendor export"))))
        self.assertEqual([c["id"] for c in self.ok("search", "vendor", "EXPORT")], [added["id"]])
        self.assertEqual([c["id"] for c in self.ok("search", "vendor export")], [added["id"]])
        self.assertEqual(self.ok("search", "nothing-like-this"), [])
        (self.data / "index.sqlite").unlink()
        self.assertEqual([c["id"] for c in self.ok("search", "vendor")], [added["id"]])

    def test_restore(self):
        from assistant.store import Store

        added = self.ok("card", "add", str(self.card_file()))
        changed = Store(self.data).update(added["id"], {"lane": "done"}, by="test")
        point = changed["audit"][-1]["restore"]
        back = self.ok("restore", point, "--by", "tester")
        self.assertEqual(back["lane"], "planned")
        self.assertEqual(back["audit"][-1]["did"], f"restore {point}")
        self.assertEqual(back["audit"][-1]["by"], "tester")
        again = self.ok("restore", back["audit"][-1]["restore"])
        self.assertEqual(again["lane"], "done")
        self.assertEqual(self.ok("card", "show", added["id"])["lane"], "done")

    def test_data_flag_wins_over_env(self):
        other = self.tmp / "other"
        added = self.ok("card", "add", str(self.card_file()), "--data", str(other))
        self.assertTrue(list((other / "items").glob(f"*/{added['id']}.json")))
        self.assertEqual(self.ok("card", "list"), [])
        self.assertEqual(self.ok("card", "list", "--data", str(other)), [added])
        self.assertEqual(self.ok("card", "show", added["id"], "--data", str(other)), added)
        self.assertEqual(len(self.ok("search", "payment", "--data", str(other))), 1)

    def test_plain_words_error_is_one_line(self):
        (self.data).mkdir()
        (self.data / "config.yaml").write_text("secondary_language: zh\n")
        line = self.fails("card", "add", str(self.card_file()))
        self.assertIn("plain.what.zh missing", line)
        self.assertFalse(list((self.data / "items").glob("*/*.json")))

    def test_errors_are_one_line_with_exit_1(self):
        self.assertIn("lane must be one of", self.fails("card", "add", str(self.card_file(lane="later"))))
        self.assertIn("cannot read", self.fails("card", "add", str(self.tmp / "missing.json")))
        (self.tmp / "bad.json").write_text("{not json")
        self.assertIn("not JSON", self.fails("card", "add", str(self.tmp / "bad.json")))
        self.assertIn("no card", self.fails("card", "show", "c-20261110-143005-abcdef"))
        self.assertIn("not a card id", self.fails("card", "show", "../../etc/passwd"))
        self.assertIn("no restore point", self.fails("restore", "r-20261110-143005-abcdef"))
        self.assertIn("not a restore point id", self.fails("restore", "nope"))
        self.assertIn(".claude", self.fails("card", "list", data=self.home / ".claude" / "assistant"))
        self.assertFalse((self.home / ".claude").exists())

    def test_undecodable_argument_bytes_are_one_line(self):
        self.ok("card", "add", str(self.card_file()))
        self.assertIn("not valid Unicode", self.fails("search", b"\xff"))
        self.assertIn("not valid Unicode", self.fails("card", "add", str(self.card_file()), "--by", b"\xff"))

    def test_corrupt_card_file_is_named(self):
        added = self.ok("card", "add", str(self.card_file()))
        path = next((self.data / "items").glob(f"*/{added['id']}.json"))
        path.write_text("{")
        self.assertIn(str(path), self.fails("card", "show", added["id"]))
        self.assertIn(str(path), self.fails("card", "list"))

    def test_usage_lists_the_new_commands(self):
        r = self.run_ap2()
        self.assertEqual(r.returncode, 2)
        for name in ("config", "card", "search", "restore"):
            self.assertIn(name, r.stderr)

    def test_into_a_pipe_nobody_reads_exits_1_without_a_traceback(self):
        added = self.ok("card", "add", str(self.card_file()))
        env = {k: v for k, v in os.environ.items() if k != "AP2_DATA"}
        env.update(HOME=str(self.home), AP2_DATA=str(self.data))
        r, w = os.pipe()
        os.close(r)
        try:
            proc = subprocess.Popen([sys.executable, str(AP2), "card", "show", added["id"]],
                                    stdin=subprocess.DEVNULL, stdout=w, stderr=subprocess.PIPE, env=env)
        finally:
            os.close(w)
        _, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 1, err)
        self.assertEqual(err, b"")

    def test_non_ascii_card_under_latin1_output_prints_parseable_json(self):
        (self.data).mkdir()
        (self.data / "config.yaml").write_text("secondary_language: zh\n")
        added = self.ok("card", "add", str(self.card_file(plain=plain(("en", "zh"), what="付款"))))
        env = {k: v for k, v in os.environ.items() if k not in ("AP2_DATA", "PYTHONUTF8")}
        env.update(HOME=str(self.home), AP2_DATA=str(self.data), PYTHONIOENCODING="latin-1")
        r = subprocess.run([sys.executable, str(AP2), "card", "show", added["id"]], capture_output=True,
                           env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout.decode("latin-1")), added)


if __name__ == "__main__":
    unittest.main()
