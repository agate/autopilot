"""The local page's server (design 9.1): routes, the request guard, the question POST, read-only and the page."""
from __future__ import annotations

import http.client
import io
import json
import os
import re
import shutil
import socket
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT

from assistant import cards, datadir, gate, server, templates
from assistant.config import Config, ConfigError
from assistant.engine import Engine
from assistant.store import Store

FIXTURES = ROOT / "tests" / "fixtures" / "ap2"
START = datetime(2026, 11, 10, 14, 30, 5, tzinfo=timezone.utc)


class Clock:
    """A clock the test moves by hand."""

    def __init__(self, at=START):
        self.at = at

    def __call__(self):
        return self.at

    def tick(self, seconds=1):
        self.at += timedelta(seconds=seconds)


def plain(text="a thing", langs=("en",)) -> dict:
    return {line: {lang: f"{line} {text} ({lang})" for lang in langs} for line in cards.PLAIN_LINES}


def new_card(**over) -> dict:
    card = {"kind": "errand", "lane": "needs", "feed": "chat", "links": [], "pri": "P2", "origin": "source",
            "plain": plain(), "title": "A card"}
    card.update(over)
    return card


def tree(folder: Path) -> dict:
    """Every file and folder under `folder` with its bytes (None for a folder or a link's target name)."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(folder):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            rel = str(p.relative_to(folder))
            if p.is_symlink():
                out[rel] = ("link", os.readlink(p))
            elif p.is_dir():
                out[rel] = None
            else:
                out[rel] = p.read_bytes()
    return out


class ServerCase(unittest.TestCase):
    cfg = Config()

    def outside_data(self) -> dict:
        """`tree(self.tmp)` without the data folder and what is inside it."""
        rel = str(self.data.relative_to(self.tmp))
        return {k: v for k, v in tree(self.tmp).items() if k != rel and not k.startswith(rel + os.sep)}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ap2-server-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.plugin = self.tmp / "plugin"
        shutil.copytree(FIXTURES / "sources" / "plugin", self.plugin)
        self.data = datadir.ensure(self.tmp / "data")
        self.clock = Clock()
        self.srv = None

    def store(self, cfg=None) -> Store:
        return Store(self.data, cfg=cfg or self.cfg, clock=self.clock)

    def add(self, cfg=None, **over) -> dict:
        self.clock.tick()
        return self.store(cfg).add(new_card(**over), by="test")

    def start(self, cfg="default"):
        srv = server.make_server(self.data, port=0, cfg=self.cfg if cfg == "default" else cfg,
                                 plugin_dir=self.plugin, clock=self.clock)
        srv.log_stream = io.StringIO()
        self.addCleanup(srv.server_close)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.addCleanup(srv.shutdown)
        self.srv = srv
        self.port = srv.server_address[1]
        return srv

    def call(self, method, path, body=None, headers=None):
        """(status, headers, body) of one request; a JSON body is decoded."""
        if self.srv is None:
            self.start()
        conn = http.client.HTTPConnection(server.HOST, self.port, timeout=10)
        try:
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode("utf-8")
            hdrs = dict(headers or {})
            if body is not None and "Content-Type" not in hdrs:
                hdrs["Content-Type"] = "application/json"
            conn.request(method, path, body=body, headers=hdrs)
            r = conn.getresponse()
            data = r.read()
            hdrs = {k.lower(): v for k, v in r.getheaders()}
            if hdrs.get("content-type", "").startswith("application/json"):
                data = json.loads(data.decode("utf-8"))
            return r.status, hdrs, data
        finally:
            conn.close()

    def get(self, path, **kw):
        status, _, body = self.call("GET", path, **kw)
        return status, body

    def ok(self, path):
        status, body = self.get(path)
        self.assertEqual(status, 200, body)
        return body

    def ask(self, card_id, text, headers=None):
        return self.call("POST", f"/api/cards/{card_id}/questions", {"text": text}, headers)


# --- criterion 1: server, request guard ------------------------------------------------------------------------

class GuardTest(ServerCase):
    def test_binds_the_loopback_address_only(self):
        srv = self.start()
        self.assertEqual(server.PORT, 4317)
        self.assertEqual(server.HOST, "127.0.0.1")
        self.assertEqual(srv.server_address[0], "127.0.0.1")
        self.assertNotEqual(srv.server_address[1], 0)
        self.assertEqual(srv.url, "http://" + f"127.0.0.1:{srv.server_address[1]}/")

    def test_a_port_in_use_is_a_config_error_naming_it(self):
        held = socket.socket()
        self.addCleanup(held.close)
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        with self.assertRaisesRegex(ConfigError, rf"^cannot listen on 127\.0\.0\.1:{port}: "):
            server.make_server(self.data, port=port)

    def test_a_host_header_for_another_name_is_refused(self):
        self.start()
        for host in ("cards.example.com", f"cards.example.com:{self.port}", "127.0.0.1", f"127.0.0.1:{self.port + 1}",
                     f"localhost.example.com:{self.port}", f"0.0.0.0:{self.port}"):
            with self.subTest(host):
                status, hdrs, body = self.call("GET", "/api/state", headers={"Host": host})
                self.assertEqual(status, 403)
                self.assertIn("error", body)
                self.assertIn("Host", body["error"])
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"LocalHost:{self.port}"):
            with self.subTest(host):
                self.assertEqual(self.call("GET", "/api/state", headers={"Host": host})[0], 200)

    def test_a_foreign_host_is_refused_on_every_route_and_an_unknown_method(self):
        card = self.add()
        self.start()
        foreign = {"Host": "cards.example.com"}
        for route in ("/", "/api/state", "/api/cards", f"/api/cards/{card['id']}", "/api/sources",
                      "/api/sources/gh-mine", "/api/templates", "/api/templates/errand", "/api/settings",
                      "/favicon.ico"):
            with self.subTest(route):
                self.assertEqual(self.call("GET", route, headers=foreign)[0], 403)
        with self.subTest("BREW /"):
            self.assertEqual(self.call("BREW", "/", headers=foreign)[0], 403)

    def raw(self, request: bytes) -> bytes:
        with socket.create_connection((server.HOST, self.port), timeout=10) as s:
            s.sendall(request)
            reply = b""
            while chunk := s.recv(65536):
                reply += chunk
        return reply

    def test_a_request_with_no_host_header_is_refused(self):
        self.start()
        reply = self.raw(b"GET /api/state HTTP/1.0\r\n\r\n")
        self.assertTrue(reply.startswith(b"HTTP/1.0 403 "), reply[:80])

    def assert_status_line_and_safe_headers(self, reply: bytes):
        self.assertTrue(reply.startswith(b"HTTP/1.0 "), reply[:80])
        head = reply.split(b"\r\n\r\n", 1)[0]
        self.assertIn(b"\r\nCache-Control: no-store\r\n", head + b"\r\n")
        self.assertIn(b"\r\nX-Content-Type-Options: nosniff\r\n", head + b"\r\n")

    def test_a_request_line_with_no_version_gets_no_reply_or_a_status_line_and_the_safe_headers(self):
        self.start()
        reply = self.raw(f"GET /api/state\r\nHost: 127.0.0.1:{self.port}\r\n\r\n".encode("ascii"))
        # Some Python versions close a request line with no version without replying.
        # A bare body with no status line and headers is never allowed.
        if reply:
            self.assert_status_line_and_safe_headers(reply)

    def test_a_garbage_request_line_still_gets_a_status_line_and_the_safe_headers(self):
        self.start()
        self.assert_status_line_and_safe_headers(self.raw(b"GARBAGE\r\n\r\n"))

    def test_a_foreign_host_cannot_post_a_question_either(self):
        card = self.add()
        before = tree(self.data)
        self.start()
        status, _, _ = self.ask(card["id"], "Why?", headers={"Host": "cards.example.com"})
        self.assertEqual(status, 403)
        self.assertEqual(tree(self.data), before)

    def test_every_reply_has_the_safe_headers_and_no_cors(self):
        self.start()
        cases = [("GET", "/", None, {}), ("GET", "/api/state", None, {}), ("GET", "/nope", None, {}),
                 ("DELETE", "/api/state", None, {}), ("OPTIONS", "/api/state", None, {}),
                 ("GET", "/api/state", None, {"Host": "cards.example.com"}),
                 ("POST", "/api/cards/c-20261110-143005-abcdef/questions", b"{}", {"Content-Type": "text/plain"}),
                 ("TRACE", "/", None, {}), ("BREW", "/", None, {})]
        for method, path, body, headers in cases:
            with self.subTest(method=method, path=path, headers=headers):
                status, hdrs, _ = self.call(method, path, body, headers)
                self.assertEqual(hdrs.get("cache-control"), "no-store")
                self.assertEqual(hdrs.get("x-content-type-options"), "nosniff")
                self.assertEqual([k for k in hdrs if k.startswith("access-control-")], [])

    def test_options_is_405(self):
        self.start()
        status, _, body = self.call("OPTIONS", "/api/cards", headers={"Origin": "https://cards.example.com",
                                                                     "Access-Control-Request-Method": "POST"})
        self.assertEqual(status, 405)
        self.assertIn("error", body)

    def test_2xx_replies_are_not_logged_and_errors_are_with_a_utc_time_first(self):
        srv = self.start()
        self.ok("/api/state")
        self.assertEqual(self.call("GET", "/")[0], 200)
        status, hdrs, body = self.call("GET", "/favicon.ico")
        self.assertEqual((status, body), (204, b""))
        self.assertEqual(hdrs["cache-control"], "no-store")
        self.assertEqual(srv.log_stream.getvalue(), "")
        self.assertEqual(self.get("/api/nope")[0], 404)
        lines = srv.log_stream.getvalue().splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertRegex(lines[0], r"^2026-11-10T14:30:05Z .*GET /api/nope.* 404")

    def test_an_unknown_route_is_404_json(self):
        status, body = self.get("/api/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", body)


# --- criterion 2: lanes, projects, search ----------------------------------------------------------------------

class ListTest(ServerCase):
    def setUp(self):
        super().setUp()
        self.n_p2_old = self.add(title="needs old", pri="P2")
        self.n_p0 = self.add(title="needs urgent", pri="P0")
        self.n_p2_new = self.add(title="needs new", pri="P2", ask={"type": "answer"})
        self.run1 = self.add(title="running one", lane="running", plain=plain("pelican"))
        self.proj = self.add(title="The project", kind="project", lane="planned")
        self.kids = [self.add(title=f"task {i}", kind="code", lane=lane, parent=self.proj["id"])
                     for i, lane in enumerate(("planned", "running", "done"))]
        self.done_p0 = self.add(title="done urgent", lane="done", pri="P0")
        self.done_p3 = self.add(title="done late", lane="done", pri="P3", plain=plain("pelican"))

    def ids(self, body):
        return [r["id"] for r in body["cards"]]

    def test_state_counts_the_lanes_and_the_projects(self):
        st = self.ok("/api/state")
        self.assertEqual(st["lanes"], {"needs": 3, "running": 2, "planned": 2, "done": 3})
        self.assertEqual(st["running"], 2)
        self.assertEqual(st["projects"], [{"id": self.proj["id"], "title": "The project", "done": 1, "total": 3}])
        self.assertEqual(st["mode"], "shadow")
        self.assertEqual(st["primary_language"], "en")
        self.assertIsNone(st["secondary_language"])
        self.assertEqual(st["languages"], ["en"])
        self.assertIsNone(st["usage"])
        self.assertEqual(st["url"], self.srv.url)
        self.assertEqual(set(st), {"mode", "primary_language", "secondary_language", "languages", "lanes",
                                   "running", "projects", "heartbeat", "usage", "url"})

    def test_an_open_lane_is_by_priority_then_newest(self):
        body = self.ok("/api/cards?lane=needs")
        self.assertEqual(body["total"], 3)
        self.assertEqual(self.ids(body), [self.n_p0["id"], self.n_p2_new["id"], self.n_p2_old["id"]])

    def test_history_is_newest_first(self):
        self.assertEqual(self.ids(self.ok("/api/cards?lane=done")),
                         [self.done_p3["id"], self.done_p0["id"], self.kids[2]["id"]])

    def test_a_row_has_the_list_fields_only(self):
        row = self.ok("/api/cards?lane=needs")["cards"][1]
        self.assertEqual(row, {"id": self.n_p2_new["id"], "title": "needs new", "kind": "errand", "lane": "needs",
                               "pri": "P2", "feed": "chat", "parent": None, "origin": "source", "ask_type": "answer",
                               "plain_where": {"en": "where a thing (en)"},
                               "updated_at": self.n_p2_new["updated_at"]})

    def test_all_lanes_come_in_lane_order(self):
        body = self.ok("/api/cards")
        self.assertEqual(body["total"], 10)
        self.assertEqual([r["lane"] for r in body["cards"]], ["needs"] * 3 + ["running"] * 2 + ["planned"] * 2
                         + ["done"] * 3)

    def test_kind_and_parent_filters(self):
        self.assertEqual(self.ids(self.ok("/api/cards?kind=project")), [self.proj["id"]])
        body = self.ok(f"/api/cards?parent={self.proj['id']}")
        self.assertEqual(sorted(self.ids(body)), sorted(k["id"] for k in self.kids))
        self.assertEqual(self.ok(f"/api/cards?parent={self.proj['id']}&lane=done")["total"], 1)

    def test_q_goes_through_the_search_and_the_other_filters_apply_to_its_result(self):
        self.assertEqual(sorted(self.ids(self.ok("/api/cards?q=PELICAN"))),
                         sorted([self.run1["id"], self.done_p3["id"]]))
        self.assertEqual(self.ids(self.ok("/api/cards?q=pelican&lane=done")), [self.done_p3["id"]])
        self.assertEqual(self.ok("/api/cards?q=nothing+like+this")["total"], 0)
        self.assertEqual(self.ok("/api/cards?q=%20%20")["total"], 10)  # blank text is no filter

    def test_limit(self):
        body = self.ok("/api/cards?limit=2")
        self.assertEqual(body["total"], 10)
        self.assertEqual(len(body["cards"]), 2)
        self.assertEqual(len(self.ok("/api/cards?limit=5000")["cards"]), 10)
        self.assertEqual(server.LIST_LIMIT, 500)
        self.assertEqual(server.LIST_MAX, 5000)

    def test_bad_filters_are_400(self):
        for query in ("lane=nope", "limit=0", "limit=5001", "limit=ten", "limit=-1", "q=%FF", "q=%ED%A0%80",
                      "parent=nope"):
            with self.subTest(query):
                status, body = self.get(f"/api/cards?{query}")
                self.assertEqual(status, 400, body)
                self.assertIn("error", body)

    def test_a_damaged_card_file_is_500_naming_it(self):
        path = self.store().path(self.n_p0["id"])
        path.write_text("{", encoding="utf-8")
        for route in ("/api/cards", "/api/state", "/api/cards?lane=needs"):
            with self.subTest(route):
                status, body = self.get(route)
                self.assertEqual(status, 500)
                self.assertIn(str(path), body["error"])
                self.assertNotIn("\n", body["error"])


# --- criterion 3: heartbeat ------------------------------------------------------------------------------------

class HeartbeatTest(ServerCase):
    def beat(self):
        return self.ok("/api/state")["heartbeat"]

    def write_gate(self, text):
        (self.data / gate.HEARTBEAT).write_text(text, encoding="utf-8")

    def test_179_seconds_is_not_late_and_180_is(self):
        self.write_gate(json.dumps({"last_run_at": cards.stamp(START)}))
        self.clock.at = START + timedelta(seconds=179)
        self.assertEqual(self.beat(), {"last_run_at": "2026-11-10T14:30:05Z", "age_seconds": 179, "late": False})
        self.clock.at = START + timedelta(seconds=180)
        self.assertEqual(self.beat(), {"last_run_at": "2026-11-10T14:30:05Z", "age_seconds": 180, "late": True})

    def test_no_gate_file_is_late(self):
        self.assertEqual(self.beat(), {"last_run_at": None, "age_seconds": None, "late": True})

    def test_a_file_that_cannot_be_used_adds_an_error_and_is_late(self):
        for text in ("{", "[]", '{"last_run_at": 5}', '{"last_run_at": "yesterday"}', "{}", "\udcff"):
            with self.subTest(text):
                if text == "\udcff":
                    (self.data / gate.HEARTBEAT).write_bytes(b"\xff\xfe")
                else:
                    self.write_gate(text)
                beat = self.beat()
                self.assertTrue(beat["late"])
                self.assertIsNone(beat["age_seconds"])
                self.assertIn(gate.HEARTBEAT, beat["error"])

    def test_a_last_run_ahead_of_the_clock_is_reported(self):
        self.write_gate(json.dumps({"last_run_at": cards.stamp(START + timedelta(minutes=5))}))
        beat = self.beat()
        self.assertTrue(beat["late"])
        self.assertIn("ahead", beat["error"])

    def test_a_heartbeat_the_gate_wrote_itself(self):
        gate.run(self.data, START)
        self.clock.at = START + timedelta(seconds=25)
        self.assertEqual(self.beat(), {"last_run_at": "2026-11-10T14:30:05Z", "age_seconds": 25, "late": False})


# --- criterion 4: the full card --------------------------------------------------------------------------------

class CardTest(ServerCase):
    def read_card(self) -> dict:
        reply = self.tmp / "gh-pr-list.json"
        shutil.copy(FIXTURES / "sources" / "replies" / "gh-pr-list.json", reply)
        Engine(self.data, cfg=self.cfg, plugin_dir=self.plugin, clock=self.clock).read_saved("gh-mine", reply)
        return next(c for c in self.store().list() if c["source"]["record"]["id"] == "42")

    def test_a_card_from_a_source_has_its_source_block_and_link(self):
        card = self.read_card()
        child = self.add(title="child", parent=card["id"])
        other = self.add(title="other")
        gone = "c-20250101-000000-abcdef"
        card = self.store().update(card["id"], {"links": [other["id"], gone]}, by="test")
        body = self.ok(f"/api/cards/{card['id']}")
        self.assertEqual(set(body), {"card", "template", "view", "source_block", "children", "links", "activity"})
        self.assertEqual(body["card"], card)
        self.assertIsNone(body["template"])  # gh-mine sorts to pr_review, a kind with no template
        self.assertEqual(body["view"], ["source_block", "children", "links", "questions", "activity"])
        sb = body["source_block"]
        self.assertEqual(sb["name"], "gh-mine")
        self.assertEqual(sb["author"], "octo-dev")
        self.assertEqual(sb["place"], "widgets")
        self.assertEqual(sb["at"], "2026-11-09T17:02:11Z")
        self.assertEqual(sb["text"], "Retry failed card payments once, 2 seconds later")
        self.assertEqual(sb["link"], "https://github.com/example/widgets/pull/42")
        self.assertEqual(sb["record"], card["source"]["record"])
        self.assertNotIn("link_refused", sb)
        self.assertEqual([r["id"] for r in body["children"]], [child["id"]])
        self.assertEqual(body["children"][0]["title"], "child")
        self.assertEqual(body["links"], [{**body["links"][0]}, {"id": gone, "missing": True}])
        self.assertEqual(body["links"][0]["id"], other["id"])
        self.assertEqual(body["links"][0]["title"], "other")
        self.assertEqual(body["activity"], card["audit"])

    def test_a_link_that_is_not_http_is_refused(self):
        for link in ("javascript:alert(1)", "data:text/html,x", "//example.com/x", " https://example.com", 5):
            with self.subTest(link):
                card = self.add(kind="question", source={"name": "chat", "link": link,
                                                         "record": {"text": "hi", "author": "someone"}})
                sb = self.ok(f"/api/cards/{card['id']}")["source_block"]
                self.assertIsNone(sb["link"])
                self.assertTrue(sb["link_refused"])
        card = self.add(kind="question", source={"name": "chat", "link": None, "record": {"text": "hi"}})
        sb = self.ok(f"/api/cards/{card['id']}")["source_block"]
        self.assertIsNone(sb["link"])
        self.assertNotIn("link_refused", sb)
        card = self.add(kind="question", source={"name": "chat", "link": "HTTP://example.com/a", "record": {}})
        self.assertEqual(self.ok(f"/api/cards/{card['id']}")["source_block"]["link"], "HTTP://example.com/a")

    def test_a_template_view_gets_questions_added_at_the_end(self):
        card = self.add(kind="project")
        body = self.ok(f"/api/cards/{card['id']}")
        builtin = templates.catalog(self.data)["project"]
        self.assertEqual(body["template"], {"kind": "project", "title": "Project", "origin": "builtin",
                                            "view": builtin.data["view"]})
        self.assertEqual(body["view"], builtin.data["view"] + ["questions"])
        self.assertIsNone(body["source_block"])  # the card has no source

    def test_a_template_that_lists_questions_keeps_its_order(self):
        card = self.add(kind="question", source={"name": "chat", "link": None, "record": {"text": "hi"}})
        body = self.ok(f"/api/cards/{card['id']}")
        self.assertEqual(body["view"], ["source_block", "draft", "findings", "questions", "activity"])

    def test_an_unknown_kind_with_no_source(self):
        card = self.add(kind="mystery")
        body = self.ok(f"/api/cards/{card['id']}")
        self.assertIsNone(body["template"])
        self.assertEqual(body["view"], ["children", "links", "questions", "activity"])
        self.assertIsNone(body["source_block"])

    def test_a_bad_user_template_still_serves_the_card(self):
        card = self.add(kind="project")
        bad = self.data / "templates" / "project.yaml"
        bad.write_text("kind: project\nview: [nosuch]\nbuttons: []\nroles: []\n", encoding="utf-8")
        body = self.ok(f"/api/cards/{card['id']}")
        self.assertIsNone(body["template"])
        self.assertIn(str(bad), body["template_error"])
        self.assertEqual(body["card"]["id"], card["id"])
        self.assertEqual(body["view"], ["children", "links", "questions", "activity"])

    def test_no_such_card_and_a_bad_id_are_404(self):
        for card_id in ("c-20250101-000000-abcdef", "nope", "..%2F..%2Fconfig.yaml", "c-20250101-000000-ABCDEF"):
            with self.subTest(card_id):
                status, body = self.get(f"/api/cards/{card_id}")
                self.assertEqual(status, 404)
                self.assertIn("error", body)

    def test_a_damaged_card_is_500(self):
        card = self.add()
        self.store().path(card["id"]).write_text("{", encoding="utf-8")
        status, body = self.get(f"/api/cards/{card['id']}")
        self.assertEqual(status, 500)
        self.assertIn(card["id"], body["error"])


# --- criterion 5: sources, templates, settings -----------------------------------------------------------------

class ManageTest(ServerCase):
    def test_sources_list_each_summary_and_its_card_count(self):
        reply = self.tmp / "gh-pr-list.json"
        shutil.copy(FIXTURES / "sources" / "replies" / "gh-pr-list.json", reply)
        Engine(self.data, cfg=self.cfg, plugin_dir=self.plugin, clock=self.clock).read_saved("gh-mine", reply)
        made = len([c for c in self.store().list() if c["feed"] == "gh-mine"])
        self.assertGreater(made, 0)
        body = self.ok("/api/sources")
        self.assertEqual([s["source"] for s in body], ["gh-mine", "slack-mentions"])
        gh = body[0]
        self.assertEqual(gh["cards"], made)
        self.assertEqual(body[1]["cards"], 0)
        self.assertEqual({k: v for k, v in gh.items() if k != "cards"},
                         {"source": "gh-mine", "brings": "messages", "via": "command", "fill": "paths",
                          "every": "5m", "origin": "built-in", "file": str(self.plugin / "gh-mine.yaml")})

    def test_one_source_adds_its_data_and_files(self):
        user = FIXTURES / "sources" / "user" / "slack-mentions.yaml"
        shutil.copy(user, self.data / "sources" / "slack-mentions.yaml")
        body = self.ok("/api/sources/slack-mentions")
        self.assertEqual(body["files"], [str(self.plugin / "slack-mentions.yaml"),
                                         str(self.data / "sources" / "slack-mentions.yaml")])
        self.assertEqual(body["data"]["source"], "slack-mentions")
        self.assertEqual(body["cards"], 0)
        self.assertTrue(body["origin"].startswith("user"))
        self.assertEqual(self.get("/api/sources/nosuch")[0], 404)
        self.assertEqual(self.get("/api/sources/..%2Fconfig")[0], 404)

    def test_a_bad_source_file_is_500_with_the_loaders_line(self):
        bad = self.data / "sources" / "broken.yaml"
        bad.write_text("source: broken\nbrings: nothing\n", encoding="utf-8")
        for route in ("/api/sources", "/api/sources/broken"):
            with self.subTest(route):
                status, body = self.get(route)
                self.assertEqual(status, 500)
                self.assertTrue(body["error"].startswith(str(bad)), body["error"])

    def test_templates_and_one_template(self):
        shutil.copy(FIXTURES / "templates" / "parcel.yaml", self.data / "templates" / "parcel.yaml")
        self.add(kind="parcel")
        body = self.ok("/api/templates")
        cat = templates.catalog(self.data)
        self.assertEqual(body, [templates.summary(t) for t in cat.values()])
        self.assertIn("parcel", [t["kind"] for t in body])
        one = self.ok("/api/templates/parcel")
        self.assertEqual({k: v for k, v in one.items() if k != "cards"}, templates.describe(cat["parcel"]))
        self.assertEqual(one["cards"], 1)
        self.assertEqual(self.ok("/api/templates/question")["cards"], 0)
        self.assertEqual(self.get("/api/templates/nosuch")[0], 404)

    def test_a_bad_template_file_is_500(self):
        bad = self.data / "templates" / "parcel.yaml"
        bad.write_text("kind: parcel\n", encoding="utf-8")
        for route in ("/api/templates", "/api/templates/parcel", "/api/templates/question"):
            with self.subTest(route):
                status, body = self.get(route)
                self.assertEqual(status, 500)
                self.assertTrue(body["error"].startswith(str(bad)), body["error"])

    def test_settings_show_the_config_and_disk_use_without_following_links(self):
        self.add()
        (self.data / "attachments" / "a.bin").write_bytes(b"x" * 1000)
        outside = self.tmp / "big.bin"
        outside.write_bytes(b"y" * 50000)
        (self.data / "attachments" / "link.bin").symlink_to(outside)
        (self.data / "chat").rmdir()
        (self.data / "chat").symlink_to(self.tmp / "plugin")
        body = self.ok("/api/settings")
        self.assertEqual(body["config"], self.cfg.as_dict())
        self.assertEqual(body["data_dir"], str(self.data))
        self.assertEqual(body["url"], self.srv.url)
        self.assertEqual(set(body["disk"]), set(datadir.FOLDERS) | {"index.sqlite"})
        att = self.data / "attachments"
        expect = (os.lstat(att).st_size + 1000 + os.lstat(att / "link.bin").st_size)
        self.assertEqual(body["disk"]["attachments"], expect)
        self.assertLess(body["disk"]["attachments"], 50000)
        self.assertEqual(body["disk"]["chat"], os.lstat(self.data / "chat").st_size)
        self.assertEqual(body["disk"]["index.sqlite"], os.lstat(self.data / "index.sqlite").st_size)
        self.assertGreater(body["disk"]["items"], 0)

    def test_settings_read_the_config_file_when_none_is_given(self):
        (self.data / "config.yaml").write_text("mode: live\nsecondary_language: zh\n", encoding="utf-8")
        self.start(cfg=None)
        body = self.ok("/api/settings")
        self.assertEqual(body["config"]["mode"], "live")
        st = self.ok("/api/state")
        self.assertEqual((st["mode"], st["secondary_language"], st["languages"]), ("live", "zh", ["en", "zh"]))
        (self.data / "config.yaml").write_text("mode: loud\n", encoding="utf-8")
        status, body = self.get("/api/state")
        self.assertEqual(status, 500)
        self.assertIn("config.yaml", body["error"])

    def test_every_get_route_leaves_the_data_folder_byte_for_byte(self):
        shutil.copy(FIXTURES / "templates" / "parcel.yaml", self.data / "templates" / "parcel.yaml")
        reply = self.tmp / "gh-pr-list.json"
        shutil.copy(FIXTURES / "sources" / "replies" / "gh-pr-list.json", reply)
        Engine(self.data, cfg=self.cfg, plugin_dir=self.plugin, clock=self.clock).read_saved("gh-mine", reply)
        card = self.add(kind="parcel")
        gate.run(self.data, START)
        before, outside_before = tree(self.data), self.outside_data()
        self.start()
        for route in ("/", "/api/state", "/api/cards", "/api/cards?lane=needs&q=card", f"/api/cards/{card['id']}",
                      "/api/sources", "/api/sources/gh-mine", "/api/templates", "/api/templates/parcel",
                      "/api/settings", "/api/nope", f"/api/cards/{card['id']}/questions"):
            with self.subTest(route):
                self.call("GET", route)
                self.assertEqual(tree(self.data), before)
                self.assertEqual(self.outside_data(), outside_before)

    def test_reads_on_a_fresh_data_folder_create_nothing(self):
        before = tree(self.data)
        self.start()
        for route in ("/", "/api/state", "/api/cards", "/api/cards?lane=done", "/api/cards/c-20250101-000000-abcdef",
                      "/api/sources", "/api/templates", "/api/settings"):
            with self.subTest(route):
                self.assertIn(self.call("GET", route)[0], (200, 404))
                self.assertEqual(tree(self.data), before)


# --- criterion 6: ask about this card --------------------------------------------------------------------------

class QuestionTest(ServerCase):
    def test_a_question_is_saved_on_the_card_and_in_its_activity(self):
        card = self.add()
        status, _, body = self.ask(card["id"], "  Why is this here?  ")
        self.assertEqual(status, 201, body)
        q = body["question"]
        self.assertRegex(q["id"], r"^q-20261110-\d{6}-[0-9a-f]{6}$")
        self.assertEqual(q, {"id": q["id"], "at": cards.stamp(self.clock()), "by": "page",
                             "text": "  Why is this here?  ", "status": "pending", "answer": None})
        self.assertEqual(body["card"]["questions"], [q])
        shown = self.ok(f"/api/cards/{card['id']}")
        self.assertEqual(shown["card"]["questions"], [q])
        last = shown["activity"][-1]
        self.assertEqual((last["by"], last["did"]), ("page", f"ask {q['id']}"))
        self.assertIsNotNone(last["restore"])
        self.assertTrue((self.data / "restore" / f"{last['restore']}.json").is_file())

    def test_the_text_may_be_2000_characters_and_the_own_origin_is_allowed(self):
        card = self.add()
        self.start()
        for origin in (None, "http://" + f"127.0.0.1:{self.port}", "http://" + f"localhost:{self.port}"):
            with self.subTest(origin):
                headers = {"Origin": origin} if origin else {}
                status, _, body = self.ask(card["id"], "x" * 2000, headers)
                self.assertEqual(status, 201, body)
        status, _, _ = self.call("POST", f"/api/cards/{card['id']}/questions", json.dumps({"text": "ok"}).encode(),
                                 {"Content-Type": "application/json; charset=utf-8"})
        self.assertEqual(status, 201)
        self.assertEqual(len(self.store().get(card["id"])["questions"]), 4)

    def test_two_threads_posting_at_once_lose_nothing(self):
        card = self.add()
        self.start()
        errors = []

        def post(n):
            for i in range(20):
                status, _, body = self.ask(card["id"], f"thread {n} question {i}")
                if status != 201:
                    errors.append((status, body))

        threads = [threading.Thread(target=post, args=(n,)) for n in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(errors, [])
        saved = self.store().get(card["id"])
        self.assertEqual(len(saved["questions"]), 40)
        self.assertEqual(len({q["id"] for q in saved["questions"]}), 40)
        self.assertEqual(len([a for a in saved["audit"] if a["did"].startswith("ask ")]), 40)

    def test_each_refusal_writes_nothing(self):
        card = self.add()
        odd = self.add(questions={"not": "a list"})
        self.start()
        path = f"/api/cards/{card['id']}/questions"
        cases = [
            ("missing text", path, b"{}", {}, 400),
            ("blank text", path, json.dumps({"text": " \n\t "}).encode(), {}, 400),
            ("text not text", path, json.dumps({"text": 5}).encode(), {}, 400),
            ("too long", path, json.dumps({"text": "x" * 2001}).encode(), {}, 400),
            ("lone surrogate", path, b'{"text": "a \\ud800 b"}', {}, 400),
            ("not utf-8", path, b'{"text": "\xff"}', {}, 400),
            ("not json", path, b"text=hi", {}, 400),
            ("a list", path, b'["hi"]', {}, 400),
            ("nan", path, b'{"text": "hi", "n": NaN}', {}, 400),
            ("too big", path, json.dumps({"text": "x" * (64 * 1024)}).encode(), {}, 413),
            ("form post", path, b"text=hi", {"Content-Type": "application/x-www-form-urlencoded"}, 415),
            ("plain text", path, b'{"text": "hi"}', {"Content-Type": "text/plain"}, 415),
            ("no type", path, b'{"text": "hi"}', {"Content-Type": ""}, 415),
            ("other origin", path, b'{"text": "hi"}', {"Origin": "https://cards.example.com"}, 403),
            ("null origin", path, b'{"text": "hi"}', {"Origin": "null"}, 403),
            ("other port", path, b'{"text": "hi"}', {"Origin": "http://" + f"127.0.0.1:{self.port + 1}"}, 403),
            ("questions not a list", f"/api/cards/{odd['id']}/questions", b'{"text": "hi"}', {}, 409),
            ("no such card", "/api/cards/c-20250101-000000-abcdef/questions", b'{"text": "hi"}', {}, 404),
            ("bad id", "/api/cards/nope/questions", b'{"text": "hi"}', {}, 404),
        ]
        for name, route, body, headers, want in cases:
            with self.subTest(name):
                before = tree(self.data)
                status, _, reply = self.call("POST", route, body, headers)
                self.assertEqual(status, want, reply)
                self.assertIn("error", reply)
                self.assertEqual(tree(self.data), before)

    def test_a_card_that_fails_the_stores_checks_is_409(self):
        card = self.add()
        self.start(cfg=Config(secondary_language="zh"))
        before = tree(self.data)
        status, _, body = self.ask(card["id"], "Why?")
        self.assertEqual(status, 409)
        self.assertIn("plain.what.zh missing", body["error"])
        self.assertEqual(tree(self.data), before)

    def test_a_body_with_no_length_is_refused(self):
        card = self.add()
        self.start()
        with socket.create_connection((server.HOST, self.port), timeout=10) as s:
            s.sendall(f"POST /api/cards/{card['id']}/questions HTTP/1.0\r\nHost: 127.0.0.1:{self.port}\r\n"
                      "Content-Type: application/json\r\n\r\n".encode())
            s.shutdown(socket.SHUT_WR)
            reply = b""
            while chunk := s.recv(65536):
                reply += chunk
        self.assertTrue(reply.startswith(b"HTTP/1.0 411 "), reply[:80])
        self.assertNotIn("questions", self.store().get(card["id"]))

    def test_nothing_starts_a_process(self):
        card = self.add()
        self.start()
        with mock.patch("subprocess.Popen", side_effect=AssertionError("a process was started")) as popen, \
                mock.patch("os.posix_spawn", side_effect=AssertionError("spawn")) as spawn:
            self.assertEqual(self.ask(card["id"], "Why?")[0], 201)
            for route in ("/", "/api/state", "/api/cards", f"/api/cards/{card['id']}", "/api/sources",
                          "/api/sources/gh-mine", "/api/templates", "/api/templates/question", "/api/settings"):
                self.assertEqual(self.get(route)[0], 200, route)
        popen.assert_not_called()
        spawn.assert_not_called()


class AskAboutStoreTest(ServerCase):
    def test_ask_about_appends_a_pending_question_under_the_lock(self):
        card = self.add()
        out = self.store().ask_about(card["id"], "What now?", by="page")
        q = out["questions"][-1]
        self.assertEqual(q["status"], "pending")
        self.assertIsNone(q["answer"])
        self.assertEqual(q["by"], "page")
        self.assertEqual(out["audit"][-1]["did"], f"ask {q['id']}")
        again = self.store().ask_about(card["id"], "And then?", by="page")
        self.assertEqual([x["text"] for x in again["questions"]], ["What now?", "And then?"])

    def test_ask_about_refuses_bad_text_and_a_bad_questions_field(self):
        card = self.add()
        odd = self.add(questions="nope")
        for text in ("", "  ", None, "a\ud800"):
            with self.subTest(text):
                with self.assertRaises(cards.CardError):
                    self.store().ask_about(card["id"], text, by="page")
        with self.assertRaisesRegex(cards.CardError, "questions must be a list"):
            self.store().ask_about(odd["id"], "hi", by="page")
        with self.assertRaisesRegex(cards.CardError, "no card"):
            self.store().ask_about("c-20250101-000000-abcdef", "hi", by="page")
        self.assertNotIn("questions", self.store().get(card["id"]))


# --- criterion 7: read-only and shadow mode --------------------------------------------------------------------

class ReadOnlyTest(ServerCase):
    def test_every_other_write_method_is_405_in_both_modes(self):
        card = self.add()
        for mode in ("shadow", "live"):
            self.srv = None
            self.start(cfg=Config(mode=mode))
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                for route in ("/", "/api/state", "/api/cards", f"/api/cards/{card['id']}", "/api/settings",
                              "/api/sources/gh-mine", "/api/templates/question", "/api/nope",
                              f"/api/cards/{card['id']}/lane", f"/api/cards/{card['id']}/restore"):
                    with self.subTest(mode=mode, method=method, route=route):
                        before = tree(self.data)
                        status, _, body = self.call(method, route, {"lane": "done", "text": "hi"})
                        self.assertEqual(status, 405, body)
                        self.assertEqual(tree(self.data), before)
            for method in ("PUT", "PATCH", "DELETE"):
                with self.subTest(mode=mode, method=method, route="questions"):
                    before = tree(self.data)
                    status, _, _ = self.call(method, f"/api/cards/{card['id']}/questions", {"text": "hi"})
                    self.assertEqual(status, 405)
                    self.assertEqual(tree(self.data), before)

    def test_a_question_in_shadow_mode_writes_only_inside_the_data_folder(self):
        home = self.tmp / "home"
        home.mkdir()
        sibling = self.tmp / "sibling"
        sibling.mkdir()
        (sibling / "keep.txt").write_text("keep", encoding="utf-8")
        card = self.add()
        real_connect = socket.create_connection
        outside = []

        def connect(address, *args, **kwargs):
            if address[0] != "127.0.0.1":
                outside.append(address)
                raise OSError("no network beyond 127.0.0.1 in this test")
            return real_connect(address, *args, **kwargs)

        with mock.patch.dict(os.environ, {"HOME": str(home)}), \
                mock.patch("socket.create_connection", connect), \
                mock.patch("assistant.config.require_live", side_effect=AssertionError("require_live")) as live:
            self.start(cfg=Config(mode="shadow"))
            home_before, sibling_before, plugin_before = tree(home), tree(sibling), tree(self.plugin)
            data_before, outside_before = tree(self.data), self.outside_data()
            status, _, body = self.ask(card["id"], "Why?")
            self.assertEqual(status, 201, body)
            for route in ("/api/state", "/api/cards", f"/api/cards/{card['id']}", "/api/sources", "/api/settings"):
                self.ok(route)
            self.assertEqual(tree(home), home_before)
            self.assertEqual(tree(sibling), sibling_before)
            self.assertEqual(tree(self.plugin), plugin_before)
            self.assertEqual(self.outside_data(), outside_before)
            self.assertNotEqual(tree(self.data), data_before)
        self.assertEqual(outside, [])
        live.assert_not_called()


# --- criterion 8: the page -------------------------------------------------------------------------------------

PAGE = ROOT / "lib" / "assistant" / "builtin" / "page" / "local.html"


class PageTest(ServerCase):
    def test_the_page_is_served_with_its_policy(self):
        status, hdrs, body = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(hdrs["content-type"], "text/html; charset=utf-8")
        self.assertEqual(hdrs["content-security-policy"],
                         "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                         "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; "
                         "frame-ancestors 'none'")
        self.assertEqual(body, PAGE.read_bytes())
        self.assertEqual(server.PAGE, PAGE)

    def test_the_page_holds_each_screens_root(self):
        text = PAGE.read_text(encoding="utf-8")
        for screen in ("top", "search", "heartbeat", "running", "lang-switch", "rail", "rail-lanes",
                       "rail-projects", "rail-manage", "list", "card", "sources", "source", "templates", "template",
                       "settings", "labels"):
            with self.subTest(screen):
                self.assertRegex(text, rf'id="{screen}"')
        for route in ("#/lane/", "#/card/", "#/sources", "#/templates", "#/settings", "#/search/", "#/project/"):
            self.assertIn(route, text)

    def test_the_page_writes_no_html_and_loads_nothing_from_another_host(self):
        text = PAGE.read_text(encoding="utf-8")
        for call in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            self.assertNotIn(call, text)
        for pattern in (r"""\bsrc\s*=\s*["']?\s*https?:""", r"""\bhref\s*=\s*["']?\s*https?:""",
                        r"""@import[^;]*https?:""", r"""url\(\s*["']?\s*https?:"""):
            self.assertIsNone(re.search(pattern, text, re.IGNORECASE), pattern)
        self.assertNotIn("<link", text)
        self.assertNotIn("eval(", text)
        self.assertNotIn("new Function", text)

    def test_the_label_table_has_the_same_keys_for_en_and_zh(self):
        text = PAGE.read_text(encoding="utf-8")
        m = re.search(r'<script type="application/json" id="labels">(.*?)</script>', text, re.S)
        self.assertIsNotNone(m)
        labels = json.loads(m.group(1))
        self.assertEqual(sorted(labels), ["en", "zh"])
        self.assertEqual(sorted(labels["en"]), sorted(labels["zh"]))
        self.assertGreater(len(labels["en"]), 40)
        for lang, table in labels.items():
            for key, value in table.items():
                self.assertTrue(isinstance(value, str) and value.strip(), (lang, key))
        used = set(re.findall(r"""\bL\(\s*["']([a-z0-9_.]+)["']""", text))
        used |= set(re.findall(r'data-l="([a-z0-9_.]+)"', text))
        used = {k for k in used if not k.endswith(".")}  # L("lane." + lane) builds its key
        self.assertEqual(sorted(used - set(labels["en"])), [])
        for lane in cards.LANES:
            self.assertIn(f"lane.{lane}", labels["en"])


if __name__ == "__main__":
    unittest.main()
