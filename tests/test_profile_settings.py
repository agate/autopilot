from __future__ import annotations

import json
import unittest

from tests.helpers import TempData
from autopilot import paths, perms, profile

URL = "https://llm-gateway.example.com/anthropic"
TOOLS = ["mcp__slack__slack_send_message", "mcp__x__read"]
BLANKED = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
           "ANTHROPIC_DEFAULT_HAIKU_MODEL")


def gateway(key_command="echo SECRET123"):
    p = profile.load("subscription")
    p.update(name="g", mode="gateway", label="G", model="big", small_model="small", daily_cap_usd=5,
             gateway={"base_url": URL, "key_command": key_command, "refresh_command": None,
                      "rotate_command": None, "models_command": None})
    return p


def read(path):
    return json.loads(path.read_text())


def write_files(ap="a"):
    """Write worker, dispatcher and one-off files the way write_for would, under the active profile."""
    d = paths.data_root()
    perms._write(paths.ap_dir(ap) / "settings.json",
                 perms.generate(TOOLS, role="worker", profile=profile.active()))
    perms._write(d / "settings-dispatcher.json",
                 perms.generate(TOOLS, role="dispatcher", profile=profile.page_profile()))
    return perms.one_off_settings(ap, "mcp__slack__slack_send_message")


class ProfileSettingsTest(unittest.TestCase):
    def test_gateway_overlay_keeps_floor(self):
        with TempData():
            g = gateway()
            got = perms.generate(TOOLS, role="worker", profile=g)
            self.assertEqual(got["permissions"], perms.generate(TOOLS, role="worker")["permissions"])
            self.assertEqual(got["hooks"], perms.generate(TOOLS, role="worker")["hooks"])
            self.assertEqual(got["env"]["ANTHROPIC_BASE_URL"], URL)
            self.assertEqual(got["env"]["AUTOPILOT_ROLE"], "worker")
            self.assertEqual(got["apiKeyHelper"], "echo SECRET123")

    def test_no_key_value_in_settings(self):
        import os
        import stat
        import tempfile
        from pathlib import Path
        with TempData() as d, tempfile.TemporaryDirectory() as tools:
            script = Path(tools) / "key.sh"
            script.write_text("#!/bin/sh\necho SECRET123\n")
            script.chmod(script.stat().st_mode | stat.S_IXUSR)
            profile.save(gateway(str(script)))
            profile.use("g")
            write_files()
            profile.apply()
            self.assertIn(str(script), (paths.ap_dir("a") / "settings.json").read_text())
            for f in d.rglob("*"):
                if f.is_file():
                    self.assertNotIn("SECRET123", f.read_text(), f)

    def test_subscription_blanks_win_over_profile_env(self):
        with TempData():
            p = profile.load("subscription")
            p["env"] = {k: "https://x.example.com" for k in BLANKED}
            p["settings"] = {"env": {k: "https://y.example.com" for k in BLANKED}}
            ov = profile.overlay(p)
            for k in BLANKED:
                self.assertEqual(ov["env"][k], "")

    def test_subscription_overlay_blanks_gateway_env(self):
        with TempData():
            ov = profile.overlay(profile.load("subscription"))
            for k in BLANKED:
                self.assertEqual(ov["env"][k], "")
            self.assertEqual(ov["env"]["AUTOPILOT_PROFILE"], "subscription")
            self.assertNotIn("permissions", ov)
            self.assertNotIn("hooks", ov)

    def test_apply_swaps_profile(self):
        with TempData():
            profile.save(gateway())
            profile.use("g")
            one = write_files()
            w = paths.ap_dir("a") / "settings.json"
            self.assertIn(URL, w.read_text())
            before = {f: read(f)["permissions"] for f in (w, one)}
            profile.use("subscription")
            self.assertNotIn(URL, w.read_text())
            self.assertNotIn(URL, one.read_text())
            self.assertNotIn("apiKeyHelper", read(w))
            self.assertEqual(before, {f: read(f)["permissions"] for f in (w, one)})
            self.assertEqual(read(w)["env"]["AUTOPILOT_ROLE"], "worker")
            self.assertEqual(read(w)["env"]["AUTOPILOT_PROFILE"], "subscription")

    def test_page_files_stay_subscription(self):
        with TempData() as d:
            profile.save(gateway())
            profile.use("g")
            write_files()
            profile.apply()
            for f in (d / "settings-dispatcher.json", profile.light_settings()):
                self.assertNotIn(URL, f.read_text())
                self.assertNotIn("apiKeyHelper", read(f))
                self.assertEqual(read(f)["env"]["AUTOPILOT_PROFILE"], "subscription")
                self.assertEqual(read(f)["env"]["ANTHROPIC_BASE_URL"], "")
            self.assertEqual(profile.light_settings(), d / "settings-light.json")
            self.assertEqual(read(profile.light_settings())["permissions"],
                             perms.static_settings("light")["permissions"])

    def test_merge_keeps_floor_autopilot_env(self):
        floor = {"permissions": {"deny": ["x"]}, "hooks": {"h": 1},
                 "env": {"AUTOPILOT_ROLE": "worker", "AUTOPILOT_PROFILE": "old", "OTHER": "1"}}
        out = profile.merge(floor, {"env": {"AUTOPILOT_PROFILE": "new", "Z": "2"}, "apiKeyHelper": "k"})
        self.assertEqual(out["env"], {"AUTOPILOT_ROLE": "worker", "AUTOPILOT_PROFILE": "new", "Z": "2"})
        self.assertEqual(out["permissions"], {"deny": ["x"]})
        self.assertEqual(out["hooks"], {"h": 1})
        self.assertEqual(out["apiKeyHelper"], "k")


if __name__ == "__main__":
    unittest.main()
