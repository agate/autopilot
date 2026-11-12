"""Every worker v2 starts or resumes runs on the active access profile, through `--settings`, without ever losing its
permission floor (design section 12, access profiles). Ported from v1's `tests/test_profile_launch.py`. The fake
claude (`tests/bin/fake-claude-v2`) runs against temp folders only."""
from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT
from tests.test_ap2_profiles import URL, gateway, key_script
from tests.test_ap2_workers import WorkerCase
from assistant import profiles, resources, transcripts
from assistant.workers import WorkerError, Workers

OTHER_URL = "https://other-gateway.example.com/anthropic"
CONNECTOR = "mcp__claude_ai_Slack__slack_send_message"


def opt(argv, flag):
    return argv[argv.index(flag) + 1]


class EveryLaunchTest(WorkerCase):
    def test_every_launch_passes_settings(self):
        """A function that starts a session (`--bg` or `-p`) must also name `--settings`, so no new launch point
        can skip the profile."""
        seen = []
        for f in sorted((ROOT / "lib" / "assistant").rglob("*.py")):
            for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                words = {n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
                if words & {"--bg", "-p"}:
                    seen.append(node.name)
                    self.assertIn("--settings", words, f"{f.name}:{node.lineno} {node.name} starts a session "
                                                       "without --settings")
        self.assertGreaterEqual(len(seen), 2)
        self.assertLessEqual({"_argv", "_discover"}, set(seen))


class WorkerProfileLaunchTest(WorkerCase):
    def use_gateway(self, name="g", url=URL, **over) -> dict:
        p = gateway(name, **over)
        p["gateway"]["base_url"] = url
        profiles.save(self.data, p)
        profiles.use(self.data, name)
        return p

    def settings_of(self, call) -> dict:
        return json.loads(Path(opt(call["argv"], "--settings")).read_text())

    def test_the_floor_is_unchanged_under_a_gateway_profile(self):
        sub = self.start()
        self.use_gateway()
        gw = self.start()
        a, b = (self.settings_of(c) for c in self.bg_calls())
        self.assertEqual(a["permissions"]["deny"], b["permissions"]["deny"])
        self.assertEqual(a["permissions"]["allow"], b["permissions"]["allow"])
        self.assertEqual(a["hooks"], b["hooks"])
        self.assertEqual(b["env"]["ANTHROPIC_BASE_URL"], URL)
        self.assertEqual(b["apiKeyHelper"], "echo k")
        self.assertEqual((b["env"]["AP2_ROLE"], b["env"]["AP2_CARD"], b["env"]["AP2_PROFILE"]),
                         ("implement", gw["card"], "g"))
        self.assertEqual((a["env"]["ANTHROPIC_BASE_URL"], a["env"]["AP2_PROFILE"]), ("", "subscription"))
        self.assertNotIn("apiKeyHelper", a)
        self.assertEqual((sub["profile"], gw["profile"]), ("subscription", "g"))
        self.assertEqual(self.get(gw["card"])["sessions"]["implement"]["profile"], "g")
        # AP2_PROFILE is in the process env too, the same as in the settings env.
        self.assertEqual(self.bg_calls()[1]["env"]["AP2_PROFILE"], "g")

    def test_worker_argv_uses_profile_model_and_effort(self):
        self.use_gateway(model="m1", effort="high")
        a = self.start()
        argv = self.bg_calls()[0]["argv"]
        self.assertEqual(opt(argv, "--model"), "m1")
        self.assertEqual(opt(argv, "--effort"), "high")
        run = transcripts.run_dir(self.data, a["card"])
        self.assertEqual(opt(argv, "--settings"), str(run / "settings-implement.json"))
        self.assertLess(argv.index("--agent"), argv.index("--model"))
        # A resume gets neither flag (design 8.2): the gateway session takes its model from ANTHROPIC_MODEL.
        w = self.workers()
        w.end(a["card"], "implement")
        w.start(a["card"], "implement", resume=True)
        last = self.bg_calls()[-1]["argv"]
        self.assertEqual(last[1:3], ["--resume", a["session_id"]])
        self.assertNotIn("--model", last)
        self.assertNotIn("--effort", last)
        self.assertEqual(self.settings_of(self.bg_calls()[-1])["env"]["ANTHROPIC_MODEL"], "m1")
        # No model of its own: no --model flag, and the overlay blanks the variable.
        self.use_gateway("g2", model=None, effort=None)
        self.start()
        argv = self.bg_calls()[-1]["argv"]
        self.assertNotIn("--model", argv)
        self.assertNotIn("--effort", argv)
        self.assertEqual(self.settings_of(self.bg_calls()[-1])["env"]["ANTHROPIC_MODEL"], "")

    def test_subscription_start_argv_is_unchanged(self):
        a = self.start()
        argv = self.bg_calls()[0]["argv"]
        self.assertNotIn("--model", argv)
        self.assertEqual(opt(argv, "--effort"), "xhigh")
        run = transcripts.run_dir(self.data, a["card"])
        self.assertEqual(argv[1:], ["-n", f"{a['card']} · implement", "--permission-mode", "auto",
                                    "--settings", str(run / "settings-implement.json"),
                                    "--agent", "autopilot:implementer", "--effort", "xhigh", argv[-1]])
        env = self.settings_of(self.bg_calls()[0])["env"]
        for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
            self.assertEqual(env[k], "", k)

    def test_no_key_value_in_any_file(self):
        script = key_script(self.tmp)
        self.use_gateway(key_command=str(script))
        a = self.start()
        w = self.workers()
        self.drop_agent(a["short_id"])
        self.assertEqual(w.check()[0]["did"], "resumed")
        self.write_result(a["card"])
        self.assertEqual(w.check()[0]["did"], "ended")
        run = transcripts.run_dir(self.data, a["card"])
        self.assertIn(str(script), (run / "settings-implement.json").read_text())
        self.assertIn(str(script), (run / "settings-discover-implement.json").read_text())
        checked = 0
        for root in (self.data, run, self.fake_home):
            for f in root.rglob("*"):
                if f.is_file():
                    checked += 1
                    self.assertNotIn(b"SECRET123", f.read_bytes(), f)
        self.assertGreater(checked, 5)
        self.assertNotIn("SECRET123", self.log.read_text())

    def test_resumed_worker_uses_new_profile(self):
        self.use_gateway("g1")
        a = self.start()
        self.assertEqual(self.get(a["card"])["sessions"]["implement"]["profile"], "g1")
        self.use_gateway("g2", url=OTHER_URL)
        self.drop_agent(a["short_id"])  # dead: an in-place resume
        self.assertEqual(self.workers().check()[0]["did"], "resumed")
        last = self.bg_calls()[-1]
        self.assertEqual(last["argv"][1:3], ["--resume", a["session_id"]])
        self.assertEqual(self.settings_of(last)["env"]["ANTHROPIC_BASE_URL"], OTHER_URL)
        self.assertEqual(self.settings_of(last)["env"]["AP2_PROFILE"], "g2")
        self.assertEqual(last["env"]["AP2_PROFILE"], "g2")
        self.assertEqual(resources.Registry(self.data).read()[0]["profile"], "g2")
        self.assertEqual(self.get(a["card"])["sessions"]["implement"]["profile"], "g2")
        # A `--resume` after an end takes the active profile at that moment too.
        w = self.workers()
        w.end(a["card"], "implement")
        profiles.use(self.data, "subscription")
        rec = w.start(a["card"], "implement", resume=True)
        self.assertEqual(rec["profile"], "subscription")
        self.assertEqual(self.settings_of(self.bg_calls()[-1])["env"]["ANTHROPIC_BASE_URL"], "")

    def test_a_running_workers_settings_are_not_rewritten_by_use(self):
        self.use_gateway("g1")
        a = self.start()
        f = transcripts.run_dir(self.data, a["card"]) / "settings-implement.json"
        before = f.read_bytes()
        self.use_gateway("g2", url=OTHER_URL)
        profiles.use(self.data, "subscription")
        self.assertEqual(self.workers().check()[0]["state"], "running")
        self.assertEqual(f.read_bytes(), before)
        self.assertEqual(resources.Registry(self.data).read()[0]["profile"], "g1")

    def test_discover_passes_profile_settings(self):
        self.use_gateway()
        a = self.start()
        run = transcripts.run_dir(self.data, a["card"])
        found = self.discovery_calls()
        self.assertEqual([opt(c["argv"], "--settings") for c in found],
                         [str(run / "settings-discover-implement.json"),
                          str(run / "settings-discover-implement-page.json")])
        for c in found:
            self.assertEqual(c["env"], self.bg_calls()[0]["env"])  # the worker's own env, both probes
            self.assertEqual(c["cwd"], str(self.wt))

    def test_discovery_settings_have_no_floor(self):
        self.use_gateway()
        a = self.start()
        run = transcripts.run_dir(self.data, a["card"])
        probe = json.loads((run / "settings-discover-implement.json").read_text())
        self.assertEqual(probe, profiles.overlay(profiles.load(self.data, "g")))
        self.assertEqual(probe["env"]["ANTHROPIC_BASE_URL"], URL)
        self.assertNotIn("permissions", probe)
        self.assertNotIn("hooks", probe)
        page = json.loads((run / "settings-discover-implement-page.json").read_text())
        self.assertEqual(page, profiles.overlay(profiles.page_profile(self.data)))
        self.assertEqual(page["env"]["ANTHROPIC_BASE_URL"], "")
        self.assertNotIn("permissions", page)

    def test_gateway_floor_denies_what_either_probe_found(self):
        probes = []

        def disc(self_, cwd, env, settings):
            probes.append(Path(settings).name)
            if Path(settings).name.endswith("-page.json"):
                return ["Bash", CONNECTOR], ["mcp__claude_ai_Gmail"]
            return ["Bash", "mcp__gw__post_message"], ["mcp__gw_off"]

        self.use_gateway()
        with mock.patch.object(Workers, "_discover", disc):
            a = self.start()
        self.assertEqual(probes, ["settings-discover-implement.json", "settings-discover-implement-page.json"])
        deny = self.deny(a["card"])
        for rule in (CONNECTOR, "mcp__claude_ai_Gmail", "mcp__gw__post_message", "mcp__gw_off"):
            self.assertIn(rule, deny)
        self.assertNotIn("Bash", deny)
        # Back on the subscription, a start probes once: its floor is written from what that one probe found (on
        # the real subscription the claude.ai connectors are in it, so they are denied from it).
        profiles.use(self.data, "subscription")
        probes.clear()
        with mock.patch.object(Workers, "_discover", disc):
            b = self.start()
        self.assertEqual(probes, ["settings-discover-implement.json"])
        self.assertIn("mcp__gw__post_message", self.deny(b["card"]))
        self.assertNotIn(CONNECTOR, self.deny(b["card"]))

    def test_a_failed_page_probe_fails_the_start(self):
        def disc(self_, cwd, env, settings):
            if Path(settings).name.endswith("-page.json"):
                raise RuntimeError("no init event")
            return ["Bash"], []

        self.use_gateway()
        cid = self.add_card()
        with mock.patch.object(Workers, "_discover", disc):
            with self.assertRaisesRegex(WorkerError, "no init event"):
                self.start(card_id=cid)
        self.assertEqual(self.bg_calls(), [])
        self.assertFalse((transcripts.run_dir(self.data, cid) / "settings-implement.json").exists())

    def test_the_profile_is_read_once_per_start(self):
        """A `use` just as the tools start to be listed changes nothing in this start: the probes, the floor, the
        argv, the process env, the record and the card all take the profile read before the tool listing."""
        self.use_gateway("g2", url=OTHER_URL, model="m2", effort="low")
        self.use_gateway("g1", model="m1", effort="high")
        real = Workers._tools
        switched = []

        def tools(self_, *args):
            if not switched:
                switched.append(profiles.use(self.data, "g2"))
            return real(self_, *args)

        with mock.patch.object(Workers, "_tools", tools):
            a = self.start()
        self.assertEqual(switched, [{"active": "g2", "was": "g1"}])
        probe = transcripts.run_dir(self.data, a["card"]) / "settings-discover-implement.json"
        self.assertEqual(json.loads(probe.read_text())["env"]["ANTHROPIC_BASE_URL"], URL)
        env = self.settings_of(self.bg_calls()[-1])["env"]
        self.assertEqual((env["ANTHROPIC_BASE_URL"], env["AP2_PROFILE"]), (URL, "g1"))
        argv = self.bg_calls()[-1]["argv"]
        self.assertEqual((opt(argv, "--model"), opt(argv, "--effort")), ("m1", "high"))
        self.assertEqual(self.bg_calls()[-1]["env"]["AP2_PROFILE"], "g1")  # the process env, as the settings env
        self.assertEqual(a["profile"], "g1")
        self.assertEqual(resources.Registry(self.data).read()[0]["profile"], "g1")
        self.assertEqual(self.get(a["card"])["sessions"]["implement"]["profile"], "g1")

    def test_the_profile_is_read_once_per_resume(self):
        """A `use` while an in-place resume lists its tools (the lock free, between `_plan_resume` and
        `_resume_dead`) changes nothing in this resume: the probe, the floor, the process env, the record and the
        card all take the profile `_plan_resume` read."""
        a = self.start()  # on the subscription, so every field below must change to g1
        self.use_gateway("g2", url=OTHER_URL, model="m2", effort="low")
        self.use_gateway("g1", model="m1", effort="high")
        self.drop_agent(a["short_id"])  # dead: an in-place resume
        probe = transcripts.run_dir(self.data, a["card"]) / "settings-discover-implement.json"
        real = Workers._tools
        switched, probes = [], []

        def tools(self_, *args):
            if not switched:
                switched.append(profiles.use(self.data, "g2"))
            found = real(self_, *args)
            probes.append(json.loads(probe.read_text()))  # what this resume's own discovery ran on
            return found

        before = len(self.discovery_calls())
        with mock.patch.object(Workers, "_tools", tools):
            self.assertEqual(self.workers().check()[0]["did"], "resumed")
        self.assertEqual(switched, [{"active": "g2", "was": "g1"}])
        self.assertGreater(len(self.discovery_calls()), before)  # the resume ran its own discovery
        self.assertEqual([(p["env"]["ANTHROPIC_BASE_URL"], p["env"]["AP2_PROFILE"]) for p in probes], [(URL, "g1")])
        last = self.bg_calls()[-1]
        self.assertEqual(last["argv"][1:3], ["--resume", a["session_id"]])
        env = self.settings_of(last)["env"]
        self.assertEqual((env["ANTHROPIC_BASE_URL"], env["AP2_PROFILE"], env["ANTHROPIC_MODEL"]), (URL, "g1", "m1"))
        # A resume gets neither --model nor --effort (design 8.2): its model comes from ANTHROPIC_MODEL above.
        self.assertNotIn("--model", last["argv"])
        self.assertNotIn("--effort", last["argv"])
        self.assertEqual(last["env"]["AP2_PROFILE"], "g1")  # the process env, as the settings env
        self.assertEqual(resources.Registry(self.data).read()[0]["profile"], "g1")
        self.assertEqual(self.get(a["card"])["sessions"]["implement"]["profile"], "g1")

    def test_an_unreadable_profiles_folder_starts_nothing(self):
        if os.geteuid() == 0:
            self.skipTest("root reads a folder with mode 000")
        self.use_gateway()
        cid = self.add_card()
        folder = self.data / "profiles"
        folder.chmod(0)
        self.addCleanup(folder.chmod, 0o755)
        with self.assertRaisesRegex(profiles.ProfileError, f"cannot read {re.escape(str(folder))}"):
            self.start(card_id=cid)
        self.assertEqual(self.calls(), [])  # no discovery, no launch
        run = transcripts.run_dir(self.data, cid)
        for name in ("settings-implement.json", "settings-discover-implement.json"):
            self.assertFalse((run / name).exists(), name)
        self.assertEqual(resources.Registry(self.data).read(), [])

    def test_one_probe_on_the_subscription(self):
        a = self.start()
        found = self.discovery_calls()
        self.assertEqual(len(found), 1)
        self.assertEqual(Path(opt(found[0]["argv"], "--settings")).name, "settings-discover-implement.json")
        self.assertFalse((transcripts.run_dir(self.data, a["card"]) / "settings-discover-implement-page.json").exists())

    def test_profiles_are_edit_denied(self):
        self.use_gateway()
        w = self.workers()
        for role in ("implement", "review", "pr"):
            cid = self.add_card()
            w.start(cid, role, cwd=self.wt)
            deny = self.deny(cid, role)
            self.assertIn(f"Edit(/{self.data}/profiles/**)", deny, role)
            self.assertIn(f"Edit(/{self.data}/active-profile.json)", deny, role)
