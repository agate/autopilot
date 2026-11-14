"""The dispatcher pass (design 8.0, section 12 "Dispatcher pass"): repo groups, the pass's floor, the gate starting
one pass and never waiting, and a later run ending it. Every session is tests/bin/fake-claude-v2; none is real."""
from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT
from tests.test_ap2_gate import FAKE, LoopCase
from assistant import dispatch, gate, profiles, routines, spend, workers
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
        for rule in ("Bash(gh pr ready:*)", "Bash(gh pr merge:*)", "Bash(gh pr create:*)", "Bash(git push:*)",
                     "Bash(git -C:*)", "Bash(git -c:*)", "Bash(git --git-dir:*)", "Bash(git --work-tree:*)",
                     "Bash(gh api:*)", "mcp__slack__slack_send_message", "mcp__jira__transitionJiraIssue"):
            self.assertIn(rule, deny)
        for tool in ("mcp__slack__slack_read_channel", "mcp__jira__getJiraIssue"):
            self.assertNotIn(tool, deny)

    def test_live_mode_allows_the_outward_writes(self):
        deny = self.floor("live")["permissions"]["deny"]
        for rule in dispatch.SHADOW_DENY + ("mcp__slack__slack_send_message", "mcp__jira__transitionJiraIssue"):
            self.assertNotIn(rule, deny)
        for rule in ("Bash(git -C:*)", "Bash(git -c:*)", "Bash(git --git-dir:*)", "Bash(git --work-tree:*)",
                     "Bash(gh api:*)"):
            self.assertNotIn(rule, deny)
        self.assertEqual(sorted(deny), sorted(set(deny)))

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
        self.assertIn(str(dispatch.RUNBOOK), argv[-1])
        self.assertIn("shop", argv[-1])
        ap2 = workers.package_root() / "bin" / "ap2"
        self.assertTrue(ap2.is_file())
        self.assertIn(f"Run ap2 as {ap2} (that exact path", argv[-1])
        self.assertEqual(call["cwd"], str(self.repo))
        self.assertEqual(call["env"]["AP2_ROLE"], "dispatcher")
        rec = self.record()
        self.assertEqual({k: rec[k] for k in ("group", "projects", "short_id", "started_at", "profile", "queue")},
                         {"group": str(self.repo), "projects": ["shop"], "short_id": "s0001",
                          "started_at": "2026-11-10T14:00:00Z", "profile": "subscription", "queue": []})
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

    def test_a_card_with_no_project_runs_its_pass_in_the_data_folder(self):
        self.add_card()
        self.gate(self.T)
        [call] = self.dispatch_calls()
        self.assertEqual(call["cwd"], str(self.data))
        self.assertEqual(call["argv"][2], "ap2 dispatch · " + dispatch.NO_PROJECT)

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

    def test_a_broken_project_keeps_the_pass_due_when_nothing_else_starts(self):
        (self.data / "projects").mkdir()
        (self.data / "projects" / "broken.yaml").write_text("nope: 1\n", encoding="utf-8")
        self.add_card(project="broken")
        out = self.gate(self.T)
        self.assertEqual([e.get("project") for e in out["errors"]], ["broken"])
        self.assertIn("project broken: refused:", self.gate_log())
        self.assertIs(self.pass_file()["due"], True)

    def test_a_launch_that_fails_starts_nothing_and_the_pass_stays_due(self):
        self.add_card()
        with mock.patch.dict(os.environ, {"FAKE_BG_FAIL": "1"}):
            out = self.gate(self.T)
        [err] = out["errors"]
        self.assertIs(err["dispatch"], True)
        self.assertIn("cannot start the dispatcher pass", err["error"])
        self.assertFalse((self.data / dispatch.DISPATCH).exists())
        self.assertIs(self.pass_file()["due"], True)
        self.assertEqual(out["dispatch"], [])
        out = self.gate(self.T + timedelta(minutes=1))  # the next run tries again
        self.assertEqual(out["dispatch"][0]["did"], "started")

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
        self.add_card(project="shop")
        self.add_card()

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
        self.assertEqual(self.dispatch_calls()[1]["cwd"], str(self.data))
        self.assertEqual(self.record()["queue"], [])
        self.assertIs(self.pass_file()["due"], True)  # the card that landed during the round
        self.set_agent("s0002", state="done", status="idle")
        dispatch.end(self.data, self.T + timedelta(minutes=3))
        third = self.gate(self.T + timedelta(minutes=3))
        self.assertEqual([e["did"] for e in third["dispatch"]], ["ended", "started"])  # the next round
        self.assertEqual(third["dispatch"][1]["group"], str(self.repo))

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
        self.assertFalse((self.data / dispatch.DISPATCH).exists())
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
                                  "ended_at": "2026-11-10T14:03:00Z"})
        self.assertEqual(json.loads((self.data / dispatch.END).read_text()), marker)
        self.assertNotIn("ended_at", self.record())
