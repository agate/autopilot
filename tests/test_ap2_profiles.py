"""Access profiles in v2 (`lib/assistant/profiles.py`): the store in `<data>/profiles/`, the built-in subscription,
validation, the settings overlay and merge, the symlink guard and `ap2 profile`. Ported from v1's
`tests/test_profile.py` and `tests/test_profile_settings.py` (AUTOPILOT_ -> AP2_). Temp folders only."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT
from tests.test_ap2_cli import temp_inside_the_accounts_claude
from tests.test_ap2_workers import WorkerCase
from assistant import profiles
from assistant.config import ConfigError
from assistant.profiles import ProfileError

URL = "https://llm-gateway.example.com/anthropic"
AP2 = ROOT / "bin" / "ap2"
BLANKED = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
           "ANTHROPIC_DEFAULT_HAIKU_MODEL")


def gateway(name="g", key_command="echo k", **over) -> dict:
    p = {"name": name, "mode": "gateway", "label": name.upper(), "model": "big", "dispatcher_model": None,
         "small_model": "small", "effort": "high", "daily_cap_usd": 5,
         "gateway": {"base_url": URL, "key_command": key_command, "refresh_command": None, "rotate_command": None,
                     "models_command": None},
         "env": {}, "settings": {}}
    p.update(over)
    return p


def key_script(folder: Path) -> Path:
    """A key command that is a script path printing SECRET123: the command text holds no secret, so a file that
    holds SECRET123 can only have got it by running the command."""
    script = Path(folder) / "key.sh"
    script.write_text("#!/bin/sh\necho SECRET123\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


class ProfileStoreTest(WorkerCase):
    def test_builtin_subscription_exists(self):
        p = profiles.active(self.data)
        self.assertEqual((p["name"], p["mode"], p["label"]), ("subscription", "subscription", "Subscription"))
        self.assertEqual((p["model"], p["effort"]), (None, "xhigh"))
        self.assertEqual(profiles.names(self.data), ["subscription"])
        self.assertFalse((self.data / "profiles").exists())  # nothing is written to read it
        self.assertFalse((self.data / "active-profile.json").exists())
        self.assertEqual(profiles.page_profile(self.data)["name"], "subscription")
        # A stored subscription.json replaces the built-in one.
        profiles.save(self.data, dict(profiles.builtin(), effort="high", label="Mine"))
        self.assertEqual((profiles.active(self.data)["effort"], profiles.active(self.data)["label"]), ("high", "Mine"))

    def test_gateway_requires_fields(self):
        errs = " ".join(profiles.validate({"name": "g", "mode": "gateway"}))
        for field in ("base_url", "key_command", "small_model", "daily_cap_usd"):
            self.assertIn(field, errs)
        for cap in (0, -1, True, "5", float("nan"), float("inf")):
            self.assertTrue(any("daily_cap_usd" in e for e in profiles.validate(gateway(daily_cap_usd=cap))), cap)
        self.assertEqual(profiles.validate(gateway()), [])
        self.assertTrue(profiles.validate(gateway(name="Bad_Name")))
        bad_url = gateway()
        bad_url["gateway"]["base_url"] = "ftp://x.example.com"
        self.assertTrue(any("base_url" in e for e in profiles.validate(bad_url)))

    def test_rejects_permissions_in_settings(self):
        for bad in ({"permissions": {}}, {"hooks": {}}, {"apiKeyHelper": "echo k"}, {"disableAllHooks": True}):
            errs = profiles.validate({"name": "s", "mode": "subscription", "settings": bad})
            self.assertTrue(any(next(iter(bad)) in e for e in errs), bad)

    def test_rejects_credential_env(self):
        for name in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "my_secret", "DB_Password", "X_TOKEN"):
            errs = profiles.validate({"name": "s", "mode": "subscription", "env": {name: "v"}})
            self.assertTrue(errs, name)
        self.assertEqual(profiles.validate({"name": "s", "mode": "subscription", "env": {"A": "b"}}), [])

    def test_rejects_credential_in_settings_env(self):
        for settings in ({"env": {"ANTHROPIC_AUTH_TOKEN": "x"}}, {"env": {"api_key": "x"}}, {"apiKeyHelper": "echo k"}):
            errs = profiles.validate({"name": "s", "mode": "subscription", "settings": settings})
            self.assertTrue(errs, settings)

    def test_models_checked_against_models_command(self):
        p = gateway(model="claude-opus-5-5", small_model="claude-haiku-4-5")
        p["gateway"]["models_command"] = "printf '[\"claude-haiku-4-5\"]'"
        with self.assertRaises(ProfileError) as cm:
            profiles.save(self.data, p)
        self.assertIn("claude-opus-5-5", str(cm.exception))
        self.assertEqual(profiles.names(self.data), ["subscription"])
        # Its output is only parsed, never shown: a command that prints something else is named, its output is not.
        p["gateway"]["models_command"] = "printf 'OUTPUT-NOT-SHOWN'"
        with self.assertRaises(ProfileError) as cm:
            profiles.save(self.data, p)
        self.assertIn("JSON list", str(cm.exception))
        self.assertNotIn("OUTPUT-NOT-SHOWN", str(cm.exception))
        p["gateway"]["models_command"] = "printf '[\"claude-haiku-4-5\", \"claude-opus-5-5\"]'"
        profiles.save(self.data, p)
        self.assertEqual(profiles.names(self.data), ["g", "subscription"])

    def test_a_models_command_printing_bad_bytes_is_named(self):
        """Output that is not UTF-8 (a models endpoint answering in gzip) is not a list of model names: the same
        one error, never a traceback."""
        p = gateway()
        p["gateway"]["models_command"] = "printf '\\377\\213'"
        with self.assertRaises(ProfileError) as cm:
            profiles.save(self.data, p)
        self.assertIn("must print a JSON list", str(cm.exception))
        self.assertEqual(profiles.names(self.data), ["subscription"])

    def test_use_unknown_fails(self):
        with self.assertRaises(ProfileError):
            profiles.use(self.data, "nope")
        with self.assertRaises(ProfileError):
            profiles.use(self.data, "../x")
        self.assertEqual(profiles.active(self.data)["name"], "subscription")
        self.assertFalse((self.data / "active-profile.json").exists())

    def test_validate_rejects_names_v2_owns(self):
        for name in ("AP2_ROLE", "AP2_PROFILE", "AP2_DATA", "AUTOPILOT_ROLE", "AUTOPILOT_PROFILE",
                     "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "CLAUDE_CODE_USE_BEDROCK",
                     "CLAUDE_CODE_USE_FOUNDRY"):
            for where in ("env", "settings"):
                p = gateway(**({"env": {name: "x"}} if where == "env" else {"settings": {"env": {name: "x"}}}))
                self.assertTrue(any(name in e for e in profiles.validate(p)), (where, name))
        p = gateway(settings={"disableAllHooks": True})
        self.assertTrue(any("disableAllHooks" in e for e in profiles.validate(p)))

    def test_a_broken_active_profile_refuses_no_fallback(self):
        active = self.data / "active-profile.json"
        for body in ('{"name": "ghost"}', "{nope", '["g"]', '{"name": "Bad/Name"}'):
            active.write_text(body)
            with self.assertRaises(ProfileError) as cm:
                profiles.active(self.data)
            msg = str(cm.exception)
            self.assertIsInstance(cm.exception, ConfigError)  # so `cli.main` exits 1 with one line
            self.assertIn(str(active) if body != '{"name": "ghost"}' else "ghost", msg)
            if body == '{"name": "ghost"}':  # the file that is missing is named
                self.assertIn(str(self.data / "profiles" / "ghost.json"), msg)
            self.assertIn("ap2 profile list", msg)
            self.assertIn("ap2 profile use <name>", msg)
        # A stored profile that no longer loads (hand-edited) refuses the same way.
        (self.data / "profiles").mkdir()
        (self.data / "profiles" / "g.json").write_text("{nope")
        active.write_text('{"name": "g"}')
        with self.assertRaisesRegex(ProfileError, "g.json"):
            profiles.active(self.data)
        (self.data / "profiles" / "g.json").write_text(json.dumps(dict(gateway(), daily_cap_usd=0)))
        with self.assertRaisesRegex(ProfileError, "daily_cap_usd"):
            profiles.active(self.data)
        # No worker starts, fresh or resumed: nothing falls back to the subscription.
        with self.assertRaises(ProfileError):
            self.start()
        self.assertEqual(self.calls(), [])  # no discovery, no session
        profiles.use(self.data, "subscription")
        a = self.start()
        active.write_text('{"name": "ghost"}')
        self.drop_agent(a["short_id"])
        line = self.workers().check()[0]
        self.assertEqual((line["state"], line["did"]), ("dead", "nothing"))
        self.assertIn("ghost", line["note"])
        self.assertEqual(len(self.bg_calls()), 1)
        self.assertEqual(self.calls()[-1]["argv"][:1], ["agents"])

    def test_a_linked_profiles_folder_is_refused(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (self.data / "profiles").symlink_to(elsewhere, target_is_directory=True)
        for call in (lambda: profiles.save(self.data, gateway()), lambda: profiles.names(self.data),
                     lambda: profiles.load(self.data, "g"), lambda: profiles.remove(self.data, "g")):
            with self.assertRaisesRegex(ProfileError, f"{self.data / 'profiles'} is a symlink"):
                call()
        self.assertEqual(list(elsewhere.iterdir()), [])
        (self.data / "profiles").unlink()
        # A linked profile file, and a linked active-profile.json, are refused too, never followed.
        (self.data / "profiles").mkdir()
        (elsewhere / "g.json").write_text(json.dumps(gateway()))
        (self.data / "profiles" / "g.json").symlink_to(elsewhere / "g.json")
        with self.assertRaisesRegex(ProfileError, "g.json is a symlink"):
            profiles.load(self.data, "g")
        with self.assertRaisesRegex(ProfileError, "g.json is a symlink"):
            profiles.save(self.data, gateway())
        (self.data / "profiles" / "g.json").unlink()
        profiles.save(self.data, gateway())
        (elsewhere / "active.json").write_text('{"name": "g"}')
        (self.data / "active-profile.json").symlink_to(elsewhere / "active.json")
        for call in (lambda: profiles.active(self.data), lambda: profiles.use(self.data, "subscription")):
            with self.assertRaisesRegex(ProfileError, "active-profile.json is a symlink"):
                call()
        self.assertEqual((elsewhere / "active.json").read_text(), '{"name": "g"}')

    def test_nothing_is_written_in_v1s_folder(self):
        v1 = self.tmp / "v1-data"
        v1.mkdir()
        with mock.patch.dict(os.environ, {"AUTOPILOT_DATA": str(v1)}):
            profiles.save(self.data, gateway())
            profiles.use(self.data, "g")
            self.start()
            profiles.use(self.data, "subscription")
            profiles.remove(self.data, "g")
        self.assertEqual(list(v1.iterdir()), [])
        self.assertTrue((self.data / "active-profile.json").is_file())


class ProfileOverlayTest(unittest.TestCase):
    def test_gateway_overlay_keeps_floor(self):
        from assistant import workers
        with tempfile.TemporaryDirectory() as d:
            data = Path(d).resolve()
            env = {"AP2_ROLE": "implement", "AP2_PROFILE": "g"}
            base = workers.floor("implement", {"tools": ["Read"]}, data, env)
            got = profiles.merge(base, profiles.overlay(gateway()))
            self.assertEqual(got["permissions"], base["permissions"])
            self.assertEqual(got["hooks"], base["hooks"])
            self.assertEqual(got["env"]["ANTHROPIC_BASE_URL"], URL)
            self.assertEqual(got["env"]["AP2_ROLE"], "implement")
            self.assertEqual(got["apiKeyHelper"], "echo k")
            self.assertNotIn("permissions", profiles.overlay(gateway()))
            self.assertNotIn("hooks", profiles.overlay(gateway()))

    def test_a_profiles_own_env_and_settings_reach_the_overlay(self):
        from assistant import workers
        with tempfile.TemporaryDirectory() as d:
            data = Path(d).resolve()
            base = workers.floor("implement", {"tools": ["Read"]}, data, {"AP2_ROLE": "implement"})
            for prof in (gateway(), profiles.builtin()):
                prof["env"] = {"API_TIMEOUT_MS": "960000"}
                prof["settings"] = {"env": {"BUN_CONFIG_HTTP_IDLE_TIMEOUT": "960"}, "pluginConfigs": {"p": {}}}
                for got in (profiles.overlay(prof), profiles.merge(base, profiles.overlay(prof))):
                    self.assertEqual(got["env"]["API_TIMEOUT_MS"], "960000", prof["mode"])
                    self.assertEqual(got["env"]["BUN_CONFIG_HTTP_IDLE_TIMEOUT"], "960", prof["mode"])
                    self.assertEqual(got["pluginConfigs"], {"p": {}}, prof["mode"])

    def test_gateway_overlay_blanks_competing_auth(self):
        g = gateway(env={"ANTHROPIC_API_KEY": "x"}, settings={"env": {"ANTHROPIC_AUTH_TOKEN": "y"}})
        ov = profiles.overlay(g)
        self.assertEqual(ov["env"]["ANTHROPIC_AUTH_TOKEN"], "")
        self.assertEqual(ov["env"]["ANTHROPIC_API_KEY"], "")
        self.assertEqual(ov["apiKeyHelper"], "echo k")
        self.assertEqual(ov["env"]["ENABLE_TOOL_SEARCH"], "true")
        self.assertEqual((ov["env"]["ANTHROPIC_SMALL_FAST_MODEL"], ov["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"]),
                         ("small", "small"))

    def test_subscription_overlay_blanks_gateway_env(self):
        ov = profiles.overlay(profiles.builtin())
        for k in BLANKED:
            self.assertEqual(ov["env"][k], "")
        self.assertEqual(ov["env"]["AP2_PROFILE"], "subscription")
        self.assertNotIn("permissions", ov)
        self.assertNotIn("hooks", ov)
        self.assertNotIn("apiKeyHelper", ov)

    def test_subscription_overlay_blanks_api_key(self):
        self.assertEqual(profiles.overlay(profiles.builtin())["env"]["ANTHROPIC_API_KEY"], "")

    def test_subscription_blanks_win_over_profile_env(self):
        p = profiles.builtin()
        p["env"] = {k: "https://x.example.com" for k in BLANKED}
        p["settings"] = {"env": {k: "https://y.example.com" for k in BLANKED}}
        ov = profiles.overlay(p)
        for k in BLANKED:
            self.assertEqual(ov["env"][k], "")

    def test_cloud_provider_switches_are_blanked_in_both_modes(self):
        for prof in (profiles.builtin(), gateway()):
            prof["env"] = {"CLAUDE_CODE_USE_BEDROCK": "1"}
            prof["settings"] = {"env": {"CLAUDE_CODE_USE_VERTEX": "1"}}
            env = profiles.overlay(prof)["env"]
            for k in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"):
                self.assertEqual(env[k], "", (prof["mode"], k))

    def test_a_gateway_profile_without_a_model_blanks_the_model(self):
        self.assertEqual(profiles.overlay(gateway(model=None))["env"]["ANTHROPIC_MODEL"], "")
        self.assertEqual(profiles.overlay(gateway())["env"]["ANTHROPIC_MODEL"], "big")

    def test_merge_keeps_floor_ap2_env(self):
        floor = {"permissions": {"deny": ["x"]}, "hooks": {"h": 1},
                 "env": {"AP2_ROLE": "implement", "AP2_PROFILE": "old", "OTHER": "1"}}
        out = profiles.merge(floor, {"env": {"AP2_PROFILE": "new", "Z": "2"}, "apiKeyHelper": "k"})
        self.assertEqual(out["env"], {"AP2_ROLE": "implement", "AP2_PROFILE": "new", "Z": "2"})
        self.assertEqual(out["permissions"], {"deny": ["x"]})
        self.assertEqual(out["hooks"], {"h": 1})
        self.assertEqual(out["apiKeyHelper"], "k")
        # An overlay can never carry its own permissions or hooks into the merge.
        out = profiles.merge(floor, {"permissions": {"allow": ["*"]}, "hooks": {}, "env": {}})
        self.assertEqual((out["permissions"], out["hooks"]), ({"deny": ["x"]}, {"h": 1}))

    def test_floor_ap2_env_wins_over_the_overlay(self):
        floor = {"permissions": {"deny": []}, "env": {"AP2_ROLE": "implement", "AP2_DATA": "/d"}}
        out = profiles.merge(floor, {"env": {"AP2_ROLE": "dispatcher", "AP2_DATA": "/x", "AP2_PROFILE": "g"}})
        self.assertEqual(out["env"], {"AP2_ROLE": "implement", "AP2_DATA": "/d", "AP2_PROFILE": "g"})


class ProfileCliTest(unittest.TestCase):
    """`ap2 profile ...` through `bin/ap2 --data <tmp>`: JSON on stdout, one `ap2: ...` line and exit 1 on an error."""

    def setUp(self):
        if temp_inside_the_accounts_claude():
            self.skipTest("the temp folder is inside the account's ~/.claude, so ap2 refuses every temp data folder")
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-profile-cli-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "data"
        self.v1 = self.tmp / "v1-data"
        self.v1.mkdir()
        home = self.tmp / "home"
        home.mkdir()
        self.env = {k: v for k, v in os.environ.items() if k != "AP2_DATA" and not k.startswith("AUTOPILOT_")}
        self.env.update(HOME=str(home), AUTOPILOT_DATA=str(self.v1), PYTHONIOENCODING="utf-8")

    def run_ap2(self, *args):
        return subprocess.run([sys.executable, str(AP2), "profile", *args, "--data", str(self.data)],
                              capture_output=True, text=True, env=self.env, timeout=60)

    def ok(self, *args):
        r = self.run_ap2(*args)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        return json.loads(r.stdout)

    def fails(self, *args, text=""):
        r = self.run_ap2(*args)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(r.stdout, "")
        self.assertEqual(len(r.stderr.splitlines()), 1, r.stderr)
        self.assertTrue(r.stderr.startswith("ap2: "), r.stderr)
        self.assertIn(text, r.stderr)
        return r.stderr

    def add_gateway(self, name="g", key="echo k"):
        return self.ok("add", name, "--gateway", "--base-url", URL, "--key-command", key,
                       "--small-model", "claude-haiku-4-5", "--model", "claude-opus-5-5",
                       "--daily-cap", "500", "--env", "A=b")

    def test_cli_add_use_list_remove(self):
        got = self.add_gateway()
        self.assertEqual((got["name"], got["mode"], got["daily_cap_usd"], got["env"]),
                         ("g", "gateway", 500.0, {"A": "b"}))
        self.assertEqual(json.loads((self.data / "profiles" / "g.json").read_text())["gateway"]["base_url"], URL)
        self.fails("add", "g", "--subscription", text="already exists")
        self.fails("add", "subscription", "--subscription", text="already exists")
        self.fails("add", "h", "--gateway", "--base-url", URL, text="key_command")
        self.fails("add", "h", "--gateway", "--daily-cap", "lots", text="--daily-cap")
        self.fails("add", "h", "--subscription", "--env", "nokey", text="KEY=VALUE")
        self.fails("add", "h", "--subscription", "--base-url", URL, text="are for a --gateway profile")
        self.assertFalse((self.data / "profiles" / "h.json").exists())
        extra = self.tmp / "extra.json"
        extra.write_text(json.dumps({"pluginConfigs": {"p": {}}}))
        self.assertEqual(self.ok("add", "s", "--subscription", "--settings-file", str(extra))["settings"],
                         {"pluginConfigs": {"p": {}}})
        self.assertEqual(json.loads((self.data / "profiles" / "s.json").read_text())["settings"],
                         {"pluginConfigs": {"p": {}}})
        self.assertEqual(self.ok("remove", "s"), {"removed": "s"})
        self.assertEqual(self.ok("use", "g"), {"active": "g", "was": "subscription"})
        listed = self.ok("list")
        self.assertEqual(listed["active"], "g")
        self.assertEqual([(p["name"], p["mode"], p["active"]) for p in listed["profiles"]],
                         [("g", "gateway", True), ("subscription", "subscription", False)])
        self.assertEqual(self.ok("show", "g")["daily_cap_usd"], 500.0)
        self.fails("remove", "g", text="active")
        self.fails("remove", "subscription", text="subscription")
        self.fails("use", "nope", text="nope")
        self.assertEqual(self.ok("use", "subscription"), {"active": "subscription", "was": "g"})
        self.assertEqual(self.ok("remove", "g"), {"removed": "g"})
        self.assertEqual([p["name"] for p in self.ok("list")["profiles"]], ["subscription"])
        self.fails("show", "g", text="no profile named 'g'")
        self.assertEqual(list(self.v1.iterdir()), [])  # v1's data folder is never touched

    def test_show_prints_commands_never_key_output(self):
        script = key_script(self.tmp)
        self.add_gateway(key=str(script))
        r = self.run_ap2("show", "g")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(str(script), r.stdout)
        self.assertEqual(json.loads(r.stdout)["gateway"]["key_command"], str(script))
        for args in (("list",), ("use", "g"), ("show", "g")):
            r = self.run_ap2(*args)
            self.assertNotIn("SECRET123", r.stdout + r.stderr, args)
        for f in self.data.rglob("*"):
            if f.is_file():
                self.assertNotIn(b"SECRET123", f.read_bytes(), f)

    def test_use_works_in_shadow_mode(self):
        self.add_gateway()
        self.assertIn("mode: shadow", (self.data / "config.yaml").read_text())
        self.assertEqual(self.ok("use", "g"), {"active": "g", "was": "subscription"})
        self.assertEqual(json.loads((self.data / "active-profile.json").read_text()), {"name": "g"})
        self.assertEqual(self.ok("use", "subscription"), {"active": "subscription", "was": "g"})  # the undo

    def test_list_still_works_when_the_active_profile_is_broken(self):
        self.data.mkdir()
        (self.data / "active-profile.json").write_text('{"name": "ghost"}')
        listed = self.ok("list")
        self.assertEqual(listed["active"], "ghost")
        self.assertIn("ghost", listed["active_error"])
        self.assertEqual(self.ok("use", "subscription"), {"active": "subscription", "was": "ghost"})
        self.assertIsNone(self.ok("list")["active_error"])

    def test_a_write_error_prints_one_line(self):
        if os.geteuid() == 0:
            self.skipTest("root writes into a read-only folder")
        self.ok("add", "s2", "--subscription")
        folder = self.data / "profiles"
        folder.chmod(0o555)
        self.addCleanup(folder.chmod, 0o755)
        for args, text in ((("remove", "s2"), "s2"), (("add", "s3", "--subscription"), "s3.json")):
            r = self.run_ap2(*args)
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertEqual(len(r.stderr.splitlines()), 1, r.stderr)
            self.assertTrue(r.stderr.startswith("ap2: "), r.stderr)
            self.assertNotIn("Traceback", r.stderr)
            self.assertIn(text, r.stderr)
        self.assertTrue((folder / "s2.json").exists())
        self.assertFalse((folder / "s3.json").exists())
        # active-profile.json sits in the data folder itself, so `use` fails only when that folder is read-only.
        self.data.chmod(0o555)
        self.addCleanup(self.data.chmod, 0o755)  # runs before the folder chmod above and the rmtree
        r = self.run_ap2("use", "s2")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(len(r.stderr.splitlines()), 1, r.stderr)
        self.assertTrue(r.stderr.startswith("ap2: "), r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("active-profile.json", r.stderr)
        self.assertFalse((self.data / "active-profile.json").exists())

    def test_list_and_show_create_nothing(self):
        self.ok("list")
        self.ok("show", "subscription")
        self.assertFalse(self.data.exists())


if __name__ == "__main__":
    unittest.main()
