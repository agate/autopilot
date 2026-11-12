"""`ap2 import-v1`: v1 projects become cards, read-only (design 10, 12 "v1 import")."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT
from tests.test_ap2_cli import temp_inside_the_accounts_claude
from tests.test_ap2_store import card as store_card
from assistant import cards, cli, datadir, importv1
from assistant.store import Store

FIXTURE = ROOT / "tests" / "fixtures" / "v1-data"
SHOP_TASKS = ("PROJ-1", "PROJ-2", "PROJ-3", "PROJ-4", "PROJ-5", "PROJ-6")


def snapshot(root: Path) -> dict:
    """Every file and folder under `root`: a file's bytes and mtime, a folder's mtime."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        out[str(d.relative_to(root)) + "/"] = (None, d.stat().st_mtime_ns)
        for name in filenames:
            p = d / name
            out[str(p.relative_to(root))] = (p.read_bytes(), p.stat().st_mtime_ns)
    return out


class ImportV1Test(unittest.TestCase):
    def setUp(self):
        if temp_inside_the_accounts_claude():
            self.skipTest("the temp folder is inside the account's ~/.claude, so ap2 refuses every temp data folder")
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-import-v1-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.v1 = self.tmp / "v1"
        shutil.copytree(FIXTURE, self.v1)
        self.data = self.tmp / "data"
        home = self.tmp / "home"
        home.mkdir()
        env = {k: v for k, v in os.environ.items() if k not in ("AP2_DATA", "AUTOPILOT_DATA")}
        # The real home and the real v1 data folder are never reached: AUTOPILOT_DATA points at an empty temp path.
        env.update(HOME=str(home), AUTOPILOT_DATA=str(self.tmp / "no-v1-here"))
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- helpers ------------------------------------------------------------------------------------------------

    def run_cli(self, *args, source=True):
        argv = ["import-v1", "--data", str(self.data)]
        if source:
            argv += ["--from", str(self.v1)]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv + list(args))
        return code, out.getvalue(), err.getvalue()

    def ok(self, *args, **kw):
        code, out, err = self.run_cli(*args, **kw)
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        return json.loads(out)

    def fails(self, *args, **kw):
        code, out, err = self.run_cli(*args, **kw)
        self.assertEqual(code, 1, out)
        self.assertEqual(out, "")
        lines = err.splitlines()
        self.assertEqual(len(lines), 1, err)
        self.assertTrue(lines[0].startswith("ap2: "), err)
        return lines[0]

    def by_key(self):
        return {c["key"]: c for c in Store(self.data).list()}

    def actions(self, report):
        return {c["key"]: c["action"] for p in report["projects"] for c in p["cards"]}

    def set_config(self, text):
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "config.yaml").write_text(text, encoding="utf-8")

    def edit_state(self, folder, change):
        path = self.v1 / folder / "state.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        change(state)
        path.write_text(json.dumps(state), encoding="utf-8")

    def card_files(self):
        return sorted((self.data / "items").glob("*/*.json")) if (self.data / "items").exists() else []

    # -- 1. a v1 project becomes cards ------------------------------------------------------------------------

    def test_a_project_becomes_a_project_card_code_cards_and_question_cards(self):
        report = self.ok("--project", "shop")
        got = self.by_key()
        self.assertEqual(sorted(got), sorted(["v1:shop", "v1:shop:D1", "v1:shop:D2"]
                                             + [f"v1:shop:{t}" for t in SHOP_TASKS]))
        for c in got.values():
            self.assertEqual(c["feed"], "v1-import")
            self.assertEqual(c["project"], "shop")
            self.assertEqual(c["audit"][0]["by"], "v1-import")

        proj = got["v1:shop"]
        self.assertEqual((proj["kind"], proj["lane"], proj["pri"], proj["title"]), ("project", "running", "P2", {"en": "shop"}))
        self.assertIsNone(proj.get("parent"))
        self.assertEqual(proj["source"]["record"], {
            "repo": "/srv/repos/shop", "branch_pattern": "{prefix}.{task}.{short}", "branch_prefix": "dev",
            "folder": str(self.v1 / "projects" / "shop")})

        lanes = {"PROJ-1": "planned", "PROJ-2": "running", "PROJ-3": "running", "PROJ-4": "needs",
                 "PROJ-5": "done", "PROJ-6": "done"}
        statuses = {"PROJ-1": "todo", "PROJ-2": "in_progress", "PROJ-3": "in_review", "PROJ-4": "blocked",
                    "PROJ-5": "done", "PROJ-6": "skipped"}
        for task in SHOP_TASKS:
            c = got[f"v1:shop:{task}"]
            with self.subTest(task=task):
                self.assertEqual(c["kind"], "code")
                self.assertEqual(c["parent"], proj["id"])
                self.assertEqual(c["lane"], lanes[task])
                self.assertEqual(c["status"], statuses[task])
                self.assertEqual(c["pri"], "P1" if task == "PROJ-2" else "P2")
                self.assertEqual(c["origin"], "found" if task == "PROJ-2" else "source")
                self.assertEqual(sorted(c["source"]["record"]), ["branch", "depends_on", "link", "pr"])
        self.assertEqual(got["v1:shop:PROJ-3"]["source"]["record"], {
            "pr": "https://github.com/org/repo/pull/7", "branch": "dev.PROJ-3.receipt", "depends_on": [],
            "link": "https://example.com/browse/PROJ-3"})
        self.assertEqual(got["v1:shop:PROJ-2"]["source"]["record"]["depends_on"], ["PROJ-1"])

        ask, gate = got["v1:shop:D1"], got["v1:shop:D2"]
        self.assertEqual((ask["kind"], ask["lane"]), ("question", "needs"))
        self.assertEqual(ask["ask"], {"type": "answer", "from": "v1"})
        self.assertEqual(ask["parent"], got["v1:shop:PROJ-4"]["id"])  # its task's card
        self.assertEqual(gate["ask"], {"type": "approve", "from": "v1"})
        self.assertEqual(gate["parent"], proj["id"])  # no task: the project card
        # self, system and answered decisions make no card; the report counts them
        self.assertEqual(report["projects"][0]["not_imported"], {"decisions": 3})
        self.assertEqual(set(self.actions(report).values()), {"add"})
        self.assertEqual(report["refused"], [])
        self.assertFalse(report["dry_run"])
        self.assertEqual(report["from"], str(self.v1))

    def test_a_project_whose_tasks_are_all_done_or_skipped_is_done(self):
        self.ok("--project", "notes")
        got = self.by_key()
        self.assertEqual(got["v1:notes"]["lane"], "done")
        self.assertEqual(got["v1:notes:PROJ-201"]["lane"], "done")
        self.assertEqual(got["v1:notes:PROJ-202"]["lane"], "done")

    # -- 2. v1's files are unchanged byte for byte --------------------------------------------------------------

    def test_v1_files_are_unchanged_after_a_dry_run_an_import_and_a_rerun(self):
        before = snapshot(self.v1)
        self.ok("--dry-run")
        self.assertEqual(snapshot(self.v1), before)
        self.ok()
        self.assertEqual(snapshot(self.v1), before)
        self.ok()
        self.assertEqual(snapshot(self.v1), before)
        self.assertTrue(self.card_files())  # the import did write, into the v2 data folder

    # -- 3. a dry run writes nothing ----------------------------------------------------------------------------

    def test_dry_run_into_an_empty_data_folder_writes_nothing(self):
        report = self.ok("--dry-run")
        self.assertTrue(report["dry_run"])
        self.assertFalse(self.data.exists())  # not even the data folder's layout
        entries = [c for p in report["projects"] for c in p["cards"]]
        self.assertTrue(entries)
        for e in entries:
            self.assertEqual(e["action"], "add")
            self.assertIsNone(e["card"])
            self.assertEqual(e["would_add"]["key"], e["key"])
            self.assertEqual(e["would_add"]["kind"], e["kind"])
            self.assertNotIn("parent", e["would_add"])  # its parent is only a would-add too

    def test_dry_run_after_an_import_reports_unchanged_and_writes_nothing(self):
        self.ok()
        before = snapshot(self.data)
        report = self.ok("--dry-run")
        self.assertEqual(set(self.actions(report).values()), {"unchanged"})
        self.assertEqual(snapshot(self.data), before)

    def test_dry_run_after_a_change_reports_would_change_and_writes_nothing(self):
        self.ok()
        self.edit_state("projects/shop", lambda s: s["tasks"][0].update(status="done"))
        before = snapshot(self.data)
        report = self.ok("--dry-run")
        changed = [c for p in report["projects"] for c in p["cards"] if c["action"] == "update"]
        self.assertEqual([c["key"] for c in changed], ["v1:shop:PROJ-1"])
        self.assertEqual(changed[0]["would_change"], ["lane", "plain", "status"])
        self.assertEqual(snapshot(self.data), before)

    def test_dry_run_into_a_data_folder_with_only_a_config_writes_nothing(self):
        self.set_config("secondary_language: zh\n")
        self.ok("--dry-run")
        self.assertEqual(sorted(p.name for p in self.data.iterdir()), ["config.yaml"])

    def test_dry_run_of_a_new_task_names_the_existing_project_card_as_its_parent(self):
        self.ok("--project", "shop")
        project_id = self.by_key()["v1:shop"]["id"]
        self.edit_state("projects/shop", lambda s: s["tasks"].append(dict(s["tasks"][0], id="PROJ-99")))
        report = self.ok("--dry-run", "--project", "shop")
        added = [c for p in report["projects"] for c in p["cards"] if c["action"] == "add"]
        self.assertEqual([c["key"] for c in added], ["v1:shop:PROJ-99"])
        self.assertEqual(added[0]["would_add"]["parent"], project_id)

    def test_dry_run_with_a_broken_project_exits_1(self):
        (self.v1 / "notes" / "state.json").write_text("{", encoding="utf-8")
        code, out, err = self.run_cli("--dry-run")
        self.assertEqual(code, 1, err)
        self.assertEqual([r["name"] for r in json.loads(out)["refused"]], ["notes"])

    # -- 4. a re-run is safe ------------------------------------------------------------------------------------

    def test_a_rerun_changes_nothing_and_a_status_change_updates_only_that_card(self):
        self.ok()
        first = self.by_key()
        again = self.ok()
        self.assertEqual(set(self.actions(again).values()), {"unchanged"})
        self.assertEqual(self.by_key(), first)

        self.edit_state("projects/shop", lambda s: s["tasks"][0].update(status="in_progress"))
        report = self.ok()
        acts = self.actions(report)
        self.assertEqual([k for k, a in acts.items() if a != "unchanged"], ["v1:shop:PROJ-1"])
        self.assertEqual(acts["v1:shop:PROJ-1"], "update")
        now = self.by_key()
        self.assertEqual(len(now), len(first))  # no second card
        card = now["v1:shop:PROJ-1"]
        self.assertEqual((card["lane"], card["status"]), ("running", "in_progress"))
        self.assertEqual(card["id"], first["v1:shop:PROJ-1"]["id"])
        self.assertEqual(len(card["audit"]), 2)
        for key, c in now.items():
            if key != "v1:shop:PROJ-1":
                self.assertEqual(c, first[key], key)

    def test_the_report_names_each_cards_id_on_an_add_and_on_a_rerun(self):
        for _ in range(2):
            report = self.ok()
            got = self.by_key()
            entries = [e for p in report["projects"] for e in p["cards"]]
            self.assertTrue(entries)
            for e in entries:
                self.assertEqual(e["card"], got[e["key"]]["id"], e["key"])

    def test_a_rerun_updates_a_changed_title_and_source_record(self):
        self.ok("--project", "shop")
        self.edit_state("projects/shop", lambda s: s["tasks"][0].update(
            title={"en": "Add the basket"}, pr="https://github.com/org/repo/pull/9"))
        report = self.ok("--project", "shop")
        self.assertEqual(self.actions(report)["v1:shop:PROJ-1"], "update")
        card = self.by_key()["v1:shop:PROJ-1"]
        self.assertEqual(card["title"], {"en": "Add the basket"})
        self.assertEqual(card["source"]["record"]["pr"], "https://github.com/org/repo/pull/9")

    def test_a_card_of_another_feed_with_the_same_key_is_left_alone(self):
        datadir.ensure(self.data)
        other = Store(self.data).add(store_card(feed="other", key="v1:shop:PROJ-1"), by="test")
        report = self.ok("--project", "shop")
        self.assertEqual(self.actions(report)["v1:shop:PROJ-1"], "add")
        self.assertEqual(Store(self.data).get(other["id"]), other)

    def test_a_decision_answered_in_v1_closes_its_question_card(self):
        self.ok("--project", "shop")
        self.edit_state("projects/shop", lambda s: s["decisions"][0].update(status="answered", answer="A"))
        report = self.ok("--project", "shop")
        self.assertEqual(self.actions(report)["v1:shop:D1"], "update")
        card = self.by_key()["v1:shop:D1"]
        self.assertEqual(card["lane"], "done")
        self.assertIsNone(card["ask"])
        self.assertEqual(card["plain"]["do"]["en"], importv1.PHRASES["en"]["do_closed"])
        self.assertEqual(report["projects"][0]["not_imported"], {"decisions": 4})

    def test_a_decision_archived_in_v1_closes_its_question_card(self):
        self.ok("--project", "shop")
        before = self.by_key()["v1:shop:D1"]

        def archive(state):
            # As v1's ops.archive: a settled decision answered long ago moves out of state.json into archive.jsonl.
            d1 = state["decisions"].pop(0)
            d1.update(status="answered", answer="A", answered_at="2000-01-01T00:00:00Z")
            with open(self.v1 / "projects" / "shop" / "archive.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(d1) + "\n")
        self.edit_state("projects/shop", archive)
        report = self.ok("--project", "shop")
        self.assertEqual(self.actions(report)["v1:shop:D1"], "update")
        card = self.by_key()["v1:shop:D1"]
        self.assertEqual((card["lane"], card["ask"]), ("done", None))
        self.assertEqual(card["plain"]["do"]["en"], importv1.PHRASES["en"]["do_closed"])
        self.assertEqual((card["id"], card["title"], card["parent"], card["source"]),
                         (before["id"], card["plain"]["what"], before["parent"], before["source"]))
        self.assertEqual(card["plain"]["what"], before["plain"]["what"])
        self.assertEqual(card["plain"]["where"], before["plain"]["where"])
        self.assertEqual(set(self.actions(self.ok("--project", "shop")).values()), {"unchanged"})

    def test_closing_looks_only_at_the_projects_own_cards(self):
        self.ok("--project", "shop")
        shop = {k: c for k, c in self.by_key().items() if k.startswith("v1:shop")}
        self.edit_state("projects/shop", lambda s: s["decisions"].pop(0))
        test = self

        class OnlyLookedUp(dict):
            """A card map that refuses to be walked whole and records each card read from it."""
            read = []

            def __getitem__(self, key):
                self.read.append(key)
                return dict.__getitem__(self, key)

            def get(self, key, default=None):
                self.read.append(key)
                return dict.get(self, key, default)

            def items(self):
                test.fail("the closing step walked every card")
            keys = values = __iter__ = items

        other = {f"v1:other:Q{n}": {"key": f"v1:other:Q{n}", "kind": "question", "lane": "needs", "ask": {}}
                 for n in range(1000)}
        existing = OnlyLookedUp({**other, **shop})
        own = [k for k in shop if k.startswith("v1:shop:")]
        entries, _ = importv1.plan("shop", self.v1 / "projects" / "shop", ("en",), importv1.Labels(self.v1),
                                   existing, own)
        self.assertIn("v1:shop:D1", [e["key"] for e in entries])
        self.assertFalse([e for e in entries if e["key"].startswith("v1:other")])
        self.assertTrue(OnlyLookedUp.read)
        self.assertTrue(all(k.startswith("v1:shop:") for k in OnlyLookedUp.read), OnlyLookedUp.read)

    def test_an_archived_decision_closes_its_card_keeping_each_languages_text(self):
        self.set_config("secondary_language: zh\n")
        self.ok("--project", "shop")
        before = self.by_key()["v1:shop:D1"]
        self.edit_state("projects/shop", lambda s: s["decisions"].pop(0))
        self.ok("--project", "shop")
        card = self.by_key()["v1:shop:D1"]
        self.assertEqual((card["lane"], card["ask"]), ("done", None))
        self.assertEqual(card["plain"]["what"], before["plain"]["what"])
        self.assertEqual(card["plain"]["what"]["zh"], "用哪家支付？")
        self.assertEqual(card["plain"]["where"], before["plain"]["where"])

    def test_an_archived_decision_closes_a_card_made_before_a_language_was_added(self):
        self.ok("--project", "shop")
        self.set_config("secondary_language: zh\n")
        self.edit_state("projects/shop", lambda s: s["decisions"].pop(0))
        report = self.ok("--project", "shop")
        self.assertEqual(report["refused"], [])
        card = self.by_key()["v1:shop:D1"]
        self.assertEqual((card["lane"], card["ask"]), ("done", None))
        cards.check_plain(card, ("en", "zh"))

    def test_a_state_without_decisions_is_refused_and_leaves_the_question_cards_open(self):
        self.ok("--project", "shop")
        before = self.by_key()
        self.edit_state("projects/shop", lambda s: s.pop("decisions"))
        code, out, err = self.run_cli("--project", "shop")
        self.assertEqual(code, 1, err)
        report = json.loads(out)
        self.assertEqual([r["name"] for r in report["refused"]], ["shop"])
        self.assertIn("decisions", report["refused"][0]["reason"])
        now = self.by_key()
        for key in ("v1:shop:D1", "v1:shop:D2"):
            self.assertEqual(now[key], before[key], key)
            self.assertEqual(now[key]["lane"], "needs", key)

    def test_a_task_gone_from_v1_keeps_its_card(self):
        self.ok("--project", "shop")
        before = self.by_key()["v1:shop:PROJ-1"]
        self.edit_state("projects/shop", lambda s: s.update(tasks=s["tasks"][1:]))
        report = self.ok("--project", "shop")
        self.assertNotIn("v1:shop:PROJ-1", self.actions(report))
        self.assertEqual(self.by_key()["v1:shop:PROJ-1"], before)

    # -- 5. text in each language -------------------------------------------------------------------------------

    def test_each_configured_language_gets_its_text(self):
        self.set_config("secondary_language: zh\n")
        self.ok()
        got = self.by_key()
        both = got["v1:shop:PROJ-1"]
        self.assertEqual(both["title"], {"en": "Add the cart", "zh": "加购物车"})  # one text per language
        self.assertEqual(both["plain"]["what"], {"en": "Add the cart", "zh": "加购物车"})
        only_en = got["v1:shop:PROJ-2"]
        self.assertEqual(only_en["plain"]["what"], {"en": "Fix the checkout total", "zh": "Fix the checkout total"})
        label = got["v1:shop:D2"]
        self.assertEqual(label["title"], {"en": "Two different answers arrived for D9. Which one counts?",
                                          "zh": "D9 收到了两个不同的回答。以哪个为准？"})
        self.assertEqual(label["plain"]["what"]["zh"], "D9 收到了两个不同的回答。以哪个为准？")  # v1's labels.zh.json
        for c in got.values():
            cards.check_plain(c, ("en", "zh"))
            cards.check_title(c, ("en", "zh"))
            self.assertIn("v1", c["plain"]["do"]["en"])
            self.assertIn("v1", c["plain"]["do"]["zh"])
        self.assertIn("shop", got["v1:shop:PROJ-1"]["plain"]["where"]["en"])
        self.assertIn("PROJ-1", got["v1:shop:PROJ-1"]["plain"]["where"]["en"])
        self.assertEqual(both["plain"]["do"]["zh"], importv1.PHRASES["zh"]["do"])
        self.assertIn("任务 PROJ-1", both["plain"]["where"]["zh"])

    def test_a_title_with_only_another_language_uses_that_text(self):
        self.set_config("secondary_language: zh\n")
        self.edit_state("projects/shop", lambda s: s["tasks"][0].update(title={"de": "Alte Idee"}))
        self.ok("--project", "shop")
        c = self.by_key()["v1:shop:PROJ-1"]
        self.assertEqual(c["title"], {"en": "Alte Idee", "zh": "Alte Idee"})
        self.assertEqual(c["plain"]["what"], {"en": "Alte Idee", "zh": "Alte Idee"})

    def test_a_title_without_the_configured_language_uses_english_before_another(self):
        self.set_config("secondary_language: zh\n")
        self.edit_state("projects/shop", lambda s: s["tasks"][0].update(title={"de": "Alte Idee", "en": "Old idea"}))
        self.ok("--project", "shop")
        self.assertEqual(self.by_key()["v1:shop:PROJ-1"]["plain"]["what"], {"en": "Old idea", "zh": "Old idea"})

    def test_a_label_with_a_format_spec_or_a_conversion_is_shown_as_written(self):
        for template in ("{id:>9223372036854775807}", "{id!r}"):
            with self.subTest(template):
                shutil.rmtree(self.data, ignore_errors=True)
                (self.v1 / "labels.en.json").write_text(json.dumps({"two_answers": template}), encoding="utf-8")
                self.ok("--project", "shop")
                c = self.by_key()["v1:shop:D2"]
                self.assertEqual(c["title"], {"en": template})
                self.assertEqual(c["plain"]["what"]["en"], template)

    def test_a_label_that_cannot_be_filled_is_shown_as_written(self):
        self.set_config("secondary_language: zh\n")
        for template in ("{ broken {id}", "{0} for {id}", "{id.real} for {id}", "{id[0]} for {id}",
                         "{id.__class__} for {id}"):
            with self.subTest(template):
                shutil.rmtree(self.data / "items", ignore_errors=True)
                (self.v1 / "labels.zh.json").write_text(json.dumps({"two_answers": template}), encoding="utf-8")
                self.ok("--project", "shop")
                self.assertEqual(self.by_key()["v1:shop:D2"]["plain"]["what"]["zh"], template)

    def test_a_placeholder_without_an_argument_stays_as_written(self):
        self.edit_state("projects/shop", lambda s: s["decisions"][1].update(title={"key": "two_answers"}))
        self.ok("--project", "shop")
        self.assertEqual(self.by_key()["v1:shop:D2"]["title"],
                         {"en": "Two different answers arrived for {id}. Which one counts?"})
        shutil.rmtree(self.data)
        (self.v1 / "labels.en.json").write_text(json.dumps({"two_answers": "{id} and {other}"}), encoding="utf-8")
        self.edit_state("projects/shop", lambda s: s["decisions"][1].update(
            title={"key": "two_answers", "args": {"id": "D9"}}))
        self.ok("--project", "shop")
        self.assertEqual(self.by_key()["v1:shop:D2"]["title"], {"en": "D9 and {other}"})

    def test_a_label_with_no_translation_falls_back_to_english(self):
        self.set_config("secondary_language: zh\n")
        (self.v1 / "labels.zh.json").unlink()
        self.ok("--project", "shop")
        what = self.by_key()["v1:shop:D2"]["plain"]["what"]
        self.assertEqual(what["zh"], what["en"])

    def test_a_language_with_a_script_uses_its_base_language(self):
        self.set_config("secondary_language: zh-Hans\n")
        self.ok("--project", "shop")
        self.assertEqual(self.by_key()["v1:shop:PROJ-1"]["plain"]["what"]["zh-Hans"], "加购物车")

    def test_a_label_with_a_base_language_script_uses_the_base_languages_labels(self):
        self.set_config("secondary_language: zh-Hans\n")
        self.ok("--project", "shop")
        self.assertEqual(self.by_key()["v1:shop:D2"]["plain"]["what"]["zh-Hans"],
                         "D9 收到了两个不同的回答。以哪个为准？")

    def test_a_labels_file_that_is_broken_or_not_text_falls_back_to_english(self):
        self.set_config("secondary_language: zh\n")
        for text in ("{", json.dumps({"two_answers": 5})):
            with self.subTest(text):
                shutil.rmtree(self.data / "items", ignore_errors=True)
                (self.v1 / "labels.zh.json").write_text(text, encoding="utf-8")
                self.ok("--project", "shop")
                what = self.by_key()["v1:shop:D2"]["plain"]["what"]
                self.assertEqual(what["zh"], what["en"])

    def test_a_label_key_with_no_text_is_shown_as_the_key(self):
        self.edit_state("projects/shop", lambda s: s["decisions"][1].update(title={"key": "no_such_label"}))
        self.ok("--project", "shop")
        self.assertEqual(self.by_key()["v1:shop:D2"]["title"], {"en": "no_such_label"})

    def test_a_label_that_would_fill_too_long_is_shown_as_written(self):
        template = "{i}" * 100
        (self.v1 / "labels.en.json").write_text(json.dumps({"boom": template}), encoding="utf-8")
        self.edit_state("projects/shop", lambda s: s["tasks"][0].update(
            title={"key": "boom", "args": {"i": "x" * 20}}))
        self.ok("--project", "shop")
        c = self.by_key()["v1:shop:PROJ-1"]
        self.assertEqual(c["title"], {"en": template})
        self.assertEqual(c["plain"]["what"]["en"], template)

    def test_a_label_too_long_to_show_as_written_is_shown_as_its_key(self):
        for template in ("A" * 5000, "{id:>9}" + "A" * 5000):
            with self.subTest(template[:10]):
                shutil.rmtree(self.data, ignore_errors=True)
                (self.v1 / "labels.en.json").write_text(json.dumps({"big": template}), encoding="utf-8")
                self.edit_state("projects/shop", lambda s: s["tasks"][0].update(
                    title={"key": "big", "args": {"id": "D9"}}))
                self.ok("--project", "shop")
                c = self.by_key()["v1:shop:PROJ-1"]
                self.assertEqual(c["title"], {"en": "big"})
                self.assertEqual(c["plain"]["what"]["en"], "big")

    def test_a_label_template_too_long_is_shown_as_its_key_without_being_parsed(self):
        template = "{a}" * 2000  # 6,000 characters that fill to nothing
        (self.v1 / "labels.en.json").write_text(json.dumps({"k": template}), encoding="utf-8")
        self.edit_state("projects/shop", lambda s: s["tasks"][0].update(title={"key": "k", "args": {"a": ""}}))
        with mock.patch.object(importv1, "_filled_size", wraps=importv1._filled_size) as spy:
            self.ok("--project", "shop")
        self.assertEqual(self.by_key()["v1:shop:PROJ-1"]["title"], {"en": "k"})
        self.assertNotIn(template, [c.args[0] for c in spy.call_args_list])

    def test_the_filled_size_stops_early_and_turns_each_argument_into_text_once(self):
        class Counted:
            def __init__(self, text):
                self.text, self.calls = text, 0

            def __str__(self):
                self.calls += 1
                return self.text

        one = Counted("x")
        self.assertGreater(importv1._filled_size("{i}" * 3000, {"i": one}), importv1.MAX_LABEL)
        self.assertEqual(one.calls, 1)  # the same field is turned into text once
        many = {f"f{n}": Counted("x" * 600) for n in range(3000)}
        self.assertGreater(importv1._filled_size("".join("{" + k + "}" for k in many), many), importv1.MAX_LABEL)
        self.assertLessEqual(sum(c.calls for c in many.values()), 2)  # it stops once past MAX_LABEL

    def test_the_title_has_one_text_per_configured_language(self):
        self.set_config("secondary_language: zh\n")
        self.ok("--project", "shop")
        self.assertEqual(self.by_key()["v1:shop:PROJ-1"]["title"], {"en": "Add the cart", "zh": "加购物车"})

    def test_the_title_has_only_the_primary_slot_when_only_one_language_is_configured(self):
        self.ok("--project", "shop")
        self.assertEqual(self.by_key()["v1:shop:PROJ-1"]["title"], {"en": "Add the cart"})

    def test_the_project_cards_title_is_its_name_in_every_language(self):
        self.set_config("secondary_language: zh\n")
        self.ok("--project", "shop")
        self.assertEqual(self.by_key()["v1:shop"]["title"], {"en": "shop", "zh": "shop"})

    def test_a_blank_title_in_one_language_falls_back_and_passes_the_title_check(self):
        self.set_config("secondary_language: zh\n")
        self.edit_state("projects/shop", lambda s: s["tasks"][0].update(title={"en": "  ", "zh": "加购物车"}))
        self.ok("--project", "shop")
        c = self.by_key()["v1:shop:PROJ-1"]
        self.assertEqual(c["title"], {"en": "加购物车", "zh": "加购物车"})
        self.assertEqual(c["title"], c["plain"]["what"])
        for card in self.by_key().values():
            cards.check_title(card, ("en", "zh"))

    def test_each_title_is_its_own_copy_of_the_plain_words(self):
        entries, _ = importv1.plan("shop", self.v1 / "projects" / "shop", ("en", "zh"),
                                   importv1.Labels(self.v1), {}, [])
        for e in entries:
            with self.subTest(e["key"]):
                self.assertEqual(e["card"]["title"], e["card"]["plain"]["what"])
                self.assertIsNot(e["card"]["title"], e["card"]["plain"]["what"])

    def test_a_rerun_moves_cards_with_a_one_text_title_to_one_text_per_language_once(self):
        self.set_config("secondary_language: zh\n")
        self.ok("--project", "shop")
        store = Store(self.data)
        # As the import wrote every card before titles had one slot per language: the primary language's text.
        for c in store.list():
            store.update(c["id"], {"title": c["plain"]["what"]["en"]}, by="test")
        old = self.by_key()
        self.assertTrue(all(isinstance(c["title"], str) for c in old.values()))
        report = self.ok("--project", "shop")
        self.assertEqual(set(self.actions(report).values()), {"update"})
        now = self.by_key()
        self.assertEqual(sorted(now), sorted(old))
        for key, c in now.items():
            with self.subTest(key):
                self.assertEqual(c["title"], old[key]["plain"]["what"])
                self.assertEqual((c["id"], c.get("parent")), (old[key]["id"], old[key].get("parent")))
                self.assertEqual(c["audit"][:len(old[key]["audit"])], old[key]["audit"])
                self.assertEqual(len(c["audit"]), len(old[key]["audit"]) + 1)
        self.assertEqual(set(self.actions(self.ok("--project", "shop")).values()), {"unchanged"})

    def test_a_question_card_with_a_one_text_title_is_closed_with_one_text_per_language(self):
        self.set_config("secondary_language: zh\n")
        self.ok("--project", "shop")
        store = Store(self.data)
        d1 = self.by_key()["v1:shop:D1"]
        store.update(d1["id"], {"title": d1["plain"]["what"]["en"]}, by="test")  # as the old import wrote it
        before = self.by_key()["v1:shop:D1"]
        self.assertIsInstance(before["title"], str)
        self.edit_state("projects/shop", lambda s: s["decisions"].pop(0))
        report = self.ok("--project", "shop")
        self.assertEqual(self.actions(report)["v1:shop:D1"], "update")
        card = self.by_key()["v1:shop:D1"]
        self.assertEqual((card["lane"], card["ask"]), ("done", None))
        self.assertEqual(card["title"], before["plain"]["what"])
        self.assertEqual(set(card["title"]), {"en", "zh"})
        cards.check_title(card, ("en", "zh"))

    def test_a_question_card_the_old_import_closed_moves_to_one_text_per_language_once(self):
        self.set_config("secondary_language: zh\n")
        self.ok("--project", "shop")
        self.edit_state("projects/shop", lambda s: s["decisions"].pop(0))
        self.ok("--project", "shop")  # D1 is closed
        store = Store(self.data)
        d1 = self.by_key()["v1:shop:D1"]
        self.assertEqual((d1["lane"], d1["ask"]), ("done", None))
        store.update(d1["id"], {"title": d1["plain"]["what"]["en"]}, by="test")  # as the old import left it
        before = self.by_key()["v1:shop:D1"]
        self.assertIsInstance(before["title"], str)
        report = self.ok("--project", "shop")
        self.assertEqual(self.actions(report)["v1:shop:D1"], "update")
        card = self.by_key()["v1:shop:D1"]
        self.assertEqual(card["title"], before["plain"]["what"])
        self.assertEqual(set(card["title"]), {"en", "zh"})
        self.assertEqual((card["id"], card.get("parent")), (before["id"], before.get("parent")))
        self.assertEqual((card["lane"], card["ask"]), ("done", None))
        cards.check_title(card, ("en", "zh"))
        report = self.ok("--project", "shop")
        self.assertNotIn("v1:shop:D1", self.actions(report))
        self.assertEqual(self.by_key()["v1:shop:D1"], card)

    def test_a_question_card_closed_again_keeps_its_words_in_a_language_no_longer_configured(self):
        self.set_config("secondary_language: zh\n")
        self.ok("--project", "shop")
        self.edit_state("projects/shop", lambda s: s["decisions"].pop(0))
        self.ok("--project", "shop")  # D1 is closed
        d1 = self.by_key()["v1:shop:D1"]
        Store(self.data).update(d1["id"], {"title": d1["plain"]["what"]["en"]}, by="test")  # as the old import left it
        before = self.by_key()["v1:shop:D1"]
        self.set_config("")
        report = self.ok("--project", "shop")
        self.assertEqual(self.actions(report)["v1:shop:D1"], "update")
        card = self.by_key()["v1:shop:D1"]
        want = {"en": before["plain"]["what"]["en"], "zh": before["plain"]["what"]["zh"]}
        self.assertEqual(card["title"], want)
        self.assertEqual(card["plain"]["what"], want)
        for field in ("where", "do"):
            self.assertEqual(card["plain"][field]["zh"], before["plain"][field]["zh"])
        # With the language added back, the card has its words in it and is left alone.
        self.set_config("secondary_language: zh\n")
        report = self.ok("--project", "shop")
        self.assertNotIn("v1:shop:D1", self.actions(report))
        card = self.by_key()["v1:shop:D1"]
        cards.validate(card, ("en", "zh"))

    def test_a_task_card_gone_from_v1_keeps_its_one_text_title(self):
        self.set_config("secondary_language: zh\n")
        self.ok("--project", "shop")
        card = self.by_key()["v1:shop:PROJ-2"]
        # As the old import wrote it: the primary language's text.
        Store(self.data).update(card["id"], {"title": card["plain"]["what"]["en"]}, by="test")
        before = self.by_key()["v1:shop:PROJ-2"]
        self.assertIsInstance(before["title"], str)
        self.edit_state("projects/shop", lambda s: s.update(tasks=[t for t in s["tasks"] if t["id"] != "PROJ-2"]))
        report = self.ok("--project", "shop")
        self.assertNotIn("v1:shop:PROJ-2", self.actions(report))
        self.assertEqual(self.by_key()["v1:shop:PROJ-2"], before)

    def test_a_closed_question_without_plain_words_takes_each_slot_from_its_old_title(self):
        make = importv1._Maker("shop", self.v1 / "projects" / "shop", ("en", "zh"), importv1.Labels(self.v1))
        for title, want in (("Old words", {"en": "Old words", "zh": "Old words"}),
                            ({"en": "Old", "zh": "旧"}, {"en": "Old", "zh": "旧"}),
                            ({"en": "Old"}, {"en": "Old", "zh": "Old"}),
                            (None, {"en": "D1", "zh": "D1"}),
                            ("  ", {"en": "D1", "zh": "D1"})):
            with self.subTest(title=title):
                old = {"key": "v1:shop:D1", "kind": "question", "title": title, "plain": {"what": {"zh": " "}},
                       "parent": "abc", "source": {"name": "v1-import", "record": {}}}
                card = make.closed(old)["card"]
                self.assertEqual(card["title"], want)
                self.assertEqual(card["plain"]["what"], want)
                cards.check_title(card, ("en", "zh"))
        # A primary language that is not English: a slot the old title lacks falls back to the primary's text.
        make = importv1._Maker("shop", self.v1 / "projects" / "shop", ("zh", "en"), importv1.Labels(self.v1))
        old = {"key": "v1:shop:D1", "kind": "question", "title": {"zh": "旧"}, "plain": {"what": {"en": " "}},
               "parent": "abc", "source": {"name": "v1-import", "record": {}}}
        card = make.closed(old)["card"]
        self.assertEqual(card["title"], {"zh": "旧", "en": "旧"})
        self.assertEqual(card["plain"]["what"], {"zh": "旧", "en": "旧"})
        cards.check_title(card, ("zh", "en"))

    def test_the_title_is_the_primary_languages_text_when_it_is_not_english(self):
        self.set_config("primary_language: zh\nsecondary_language: en\n")
        self.ok("--project", "shop")
        self.assertEqual(self.by_key()["v1:shop:PROJ-1"]["title"], {"zh": "加购物车", "en": "Add the cart"})

    def test_where_names_the_project_and_a_questions_task(self):
        self.set_config("secondary_language: zh\n")
        self.ok("--project", "shop")
        got = self.by_key()
        for lang in ("en", "zh"):
            with self.subTest(lang):
                self.assertIn("shop", got["v1:shop"]["plain"]["where"][lang])
                self.assertIn("shop", got["v1:shop:D1"]["plain"]["where"][lang])
                self.assertIn("PROJ-4", got["v1:shop:D1"]["plain"]["where"][lang])  # D1 is about task PROJ-4
                self.assertNotIn("PROJ", got["v1:shop:D2"]["plain"]["where"][lang])  # D2 has no task

    def test_a_language_without_phrases_is_refused_before_anything_is_written(self):
        self.set_config("secondary_language: fr\n")
        line = self.fails()
        self.assertIn("fr", line)
        self.assertEqual(self.card_files(), [])
        self.assertEqual(sorted(p.name for p in self.data.iterdir()), ["config.yaml"])

    # -- 6. finding projects ------------------------------------------------------------------------------------

    def test_both_the_projects_folder_and_a_legacy_top_level_project_are_found(self):
        report = self.ok()
        self.assertEqual([(p["name"], p["folder"]) for p in report["projects"]],
                         [("notes", str(self.v1 / "notes")), ("shop", str(self.v1 / "projects" / "shop"))])
        self.assertIn("v1:notes", self.by_key())

    def test_a_legacy_folder_shadowed_by_the_projects_folder_is_not_imported_twice(self):
        shutil.copytree(self.v1 / "projects" / "shop", self.v1 / "shop")
        self.edit_state("shop", lambda s: s["tasks"].append(dict(s["tasks"][0], id="PROJ-99")))
        report = self.ok()
        self.assertEqual([p["name"] for p in report["projects"]], ["notes", "shop"])
        self.assertEqual(report["projects"][1]["folder"], str(self.v1 / "projects" / "shop"))
        self.assertNotIn("v1:shop:PROJ-99", self.by_key())

    def test_a_projects_folder_without_config_does_not_hide_the_legacy_project(self):
        (self.v1 / "projects" / "notes").mkdir()
        (self.v1 / "projects" / "notes" / "state.json").write_text("{}", encoding="utf-8")
        report = self.ok("--project", "notes")
        self.assertEqual(report["projects"][0]["folder"], str(self.v1 / "notes"))

    def test_a_folder_name_v1_would_refuse_is_not_a_project(self):
        shutil.copytree(self.v1 / "notes", self.v1 / "projects" / "Bad_Name")
        report = self.ok()
        self.assertEqual([p["name"] for p in report["projects"]], ["notes", "shop"])

    def test_a_legacy_folder_name_v1_would_refuse_is_not_a_project(self):
        shutil.copytree(self.v1 / "notes", self.v1 / "Bad_Name")
        report = self.ok()
        self.assertEqual([p["name"] for p in report["projects"]], ["notes", "shop"])

    def test_a_project_folder_that_cannot_be_read_is_refused_and_the_others_still_import(self):
        if os.geteuid() == 0:
            self.skipTest("root reads a folder whatever its mode")
        locked = self.v1 / "projects" / "locked"
        shutil.copytree(self.v1 / "projects" / "shop", locked)
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o755)
        code, out, err = self.run_cli()
        self.assertEqual(code, 1, err)
        report = json.loads(out)
        self.assertEqual([r["name"] for r in report["refused"]], ["locked"])
        self.assertIn("config.json", report["refused"][0]["reason"])
        self.assertEqual([p["name"] for p in report["projects"]], ["notes", "shop"])

    def test_project_limits_the_import(self):
        report = self.ok("--project", "notes")
        self.assertEqual([p["name"] for p in report["projects"]], ["notes"])
        self.assertEqual({c["project"] for c in self.by_key().values()}, {"notes"})

    def test_an_unknown_project_is_one_line_and_writes_nothing(self):
        line = self.fails("--project", "shop", "--project", "nope")
        self.assertIn("nope", line)
        self.assertFalse(self.data.exists())

    def test_with_no_from_autopilot_data_is_used(self):
        with mock.patch.dict(os.environ, {"AUTOPILOT_DATA": str(self.v1)}):
            report = self.ok("--project", "notes", source=False)
        self.assertEqual(report["from"], str(self.v1))
        self.assertIn("v1:notes", self.by_key())

    def test_with_no_from_and_no_autopilot_data_the_home_folder_is_used(self):
        home = Path(os.environ["HOME"])
        shutil.copytree(FIXTURE, home / ".claude" / "autopilot-data")
        os.environ.pop("AUTOPILOT_DATA")
        report = self.ok("--dry-run", source=False)
        self.assertEqual(report["from"], str(home / ".claude" / "autopilot-data"))
        self.assertEqual([p["name"] for p in report["projects"]], ["notes", "shop"])

    def test_a_missing_v1_folder_is_one_line(self):
        line = self.fails(source=False)  # AUTOPILOT_DATA points at a path that does not exist
        self.assertIn("no-v1-here", line)
        self.assertFalse(self.data.exists())

    def test_an_unknown_user_in_from_is_one_line(self):
        self.v1 = Path("~nosuchuser12345xyz/v1")  # pathlib cannot expand ~user for a user that does not exist
        line = self.fails()
        self.assertIn("v1 data folder", line)
        self.assertIn("~nosuchuser12345xyz", line)
        self.assertFalse(self.data.exists())

    def test_a_data_folder_inside_the_v1_folder_is_refused(self):
        self.data = self.v1 / "v2-data"
        line = self.fails()
        self.assertIn("inside", line)
        self.assertFalse(self.data.exists())

    def test_a_data_folder_that_is_the_v1_folder_is_refused(self):
        self.data = self.v1
        self.fails()
        self.assertFalse((self.v1 / "items").exists())
        self.assertFalse((self.v1 / "config.yaml").exists())

    def test_a_data_folder_inside_the_v1_folder_through_a_symlink_is_refused(self):
        link = self.tmp / "link"
        link.symlink_to(self.v1)
        self.data = link / "v2-data"
        code, out, err = self.run_cli("--from", str(link), source=False)
        self.assertEqual(code, 1, out)
        self.assertIn(f"the v1 data folder {self.v1} ", err)  # the folder the link points at, not the link
        self.assertFalse((self.v1 / "v2-data").exists())

    def test_a_data_folder_inside_a_symlinked_project_folders_real_location_is_refused(self):
        if not hasattr(os, "symlink"):
            self.skipTest("no symlinks here")
        real = self.tmp / "elsewhere" / "shop"
        real.parent.mkdir()
        shutil.move(str(self.v1 / "projects" / "shop"), str(real))
        try:
            (self.v1 / "projects" / "shop").symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError) as e:
            self.skipTest(f"cannot make a symlink: {e}")
        self.data = real / "v2"
        line = self.fails()
        self.assertIn("shop", line)
        self.assertFalse(self.data.exists())

    def test_a_symlinked_project_folder_whose_real_location_is_inside_the_data_folder_is_refused(self):
        if not hasattr(os, "symlink"):
            self.skipTest("no symlinks here")
        real = self.data / "mirror" / "shop"
        real.parent.mkdir(parents=True)
        shutil.move(str(self.v1 / "projects" / "shop"), str(real))
        try:
            (self.v1 / "projects" / "shop").symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError) as e:
            self.skipTest(f"cannot make a symlink: {e}")
        before = snapshot(self.data)
        line = self.fails()
        self.assertIn("shop", line)
        self.assertEqual(snapshot(self.data), before)

    def test_a_data_folder_inside_an_unnamed_symlinked_project_folders_real_location_is_refused(self):
        if not hasattr(os, "symlink"):
            self.skipTest("no symlinks here")
        real = self.tmp / "outside" / "other"
        shutil.copytree(self.v1 / "notes", real)
        try:
            (self.v1 / "projects" / "other").symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError) as e:
            self.skipTest(f"cannot make a symlink: {e}")
        self.data = real / "v2"
        line = self.fails("--project", "shop")
        self.assertIn("other", line)
        self.assertFalse(self.data.exists())

    def test_a_data_folder_inside_a_symlinked_projects_folders_real_location_is_refused(self):
        if not hasattr(os, "symlink"):
            self.skipTest("no symlinks here")
        real = self.tmp / "P"
        shutil.move(str(self.v1 / "projects"), str(real))
        try:
            (self.v1 / "projects").symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError) as e:
            self.skipTest(f"cannot make a symlink: {e}")
        self.data = real / "v2"
        line = self.fails()
        self.assertIn("projects folder", line)
        self.assertFalse(self.data.exists())

    def test_a_dangling_symlinked_projects_folder_that_points_at_the_data_folder_is_refused(self):
        if not hasattr(os, "symlink"):
            self.skipTest("no symlinks here")
        shutil.move(str(self.v1 / "projects" / "shop"), str(self.v1 / "shop"))  # keep shop as a legacy project
        shutil.rmtree(self.v1 / "projects")
        self.assertFalse(self.data.exists())
        try:
            (self.v1 / "projects").symlink_to(self.data, target_is_directory=True)
        except (OSError, NotImplementedError) as e:
            self.skipTest(f"cannot make a symlink: {e}")
        line = self.fails()
        self.assertIn("projects folder", line)
        self.assertFalse(self.data.exists())

    def test_a_dangling_symlinked_projects_folder_stops_the_import_with_one_line(self):
        if not hasattr(os, "symlink"):
            self.skipTest("no symlinks here")
        shutil.move(str(self.v1 / "projects" / "shop"), str(self.v1 / "shop"))  # keep shop as a legacy project
        shutil.rmtree(self.v1 / "projects")
        target = self.tmp / "nowhere" / "projects"
        try:
            (self.v1 / "projects").symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError) as e:
            self.skipTest(f"cannot make a symlink: {e}")
        line = self.fails()
        self.assertIn("projects folder", line)
        self.assertFalse(self.data.exists())
        self.assertFalse(target.exists())

    def test_a_dangling_projects_link_to_the_data_folder_spelled_another_way_is_refused(self):
        if not hasattr(os, "symlink"):
            self.skipTest("no symlinks here")
        nfc, nfd = "caf\u00e9", "cafe\u0301"
        (self.tmp / nfc).mkdir()
        if not os.path.exists(self.tmp / nfd):
            self.skipTest("this file system does not treat the NFC and NFD spellings as one name")
        shutil.move(str(self.v1 / "projects" / "shop"), str(self.v1 / "shop"))  # keep shop as a legacy project
        shutil.rmtree(self.v1 / "projects")
        try:
            (self.v1 / "projects").symlink_to(self.tmp / nfc / "target", target_is_directory=True)
        except (OSError, NotImplementedError) as e:
            self.skipTest(f"cannot make a symlink: {e}")
        self.data = self.tmp / nfd / "target"
        line = self.fails()
        self.assertIn("projects folder", line)
        self.assertFalse((self.tmp / nfc / "target").exists())

    def test_a_data_folder_the_overlap_check_cannot_look_into_is_one_line(self):
        # Only the import's own first check (data against --from) fails, as Path.exists can on a locked folder;
        # datadir.resolve's check against ~/.claude runs as usual.
        inside = datadir._inside
        def locked(path, other):
            if other == self.v1:
                raise PermissionError(13, "Permission denied")
            return inside(path, other)
        with mock.patch.object(importv1.datadir, "_inside", side_effect=locked):
            line = self.fails()
        self.assertIn("data folder", line)
        self.assertIn("Permission denied", line)
        self.assertFalse(self.data.exists())

    def test_a_symlinked_project_folder_inside_an_unreadable_folder_stops_the_import_with_one_line(self):
        if not hasattr(os, "symlink"):
            self.skipTest("no symlinks here")
        if os.geteuid() == 0:
            self.skipTest("root reads a folder whatever its mode")
        locked = self.tmp / "locked"
        locked.mkdir()
        shutil.move(str(self.v1 / "projects" / "shop"), str(locked / "shop"))
        try:
            (self.v1 / "projects" / "shop").symlink_to(locked / "shop", target_is_directory=True)
        except (OSError, NotImplementedError) as e:
            self.skipTest(f"cannot make a symlink: {e}")
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o755)
        line = self.fails()
        self.assertIn("v1 project shop's folder", line)
        self.assertFalse(self.data.exists())  # nothing imported, not even notes

    def test_a_project_folder_reached_through_an_unreadable_folder_is_not_written_into(self):
        if not hasattr(os, "symlink"):
            self.skipTest("no symlinks here")
        if os.geteuid() == 0:
            self.skipTest("root reads a folder whatever its mode")
        # P is project p's real folder; the import reaches it as projects/p -> B/locked/sym -> P, and the data
        # folder is P/data: resolve() stops at the locked folder, so only the stat of its answer shows the overlap.
        real = self.tmp / "P"
        shutil.copytree(self.v1 / "projects" / "shop", real)
        locked = self.tmp / "B" / "locked"
        locked.mkdir(parents=True)
        try:
            (locked / "sym").symlink_to(real, target_is_directory=True)
            (self.v1 / "projects" / "p").symlink_to(locked / "sym", target_is_directory=True)
        except (OSError, NotImplementedError) as e:
            self.skipTest(f"cannot make a symlink: {e}")
        self.data = real / "data"
        before = snapshot(real)
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o755)
        line = self.fails()
        # A Python whose exists() says False for such a path, instead of raising, gets the same line.
        def quiet(path, *args, **kw):
            try:
                return exists(path, *args, **kw)
            except OSError:
                return False
        exists = Path.exists
        with mock.patch.object(Path, "exists", quiet):
            quiet_line = self.fails()
        os.chmod(locked, 0o755)
        self.assertIn("v1 project p's folder", line)
        self.assertIn("v1 project p's folder", quiet_line)
        self.assertFalse(self.data.exists())
        self.assertEqual(snapshot(real), before)

    def test_a_v1_folder_inside_an_unreadable_folder_is_one_line(self):
        if os.geteuid() == 0:
            self.skipTest("root reads a folder whatever its mode")
        locked = self.tmp / "locked"
        shutil.copytree(FIXTURE, locked / "v1")
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o755)
        line = self.fails("--from", str(locked / "v1"), source=False)
        self.assertIn("v1 data folder", line)
        self.assertFalse(self.data.exists())

    def test_a_v1_folder_with_a_name_too_long_is_one_line(self):
        line = self.fails("--from", str(self.tmp / ("v" * 300)), source=False)
        self.assertIn("v1 data folder", line)
        self.assertFalse(self.data.exists())

    def test_a_symlinked_projects_folder_inside_an_unreadable_folder_is_one_line(self):
        if not hasattr(os, "symlink"):
            self.skipTest("no symlinks here")
        if os.geteuid() == 0:
            self.skipTest("root reads a folder whatever its mode")
        locked = self.tmp / "locked"
        locked.mkdir()
        shutil.move(str(self.v1 / "projects"), str(locked / "projects"))
        try:
            (self.v1 / "projects").symlink_to(locked / "projects", target_is_directory=True)
        except (OSError, NotImplementedError) as e:
            self.skipTest(f"cannot make a symlink: {e}")
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o755)
        line = self.fails()
        self.assertIn("projects folder", line)
        self.assertFalse(self.data.exists())
        # A Python whose exists() and is_dir() say False for such a path, instead of raising, gets the same line.
        def quiet(check):
            def wrapped(path, *args, **kw):
                try:
                    return check(path, *args, **kw)
                except OSError:
                    return False
            return wrapped
        with mock.patch.object(Path, "exists", quiet(Path.exists)), \
                mock.patch.object(Path, "is_dir", quiet(Path.is_dir)):
            line = self.fails()
        self.assertIn("projects folder", line)
        self.assertFalse(self.data.exists())

    def test_a_symlinked_projects_folder_whose_real_location_is_inside_the_data_folder_is_refused(self):
        if not hasattr(os, "symlink"):
            self.skipTest("no symlinks here")
        shutil.move(str(self.v1 / "projects" / "shop"), str(self.v1 / "shop"))  # keep shop as a legacy project
        self.ok("--project", "notes")
        self.assertTrue((self.data / "items").is_dir())
        shutil.rmtree(self.v1 / "projects")
        try:
            (self.v1 / "projects").symlink_to(self.data / "items", target_is_directory=True)
        except (OSError, NotImplementedError) as e:
            self.skipTest(f"cannot make a symlink: {e}")
        before = snapshot(self.data)
        line = self.fails()
        self.assertIn("projects folder", line)
        self.assertEqual(snapshot(self.data), before)

    def test_a_v1_folder_inside_the_data_folder_is_refused(self):
        self.data = self.tmp / "d"
        self.v1 = self.data / "v1"
        shutil.copytree(FIXTURE, self.v1)
        before = snapshot(self.data)
        self.fails()
        self.assertEqual(snapshot(self.data), before)

    def test_a_data_folder_inside_the_v1_folder_spelled_in_other_letter_case_is_refused(self):
        probe = self.tmp / "case-probe"
        probe.touch()
        if not os.path.exists(str(probe).upper()):
            self.skipTest("the temp file system tells letter case apart")
        self.data = self.v1 / "v2-data"
        code, out, err = self.run_cli("--from", str(self.tmp / "V1"), source=False)
        self.assertEqual(code, 1, out)
        self.assertFalse((self.v1 / "v2-data").exists())

    def test_odd_v1_values_do_not_stop_the_import(self):
        def odd(state):
            state["decisions"].append({"id": "D8", "kind": ["ask"], "status": "open", "title": {"en": "x"}})
            state["decisions"].append({"id": "D7", "kind": "gate", "status": "open", "title": {"key": ["x"]}})
        self.edit_state("notes", odd)
        report = self.ok("--project", "notes")
        self.assertEqual(report["projects"][0]["not_imported"], {"decisions": 1})
        self.assertEqual(self.by_key()["v1:notes:D7"]["title"], {"en": "['x']"})

    # -- 7. a broken project does not stop the others -----------------------------------------------------------

    def test_a_project_with_bad_state_is_refused_and_the_others_still_import(self):
        (self.v1 / "notes" / "state.json").write_text("{not json", encoding="utf-8")
        code, out, err = self.run_cli()
        self.assertEqual(code, 1, err)
        report = json.loads(out)
        self.assertEqual([p["name"] for p in report["projects"]], ["shop"])
        self.assertEqual(len(report["refused"]), 1)
        refused = report["refused"][0]
        self.assertEqual((refused["name"], refused["folder"]), ("notes", str(self.v1 / "notes")))
        self.assertIn("state.json", refused["reason"])
        self.assertIn("v1:shop", self.by_key())
        self.assertNotIn("v1:notes", self.by_key())
        self.assertTrue(err.startswith("ap2: "), err)
        self.assertEqual(len(err.splitlines()), 1, err)

    def test_each_kind_of_broken_project_is_refused_with_its_reason(self):
        cases = {
            "missing state.json": (lambda d: (d / "state.json").unlink(), "state.json"),
            "config.json not JSON": (lambda d: (d / "config.json").write_text("[", encoding="utf-8"), "config.json"),
            "state not an object": (lambda d: (d / "state.json").write_text("[]", encoding="utf-8"), "object"),
            "config not an object": (lambda d: (d / "config.json").write_text("[]", encoding="utf-8"), "object"),
            "tasks not a list": (lambda d: (d / "state.json").write_text(
                json.dumps({"tasks": None}), encoding="utf-8"), "not a list"),
            "a task not an object": (lambda d: (d / "state.json").write_text(
                json.dumps({"tasks": ["x"]}), encoding="utf-8"), "not an object"),
            "a task without id": (lambda d: (d / "state.json").write_text(
                json.dumps({"tasks": [{"title": {"en": "x"}, "status": "todo"}], "decisions": []}),
                encoding="utf-8"), "id"),
            "an unknown task status": (lambda d: (d / "state.json").write_text(
                json.dumps({"tasks": [{"id": "PROJ-9", "status": "later"}], "decisions": []}),
                encoding="utf-8"), "later"),
            "an id used twice": (lambda d: (d / "state.json").write_text(
                json.dumps({"tasks": [{"id": "PROJ-9", "status": "todo"}],
                            "decisions": [{"id": "PROJ-9", "kind": "ask", "status": "open"}]}),
                encoding="utf-8"), "twice"),
            "a decision without id": (lambda d: (d / "state.json").write_text(
                json.dumps({"tasks": [], "decisions": [{"kind": "ask", "status": "open"}]}), encoding="utf-8"),
                "decisions[0] has no id"),
            "a status that is not text": (lambda d: (d / "state.json").write_text(
                json.dumps({"tasks": [{"id": "PROJ-9", "status": ["todo"]}], "decisions": []}),
                encoding="utf-8"), "unknown status"),
            "a state without tasks": (lambda d: (d / "state.json").write_text(
                json.dumps({"decisions": []}), encoding="utf-8"), "tasks"),
            "a state without decisions": (lambda d: (d / "state.json").write_text(
                json.dumps({"tasks": []}), encoding="utf-8"), "decisions"),
            "text that is not Unicode": (lambda d: (d / "state.json").write_text(
                '{"tasks": [{"id": "PROJ-9", "status": "todo", "title": {"en": "\\ud800"}}], "decisions": []}',
                encoding="utf-8"), "Unicode"),
        }
        for name, (breakit, word) in cases.items():
            with self.subTest(name):
                shutil.rmtree(self.v1)
                shutil.copytree(FIXTURE, self.v1)
                shutil.rmtree(self.data, ignore_errors=True)
                breakit(self.v1 / "notes")
                code, out, err = self.run_cli()
                self.assertEqual(code, 1, err)
                report = json.loads(out)
                self.assertEqual([r["name"] for r in report["refused"]], ["notes"])
                self.assertIn(word, report["refused"][0]["reason"])
                self.assertEqual([p["name"] for p in report["projects"]], ["shop"])
                self.assertNotIn("v1:notes", self.by_key())

    def test_a_task_nested_too_deeply_refuses_its_project_before_any_card_is_written(self):
        for depth in (300, 600):
            with self.subTest(depth=depth):
                shutil.rmtree(self.data, ignore_errors=True)
                path = self.v1 / "projects" / "shop" / "state.json"
                shutil.copy(FIXTURE / "projects" / "shop" / "state.json", path)
                text = path.read_text(encoding="utf-8")
                text = text.replace('"link": null, "needs": [], "critical": false, "phase": 2',
                                    '"link": ' + "[" * depth + "]" * depth + ', "needs": [], "critical": false, '
                                    '"phase": 2', 1)
                path.write_text(text, encoding="utf-8")
                code, out, err = self.run_cli()
                self.assertEqual(code, 1, err)
                report = json.loads(out)
                self.assertEqual([r["name"] for r in report["refused"]], ["shop"])
                self.assertIn("deep", report["refused"][0]["reason"])
                self.assertEqual([p["name"] for p in report["projects"]], ["notes"])
                self.assertEqual({c["project"] for c in self.by_key().values()}, {"notes"})

    # -- the command line ---------------------------------------------------------------------------------------

    def test_the_command_is_in_the_usage(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main([]), 2)
        self.assertIn("import-v1", err.getvalue())


if __name__ == "__main__":
    unittest.main()
