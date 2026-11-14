"""The dispatcher pass (design 8.0, section 12 "Dispatcher pass"): repo groups, the pass's floor, the gate starting
one pass and never waiting, and a later run ending it. Every session is tests/bin/fake-claude-v2; none is real."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT
from tests.test_ap2_gate import FAKE, LoopCase
from assistant import cards, dispatch, gate, profiles, routines, spend, workers
from assistant.importv1 import FEED as V1_FEED


def project_file(data: Path, name: str, repo=None, tools=()) -> None:
    (data / "projects").mkdir(exist_ok=True)
    lines = [f"repo: {repo}"] if repo else []
    if tools:
        lines.append("tools:")
        lines += [f"  - {t}" for t in tools]
    (data / "projects" / f"{name}.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


class DispatchCase(LoopCase):
    dispatch = True

    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        # The group None's own folder (`tempfile.mkdtemp`) lands in self.tmp, which the test removes.
        tmp = mock.patch.object(tempfile, "tempdir", str(self.tmp))
        tmp.start()
        self.addCleanup(tmp.stop)
        # In-process calls run as a person at a terminal, even inside a v2 worker (which exports AP2_ROLE); a test
        # that needs either name sets it itself.
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in ("AP2_ROLE", "AP2_PASS"):
            os.environ.pop(name, None)

    def own_folder(self, cwd: str) -> Path:
        """`cwd` is a group None pass's own folder: new, empty, in the temp folder, outside the data folder."""
        path = Path(cwd)
        self.assertEqual(path.parent, self.tmp)
        self.assertTrue(path.name.startswith(dispatch.OWN_PREFIX), path)
        self.assertNotEqual(path, self.data)
        self.assertFalse(path.is_relative_to(self.data))
        return path

    def record(self) -> dict:
        return json.loads((self.data / dispatch.DISPATCH).read_text())

    def pass_file(self) -> dict:
        return json.loads((self.data / "pass.json").read_text())

    def dispatch_calls(self) -> list:
        return [c for c in self.bg_calls() if c["argv"][c["argv"].index("-n") + 1].startswith(dispatch.NAME_PREFIX)]


# -- criterion 1: repo groups ---------------------------------------------------------------------------------------

class GroupsTest(DispatchCase):
    def test_two_projects_on_one_repo_share_a_group_and_a_card_with_no_project_lands_in_none(self):
        other = self.tmp / "other"
        other.mkdir()
        project_file(self.data, "shop", self.repo)
        project_file(self.data, "shop-web", self.repo)
        project_file(self.data, "blog", other)
        self.add_card(project="shop")
        self.add_card(project="shop-web")
        self.add_card(project="blog")
        self.add_card()
        got, errors = dispatch.groups(self.data, self.store())
        self.assertEqual(errors, [])
        self.assertEqual(got, sorted([{"group": str(self.repo), "projects": ["shop", "shop-web"]},
                                      {"group": str(other), "projects": ["blog"]}],
                                     key=lambda g: g["group"]) + [{"group": None, "projects": []}])

    def test_done_cards_and_v1_copies_are_not_live(self):
        project_file(self.data, "shop", self.repo)
        self.add_card(project="shop", lane="done")
        self.add_card(feed=V1_FEED)
        self.assertEqual(dispatch.groups(self.data, self.store()), ([], []))

    def test_a_taken_over_v1_card_is_live_and_a_plain_v1_copy_is_not(self):
        other = self.tmp / "other"
        other.mkdir()
        project_file(self.data, "moved", self.repo)
        project_file(self.data, "still-v1", other)
        self.add_card(project="moved", feed=V1_FEED, taken_over="2026-11-10T12:00:00Z")
        self.add_card(project="still-v1", feed=V1_FEED)
        self.assertEqual(dispatch.groups(self.data, self.store()),
                         ([{"group": str(self.repo), "projects": ["moved"]}], []))

    def test_a_project_with_no_repo_joins_the_none_group(self):
        project_file(self.data, "notes")
        self.add_card(project="notes")
        self.assertEqual(dispatch.groups(self.data, self.store())[0], [{"group": None, "projects": ["notes"]}])

    def test_a_broken_project_is_one_error_and_the_others_still_group(self):
        project_file(self.data, "shop", self.repo)
        (self.data / "projects" / "broken.yaml").write_text("nope: 1\n", encoding="utf-8")
        self.add_card(project="shop")
        self.add_card(project="broken")
        self.add_card(project="broken")
        got, errors = dispatch.groups(self.data, self.store())
        self.assertEqual(got, [{"group": str(self.repo), "projects": ["shop"]}])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["project"], "broken")
        self.assertIn("unknown key 'nope'", errors[0]["error"])

    def test_a_project_name_that_is_not_a_name_is_an_error_not_a_crash(self):
        self.add_card(project=["shop"])
        self.add_card(project="Bad Name")
        got, errors = dispatch.groups(self.data, self.store())
        self.assertEqual(got, [])
        self.assertEqual(len(errors), 2)


# -- criterion 4: the pass's floor ------------------------------------------------------------------------------------

class FloorTest(DispatchCase):
    TOOLS = ("mcp__slack__slack_send_message", "mcp__slack__slack_read_channel", "mcp__jira__transitionJiraIssue",
             "mcp__jira__getJiraIssue")

    def floor(self, mode, profile=None) -> dict:
        project_file(self.data, "shop", self.repo, tools=self.TOOLS)
        from assistant import projects
        return dispatch.settings(self.data, profile or profiles.builtin(), [projects.load(self.data, "shop")], mode)

    def test_every_mode_denies_v1s_dispatcher_bash_rules_and_the_protected_files(self):
        v1 = json.loads((ROOT / "settings" / "dispatcher.json").read_text())["permissions"]["deny"]
        v1_bash = [r for r in v1 if r.startswith("Bash(")]
        for mode in ("shadow", "live"):
            with self.subTest(mode=mode):
                s = self.floor(mode)
                deny = s["permissions"]["deny"]
                for rule in v1_bash:
                    self.assertIn(rule, deny)
                for name in ("config.yaml", "workers.json", "gate.json", "pass.json", "dispatch.json",
                             "dispatch-end.json", "sessions/**/settings-*.json", "projects/**"):
                    self.assertIn(f"Edit(/{self.data}/{name})", deny)
                self.assertIn(f"Edit(/{workers.package_root()}/**)", deny)
                for name in (gate.PASS, dispatch.DISPATCH, dispatch.END):  # every worker's floor denies them too
                    self.assertIn(name, workers.PROTECTED)
                self.assertEqual(s["env"]["AP2_ROLE"], "dispatcher")
                self.assertEqual(s["env"]["AP2_DATA"], str(self.data))
                self.assertEqual(s["env"]["AP2_RUN_DIR"], str(self.data / "sessions" / "dispatch"))
                hooks = [h["command"] for m in s["hooks"]["PreToolUse"] for h in m["hooks"]]
                ap2 = workers.package_root() / "bin" / "ap2"
                self.assertEqual(hooks, [f"\"{ap2}\" hook-park", f"\"{ap2}\" hook-guard"])
                self.assertIn(f"Bash({ap2} pass end:*)", s["permissions"]["allow"])

    def test_the_allow_rules_are_exactly_the_runbooks_steps(self):
        ap2 = workers.package_root() / "bin" / "ap2"
        allow = self.floor("shadow")["permissions"]["allow"]
        self.assertEqual(sorted(allow), sorted(
            [f"Bash({ap2} {v}:*)" for v in ("pass status", "pass end", "card list", "card show", "card ask",
                                             "card lane", "worker list", "worker start", "template show", "search")]
            + [f"Bash({ap2} cap)", f"Bash({ap2} cap --json)"]))

        def allowed(command: str) -> list:
            hits = []
            for rule in allow:
                inner = rule[len("Bash("):-1]
                if inner.endswith(":*") and command.startswith(inner[:-2]) or command == inner:
                    hits.append(rule)
            return hits

        for words in ("cap answer raise100", "cap --json answer raise100", "cap resume", "worker answer C implement",
                      "card add f.json", "worker end C implement", "worker park C implement", "worker finish C"):
            with self.subTest(command=words):
                self.assertEqual(allowed(f"{ap2} {words}"), [])
        for words in ("pass end", "cap --json", "worker start C implement --cwd /x --prompt-file /y"):
            self.assertTrue(allowed(f"{ap2} {words}"), words)

    def test_shadow_mode_also_denies_the_outward_writes(self):
        deny = self.floor("shadow")["permissions"]["deny"]
        self.assertEqual(dispatch.SHADOW_DENY, ("Bash(git:*)", "Bash(gh:*)"))
        for rule in ("Bash(git:*)", "Bash(gh:*)", "mcp__slack__slack_send_message", "mcp__jira__transitionJiraIssue"):
            self.assertIn(rule, deny)
        for tool in ("mcp__slack__slack_read_channel", "mcp__jira__getJiraIssue"):
            self.assertNotIn(tool, deny)

    def test_git_and_gh_are_denied_whole_however_an_option_comes_before_the_verb(self):
        """Claude Code's `:*` rule matches the command itself or the command and a space; a rule per verb missed
        each of these."""
        deny = [r[len("Bash("):-len(":*)")] for r in self.floor("shadow")["permissions"]["deny"]
                if r.startswith("Bash(") and r.endswith(":*)")]
        for command in ("git --git-dir=/r/.git push", "git -P push", "git --no-pager push", "git push",
                        "gh -R o/r pr merge 5", "gh --repo=o/r pr ready 5", "gh pr create", "gh api x", "git", "gh"):
            with self.subTest(command=command):
                self.assertTrue(any(command == p or command.startswith(p + " ") for p in deny), command)

    def test_live_mode_allows_the_outward_writes(self):
        deny = self.floor("live")["permissions"]["deny"]
        for rule in dispatch.SHADOW_DENY + ("mcp__jira__transitionJiraIssue",):  # live moves the ticket
            self.assertNotIn(rule, deny)
        self.assertIn("mcp__slack__slack_send_message", deny)  # v1's dispatcher floor denies it in every mode
        for tool in ("mcp__slack__slack_read_channel", "mcp__jira__getJiraIssue"):
            self.assertNotIn(tool, deny)
        for rule in ("Bash(git -C:*)", "Bash(git -c:*)", "Bash(git --git-dir:*)", "Bash(git --work-tree:*)",
                     "Bash(gh api:*)"):
            self.assertNotIn(rule, deny)
        self.assertEqual(sorted(deny), sorted(set(deny)))

    def test_every_mode_denies_a_nested_session_as_the_read_only_floor_does(self):
        """`claude -p` would run under the user's own settings and MCP servers, and v1's `ap oneoff` runs it."""
        pkg = workers.package_root()
        for mode in ("shadow", "live"):
            with self.subTest(mode=mode):
                deny = self.floor(mode)["permissions"]["deny"]
                for rule in workers.nested_session_denies():
                    self.assertIn(rule, deny)
                for rule in ("Bash(claude:*)", "Bash(ap oneoff:*)", f"Bash({pkg / 'bin' / 'ap'} oneoff:*)"):
                    self.assertIn(rule, deny)
                self.assertNotIn("Bash(ap:*)", deny)  # it would prefix every `ap2` command
                self.assertEqual(len(deny), len(set(deny)))
        fake = dispatch.settings(self.data, profiles.builtin(), [], "live", claude=str(FAKE))["permissions"]["deny"]
        self.assertIn(f"Bash({FAKE}:*)", fake)  # the claude the pass is started with, as typed

    def test_every_mode_denies_the_outward_built_in_tools_and_keeps_the_runbooks(self):
        self.assertEqual(set(dispatch.BUILTIN_DENY), {
            "RemoteTrigger", "CronCreate", "CronDelete", "ScheduleWakeup", "ArtifactComments", "Artifact",
            "ArtifactData", "PushNotification", "SendMessage", "Workflow", "PublishPlugin", "ShareOnboardingGuide",
            "DesignSync"})
        for mode in ("shadow", "live"):
            with self.subTest(mode=mode):
                deny = self.floor(mode)["permissions"]["deny"]
                for tool in dispatch.BUILTIN_DENY:
                    self.assertIn(tool, deny)
                for tool in ("Read", "Write", "Bash", "Glob", "Grep", "Edit", "ToolSearch", "Task", "Agent"):
                    self.assertNotIn(tool, deny)

    def test_the_profile_overlay_is_merged_under_the_floor(self):
        prof = dict(profiles.builtin(), name="mine", settings={"cleanupPeriodDays": 9, "env": {"AP2_ROLE": "x",
                                                                                              "FOO": "1"}})
        s = self.floor("shadow", prof)
        self.assertEqual(s["cleanupPeriodDays"], 9)
        self.assertEqual(s["env"]["FOO"], "1")
        self.assertEqual(s["env"]["AP2_ROLE"], "dispatcher")  # the floor's AP2_* wins
        self.assertEqual(s["env"]["AP2_PROFILE"], "mine")
        self.assertEqual(s["permissions"]["deny"], self.floor("shadow")["permissions"]["deny"])

    def test_the_spend_launch_row_is_kind_dispatcher(self):
        self.add_card()
        self.gate(self.T)
        rows = [json.loads(line) for line in (self.data / spend.FOLDER / spend.LAUNCHES).read_text().splitlines()]
        self.assertEqual([(r["kind"], r["short"]) for r in rows], [("dispatcher", "s0001")])


# -- criterion 2: the gate starts one pass and never waits ---------------------------------------------------------

class StartTest(DispatchCase):
    def test_a_due_pass_starts_one_session_and_writes_dispatch_json(self):
        project_file(self.data, "shop", self.repo)
        cid = self.add_card(project="shop")
        out = self.gate(self.T)
        self.assertIs(out["pass_due"], True)
        [call] = self.dispatch_calls()
        argv = call["argv"]
        self.assertEqual(argv[:3], ["--bg", "-n", "ap2 dispatch · shop"])
        self.assertEqual(argv[argv.index("--settings") + 1], str(self.data / "sessions" / "dispatch" /
                                                                 "settings-dispatcher.json"))
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "auto")
        self.assertNotIn("--model", argv)  # the subscription: the default model
        self.assertIn("--strict-mcp-config", argv[:-1])  # shadow mode: no MCP server; the prompt stays last
        self.assertNotIn("--mcp-config", argv)
        self.assertIn(str(dispatch.RUNBOOK), argv[-1])
        self.assertIn("shop", argv[-1])
        ap2 = workers.package_root() / "bin" / "ap2"
        self.assertTrue(ap2.is_file())
        self.assertIn(f"Run ap2 as {ap2} (that exact path", argv[-1])
        self.assertEqual(call["cwd"], str(self.repo))
        self.assertEqual(call["env"]["AP2_ROLE"], "dispatcher")
        rec = self.record()
        self.assertEqual({k: rec[k] for k in ("group", "projects", "short_id", "started_at", "profile", "queue",
                                              "why")},
                         {"group": str(self.repo), "projects": ["shop"], "short_id": "s0001",
                          "started_at": "2026-11-10T14:00:00Z", "profile": "subscription", "queue": [],
                          "why": [{"why": "ready", "card": cid}]})
        self.assertRegex(rec["token"], r"\A[0-9a-f]{16}\Z")
        self.assertEqual(call["env"]["AP2_PASS"], rec["token"])  # the pass's own token, in its launch env
        settings_file = json.loads((self.data / "sessions" / "dispatch" / "settings-dispatcher.json").read_text())
        self.assertNotIn("AP2_PASS", settings_file["env"])  # never in the shared settings file
        self.assertTrue(rec["session_id"])
        self.assertEqual(out["dispatch"], [{"did": "started", "short_id": "s0001", "group": str(self.repo),
                                            "projects": ["shop"]}])
        self.assertEqual(out["errors"], [])
        # pass_started ran: what was there is seen, so the pass is not due any more.
        self.assertIs(self.pass_file()["due"], False)
        self.assertEqual(self.pass_file()["seen"]["ready"], [cid])
        self.assertIn("dispatch: started s0001 for shop", self.gate_log())

    def test_a_second_run_while_the_pass_works_starts_nothing(self):
        self.add_card()
        self.gate(self.T)
        self.add_card()  # a new ready card: the next round is due, but one pass is open
        out = self.gate(self.T + timedelta(minutes=1))
        self.assertIs(out["pass_due"], True)
        self.assertEqual(len(self.dispatch_calls()), 1)
        self.assertEqual(out["dispatch"], [])
        self.assertEqual(self.record()["short_id"], "s0001")

    def test_a_card_with_no_project_runs_its_pass_in_an_empty_folder_of_its_own(self):
        self.add_card()
        self.gate(self.T)
        [call] = self.dispatch_calls()
        folder = self.own_folder(call["cwd"])
        self.assertTrue(folder.is_dir())
        self.assertEqual(list(folder.iterdir()), [])
        self.assertEqual(self.record()["cwd"], str(folder))
        self.assertEqual(call["argv"][2], "ap2 dispatch · " + dispatch.NO_PROJECT)
        self.assertNotIn("in the data folder", call["argv"][-1])
        self.assertIn(f"The data folder is {self.data}.", call["argv"][-1])
        self.assertEqual(call["env"]["AP2_DATA"], str(self.data))

    def test_a_pass_in_a_repo_has_no_folder_of_its_own(self):
        project_file(self.data, "shop", self.repo)
        self.add_card(project="shop")
        self.gate(self.T)
        self.assertNotIn("cwd", self.record())

    def test_a_launch_that_fails_removes_the_empty_folder_it_made(self):
        self.add_card()
        with mock.patch.dict(os.environ, {"FAKE_BG_FAIL": "1"}):
            self.gate(self.T)
        self.assertEqual([p for p in self.tmp.iterdir() if p.name.startswith(dispatch.OWN_PREFIX)], [])

    def test_the_profile_model_goes_on_the_start(self):
        prof = dict(profiles.builtin(), dispatcher_model="claude-x", model="claude-y")
        group = {"group": None, "projects": []}
        dispatch.start(self.data, group, [], prof, self.T, "shadow", claude=FAKE)
        argv = self.dispatch_calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "claude-x")
        (self.data / dispatch.DISPATCH).unlink()
        dispatch.start(self.data, group, [], dict(prof, dispatcher_model=None), self.T, "shadow", claude=FAKE)
        argv = self.dispatch_calls()[1]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "claude-y")

    def test_live_mode_loads_the_mcp_servers_and_each_pass_gets_its_own_token(self):
        group = {"group": None, "projects": []}
        first = dispatch.start(self.data, group, [], profiles.builtin(), self.T, "live", claude=FAKE)
        (self.data / dispatch.DISPATCH).unlink()
        second = dispatch.start(self.data, group, [], profiles.builtin(), self.T, "shadow", claude=FAKE)
        live, shadow = (c["argv"] for c in self.dispatch_calls())
        self.assertNotIn("--strict-mcp-config", live)
        self.assertEqual(shadow[-2], "--strict-mcp-config")
        self.assertNotEqual(first["token"], second["token"])

    def test_nothing_due_starts_nothing(self):
        self.add_card()
        self.gate(self.T)
        self.set_agent("s0001", state="done", status="idle")
        dispatch.end(self.data, self.T)
        out = self.gate(self.T + timedelta(minutes=1))
        self.assertEqual([e["did"] for e in out["dispatch"]], ["ended"])
        self.assertIs(out["pass_due"], False)
        self.assertEqual(len(self.dispatch_calls()), 1)
        self.assertFalse((self.data / dispatch.DISPATCH).exists())

    def test_a_due_pass_with_nothing_live_to_serve_starts_nothing_and_is_seen(self):
        cid = self.add_card(lane="done")
        self.run_file(cid, "handback.json")  # a result file of a done card: a pass is due, but no group is live
        out = self.gate(self.T)
        self.assertIs(out["pass_due"], True)
        self.assertEqual((out["dispatch"], out["errors"], self.dispatch_calls()), ([], [], []))
        self.assertIs(self.pass_file()["due"], False)  # seen, so the next run is not due again
        self.assertIs(self.gate(self.T + timedelta(minutes=1))["pass_due"], False)

    def test_a_failing_pass_started_with_nothing_to_serve_is_one_error_and_gate_json_is_written(self):
        cid = self.add_card(lane="done")
        self.run_file(cid, "handback.json")
        with mock.patch.object(gate, "pass_started", side_effect=OSError("disk full")):
            out = self.gate(self.T)
        self.assertEqual(out["errors"], [{"pass": True, "error": "disk full"}])
        self.assertEqual(json.loads((self.data / "gate.json").read_text()), out)
        self.assertIn("pass: error: disk full", self.gate_log())

    def test_a_broken_project_keeps_the_pass_due_and_starts_nothing(self):
        (self.data / "projects").mkdir()
        (self.data / "projects" / "broken.yaml").write_text("nope: 1\n", encoding="utf-8")
        cid = self.add_card(project="broken")
        out = self.gate(self.T)
        self.assertIs(out["pass_due"], True)
        self.assertEqual([e.get("project") for e in out["errors"]], ["broken"])
        self.assertIn("project broken: refused:", self.gate_log())
        self.assertNotIn(cid, self.pass_file()["seen"]["ready"])  # left unseen: the next run is due for it again
        out = self.gate(self.T + timedelta(minutes=1))
        self.assertIs(out["pass_due"], True)
        self.assertEqual([e.get("project") for e in out["errors"]], ["broken"])
        self.assertEqual(self.dispatch_calls(), [])

    def test_a_launch_that_fails_starts_nothing_and_the_pass_stays_due(self):
        self.add_card()
        with mock.patch.dict(os.environ, {"FAKE_BG_FAIL": "1"}):
            out = self.gate(self.T)
        [err] = out["errors"]
        self.assertIs(err["dispatch"], True)
        self.assertIn("cannot start the dispatcher pass", err["error"])
        retry = cards.stamp(self.T + timedelta(minutes=dispatch.LAUNCH_RETRY_MIN))
        self.assertTrue(err["error"].endswith(f"; next try at {retry}"), err)
        self.assertIn(f"no other group is tried this run; next try at {retry}", self.gate_log())
        # A new round: a queue-only record of its own, with nothing queued and the backoff.
        self.assertEqual(self.record(), {"queue": [], "why": self.record()["why"], "retry_at": retry})
        self.assertIs(self.pass_file()["due"], True)
        self.assertEqual(out["dispatch"], [])
        out = self.gate(self.T + timedelta(minutes=dispatch.LAUNCH_RETRY_MIN))  # the run at retry_at tries again
        self.assertEqual(out["dispatch"][0]["did"], "started")
        self.assertNotIn("retry_at", self.record())

    def test_a_launch_that_keeps_failing_is_tried_once_per_backoff_not_once_per_run(self):
        """A `claude --bg` that hangs costs its 120-second timeout (under the gate lock) once per LAUNCH_RETRY_MIN
        minutes: the runs in between start nothing and make no launch call."""
        self.add_card()
        hang = subprocess.TimeoutExpired("claude", 120)
        with mock.patch.object(dispatch.v1_workers, "_launch", side_effect=hang) as launch:
            self.gate(self.T)
            self.assertEqual(launch.call_count, 1)
            for minute in range(1, dispatch.LAUNCH_RETRY_MIN):
                out = self.gate(self.T + timedelta(minutes=minute))
                self.assertEqual((launch.call_count, out["dispatch"]), (1, []), minute)
                self.assertEqual([e for e in out["errors"] if e.get("dispatch")], [], minute)
                self.assertIs(out["pass_due"], True)
            self.gate(self.T + timedelta(minutes=dispatch.LAUNCH_RETRY_MIN))
            self.assertEqual(launch.call_count, 2)
        again = self.T + timedelta(minutes=2 * dispatch.LAUNCH_RETRY_MIN)
        self.assertEqual(self.record()["retry_at"], cards.stamp(again))

    def test_a_retry_at_beyond_the_backoff_means_the_clock_went_back_and_is_ignored(self):
        self.add_card()
        far = cards.stamp(self.T + timedelta(minutes=dispatch.LAUNCH_RETRY_MIN, seconds=1))
        (self.data / dispatch.DISPATCH).write_text(json.dumps({"queue": [], "why": [], "retry_at": far}))
        out = self.gate(self.T)
        self.assertEqual([e["did"] for e in out["dispatch"]], ["started"])

    def test_a_record_with_retry_at_is_no_open_pass_and_a_bad_one_does_not_read(self):
        retry = cards.stamp(self.T + timedelta(minutes=5))
        (self.data / dispatch.DISPATCH).write_text(json.dumps({"queue": [], "why": [], "retry_at": retry}))
        self.assertEqual(dispatch.read_record(self.data)["retry_at"], retry)
        self.assertIsNone(dispatch.finish(self.data, self.T, claude=FAKE))
        with self.assertRaisesRegex(dispatch.DispatchError, "no dispatcher pass is open"):
            dispatch.end(self.data, self.T)
        for bad in (7, "soon"):
            with self.subTest(bad=bad):
                (self.data / dispatch.DISPATCH).write_text(json.dumps({"queue": [], "retry_at": bad}))
                with self.assertRaises(ValueError):
                    dispatch.read_record(self.data)

    def test_a_group_that_fails_for_another_reason_does_not_back_off(self):
        project_file(self.data, "shop", self.tmp / "gone")
        self.add_card(project="shop")
        out = self.gate(self.T)
        self.assertIn("is not a folder", [e for e in out["errors"] if e.get("dispatch")][0]["error"])
        self.assertFalse((self.data / dispatch.DISPATCH).exists())
        (self.tmp / "gone").mkdir()
        out = self.gate(self.T + timedelta(minutes=1))  # the next round tries it again at once
        self.assertEqual([e["did"] for e in out["dispatch"]], ["started"])

    def test_a_profile_that_does_not_load_starts_nothing(self):
        self.add_card()
        (self.data / profiles.ACTIVE).write_text("not json", encoding="utf-8")
        out = self.gate(self.T)
        errs = [e for e in out["errors"] if e.get("dispatch")]
        self.assertEqual(len(errs), 1)
        self.assertIn("cannot read", errs[0]["error"])
        self.assertEqual(self.dispatch_calls(), [])
        self.assertIs(self.pass_file()["due"], True)

    def test_a_paused_cap_starts_nothing(self):
        self.add_card()
        with mock.patch("assistant.cap.allowance", return_value={"allow": 0, "reason": "paused", "paused": {}}):
            out = self.gate(self.T)
        [err] = out["errors"]
        self.assertTrue(err["error"].startswith("no dispatcher pass started: "), err)
        self.assertEqual(self.dispatch_calls(), [])
        self.assertIs(self.pass_file()["due"], True)

    def test_shadow_mode_runs_the_pass_and_starts_only_the_fake(self):
        self.assertEqual(self.store().cfg.mode, "shadow")
        self.add_card()
        self.gate(self.T)
        self.assertEqual(len(self.dispatch_calls()), 1)
        for argv in self.argvs():
            self.assertIn(argv[0], ("agents", "--bg"), argv)


class RoundTest(DispatchCase):
    def setUp(self):
        super().setUp()
        project_file(self.data, "shop", self.repo)
        self.shop = self.add_card(project="shop")
        self.loose = self.add_card()

    def failing_in(self, folder: Path):
        """`_launch` failing with a RuntimeError in `folder` only (as `claude --bg` printing no short id in one
        repo), and the fake claude everywhere else."""
        real = dispatch.v1_workers._launch

        def launch(argv, cwd, env):
            if cwd == str(folder):
                raise RuntimeError("claude --bg printed no short id")
            return real(argv, cwd, env)

        return mock.patch.object(dispatch.v1_workers, "_launch", side_effect=launch)

    def test_the_groups_of_a_round_go_one_after_another_and_seen_moves_once(self):
        with mock.patch.object(gate, "pass_started", wraps=gate.pass_started) as started:
            first = self.gate(self.T)
            self.assertEqual(self.record()["queue"], [{"group": None, "projects": []}])
            self.add_card(project="shop")  # lands while the round runs: the next round is due
            self.gate(self.T + timedelta(minutes=1))
            self.assertEqual(len(self.dispatch_calls()), 1)  # the open pass is not done yet
            dispatch.end(self.data, self.T + timedelta(minutes=1))
            second = self.gate(self.T + timedelta(minutes=2))
            self.assertEqual(started.call_count, 1)
        self.assertEqual(first["dispatch"][0]["group"], str(self.repo))
        self.assertEqual([e["did"] for e in second["dispatch"]], ["ended", "started"])
        self.assertIsNone(second["dispatch"][1]["group"])
        self.own_folder(self.dispatch_calls()[1]["cwd"])
        self.assertEqual(self.record()["queue"], [])
        self.assertIs(self.pass_file()["due"], True)  # the card that landed during the round
        self.set_agent("s0002", state="done", status="idle")
        dispatch.end(self.data, self.T + timedelta(minutes=3))
        third = self.gate(self.T + timedelta(minutes=3))
        self.assertEqual([e["did"] for e in third["dispatch"]], ["ended", "started"])  # the next round
        self.assertEqual(third["dispatch"][1]["group"], str(self.repo))

    def test_every_group_of_the_round_reads_the_rounds_why(self):
        """pass_started empties pass.json's `why` as the round starts; each pass reads its record's instead."""
        self.gate(self.T)
        why = self.record()["why"]
        self.assertEqual(sorted(w["why"] for w in why), ["ready", "ready"])
        self.assertEqual(self.pass_file()["why"], [])
        dispatch.end(self.data, self.T + timedelta(minutes=1))
        self.gate(self.T + timedelta(minutes=1))
        rec = self.record()
        self.assertEqual((rec["short_id"], rec["group"], rec["why"]), ("s0002", None, why))  # `ap2 pass status`

    def test_a_record_without_why_or_token_still_reads(self):
        (self.data / dispatch.DISPATCH).write_text(json.dumps({"queue": [{"group": None, "projects": []}]}))
        self.assertEqual(dispatch.read_record(self.data), {"queue": [{"group": None, "projects": []}]})
        for bad in ({"why": "x"}, {"why": ["x"]}, {"token": 7}):
            with self.subTest(bad=bad):
                (self.data / dispatch.DISPATCH).write_text(json.dumps(dict({"queue": []}, **bad)))
                with self.assertRaises(ValueError):
                    dispatch.read_record(self.data)

    def test_a_launch_that_fails_is_tried_once_per_run_and_its_group_is_dropped_from_the_round(self):
        with mock.patch.object(dispatch.v1_workers, "_launch",
                               side_effect=subprocess.TimeoutExpired("claude", 120)) as launch:
            out = self.gate(self.T)
        self.assertEqual(launch.call_count, 1)
        [err] = [e for e in out["errors"] if e.get("dispatch")]
        self.assertIn("cannot start the dispatcher pass for shop", err["error"])
        self.assertIn("shop is dropped from this round and tried again next round; no other group is tried this run",
                      self.gate_log())
        # The rest of the round waits for the next run; the failed group's card is left unseen.
        rec = self.record()
        self.assertEqual(sorted(rec), ["queue", "retry_at", "why"])
        self.assertEqual(rec["retry_at"], cards.stamp(self.T + timedelta(minutes=dispatch.LAUNCH_RETRY_MIN)))
        self.assertEqual(rec["queue"], [{"group": None, "projects": []}])
        self.assertEqual(sorted(w["card"] for w in rec["why"]), sorted([self.shop, self.loose]))
        self.assertEqual(self.pass_file()["seen"]["ready"], [self.loose])

    def test_a_group_whose_launch_fails_at_the_head_of_a_new_round_never_holds_the_round(self):
        back = dispatch.LAUNCH_RETRY_MIN  # each failed launch: no pass starts for this many minutes
        with self.failing_in(self.repo):
            out = self.gate(self.T)
            self.assertEqual(out["dispatch"], [])
            self.assertIn("claude --bg printed no short id", [e for e in out["errors"] if e.get("dispatch")][0]["error"])
            out = self.gate(self.T + timedelta(minutes=back))  # the next group, not the failed one again
            self.assertEqual([(e["did"], e["group"]) for e in out["dispatch"]], [("started", None)])
            self.assertEqual([e for e in out["errors"] if e.get("dispatch")], [])
            self.assertEqual(self.record()["queue"], [])
            dispatch.end(self.data, self.T + timedelta(minutes=back))
            out = self.gate(self.T + timedelta(minutes=back + 1))  # the next round is due for the failed group's card
        self.assertIs(out["pass_due"], True)
        self.assertEqual(json.loads((self.data / "gate.json").read_text())["pass_due"], True)
        self.assertEqual([e["did"] for e in out["dispatch"]], ["ended"])
        self.assertEqual(len([e for e in out["errors"] if e.get("dispatch")]), 1)  # one error this round
        self.assertEqual(self.pass_file()["seen"]["ready"], [self.loose])
        # The round names only the failed group's card: the group None is not served again, so nothing is queued.
        self.assertEqual(self.record()["queue"], [])
        self.assertEqual(self.record()["retry_at"], cards.stamp(self.T + timedelta(minutes=2 * back + 1)))
        self.assertEqual(len(self.dispatch_calls()), 1)
        out = self.gate(self.T + timedelta(minutes=2 * back + 1))  # healed: the next round serves it, and only it
        self.assertEqual([(e["did"], e["group"]) for e in out["dispatch"]], [("started", str(self.repo))])
        self.assertEqual(self.record()["why"], [{"why": "ready", "card": self.shop}])
        self.assertEqual(self.record()["queue"], [])

    def test_a_group_whose_launch_fails_inside_a_running_round_is_dropped_and_left_unseen(self):
        first = self.tmp / "a-repo"
        first.mkdir()
        project_file(self.data, "alpha", first)
        alpha = self.add_card(project="alpha")
        self.gate(self.T)
        self.assertEqual(self.record()["group"], str(first))
        why = self.record()["why"]
        self.assertEqual(sorted(self.pass_file()["seen"]["ready"]), sorted([alpha, self.shop, self.loose]))
        dispatch.end(self.data, self.T + timedelta(minutes=1))
        with self.failing_in(self.repo):
            out = self.gate(self.T + timedelta(minutes=1))
        self.assertEqual([e["did"] for e in out["dispatch"]], ["ended"])
        retry = self.T + timedelta(minutes=1 + dispatch.LAUNCH_RETRY_MIN)
        self.assertEqual(self.record(), {"queue": [{"group": None, "projects": []}], "why": why,
                                         "retry_at": cards.stamp(retry)})
        self.assertNotIn(self.shop, self.pass_file()["seen"]["ready"])
        out = self.gate(retry)  # the next group, not the failed one
        self.assertEqual([(e["did"], e["group"]) for e in out["dispatch"]], [("started", None)])
        dispatch.end(self.data, retry)
        out = self.gate(retry + timedelta(minutes=1))  # a new round, due for the failed group's card
        self.assertEqual([e["did"] for e in out["dispatch"]], ["ended", "started"])
        self.assertEqual(self.record()["why"], [{"why": "ready", "card": self.shop}])

    def test_a_queued_group_whose_named_cards_went_starts_no_pass(self):
        """The running round's queue is checked again: the group None's only named card moved to done while the
        first pass ran, so no pass starts for it and the round is over."""
        self.gate(self.T)
        self.assertEqual(self.record()["queue"], [{"group": None, "projects": []}])
        self.store().update(self.loose, {"lane": "done"}, by="test")
        dispatch.end(self.data, self.T + timedelta(minutes=1))
        out = self.gate(self.T + timedelta(minutes=1))
        self.assertEqual([e["did"] for e in out["dispatch"]], ["ended"])
        self.assertEqual([e for e in out["errors"] if e.get("dispatch")], [])
        self.assertEqual(len(self.dispatch_calls()), 1)
        self.assertFalse((self.data / dispatch.DISPATCH).exists())
        self.assertIn("no group left in the round holds a card it names; the round is over", self.gate_log())

    def test_a_store_that_does_not_read_keeps_the_running_rounds_queue(self):
        self.gate(self.T)
        dispatch.end(self.data, self.T + timedelta(minutes=1))
        with mock.patch.object(dispatch, "named", side_effect=OSError("lock timeout")):
            out = self.gate(self.T + timedelta(minutes=1))
        self.assertEqual([e for e in out["errors"] if e.get("dispatch")], [{"dispatch": True, "error": "lock timeout"}])
        self.assertEqual([(e["did"], e["group"]) for e in out["dispatch"]], [("ended", str(self.repo)),
                                                                             ("started", None)])

    def test_a_refused_project_next_to_healthy_groups_costs_them_no_pass(self):
        """The refused project's card stays due, but a round serves only the groups its `why` names, and that card
        is in none: each healthy group starts once, and every later run is one error line and no pass."""
        (self.data / "projects" / "broken.yaml").write_text("nope: 1\n", encoding="utf-8")
        broken = self.add_card(project="broken")
        refused = []
        for minute in range(6):
            now = self.T + timedelta(minutes=minute)
            out = self.gate(now)
            refused.append(len([e for e in out["errors"] if e.get("project") == "broken"]))
            if any(e["did"] == "started" for e in out["dispatch"]):
                dispatch.end(self.data, now)
        calls = self.dispatch_calls()
        self.assertEqual(len(calls), 2)  # the first round: shop, then the group None; never again
        self.assertEqual(calls[0]["cwd"], str(self.repo))
        self.own_folder(calls[1]["cwd"])
        self.assertEqual(refused, [1, 0, 1, 1, 1, 1])  # the error repeats on each run of a new round
        self.assertEqual(self.gate_log().count("project broken: refused:"), 5)
        self.assertIs(out["pass_due"], True)  # still due until the project is fixed
        self.assertNotIn(broken, self.pass_file()["seen"]["ready"])
        self.assertFalse((self.data / dispatch.DISPATCH).exists())
        project_file(self.data, "broken", self.repo)  # fixed: the next round serves its group
        out = self.gate(self.T + timedelta(minutes=6))
        self.assertEqual([(e["did"], e["group"]) for e in out["dispatch"]], [("started", str(self.repo))])
        self.assertEqual(self.record()["why"], [{"why": "ready", "card": broken}])

    def test_a_store_that_does_not_read_after_a_failure_leaves_every_named_card_unseen(self):
        with self.failing_in(self.repo), mock.patch.object(dispatch, "members", side_effect=OSError("lock timeout")):
            out = self.gate(self.T)
        self.assertIn({"dispatch": True, "error": "lock timeout"}, out["errors"])
        self.assertEqual(self.pass_file()["seen"]["ready"], [])  # neither card is dropped as seen
        self.assertEqual(self.record()["queue"], [{"group": None, "projects": []}])

    def test_unsee_undoes_only_the_reasons_of_the_given_cards(self):
        seen = {"ready": ["a", "b"], "results": {"a/handback.json": 5, "b/handback.json": 6},
                "asks": {"a": "fix/dispatch//", "b": "fix/dispatch//"}}
        why = [{"why": "ready", "card": "a"}, {"why": "result", "card": "a", "file": "handback.json"},
               {"why": "answered", "card": "a"}, {"why": "ready", "card": "b"}, {"why": "ready", "card": ["x"]}]
        got = gate._unsee(seen, why, {"a"})
        self.assertEqual(got, {"ready": ["b"], "results": {"b/handback.json": 6},
                               "asks": {"a": gate.UNSEEN, "b": "fix/dispatch//"}})
        present = {"ready": [], "results": {}, "asks": {}}
        self.assertIn({"why": "answered", "card": "a"}, gate._why(present, got))  # still a reason
        self.assertEqual(seen["ready"], ["a", "b"])  # not changed in place

    def test_a_repo_the_launch_cannot_enter_is_this_groups_error_and_the_next_group_starts(self):
        """A folder that exists but cannot be entered fails the launch with an OSError naming it: that is this
        group's failure, not `claude --bg`'s, so it never holds the other groups (and every later round) back."""
        if os.geteuid() == 0:
            self.skipTest("root enters a folder with no permissions")
        first, second = self.tmp / "a-repo", self.tmp / "b-repo"
        first.mkdir()
        second.mkdir()
        project_file(self.data, "alpha", first)
        project_file(self.data, "beta", second)
        alpha = self.add_card(project="alpha")
        beta = self.add_card(project="beta")
        first.chmod(0)
        self.addCleanup(first.chmod, 0o755)
        out = self.gate(self.T)
        errs = [e for e in out["errors"] if e.get("dispatch")]
        self.assertEqual(len(errs), 1)
        self.assertIn("cannot enter the repo", errs[0]["error"])
        self.assertNotIn("no other group is tried this run", self.gate_log())
        [started] = out["dispatch"]
        self.assertEqual((started["did"], started["group"]), ("started", str(second)))
        self.assertEqual(self.dispatch_calls()[-1]["cwd"], str(second))
        self.assertEqual(self.record()["queue"], [{"group": str(self.repo), "projects": ["shop"]},
                                                  {"group": None, "projects": []}])
        self.assertIs(self.pass_file()["due"], False)
        self.assertNotIn(alpha, self.pass_file()["seen"]["ready"])  # left unseen: the next round tries it again
        self.assertIn(beta, self.pass_file()["seen"]["ready"])

    def test_a_repo_gone_between_the_check_and_the_launch_is_this_groups_error(self):
        err = FileNotFoundError(2, "No such file or directory", str(self.repo))
        with mock.patch.object(dispatch.v1_workers, "_launch", side_effect=err):
            with self.assertRaises(dispatch.DispatchError) as caught:
                dispatch.start(self.data, {"group": str(self.repo), "projects": ["shop"]}, [], profiles.builtin(),
                               self.T, "shadow", claude=str(FAKE))
        self.assertNotIsInstance(caught.exception, dispatch.LaunchError)
        self.assertIn("cannot enter the repo", str(caught.exception))
        # An OSError about anything else (the claude CLI itself) is still a launch failure.
        missing = FileNotFoundError(2, "No such file or directory", "/no/claude")
        with mock.patch.object(dispatch.v1_workers, "_launch", side_effect=missing):
            with self.assertRaises(dispatch.LaunchError):
                dispatch.start(self.data, {"group": str(self.repo), "projects": ["shop"]}, [], profiles.builtin(),
                               self.T, "shadow", claude=str(FAKE))

    def test_a_change_that_lands_while_the_round_starts_makes_the_next_round_due(self):
        """`seen` is the snapshot the round's `why` came from, not a second look after the launch: a result file
        written while `claude --bg` runs is in no round's `why` yet, so the next round is due with it."""
        cid = self.add_card(project="shop")
        real = dispatch.start

        def start(*a, **kw):
            rec = real(*a, **kw)
            self.run_file(cid, "handback.json")
            return rec

        with mock.patch.object(dispatch, "start", side_effect=start):
            self.gate(self.T)
        self.assertNotIn(f"{cid}/handback.json", self.pass_file()["seen"]["results"])
        self.assertEqual([w for w in self.record()["why"] if w["why"] == "result"], [])
        dispatch.end(self.data, self.T + timedelta(minutes=1))
        self.gate(self.T + timedelta(minutes=1))  # the second group of the first round
        dispatch.end(self.data, self.T + timedelta(minutes=2))
        out = self.gate(self.T + timedelta(minutes=2))
        self.assertIs(out["pass_due"], True)
        self.assertEqual([e["did"] for e in out["dispatch"]], ["ended", "started"])
        self.assertIn({"why": "result", "card": cid, "file": "handback.json"}, self.record()["why"])
        self.assertIn(f"{cid}/handback.json", self.pass_file()["seen"]["results"])

    def test_a_group_that_cannot_start_is_one_error_and_the_next_group_starts(self):
        self.repo.rmdir()
        out = self.gate(self.T)
        errs = [e for e in out["errors"] if e.get("dispatch")]
        self.assertEqual(len(errs), 1)
        self.assertIn("is not a folder", errs[0]["error"])
        self.assertEqual(out["dispatch"][0]["group"], None)
        self.assertEqual(self.record()["queue"], [])
        self.assertIs(self.pass_file()["due"], False)


# -- criterion 3: a later run ends the pass ---------------------------------------------------------------------------

class FinishTest(DispatchCase):
    def setUp(self):
        super().setUp()
        self.cid = self.add_card()
        self.gate(self.T)
        self.assertEqual(self.record()["short_id"], "s0001")

    def ends(self, out) -> list:
        return [e for e in out["dispatch"] if e["did"] != "started"]

    def test_pass_end_ends_the_session_at_once_with_no_error(self):
        dispatch.end(self.data, self.T + timedelta(minutes=1))
        out = self.gate(self.T + timedelta(minutes=1))
        self.assertIn(["stop", "s0001"], self.argvs())
        self.assertIn(["rm", "s0001"], self.argvs())
        self.assertFalse((self.data / dispatch.DISPATCH).exists())
        self.assertFalse((self.data / dispatch.END).exists())
        self.assertEqual(self.ends(out), [{"did": "ended", "short_id": "s0001", "group": None, "projects": []}])
        self.assertEqual(out["errors"], [])
        self.assertIn("dispatch: s0001 (cards with no project) ended (ended: it ran ap2 pass end)", self.gate_log())

    def test_the_end_removes_the_passs_own_folder_with_its_session(self):
        for minute, (how, agent) in enumerate((("ended", None), ("turn", {"state": "done", "status": "idle"}),
                                               ("gone", [])), start=1):
            with self.subTest(how=how):
                now = self.T + timedelta(minutes=2 * minute)
                if how != "ended":
                    self.add_card()
                    self.gate(now - timedelta(minutes=1))  # a new round: a new pass, a new folder
                folder = self.own_folder(self.record()["cwd"])
                self.assertTrue(folder.is_dir())
                if agent is None:
                    dispatch.end(self.data, now)
                elif agent == []:
                    (self.fake_home / "agents.json").write_text("[]")
                else:
                    self.set_agent(self.record()["short_id"], **agent)
                out = self.gate(now)
                self.assertEqual([e["did"] for e in self.ends(out)], [how])
                self.assertFalse(folder.exists())

    def test_a_folder_the_record_names_but_start_did_not_make_is_never_removed(self):
        other = self.tmp / "keep"
        other.mkdir()
        for cwd in (str(other), str(self.data), str(self.tmp / (dispatch.OWN_PREFIX + "x") / ".." / "keep")):
            with self.subTest(cwd=cwd):
                self.assertIsNone(dispatch._own_folder({"group": None, "cwd": cwd}))
        named = self.tmp / (dispatch.OWN_PREFIX + "x")
        self.assertIsNone(dispatch._own_folder({"group": str(self.repo), "cwd": str(named)}))
        self.assertEqual(dispatch._own_folder({"group": None, "cwd": str(named)}), named)

    def test_a_turn_that_ended_without_pass_end_is_stopped_removed_and_one_error(self):
        self.set_agent("s0001", state="done", status="idle")
        out = self.gate(self.T + timedelta(minutes=2))
        self.assertIn(["stop", "s0001"], self.argvs())
        self.assertIn(["rm", "s0001"], self.argvs())
        self.assertEqual([e["did"] for e in self.ends(out)], ["turn"])
        [err] = out["errors"]
        self.assertEqual(err, {"dispatch": True, "error": "the dispatcher pass s0001 (cards with no project) ended "
                                                          "without `ap2 pass end`; it was stopped and removed"})
        self.assertFalse((self.data / dispatch.DISPATCH).exists())

    def test_an_idle_pass_never_seen_busy_waits_three_runs(self):
        rec = self.record()
        (self.data / dispatch.DISPATCH).write_text(json.dumps(dict(rec, seen_busy=False)))
        self.set_agent("s0001", status="idle", state="working")
        for minute in (1, 2):
            out = self.gate(self.T + timedelta(minutes=minute))
            self.assertEqual(self.ends(out), [])
            self.assertEqual(self.record()["looks"], minute)
        out = self.gate(self.T + timedelta(minutes=3))
        self.assertEqual([e["did"] for e in self.ends(out)], ["turn"])

    def test_a_pass_at_a_permission_prompt_opens_one_fix_ask_and_keeps_its_session(self):
        self.set_agent("s0001", state="blocked", status="waiting")
        out = self.gate(self.T + timedelta(minutes=2))
        self.assertIn(["stop", "s0001"], self.argvs())
        self.assertNotIn(["rm", "s0001"], self.argvs())
        [end] = self.ends(out)
        self.assertEqual(end["did"], "blocked")
        card = self.store().get(end["card"])
        self.assertEqual((card["kind"], card["lane"]), ("dispatch", "needs"))
        self.assertEqual(card["ask"]["type"], "fix")
        self.assertEqual(card["ask"]["from"], "dispatch")
        self.assertIn("claude attach s0001", card["ask"]["text"])
        folder = self.own_folder(self.dispatch_calls()[0]["cwd"])
        self.assertFalse((self.data / dispatch.DISPATCH).exists())
        self.assertTrue(folder.is_dir())  # kept with the session, for `claude attach`
        self.assertEqual([c for c in self.store().list(kind="dispatch")], [card])

    def test_a_pass_still_working_past_30_minutes_is_stopped_with_one_error(self):
        out = self.gate(self.T + timedelta(minutes=29))
        self.assertEqual(self.ends(out), [])
        self.assertNotIn(["stop", "s0001"], self.argvs())
        out = self.gate(self.T + timedelta(minutes=dispatch.DISPATCH_MAX_MIN))
        self.assertIn(["stop", "s0001"], self.argvs())
        self.assertEqual(self.ends(out)[0]["did"], "late")
        [err] = [e for e in out["errors"] if e.get("dispatch")]
        self.assertIn("still working after 30 minutes", err["error"])

    def test_a_pass_that_is_gone_is_one_error(self):
        (self.fake_home / "agents.json").write_text("[]")
        out = self.gate(self.T + timedelta(minutes=1))
        self.assertNotIn(["stop", "s0001"], self.argvs())  # nothing to stop
        self.assertEqual(self.ends(out)[0]["did"], "gone")
        self.assertEqual(len([e for e in out["errors"] if e.get("dispatch")]), 1)

    def test_a_clock_that_went_back_stops_the_pass_with_one_error(self):
        self.assertEqual(self.gate(self.T - timedelta(minutes=2))["errors"], [])  # inside routines.GRACE
        out = self.gate(self.T - timedelta(hours=1))
        self.assertIn(["stop", "s0001"], self.argvs())
        [err] = [e for e in out["errors"] if e.get("dispatch")]
        self.assertTrue(err["error"].endswith(routines.WENT_BACK), err)
        self.assertIn("later than now", err["error"])

    def test_a_failed_listing_waits_and_reports_once_late(self):
        with mock.patch("autopilot.sessions.list_all", return_value=None):
            soon = self.gate(self.T + timedelta(minutes=2))
            late = self.gate(self.T + timedelta(minutes=31))
        self.assertEqual((soon["dispatch"], soon["errors"]), ([], []))
        self.assertEqual(late["errors"], [{"dispatch": True, "error": "cannot list the sessions; the dispatcher pass "
                                                                      "s0001 (cards with no project) has been open "
                                                                      "since 2026-11-10T14:00:00Z"}])
        self.assertEqual(self.record()["short_id"], "s0001")
        self.assertNotIn(["stop", "s0001"], self.argvs())

    def test_a_record_with_a_bad_look_count_is_removed_with_one_error_and_a_pass_starts_again(self):
        for bad in ({"looks": "x"}, {"looks": True}, {"seen_busy": "yes"}):
            with self.subTest(bad=bad):
                rec = dispatch.read_record(self.data) if (self.data / dispatch.DISPATCH).exists() else None
                rec = rec if rec and "short_id" in rec else {
                    "group": None, "projects": [], "short_id": "s0001", "session_id": "", "profile": "subscription",
                    "started_at": "2026-11-10T14:00:00Z", "queue": []}
                (self.data / dispatch.DISPATCH).write_text(json.dumps(dict(rec, **bad)))
                self.add_card()  # a new ready card: a pass is due again
                out = self.gate(self.T + timedelta(minutes=1))
                [err] = [e for e in out["errors"] if e.get("dispatch")]
                self.assertIn("it was removed", err["error"])
                self.assertEqual([e["did"] for e in out["dispatch"]], ["started"])  # not stuck behind it

    def test_a_damaged_record_is_removed_with_one_error(self):
        (self.data / dispatch.DISPATCH).write_text("{")
        out = self.gate(self.T + timedelta(minutes=1))
        [err] = [e for e in out["errors"] if e.get("dispatch")]
        self.assertIn("it was removed", err["error"])
        self.assertFalse((self.data / dispatch.DISPATCH).exists())

    def test_an_end_marker_of_another_pass_does_not_end_this_one(self):
        (self.data / dispatch.END).write_text(json.dumps({"short_id": "s0001", "started_at": "2026-11-09T14:00:00Z",
                                                          "ended_at": "2026-11-09T14:05:00Z"}))
        out = self.gate(self.T + timedelta(minutes=1))
        self.assertEqual(self.ends(out), [])
        self.assertEqual(self.record()["short_id"], "s0001")


class HeartbeatKeyTest(DispatchCase):
    def test_gate_json_has_the_dispatch_key(self):
        out = self.gate(self.T)
        self.assertEqual(out["dispatch"], [])
        self.assertEqual(json.loads((self.data / "gate.json").read_text())["dispatch"], [])


class EndTest(DispatchCase):
    def test_end_with_no_open_pass_is_refused(self):
        with self.assertRaises(dispatch.DispatchError):
            dispatch.end(self.data, self.T)
        (self.data / dispatch.DISPATCH).write_text(json.dumps({"queue": []}))
        with self.assertRaises(dispatch.DispatchError):
            dispatch.end(self.data, self.T)

    def test_end_writes_its_own_marker(self):
        self.add_card()
        self.gate(self.T)
        marker = dispatch.end(self.data, self.T + timedelta(minutes=3))
        self.assertEqual(marker, {"short_id": "s0001", "started_at": "2026-11-10T14:00:00Z",
                                  "ended_at": "2026-11-10T14:03:00Z", "token": self.record()["token"]})
        self.assertEqual(json.loads((self.data / dispatch.END).read_text()), marker)
        self.assertNotIn("ended_at", self.record())

    def test_end_from_another_pass_is_refused_and_from_this_pass_or_a_terminal_ends_it(self):
        self.add_card()
        self.gate(self.T)
        token = self.record()["token"]
        with mock.patch.dict(os.environ, {"AP2_PASS": "0" * 16}):
            with self.assertRaisesRegex(dispatch.DispatchError, "not the open dispatcher pass"):
                dispatch.end(self.data, self.T)
        self.assertFalse((self.data / dispatch.END).exists())
        with mock.patch.dict(os.environ, {"AP2_PASS": token}):
            self.assertEqual(dispatch.end(self.data, self.T)["token"], token)
        (self.data / dispatch.END).unlink()
        with mock.patch.dict(os.environ):
            os.environ.pop("AP2_PASS", None)
            self.assertEqual(dispatch.end(self.data, self.T)["token"], token)

    def test_a_marker_with_another_token_does_not_end_the_pass(self):
        self.add_card()
        self.gate(self.T)
        marker = dispatch.end(self.data, self.T)
        (self.data / dispatch.END).write_text(json.dumps(dict(marker, token="0" * 16)))
        out = self.gate(self.T + timedelta(minutes=1))
        self.assertEqual([e for e in out["dispatch"] if e["did"] != "started"], [])
        self.assertEqual(self.record()["short_id"], "s0001")
