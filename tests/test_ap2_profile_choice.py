"""Choosing an access profile's model and effort from a list fetched fresh (design section 12, access profiles,
"Model and effort choice"): `chosen_at`, `model_choices` / `choices` / `choose`, and `ap2 profile choices`,
`choose`, `add` and `use` asking once, in a terminal or with exit 3 without one. Ported from the command-line half
of v1's `tests/test_profile_choice.py`. Temp folders only."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.helpers import ROOT
from tests.test_ap2_cli import temp_inside_the_accounts_claude
from tests.test_ap2_profile_launch import opt
from tests.test_ap2_profiles import URL, gateway
from tests.test_ap2_workers import WorkerCase
from assistant import cli, profiles
from assistant.profiles import ModelsUnavailable, ProfileError

AP2 = ROOT / "bin" / "ap2"
LISTED = '["claude-opus-5-5", "claude-haiku-4-5", "claude-sonnet-5-5", "gpt-x"]'
WHEN = "2026-11-12T10:00:00-08:00"
CTRL_C = KeyboardInterrupt  # an answer that stands for the user pressing Ctrl-C at the prompt


def counting(folder: Path, listed: str = LISTED) -> str:
    """A stand-in for a gateway's models command: it counts its runs in a temp file and prints `listed`."""
    return f"echo run >> {shlex.quote(str(folder / 'models-runs'))}; printf '%s' {shlex.quote(listed)}"


def runs(folder: Path) -> int:
    f = folder / "models-runs"
    return len(f.read_text().splitlines()) if f.exists() else 0


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-profile-choice-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "data"
        self.data.mkdir()

    def gateway(self, name="g", listed=LISTED, **over) -> dict:
        """A stored gateway profile whose models command counts its runs; unchosen unless `over` says otherwise."""
        p = gateway(name, **{"model": None, "effort": None, "small_model": "claude-haiku-4-5", **over})
        p["gateway"]["models_command"] = counting(self.tmp, listed)
        return profiles.save(self.data, p)

    def file(self, name="g") -> Path:
        return self.data / "profiles" / f"{name}.json"


class CliCase(Case):
    def setUp(self):
        if temp_inside_the_accounts_claude():
            self.skipTest("the temp folder is inside the account's ~/.claude, so ap2 refuses every temp data folder")
        super().setUp()

    def run_cli(self, *argv, tty=False, answers=None):
        """`ap2 profile ...` in this process, with stdin a terminal or not; `answers` are what the user types at
        each prompt (`CTRL_C`: Ctrl-C there), and the end of them is the end of input."""
        out, err = io.StringIO(), io.StringIO()
        typed = iter(answers or [])

        def fake_input(prompt=""):
            err.write(prompt)
            try:
                got = next(typed)
            except StopIteration:
                raise EOFError from None
            if got is CTRL_C:
                raise KeyboardInterrupt
            return got

        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(sys, "stdin", SimpleNamespace(isatty=lambda: tty)), \
                mock.patch("builtins.input", fake_input):
            try:
                code = cli.main(["profile", *argv, "--data", str(self.data)])
            except KeyboardInterrupt:  # caught here, so it fails this test instead of stopping the whole run
                self.fail("Ctrl-C at a prompt escaped `ap2 profile` as a traceback")
        return code, out.getvalue(), err.getvalue()

    def add_gateway(self, *extra, tty=False, answers=None):
        return self.run_cli("add", "g", "--gateway", "--base-url", URL, "--key-command", "echo k",
                            "--small-model", "claude-haiku-4-5", "--daily-cap", "50",
                            "--models-command", counting(self.tmp), *extra, tty=tty, answers=answers)

    def assert_one_error(self, code, out, err, text=""):
        self.assertEqual((code, out), (1, ""), err)
        self.assertEqual(len(err.splitlines()), 1, err)
        self.assertTrue(err.startswith("ap2: "), err)
        self.assertIn(text, err)


# -- criterion 1: chosen_at -------------------------------------------------------------------------------------------

class ChosenAtTest(Case):
    def test_the_field_is_stored_and_blank_by_default(self):
        self.assertEqual(profiles.FIELDS[-1], "chosen_at")
        self.assertIsNone(profiles._blank("x")["chosen_at"])
        self.assertIsNone(profiles.builtin()["chosen_at"])
        self.assertIsNone(self.gateway()["chosen_at"])
        self.assertIn("chosen_at", json.loads(self.file().read_text()))

    def test_validate_takes_an_iso_time_or_null_only(self):
        base = {"name": "s", "mode": "subscription"}
        for good in (None, WHEN, "2026-11-12T18:00:00", "2026-11-12"):
            self.assertEqual(profiles.validate(dict(base, chosen_at=good)), [], good)
        for bad in (5, True, "", "yesterday", "2026-13-40", ["2026-11-12"]):
            self.assertEqual(profiles.validate(dict(base, chosen_at=bad)),
                             ["chosen_at: must be an ISO time or null"], bad)
        with self.assertRaisesRegex(ProfileError, "chosen_at: must be an ISO time or null"):
            profiles.save(self.data, dict(base, chosen_at="soon"))
        self.assertFalse(self.file("s").exists())

    def test_needs_choice(self):
        p = self.gateway()
        self.assertTrue(profiles.needs_choice(self.data, p))
        self.assertFalse(profiles.needs_choice(self.data, dict(p, chosen_at=WHEN)))
        # The subscription never needs one: built in, or stored with no chosen_at (v2 has no migration).
        self.assertFalse(profiles.needs_choice(self.data, profiles.load(self.data, "subscription")))
        profiles.save(self.data, dict(profiles.builtin(), model="claude-opus-5-5"))
        self.assertIsNone(json.loads(self.file("subscription").read_text())["chosen_at"])
        self.assertFalse(profiles.needs_choice(self.data, profiles.load(self.data, "subscription")))
        # A stored subscription.json written by hand, with no chosen_at key at all.
        self.file("subscription").write_text(json.dumps({"name": "subscription", "mode": "subscription"}))
        self.assertFalse(profiles.needs_choice(self.data, profiles.load(self.data, "subscription")))
        # Only the profile named subscription is exempt: another subscription-mode profile needs a choice.
        self.assertTrue(profiles.needs_choice(self.data, dict(profiles.builtin(), name="s")))

    def test_from_args_marks_a_profile_chosen_only_with_both_flags(self):
        def args(**kw):
            base = dict(name="s", gateway=False, label=None, model=None, dispatcher_model=None, small_model=None,
                        effort=None, base_url=None, key_command=None, refresh_command=None, rotate_command=None,
                        models_command=None, daily_cap=None, env=[], settings_file=None)
            return SimpleNamespace(**{**base, **kw})

        with mock.patch.object(profiles, "now", return_value=WHEN):
            self.assertEqual(profiles.from_args(args(model="claude-opus-5-5", effort="high"))["chosen_at"], WHEN)
            self.assertIsNone(profiles.from_args(args(model="claude-opus-5-5"))["chosen_at"])
            self.assertIsNone(profiles.from_args(args(effort="high"))["chosen_at"])
            both_default = profiles.from_args(args(model="default", effort="low"))
            self.assertEqual((both_default["model"], both_default["chosen_at"]), (None, WHEN))

    def test_now_is_an_iso_time_with_its_offset(self):
        from datetime import datetime
        t = datetime.fromisoformat(profiles.now())
        self.assertIsNotNone(t.utcoffset())


# -- criterion 2: the choice lists, fetched fresh -----------------------------------------------------------------------

class ModelChoicesTest(Case):
    def test_the_constants(self):
        self.assertEqual(profiles.EFFORTS, ("low", "medium", "high", "xhigh", "max"))
        self.assertEqual((profiles.RECOMMENDED_MODEL, profiles.RECOMMENDED_EFFORT, profiles.DEFAULT_MODEL),
                         ("claude-opus-5-5[1m]", "high", "default"))
        self.assertEqual((profiles.CHOICE_TIMEOUT, profiles.NEEDS_CHOICE), (20, 3))
        self.assertTrue(issubclass(ModelsUnavailable, ProfileError))

    def test_a_gateway_list_offers_the_1m_variant_first(self):
        p = self.gateway(listed='["claude-opus-5-5", "claude-haiku-4-5", "gpt-x"]')
        self.assertEqual(profiles.model_choices(self.data, p),
                         ["claude-opus-5-5[1m]", "claude-opus-5-5", "claude-haiku-4-5", "gpt-x"])
        # No duplicates, whether the gateway lists the [1m] name too or a name twice.
        p["gateway"]["models_command"] = "printf '%s' '[\"claude-opus-5-5[1m]\", \"claude-opus-5-5\", \"gpt-x\", \"gpt-x\"]'"
        self.assertEqual(profiles.model_choices(self.data, p), ["claude-opus-5-5[1m]", "claude-opus-5-5", "gpt-x"])

    def test_only_entries_shaped_like_a_model_name_are_offered(self):
        p = self.gateway(small_model="claude-opus-5-5",
                         listed=r'["claude-opus-5-5", "Bearer sk-ant-api03-SECRETVALUE", "\u001b]0;pwned\u0007x"]')
        self.assertEqual(profiles.model_choices(self.data, p), ["claude-opus-5-5[1m]", "claude-opus-5-5"])
        # A bare token shaped like a model name matches: the shape drops error text and control characters only.
        for name in ("gpt-x", "claude-opus-5-5[1m]", "us.anthropic.claude-x-v1:0", "anthropic/claude-x", "a@b",
                     "claude_x", "Llama-3", "a" * 128, "sk-ant-x"):
            self.assertTrue(profiles.MODEL_NAME.fullmatch(name), name)
        for name in ("", "-x", "a b", "x\n", "a\x1bb", "x" * 129):
            self.assertIsNone(profiles.MODEL_NAME.fullmatch(name), name)

    def test_the_command_runs_on_every_call(self):
        p = self.gateway()
        before = runs(self.tmp)
        profiles.model_choices(self.data, p)
        self.assertEqual(runs(self.tmp) - before, 1)
        profiles.model_choices(self.data, p)
        self.assertEqual(runs(self.tmp) - before, 2)  # never cached

    def test_the_subscription_offers_the_priced_claude_models_then_default(self):
        from assistant import spend
        ms = profiles.model_choices(self.data, profiles.load(self.data, "subscription"))
        self.assertEqual(ms[-1], "default")
        self.assertEqual(len(ms), len(set(ms)))
        self.assertTrue(all(m == "default" or m.startswith("claude-") for m in ms))
        self.assertLess(ms.index("claude-opus-5-5[1m]"), ms.index("claude-opus-5-5"))
        self.assertIn("claude-haiku-4-5", ms)
        self.assertNotIn("claude-haiku-4-5[1m]", ms)
        priced = [m for m in spend.prices(self.data) if m.startswith("claude-")]
        self.assertEqual([m for m in ms if not m.endswith("[1m]") and m != "default"], priced)
        # The data folder's price override counts (spend.prices(data)).
        (self.data / "prices.json").write_text(json.dumps({"claude-test-9": {"input": 1, "output": 1}}))
        ms = profiles.model_choices(self.data, profiles.load(self.data, "subscription"))
        self.assertEqual(ms[-3:], ["claude-test-9[1m]", "claude-test-9", "default"])

    def test_a_gateway_with_no_models_command_offers_the_priced_list(self):
        p = profiles.save(self.data, gateway(small_model="claude-haiku-4-5", model=None))
        ms = profiles.model_choices(self.data, p)
        self.assertEqual(ms, profiles.model_choices(self.data, profiles.load(self.data, "subscription")))

    def test_a_failing_command_names_its_exit_code_only(self):
        p = self.gateway()
        for cmd, text in (("printf 'OUT-NOT-SHOWN'; exit 4", "failed (exit 4)"),
                          ("printf 'OUT-NOT-SHOWN'", "must print a JSON list of model names"),
                          ("printf '{\"OUT-NOT-SHOWN\": 1}'", "must print a JSON list of model names"),
                          ("printf '[\"OUT-NOT-SHOWN\", 1]'", "must print a JSON list of model names")):
            p["gateway"]["models_command"] = cmd
            with self.assertRaises(ModelsUnavailable, msg=cmd) as cm:
                profiles.model_choices(self.data, p)
            msg = str(cm.exception)
            self.assertEqual(msg, f"gateway.models_command: {text}")
            self.assertNotIn("OUT-NOT-SHOWN", msg)
            self.assertNotIn("printf", msg)

    def test_a_slow_command_stops_at_the_choice_timeout(self):
        p = self.gateway()
        p["gateway"]["models_command"] = "sleep 5; printf 'OUT-NOT-SHOWN'"
        with mock.patch.object(profiles, "CHOICE_TIMEOUT", 1):
            with self.assertRaises(ModelsUnavailable) as cm:
                profiles.model_choices(self.data, p)
        self.assertEqual(str(cm.exception), "gateway.models_command: failed (no answer within 1 s)")

    def test_choices(self):
        c = profiles.choices(self.data, self.gateway())
        self.assertEqual(c, {"profile": "g",
                             "models": ["claude-opus-5-5[1m]", "claude-opus-5-5", "claude-haiku-4-5",
                                        "claude-sonnet-5-5[1m]", "claude-sonnet-5-5", "gpt-x"],
                             "efforts": ["low", "medium", "high", "xhigh", "max"],
                             "recommended_model": "claude-opus-5-5[1m]", "recommended_effort": "high"})
        other = self.gateway("o", listed='["gpt-x", "gpt-y", "claude-haiku-4-5"]')
        self.assertEqual(profiles.choices(self.data, other)["recommended_model"], "gpt-x")  # the first entry
        self.assertIsNone(profiles.recommended_model([]))


# -- criterion 3: one fetch per choice ----------------------------------------------------------------------------------

class ChooseTest(Case):
    def test_save_with_known_checks_that_list_and_runs_no_command(self):
        p = gateway(small_model="claude-haiku-4-5", model="claude-opus-5-5[1m]")
        p["gateway"]["models_command"] = counting(self.tmp)
        before = runs(self.tmp)
        profiles.save(self.data, p, ["claude-opus-5-5", "claude-haiku-4-5"])
        self.assertEqual(runs(self.tmp), before)
        with self.assertRaisesRegex(ProfileError, "model: gpt-x is not offered by the gateway"):
            profiles.save(self.data, dict(p, name="h", model="gpt-x"), ["claude-opus-5-5", "claude-haiku-4-5"])
        self.assertEqual(runs(self.tmp), before)
        self.assertFalse(self.file("h").exists())

    def test_one_choose_fetches_once_and_saves(self):
        self.gateway()
        before = runs(self.tmp)
        with mock.patch.object(profiles, "now", return_value=WHEN):
            p = profiles.choose(self.data, "g", "claude-sonnet-5-5[1m]", "max")
        self.assertEqual(runs(self.tmp) - before, 1)
        self.assertEqual((p["model"], p["effort"], p["chosen_at"]), ("claude-sonnet-5-5[1m]", "max", WHEN))
        self.assertEqual(profiles.load(self.data, "g"), p)
        self.assertFalse(profiles.needs_choice(self.data, p))
        # With the list given, no command runs at all.
        before = runs(self.tmp)
        later = "2026-11-13T09:00:00+00:00"
        with mock.patch.object(profiles, "now", return_value=later):
            p = profiles.choose(self.data, "g", "gpt-x", "low", models=["gpt-x", "claude-haiku-4-5"])
        self.assertEqual(runs(self.tmp), before)
        self.assertEqual(p["chosen_at"], later)  # a second choice records its own time

    def test_a_refusal_writes_nothing(self):
        self.gateway()
        before = self.file().read_bytes()
        with self.assertRaises(ProfileError) as cm:
            profiles.choose(self.data, "g", "claude-opus-9", "high")
        self.assertIn("claude-opus-9", str(cm.exception))
        self.assertIn("choose one of: claude-opus-5-5[1m], claude-opus-5-5, claude-haiku-4-5", str(cm.exception))
        with self.assertRaises(ProfileError) as cm:
            profiles.choose(self.data, "g", "claude-opus-5-5", "huge")
        self.assertIn("'huge'", str(cm.exception))
        self.assertIn("low, medium, high, xhigh, max", str(cm.exception))
        self.assertEqual(self.file().read_bytes(), before)
        with self.assertRaises(ProfileError):
            profiles.choose(self.data, "nope", "claude-opus-5-5", "high")
        self.assertFalse(self.file("nope").exists())

    def test_default_is_stored_as_null(self):
        p = profiles.choose(self.data, "subscription", "default", "medium")
        self.assertEqual((p["model"], p["effort"]), (None, "medium"))
        self.assertIsNotNone(p["chosen_at"])
        self.assertTrue(self.file("subscription").exists())


# -- criterion 4: ap2 profile choices and choose -------------------------------------------------------------------------

class ChoicesCliTest(CliCase):
    def test_choices_prints_the_json(self):
        self.gateway()
        code, out, err = self.run_cli("choices", "g")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), profiles.choices(self.data, profiles.load(self.data, "g")))
        code, out, err = self.run_cli("choices", "subscription")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["models"][-1], "default")

    def test_a_fetch_failure_is_one_line(self):
        self.gateway()
        p = json.loads(self.file().read_text())
        p["gateway"]["models_command"] = "printf 'OUT-NOT-SHOWN'; exit 7"
        self.file().write_text(json.dumps(p))
        code, out, err = self.run_cli("choices", "g")
        self.assert_one_error(code, out, err, "ap2: gateway.models_command: failed (exit 7)")
        self.assertNotIn("OUT-NOT-SHOWN", err)
        self.assertNotIn("printf", err)
        self.assert_one_error(*self.run_cli("choices", "nope"), text="no profile named 'nope'")


    def test_a_listed_entry_not_shaped_like_a_model_name_is_never_shown(self):
        self.gateway(small_model="claude-opus-5-5",
                     listed=r'["claude-opus-5-5", "Bearer sk-ant-api03-SECRETVALUE", "\u001b]0;pwned\u0007x"]')
        code, out, err = self.run_cli("choices", "g")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["models"], ["claude-opus-5-5[1m]", "claude-opus-5-5"])
        code, out, err = self.run_cli("choose", "g", "--model", "nope", "--effort", "high")
        self.assert_one_error(code, out, err, "model 'nope' is not offered")
        self.assert_never_shown(out, err)
        # g is still unchosen and not active: with no terminal, `choose` and `use` print the shown list only.
        for argv in (("choose", "g"), ("use", "g")):
            code, out, err = self.run_cli(*argv)
            self.assertEqual(code, profiles.NEEDS_CHOICE, (argv, err))
            self.assertEqual(json.loads(out)["models"], ["claude-opus-5-5[1m]", "claude-opus-5-5"], argv)
            self.assert_never_shown(out, err)
        self.assertTrue(profiles.needs_choice(self.data, profiles.load(self.data, "g")))

    def assert_never_shown(self, *texts):
        for shown in texts:
            self.assertNotIn("SECRETVALUE", shown)
            self.assertNotIn("\x1b", shown)
            self.assertNotIn("pwned", shown)


class ChooseCliTest(CliCase):
    def test_both_flags_save_and_print_the_profile(self):
        self.gateway()
        before = runs(self.tmp)
        code, out, err = self.run_cli("choose", "g", "--model", "gpt-x", "--effort", "xhigh")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(runs(self.tmp) - before, 1)
        self.assertEqual(json.loads(out), profiles.load(self.data, "g"))
        self.assertEqual((json.loads(out)["model"], json.loads(out)["effort"]), ("gpt-x", "xhigh"))

    def test_a_value_not_offered_is_one_line_and_writes_nothing(self):
        self.gateway()
        before = self.file().read_bytes()
        for args, text in ((("--model", "claude-nope", "--effort", "high"), "claude-nope"),
                           (("--model", "gpt-x", "--effort", "huge"), "huge"),
                           (("--effort", "huge"), "huge"), (("--model", "claude-nope"), "claude-nope")):
            for tty in (False, True):
                with self.subTest(args=args, tty=tty):
                    self.assert_one_error(*self.run_cli("choose", "g", *args, tty=tty, answers=["1", "1"]),
                                          text=text)
        self.assertEqual(self.file().read_bytes(), before)

    def test_in_a_terminal_a_numbered_prompt_with_enter_for_the_recommended(self):
        self.gateway()
        before = runs(self.tmp)
        code, out, err = self.run_cli("choose", "g", tty=True, answers=["", "2"])
        self.assertEqual(code, 0, err)
        self.assertEqual(runs(self.tmp) - before, 1)  # the prompt and the save share one fetch
        self.assertIn("Model for g:\n  1) claude-opus-5-5[1m] (recommended)\n  2) claude-opus-5-5\n", err)
        self.assertIn("Effort for g:\n  1) low\n  2) medium\n  3) high (recommended)\n", err)
        self.assertIn("Number [1]: ", err)
        p = json.loads(out)  # stdout holds only the saved profile
        self.assertEqual((p["model"], p["effort"]), ("claude-opus-5-5[1m]", "medium"))
        self.assertEqual(profiles.load(self.data, "g"), p)

    def test_a_number_or_the_exact_name_is_taken_and_anything_else_asks_again(self):
        self.gateway()
        code, out, err = self.run_cli("choose", "g", tty=True, answers=["9", "0", "opus", "gpt-x", "x", "5"])
        self.assertEqual(code, 0, err)
        self.assertEqual(err.count("Type a number from 1 to"), 4)
        p = profiles.load(self.data, "g")
        self.assertEqual((p["model"], p["effort"]), ("gpt-x", "max"))
        # Only the missing value is asked for.
        code, out, err = self.run_cli("choose", "g", "--model", "claude-opus-5-5", tty=True, answers=["xhigh"])
        self.assertEqual(code, 0, err)
        self.assertNotIn("Model for g", err)
        p = profiles.load(self.data, "g")
        self.assertEqual((p["model"], p["effort"]), ("claude-opus-5-5", "xhigh"))

    def test_zero_is_not_on_the_list_and_asks_again(self):
        # "0" is outside 1..N: it must ask again, never wrap around to the last entry.
        self.gateway()
        code, out, err = self.run_cli("choose", "g", tty=True, answers=["0", "1", "1"])
        self.assertEqual(code, 0, err)
        self.assertEqual(err.count("Type a number from 1 to"), 1)
        p = profiles.load(self.data, "g")
        self.assertEqual((p["model"], p["effort"]), ("claude-opus-5-5[1m]", "low"))

    def test_a_digit_int_cannot_read_asks_again(self):
        self.gateway()
        code, out, err = self.run_cli("choose", "g", tty=True, answers=["\u00b2", "9" * 5000, "1", "1"])
        self.assertEqual(code, 0, err)
        self.assertEqual(err.count("Type a number from 1 to"), 2)
        p = profiles.load(self.data, "g")
        self.assertEqual((p["model"], p["effort"]), ("claude-opus-5-5[1m]", "low"))

    def test_ctrl_c_at_the_prompt_changes_nothing(self):
        self.gateway()
        before = self.file().read_bytes()
        for answers in ([CTRL_C], ["", CTRL_C]):
            with self.subTest(answers=answers):
                code, out, err = self.run_cli("choose", "g", tty=True, answers=answers)
                self.assertEqual((code, out), (1, ""))
                self.assertNotIn("Traceback", err)
                self.assertTrue(err.endswith("\nap2: no choice made; nothing changed\n"), err)
        self.assertEqual(self.file().read_bytes(), before)

    def test_end_of_input_changes_nothing(self):
        self.gateway()
        before = self.file().read_bytes()
        for answers in ([], [""]):
            code, out, err = self.run_cli("choose", "g", tty=True, answers=answers)
            self.assertEqual((code, out), (1, ""))
            self.assertTrue(err.endswith("ap2: no choice made; nothing changed\n"), err)
        self.assertEqual(self.file().read_bytes(), before)

    def test_a_command_that_lists_no_model_name_is_one_line_and_asks_nothing(self):
        # Without this refusal the prompt would show no options and ask again until the input ends. The profile is
        # saved against a list of its own (`known`), as the command lists no small model it could save with.
        for listed in ("[]", '["bad name"]'):
            with self.subTest(listed=listed):
                p = gateway("g", model=None, effort=None, small_model="claude-haiku-4-5")
                p["gateway"]["models_command"] = counting(self.tmp, listed)
                profiles.save(self.data, p, known=["claude-haiku-4-5"])
                before = self.file().read_bytes()
                code, out, err = self.run_cli("choose", "g", tty=True, answers=["1"])
                self.assert_one_error(code, out, err, "listed no models")
                self.assertNotIn("Model for", err)
                self.assertEqual(self.file().read_bytes(), before)

    def test_without_a_terminal_it_prints_the_choices_and_exits_3(self):
        self.gateway()
        before = self.file().read_bytes()
        for args in ((), ("--model", "gpt-x"), ("--effort", "low")):
            with self.subTest(args=args):
                code, out, err = self.run_cli("choose", "g", *args)
                self.assertEqual(code, profiles.NEEDS_CHOICE)
                self.assertEqual(json.loads(out)["profile"], "g")
                self.assertEqual(len(err.splitlines()), 1, err)
                self.assertIn("ap2 profile choose g --model M --effort E", err)
        self.assertEqual(self.file().read_bytes(), before)

    def test_a_closed_stdin_counts_as_no_terminal(self):
        self.gateway()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(sys, "stdin", None):  # `ap2 profile choose g <&-`
            code = cli.main(["profile", "choose", "g", "--data", str(self.data)])
        self.assertEqual(code, profiles.NEEDS_CHOICE, err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["profile"], "g")

    def test_bin_ap2_with_stdin_from_dev_null_exits_3(self):
        self.gateway()
        home = self.tmp / "home"
        home.mkdir()
        env = {k: v for k, v in os.environ.items() if k != "AP2_DATA" and not k.startswith("AUTOPILOT_")}
        env.update(HOME=str(home), AUTOPILOT_DATA=str(self.tmp / "v1-data"), PYTHONIOENCODING="utf-8")
        with open(os.devnull) as devnull:
            r = subprocess.run([sys.executable, str(AP2), "profile", "choose", "g", "--data", str(self.data)],
                               stdin=devnull, capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 3, r.stderr)
        c = json.loads(r.stdout)
        self.assertEqual((c["profile"], c["recommended_model"], c["recommended_effort"]),
                         ("g", "claude-opus-5-5[1m]", "high"))
        self.assertEqual(r.stderr.count("\n"), 1, r.stderr)
        self.assertIn("ap2 profile choose g --model M --effort E", r.stderr)
        self.assertTrue(profiles.needs_choice(self.data, profiles.load(self.data, "g")))


# -- criterion 5: add and use ask once ----------------------------------------------------------------------------------

class AddCliTest(CliCase):
    def test_both_flags_check_the_model_against_the_list_and_ask_nothing(self):
        before = runs(self.tmp)
        code, out, err = self.add_gateway("--model", "claude-sonnet-5-5[1m]", "--effort", "high", tty=True)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(runs(self.tmp) - before, 1)
        p = json.loads(out)
        self.assertEqual((p["model"], p["effort"]), ("claude-sonnet-5-5[1m]", "high"))
        self.assertFalse(profiles.needs_choice(self.data, p))
        # A haiku has no [1m] variant in the list: refused, nothing saved.
        self.file().unlink()
        self.assert_one_error(*self.add_gateway("--model", "claude-nope-1", "--effort", "high"), text="claude-nope-1")
        self.assert_one_error(*self.add_gateway("--model", "gpt-x", "--effort", "turbo"), text="turbo")
        self.assertFalse(self.file().exists())

    def test_a_model_flag_alone_is_checked_before_anything_is_saved(self):
        before = runs(self.tmp)
        self.assert_one_error(*self.add_gateway("--model", "claude-nope-1"),
                              text="model 'claude-nope-1' is not offered")
        self.assertEqual(runs(self.tmp) - before, 1)
        self.assertFalse(self.file().exists())
        self.assert_one_error(*self.add_gateway("--model", "claude-nope-1", tty=True, answers=["1", "1"]),
                              text="claude-nope-1")
        self.assertFalse(self.file().exists())
        self.assert_one_error(*self.run_cli("add", "s", "--subscription", "--model", "bogus"),
                              text="model 'bogus' is not offered")
        self.assertFalse(self.file("s").exists())
        # An effort flag alone is checked before anything is saved too.
        self.assert_one_error(*self.add_gateway("--effort", "turbo"), text="effort 'turbo' is not offered")
        self.assertFalse(self.file().exists())

    def test_both_flags_on_a_subscription_profile(self):
        code, out, err = self.run_cli("add", "s", "--subscription", "--model", "default", "--effort", "low")
        self.assertEqual((code, err), (0, ""))
        p = json.loads(out)
        self.assertEqual((p["model"], p["effort"]), (None, "low"))
        self.assertFalse(profiles.needs_choice(self.data, p))
        self.assert_one_error(*self.run_cli("add", "t", "--subscription", "--model", "gpt-x", "--effort", "low"),
                              text="model 'gpt-x' is not offered")
        self.assertFalse(self.file("t").exists())

    def test_without_a_terminal_it_is_saved_unchosen_and_exits_3(self):
        before = runs(self.tmp)
        code, out, err = self.add_gateway("--effort", "low")
        self.assertEqual(code, profiles.NEEDS_CHOICE)
        self.assertEqual(runs(self.tmp) - before, 1)
        self.assertEqual(json.loads(out)["recommended_model"], "claude-opus-5-5[1m]")
        self.assertEqual(len(err.splitlines()), 1, err)
        self.assertIn("g is saved", err)
        self.assertIn("ap2 profile choose g --model M --effort E", err)
        p = profiles.load(self.data, "g")
        self.assertTrue(profiles.needs_choice(self.data, p))
        self.assertEqual(p["effort"], "low")

    def test_a_subscription_mode_profile_needs_a_choice_too(self):
        code, out, err = self.run_cli("add", "s", "--subscription")
        self.assertEqual(code, profiles.NEEDS_CHOICE, err)
        self.assertTrue(profiles.needs_choice(self.data, profiles.load(self.data, "s")))

    def test_in_a_terminal_a_model_flag_alone_asks_only_for_the_effort(self):
        self.assertEqual(profiles.RECOMMENDED_EFFORT, "high")
        code, out, err = self.add_gateway("--model", "gpt-x", tty=True, answers=[""])
        self.assertEqual(code, 0, err)
        self.assertNotIn("Model for g", err)
        p = profiles.load(self.data, "g")
        self.assertEqual((p["model"], p["effort"]), ("gpt-x", "high"))

    def test_in_a_terminal_it_asks_at_the_end(self):
        before = runs(self.tmp)
        code, out, err = self.add_gateway("--effort", "low", tty=True, answers=["6"])
        self.assertEqual(code, 0, err)
        self.assertEqual(runs(self.tmp) - before, 1)
        self.assertNotIn("Effort for g", err)
        p = json.loads(out)
        self.assertEqual((p["model"], p["effort"]), ("gpt-x", "low"))
        self.assertFalse(profiles.needs_choice(self.data, profiles.load(self.data, "g")))

    def test_in_a_terminal_leaving_the_prompt_says_it_is_saved(self):
        code, out, err = self.add_gateway(tty=True, answers=[])
        self.assertEqual((code, out), (1, ""))
        last = err.splitlines()[-1]
        self.assertTrue(last.startswith("ap2: no choice made; g is saved"), last)
        self.assertIn("`ap2 profile choose g` finishes it", last)
        self.assertTrue(profiles.needs_choice(self.data, profiles.load(self.data, "g")))

    def test_in_a_terminal_ctrl_c_at_the_prompt_says_it_is_saved(self):
        code, out, err = self.add_gateway(tty=True, answers=[CTRL_C])
        self.assertEqual((code, out), (1, ""))
        self.assertNotIn("Traceback", err)
        last = err.splitlines()[-1]
        self.assertTrue(last.startswith("ap2: no choice made; g is saved"), last)
        self.assertIn("`ap2 profile choose g` finishes it", last)
        self.assertTrue(err.endswith(last + "\n") and err.count("ap2: ") == 1, err)
        self.assertTrue(profiles.needs_choice(self.data, profiles.load(self.data, "g")))

    def test_in_a_terminal_a_list_with_no_model_name_says_it_is_saved(self):
        # The command lists only a small model of another shape: the save takes it, but nothing can be shown.
        small = "anthropic/claude-haiku+latest"
        before = runs(self.tmp)
        code, out, err = self.run_cli("add", "h", "--gateway", "--base-url", URL, "--key-command", "echo k",
                                      "--small-model", small, "--daily-cap", "5",
                                      "--models-command", counting(self.tmp, f'["{small}"]'),
                                      tty=True, answers=["1"])
        self.assertEqual((code, out), (1, ""), err)
        self.assertEqual(runs(self.tmp) - before, 1)
        self.assertNotIn("Model for", err)
        last = err.splitlines()[-1]
        self.assertIn("listed no models", last)
        self.assertIn("`ap2 profile choose h` finishes it", last)
        self.assertTrue(profiles.needs_choice(self.data, profiles.load(self.data, "h")))

    def test_a_listed_small_model_of_another_shape_still_saves_and_is_never_shown(self):
        listed = '["claude-opus-5-5", "anthropic/claude-haiku+latest"]'
        small = "anthropic/claude-haiku+latest"

        def add(name, *extra, **kw):
            return self.run_cli("add", name, "--gateway", "--base-url", URL, "--key-command", "echo k",
                                "--small-model", small, "--daily-cap", "5",
                                "--models-command", counting(self.tmp, listed), *extra, **kw)

        def once(call):
            before = runs(self.tmp)
            got = call()
            self.assertEqual(runs(self.tmp) - before, 1)
            return got

        code, out, err = once(lambda: add("g", "--model", "claude-opus-5-5", "--effort", "high"))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual((json.loads(out)["small_model"], json.loads(out)["model"]), (small, "claude-opus-5-5"))
        code, out, err = once(lambda: add("h"))
        self.assertEqual(code, profiles.NEEDS_CHOICE, err)
        self.assertTrue(self.file("h").exists())
        self.assertEqual(json.loads(out)["models"], ["claude-opus-5-5[1m]", "claude-opus-5-5"])  # never shown
        self.assertNotIn(small, out)
        code, out, err = once(lambda: self.run_cli("choose", "h", "--model", "claude-opus-5-5", "--effort", "high"))
        self.assertEqual((code, err), (0, ""))
        self.assertFalse(profiles.needs_choice(self.data, profiles.load(self.data, "h")))
        # The prompt paths save against the full list too: `choose` and `use` in a terminal.
        self.assertEqual(once(lambda: add("k"))[0], profiles.NEEDS_CHOICE)
        code, out, err = once(lambda: self.run_cli("choose", "k", tty=True, answers=["", ""]))
        self.assertEqual(code, 0, err)
        self.assertEqual(once(lambda: add("u"))[0], profiles.NEEDS_CHOICE)
        code, out, err = once(lambda: self.run_cli("use", "u", tty=True, answers=["", ""]))
        self.assertEqual(code, 0, err)
        self.assertEqual(profiles.active(self.data)["name"], "u")
        # A model of another shape cannot be picked as the profile's model, though a save accepts it as small_model.
        self.assert_one_error(*self.run_cli("choose", "h", "--model", small, "--effort", "high"),
                              text="is not offered")
        self.assert_one_error(*add("x", "--model", small, "--effort", "high"), text="is not offered")
        self.assertFalse(self.file("x").exists())

    def test_a_save_accepts_the_plain_name_of_a_listed_1m_entry(self):
        # The command lists only the [1m] name; the shown list offers the plain name too, so a save takes it.
        listed = '["claude-opus-5-5[1m]", "claude-haiku-4-5"]'

        def add(name, *extra):
            return self.run_cli("add", name, "--gateway", "--base-url", URL, "--key-command", "echo k",
                                "--small-model", "claude-haiku-4-5", "--daily-cap", "5",
                                "--models-command", counting(self.tmp, listed), *extra)

        code, out, err = add("g", "--model", "claude-opus-5-5", "--effort", "high")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["model"], "claude-opus-5-5")
        self.assertEqual(add("h")[0], profiles.NEEDS_CHOICE)
        code, out, err = self.run_cli("choose", "h", "--model", "claude-opus-5-5", "--effort", "high")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(profiles.load(self.data, "h")["model"], "claude-opus-5-5")

    def test_a_broken_profile_is_refused_before_its_models_command_runs(self):
        code, out, err = self.run_cli("add", "g", "--gateway", "--base-url", URL, "--models-command",
                                      counting(self.tmp), "--model", "gpt-x", "--effort", "high")
        self.assert_one_error(code, out, err, "key_command")
        self.assertEqual(runs(self.tmp), 0)


class UseCliTest(CliCase):
    def setUp(self):
        super().setUp()
        profiles.use(self.data, "subscription")  # a file there, so "untouched" can be checked byte for byte
        self.active = self.data / "active-profile.json"

    def test_without_a_terminal_nothing_switches_and_it_exits_3(self):
        self.gateway()
        before = self.active.read_bytes()
        code, out, err = self.run_cli("use", "g")
        self.assertEqual(code, profiles.NEEDS_CHOICE)
        self.assertEqual(json.loads(out)["profile"], "g")
        self.assertEqual(len(err.splitlines()), 1, err)
        self.assertIn("nothing switched", err)
        self.assertIn("ap2 profile choose g --model M --effort E", err)
        self.assertEqual(self.active.read_bytes(), before)

    def test_in_a_terminal_it_asks_saves_then_switches(self):
        self.gateway()
        code, out, err = self.run_cli("use", "g", tty=True, answers=["", ""])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), {"active": "g", "was": "subscription"})
        p = profiles.active(self.data)
        self.assertEqual((p["name"], p["model"], p["effort"]), ("g", "claude-opus-5-5[1m]", "high"))
        self.assertFalse(profiles.needs_choice(self.data, p))

    def test_in_a_terminal_leaving_the_prompt_switches_nothing(self):
        self.gateway()
        before = self.active.read_bytes(), self.file().read_bytes()
        code, out, err = self.run_cli("use", "g", tty=True, answers=[])
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.endswith("ap2: no choice made; nothing changed\n"), err)
        self.assertEqual((self.active.read_bytes(), self.file().read_bytes()), before)

    def test_in_a_terminal_ctrl_c_at_the_prompt_switches_nothing(self):
        self.gateway()
        before = self.active.read_bytes(), self.file().read_bytes()
        code, out, err = self.run_cli("use", "g", tty=True, answers=[CTRL_C])
        self.assertEqual((code, out), (1, ""))
        self.assertNotIn("Traceback", err)
        self.assertTrue(err.endswith("\nap2: no choice made; nothing changed\n"), err)
        self.assertEqual((self.active.read_bytes(), self.file().read_bytes()), before)

    def test_a_broken_active_profile_file_still_asks_before_switching_to_an_unchosen_profile(self):
        self.gateway()
        self.active.write_text("{nope")
        code, out, err = self.run_cli("use", "g")
        self.assertEqual(code, profiles.NEEDS_CHOICE, err)
        self.assertEqual(json.loads(out)["profile"], "g")
        self.assertIn("nothing switched", err)
        self.assertEqual(self.active.read_bytes(), b"{nope")
        code, out, err = self.run_cli("use", "g", tty=True, answers=["", ""])
        self.assertEqual(code, 0, err)
        self.assertIn("Model for g:", err)
        p = profiles.active(self.data)
        self.assertEqual(p["name"], "g")
        self.assertFalse(profiles.needs_choice(self.data, p))

    def test_a_chosen_profile_the_active_one_and_the_subscription_ask_nothing(self):
        self.gateway("c", model="gpt-x", effort="high", chosen_at=WHEN)
        self.gateway()
        before = runs(self.tmp)
        for tty in (False, True):  # a prompt would read the end of input and fail
            code, out, err = self.run_cli("use", "c", tty=tty)
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(json.loads(out)["active"], "c")
            code, out, err = self.run_cli("use", "subscription", tty=tty)
            self.assertEqual((code, err), (0, ""))
        self.assertEqual(runs(self.tmp), before)  # nothing was fetched
        # The unchosen g made active by hand: switching to it again asks nothing.
        profiles.use(self.data, "g")
        for tty in (False, True):
            code, out, err = self.run_cli("use", "g", tty=tty)
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(json.loads(out), {"active": "g", "was": "g"})
        self.assertEqual(runs(self.tmp), before)


# -- criterion 6: workers never prompt ----------------------------------------------------------------------------------

class WorkerNeverAsksTest(WorkerCase):
    def test_a_worker_starts_on_an_unchosen_active_profile_as_stored(self):
        p = profiles.save(self.data, gateway(model="m1", effort="low"))
        self.assertTrue(profiles.needs_choice(self.data, p))
        profiles.use(self.data, "g")  # set by hand, never chosen
        with mock.patch("builtins.input", side_effect=AssertionError("a worker start asked")), \
                mock.patch.object(profiles, "fetch_models", side_effect=AssertionError("a worker start fetched")), \
                mock.patch.object(profiles, "model_choices", side_effect=AssertionError("a worker start fetched")):
            self.start()
        argv = self.bg_calls()[-1]["argv"]
        self.assertEqual((opt(argv, "--model"), opt(argv, "--effort")), ("m1", "low"))


if __name__ == "__main__":
    unittest.main()
