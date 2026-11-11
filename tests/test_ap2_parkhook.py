"""`ap2 hook-park` (design 12, workers): the PreToolUse hook that stops a v2 worker from calling tools while a usage
window is at the park line, except for its own `ap2 worker park` command. Unit tests call `parkhook.decide` with a
usage dict; the command tests run the real `bin/ap2 hook-park` with a payload on stdin, a fake usage file, and a
temp HOME, so nothing reaches the real ~/.claude.json."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.helpers import LIB, ROOT
from assistant import parkhook
from assistant import workers as workers_mod
from autopilot import park as v1_park

AP2 = str(ROOT / "bin" / "ap2")
RESET = "2026-01-05T12:00:00+00:00"
RULES = parkhook.PARK_RULES
WORKER = {"AP2_ROLE": "implement", "AP2_CARD": "C1"}


def usage(percent=95, age_min=5, ok=True) -> dict:
    """What `autopilot.budget.read` returns: one session window at `percent`."""
    if not ok:
        return {"ok": False, "error": "unreadable"}
    return {"ok": True, "age_min": age_min, "stale": age_min > RULES["stale_min"],
            "windows": [{"kind": "session", "model": None, "percent": percent, "resets_at": RESET}],
            "credits_used_usd": 0, "credits_limit_usd": 0, "spend_limit_reached": False}


def bash(cmd: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


def denied(out) -> bool:
    return bool(out) and out["hookSpecificOutput"]["permissionDecision"] == "deny"


class DecideTest(unittest.TestCase):
    def test_no_opinion_outside_a_v2_worker(self):
        envs = {"no AP2_ROLE": {},
                "v1 worker only": {"AUTOPILOT_ROLE": "worker"},
                "v1 worker with a card": {"AUTOPILOT_ROLE": "worker", "AP2_CARD": "C1"},
                "unknown role": {"AP2_ROLE": "nosuchrole", "AP2_CARD": "C1"},
                "role but no card": {"AP2_ROLE": "implement"},
                "role and an empty card": {"AP2_ROLE": "implement", "AP2_CARD": ""}}
        for name, env in envs.items():
            with self.subTest(name):
                self.assertIsNone(parkhook.decide({"tool_name": "Read", "tool_input": {}}, env, usage(95), RULES))

    def test_every_tool_is_denied_at_the_line(self):
        payloads = {"Read": {"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}},
                    "Edit": {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/x"}},
                    "Bash ls": bash("ls"),
                    "MCP tool": {"tool_name": "mcp__Tracker__read_issue", "tool_input": {}},
                    "no tool_input": {"tool_name": "Bash"}}
        for name, payload in payloads.items():
            for role in workers_mod.ROLES:
                with self.subTest(name, role=role):
                    out = parkhook.decide(payload, {"AP2_ROLE": role, "AP2_CARD": "C1"}, usage(95), RULES)
                    self.assertTrue(denied(out), out)
                    self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PreToolUse")

    def test_exactly_the_park_percent_is_at_the_line(self):
        self.assertTrue(denied(parkhook.decide(bash("ls"), WORKER, usage(RULES["park_pct"]), RULES)))
        self.assertIsNone(parkhook.decide(bash("ls"), WORKER, usage(RULES["park_pct"] - 1), RULES))

    def test_no_opinion_under_the_line_or_when_usage_is_unknown(self):
        cases = {"50%": usage(50), "stale": usage(95, age_min=RULES["stale_min"] + 1), "not ok": usage(ok=False),
                 "no windows": dict(usage(95), windows=[])}
        for name, u in cases.items():
            with self.subTest(name):
                self.assertIsNone(parkhook.decide(bash("ls"), WORKER, u, RULES))

    def test_only_its_own_park_command_passes(self):
        own = f"{AP2} worker park C1 implement"
        allowed = {"bare": own, "with a note": own + ' --note "x y"', "quoted program": f'"{AP2}" worker park C1 '
                   'implement --note "x y"', "single-quoted note": own + " --note 'halfway; next: tests'"}
        for name, cmd in allowed.items():
            with self.subTest(name, cmd=cmd):
                self.assertTrue(parkhook.is_park_command(cmd, "C1", "implement"))
                self.assertIsNone(parkhook.decide(bash(cmd), WORKER, usage(95), RULES))
        refused = {"another card": f"{AP2} worker park C2 implement",
                   "another role": f"{AP2} worker park C1 review",
                   "--usage": own + " --usage /tmp/u",
                   "--data": own + " --data /tmp/d",
                   "--by": own + " --by me",
                   "--note= form": own + " --note=x",
                   "two notes": own + " --note a --note b",
                   "a note with no text": own + " --note",
                   "a different ap2": f"/tmp/other/bin/ap2 worker park C1 implement",
                   "bare ap2": "ap2 worker park C1 implement",
                   "v1's ap": f"{ROOT / 'bin' / 'ap'} worker park C1 implement",
                   "another subcommand": f"{AP2} worker end C1 implement",
                   "; ls": own + "; ls", "&& ls": own + " && ls", "| cat": own + " | cat",
                   "$(ls)": own + ' --note "$(ls)"', "backtick": own + " --note `ls`",
                   "newline": own + "\nls", "background": own + " --note &", "redirect": own + " --note >",
                   "unbalanced quote": own + ' --note "x', "empty": ""}
        for name, cmd in refused.items():
            with self.subTest(name, cmd=cmd):
                self.assertFalse(parkhook.is_park_command(cmd, "C1", "implement"))
                self.assertTrue(denied(parkhook.decide(bash(cmd), WORKER, usage(95), RULES)))

    def test_the_park_command_passes_only_as_bash(self):
        payload = {"tool_name": "Read", "tool_input": {"command": f"{AP2} worker park C1 implement"}}
        self.assertTrue(denied(parkhook.decide(payload, WORKER, usage(95), RULES)))

    def test_the_reason_names_the_workers_own_park_command(self):
        why = parkhook.decide(bash("ls"), WORKER, usage(95), RULES)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn(f"{AP2} worker park C1 implement --note", why)
        self.assertIn("95%", why)
        self.assertIn("session", why)
        review = parkhook.decide(bash("ls"), {"AP2_ROLE": "review", "AP2_CARD": "C9"}, usage(95), RULES)
        self.assertIn(f"{AP2} worker park C9 review --note",
                      review["hookSpecificOutput"]["permissionDecisionReason"])

    def test_a_subagent_is_told_to_stop_and_reply(self):
        payload = dict(bash("ls"), agent_id="agent-1")
        why = parkhook.decide(payload, WORKER, usage(95), RULES)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("parked for quota", why)
        self.assertIn("95%", why)
        self.assertNotIn("worker park", why)

    def test_the_copies_match_the_workers_module_and_v1(self):
        self.assertEqual(parkhook.PARK_RULES, workers_mod.PARK_RULES)
        self.assertEqual(parkhook.ROLES, workers_mod.ROLES)
        self.assertEqual(parkhook.PUNCT, v1_park.PUNCT)
        self.assertEqual(parkhook.ap2_bin(), str(workers_mod.package_root() / "bin" / "ap2"))


class HookCommandTest(unittest.TestCase):
    """The real `bin/ap2 hook-park` as Claude Code runs it: a payload on stdin, the verdict (or nothing) on stdout,
    exit 0 always."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-parkhook-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.usage = self.usage_file(95)

    def usage_file(self, percent, name="claude.json") -> Path:
        path = self.tmp / name
        path.write_text(json.dumps({"cachedUsageUtilization": {
            "fetchedAtMs": int(time.time() * 1000),
            "utilization": {"limits": [{"kind": "session", "percent": percent, "resets_at": RESET}]}}}))
        return path

    def env(self, **extra) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AUTOPILOT_", "AP2_"))}
        env.update(HOME=str(self.home), **extra)
        return env

    def run_hook(self, stdin=b"", env=None, usage=None, args=None, **kw):
        argv = [sys.executable, AP2, "hook-park"] + (args if args is not None else ["--usage", str(usage or
                                                                                                 self.usage)])
        if stdin is None:
            kw["stdin"] = subprocess.DEVNULL
            kw["preexec_fn"] = lambda: os.close(0)  # stdin closed, not just empty
        else:
            kw["input"] = stdin
        return subprocess.run(argv, capture_output=True, env=env if env is not None else self.env(**WORKER),
                              timeout=60, **kw)

    def test_a_parked_worker_gets_the_deny_and_others_get_nothing(self):
        r = self.run_hook(json.dumps(bash("ls")).encode())
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn(f"{AP2} worker park C1 implement --note", out["hookSpecificOutput"]["permissionDecisionReason"])
        for name, env in {"user session": self.env(), "v1 worker": self.env(AUTOPILOT_ROLE="worker")}.items():
            with self.subTest(name):
                r = self.run_hook(json.dumps(bash("ls")).encode(), env=env)
                self.assertEqual((r.returncode, r.stdout), (0, b""))
        r = self.run_hook(json.dumps(bash(f"{AP2} worker park C1 implement --note 'x'")).encode())
        self.assertEqual((r.returncode, r.stdout), (0, b""))
        r = self.run_hook(json.dumps(bash("ls")).encode(), usage=self.usage_file(50, "low.json"))
        self.assertEqual((r.returncode, r.stdout), (0, b""))

    def test_errors_never_break_the_session(self):
        cases = {"invalid JSON": dict(stdin=b"{not json"),
                 "a JSON list": dict(stdin=b"[1, 2]"),
                 "empty stdin": dict(stdin=b""),
                 "closed stdin": dict(stdin=None),
                 "not UTF-8": dict(stdin=b"\xff\xfe{"),
                 "missing usage file": dict(stdin=json.dumps(bash("ls")).encode(), usage=self.tmp / "nope.json"),
                 "past 1 MiB": dict(stdin=json.dumps(dict(bash("ls"), pad="x" * (1 << 20))).encode()),
                 # Valid JSON in its first 1 MiB and one byte more: still not read.
                 "a valid payload padded past 1 MiB": dict(stdin=json.dumps(bash("ls")).encode().ljust(
                     parkhook.MAX_STDIN + 1)),
                 "an unknown argument": dict(stdin=json.dumps(bash("ls")).encode(), args=["--nope"]),
                 "--usage with no path": dict(stdin=json.dumps(bash("ls")).encode(), args=["--usage"])}
        for name, kw in cases.items():
            with self.subTest(name):
                r = self.run_hook(**kw)
                self.assertEqual((r.returncode, r.stdout), (0, b""), r.stderr)

    def test_it_writes_nothing(self):
        data = self.tmp / "no-such-data"
        r = self.run_hook(json.dumps(bash("ls")).encode(), env=self.env(AP2_DATA=str(data), **WORKER))
        self.assertTrue(denied(json.loads(r.stdout)))
        self.assertFalse(data.exists())
        self.assertEqual(list(self.home.iterdir()), [])  # not v1's data root under HOME either
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["claude.json", "home"])

    def test_it_stays_light(self):
        """The hook runs before every tool call: `parkhook` never loads the workers, store or config modules."""
        code = ("import json, sys; sys.path.insert(0, sys.argv[1]); from assistant import parkhook; "
                "parkhook.main(['--usage', sys.argv[2]], sys.stdin); "
                "print(json.dumps(sorted(m for m in sys.modules if m.startswith('assistant'))), file=sys.stderr)")
        r = subprocess.run([sys.executable, "-c", code, str(LIB), str(self.usage)], capture_output=True,
                           input=json.dumps(bash("ls")).encode(), env=self.env(**WORKER), timeout=60)
        self.assertTrue(denied(json.loads(r.stdout)), r.stderr)
        self.assertEqual(json.loads(r.stderr), ["assistant", "assistant.parkhook"])

    def test_the_real_command_stays_light(self):
        """`bin/ap2 hook-park` as Claude Code runs it routes before `bin/ap2` imports `cli`, so none of the heavy
        modules load on a worker's tool call."""
        r = subprocess.run([sys.executable, "-X", "importtime", AP2, "hook-park", "--usage", str(self.usage)],
                           capture_output=True, input=json.dumps(bash("ls")).encode(), env=self.env(**WORKER),
                           timeout=60)
        self.assertEqual(r.returncode, 0)
        self.assertTrue(denied(json.loads(r.stdout)))
        imported = {line.rsplit("|", 1)[-1].strip() for line in r.stderr.decode().splitlines()
                    if line.startswith("import time:")}
        self.assertIn("assistant.parkhook", imported)  # the -X importtime list was read
        for name in ("assistant.cli", "assistant.workers", "assistant.store", "assistant.config", "autopilot.workers"):
            with self.subTest(name):
                self.assertNotIn(name, imported)

    def test_the_ap2_command_routes_before_the_full_parser(self):
        """`bin/ap2 hook-park` is handled before `cli` builds its parser: an argument the parser would reject (exit
        2) still gives exit 0, and `ap2`'s usage text lists the command."""
        r = self.run_hook(json.dumps(bash("ls")).encode(), args=["--usage", str(self.usage), "--whatever"])
        self.assertEqual((r.returncode, r.stdout), (0, b""))
        r = subprocess.run([sys.executable, AP2], capture_output=True, env=self.env(), timeout=60)
        self.assertIn("hook-park", r.stderr.decode())


if __name__ == "__main__":
    unittest.main()
