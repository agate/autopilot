from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT  # noqa: F401  (puts lib/ on sys.path)
from assistant import config, datadir
from assistant.config import Config, ConfigError, ShadowModeError


class TempHome:
    """A fresh temp folder used as HOME, with AP2_DATA cleared, so no test can reach the real home folder."""

    def __enter__(self) -> Path:
        self.dir = Path(tempfile.mkdtemp(prefix="ap2-home-")).resolve()
        env = {k: v for k, v in os.environ.items() if k != "AP2_DATA"}
        env["HOME"] = str(self.dir)
        self.patch = mock.patch.dict(os.environ, env, clear=True)
        self.patch.start()
        return self.dir

    def __exit__(self, *exc) -> None:
        self.patch.stop()
        shutil.rmtree(self.dir, ignore_errors=True)


def write_config(data: Path, text: str) -> Path:
    data.mkdir(parents=True, exist_ok=True)
    (data / "config.yaml").write_text(text)
    return data


class DefaultsTest(unittest.TestCase):
    def setUp(self):
        self.data = Path(tempfile.mkdtemp(prefix="ap2-data-"))
        self.addCleanup(shutil.rmtree, self.data, True)

    def assert_defaults(self, cfg):
        self.assertEqual(cfg.primary_language, "en")
        self.assertIsNone(cfg.secondary_language)
        self.assertEqual(cfg.mode, "shadow")
        self.assertEqual(cfg.keep_transcripts, "always")

    def test_missing_file(self):
        self.assert_defaults(config.load(self.data))

    def test_empty_file(self):
        self.assert_defaults(config.load(write_config(self.data, "")))

    def test_absent_keys_take_defaults(self):
        cfg = config.load(write_config(self.data, "mode: live\n"))
        self.assertEqual(cfg.mode, "live")
        self.assertEqual(cfg.primary_language, "en")
        self.assertIsNone(cfg.secondary_language)
        self.assertEqual(cfg.keep_transcripts, "always")

    def test_empty_secondary_language_is_none(self):
        self.assertIsNone(config.load(write_config(self.data, "secondary_language:\n")).secondary_language)
        self.assertIsNone(config.load(write_config(self.data, "secondary_language: ~\n")).secondary_language)
        self.assertIsNone(config.load(write_config(self.data, "secondary_language: ''\n")).secondary_language)
        self.assertIsNone(config.load(write_config(self.data, 'secondary_language: ""\n')).secondary_language)

    def test_written_template_reads_back_as_defaults(self):
        self.assert_defaults(config.load(write_config(self.data, config.DEFAULT_TEXT)))
        self.assertEqual(config.load(self.data), Config())

    def test_template_comments_every_key(self):
        lines = config.DEFAULT_TEXT.splitlines()
        for key in config.KEYS:
            i = next(n for n, line in enumerate(lines) if line.startswith(key + ":"))
            self.assertTrue(lines[i - 1].startswith("#"), key)

    def test_valid_values(self):
        text = "primary_language: pt-BR\nsecondary_language: zh\nmode: live\nkeep_transcripts: 90d\n"
        cfg = config.load(write_config(self.data, text))
        self.assertEqual((cfg.primary_language, cfg.secondary_language, cfg.mode, cfg.keep_transcripts),
                         ("pt-BR", "zh", "live", "90d"))
        self.assertEqual(config.load(write_config(self.data, "keep_transcripts: never\n")).keep_transcripts,
                         "never")


# One row per refusal: (name, config.yaml text, words the error must hold).
REFUSALS = [
    ("mode not shadow or live", "mode: dry-run\n", "mode"),
    ("mode not a string", "mode: true\n", "mode"),
    ("keep_transcripts unknown word", "keep_transcripts: sometimes\n", "keep_transcripts"),
    ("keep_transcripts bare number", "keep_transcripts: 90\n", "keep_transcripts"),
    ("keep_transcripts zero days", "keep_transcripts: 0d\n", "keep_transcripts"),
    ("keep_transcripts weeks", "keep_transcripts: 2w\n", "keep_transcripts"),
    ("keep_transcripts trailing text", "keep_transcripts: 90days\n", "keep_transcripts"),
    ("primary_language long name", "primary_language: english\n", "primary_language"),
    ("primary_language lower region", "primary_language: pt-br\n", "primary_language"),
    ("primary_language empty", "primary_language:\n", "primary_language"),
    ("primary_language empty quotes", "primary_language: ''\n", "primary_language"),
    ("secondary_language not a code", "secondary_language: Chinese\n", "secondary_language"),
    ("secondary_language a number", "secondary_language: 12\n", "secondary_language"),
    ("secondary_language false", "secondary_language: false\n", "secondary_language"),
    ("secondary equals primary", "primary_language: zh\nsecondary_language: zh\n", "same as primary_language"),
    ("secondary equals default primary", "secondary_language: en\n", "same as primary_language"),
]


class ValidationTest(unittest.TestCase):
    def setUp(self):
        self.data = Path(tempfile.mkdtemp(prefix="ap2-data-"))
        self.addCleanup(shutil.rmtree, self.data, True)

    def test_each_refusal(self):
        for name, text, words in REFUSALS:
            with self.subTest(name):
                with self.assertRaises(ConfigError) as ctx:
                    config.load(write_config(self.data, text))
                msg = str(ctx.exception)
                self.assertIn(words, msg)
                self.assertIn("config.yaml", msg)

    def test_refusal_names_the_line(self):
        with self.assertRaises(ConfigError) as ctx:
            config.load(write_config(self.data, "# c\nprimary_language: en\nmode: loud\n"))
        self.assertIn("line 3:", str(ctx.exception))

    def test_unknown_key_named_with_its_line(self):
        with self.assertRaises(ConfigError) as ctx:
            config.load(write_config(self.data, "mode: shadow\n\ndata_dir: /tmp/x\n"))
        msg = str(ctx.exception)
        self.assertIn("unknown key 'data_dir'", msg)
        self.assertIn("line 3:", msg)

    def test_yaml_error_becomes_config_error(self):
        with self.assertRaises(ConfigError) as ctx:
            config.load(write_config(self.data, "mode: shadow\nmode: live\n"))
        self.assertIn("line 2:", str(ctx.exception))
        self.assertIn("duplicate key", str(ctx.exception))

    def test_top_level_must_be_a_map(self):
        with self.assertRaises(ConfigError):
            config.load(write_config(self.data, "- mode\n"))


class RequireLiveTest(unittest.TestCase):
    def test_shadow_refuses_naming_what(self):
        with self.assertRaises(ShadowModeError) as ctx:
            config.require_live(Config(), "move a Jira ticket")
        self.assertIn("move a Jira ticket", str(ctx.exception))
        self.assertIn("shadow", str(ctx.exception))

    def test_live_returns(self):
        self.assertIsNone(config.require_live(Config(mode="live"), "move a Jira ticket"))


class LookupTest(unittest.TestCase):
    def test_default_is_documents_assistant_under_home(self):
        with TempHome() as home:
            self.assertEqual(datadir.resolve(), home / "Documents" / "Assistant")

    def test_env_wins_over_default(self):
        with TempHome() as home:
            os.environ["AP2_DATA"] = str(home / "elsewhere")
            self.assertEqual(datadir.resolve(), home / "elsewhere")
            self.assertFalse((home / "Documents").exists())

    def test_flag_wins_over_env(self):
        with TempHome() as home:
            os.environ["AP2_DATA"] = str(home / "from-env")
            self.assertEqual(datadir.resolve(str(home / "from-flag")), home / "from-flag")

    def test_empty_env_falls_back_to_default(self):
        with TempHome() as home:
            os.environ["AP2_DATA"] = ""
            self.assertEqual(datadir.resolve(), home / "Documents" / "Assistant")

    def test_empty_flag_is_refused(self):
        with TempHome() as home:
            with self.assertRaises(ConfigError) as ctx:
                datadir.resolve("")
            self.assertIn("--data is empty", str(ctx.exception))
            self.assertFalse((home / "Documents").exists())

    def test_tilde_expands_against_home(self):
        with TempHome() as home:
            self.assertEqual(datadir.resolve("~/notes"), home / "notes")

    def test_inside_dot_claude_is_refused(self):
        with TempHome() as home:
            for p in ("~/.claude", "~/.claude/assistant", str(home / ".claude" / "autopilot-data"),
                      "~/nope/../.claude/x"):  # '..' is folded away before the check
                with self.subTest(p):
                    with self.assertRaises(ConfigError) as ctx:
                        datadir.resolve(p)
                    self.assertIn(".claude", str(ctx.exception))
            os.environ["AP2_DATA"] = str(home / ".claude" / "x")
            with self.assertRaises(ConfigError):
                datadir.resolve()

    def test_symlink_into_dot_claude_is_refused(self):
        with TempHome() as home:
            (home / ".claude").mkdir()
            (home / "link").symlink_to(home / ".claude")
            with self.assertRaises(ConfigError):
                datadir.resolve(str(home / "link"))

    @unittest.skipUnless(sys.platform == "darwin", "macOS firmlinks")
    def test_alias_that_resolve_does_not_fold_is_refused(self):
        # Path.resolve() keeps the /System/Volumes/Data firmlink prefix, so only the same-folder check catches it.
        with TempHome() as home:
            (home / ".claude").mkdir()
            alias = "/System/Volumes/Data" + str(home.resolve()) + "/.claude/x"
            if not Path("/System/Volumes/Data" + str(home.resolve())).exists():
                self.skipTest("no /System/Volumes/Data firmlink")
            with self.assertRaises(ConfigError):
                datadir.resolve(alias)

    def test_other_case_of_dot_claude_is_refused(self):
        # On a case-insensitive file system (macOS APFS by default) ~/.CLAUDE is the same folder as ~/.claude.
        with TempHome() as home:
            (home / ".claude").mkdir()
            if not (home / ".CLAUDE").exists():
                self.skipTest("case-sensitive file system")
            with self.assertRaises(ConfigError):
                datadir.resolve("~/.CLAUDE/assistant")

    def test_other_case_of_dot_claude_is_refused_before_it_exists(self):
        # The name check ignores letter case, so this holds on every OS and leaves nothing behind.
        with TempHome() as home:
            with self.assertRaises(ConfigError):
                datadir.resolve("~/.CLAUDE/assistant")
            with self.assertRaises(ConfigError):
                datadir.resolve("~/.Claude")
            self.assertEqual(list(home.iterdir()), [])

    def test_similar_name_is_allowed(self):
        with TempHome() as home:
            self.assertEqual(datadir.resolve("~/.claude-notes"), home / ".claude-notes")
            self.assertEqual(datadir.resolve("~/.claudex"), home / ".claudex")
            self.assertEqual(datadir.resolve("~/.CLAUDEX/x"), home / ".CLAUDEX" / "x")


class LayoutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-data-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "Assistant"

    def test_first_use_creates_the_layout(self):
        datadir.ensure(self.data)
        for sub in ("items", "chat", "sessions", "templates", "sources", "routines", "restore", "attachments"):
            self.assertTrue((self.data / sub).is_dir(), sub)
        self.assertFalse((self.data / "index.sqlite").exists())
        self.assertEqual((self.data / "config.yaml").read_text(), config.DEFAULT_TEXT)
        self.assertEqual(config.load(self.data), Config())

    def test_existing_config_is_never_overwritten(self):
        write_config(self.data, "mode: live  # mine\n")
        datadir.ensure(self.data)
        datadir.ensure(self.data)
        self.assertEqual((self.data / "config.yaml").read_text(), "mode: live  # mine\n")
        self.assertTrue((self.data / "items").is_dir())

    def test_a_file_in_the_way_is_refused(self):
        self.data.write_text("not a folder")
        with self.assertRaises(ConfigError):
            datadir.ensure(self.data)


if __name__ == "__main__":
    unittest.main()
