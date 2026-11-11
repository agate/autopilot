"""Workers and sessions (design 8.1, 8.2, 8.5, 3.5): ports, single-use resources, the per-project setup, start /
end / dead / parked, no process waiting for a human, transcripts and shadow mode. Every test runs the fake claude
(`tests/bin/fake-claude-v2`) against temp folders and an injected clock; nothing reaches the real ~/.claude."""
from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

try:
    import fcntl
except ImportError:  # not POSIX
    fcntl = None

from tests.helpers import ROOT
from tests.test_ap2_store import card
from assistant import config, resources, transcripts
from assistant.config import Config, ShadowModeError
from assistant.resources import WorkerError
from assistant.store import Store
from assistant.workers import DIED, PROTECTED, Workers
from autopilot import perms as v1_perms
from autopilot import sessions as v1_sessions

FAKE = str(ROOT / "tests" / "bin" / "fake-claude-v2")
T0 = datetime(2026, 1, 5, 9, 0, 0, tzinfo=timezone.utc)
LIVE = Config(mode="live")


class Clock:
    def __init__(self, at=T0):
        self.at = at

    def __call__(self):
        return self.at

    def advance(self, minutes):
        self.at += timedelta(minutes=minutes)


def usage_file(path: Path, percent: int, resets_at="2026-01-05T12:00:00+00:00", fetched=T0) -> Path:
    """A Claude Code usage cache fetched at `fetched` with one window at `percent`."""
    path.write_text(json.dumps({"cachedUsageUtilization": {
        "fetchedAtMs": int(fetched.timestamp() * 1000),
        "utilization": {"limits": [{"kind": "session", "percent": percent, "resets_at": resets_at}]}}}))
    return path


class WorkerCase(unittest.TestCase):
    cfg = LIVE

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-workers-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.fake_home = self.tmp / "fake-claude"
        self.fake_home.mkdir()
        self.log = self.tmp / "claude.log"
        self.home = self.tmp / "home"  # the real home folder is never reached
        self.home.mkdir()
        patch = mock.patch.dict(os.environ, {"FAKE_CLAUDE_HOME": str(self.fake_home), "FAKE_CLAUDE_LOG": str(self.log),
                                             "HOME": str(self.home)})
        patch.start()
        self.addCleanup(patch.stop)
        self.clock = Clock()
        self.wt = self.tmp / "worktree"
        self.wt.mkdir()

    def workers(self, cfg=None, **kw) -> Workers:
        cfg = cfg or self.cfg
        return Workers(self.data, cfg=cfg, claude=FAKE, transcripts_dir=self.fake_home / "projects", clock=self.clock,
                       store=Store(self.data, cfg, self.clock), **kw)

    def add_card(self, **over) -> str:
        return Store(self.data, self.cfg, self.clock).add(card(**over), by="test")["id"]

    def get(self, card_id) -> dict:
        return Store(self.data, self.cfg, self.clock).get(card_id)

    def calls(self) -> list:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def bg_calls(self) -> list:
        return [c for c in self.calls() if c["argv"][:1] == ["--bg"]]

    def discovery_calls(self) -> list:
        return [c for c in self.calls() if c["argv"][:1] == ["-p"]]

    def deny(self, card_id, role="implement") -> list:
        f = transcripts.run_dir(self.data, card_id) / f"settings-{role}.json"
        return json.loads(f.read_text())["permissions"]["deny"]

    def agents(self) -> list:
        f = self.fake_home / "agents.json"
        return json.loads(f.read_text()) if f.exists() else []

    def set_agent(self, short, **fields):
        rows = self.agents()
        for a in rows:
            if a["id"] == short:
                a.update(fields)
        (self.fake_home / "agents.json").write_text(json.dumps(rows))

    def drop_agent(self, short):
        (self.fake_home / "agents.json").write_text(json.dumps([a for a in self.agents() if a["id"] != short]))

    def start(self, w=None, card_id=None, role="implement", **kw):
        w = w or self.workers()
        card_id = card_id or self.add_card()
        return w.start(card_id, role, cwd=self.wt, prompt="Do the work.", **kw)

    def write_result(self, card_id, name="handback.json", body=None):
        d = transcripts.run_dir(self.data, card_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(json.dumps(body if body is not None else {"summary": "done"}))
        return d / name


# -- 1. port ranges ------------------------------------------------------------------------------------------------

class PortsTest(WorkerCase):
    def test_two_workers_never_share_a_range(self):
        a, b = self.start(), self.start()
        self.assertEqual(a["ports"], [7200, 7209])
        self.assertEqual(b["ports"], [7210, 7219])
        envs = [c["env"]["AP2_PORTS"] for c in self.bg_calls()]
        self.assertEqual(envs, ["7200-7209", "7210-7219"])
        self.assertIn("Your port range: 7210-7219.", self.bg_calls()[1]["argv"][-1])

    def test_an_ended_workers_range_is_handed_out_again(self):
        a, b = self.start(), self.start()
        self.workers().end(a["card"], "implement")
        self.assertEqual(self.start()["ports"], [7200, 7209])
        self.assertEqual(b["ports"], [7210, 7219])

    def test_port_base_is_a_setting(self):
        self.assertEqual(self.start(self.workers(port_base=9000))["ports"], [9000, 9009])

    def test_parked_worker_keeps_its_range_when_it_resumes(self):
        w = self.workers()
        a, b = self.start(w), self.start(w)
        parked = w.park(a["card"], "implement", "halfway", usage_file=usage_file(self.tmp / "u.json", 95))
        self.assertEqual(parked["percent"], 95)
        w.end(b["card"], "implement")
        c = self.start(w)  # takes b's freed range; a's stays a's while it is parked
        self.assertEqual(c["ports"], [7210, 7219])
        self.assertEqual([r["state"] for r in w.list() if r["card"] == a["card"]], ["parked"])
        back = w.start(a["card"], "implement", prompt="Continue.", resume=True)
        self.assertEqual(back["ports"], [7200, 7209])
        last = self.bg_calls()[-1]
        self.assertEqual(last["argv"][1:3], ["--resume", a["session_id"]])
        self.assertEqual(last["env"]["AP2_PORTS"], "7200-7209")
        self.assertIn(["stop", a["short_id"]], [c["argv"] for c in self.calls()])
        self.assertEqual(len([r for r in resources.Registry(self.data).read() if r["card"] == a["card"]]), 1)

    def test_a_parked_worker_with_a_resource_at_the_cap_resumes_into_its_own_slot(self):
        w = self.workers(max_workers=2)
        a = self.start(w, needs=["device"])
        self.start(w)
        w.park(a["card"], "implement", "x", usage_file=usage_file(self.tmp / "u.json", 95))
        back = w.start(a["card"], "implement", prompt="Continue.", resume=True)  # its own slot and resource
        self.assertEqual((back["needs"], back["ports"]), (["device"], a["ports"]))

    def test_park_is_refused_under_the_line(self):
        w = self.workers()
        a = self.start(w)
        with self.assertRaisesRegex(WorkerError, "under the park line"):
            w.park(a["card"], "implement", "x", usage_file=usage_file(self.tmp / "u.json", 40))
        self.assertIsNone(resources.Registry(self.data).read()[0]["parked"])

    def test_a_parked_worker_resumes_once_its_window_has_reset(self):
        w = self.workers()
        a = self.start(w)
        w.park(a["card"], "implement", "x", usage_file=usage_file(self.tmp / "u.json", 95))
        self.assertEqual(w.check(usage_file=self.tmp / "u.json")[0]["did"], "nothing")  # still at the line
        self.clock.advance(4 * 60)  # 13:00, past the 12:00 reset and its 5 minutes
        high = usage_file(self.tmp / "high.json", 88, resets_at="2026-01-05T17:00:00+00:00", fetched=self.clock())
        self.assertEqual(w.check(usage_file=high)[0]["did"], "nothing")  # under the park line, over the new-work line
        fresh = usage_file(self.tmp / "fresh.json", 3, resets_at="2026-01-05T17:00:00+00:00", fetched=self.clock())
        self.assertEqual(w.check(usage_file=fresh)[0]["did"], "resumed")
        rec = resources.Registry(self.data).read()[0]
        self.assertEqual((rec["ports"], rec["parked"]), ([7200, 7209], None))
        self.assertEqual(self.bg_calls()[-1]["argv"][1:3], ["--resume", a["session_id"]])
        # the parked session sat idle; the copy that continues takes its place
        self.assertIn(["stop", a["short_id"]], [c["argv"] for c in self.calls()])
        self.assertNotIn(a["short_id"], [x["id"] for x in self.agents()])
        self.assertEqual(rec["resumes"], 0)  # a parked worker's resume is not a death
        self.drop_agent(rec["short_id"])
        self.assertEqual(w.check()[0]["did"], "resumed")  # so its first death still gets its one resume

    def test_a_parked_worker_that_cannot_resume_is_stopped(self):
        w = self.workers()
        a = self.start(w)
        w.park(a["card"], "implement", "x", usage_file=usage_file(self.tmp / "u.json", 95))
        shutil.rmtree(self.wt)  # its worktree is gone, so its resume cannot start
        self.clock.advance(4 * 60)
        fresh = usage_file(self.tmp / "fresh.json", 3, resets_at="2026-01-05T17:00:00+00:00", fetched=self.clock())
        out = w.check(usage_file=fresh)[0]
        self.assertEqual((out["state"], out["did"]), ("parked", "stopped"))
        self.assertIn("cannot start the session", out["note"])
        c = self.get(a["card"])
        self.assertEqual((c["ask"]["type"], c["ask"]["reason"]), ("fix", DIED))
        self.assertIn("could not resume after parking", c["ask"]["text"])
        self.assertEqual(c["sessions"]["implement"]["state"], "stopped")
        self.assertEqual(resources.Registry(self.data).read(), [])  # its slot, ports and resources are free
        self.assertEqual(w.check(usage_file=fresh), [])


# -- 2. single-use resources, needs and max_workers ---------------------------------------------------------------

class ResourcesTest(WorkerCase):
    def test_a_held_resource_refuses_the_start_naming_the_holder(self):
        w = self.workers()
        a = self.start(w, needs=["native-dev-loop"])
        other = self.add_card()
        with self.assertRaisesRegex(WorkerError, f"native-dev-loop is held by {a['card']} · implement"):
            w.start(other, "implement", cwd=self.wt, needs=["native-dev-loop"])
        self.assertEqual(len(self.bg_calls()), 1)
        self.assertEqual(len(resources.Registry(self.data).read()), 1)

    def test_the_resource_is_free_once_its_holder_ends(self):
        w = self.workers()
        a = self.start(w, needs=["native-dev-loop"])
        w.end(a["card"], "implement")
        b = self.start(w, needs=["native-dev-loop"])
        self.assertEqual(b["needs"], ["native-dev-loop"])

    def test_a_parked_holder_still_holds_its_resource(self):
        w = self.workers()
        a = self.start(w, needs=["device"])
        w.park(a["card"], "implement", "x", usage_file=usage_file(self.tmp / "u.json", 95))
        with self.assertRaisesRegex(WorkerError, "device is held"):
            self.start(w, needs=["device"])

    def test_a_paused_holder_resumes_with_its_resource(self):
        w = self.workers()
        a = self.start(w, needs=["device"])
        self.set_agent(a["short_id"], status="idle")
        w.check()
        self.clock.advance(60)
        self.assertEqual(w.check()[0]["did"], "paused")
        back = w.start(a["card"], "implement", prompt="Continue.", resume=True)  # no needs given
        self.assertEqual(back["needs"], ["device"])
        self.assertEqual(resources.Registry(self.data).read()[0]["needs"], ["device"])
        with self.assertRaisesRegex(WorkerError, f"device is held by {a['card']} · implement"):
            self.start(w, needs=["device"])

    def test_the_planner_never_starts_two_cards_needing_the_same_resource(self):
        cands = [{"project": "app", "card": "c1", "needs": ["native-dev-loop"]},
                 {"project": "app", "card": "c2", "needs": ["native-dev-loop"]},
                 {"project": "web", "card": "c3", "needs": []}]
        self.assertEqual(resources.plan(cands, []), [("app", "c1"), ("web", "c3")])
        held = [{"card": "c9", "needs": ["native-dev-loop"]}]
        self.assertEqual(resources.plan(cands, held), [("web", "c3")])
        self.assertEqual(resources.plan(cands, held, max_workers=1), [])
        busy = [{"card": "c1", "needs": []}]  # a card that already has a worker is not started again
        self.assertEqual(resources.plan(cands, busy), [("app", "c2"), ("web", "c3")])

    def test_the_planner_is_round_robin_across_projects(self):
        cands = [{"project": "a", "card": "a1"}, {"project": "a", "card": "a2"}, {"project": "b", "card": "b1"}]
        self.assertEqual(resources.plan(cands, [], max_workers=2), [("a", "a1"), ("b", "b1")])

    def test_a_sixth_start_is_refused(self):
        w = self.workers()
        for _ in range(5):
            self.start(w)
        with self.assertRaisesRegex(WorkerError, r"5 workers already hold a slot \(max_workers is 5\)"):
            self.start(w)
        self.assertEqual(len(self.bg_calls()), 5)

    def test_parked_workers_count_against_the_cap(self):
        w = self.workers(max_workers=1)
        a = self.start(w)
        w.park(a["card"], "implement", "x", usage_file=usage_file(self.tmp / "u.json", 95))
        with self.assertRaisesRegex(WorkerError, "max_workers is 1"):
            self.start(w)

    def test_one_worker_per_card(self):
        w = self.workers()
        a = self.start(w)
        with self.assertRaisesRegex(WorkerError, "already running"):
            w.start(a["card"], "implement", cwd=self.wt)
        with self.assertRaisesRegex(WorkerError, "already has a worker"):
            w.start(a["card"], "review", cwd=self.wt)


# -- 3. per-project setup ------------------------------------------------------------------------------------------

PROJECT = """\
repo: /repos/shop
branch_prefix: me
resources: [native-dev-loop]
extra_hosts: [api.example.com, cdn.example.com]
mcp_servers: [Tracker]
tools: [mcp__Tracker__send_message, mcp__Tracker__read_issue]
health_checks:
  - name: cloud
    command: cloud auth status
    fix: cloud auth login
"""


class ProjectSetupTest(WorkerCase):
    def setUp(self):
        super().setUp()
        (self.data / "projects").mkdir()
        (self.data / "projects" / "shop.yaml").write_text(PROJECT)

    def test_the_setup_reaches_the_session_through_settings_and_env(self):
        cid = self.add_card(project="shop", key="PROJ-123")
        rec = self.workers().start(cid, "implement", cwd=self.wt, prompt="Do the work.")
        call = self.bg_calls()[0]
        argv, env = call["argv"], call["env"]
        self.assertEqual(call["cwd"], str(self.wt))
        self.assertEqual(argv[argv.index("-n") + 1], f"{cid} · implement")
        self.assertEqual(argv[argv.index("--agent") + 1], "autopilot:implementer")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "auto")
        self.assertEqual(env["AP2_BRANCH"], "me.PROJ-123.retry-failed-payments")
        self.assertEqual(rec["branch"], "me.PROJ-123.retry-failed-payments")
        self.assertEqual(env["AP2_EXTRA_HOSTS"], "api.example.com,cdn.example.com")
        self.assertEqual(env["AP2_MCP_SERVERS"], "Tracker")
        self.assertEqual(json.loads(env["AP2_HEALTH_CHECKS"]),
                         [{"name": "cloud", "command": "cloud auth status", "fix": "cloud auth login"}])
        self.assertEqual((env["AP2_ROLE"], env["AP2_CARD"], env["AP2_PROJECT"], env["AP2_DATA"]),
                         ("implement", cid, "shop", str(self.data)))
        self.assertNotIn("AUTOPILOT_ROLE", env)  # v1's park hook never takes a v2 worker for its own
        settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())
        deny = settings["permissions"]["deny"]
        self.assertIn("mcp__Tracker__send_message", deny)  # outward tool: denied (v1 perms.denied_tools)
        self.assertNotIn("mcp__Tracker__read_issue", deny)
        # The data folder's own files, the user's templates and sources (design 6), and every card's settings files.
        self.assertLessEqual({"config.yaml", "workers.json", ".workers.lock", ".store.lock", "index.sqlite",
                              "items/**", "restore/**", "projects/**", "templates/**", "sources/**",
                              "sessions/**/settings-*.json"}, set(PROTECTED))
        for name in PROTECTED:
            self.assertIn(f"Edit(/{self.data}/{name})", deny)
        self.assertIn("Bash(gh pr comment:*)", deny)
        self.assertEqual(settings["permissions"]["allow"], [f"Bash({ROOT / 'bin' / 'ap2'} worker park:*)"])
        self.assertEqual(settings["env"], {k: v for k, v in env.items() if k.startswith("AP2_")})
        self.assertIn("Hosts you may reach besides the usual ones: api.example.com, cdn.example.com.", argv[-1])

    def test_inherited_v1_variables_are_dropped(self):
        with mock.patch.dict(os.environ, {"AUTOPILOT_ROLE": "worker", "AUTOPILOT_DATA": "/v1"}):
            self.start()
        self.assertEqual({k for k in self.bg_calls()[0]["env"] if k.startswith("AUTOPILOT_")}, set())

    def test_an_unknown_key_is_refused_with_its_line(self):
        (self.data / "projects" / "shop.yaml").write_text("repo: /x\nextra_host: [a]\n")
        cid = self.add_card(project="shop")
        with self.assertRaisesRegex(config.ConfigError, r"shop.yaml: line 2: unknown key 'extra_host'"):
            self.workers().start(cid, "implement", cwd=self.wt)
        self.assertEqual(self.bg_calls(), [])

    def test_a_value_of_the_wrong_type_is_refused_with_its_line(self):
        rows = [("tools: mcp__Tracker__send_message", "tools must be a list of names"),
                ("extra_hosts: a.com", "extra_hosts must be a list of names"),
                ("resources: [native-dev-loop, '']", "resources must be a list of names"),
                ("branch_prefix: [a]", "branch_prefix must be a text"),
                ("repo: ''", "repo must be a text"),
                ("health_checks:\n  - name: x", "health_checks must be a list of {name, command, fix} maps")]
        cid = self.add_card(project="shop")
        for row, reason in rows:
            with self.subTest(row=row):
                (self.data / "projects" / "shop.yaml").write_text(f"branch_pattern: x\n{row}\n")
                with self.assertRaisesRegex(config.ConfigError, r"shop\.yaml: line 2: " + re.escape(reason)):
                    self.workers().start(cid, "implement", cwd=self.wt)
        self.assertEqual(self.bg_calls(), [])

    def test_a_project_name_that_is_not_a_name_is_refused(self):
        from assistant import projects
        for bad in ("../x", "Shop", "a/b", "", None):
            with self.subTest(name=bad):
                with self.assertRaisesRegex(config.ConfigError, "not a project name"):
                    projects.path(self.data, bad)
        cid = self.add_card(project="../projects/shop")
        with self.assertRaisesRegex(config.ConfigError, "not a project name"):
            self.workers().start(cid, "implement", cwd=self.wt)
        self.assertEqual(self.bg_calls(), [])

    def test_branch_without_a_prefix_and_from_the_card(self):
        from assistant import projects
        self.assertEqual(projects.branch(projects.DEFAULTS, "PROJ-1", "fix-it"), "PROJ-1.fix-it")
        cid = self.add_card(branch="me.PROJ-9.given")
        self.assertEqual(self.workers().start(cid, "implement", cwd=self.wt)["branch"], "me.PROJ-9.given")

    def test_listed_tools_are_used_as_they_are(self):
        cid = self.add_card(project="shop")
        self.workers().start(cid, "implement", cwd=self.wt)
        self.assertEqual(self.discovery_calls(), [])  # no `claude -p`: the project lists its tools
        deny = self.deny(cid)
        self.assertIn("mcp__Tracker__send_message", deny)
        self.assertNotIn("mcp__slack__slack_send_message", deny)


class ToolDiscoveryTest(WorkerCase):
    """A project that lists no `tools` gets them the way v1 does (`perms.discover_tools`), so its outward MCP tools
    are denied as well. The list comes only from places no worker can change: the project file, else a discovery
    on every floor ap2 writes, in the project's `repo` (a folder no worker runs in) or a fresh empty folder. It is
    never read from or saved on a card, and never found in the worker's folder or the data folder."""

    def repo_project(self, name="bare", extra=""):
        repo = self.tmp / "repo"
        repo.mkdir(exist_ok=True)
        (self.data / "projects").mkdir(exist_ok=True)
        (self.data / "projects" / f"{name}.yaml").write_text(f"repo: {repo}\n{extra}")
        return repo

    def assert_found_elsewhere(self, calls):
        """Each discovery ran in a folder that is neither under the worker's folder nor the data folder, and is
        gone now."""
        self.assertTrue(calls)
        for c in calls:
            where = Path(c["cwd"]).resolve()
            for folder in (self.wt, self.data):
                self.assertNotEqual(where, folder)
                self.assertNotIn(folder, where.parents)
            self.assertFalse(where.exists())

    def test_unlisted_tools_are_discovered_and_the_outward_ones_denied(self):
        cid = self.add_card()  # no project: no `tools` listed
        self.workers().start(cid, "implement", cwd=self.wt)
        found = self.discovery_calls()
        self.assertEqual(len(found), 1)
        self.assert_found_elsewhere(found)
        self.assertEqual(found[0]["argv"][found[0]["argv"].index("--output-format") + 1], "stream-json")
        self.assertLess(self.calls().index(found[0]), self.calls().index(self.bg_calls()[0]))
        deny = self.deny(cid)
        self.assertIn("mcp__slack__slack_send_message", deny)
        self.assertNotIn("mcp__slack__slack_read_channel", deny)
        self.assertNotIn("Read", deny)
        self.assertNotIn("tools", self.get(cid)["sessions"]["implement"])  # never saved on the card

    def test_with_no_repo_folder_the_tools_are_found_in_a_fresh_empty_folder(self):
        (self.data / "projects").mkdir()
        (self.data / "projects" / "bare.yaml").write_text("repo: /repos/bare\n")  # not a folder here
        seen = []
        real = v1_perms.discover_tools

        def spy(claude, cwd):
            seen.append((cwd, sorted(os.listdir(cwd))))
            return real(claude, cwd)

        with mock.patch.object(v1_perms, "discover_tools", side_effect=spy):
            self.workers().start(self.add_card(project="bare"), "implement", cwd=self.wt)
            self.workers().start(self.add_card(), "implement", cwd=self.data)  # no project; <data> as its folder
        self.assertEqual([files for _cwd, files in seen], [[], []])  # empty when claude ran there
        self.assertNotEqual(seen[0][0], seen[1][0])  # one folder per call
        self.assert_found_elsewhere(self.discovery_calls())
        self.assertEqual([c["cwd"] for c in self.bg_calls()], [str(self.wt), str(self.data)])

    def test_every_floor_finds_the_tools_in_the_projects_repo(self):
        repo = self.repo_project()
        w = self.workers()
        a = w.start(self.add_card(project="bare"), "implement", cwd=self.wt)  # a fresh start
        self.drop_agent(a["short_id"])
        self.assertEqual(w.check()[0]["did"], "resumed")  # an in-place resume
        self.write_result(a["card"])
        self.assertEqual(w.check()[0]["did"], "ended")
        w.start(a["card"], "review", cwd=self.wt)  # a later role
        self.write_result(a["card"], "question.json", {"text": "Which?"})
        self.assertEqual(w.check()[0]["did"], "asked")
        w.answer(a["card"], "This one.")  # an answer
        self.assertEqual([c["cwd"] for c in self.discovery_calls()], [str(repo)] * 4)  # the main checkout, as v1
        self.assertEqual({c["cwd"] for c in self.bg_calls()}, {str(self.wt)})
        self.assertIn("mcp__slack__slack_send_message", self.deny(a["card"], "review"))

    def test_a_card_cannot_bring_its_own_tool_list(self):
        sid = "0b4c6f4e-0000-4000-8000-000000000001"
        cid = self.add_card(sessions={"implement": {"tools": []}, "review": {"id": sid, "ids": [sid], "tools": []}})
        w = self.workers()
        w.start(cid, "implement", cwd=self.wt)
        self.assertIn("mcp__slack__slack_send_message", self.deny(cid))
        w.end(cid, "implement")
        w.start(cid, "review", cwd=self.wt)  # a role of the card that ran before is not refused either
        self.assertIn("mcp__slack__slack_send_message", self.deny(cid, "review"))
        self.assertEqual(len(self.discovery_calls()), 2)

    def test_another_cards_folder_is_never_where_the_tools_are_found(self):
        w = self.workers()
        a = self.start(w)  # card A's worker in the shared folder
        (self.wt / "CLAUDE.md").write_text("planted\n")
        (self.wt / ".claude").mkdir()
        (self.wt / ".claude" / "settings.json").write_text("{}\n")
        w.end(a["card"], "implement")
        self.start(w)  # card B's first start in the same folder, no repo
        self.assertEqual(len(self.discovery_calls()), 2)
        self.assert_found_elsewhere(self.discovery_calls())

    def test_a_worker_never_runs_in_the_projects_repo(self):
        repo = self.repo_project()
        link = self.tmp / "link"
        link.symlink_to(repo)
        cid = self.add_card(project="bare")
        for cwd in (repo, link):
            with self.assertRaisesRegex(WorkerError, rf"^a worker runs in a worktree, not in the project's repo "
                                                     rf"{re.escape(str(repo))}$"):
                self.workers().start(cid, "implement", cwd=cwd)
        self.assertEqual((self.discovery_calls(), self.bg_calls()), ([], []))
        self.assertEqual(resources.Registry(self.data).read(), [])

    def test_a_project_that_lists_tools_always_wins(self):
        self.repo_project()
        w = self.workers()
        a = w.start(self.add_card(project="bare"), "implement", cwd=self.wt)
        self.assertEqual(len(self.discovery_calls()), 1)
        w.end(a["card"], "implement")
        self.repo_project(extra="tools: [mcp__Tracker__create_issue]\n")
        w.start(a["card"], "review", cwd=self.wt)
        self.assertEqual(len(self.discovery_calls()), 1)  # no new `claude -p`
        deny = self.deny(a["card"], "review")
        self.assertIn("mcp__Tracker__create_issue", deny)
        self.assertNotIn("mcp__slack__slack_send_message", deny)

    def test_an_in_place_resume_lists_the_tools_again(self):
        (self.data / "projects").mkdir()
        (self.data / "projects" / "shop.yaml").write_text("tools: [mcp__Tracker__read_issue]\n")
        cid = self.add_card(project="shop")
        w = self.workers()
        a = w.start(cid, "implement", cwd=self.wt)
        self.assertEqual(self.discovery_calls(), [])
        (self.data / "projects" / "shop.yaml").write_text("repo: /repos/shop\n")  # its tools no longer listed
        self.drop_agent(a["short_id"])
        with mock.patch.dict(os.environ, {"FAKE_TOOLS": "Read,mcp__Tracker__create_issue"}):
            self.assertEqual(w.check()[0]["did"], "resumed")
        self.assertEqual(len(self.discovery_calls()), 1)
        self.assert_found_elsewhere(self.discovery_calls())
        self.assertIn("mcp__Tracker__create_issue", self.deny(cid))
        self.assertEqual(len(self.bg_calls()), 2)

    def test_a_failed_discovery_starts_nothing(self):
        cid = self.add_card()
        with mock.patch.dict(os.environ, {"FAKE_TOOLS_FAIL": "1"}):
            with self.assertRaisesRegex(WorkerError, r"cannot list the session's tools for the permission floor: "
                                                     r".*no init event.*; give the card a project and list them "
                                                     r"in its projects/<name>\.yaml `tools`"):
                self.workers().start(cid, "implement", cwd=self.wt)
        self.assertEqual(len(self.discovery_calls()), 1)
        self.assertEqual(self.bg_calls(), [])
        self.assertEqual(resources.Registry(self.data).read(), [])

    def test_a_failed_discovery_on_a_dead_workers_resume_counts_as_its_one_resume(self):
        w = self.workers()
        a = self.start(w)
        self.drop_agent(a["short_id"])
        with mock.patch.dict(os.environ, {"FAKE_TOOLS_FAIL": "1"}):
            out = w.check()[0]
        self.assertEqual((out["state"], out["did"]), ("dead", "nothing"))
        self.assertIn("cannot list the session's tools", out["note"])
        self.assertEqual(len(self.bg_calls()), 1)  # never resumed without its floor
        self.assertEqual(resources.Registry(self.data).read()[0]["resumes"], 1)
        self.assertEqual(w.check()[0]["did"], "stopped")

    def test_a_parked_worker_whose_discovery_fails_is_stopped(self):
        w = self.workers()
        a = self.start(w)
        w.park(a["card"], "implement", "x", usage_file=usage_file(self.tmp / "u.json", 95))
        self.clock.advance(4 * 60)
        fresh = usage_file(self.tmp / "fresh.json", 3, resets_at="2026-01-05T17:00:00+00:00", fetched=self.clock())
        with mock.patch.dict(os.environ, {"FAKE_TOOLS_FAIL": "1"}):
            out = w.check(usage_file=fresh)[0]
        self.assertEqual((out["state"], out["did"]), ("parked", "stopped"))
        self.assertIn("cannot list the session's tools", out["note"])
        c = self.get(a["card"])
        self.assertEqual((c["ask"]["type"], c["ask"]["reason"]), ("fix", DIED))
        self.assertEqual(len(self.bg_calls()), 1)
        self.assertEqual(resources.Registry(self.data).read(), [])

    def test_a_failed_discovery_names_the_cards_project_file(self):
        (self.data / "projects").mkdir()
        (self.data / "projects" / "bare.yaml").write_text("repo: /repos/bare\n")
        cid = self.add_card(project="bare")
        with mock.patch.dict(os.environ, {"FAKE_TOOLS_FAIL": "1"}):
            with self.assertRaisesRegex(WorkerError, r"; list them in projects/bare\.yaml `tools`$"):
                self.workers().start(cid, "implement", cwd=self.wt)

    def test_a_discovery_that_times_out_starts_nothing(self):
        timeout = subprocess.TimeoutExpired("claude", 180)
        with mock.patch.object(v1_perms, "discover_tools", side_effect=timeout):
            with self.assertRaisesRegex(WorkerError, "cannot list the session's tools"):
                self.start()
        self.assertEqual(self.bg_calls(), [])
        w = self.workers()
        a = self.start(w)
        self.drop_agent(a["short_id"])
        with mock.patch.object(v1_perms, "discover_tools", side_effect=timeout) as found:
            out = w.check()[0]
        found.assert_called_once()
        self.assertEqual((out["did"], len(self.bg_calls())), ("nothing", 1))  # counts as its one resume
        self.assertEqual(resources.Registry(self.data).read()[0]["resumes"], 1)

    def test_a_missing_claude_starts_nothing(self):
        cid = self.add_card()  # no project: no `tools` listed, so they must be discovered
        w = Workers(self.data, cfg=self.cfg, claude=str(self.tmp / "no-claude"),
                    transcripts_dir=self.fake_home / "projects", clock=self.clock,
                    store=Store(self.data, self.cfg, self.clock))
        with self.assertRaisesRegex(WorkerError, "cannot list the session's tools"):
            w.start(cid, "implement", cwd=self.wt)
        self.assertEqual(self.bg_calls(), [])
        self.assertEqual(resources.Registry(self.data).read(), [])


# -- 4. start, end, dead, parked -----------------------------------------------------------------------------------

class LifecycleTest(WorkerCase):
    def test_the_result_file_is_done_and_check_ends_the_worker(self):
        w = self.workers()
        a = self.start(w)
        self.assertEqual(w.list()[0]["state"], "running")
        self.write_result(a["card"])
        self.assertEqual(w.list()[0]["state"], "done")
        out = w.check()
        self.assertEqual(out[0]["did"], "ended")
        self.assertIn(["stop", a["short_id"]], [c["argv"] for c in self.calls()])
        saved = self.get(a["card"])["sessions"]["implement"]
        self.assertEqual((saved["id"], saved["state"]), (a["session_id"], "ended"))
        self.assertEqual(resources.Registry(self.data).read(), [])
        self.assertTrue(Path(out[0]["archive"]).is_file())

    def test_each_role_has_its_result_file(self):
        w = self.workers()
        for role, name in (("review", "review.json"), ("pr", "prloop.json")):
            rec = self.start(w, role=role)
            self.write_result(rec["card"], "handback.json")
            self.assertEqual(next(r for r in w.list() if r["card"] == rec["card"])["state"], "running")
            self.write_result(rec["card"], name)
            self.assertEqual(next(r for r in w.list() if r["card"] == rec["card"])["state"], "done")
        pr = self.bg_calls()[-1]["argv"]
        self.assertNotIn("--agent", pr)
        self.assertTrue(pr[-1].startswith("/autopilot:pr-loop "))

    def test_a_result_file_older_than_the_start_is_not_done(self):
        w = self.workers()
        a = self.start(w)
        f = self.write_result(a["card"])
        old = (T0 - timedelta(hours=1)).timestamp()
        os.utime(f, (old, old))
        self.assertEqual(w.list()[0]["state"], "running")

    def test_end_saves_the_session_on_the_card(self):
        w = self.workers()
        a = self.start(w)
        out = w.end(a["card"], "implement")
        self.assertEqual(out["session_id"], a["session_id"])
        c = self.get(a["card"])
        self.assertEqual(c["sessions"]["implement"]["ids"], [a["session_id"]])
        self.assertEqual(c["audit"][-1]["did"], "worker implement: ended")
        with self.assertRaisesRegex(WorkerError, "no implement worker"):
            w.end(a["card"], "implement")

    def test_dead_once_resumes_with_you_were_interrupted(self):
        w = self.workers()
        a = self.start(w)
        self.drop_agent(a["short_id"])  # the session is gone, with no result file
        self.assertEqual(w.list()[0]["state"], "dead")
        self.clock.advance(5)
        self.assertEqual(w.check()[0]["did"], "resumed")
        last = self.bg_calls()[-1]
        self.assertEqual(last["argv"][1:3], ["--resume", a["session_id"]])
        self.assertIn("You were interrupted", last["argv"][-1])
        rec = resources.Registry(self.data).read()[0]
        self.assertEqual((rec["resumes"], rec["ports"]), (1, a["ports"]))
        self.assertNotEqual(rec["short_id"], a["short_id"])
        self.assertEqual(rec["started_at"], "2026-01-05T09:05:00Z")
        self.assertEqual(self.get(a["card"])["sessions"]["implement"]["ids"], [a["session_id"], rec["session_id"]])

    def test_a_process_that_is_gone_is_dead(self):
        w = self.workers()
        a = self.start(w)
        self.set_agent(a["short_id"], pid=2 ** 22 + 12345)  # no such process
        self.assertEqual(w.list()[0]["state"], "dead")

    def test_a_failed_session_is_dead(self):
        w = self.workers()
        a = self.start(w)
        self.set_agent(a["short_id"], state="failed", status="idle")
        self.assertEqual(w.list()[0]["state"], "dead")

    def test_a_saved_session_id_that_is_a_flag_is_refused(self):
        cid = self.add_card(sessions={"implement": {"id": "--allowedTools=Bash", "cwd": str(self.wt)}})
        with self.assertRaisesRegex(WorkerError, "is not a session id"):
            self.workers().start(cid, "implement", prompt="Continue.", resume=True)
        self.assertEqual(self.bg_calls(), [])
        self.assertEqual(resources.Registry(self.data).read(), [])

    def lock_held(self) -> bool:
        fd = os.open(str(self.data / resources.LOCK), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)  # closing it drops the lock this probe may have taken
        return False

    @unittest.skipIf(fcntl is None, "the worker list is locked with flock")
    def test_check_takes_the_listing_under_the_lock(self):
        """A start that lands between the listing and the lock would be judged against the stale listing: dead,
        and resumed in place next to itself, two sessions in one worktree."""
        w = self.workers()
        self.start(w)
        real, seen = v1_sessions.list_all, []

        def listing(claude):
            out = real(claude)
            seen.append(self.lock_held())
            if len(seen) == 1 and not seen[0]:
                self.start(w)  # another process starts a worker after this listing was taken
            return out

        with mock.patch("autopilot.sessions.list_all", side_effect=listing):
            out = w.check()
        self.assertEqual(seen[:1], [True])
        self.assertEqual([o["did"] for o in out], ["nothing"])
        self.assertEqual([c for c in self.bg_calls() if "--resume" in c["argv"]], [])

    def test_a_relative_cwd_is_saved_absolute(self):
        old = os.getcwd()
        self.addCleanup(os.chdir, old)
        os.chdir(self.tmp)
        cid = self.add_card()
        rec = self.workers().start(cid, "implement", cwd="worktree", prompt="Do the work.")
        self.assertEqual(rec["cwd"], str(self.wt))
        self.assertEqual(resources.Registry(self.data).read()[0]["cwd"], str(self.wt))
        self.assertEqual(self.get(cid)["sessions"]["implement"]["cwd"], str(self.wt))

    def test_a_resume_that_cannot_start_counts_as_a_death(self):
        w = self.workers()
        a = self.start(w)
        shutil.rmtree(self.wt)  # its worktree is gone, so its resume cannot start
        self.drop_agent(a["short_id"])
        out = w.check()[0]
        self.assertEqual((out["state"], out["did"]), ("dead", "nothing"))
        self.assertIn("cannot start the session", out["note"])
        self.assertEqual(resources.Registry(self.data).read()[0]["resumes"], 1)
        out = w.check()[0]
        self.assertEqual(out["did"], "stopped")
        ask = self.get(a["card"])["ask"]
        self.assertEqual((ask["type"], ask["reason"]), ("fix", DIED))
        self.assertEqual(resources.Registry(self.data).read(), [])  # its slot, ports and resources are free

    def test_dead_twice_stops_and_asks_for_a_fix(self):
        w = self.workers()
        a = self.start(w)
        self.drop_agent(a["short_id"])
        w.check()
        rec = resources.Registry(self.data).read()[0]
        self.drop_agent(rec["short_id"])
        out = w.check()
        self.assertEqual(out[0]["did"], "stopped")
        c = self.get(a["card"])
        self.assertEqual(c["ask"]["type"], "fix")
        self.assertIn("died twice", c["ask"]["text"])
        self.assertEqual(c["sessions"]["implement"]["state"], "stopped")
        self.assertEqual(resources.Registry(self.data).read(), [])

    def test_one_workers_error_does_not_abort_the_pass(self):
        (self.data / "projects").mkdir()
        (self.data / "projects" / "shop.yaml").write_text("repo: /repos/shop\n")
        w = self.workers()
        a = self.start(w)
        b = self.start(w, card_id=self.add_card(project="shop"))
        self.drop_agent(a["short_id"])
        self.drop_agent(b["short_id"])
        (self.data / "projects" / "shop.yaml").write_text("repo: /repos/shop\nnosuch: 1\n")
        out = {o["card"]: o for o in w.check()}
        self.assertEqual(len(out), 2)
        self.assertEqual(out[a["card"]]["did"], "resumed")
        self.assertEqual(out[b["card"]]["did"], "nothing")
        self.assertIn("unknown key 'nosuch'", out[b["card"]]["note"])
        recs = {r["card"]: r for r in resources.Registry(self.data).read()}
        self.assertNotEqual(recs[a["card"]]["short_id"], a["short_id"])
        self.assertEqual(recs[a["card"]]["resumes"], 1)
        self.assertEqual((recs[b["card"]]["short_id"], recs[b["card"]]["resumes"]), (b["short_id"], 0))

    def test_an_unexpected_error_is_named_on_its_line(self):
        w = self.workers()
        a = self.start(w)
        self.write_result(a["card"])
        with mock.patch.object(Workers, "_close", side_effect=OSError("disk full")):
            out = w.check()[0]
        self.assertEqual((out["did"], out["note"], out["error"]), ("nothing", "disk full", "OSError"))
        self.assertEqual(len(resources.Registry(self.data).read()), 1)  # still on record: the next check ends it
        self.assertEqual(w.check()[0]["did"], "ended")

    def test_an_unknown_session_id_is_not_saved_as_an_empty_id(self):
        cid = self.add_card()
        with mock.patch("autopilot.sessions.list_all", return_value=[]):  # the new session is not listed yet
            rec = self.workers().start(cid, "implement", cwd=self.wt)
        self.assertEqual(rec["session_id"], "")
        self.assertEqual(self.get(cid)["sessions"]["implement"].get("ids", []), [])
        self.assertNotIn("id", self.get(cid)["sessions"]["implement"])

    def test_a_session_id_the_listing_did_not_show_yet_is_filled_in(self):
        w = self.workers()
        a = self.start(w)
        self.write_result(a["card"], "question.json", {"text": "Three or five?"})
        w.check()
        with mock.patch("autopilot.sessions.list_all", return_value=[]):  # the copy is not listed yet
            self.assertEqual(w.answer(a["card"], "Three.")["session_id"], "")
        new = self.agents()[-1]["sessionId"]
        w.check()  # now it is listed
        self.assertEqual(resources.Registry(self.data).read()[0]["session_id"], new)
        saved = self.get(a["card"])["sessions"]["implement"]
        self.assertEqual((saved["id"], saved["ids"], saved["state"]), (new, [a["session_id"], new], "running"))
        self.write_result(a["card"], "question.json", {"text": "And the delay?"})
        w.check()
        w.answer(a["card"], "One second.")
        self.assertEqual(self.bg_calls()[-1]["argv"][1:3], ["--resume", new])

    def test_an_in_place_resumes_id_the_listing_did_not_show_yet_is_filled_in(self):
        w = self.workers()
        a = self.start(w)
        self.drop_agent(a["short_id"])  # dead once: resumed in place
        real, calls = v1_sessions.list_all, []

        def listing(claude):
            calls.append(claude)
            return [] if len(calls) == 2 else real(claude)  # check's own listing, then `_launch`'s: the copy is late

        with mock.patch("autopilot.sessions.list_all", side_effect=listing):
            self.assertEqual(w.check()[0]["did"], "resumed")
        self.assertEqual(len(calls), 2)
        self.assertEqual(resources.Registry(self.data).read()[0]["session_id"], "")  # not the old one
        new = self.agents()[-1]["sessionId"]
        self.assertNotEqual(new, a["session_id"])
        w.check()  # now it is listed
        self.assertEqual(resources.Registry(self.data).read()[0]["session_id"], new)
        saved = self.get(a["card"])["sessions"]["implement"]
        self.assertEqual((saved["id"], saved["ids"], saved["state"]), (new, [a["session_id"], new], "running"))
        self.write_result(a["card"], "question.json", {"text": "Three or five?"})
        self.assertEqual(w.check()[0]["session_id"], new)
        w.answer(a["card"], "Three.")
        self.assertEqual(self.bg_calls()[-1]["argv"][1:3], ["--resume", new])

    def test_an_unknown_listing_does_nothing(self):
        w = self.workers()
        self.start(w)
        with mock.patch("autopilot.sessions.list_all", return_value=None):
            self.assertEqual(w.check()[0], {"card": w.list()[0]["card"], "role": "implement", "state": "unknown",
                                            "did": "nothing"})

    def test_a_failed_launch_records_nothing(self):
        cid = self.add_card()
        with mock.patch.dict(os.environ, {"FAKE_BG_FAIL": "1"}):
            with self.assertRaisesRegex(WorkerError, "cannot start the session"):
                self.workers().start(cid, "implement", cwd=self.wt)
        self.assertEqual(resources.Registry(self.data).read(), [])

    def test_a_cwd_that_is_not_a_folder_is_refused(self):
        cid = self.add_card()
        with self.assertRaisesRegex(WorkerError, "not a folder"):
            self.workers().start(cid, "implement", cwd=self.tmp / "nope")
        self.assertEqual(self.bg_calls(), [])


# -- 5. no process waits for a human -------------------------------------------------------------------------------

class NobodyWaitsTest(WorkerCase):
    def test_a_question_becomes_the_cards_ask_and_the_answer_resumes_the_session(self):
        w = self.workers()
        a = self.start(w)
        self.write_result(a["card"], "question.json", {"text": "Three retries or five?"})
        out = w.check()
        self.assertEqual((out[0]["state"], out[0]["did"]), ("question", "asked"))
        self.assertIn(["stop", a["short_id"]], [c["argv"] for c in self.calls()])
        self.assertEqual(resources.Registry(self.data).read(), [])  # the slot is free while the user thinks
        ask = self.get(a["card"])["ask"]
        self.assertEqual((ask["type"], ask["text"], ask["role"], ask["session_id"]),
                         ("answer", "Three retries or five?", "implement", a["session_id"]))
        self.clock.advance(90)
        rec = w.answer(a["card"], "Three.")
        last = self.bg_calls()[-1]
        self.assertEqual(last["argv"][1:3], ["--resume", a["session_id"]])
        self.assertIn("The answer: Three.", last["argv"][-1])
        self.assertIn("Three retries or five?", last["argv"][-1])
        self.assertEqual(last["cwd"], str(self.wt))
        self.assertIsNone(self.get(a["card"])["ask"])
        self.assertEqual(rec["ports"], [7200, 7209])
        run = transcripts.run_dir(self.data, a["card"])
        self.assertFalse((run / "question.json").exists())
        self.assertEqual(len(list(run.glob("question-*.json"))), 1)
        self.assertEqual(w.list()[0]["state"], "running")

    def test_answer_needs_a_workers_question(self):
        w = self.workers()
        cid = self.add_card(ask={"type": "answer", "text": "from a person"})
        with self.assertRaisesRegex(WorkerError, "no question from a worker"):
            w.answer(cid, "yes")

    def test_a_users_own_open_ask_is_not_overwritten(self):
        w = self.workers()
        cid = self.add_card(ask={"type": "confirm", "text": "the plan"})
        self.start(w, card_id=cid)
        self.write_result(cid, "question.json", {"text": "?"})
        out = w.check()
        self.assertEqual(out[0]["did"], "ended")
        self.assertIn("already has an open ask", out[0]["note"])
        self.assertEqual(self.get(cid)["ask"]["type"], "confirm")
        # the question is kept on the card, so it can still be answered; the user's ask stays open
        self.assertEqual(self.get(cid)["sessions"]["implement"]["question"], "?")
        w.answer(cid, "Yes.")
        self.assertIn("The answer: Yes.", self.bg_calls()[-1]["argv"][-1])
        c = self.get(cid)
        self.assertEqual(c["ask"]["type"], "confirm")
        self.assertIsNone(c["sessions"]["implement"]["question"])
        with self.assertRaisesRegex(WorkerError, "no question from a worker"):
            w.answer(cid, "again")

    def test_blocked_at_a_permission_prompt_asks_for_attach_then_stops_after_30_minutes(self):
        w = self.workers()
        a = self.start(w)
        self.set_agent(a["short_id"], state="blocked", status="waiting")
        self.assertEqual(w.check()[0]["did"], "asked")
        ask = self.get(a["card"])["ask"]
        self.assertEqual(ask["type"], "fix")
        self.assertIn(f"claude attach {a['short_id']}", ask["text"])
        self.clock.advance(29)
        self.assertEqual(w.check()[0]["did"], "nothing")
        self.clock.advance(1)
        self.assertEqual(w.check()[0]["did"], "paused")
        self.assertEqual(resources.Registry(self.data).read(), [])
        saved = self.get(a["card"])["sessions"]["implement"]
        self.assertEqual(saved["state"], "paused")
        self.assertIn("permission prompt", saved["why"])
        # kept resumable: the saved session continues in the same folder
        w.start(a["card"], "implement", prompt="Continue.", resume=True)
        self.assertEqual(self.bg_calls()[-1]["argv"][1:3], ["--resume", a["session_id"]])

    def test_the_attach_ask_is_cleared_once_the_worker_moves_on(self):
        w = self.workers()
        a = self.start(w)
        self.set_agent(a["short_id"], status="waiting")
        self.assertEqual(w.check()[0]["did"], "asked")
        self.set_agent(a["short_id"], status="busy")
        self.assertEqual(w.check()[0]["state"], "running")
        self.assertIsNone(self.get(a["card"])["ask"])
        self.assertIsNone(resources.Registry(self.data).read()[0]["blocked_since"])

    def test_the_attach_ask_is_cleared_when_the_worker_ends(self):
        w = self.workers()
        a = self.start(w)
        self.set_agent(a["short_id"], status="waiting")
        w.check()
        self.write_result(a["card"])
        self.assertEqual(w.check()[0]["did"], "ended")
        self.assertIsNone(self.get(a["card"])["ask"])

    def test_a_users_ask_is_kept_when_the_block_clears(self):
        w = self.workers()
        cid = self.add_card(ask={"type": "fix", "text": "the user's own", "role": "implement"})
        a = self.start(w, card_id=cid)
        self.set_agent(a["short_id"], status="waiting")
        w.check()
        self.set_agent(a["short_id"], status="busy")
        w.check()
        self.assertEqual(self.get(cid)["ask"]["text"], "the user's own")

    def test_a_new_permission_prompt_starts_a_new_clock(self):
        w = self.workers()
        a = self.start(w)
        self.set_agent(a["short_id"], status="waiting")
        w.check()
        self.clock.advance(20)
        self.set_agent(a["short_id"], status="idle")
        self.assertEqual(w.check()[0]["state"], "idle")
        self.clock.advance(10)
        self.set_agent(a["short_id"], status="waiting")
        self.assertEqual(w.check()[0]["did"], "asked")
        self.assertIn("claude attach", self.get(a["card"])["ask"]["text"])
        self.clock.advance(29)
        self.assertEqual(w.check()[0]["did"], "nothing")

    def test_a_block_resets_the_idle_clock(self):
        w = self.workers()
        a = self.start(w)
        self.set_agent(a["short_id"], status="idle")
        w.check()
        self.clock.advance(50)
        self.set_agent(a["short_id"], status="waiting")
        w.check()
        self.set_agent(a["short_id"], status="idle")
        self.clock.advance(5)
        self.assertEqual(w.check()[0]["did"], "nothing")
        self.clock.advance(15)
        self.assertEqual(w.check()[0]["did"], "nothing")  # 15 minutes idle since the block, not 70

    def test_the_attach_ask_does_not_outlive_the_pause(self):
        w = self.workers()
        a = self.start(w)
        self.set_agent(a["short_id"], status="waiting")
        self.assertEqual(w.check()[0]["did"], "asked")
        self.clock.advance(30)
        self.assertEqual(w.check()[0]["did"], "paused")
        ask = self.get(a["card"])["ask"]
        self.assertNotIn("claude attach", ask["text"])  # that session is stopped now
        self.assertIn(f"ap2 worker start {a['card']} implement --resume", ask["text"])
        self.assertEqual((ask["type"], ask["reason"], ask["stage"]), ("fix", "permission_prompt", "paused"))
        w.start(a["card"], "implement", prompt="Continue.", resume=True)
        self.assertIsNone(self.get(a["card"])["ask"])
        self.write_result(a["card"])
        self.assertEqual(w.check()[0]["did"], "ended")
        self.assertIsNone(self.get(a["card"])["ask"])

    def test_a_users_ask_survives_a_resume(self):
        w = self.workers()
        cid = self.add_card(ask={"type": "fix", "text": "the user's own", "role": "implement"})
        a = self.start(w, card_id=cid)
        w.end(cid, "implement")
        w.start(cid, "implement", prompt="Continue.", resume=True)
        self.assertEqual(self.get(cid)["ask"]["text"], "the user's own")

    def blocked(self, w):
        """A started worker that hit a permission prompt: the card holds its attach ask."""
        a = self.start(w)
        self.set_agent(a["short_id"], status="waiting")
        self.assertEqual(w.check()[0]["did"], "asked")
        self.assertIn("claude attach", self.get(a["card"])["ask"]["text"])
        return a

    def test_ending_a_blocked_worker_by_hand_closes_its_attach_ask(self):
        w = self.workers()
        a = self.blocked(w)
        w.end(a["card"], "implement")
        self.assertIsNone(self.get(a["card"])["ask"])

    def test_finishing_the_card_closes_the_resume_hint(self):
        w = self.workers()
        a = self.blocked(w)
        self.clock.advance(30)
        self.assertEqual(w.check()[0]["did"], "paused")
        self.assertIn("--resume", self.get(a["card"])["ask"]["text"])
        w.finish(a["card"])
        self.assertIsNone(self.get(a["card"])["ask"])

    def test_no_way_out_of_a_block_leaves_the_attach_ask(self):
        """Every path that stops, replaces or drops a blocked worker's session: the card never keeps telling the
        user to attach to a session that is gone."""
        def end(w, a):
            w.end(a["card"], "implement")

        def done(w, a):
            self.write_result(a["card"])
            self.assertEqual(w.check()[0]["did"], "ended")

        def question(w, a):
            self.write_result(a["card"], "question.json", {"text": "?"})
            self.assertEqual(w.check()[0]["did"], "asked")

        def dead_once(w, a):
            self.drop_agent(a["short_id"])
            self.assertEqual(w.check()[0]["did"], "resumed")

        def dead_twice(w, a):
            dead_once(w, a)
            self.drop_agent(resources.Registry(self.data).read()[0]["short_id"])
            self.assertEqual(w.check()[0]["did"], "stopped")

        def idle_pause(w, a):
            self.set_agent(a["short_id"], status="idle")
            w.check()
            self.clock.advance(60)
            self.assertEqual(w.check()[0]["did"], "paused")

        def block_pause_then_resume(w, a):
            self.clock.advance(30)
            self.assertEqual(w.check()[0]["did"], "paused")
            w.start(a["card"], "implement", prompt="Continue.", resume=True)

        def block_pause_then_finish(w, a):
            self.clock.advance(30)
            w.check()
            w.finish(a["card"])

        def parked_then_resumed(w, a):
            w.park(a["card"], "implement", "x", usage_file=usage_file(self.tmp / "u.json", 95))
            w.start(a["card"], "implement", prompt="Continue.", resume=True)

        def parked_then_resumed_by_check(w, a):
            w.park(a["card"], "implement", "x", usage_file=usage_file(self.tmp / "u.json", 95))
            self.clock.advance(4 * 60)
            fresh = usage_file(self.tmp / "fresh.json", 3, resets_at="2026-01-05T17:00:00+00:00", fetched=self.clock())
            self.assertEqual(w.check(usage_file=fresh)[0]["did"], "resumed")

        def failed_save_after_stop(w, a):
            self.write_result(a["card"])
            with mock.patch.object(Workers, "_save_session", side_effect=OSError("disk full")):
                self.assertEqual(w.check()[0]["error"], "OSError")
            self.assertEqual(w.check()[0]["did"], "ended")

        for path in (end, done, question, dead_once, dead_twice, idle_pause, block_pause_then_resume,
                     block_pause_then_finish, parked_then_resumed, parked_then_resumed_by_check,
                     failed_save_after_stop):
            with self.subTest(path=path.__name__):
                for r in resources.Registry(self.data).read():  # each path starts from a clean worker list
                    self.workers().end(r["card"], r["role"])
                self.clock = Clock()
                w = self.workers()
                a = self.blocked(w)
                path(w, a)
                ask = self.get(a["card"])["ask"]
                self.assertNotIn("claude attach", (ask or {}).get("text", ""))

    def test_an_in_place_resume_closes_the_attach_ask_even_with_no_block_on_record(self):
        w = self.workers()
        a = self.blocked(w)
        with resources.Registry(self.data).locked() as records:  # the clock lost, e.g. a hand edit
            records[0]["blocked_since"] = None
        self.drop_agent(a["short_id"])
        self.assertEqual(w.check()[0]["did"], "resumed")
        self.assertIsNone(self.get(a["card"])["ask"])

    def test_a_prompt_while_the_users_ask_is_open_says_it_asked_nothing(self):
        w = self.workers()
        cid = self.add_card(ask={"type": "confirm", "text": "the plan"})
        a = self.start(w, card_id=cid)
        self.set_agent(a["short_id"], status="waiting")
        out = w.check()[0]
        self.assertEqual((out["state"], out["did"]), ("blocked", "nothing"))
        self.assertIn("already has an open ask", out["note"])
        self.assertIn(f"claude attach {a['short_id']}", out["note"])
        self.assertEqual(self.get(cid)["ask"]["text"], "the plan")

    def test_a_users_ask_survives_end_and_finish(self):
        w = self.workers()
        cid = self.add_card(ask={"type": "fix", "text": "the user's own", "role": "implement"})
        self.start(w, card_id=cid)
        w.end(cid, "implement")
        w.finish(cid)
        self.assertEqual(self.get(cid)["ask"]["text"], "the user's own")

    def test_a_new_session_or_finish_closes_the_workers_open_question(self):
        w = self.workers()
        a = self.start(w)
        self.write_result(a["card"], "question.json", {"text": "Three or five?"})
        self.assertEqual(w.check()[0]["did"], "asked")
        w.start(a["card"], "implement", prompt="Continue.", resume=True)
        self.assertIsNone(self.get(a["card"])["ask"])
        with self.assertRaisesRegex(WorkerError, "no question from a worker"):
            w.answer(a["card"], "x")

    def test_finish_closes_the_workers_open_question(self):
        w = self.workers()
        a = self.start(w)
        self.write_result(a["card"], "question.json", {"text": "Three or five?"})
        w.check()
        w.finish(a["card"])
        self.assertIsNone(self.get(a["card"])["ask"])

    def test_the_attach_ask_is_raised_once_the_users_ask_is_closed(self):
        w = self.workers()
        cid = self.add_card(ask={"type": "confirm", "text": "the plan"})
        a = self.start(w, card_id=cid)
        self.set_agent(a["short_id"], status="waiting")
        self.assertEqual(w.check()[0]["did"], "nothing")
        Store(self.data, self.cfg, self.clock).update(cid, {"ask": None}, by="user")
        self.clock.advance(5)
        self.assertEqual(w.check()[0]["did"], "asked")
        self.assertIn(f"claude attach {a['short_id']}", self.get(cid)["ask"]["text"])
        self.assertEqual(resources.Registry(self.data).read()[0]["blocked_since"], "2026-01-05T09:00:00Z")
        self.clock.advance(25)  # 30 minutes from the first sight of the prompt, not from the ask
        self.assertEqual(w.check()[0]["did"], "paused")

    def test_idle_with_no_result_file_is_paused_after_60_minutes(self):
        w = self.workers()
        a = self.start(w)
        self.set_agent(a["short_id"], state="blocked", status="idle")  # its last message was a question
        self.assertEqual(w.check()[0], {"card": a["card"], "role": "implement", "state": "idle", "did": "nothing"})
        self.clock.advance(59)
        self.assertEqual(w.check()[0]["did"], "nothing")
        self.clock.advance(1)
        out = w.check()[0]
        self.assertEqual(out["did"], "paused")
        saved = self.get(a["card"])["sessions"]["implement"]
        self.assertEqual(saved["state"], "paused")
        self.assertIn("paused, can resume", saved["why"])
        self.assertIsNone(self.get(a["card"]).get("ask"))

    def test_an_unknown_listing_keeps_the_block_clock_and_its_ask(self):
        w = self.workers()
        a = self.start(w)
        self.set_agent(a["short_id"], status="waiting")
        self.assertEqual(w.check()[0]["did"], "asked")
        since = resources.Registry(self.data).read()[0]["blocked_since"]
        self.clock.advance(20)
        with mock.patch("autopilot.sessions.list_all", return_value=None):  # the listing failed: says nothing
            self.assertEqual(w.check()[0]["state"], "unknown")
        self.assertEqual(self.get(a["card"])["ask"]["reason"], "permission_prompt")
        self.assertEqual(resources.Registry(self.data).read()[0]["blocked_since"], since)
        self.clock.advance(10)
        self.assertEqual(w.check()[0]["did"], "paused")  # 30 minutes from the first sight of the prompt

    def test_an_unknown_listing_keeps_the_idle_clock(self):
        w = self.workers()
        a = self.start(w)
        self.set_agent(a["short_id"], status="idle")
        w.check()
        self.clock.advance(50)
        with mock.patch("autopilot.sessions.list_all", return_value=None):
            self.assertEqual(w.check()[0]["state"], "unknown")
        self.clock.advance(10)
        self.assertEqual(w.check()[0]["did"], "paused")  # 60 minutes from the first sight of the idle session

    def test_working_again_resets_the_idle_clock(self):
        w = self.workers()
        a = self.start(w)
        self.set_agent(a["short_id"], state="done", status="idle")
        w.check()
        self.clock.advance(50)
        self.set_agent(a["short_id"], state="working", status="busy")
        w.check()
        self.set_agent(a["short_id"], state="done", status="idle")
        self.clock.advance(50)
        w.check()
        self.clock.advance(50)
        self.assertEqual(w.check()[0]["did"], "nothing")
        self.clock.advance(10)
        self.assertEqual(w.check()[0]["did"], "paused")


class WorkerAskTableTest(WorkerCase):
    """One rule for every ask a worker puts on its card (criterion 5, added after review round 5): it never
    outlives its cause. Each row: the ask on the card, a path, the ask expected afterwards. "user" is the user's own
    ask and "died" the "died twice" ask: no path may close them."""

    def kind(self, ask):
        if ask is None:
            return None
        text = ask.get("text") or ""
        if text == "mine":
            return "user"
        if "died twice" in text:
            return "died"
        if ask.get("type") == "answer":
            return "question"
        if "--resume" in text:
            return "paused"
        if "claude attach" in text:
            return "attach"
        return f"unexpected: {ask}"

    # -- the ask on the card before the path; each returns (workers, card id)

    def setup_attach(self, cid):
        w = self.workers()
        rec = self.start(w, card_id=cid)
        self.set_agent(rec["short_id"], status="waiting")
        w.check()
        return w

    def setup_attach_clock_lost(self, cid):
        """The attach ask is on the card, but the record lost its blocked clock (a hand edit, or an older file)."""
        w = self.setup_attach(cid)
        with resources.Registry(self.data).locked() as records:
            records[0]["blocked_since"] = None
        return w

    def setup_paused(self, cid):
        w = self.setup_attach(cid)
        self.clock.advance(30)
        w.check()
        return w

    def setup_question(self, cid):
        w = self.workers()
        self.start(w, card_id=cid)
        self.write_result(cid, "question.json", {"text": "Three or five?"})
        w.check()
        return w

    def setup_died(self, cid):
        w = self.workers()
        rec = self.start(w, card_id=cid)
        self.drop_agent(rec["short_id"])
        w.check()
        self.drop_agent(resources.Registry(self.data).read()[0]["short_id"])
        w.check()
        return w

    def setup_review_question(self, cid):
        """Another role's question: a review worker asked and was stopped."""
        w = self.workers()
        self.start(w, card_id=cid, role="review")
        q = self.write_result(cid, "question.json", {"text": "Three or five?"})
        w.check()
        old = (T0 - timedelta(hours=1)).timestamp()
        os.utime(q, (old, old))  # older than the next worker's start, so it is not that worker's question
        return w

    def setup_user(self, cid):
        Store(self.data, self.cfg, self.clock).update(cid, {"ask": {"type": "fix", "text": "mine",
                                                                    "role": "implement"}}, by="user")
        w = self.workers()
        self.start(w, card_id=cid)
        return w

    # -- the paths

    def rec(self):
        return resources.Registry(self.data).read()[0]

    def path_start(self, w, cid):
        w.start(cid, "implement", cwd=self.wt, prompt="Again.")

    def path_resume(self, w, cid):
        w.start(cid, "implement", prompt="Continue.", resume=True)

    def path_answer(self, w, cid):
        w.answer(cid, "Three.")

    def path_in_place_resume(self, w, cid):
        self.drop_agent(self.rec()["short_id"])
        self.assertEqual(w.check()[0]["did"], "resumed")

    def path_parked_resume(self, w, cid):
        w.park(cid, "implement", "x", usage_file=usage_file(self.tmp / "u.json", 95, fetched=self.clock()))
        w.start(cid, "implement", prompt="Continue.", resume=True)

    def path_prompt_answered(self, w, cid):
        """The user answered the prompt: the session runs on, nothing is stopped or started."""
        self.set_agent(self.rec()["short_id"], status="busy")
        self.assertEqual(w.check()[0]["state"], "running")

    def path_start_block_answered(self, w, cid):
        """A new implement worker starts, waits at a permission prompt, and the user answers the prompt."""
        rec = w.start(cid, "implement", cwd=self.wt, prompt="Again.")
        self.set_agent(rec["short_id"], status="waiting")
        self.assertEqual(w.check()[0]["state"], "blocked")
        self.set_agent(rec["short_id"], status="busy")
        self.assertEqual(w.check()[0]["state"], "running")

    def path_done(self, w, cid):
        self.write_result(cid)
        self.assertEqual(w.check()[0]["did"], "ended")

    def path_end(self, w, cid):
        w.end(cid, "implement")

    def path_end_then_resume(self, w, cid):
        w.end(cid, "implement")
        self.path_resume(w, cid)

    def path_end_then_finish(self, w, cid):
        w.end(cid, "implement")
        w.finish(cid)

    def path_finish(self, w, cid):
        w.finish(cid)

    def path_pause(self, w, cid):
        self.set_agent(self.rec()["short_id"], status="waiting")
        w.check()
        self.clock.advance(30)
        self.assertEqual(w.check()[0]["did"], "paused")

    def path_question(self, w, cid):
        self.write_result(cid, "question.json", {"text": "Three or five?"})
        self.assertEqual(w.check()[0]["state"], "question")

    ROWS = [
        # attach: the worker is on record, waiting at a permission prompt
        ("attach", "prompt_answered", None), ("attach", "done", None),
        ("attach", "in_place_resume", None), ("attach", "parked_resume", None), ("attach", "end", None),
        ("attach_clock_lost", "in_place_resume", None),
        ("attach", "end_then_finish", None), ("attach", "pause", "paused"), ("attach", "question", "question"),
        # paused: stopped after 30 minutes at the prompt, no worker on record
        ("paused", "start", None), ("paused", "resume", None), ("paused", "finish", None),
        # question: the worker asked and was stopped, no worker on record
        ("question", "start", None), ("question", "resume", None), ("question", "answer", None),
        ("question", "finish", None),
        # never closed by any path
        ("died", "start", "died"), ("died", "resume", "died"), ("died", "finish", "died"),
        ("died", "start_block_answered", "died"), ("review_question", "start_block_answered", "question"),
        ("user", "in_place_resume", "user"), ("user", "parked_resume", "user"), ("user", "end", "user"),
        ("user", "end_then_resume", "user"), ("user", "end_then_finish", "user"), ("user", "pause", "user"),
        ("user", "question", "user"), ("user", "prompt_answered", "user"), ("user", "done", "user"),
    ]

    def test_every_worker_ask_against_every_path(self):
        for before, path, after in self.ROWS:
            with self.subTest(ask=before, path=path):
                for r in resources.Registry(self.data).read():  # each row starts with no worker on record
                    self.workers().end(r["card"], r["role"])
                self.clock = Clock()
                cid = self.add_card()
                w = getattr(self, f"setup_{before}")(cid)
                shown = {"attach_clock_lost": "attach", "review_question": "question"}.get(before, before)
                self.assertEqual(self.kind(self.get(cid).get("ask")), shown)
                getattr(self, f"path_{path}")(w, cid)
                self.assertEqual(self.kind(self.get(cid).get("ask")), after)


# -- 6. transcripts --------------------------------------------------------------------------------------------------

class TranscriptsTest(WorkerCase):
    def original(self, rec) -> Path:
        return transcripts.find(self.fake_home / "projects", rec["session_id"])

    def test_always_keeps_a_gzipped_copy_each_round(self):
        w = self.workers()
        a = self.start(w)
        self.write_result(a["card"])
        out = w.check()[0]
        archive = Path(out["archive"])
        self.assertEqual(archive, self.data / "sessions" / a["card"] / f"implement-{a['session_id']}.jsonl.gz")
        self.assertEqual(gzip.decompress(archive.read_bytes()), self.original(a).read_bytes())
        self.assertTrue(self.original(a).exists())  # the original stays, so the session can resume

    def test_never_keeps_only_the_summary_files(self):
        w = self.workers(cfg=Config(mode="live", keep_transcripts="never"))
        a = self.start(w)
        old = self.data / "sessions" / a["card"] / "review-old.jsonl.gz"
        old.write_bytes(gzip.compress(b"{}\n"))
        self.write_result(a["card"])
        out = w.check()[0]
        self.assertIsNone(out["archive"])
        self.assertEqual(sorted(p.name for p in (self.data / "sessions" / a["card"]).iterdir()),
                         ["handback.json", "settings-implement.json"])

    def test_days_prune_archives_older_than_n_days(self):
        cfg = Config(mode="live", keep_transcripts="90d")
        w = self.workers(cfg=cfg)
        a = self.start(w)
        folder = self.data / "sessions" / a["card"]
        old, recent = folder / "implement-old.jsonl.gz", folder / "implement-recent.jsonl.gz"
        for p, days in ((old, 91), (recent, 89)):
            p.write_bytes(gzip.compress(b"{}\n"))
            t = (T0 - timedelta(days=days)).timestamp()
            os.utime(p, (t, t))
        w.end(a["card"], "implement")
        names = sorted(p.name for p in folder.glob("*.jsonl.gz"))
        self.assertEqual(names, sorted(["implement-recent.jsonl.gz", f"implement-{a['session_id']}.jsonl.gz"]))

    def test_finish_in_live_mode_makes_the_last_copy_and_removes_the_original(self):
        w = self.workers()
        a = self.start(w)
        w.end(a["card"], "implement")
        orig = self.original(a)
        with orig.open("a") as f:
            f.write('{"type": "assistant", "text": "last words"}\n')
        out = w.finish(a["card"])
        self.assertEqual(out["removed"], [a["session_id"]])
        self.assertIn(["rm", a["session_id"]], [c["argv"] for c in self.calls()])
        self.assertFalse(orig.exists())
        self.assertIn(b"last words", gzip.decompress(Path(out["archived"][0]).read_bytes()))
        self.assertTrue(self.get(a["card"])["sessions"]["implement"]["removed"])

    def test_finish_archives_every_id_of_a_role(self):
        w = self.workers()
        a = self.start(w)
        self.drop_agent(a["short_id"])
        self.assertEqual(w.check()[0]["did"], "resumed")  # in place, under a new id
        b = resources.Registry(self.data).read()[0]
        w.end(a["card"], "implement")
        self.assertEqual(self.get(a["card"])["sessions"]["implement"]["ids"], [a["session_id"], b["session_id"]])
        with self.original(b).open("a") as f:
            f.write('{"type": "assistant", "text": "last words"}\n')
        out = w.finish(a["card"])
        folder = self.data / "sessions" / a["card"]
        self.assertEqual(sorted(out["archived"]), sorted(str(folder / f"implement-{s}.jsonl.gz")
                                                         for s in (a["session_id"], b["session_id"])))
        newest = folder / f"implement-{b['session_id']}.jsonl.gz"
        self.assertIn(b"last words", gzip.decompress(newest.read_bytes()))

    def test_finish_never_writes_outside_the_data_folder_for_an_unknown_role(self):
        w = self.workers()
        a = self.start(w)
        w.end(a["card"], "implement")
        sessions = dict(self.get(a["card"])["sessions"], **{"../../../escaped": {"ids": [a["session_id"]]}})
        Store(self.data, self.cfg, self.clock).update(a["card"], {"sessions": sessions}, by="test")
        out = w.finish(a["card"])
        self.assertEqual(out["archived"], [str(self.data / "sessions" / a["card"] / f"implement-{a['session_id']}"
                                                                                  ".jsonl.gz")])
        self.assertEqual([p for p in self.tmp.rglob("*.gz") if self.data not in p.parents], [])
        with self.assertRaisesRegex(ValueError, "not a role"):
            transcripts.archive_path(self.data, a["card"], "..", a["session_id"])
        with self.assertRaisesRegex(ValueError, "not a role"):
            transcripts.archive_path(self.data, a["card"], "implement/../../x", a["session_id"])

    def test_finish_marks_only_roles_whose_originals_are_gone(self):
        w = self.workers()
        a = self.start(w)
        w.end(a["card"], "implement")
        sessions = dict(self.get(a["card"])["sessions"], notes={"kept": "by the user"}, review={"state": "paused"},
                        memo="a plain text")
        Store(self.data, self.cfg, self.clock).update(a["card"], {"sessions": sessions}, by="test")
        self.assertEqual(w.finish(a["card"])["removed"], [a["session_id"]])
        done = self.get(a["card"])["sessions"]
        self.assertEqual((done["notes"], done["review"], done["memo"]),
                         ({"kept": "by the user"}, {"state": "paused"}, "a plain text"))
        self.assertEqual((done["implement"]["state"], done["implement"]["removed"]), ("finished", True))
        self.assertEqual((done["implement"]["ids"], done["implement"]["id"]), ([a["session_id"]], a["session_id"]))

    def test_a_failed_claude_rm_is_not_listed_as_removed(self):
        w = self.workers()
        a = self.start(w)
        w.end(a["card"], "implement")
        with mock.patch.dict(os.environ, {"FAKE_RM_FAIL": "1"}):
            out = w.finish(a["card"])
        self.assertEqual(out["removed"], [])
        self.assertEqual(len(out["failed"]), 1)
        self.assertIn("cannot delete", out["failed"][0])
        self.assertTrue(self.original(a).exists())
        self.assertNotIn("removed", self.get(a["card"])["sessions"]["implement"])

    def test_claude_rm_failure_raises(self):
        with mock.patch.dict(os.environ, {"FAKE_RM_FAIL": "1"}):
            with self.assertRaisesRegex(WorkerError, "claude rm abc exited 1: error: no such session"):
                transcripts.remove_original(LIVE, FAKE, "abc")

    def test_finish_waits_for_the_worker_to_end(self):
        w = self.workers()
        a = self.start(w)
        with self.assertRaisesRegex(WorkerError, "still has a worker"):
            w.finish(a["card"])

    def test_the_jsonl_folder_is_passed_in(self):
        self.assertEqual(transcripts.default_projects(), self.home / ".claude" / "projects")
        self.assertIsNone(transcripts.find(self.tmp / "missing", "abc"))


# -- 7. shadow mode ----------------------------------------------------------------------------------------------------

class ShadowModeTest(WorkerCase):
    cfg = Config()  # shadow, the default

    def test_starting_is_refused_and_nothing_starts(self):
        cid = self.add_card()
        with self.assertRaisesRegex(ShadowModeError, "shadow mode: refusing to start a implement worker"):
            self.workers().start(cid, "implement", cwd=self.wt)
        self.assertEqual(self.calls(), [])
        self.assertEqual(resources.Registry(self.data).read(), [])
        self.assertFalse(transcripts.run_dir(self.data, cid).exists())

    def test_resume_and_answer_are_refused(self):
        cid = self.add_card(ask={"type": "answer", "from": "worker", "role": "implement", "session_id": "s"})
        with self.assertRaises(ShadowModeError):
            self.workers().start(cid, "implement", cwd=self.wt, resume=True)
        with self.assertRaises(ShadowModeError):
            self.workers().answer(cid, "yes")
        self.assertEqual(self.calls(), [])

    def test_claude_rm_is_refused(self):
        with self.assertRaisesRegex(ShadowModeError, "claude rm"):
            transcripts.remove_original(self.cfg, FAKE, "abc")
        self.assertEqual(self.calls(), [])

    def test_listing_classifying_ending_and_archiving_work(self):
        a = self.start(self.workers(cfg=LIVE))  # a worker started in live mode, then the user switched back
        shadow = self.workers()
        self.assertEqual(shadow.list()[0]["state"], "running")
        self.write_result(a["card"])
        out = shadow.check()[0]
        self.assertEqual(out["did"], "ended")
        self.assertTrue(Path(out["archive"]).is_file())
        fin = shadow.finish(a["card"])
        self.assertEqual((len(fin["archived"]), fin["removed"]), (1, []))
        self.assertIn("shadow mode", fin["note"])
        self.assertNotIn("rm", [c["argv"][0] for c in self.calls()])
        self.assertTrue(self.original_exists(a))

    def original_exists(self, rec) -> bool:
        return transcripts.find(self.fake_home / "projects", rec["session_id"]) is not None

    def test_a_dead_worker_is_not_resumed_in_shadow_mode(self):
        a = self.start(self.workers(cfg=LIVE))
        self.drop_agent(a["short_id"])
        out = self.workers().check()[0]
        self.assertEqual((out["state"], out["did"]), ("dead", "nothing"))
        self.assertIn("shadow mode", out["note"])
        self.assertEqual(len(self.bg_calls()), 1)
        self.assertEqual(len(self.discovery_calls()), 1)  # the refusal comes first: no `claude -p` in shadow mode
        self.assertEqual(resources.Registry(self.data).read()[0].get("resumes", 0), 0)  # a refusal is no resume
        self.assertEqual(self.workers().check()[0]["did"], "nothing")

    def test_a_parked_worker_stays_parked_in_shadow_mode(self):
        w = self.workers(cfg=LIVE)
        a = self.start(w)
        w.park(a["card"], "implement", "x", usage_file=usage_file(self.tmp / "u.json", 95))
        self.clock.advance(4 * 60)  # past the reset: it would resume in live mode
        fresh = usage_file(self.tmp / "fresh.json", 3, resets_at="2026-01-05T17:00:00+00:00", fetched=self.clock())
        out = self.workers(cfg=Config()).check(usage_file=fresh)[0]
        self.assertEqual((out["state"], out["did"]), ("parked", "nothing"))
        self.assertIn("shadow mode", out["note"])
        self.assertEqual(len(resources.Registry(self.data).read()), 1)  # not stopped as a worker that died twice
        self.assertIsNone(self.get(a["card"]).get("ask"))
        self.assertEqual(len(self.bg_calls()), 1)


if __name__ == "__main__":
    unittest.main()
