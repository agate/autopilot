from __future__ import annotations

import contextlib
import io
import json
import unittest

from tests.helpers import TempData
from autopilot import cli, config, events, profile

URL = "https://llm-gateway.example.com/anthropic"


def run(*argv):
    from unittest import mock
    out, err = io.StringIO(), io.StringIO()
    # No terminal: a profile without a chosen model and effort never waits on a prompt here.
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            mock.patch("sys.stdin.isatty", return_value=False):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class ProfileTest(unittest.TestCase):
    def test_builtin_subscription_exists(self):
        with TempData():
            self.assertEqual(profile.active()["name"], "subscription")
            self.assertEqual(profile.active()["mode"], "subscription")
            self.assertEqual(profile.names(), ["subscription"])

    def test_gateway_requires_fields(self):
        errs = " ".join(profile.validate({"name": "g", "mode": "gateway"}))
        for field in ("base_url", "key_command", "small_model", "daily_cap_usd"):
            self.assertIn(field, errs)

    def test_rejects_permissions_in_settings(self):
        for bad in ({"permissions": {}}, {"hooks": {}}):
            errs = profile.validate({"name": "s", "mode": "subscription", "settings": bad})
            self.assertTrue(errs, bad)

    def test_rejects_credential_env(self):
        for name in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "my_secret", "DB_Password", "X_TOKEN"):
            errs = profile.validate({"name": "s", "mode": "subscription", "env": {name: "v"}})
            self.assertTrue(errs, name)
        self.assertEqual(profile.validate({"name": "s", "mode": "subscription", "env": {"A": "b"}}), [])

    def test_rejects_credential_in_settings_env(self):
        for settings in ({"env": {"ANTHROPIC_AUTH_TOKEN": "x"}}, {"env": {"api_key": "x"}}, {"apiKeyHelper": "echo k"}):
            errs = profile.validate({"name": "s", "mode": "subscription", "settings": settings})
            self.assertTrue(errs, settings)

    def test_fast_command_skips_migration(self):
        from unittest import mock
        with TempData(), mock.patch.object(profile, "migrate_from_config") as m, \
                mock.patch("sys.stdin", io.StringIO("{}")), contextlib.redirect_stdout(io.StringIO()):
            cli.main(["hook-park"])
            m.assert_not_called()

    def test_migration_failure_only_warns(self):
        from unittest import mock
        with TempData(), mock.patch.object(profile, "migrate_from_config", side_effect=OSError("disk")):
            code, _, err = run("profile", "list")
            self.assertEqual(code, 0)
            self.assertIn("disk", err)

    def test_models_checked_against_models_command(self):
        with TempData():
            p = {"name": "g", "mode": "gateway", "model": "claude-opus-5-5", "small_model": "claude-haiku-4-5",
                 "daily_cap_usd": 5, "gateway": {"base_url": URL, "key_command": "echo k",
                                                 "models_command": "printf '[\"claude-haiku-4-5\"]'"}}
            with self.assertRaises(ValueError) as cm:
                profile.save(p)
            self.assertIn("claude-opus-5-5", str(cm.exception))
            self.assertEqual(profile.names(), ["subscription"])

    def test_migration_seeds_subscription(self):
        with TempData() as d:
            (d / "config.json").write_text(json.dumps(
                {"models": {"worker": "opus"}, "worker_effort": "high", "reply_check": {"model": "haiku"}}))
            self.assertTrue(profile.migrate_from_config())
            p = profile.load("subscription")
            self.assertEqual((p["model"], p["effort"], p["small_model"]), ("opus", "high", "haiku"))
            raw = json.loads((d / "config.json").read_text())
            self.assertNotIn("worker_effort", raw)
            self.assertNotIn("model", raw.get("reply_check", {}))
            self.assertNotIn("worker", raw.get("models", {}))
            self.assertFalse(profile.migrate_from_config())

    def test_migration_applies_the_blanks_to_existing_settings(self):
        with TempData() as d:
            f = d / "projects" / "a" / "settings.json"
            f.parent.mkdir(parents=True)
            f.write_text(json.dumps({"permissions": {"deny": ["x"]}, "env": {"AUTOPILOT_ROLE": "worker"}}))
            (d / "settings-dispatcher.json").write_text(json.dumps({"permissions": {"deny": []}, "env": {}}))
            self.assertTrue(profile.migrate_from_config())
            for g in (f, d / "settings-dispatcher.json", d / "settings-light.json"):
                body = json.loads(g.read_text())
                self.assertEqual(body["env"]["ANTHROPIC_BASE_URL"], "", g.name)
                self.assertEqual(body["env"]["AUTOPILOT_PROFILE"], "subscription", g.name)
            self.assertEqual(json.loads(f.read_text())["permissions"], {"deny": ["x"]})

    def test_a_failed_migration_does_not_stop_the_gate(self):
        import contextlib
        import io
        from unittest import mock
        from autopilot import gate
        with TempData():
            err = io.StringIO()
            with mock.patch.object(profile, "migrate_from_config", side_effect=OSError("read-only")), \
                    mock.patch.object(gate, "_sessions", lambda g: []), mock.patch.object(gate, "notify", lambda m: None), \
                    contextlib.redirect_stderr(err):
                self.assertEqual(gate.run(dispatch_fn=lambda g, aps: []), 0)
            self.assertIn("read-only", err.getvalue())

    def test_use_unknown_fails(self):
        with TempData():
            with self.assertRaises(KeyError):
                profile.use("nope")
            self.assertEqual(config.load_global()["active_profile"], "subscription")

    def test_cli_add_and_use(self):
        with TempData() as d:
            code, _, err = run("profile", "add", "g", "--gateway", "--base-url", URL, "--key-command", "echo k",
                               "--small-model", "claude-haiku-4-5", "--model", "claude-opus-5-5",
                               "--effort", "high", "--daily-cap", "500", "--env", "A=b")
            self.assertEqual(code, 0, err)
            self.assertEqual(run("profile", "use", "g")[0], 0)
            p = profile.active()
            self.assertEqual((p["name"], p["daily_cap_usd"], p["env"]), ("g", 500.0, {"A": "b"}))
            self.assertIn("profile_used", (d / "events-global.jsonl").read_text())
            self.assertIn("g", run("profile", "list")[1])
            self.assertEqual(run("profile", "remove", "g")[0], 1)   # active
            self.assertEqual(run("profile", "use", "subscription")[0], 0)
            self.assertEqual(run("profile", "remove", "g")[0], 0)
            self.assertEqual(profile.names(), ["subscription"])


class ProfileNameTest(unittest.TestCase):
    def test_project_style_names_with_underscores_are_valid(self):
        for name in ("team_project", "team_a-2", "a"):
            errs = profile.validate({"name": name, "mode": "subscription"})
            self.assertFalse([e for e in errs if e.startswith("name:")], name)

    def test_a_profile_with_an_underscore_saves_loads_and_lists(self):
        with TempData():
            profile.save({"name": "team_project", "mode": "subscription", "label": "team_project",
                          "model": None, "effort": "high", "env": {}, "settings": {}})
            self.assertEqual(profile.load("team_project")["name"], "team_project")
            self.assertIn("team_project", profile.names())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["profile", "list"]), 0)
            self.assertIn("team_project", out.getvalue())

    def test_bad_profile_names_are_refused(self):
        for name in ("", "Team", "_x", "1x", "a/b", "a.b", "a b", "x" * 41):
            errs = profile.validate({"name": name, "mode": "subscription"})
            self.assertTrue([e for e in errs if e.startswith("name:")], name)


if __name__ == "__main__":
    unittest.main()
