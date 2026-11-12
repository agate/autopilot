"""Choosing a profile's model and effort from a list fetched fresh, the first time it is set up or used."""
from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import TempData
from autopilot import cli, config, profile

URL = "https://llm-gateway.example.com/anthropic"
LISTED = '["claude-opus-5-5","claude-haiku-4-5","claude-sonnet-5-5","gpt-5"]'


def run(*argv, tty=False, answers=None):
    """Run `ap ...` with stdin a terminal or not; `answers` are what the user types at each prompt."""
    out, err = io.StringIO(), io.StringIO()
    typed = iter(answers or [])

    def fake_input(prompt=""):
        out.write(prompt)
        try:
            return next(typed)
        except StopIteration:
            raise EOFError from None

    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            mock.patch("sys.stdin.isatty", return_value=tty), mock.patch("builtins.input", fake_input):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def models_command(d: Path, listed: str = LISTED) -> str:
    """A tiny shell stand-in for a gateway's models command: it counts its runs and prints `listed`."""
    return f"echo run >> {d}/models-runs; printf '%s' '{listed}'"


def runs(d: Path) -> int:
    f = d / "models-runs"
    return len(f.read_text().splitlines()) if f.exists() else 0


def gateway(d: Path, name="g", listed=LISTED, **kw) -> dict:
    p = {"name": name, "mode": "gateway", "label": "Team gateway", "model": None, "small_model": "claude-haiku-4-5",
         "daily_cap_usd": 50, "gateway": {"base_url": URL, "key_command": "echo k",
                                          "models_command": models_command(d, listed)}}
    p.update(kw)
    profile.save(p)
    return profile.load(name)


def add_gateway(d: Path, *extra, tty=False, answers=None):
    return run("profile", "add", "g", "--gateway", "--base-url", URL, "--key-command", "echo k",
               "--small-model", "claude-haiku-4-5", "--daily-cap", "50", "--models-command", models_command(d),
               *extra, tty=tty, answers=answers)


class LongContextNameTest(unittest.TestCase):
    def test_a_1m_name_is_checked_without_its_suffix(self):
        with TempData() as d:
            p = gateway(d, model="claude-opus-5-5[1m]", dispatcher_model="claude-sonnet-5-5[1m]")
            self.assertEqual(p["model"], "claude-opus-5-5[1m]")
            with self.assertRaises(ValueError) as cm:
                gateway(d, name="h", small_model="claude-nope-1[1m]")
            self.assertIn("claude-nope-1[1m]", str(cm.exception))


class ModelChoicesTest(unittest.TestCase):
    def test_gateway_list_offers_the_1m_variant_first_and_is_fetched_every_time(self):
        with TempData() as d:
            p = gateway(d)
            before = runs(d)
            self.assertEqual(profile.model_choices(p), [
                "claude-opus-5-5[1m]", "claude-opus-5-5", "claude-haiku-4-5",
                "claude-sonnet-5-5[1m]", "claude-sonnet-5-5", "gpt-5"])
            profile.model_choices(p)
            self.assertEqual(runs(d) - before, 2)   # never cached

    def test_a_failing_or_wrong_models_command_raises(self):
        with TempData() as d:
            p = gateway(d)
            for cmd in ("exit 4", "printf 'not json'", "printf '{}'"):
                p["gateway"]["models_command"] = cmd
                with self.assertRaises(profile.ModelsUnavailable, msg=cmd):
                    profile.model_choices(p)

    def test_subscription_offers_the_priced_claude_models_and_default(self):
        with TempData():
            ms = profile.model_choices(profile.load("subscription"))
            self.assertIn("claude-opus-5-5[1m]", ms)
            self.assertLess(ms.index("claude-opus-5-5[1m]"), ms.index("claude-opus-5-5"))
            self.assertIn("claude-haiku-4-5", ms)
            self.assertNotIn("claude-haiku-4-5[1m]", ms)
            self.assertIn("default", ms)
            self.assertTrue(all(m == "default" or m.startswith("claude-") for m in ms))

    def test_recommended_values(self):
        self.assertEqual(profile.EFFORTS, ("low", "medium", "high", "xhigh", "max"))
        self.assertEqual(profile.RECOMMENDED_EFFORT, "high")
        self.assertEqual(profile.recommended_model(["a", "claude-opus-5-5[1m]"]), "claude-opus-5-5[1m]")
        self.assertEqual(profile.recommended_model(["gpt-5", "claude-x"]), "gpt-5")
        with TempData() as d:
            c = profile.choices(gateway(d))
            self.assertEqual(set(c), {"profile", "models", "efforts", "recommended_model", "recommended_effort"})
            self.assertEqual((c["profile"], c["recommended_model"], c["recommended_effort"]),
                             ("g", "claude-opus-5-5[1m]", "high"))
            self.assertEqual(c["efforts"], list(profile.EFFORTS))


class NeedsChoiceTest(unittest.TestCase):
    def test_a_new_profile_needs_a_choice_until_one_is_saved(self):
        with TempData() as d:
            self.assertTrue(profile.needs_choice(gateway(d)))
            self.assertFalse(profile.needs_choice({"chosen_at": "2026-11-11T10:00:00+00:00"}))

    def test_migration_marks_the_subscription_chosen(self):
        with TempData():
            self.assertTrue(profile.migrate_from_config())
            self.assertFalse(profile.needs_choice(profile.load("subscription")))

    def test_an_install_migrated_before_gets_its_subscription_marked(self):
        with TempData() as d:
            (d / "profiles").mkdir()
            (d / "profiles" / "subscription.json").write_text(json.dumps(
                {"name": "subscription", "mode": "subscription", "label": "subscription", "model": "opus"}))
            self.assertFalse(profile.migrate_from_config())
            p = profile.load("subscription")
            self.assertFalse(profile.needs_choice(p))
            self.assertEqual(p["model"], "opus")

    def test_subscription_only_users_are_never_prompted(self):
        with TempData():
            for tty in (True, False):   # a prompt would read the end of input and fail
                code, out, err = run("profile", "use", "subscription", tty=tty)
                self.assertEqual((code, out), (0, ""), err)


class ChooseTest(unittest.TestCase):
    def test_choose_validates_against_a_fresh_list_and_saves(self):
        with TempData() as d:
            gateway(d)
            with self.assertRaises(ValueError):
                profile.choose("g", "claude-opus-9", "high")
            with self.assertRaises(ValueError):
                profile.choose("g", "claude-opus-5-5[1m]", "turbo")
            self.assertTrue(profile.needs_choice(profile.load("g")))
            before = runs(d)
            profile.choose("g", "claude-opus-5-5[1m]", "high")
            self.assertGreaterEqual(runs(d) - before, 1)
            p = profile.load("g")
            self.assertEqual((p["model"], p["effort"]), ("claude-opus-5-5[1m]", "high"))
            self.assertFalse(profile.needs_choice(p))

    def test_choose_on_the_active_profile_rewrites_its_settings(self):
        with TempData() as d:
            gateway(d, model="claude-opus-5-5", effort="low", chosen_at="2026-11-01T00:00:00+00:00")
            f = d / "projects" / "a" / "settings.json"
            f.parent.mkdir(parents=True)
            f.write_text(json.dumps({"permissions": {"deny": ["x"]}, "env": {}}))
            profile.use("g")
            profile.choose("g", "claude-sonnet-5-5[1m]", "max")
            body = json.loads(f.read_text())
            self.assertEqual(body["env"]["ANTHROPIC_MODEL"], "claude-sonnet-5-5[1m]")
            self.assertEqual(body["permissions"], {"deny": ["x"]})

    def test_default_on_the_subscription_is_stored_as_null(self):
        with TempData():
            profile.migrate_from_config()
            profile.choose("subscription", "default", "medium")
            p = profile.load("subscription")
            self.assertEqual((p["model"], p["effort"]), (None, "medium"))


class ReviewFixTest(unittest.TestCase):
    def test_the_builtin_subscription_without_a_file_needs_no_choice(self):
        with TempData():
            self.assertFalse(profile.needs_choice(profile.load("subscription")))

    def test_re_applying_the_active_profile_never_asks(self):
        with TempData() as d:
            gateway(d)
            profile.use("g")   # e.g. made active before it had a choice
            code, out, err = run("profile", "use", "g")
            self.assertEqual((code, out), (0, ""), err)

    def test_add_with_both_flags_checks_the_model_against_a_fresh_list(self):
        with TempData() as d:
            code, _, err = add_gateway(d, "--model", "claude-haiku-4-5[1m]", "--effort", "high")
            self.assertEqual(code, 1)
            self.assertIn("claude-haiku-4-5[1m]", err)
            self.assertEqual(profile.names(), ["subscription"])
            code, _, err = run("profile", "add", "s", "--subscription", "--model", "default", "--effort", "low")
            self.assertEqual(code, 0, err)
            p = profile.load("s")
            self.assertEqual((p["model"], p["effort"]), (None, "low"))
            self.assertFalse(profile.needs_choice(p))

    def test_one_choice_runs_the_models_command_once(self):
        with TempData() as d:
            gateway(d)
            before = runs(d)
            self.assertEqual(run("profile", "choose", "g", "--model", "gpt-5", "--effort", "high")[0], 0)
            self.assertEqual(runs(d) - before, 1)
            before = runs(d)
            self.assertEqual(run("profile", "choose", "g", tty=True, answers=["", ""])[0], 0)
            self.assertEqual(runs(d) - before, 1)

    def test_add_with_both_flags_runs_the_models_command_once(self):
        with TempData() as d:
            before = runs(d)
            self.assertEqual(add_gateway(d, "--model", "gpt-5", "--effort", "high")[0], 0)
            self.assertEqual(runs(d) - before, 1)

    def test_a_cli_switch_closes_an_abandoned_choice_card(self):
        with TempData() as d:
            gateway(d)
            profile.open_choice("g")
            self.assertEqual(run("profile", "use", "subscription")[0], 0)
            self.assertIsNone(profile.choice_card())

    def test_a_choice_card_expires_six_hours_after_it_opened(self):
        from datetime import datetime, timedelta
        with TempData() as d:
            gateway(d)
            profile.open_choice("g")
            f = d / "profile-choice.json"
            c = json.loads(f.read_text())
            c["at"] = (datetime.now().astimezone() - timedelta(hours=5, minutes=50)).isoformat()
            f.write_text(json.dumps(c))
            self.assertEqual(profile.choice_card()["profile"], "g")
            c["at"] = (datetime.now().astimezone() - timedelta(hours=6, minutes=1)).isoformat()
            f.write_text(json.dumps(c))
            self.assertIsNone(profile.choice_card())
            self.assertFalse(f.exists())

    def test_choose_leaves_the_card_to_its_caller(self):
        with TempData() as d:
            gateway(d)
            profile.open_choice("g")
            profile.choose("g", "gpt-5", "high")
            self.assertEqual(profile.choice_card()["profile"], "g")   # the page path closes it after the switch
            profile.open_choice("g")
            self.assertEqual(run("profile", "choose", "g", "--model", "gpt-5", "--effort", "low")[0], 0)
            self.assertIsNone(profile.choice_card())


class ChoicesCliTest(unittest.TestCase):
    def test_choices_json(self):
        with TempData() as d:
            gateway(d)
            code, out, err = run("profile", "choices", "g", "--json")
            self.assertEqual(code, 0, err)
            c = json.loads(out)
            self.assertEqual(c["recommended_model"], "claude-opus-5-5[1m]")
            self.assertIn("gpt-5", c["models"])

    def test_choices_text_marks_the_recommended_entries(self):
        with TempData() as d:
            gateway(d)
            code, out, _ = run("profile", "choices", "g")
            self.assertEqual(code, 0)
            self.assertIn("claude-opus-5-5[1m] (recommended)", out)
            self.assertIn("high (recommended)", out)

    def test_choices_says_when_the_list_cannot_be_fetched(self):
        with TempData() as d:
            gateway(d)
            p = json.loads((d / "profiles" / "g.json").read_text())
            p["gateway"]["models_command"] = "exit 7"
            (d / "profiles" / "g.json").write_text(json.dumps(p))
            code, _, err = run("profile", "choices", "g", "--json")
            self.assertEqual(code, 1)
            self.assertIn("exit 7", err)


class ChooseCliTest(unittest.TestCase):
    def test_both_flags(self):
        with TempData() as d:
            gateway(d)
            code, _, err = run("profile", "choose", "g", "--model", "gpt-5", "--effort", "xhigh")
            self.assertEqual(code, 0, err)
            self.assertEqual((profile.load("g")["model"], profile.load("g")["effort"]), ("gpt-5", "xhigh"))

    def test_a_bad_model_is_refused(self):
        with TempData() as d:
            gateway(d)
            code, _, err = run("profile", "choose", "g", "--model", "claude-nope", "--effort", "high")
            self.assertEqual(code, 1)
            self.assertIn("claude-nope", err)
            self.assertTrue(profile.needs_choice(profile.load("g")))

    def test_without_a_terminal_a_missing_flag_prints_the_choices_and_exits_3(self):
        with TempData() as d:
            gateway(d)
            code, out, err = run("profile", "choose", "g", "--model", "gpt-5")
            self.assertEqual(code, 3)
            self.assertEqual(json.loads(out)["profile"], "g")
            self.assertIn("--effort", err)
            self.assertTrue(profile.needs_choice(profile.load("g")))

    def test_in_a_terminal_a_missing_flag_is_a_numbered_prompt_with_enter_for_the_recommended(self):
        with TempData() as d:
            gateway(d)
            code, out, err = run("profile", "choose", "g", tty=True, answers=["", "2"])
            self.assertEqual(code, 0, err)
            self.assertIn("1) claude-opus-5-5[1m] (recommended)", out)
            p = profile.load("g")
            self.assertEqual((p["model"], p["effort"]), ("claude-opus-5-5[1m]", "medium"))

    def test_a_wrong_answer_asks_again(self):
        with TempData() as d:
            gateway(d)
            code, _, err = run("profile", "choose", "g", "--model", "gpt-5", tty=True, answers=["9", "x", "5"])
            self.assertEqual(code, 0, err)
            self.assertEqual(profile.load("g")["effort"], "max")

    def test_end_of_input_stops_without_saving(self):
        with TempData() as d:
            gateway(d)
            code, _, _ = run("profile", "choose", "g", tty=True, answers=[])
            self.assertEqual(code, 1)
            self.assertTrue(profile.needs_choice(profile.load("g")))


class AddCliTest(unittest.TestCase):
    def test_add_with_model_and_effort_is_chosen_at_creation(self):
        with TempData() as d:
            code, _, err = add_gateway(d, "--model", "claude-opus-5-5[1m]", "--effort", "high", tty=True)
            self.assertEqual(code, 0, err)
            self.assertFalse(profile.needs_choice(profile.load("g")))

    def test_add_without_a_terminal_saves_it_unchosen_and_prints_the_choices(self):
        with TempData() as d:
            code, out, err = add_gateway(d)
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["recommended_model"], "claude-opus-5-5[1m]")
            self.assertIn("ap profile choose g", err)
            self.assertTrue(profile.needs_choice(profile.load("g")))

    def test_add_in_a_terminal_prompts_at_the_end(self):
        with TempData() as d:
            code, _, err = add_gateway(d, "--effort", "low", tty=True, answers=["6"])
            self.assertEqual(code, 0, err)
            p = profile.load("g")
            self.assertEqual((p["model"], p["effort"]), ("gpt-5", "low"))
            self.assertFalse(profile.needs_choice(p))


    def test_add_refuses_an_effort_not_in_the_list(self):
        with TempData() as d:
            code, _, err = add_gateway(d, "--model", "gpt-5", "--effort", "turbo")
            self.assertEqual(code, 1)
            self.assertIn("turbo", err)
            self.assertEqual(profile.names(), ["subscription"])


    def test_add_in_a_terminal_left_without_a_choice_says_it_is_saved(self):
        with TempData() as d:
            code, _, err = add_gateway(d, tty=True, answers=[])
            self.assertEqual(code, 1)
            self.assertIn("ap profile choose g", err)
            self.assertTrue(profile.needs_choice(profile.load("g")))


class UseCliTest(unittest.TestCase):
    def test_use_without_a_terminal_refuses_until_chosen(self):
        with TempData() as d:
            gateway(d)
            code, out, err = run("profile", "use", "g")
            self.assertEqual(code, 3)
            self.assertEqual(json.loads(out)["profile"], "g")
            self.assertIn("ap profile choose g", err)
            self.assertEqual(config.load_global()["active_profile"], "subscription")

    def test_use_in_a_terminal_prompts_then_switches(self):
        with TempData() as d:
            gateway(d)
            code, _, err = run("profile", "use", "g", tty=True, answers=["", ""])
            self.assertEqual(code, 0, err)
            p = profile.active()
            self.assertEqual((p["name"], p["model"], p["effort"]), ("g", "claude-opus-5-5[1m]", "high"))

    def test_use_in_a_terminal_switches_nothing_when_the_prompt_is_left(self):
        with TempData() as d:
            gateway(d)
            self.assertEqual(run("profile", "use", "g", tty=True, answers=[])[0], 1)
            self.assertEqual(config.load_global()["active_profile"], "subscription")

    def test_a_chosen_profile_switches_without_a_prompt(self):
        with TempData() as d:
            gateway(d, model="gpt-5", effort="high", chosen_at="2026-11-01T00:00:00+00:00")
            self.assertEqual(run("profile", "use", "g")[0], 0)
            self.assertEqual(profile.active()["name"], "g")


if __name__ == "__main__":
    unittest.main()
