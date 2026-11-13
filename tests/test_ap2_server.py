"""The local page's server (design 9.1): routes, the request guard, the question POST, read-only and the page."""
from __future__ import annotations

import http.client
import io
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT
from tests.test_ap2_store import BLANK_SLOTS, NOT_BLANK_SLOTS

from assistant import cap, cards, datadir, gate, profiles, server, templates
from assistant.config import Config, ConfigError
from assistant.engine import Engine
from assistant.resources import WorkerError
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
                                 plugin_dir=self.plugin, clock=self.clock, usage_file=self.tmp / "claude.json")
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

    def test_a_port_bound_but_not_listening_is_a_config_error_from_the_bind(self):
        # Nothing answers the probe there, so the bind itself fails (no SO_REUSEADDR on the held socket).
        held = socket.socket()
        self.addCleanup(held.close)
        held.bind(("127.0.0.1", 0))
        port = held.getsockname()[1]
        with self.assertRaisesRegex(ConfigError, rf"^cannot listen on 127\.0\.0\.1:{port}: ") as caught:
            server.make_server(self.data, port=port)
        self.assertNotIn("in use by another program", str(caught.exception))  # the bind's error, not the probe's
        cause = caught.exception.__cause__
        self.assertIsInstance(cause, OSError)
        self.assertIn(str(cause), str(caught.exception))  # the message carries the OS's own reason

    def test_a_probe_that_gets_no_answer_takes_the_port(self):
        # A probe that times out means nothing listens there: the port is free (whatever the OS does on refusal).
        free = socket.socket()
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
        free.close()
        with mock.patch("assistant.server.socket.create_connection",
                        side_effect=TimeoutError("timed out")) as probe:
            srv = server.make_server(self.data, port=port)
        self.addCleanup(srv.server_close)
        probe.assert_called_once()
        self.assertLessEqual(probe.call_args.kwargs["timeout"], 1)  # a silent port must not stall the start
        self.assertEqual(srv.server_address, ("127.0.0.1", port))

    def test_a_port_another_program_holds_on_all_addresses_is_a_config_error(self):
        # macOS lets SO_REUSEADDR bind 127.0.0.1:<port> while 0.0.0.0:<port> listens; Linux refuses the bind.
        held = socket.socket()
        self.addCleanup(held.close)
        held.bind(("0.0.0.0", 0))
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

    def test_two_host_headers_are_refused(self):
        self.start()
        mine, foreign = f"127.0.0.1:{self.port}", "cards.example.com"
        for name, first, second in (("valid then foreign", mine, foreign), ("foreign then valid", foreign, mine),
                                    ("the same valid one twice", mine, mine)):
            with self.subTest(name):
                reply = self.raw(f"GET /api/state HTTP/1.0\r\nHost: {first}\r\nHost: {second}\r\n\r\n"
                                 .encode("ascii"))
                self.assertTrue(reply.startswith(b"HTTP/1.0 403"), reply[:80])

    def assert_status_line_and_safe_headers(self, reply: bytes):
        self.assertTrue(reply.startswith(b"HTTP/1.0 "), reply[:80])
        head = reply.split(b"\r\n\r\n", 1)[0]
        self.assertIn(b"\r\nCache-Control: no-store\r\n", head + b"\r\n")
        self.assertIn(b"\r\nX-Content-Type-Options: nosniff\r\n", head + b"\r\n")
        self.assertIn(b"\r\nCross-Origin-Opener-Policy: same-origin\r\n", head + b"\r\n")

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

    def test_an_api_request_a_browser_marks_as_from_another_site_is_refused(self):
        card = self.add()
        before = tree(self.data)
        self.start()
        origin = {"Origin": "http://" + f"127.0.0.1:{self.port}"}
        for site in ("cross-site", "same-site", " Cross-Site "):
            for method, path, body, headers in (("GET", "/api/cards?q=x", None, {}), ("GET", "/api/state", None, {}),
                                                ("POST", f"/api/cards/{card['id']}/questions", {"text": "Why?"},
                                                 origin)):
                with self.subTest(site=site, method=method, path=path), \
                        mock.patch("assistant.store.Store.search", side_effect=AssertionError("the search ran")):
                    status, hdrs, reply = self.call(method, path, body, {**headers, "Sec-Fetch-Site": site})
                    self.assertEqual(status, 403, reply)
                    self.assertIn("another site", reply["error"])
                    self.assertEqual(hdrs.get("cache-control"), "no-store")
                    self.assertEqual(tree(self.data), before)
        for site in ("same-origin", "none", "Same-Origin"):
            with self.subTest(site=site):
                self.assertEqual(self.call("GET", "/api/state", headers={"Sec-Fetch-Site": site})[0], 200)
        for path in ("/api/state", "/api/cards", f"/api/cards/{card['id']}", "/api/sources", "/api/sources/gh-mine",
                     "/api/templates", "/api/templates/errand", "/api/settings"):
            with self.subTest("every GET route", path=path):
                status, _, reply = self.call("GET", path, headers={"Sec-Fetch-Site": "cross-site"})
                self.assertEqual(status, 403, reply)
                self.assertIn("another site", reply["error"])
        with self.subTest("the page itself"):
            status, hdrs, page = self.call("GET", "/", headers={"Sec-Fetch-Site": "cross-site"})
            self.assertEqual(status, 200)
            self.assertTrue(hdrs["content-type"].startswith("text/html"))
        with self.subTest("a foreign Host is refused for its Host first"):
            status, _, reply = self.call("GET", "/api/state",
                                         headers={"Host": "cards.example.com", "Sec-Fetch-Site": "cross-site"})
            self.assertEqual(status, 403)
            self.assertIn("Host", reply["error"])

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
                self.assertEqual(hdrs.get("cross-origin-opener-policy"), "same-origin")
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
        self.assertEqual(st["projects"], [{"id": self.proj["id"], "title": {"en": "The project"}, "done": 1,
                                            "total": 3}])
        self.assertEqual(st["mode"], "shadow")
        self.assertEqual(st["primary_language"], "en")
        self.assertIsNone(st["secondary_language"])
        self.assertEqual(st["languages"], ["en"])
        self.assertEqual(st["usage"]["mode"], "subscription")
        self.assertIsNone(st["usage"]["windows"])  # the test's usage file does not exist
        self.assertEqual(st["access"]["profile"]["name"], "subscription")
        self.assertEqual(set(st["access"]), {"profile", "profile_error", "page_sessions", "profiles",
                                             "broken_profiles", "key_error", "cap"})
        self.assertEqual(set(st["usage"]), {"mode", "windows", "stale", "credits_used_usd", "credits_limit_usd",
                                            "gateway"})
        self.assertEqual(st["url"], self.srv.url)
        self.assertEqual(set(st), {"mode", "primary_language", "secondary_language", "languages", "lanes",
                                   "running", "projects", "heartbeat", "access", "usage", "url"})

    def test_with_no_usage_file_the_state_reads_homes_claude_json(self):
        # `make_server` with no `usage_file` reads `~/.claude.json` at each request: HOME is a temp folder here.
        from tests.test_ap2_profileview import usage_json
        home = self.tmp / "home"
        home.mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            srv = server.make_server(self.data, port=0, cfg=self.cfg, plugin_dir=self.plugin, clock=self.clock)
            srv.log_stream = io.StringIO()
            self.addCleanup(srv.server_close)
            thread = threading.Thread(target=srv.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(thread.join, 10)
            self.addCleanup(srv.shutdown)
            self.srv, self.port = srv, srv.server_address[1]
            self.assertIsNone(self.ok("/api/state")["usage"]["windows"])  # no file yet
            usage_json(home / ".claude.json", now=self.clock(), percent=63)  # looked up again at the next request
            windows = self.ok("/api/state")["usage"]["windows"]
        self.assertEqual([(w["kind"], w["percent"]) for w in windows], [("five_hour", 63), ("seven_day", 7)])

    def test_an_open_lane_is_by_priority_then_newest(self):
        body = self.ok("/api/cards?lane=needs")
        self.assertEqual(body["total"], 3)
        self.assertEqual(self.ids(body), [self.n_p0["id"], self.n_p2_new["id"], self.n_p2_old["id"]])

    def test_history_is_newest_first(self):
        self.assertEqual(self.ids(self.ok("/api/cards?lane=done")),
                         [self.done_p3["id"], self.done_p0["id"], self.kids[2]["id"]])

    def test_a_row_has_the_list_fields_only(self):
        row = self.ok("/api/cards?lane=needs")["cards"][1]
        self.assertEqual(row, {"id": self.n_p2_new["id"], "title": {"en": "needs new"}, "kind": "errand",
                               "lane": "needs", "pri": "P2", "feed": "chat", "parent": None, "origin": "source",
                               "ask_type": "answer",
                               "plain_where": {"en": "where a thing (en)"},
                               "updated_at": self.n_p2_new["updated_at"]})

    def test_a_row_has_plain_where_in_both_languages(self):
        zh = Config(secondary_language="zh")
        card = self.add(cfg=zh, title="two languages", plain=plain("heron", langs=("en", "zh")))
        self.start(cfg=zh)
        rows = {r["id"]: r for r in self.ok("/api/cards")["cards"]}
        self.assertEqual(rows[card["id"]]["plain_where"], {"en": "where heron (en)", "zh": "where heron (zh)"})

    def test_a_row_has_the_title_in_every_language(self):
        zh = Config(secondary_language="zh")
        two = plain("heron", langs=("en", "zh"))
        text = self.add(cfg=zh, title="Same words", plain=two)
        obj = self.add(cfg=zh, title={"en": "Retry payments", "zh": "重试付款"}, plain=two)
        none = self.add(cfg=zh, title=None, plain=two)
        self.start(cfg=zh)
        rows = {r["id"]: r for r in self.ok("/api/cards?q=heron")["cards"]}
        self.assertEqual(rows[text["id"]]["title"], {"en": "Same words", "zh": "Same words"})
        self.assertEqual(rows[obj["id"]]["title"], {"en": "Retry payments", "zh": "重试付款"})
        self.assertIsNone(rows[none["id"]]["title"])
        full = self.ok(f"/api/cards/{obj['id']}")
        self.assertEqual(full["card"]["title"], {"en": "Retry payments", "zh": "重试付款"})  # the stored card
        full = self.ok(f"/api/cards/{text['id']}")
        self.assertEqual(full["card"]["title"], "Same words")

    def test_a_row_title_falls_back_to_the_primary_slot(self):
        en = Config()
        zh = Config(secondary_language="zh")
        card = self.add(cfg=en, title={"en": "English only"}, plain=plain("egret", langs=("en", "zh")))
        self.start(cfg=zh)
        rows = {r["id"]: r for r in self.ok("/api/cards?q=egret")["cards"]}
        self.assertEqual(rows[card["id"]]["title"], {"en": "English only", "zh": "English only"})
        self.assertEqual(self.ok(f"/api/cards/{card['id']}")["card"]["title"], {"en": "English only"})

    def test_row_title_unit(self):
        langs = ("en", "zh")
        self.assertEqual(server.row({"title": "T"}, langs)["title"], {"en": "T", "zh": "T"})
        self.assertEqual(server.row({"title": {"en": "E", "zh": "Z"}}, langs)["title"], {"en": "E", "zh": "Z"})
        self.assertEqual(server.row({"title": {"en": "E"}}, langs)["title"], {"en": "E", "zh": "E"})
        self.assertEqual(server.row({"title": {"zh": "Z"}}, langs)["title"], {"en": "Z", "zh": "Z"})
        for title in (None, 3, {}, {"en": "", "zh": " "}, {"ja": 3}):
            with self.subTest(title=title):
                self.assertIsNone(server.row({"title": title}, langs)["title"])
        self.assertIsNone(server.row({}, langs)["title"])
        for title in ("", "   "):
            with self.subTest(title=title):
                self.assertIsNone(server.row({"title": title}, langs)["title"])
        self.assertEqual(server.row({"title": {"ja": "J"}}, ("ja", "en"))["title"], {"ja": "J", "en": "J"})
        self.assertEqual(server.row({"title": {"de": "D", "ja": "J"}}, ("ja", "en"))["title"], {"ja": "J", "en": "J"})

    def test_titles_falls_back_to_any_non_blank_slot(self):
        # A title with neither the viewer's nor the primary language (say the primary changed) still shows words.
        card = {"title": {"ja": "再試行"}}
        self.assertEqual(server.titles(card, ("en", "zh")), {"en": "再試行", "zh": "再試行"})
        self.assertEqual(server.row(card, ("en", "zh"))["title"], {"en": "再試行", "zh": "再試行"})
        self.assertEqual(server.titles({"title": {"ko": "b", "ja": "a"}}, ("en",)), {"en": "a"})
        self.assertEqual(server.titles({"title": {"ko": "a", "ja": "b"}}, ("en",)), {"en": "b"})
        self.assertEqual(server.titles({"title": {"en": "Retry", "ja": "x"}}, ("en", "zh")),
                         {"en": "Retry", "zh": "Retry"})
        self.assertEqual(server.titles({"title": {"en": "Retry", "de": "x"}}, ("en", "zh")),
                         {"en": "Retry", "zh": "Retry"})
        self.assertIsNone(server.titles({"title": {"ja": " "}}, ("en", "zh")))

    def test_titles_with_only_invisible_slots_is_none(self):
        # cards.blank: a slot of only a byte order mark (or U+001C-U+001F, U+0085) has no words to show.
        self.assertIsNone(server.titles({"title": {"en": "\ufeff"}}, ("en",)))
        self.assertIsNone(server.titles({"title": {"en": "\x1c", "zh": "\ufeff "}}, ("en", "zh")))
        self.assertIsNone(server.row({"title": "\ufeff"}, ("en",))["title"])
        self.assertEqual(server.titles({"title": {"en": "\ufeff", "zh": "中文"}}, ("en", "zh")),
                         {"en": "中文", "zh": "中文"})

    def test_state_projects_have_the_title_in_every_language(self):
        zh = Config(secondary_language="zh")
        two = plain("ibis", langs=("en", "zh"))
        obj = self.add(cfg=zh, kind="project", lane="planned", title={"en": "Checkout", "zh": "结账"}, plain=two)
        none = self.add(cfg=zh, kind="project", lane="planned", title=None, plain=two)
        self.start(cfg=zh)
        projects = {p["id"]: p for p in self.ok("/api/state")["projects"]}
        self.assertEqual(projects[self.proj["id"]]["title"], {"en": "The project", "zh": "The project"})
        self.assertEqual(projects[obj["id"]]["title"], {"en": "Checkout", "zh": "结账"})
        self.assertIsNone(projects[none["id"]]["title"])

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
        self.assertEqual(self.ok("/api/cards?q=thing")["total"], 8)
        self.assertEqual(self.ids(self.ok("/api/cards?q=thing&kind=project")), [self.proj["id"]])
        self.assertEqual(sorted(self.ids(self.ok(f"/api/cards?q=thing&parent={self.proj['id']}"))),
                         sorted(k["id"] for k in self.kids))
        self.assertEqual(self.ok("/api/cards?q=nothing+like+this")["total"], 0)
        self.assertEqual(self.ok("/api/cards?q=%20%20")["total"], 10)  # blank text is no filter

    def test_an_oversized_search_is_400_and_leaves_the_data_folder_alone(self):
        self.assertEqual(self.ok("/api/cards?q=pelican")["total"], 2)  # the index is built
        self.assertTrue((self.data / "index.sqlite").exists())
        for name, q in (("51 words", "+".join(["a"] * 51)), ("51 tab-separated words", "%09".join(["a"] * 51)),
                        ("501 characters", "a" * 501)):
            ran = AssertionError("the search ran")  # the cap is checked before any search
            with self.subTest(name), mock.patch("assistant.store.Store.search", side_effect=ran):
                before = tree(self.data)
                status, body = self.get(f"/api/cards?q={q}")
                self.assertEqual(status, 400, body)
                self.assertIn("error", body)
                self.assertEqual(tree(self.data), before)
        for name, q in (("50 words", "+".join(["a"] * 50)), ("500 characters", "a" * 500)):
            with self.subTest(name):
                self.assertEqual(self.get(f"/api/cards?q={q}")[0], 200)
        self.assertEqual((server.SEARCH_MAX_CHARS, server.SEARCH_MAX_WORDS), (500, 50))

    def test_limit(self):
        body = self.ok("/api/cards?limit=2")
        self.assertEqual(body["total"], 10)
        self.assertEqual(len(body["cards"]), 2)
        self.assertEqual(len(self.ok("/api/cards?limit=5000")["cards"]), 10)
        self.assertEqual(server.LIST_LIMIT, 500)
        self.assertEqual(server.LIST_MAX, 5000)
        with mock.patch.object(server, "LIST_LIMIT", 3):
            body = self.ok("/api/cards")
        self.assertEqual(len(body["cards"]), 3)
        self.assertEqual(body["total"], 10)

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
        self.assertEqual(body["children"][0]["title"], {"en": "child"})
        self.assertEqual(body["links"], [{**body["links"][0]}, {"id": gone, "missing": True}])
        self.assertEqual(body["links"][0]["id"], other["id"])
        self.assertEqual(body["links"][0]["title"], {"en": "other"})
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
        self.assertEqual(self.ok("/api/sources/gh-mine")["cards"], made)
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
        (self.data / "attachments" / "sub").mkdir()
        (self.data / "attachments" / "sub" / "b.bin").write_bytes(b"z" * 2000)
        outside = self.tmp / "big.bin"
        outside.write_bytes(b"y" * 50000)
        (self.data / "attachments" / "link.bin").symlink_to(outside)
        far = self.tmp / "far"
        far.mkdir()
        (far / "c.bin").write_bytes(b"w" * 70000)
        (self.data / "attachments" / "dirlink").symlink_to(far, target_is_directory=True)
        (self.data / "chat").rmdir()
        (self.data / "chat").symlink_to(self.tmp / "plugin")
        body = self.ok("/api/settings")
        self.assertEqual(body["config"], self.cfg.as_dict())
        self.assertEqual(body["data_dir"], str(self.data))
        self.assertEqual(body["url"], self.srv.url)
        self.assertEqual(set(body["disk"]), set(datadir.FOLDERS) | {"index.sqlite"})
        att = self.data / "attachments"
        expect = (os.lstat(att).st_size + 1000 + os.lstat(att / "link.bin").st_size
                  + os.lstat(att / "sub").st_size + 2000 + os.lstat(att / "dirlink").st_size)
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
        status, _, body = self.ask(card["id"], "\u95ee" * 2000)
        self.assertEqual(status, 201, body)
        status, _, _ = self.call("POST", f"/api/cards/{card['id']}/questions", json.dumps({"text": "ok"}).encode(),
                                 {"Content-Type": "application/json; charset=utf-8"})
        self.assertEqual(status, 201)
        self.assertEqual(len(self.store().get(card["id"])["questions"]), 5)

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
        broken = self.add()
        broken_file = self.store().path(broken["id"])
        broken_file.write_text("{", encoding="utf-8")
        self.start()
        path = f"/api/cards/{card['id']}/questions"
        cases = [
            ("missing text", path, b"{}", {}, 400),
            ("blank text", path, json.dumps({"text": " \n\t "}).encode(), {}, 400),
            ("text not text", path, json.dumps({"text": 5}).encode(), {}, 400),
            ("too long", path, json.dumps({"text": "x" * 2001}).encode(), {}, 400),
            ("too long in characters", path, json.dumps({"text": "\u95ee" * 2001}).encode(), {}, 400),
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
            ("a damaged card", f"/api/cards/{broken['id']}/questions", b'{"text": "hi"}', {}, 500),
            ("no such card", "/api/cards/c-20250101-000000-abcdef/questions", b'{"text": "hi"}', {}, 404),
            ("bad id", "/api/cards/nope/questions", b'{"text": "hi"}', {}, 404),
        ]
        for name, route, body, headers, want in cases:
            with self.subTest(name):
                before = tree(self.data)
                status, _, reply = self.call("POST", route, body, headers)
                self.assertEqual(status, want, reply)
                self.assertIn("error", reply)
                if want == 500:
                    self.assertIn(str(broken_file), reply["error"])
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
            for method in ("GET", "PUT", "PATCH", "DELETE"):
                for route, body, what in (
                        ("/api/cap/answer", {"card_id": card["id"], "option": "pause"}, "cap answers are"),
                        ("/api/profile/use", {"name": "subscription"}, "profile switches are"),
                        ("/api/profile/choose", {"name": "subscription", "model": "default", "effort": "high"},
                         "profile choices are")):
                    with self.subTest(mode=mode, method=method, route=route):
                        before = tree(self.data)
                        status, hdrs, reply = self.call(method, route, body)
                        self.assertEqual(status, 405, reply)
                        self.assertEqual(hdrs.get("allow"), "POST")
                        self.assertIn(f"{what} sent with POST", reply["error"])
                        self.assertEqual(tree(self.data), before)
            status, _, reply = self.call("POST", "/api/state", {})
            self.assertIn("saving a question, answering the cap card and switching the access profile",
                          reply["error"])
            for route in ("/api/profile", "/api/profile/use/x", "/api/profile/remove"):
                with self.subTest(mode=mode, route=route):
                    self.assertEqual(self.call("POST", route, {"name": "subscription"})[0], 405)

    def test_a_question_in_shadow_mode_writes_only_inside_the_data_folder(self):
        home = self.tmp / "home"
        home.mkdir()
        sibling = self.tmp / "sibling"
        sibling.mkdir()
        (sibling / "keep.txt").write_text("keep", encoding="utf-8")
        card = self.add()
        profiles.save(self.data, gateway_profile("g"))
        cap_id = open_cap_card(self, "spent")
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
            status, _, body = self.call("POST", "/api/cap/answer", {"card_id": cap_id, "option": "pause"})
            self.assertEqual(status, 200, body)
            status, _, body = self.call("POST", "/api/profile/use", {"name": "g"})
            self.assertEqual(status, 200, body)
            status, _, body = self.call("POST", "/api/profile/choose",
                                        {"name": "g", "model": "default", "effort": "low"})
            self.assertEqual(status, 200, body)
            for route in ("/api/state", "/api/cards", f"/api/cards/{card['id']}", "/api/sources", "/api/settings"):
                self.ok(route)
            self.assertEqual(tree(home), home_before)
            self.assertEqual(tree(sibling), sibling_before)
            self.assertEqual(tree(self.plugin), plugin_before)
            self.assertEqual(self.outside_data(), outside_before)
            self.assertNotEqual(tree(self.data), data_before)
            self.assertTrue((self.data / cap.PAUSED).exists())
            self.assertEqual(profiles.active_name(self.data), "g")
            self.assertEqual(profiles.load(self.data, "g")["effort"], "low")
        self.assertEqual(outside, [])
        live.assert_not_called()


# --- the cap card's answers -----------------------------------------------------------------------------------

def gateway_profile(name) -> dict:
    """A gateway profile whose key command is never run here (the page reads only the cached key record)."""
    return {"name": name, "mode": "gateway", "label": name.upper(), "model": "big", "small_model": "small",
            "effort": "high", "daily_cap_usd": 500, "chosen_at": "2026-11-01T09:00:00-05:00",
            "gateway": {"base_url": "https://gateway.example.com/v1", "key_command": "exit 7",
                        "refresh_command": None, "rotate_command": None, "models_command": None}}


def open_cap_card(case, cause="spent") -> str:
    """Open today's cap card (the server's local day) as the cap does at the limit; its id."""
    now = case.clock().astimezone()
    t = {"today": 510.0, "week": 510.0, "month": 510.0, "unknown": cause == "unknown",
         "unknown_today": cause == "unknown", "unknown_models": [], "unknown_launches": 0}
    cap.open_card(case.data, now, 500.0, cause, t, "g", case.store())
    return cap.state(case.data, now)["card_id"]


class CapAnswerRouteTest(ServerCase):
    def setUp(self):
        # A zone whose day differs from UTC's at START, so a cap day read in UTC is the wrong day. Cleanups run
        # last-first: the second tzset runs after the zone is put back.
        self.addCleanup(time.tzset)
        tz = mock.patch.dict(os.environ, {"TZ": "Pacific/Kiritimati"})
        tz.start()
        self.addCleanup(tz.stop)
        time.tzset()
        super().setUp()
        profiles.save(self.data, gateway_profile("g"))
        profiles.use(self.data, "g")
        self.card_id = open_cap_card(self)

    def answer(self, option, card_id=None, headers=None):
        return self.call("POST", "/api/cap/answer", {"card_id": card_id or self.card_id, "option": option}, headers)

    def test_raise50_answers_todays_open_card(self):
        st = self.ok("/api/state")
        self.assertEqual((st["access"]["cap"]["card"], st["access"]["cap"]["card_id"]), ("open", self.card_id))
        status, _, body = self.answer("raise50")
        self.assertEqual(status, 200, body)
        self.assertEqual((body["answered"], body["raise_pct"], body["card"], body["card_id"]),
                         ("raise50", 50, "answered", self.card_id))
        now = self.clock().astimezone()
        self.assertEqual(cap.state(self.data, now)["card"], "answered")
        self.assertEqual(cap.today_limit(self.data, profiles.active(self.data), now), 750.0)
        card = self.store().get(self.card_id)
        self.assertIsNone(card.get("ask"))
        self.assertEqual(card["lane"], "done")
        self.assertEqual(card["audit"][-1]["did"], "daily cap answered: raise50")
        st = self.ok("/api/state")
        self.assertEqual((st["access"]["cap"]["card"], st["access"]["cap"]["limit"]), ("answered", 750.0))

    def test_a_stale_card_id_is_409_and_changes_nothing(self):
        other = self.add()["id"]
        for card_id in (other, "c-20250101-000000-abcdef", "nope"):
            with self.subTest(card_id):
                before = tree(self.data)
                status, _, body = self.answer("raise50", card_id=card_id)
                self.assertEqual(status, 409, body)
                self.assertIn("this cap card is no longer open", body["error"])
                self.assertEqual(tree(self.data), before)
        self.clock.tick(86400)  # tomorrow: yesterday's card is not today's
        before = tree(self.data)
        status, _, body = self.answer("raise50")
        self.assertEqual(status, 409, body)
        self.assertEqual(tree(self.data), before)

    def test_an_option_not_on_the_card_is_409(self):
        before = tree(self.data)
        status, _, body = self.answer("accept_unknown")  # a `spent` card does not offer it
        self.assertEqual(status, 409, body)
        self.assertIn("accept_unknown is not an answer on this cap card", body["error"])
        self.assertEqual(tree(self.data), before)
        for option in ("raise7", None, 5, ""):
            with self.subTest(option):
                status, _, body = self.answer(option)
                self.assertEqual(status, 400, body)
                self.assertEqual(tree(self.data), before)
        status, _, body = self.call("POST", "/api/cap/answer", {"option": "raise50"})
        self.assertEqual((status, body["error"]), (400, "card_id missing"))
        self.assertEqual(tree(self.data), before)

    def test_an_answered_card_is_409(self):
        self.assertEqual(self.answer("raise100")[0], 200)
        before = tree(self.data)
        status, _, body = self.answer("raise50")
        self.assertEqual(status, 409, body)
        self.assertEqual(tree(self.data), before)
        self.assertEqual(cap.state(self.data, self.clock().astimezone())["raise_pct"], 100)

    def test_pause_in_shadow_mode_stops_nothing(self):
        with mock.patch("assistant.workers.Workers", side_effect=AssertionError("Workers built")) as built:
            status, _, body = self.answer("pause")
        self.assertEqual(status, 200, body)
        built.assert_not_called()
        self.assertTrue(body["paused"])
        self.assertIn("shadow mode", body["note"])
        self.assertTrue((self.data / cap.PAUSED).exists())
        self.assertTrue(self.ok("/api/state")["access"]["cap"]["paused"])

    def test_pause_in_live_mode_calls_cap_stop(self):
        self.start(cfg=Config(mode="live"))
        with mock.patch("assistant.workers.Workers.cap_stop", return_value={"stopped": ["w1"]}) as stop:
            status, _, body = self.answer("pause")
        self.assertEqual(status, 200, body)
        stop.assert_called_once_with()
        self.assertEqual(body["stopped"], ["w1"])
        self.assertNotIn("note", body)
        self.assertTrue((self.data / cap.PAUSED).exists())

    def test_a_damaged_worker_list_in_live_mode_answers_and_says_the_stop_failed(self):
        # cap_stop runs after paused.json and the answer are written: the reply is 200 with `stop_error`, never a 500
        # that the page would read as "not answered".
        self.start(cfg=Config(mode="live"))
        err = WorkerError("<data>/workers.json: cannot read the worker list")
        with mock.patch("assistant.workers.Workers.cap_stop", side_effect=err):
            status, _, body = self.answer("pause")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["answered"], "pause")
        self.assertIs(body["paused"], True)
        self.assertEqual(body["stop_error"], "<data>/workers.json: cannot read the worker list")
        self.assertEqual((body["stopped"], body["failed"]), ([], []))
        self.assertTrue((self.data / cap.PAUSED).exists())
        self.assertEqual(cap.state(self.data, self.clock().astimezone())["card"], "answered")

    def test_a_worker_setup_error_in_live_mode_is_a_500_with_nothing_written(self):
        self.start(cfg=Config(mode="live"))
        before = tree(self.data)
        err = WorkerError("<data>/workers.json: cannot open the worker list")
        with mock.patch("assistant.workers.Workers.__init__", side_effect=err):
            status, _, body = self.answer("pause")
        self.assertEqual(status, 500, body)
        self.assertIn("<data>/workers.json: cannot open the worker list", body["error"])
        self.assertEqual(tree(self.data), before)
        self.assertFalse((self.data / cap.PAUSED).exists())
        self.assertEqual(cap.state(self.data, self.clock().astimezone())["card"], "open")

    def test_two_answers_at_once_raise_once(self):
        self.start()
        statuses, ready = [], threading.Barrier(2)
        real = cap.answer

        def slow(*args, **kwargs):  # hold the first answer open, so a second one without the lock overlaps it
            time.sleep(0.3)
            return real(*args, **kwargs)

        def go():
            ready.wait()
            statuses.append(self.answer("raise50")[0])

        with mock.patch.object(cap, "answer", side_effect=slow):
            threads = [threading.Thread(target=go) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        self.assertEqual(sorted(statuses), [200, 409])
        self.assertEqual(cap.state(self.data, self.clock().astimezone())["raise_pct"], 50)

    def test_another_origin_or_a_form_post_is_refused(self):
        self.start()
        before = tree(self.data)
        body = json.dumps({"card_id": self.card_id, "option": "raise50"}).encode()
        for headers, want in (({"Origin": "https://cards.example.com"}, 403), ({"Origin": "null"}, 403),
                              ({"Content-Type": "application/x-www-form-urlencoded"}, 415),
                              ({"Content-Type": "text/plain"}, 415)):
            with self.subTest(headers):
                status, _, reply = self.call("POST", "/api/cap/answer", body, headers)
                self.assertEqual(status, want, reply)
                self.assertEqual(tree(self.data), before)
        status, _, reply = self.call("POST", "/api/cap/answer", b"x" * (64 * 1024 + 1))
        self.assertEqual(status, 413)
        own = {"Origin": f"http://localhost:{self.port}"}
        self.assertEqual(self.answer("raise50", headers=own)[0], 200)


# --- the profile switch and its model and effort choice -------------------------------------------------------

LISTED = '["big", "small", "claude-opus-5-5"]'


class ProfileRouteCase(ServerCase):
    """A gateway profile whose models command counts its runs in a temp file (outside the data folder) and prints
    the list held in `self.listing`, so a test can change the list between two requests."""

    def setUp(self):
        super().setUp()
        self.listing = self.tmp / "models.json"
        self.listing.write_text(LISTED, encoding="utf-8")
        self.counter = self.tmp / "models-runs"

    def listed(self, name, chosen=True) -> dict:
        p = gateway_profile(name)
        p["gateway"]["models_command"] = (f"echo run >> {shlex.quote(str(self.counter))}; "
                                          f"cat {shlex.quote(str(self.listing))}")
        if not chosen:
            p.update(model=None, effort=None, chosen_at=None)
        out = profiles.save(self.data, p)
        self.counter.unlink(missing_ok=True)  # the save checks its models once; the routes' runs count from here
        return out

    def runs(self) -> int:
        return len(self.counter.read_text().splitlines()) if self.counter.exists() else 0

    def active_bytes(self):
        f = profiles.active_file(self.data)
        return f.read_bytes() if f.exists() else None

    def use(self, name, headers=None):
        return self.call("POST", "/api/profile/use", {"name": name}, headers)

    def choose(self, name, model, effort, headers=None, **more):
        return self.call("POST", "/api/profile/choose", {"name": name, "model": model, "effort": effort, **more},
                         headers)


class ProfileUseRouteTest(ProfileRouteCase):
    def test_a_chosen_profile_switches_and_says_what_it_was_with_no_models_command(self):
        self.listed("g")
        status, _, body = self.use("g")
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"active": "g", "was": "subscription"})
        self.assertEqual(profiles.active_name(self.data), "g")
        st = self.ok("/api/state")
        self.assertEqual(st["access"]["profile"]["name"], "g")
        self.assertEqual(self.use("subscription")[2], {"active": "subscription", "was": "g"})
        self.assertEqual(self.use("subscription")[2], {"active": "subscription", "was": "subscription"})
        self.assertEqual(self.runs(), 0)

    def test_the_active_profile_switches_again_without_a_choice_even_when_unchosen(self):
        self.listed("h", chosen=False)
        profiles.use(self.data, "h")
        status, _, body = self.use("h")
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"active": "h", "was": "h"})
        self.assertEqual(self.runs(), 0)

    def test_a_bad_name_is_400_and_writes_nothing(self):
        self.listed("g")
        profiles.use(self.data, "g")
        before, active = tree(self.data), self.active_bytes()
        for name in ("Bad Name", "../x", "", None, 3, ["g"], "x" * 41, "g\n"):
            with self.subTest(name=name):
                status, _, body = self.use(name)
                self.assertEqual(status, 400, body)
                self.assertIn("name must be a profile name", body["error"])
                self.assertEqual(tree(self.data), before)
                self.assertEqual(self.active_bytes(), active)

    def test_an_unknown_profile_is_404_and_writes_nothing(self):
        before, active = tree(self.data), self.active_bytes()
        status, _, body = self.use("nobody")
        self.assertEqual(status, 404, body)
        self.assertIn("no profile named 'nobody'", body["error"])
        self.assertEqual(tree(self.data), before)
        self.assertEqual(self.active_bytes(), active)

    def test_a_profile_that_does_not_load_is_409_with_its_error(self):
        self.listed("g")
        profiles.use(self.data, "g")
        profiles.path(self.data, "bad").write_text("{nope", encoding="utf-8")
        before, active = tree(self.data), self.active_bytes()
        status, _, body = self.use("bad")
        self.assertEqual(status, 409, body)
        self.assertIn("bad.json", body["error"])
        self.assertEqual(tree(self.data), before)
        self.assertEqual(self.active_bytes(), active)
        self.assertEqual(self.runs(), 0)

    def test_an_unchosen_profile_replies_with_its_choices_fetched_once(self):
        self.listed("g")
        profiles.use(self.data, "g")
        h = self.listed("h", chosen=False)
        before, active = tree(self.data), self.active_bytes()
        status, _, body = self.use("h")
        self.assertEqual(status, 200, body)
        self.assertEqual(self.runs(), 1)
        self.assertEqual(body, {"needs_choice": True,
                                "choices": profiles.choices(self.data, h, profiles.fetch_models(self.data, h)[0])})
        self.assertEqual(body["choices"]["models"], ["big", "small", "claude-opus-5-5[1m]", "claude-opus-5-5"])
        self.assertEqual(body["choices"]["recommended_model"], "claude-opus-5-5[1m]")
        self.assertEqual(body["choices"]["recommended_effort"], profiles.RECOMMENDED_EFFORT)
        self.assertEqual(tree(self.data), before)
        self.assertEqual(self.active_bytes(), active)
        self.assertEqual(profiles.active_name(self.data), "g")

    def test_a_models_command_that_fails_is_502_and_writes_nothing(self):
        self.listed("h", chosen=False)
        self.listing.unlink()  # `cat` fails: exit 1
        before, active = tree(self.data), self.active_bytes()
        status, _, body = self.use("h")
        self.assertEqual(status, 502, body)
        self.assertEqual(body["error"], "gateway.models_command: failed (exit 1)")
        self.assertEqual(self.runs(), 1)
        self.assertEqual(tree(self.data), before)
        self.assertEqual(self.active_bytes(), active)

    def test_the_switch_works_in_shadow_mode_and_refuses_another_origin(self):
        self.listed("g")
        with mock.patch("assistant.config.require_live", side_effect=AssertionError("require_live")) as live:
            self.start(cfg=Config(mode="shadow"))
            before = tree(self.data)
            for headers, want in (({"Origin": "https://cards.example.com"}, 403),
                                  ({"Content-Type": "text/plain"}, 415)):
                with self.subTest(headers):
                    self.assertEqual(self.use("g", headers=headers)[0], want)
                    self.assertEqual(tree(self.data), before)
            own = {"Origin": f"http://localhost:{self.port}"}
            self.assertEqual(self.use("g", headers=own)[0], 200)
        live.assert_not_called()
        self.assertEqual(profiles.active_name(self.data), "g")


class ProfileChooseRouteTest(ProfileRouteCase):
    def setUp(self):
        super().setUp()
        self.listed("g")
        profiles.use(self.data, "g")
        self.listed("h", chosen=False)

    def unchanged(self):
        return tree(self.data), self.active_bytes(), profiles.path(self.data, "h").read_bytes()

    def test_a_choice_from_the_fresh_list_is_saved_then_switched(self):
        status, _, body = self.choose("h", "claude-opus-5-5[1m]", "xhigh")
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"chosen": {"name": "h", "model": "claude-opus-5-5[1m]", "effort": "xhigh"},
                                "active": "h", "was": "g"})
        self.assertEqual(self.runs(), 1)
        saved = profiles.load(self.data, "h")
        self.assertEqual((saved["model"], saved["effort"]), ("claude-opus-5-5[1m]", "xhigh"))
        self.assertTrue(saved["chosen_at"])
        self.assertFalse(profiles.needs_choice(self.data, saved))
        self.assertEqual(profiles.active_name(self.data), "h")

    def test_a_model_gone_from_the_list_since_the_box_opened_is_refused(self):
        status, _, body = self.use("h")
        self.assertIn("big", body["choices"]["models"])
        self.listing.write_text('["small", "claude-opus-5-5"]', encoding="utf-8")
        before = self.unchanged()
        status, _, body = self.choose("h", "big", "high")
        self.assertEqual(status, 409, body)
        self.assertTrue(body["error"].startswith("model 'big' is not offered; choose one of: "), body)
        self.assertEqual(self.runs(), 2)  # one fetch for each request
        self.assertEqual(self.unchanged(), before)
        self.assertEqual(profiles.active_name(self.data), "g")

    def test_a_list_the_page_sends_is_never_used(self):
        before = self.unchanged()
        status, _, body = self.choose("h", "evil", "high", models=["evil"], known=["evil"])
        self.assertEqual(status, 409, body)
        self.assertIn("model 'evil' is not offered", body["error"])
        self.assertEqual(self.unchanged(), before)

    def test_an_effort_not_offered_is_409_and_saves_nothing(self):
        before = self.unchanged()
        status, _, body = self.choose("h", "big", "turbo")
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"], f"effort 'turbo' is not offered; choose one of: "
                                        f"{', '.join(profiles.EFFORTS)}")
        self.assertEqual(self.runs(), 1)
        self.assertEqual(self.unchanged(), before)

    def test_a_models_command_that_fails_is_502_and_saves_nothing(self):
        self.listing.unlink()
        before = self.unchanged()
        status, _, body = self.choose("h", "big", "high")
        self.assertEqual(status, 502, body)
        self.assertEqual(body["error"], "gateway.models_command: failed (exit 1)")
        self.assertEqual(self.unchanged(), before)

    def test_a_bad_body_unknown_or_broken_profile_is_refused_before_any_fetch(self):
        profiles.path(self.data, "bad").write_text("{nope", encoding="utf-8")
        before = self.unchanged()
        for (name, model, effort), want in ((("Bad Name", "big", "high"), 400), ((None, "big", "high"), 400),
                                            (("h", None, "high"), 400), (("h", "big", 3), 400),
                                            (("h", ["big"], "high"), 400), (("nobody", "big", "high"), 404),
                                            (("bad", "big", "high"), 409)):
            with self.subTest(name=name, model=model, effort=effort):
                status, _, body = self.choose(name, model, effort)
                self.assertEqual(status, want, body)
                self.assertEqual(self.unchanged(), before)
        self.assertEqual(self.runs(), 0)

    def test_choose_works_in_shadow_mode_and_refuses_another_origin(self):
        with mock.patch("assistant.config.require_live", side_effect=AssertionError("require_live")) as live:
            self.start(cfg=Config(mode="shadow"))
            before = self.unchanged()
            status, _, _ = self.choose("h", "big", "high", headers={"Origin": "https://cards.example.com"})
            self.assertEqual(status, 403)
            self.assertEqual(self.unchanged(), before)
            self.assertEqual(self.choose("h", "big", "high")[0], 200)
        live.assert_not_called()
        self.assertEqual(profiles.active_name(self.data), "h")

    def test_default_is_stored_as_no_model_and_echoed_back_as_default(self):
        # A profile with no models command offers `default` (Claude Code's own model); choosing it saves a null
        # model, while the reply names what the user chose.
        self.assertIn(profiles.DEFAULT_MODEL, profiles.model_choices(self.data, profiles.load(self.data, "subscription")))
        status, _, body = self.choose("subscription", profiles.DEFAULT_MODEL, "low")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["chosen"], {"name": "subscription", "model": "default", "effort": "low"})
        self.assertEqual(body["active"], "subscription")
        saved = profiles.load(self.data, "subscription")
        self.assertIsNone(saved["model"])
        self.assertEqual(saved["effort"], "low")
        self.assertEqual(self.runs(), 0)  # the subscription has no models command to run

    def test_a_switch_and_a_choice_wait_for_each_other(self):
        self.start()
        order, ready = [], threading.Barrier(2)
        real = profiles.fetch_models

        def slow(*args, **kwargs):  # hold the first request inside the lock, so a second one without it overlaps
            order.append("in")
            time.sleep(0.3)
            out = real(*args, **kwargs)
            order.append("out")
            return out

        def go(fn):
            ready.wait()
            fn()

        with mock.patch.object(profiles, "fetch_models", side_effect=slow):
            threads = [threading.Thread(target=go, args=(fn,)) for fn in
                       (lambda: self.choose("h", "big", "high"), lambda: self.use("h"))]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        self.assertEqual(order[:2], ["in", "out"])


# --- criterion 8: the page -------------------------------------------------------------------------------------

PAGE = ROOT / "lib" / "assistant" / "builtin" / "page" / "local.html"


def page_functions(*names) -> str:
    """The source of each named top-level function of the page's script, for running under node."""
    text = PAGE.read_text(encoding="utf-8")
    out = []
    for name in names:
        head = f"  function {name}("
        if head not in text:
            head = f"  async function {name}("
        src = text[text.index(head):]
        out.append(src[:src.index("\n  }\n") + 5])
    return "".join(out)


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

    def test_a_source_that_extends_another_shows_its_origin_in_the_pages_language(self):
        text = PAGE.read_text(encoding="utf-8")
        labels = json.loads(re.search(r'<script type="application/json" id="labels">(.*?)</script>', text,
                                      re.S).group(1))
        for lang in ("en", "zh"):
            with self.subTest(lang):
                self.assertIn("{name}", labels[lang]["origin.extends"])
        self.assertIn("/^user, extends (.+)$/", text)
        self.assertRegex(text, r"""L\('origin\.extends', \{name: """)

    def test_the_pages_error_lines_stay_in_its_language(self):
        text = PAGE.read_text(encoding="utf-8")
        labels = json.loads(re.search(r'<script type="application/json" id="labels">(.*?)</script>', text,
                                      re.S).group(1))
        for lang in ("en", "zh"):
            for key in ("error.offline", "q.refused"):
                with self.subTest(lang=lang, key=key):
                    self.assertIn(key, labels[lang])
        # A fetch that never reaches the server is reported with a label, not the browser's English words.
        self.assertRegex(text, r"""\} catch \(e\) \{[^}]*new Error\(L\('error\.offline'\)\)""")
        # A refused question shows a label, with the store's reason as a separate diagnostic line.
        self.assertIn("if (e.status === 409) status.replaceChildren(h('p', {text: L('q.refused')}), "
                      "h('p', {class: 'mono', text: e.message}))", text)

    def test_the_page_picks_a_title_in_its_language_at_each_place(self):
        text = PAGE.read_text(encoding="utf-8")
        self.assertIn("function titleIn(t, fallback)", text)
        for use in ("h('div', {class: 'row-title', text: titleIn(r.title)})",  # a list row
                    "det.append(h('h1', {text: titleIn(c.title)}));",  # the card header, from the stored card
                    "navLink(route('project', p.id), titleIn(p.title, p.id),",  # the project rail
                    "L('list.project', {title: titleIn(p && p.title, l.arg)})"):  # the project list's header
            with self.subTest(use):
                self.assertIn(use, text)
        for old in ("r.title ||", "c.title ||", "p.title ||", "p && p.title) ||"):
            with self.subTest(old):
                self.assertNotIn(old, text)

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_title_in_shows_one_language_only(self):
        js = page_functions("blankText", "titleIn") + """
const S = {lang: 'zh', state: {primary_language: 'en', languages: ['en', 'zh']}};
const L = key => '[' + key + ']';
const out = [
  titleIn({en: 'Retry payments', zh: '重试付款'}),
  titleIn({en: 'English only'}),
  titleIn({en: 'English only', zh: '  '}),
  titleIn('One text from an older reply'),
  titleIn(null), titleIn(undefined), titleIn(''), titleIn('   '), titleIn({}), titleIn({ja: 'x'}), titleIn(3),
  titleIn(null, 'c-id'), titleIn({en: 'E'}, 'c-id'),
  titleIn({en: '', zh: ' '}), titleIn({ko: 'b', ja: 'a'}), titleIn({ko: 'a', ja: 'b'}),
  titleIn({en: 'Retry', ja: 'x'}), titleIn({ja: 3}, 'c-id'),
  titleIn({en: 'Retry', de: 'x'}), titleIn(['', 'x']),
];
S.lang = 'en';
out.push(titleIn({en: 'Retry payments', zh: '重试付款'}), titleIn({zh: '只有中文'}));
// One blank rule with cards.blank: Python's strip() white space and U+FEFF are blank, U+200B is not.
out.push(titleIn({en: '\\x1c', zh: '中文'}), titleIn({en: '\\x85'}), titleIn({en: '\\ufeff', zh: '中文'}),
         titleIn('\\x1f'), titleIn({en: '\\ufeff \\x1d', zh: '\\x1e', ja: '\\u200b'}));
S.state = {primary_language: 'ja', languages: ['ja', 'en']};
out.push(titleIn({ja: 'J'}), titleIn({de: 'D', ja: 'J'}));
console.log(JSON.stringify(out));
"""
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [
            "重试付款", "English only", "English only", "One text from an older reply",
            "[card.untitled]", "[card.untitled]", "[card.untitled]", "[card.untitled]", "[card.untitled]",
            "x", "[card.untitled]", "c-id", "E", "[card.untitled]", "a", "b",
            "Retry", "c-id", "Retry", "[card.untitled]",
            "Retry payments", "只有中文",
            "中文", "[card.untitled]", "中文", "[card.untitled]", "\u200b",
            "J", "J"])

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_the_page_and_python_agree_on_a_blank_title_slot(self):
        # The same slot texts through cards.blank and the page's blankText give the same answers: criterion strings,
        # then every single character of the Basic Multilingual Plane (what either side calls white space included).
        texts = [*BLANK_SLOTS, *NOT_BLANK_SLOTS, *(chr(n) for n in range(0x10000)), "\U0001f600"]
        js = page_functions("blankText") + """
let input = '';
process.stdin.on('data', d => { input += d; });
process.stdin.on('end', () => { console.log(JSON.stringify(JSON.parse(input).map(blankText))); });
"""
        out = subprocess.run(["node", "-e", js], input=json.dumps(texts), capture_output=True, text=True,
                             encoding="utf-8", timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        page = json.loads(out.stdout)
        python = [cards.blank(t) for t in texts]
        self.assertEqual(len(page), len(python))
        self.assertEqual([t for t, a, b in zip(texts, page, python) if a != b], [])
        self.assertEqual(page[:len(BLANK_SLOTS) + len(NOT_BLANK_SLOTS)],
                         [True] * len(BLANK_SLOTS) + [False] * len(NOT_BLANK_SLOTS))

    def run_access(self, script: str):
        """Run the page's access functions under node with a label stub (`[key {vars}]`) and return the JSON the
        script prints."""
        js = page_functions("money", "badge", "windowName", "topWindow", "usageShort", "accessAlerts", "capOptions",
                            "usageRows", "switchButtons", "switchError", "pick") + """
const S = {lang: 'en'};
const L = (key, vars) => '[' + key + (vars ? ' ' + JSON.stringify(vars) : '') + ']';
const text = v => (v === null || v === undefined ? '' : String(v));
const route = (...parts) => '#/' + parts.map(p => encodeURIComponent(p)).join('/');
const ago = s => 'ago:' + Math.round(s);
const parseStamp = s => Date.parse(s);
const when = s => 'when:' + s;
""" + script
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_badge_text_by_mode(self):
        out = self.run_access("""
console.log(JSON.stringify([
  badge({profile: {name: 'subscription', mode: 'subscription', label: 'Subscription'}}),
  badge({profile: {name: 'g', mode: 'gateway', label: 'Team Proj', model: 'claude-opus-5-5[1m]', effort: 'high'}}),
  badge({profile: {name: 'g', mode: 'gateway', label: null, model: null, effort: 'high'}}),
  badge({profile: null, profile_error: 'broken'}), badge(null)]));
""")
        self.assertEqual(out[0], {"cls": "abadge", "text": "[access.subscription]", "detail": None, "pages": None})
        self.assertEqual(out[1], {"cls": "abadge gw", "text": '[access.workers {"label":"Team Proj"}]',
                                  "detail": "claude-opus-5-5[1m] · high", "pages": "[access.page_sessions]"})
        self.assertEqual((out[2]["text"], out[2]["detail"]), ('[access.workers {"label":"g"}]', "high"))
        self.assertEqual(out[3:], [None, None])
        labels = self.labels()
        self.assertEqual(labels["en"]["access.workers"], "Workers: Gateway · {label}")
        self.assertEqual(labels["en"]["access.page_sessions"], "Page sessions: Subscription")
        self.assertEqual(labels["en"]["access.subscription"], "Subscription")
        text = PAGE.read_text(encoding="utf-8")
        self.assertIn('<span id="access" class="access"></span>', text)
        self.assertIn("parts.push(h('a', {class: b.cls, href: '#/settings', text: b.text}));", text)
        # The top bar draws the badge on each state, and the settings screen opens with the access set.
        self.assertIn("    drawAccess();\n    const sw = $('lang-switch');", text)
        self.assertIn("body.replaceChildren(h('div', {class: 'sets'}, accessSet(),", text)

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_key_and_profile_errors_show_a_label_and_the_server_text(self):
        out = self.run_access("""
console.log(JSON.stringify([
  accessAlerts({profile: null, profile_error: 'the active profile cannot be loaded; run X', cap: {}}),
  accessAlerts({profile: {mode: 'gateway'}, key_error: 'the gateway key does not work: exit 1. Run it.',
                cap: {card: 'open', card_id: 'c-20261111-150000-abcdef', paused: true, pause: {reason: 'cap'}}}),
  accessAlerts({profile: {mode: 'subscription'}, cap: {card: 'answered', card_id: 'c-1', paused: false}})]));
""")
        self.assertEqual(out[0], [{"cls": "keyerr", "label": "[access.profile_error]",
                                   "detail": "the active profile cannot be loaded; run X", "href": "#/settings"}])
        self.assertEqual(out[1], [
            {"cls": "keyerr", "label": "[access.key_error]", "detail": "the gateway key does not work: exit 1. Run it.",
             "href": None},
            {"cls": "capnote", "label": "[cap.reached]", "detail": None, "href": "#/card/c-20261111-150000-abcdef"},
            {"cls": "keyerr", "label": "[cap.paused]", "detail": None, "href": None}])
        self.assertEqual(out[2], [])
        labels = self.labels()
        self.assertEqual(labels["en"]["access.key_error"], "Gateway key not working; new workers wait")
        self.assertEqual(labels["en"]["access.profile_error"], "Profile not working; new workers wait")
        self.assertEqual(labels["en"]["cap.reached"], "Daily cap reached: answer the card")
        self.assertEqual(labels["en"]["cap.paused"], "Paused at the daily cap; run `ap2 cap resume`")

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_cap_buttons_only_on_todays_open_cap_card(self):
        out = self.run_access("""
const id = 'c-20261111-150000-abcdef';
const card = (over) => Object.assign({id, kind: 'cap', ask: {type: 'approve', from: 'cap',
  options: ['raise50', 'raise100', 'pause']}}, over || {});
const open = {cap: {card: 'open', card_id: id}};
console.log(JSON.stringify([
  capOptions(card(), open),
  capOptions(card({ask: {from: 'cap', options: ['raise50', 'raise100', 'accept_unknown', 'pause', 'other']}}), open),
  capOptions(card(), {cap: {card: 'answered', card_id: id}}),
  capOptions(card(), {cap: {card: 'open', card_id: 'c-20261110-150000-abcdef'}}),
  capOptions(card(), {cap: {card: null, card_id: null}}),
  capOptions(card({kind: 'errand'}), open),
  capOptions(card({ask: {from: 'user', options: ['raise50']}}), open),
  capOptions(card({ask: null}), open),
  capOptions(card(), null), capOptions(null, open)]));
""")
        self.assertEqual(out[0], ["raise50", "raise100", "pause"])
        self.assertEqual(out[1], ["raise50", "raise100", "accept_unknown", "pause"])
        self.assertEqual(out[2:], [[]] * 8)
        text = PAGE.read_text(encoding="utf-8")
        self.assertIn("class: o === 'pause' ? 'btn quiet' : 'btn'", text)  # pause is the quiet button
        self.assertIn("await post('/api/cap/answer', {card_id: card.id, option});", text)
        self.assertIn("if (e.status === 409) status.replaceChildren(h('p', {text: L('cap.stale')}), "
                      "h('p', {class: 'mono', text: e.message}));", text)
        self.assertIn("    const area = capArea(c);  // the cap card's answer buttons, or the note of its answer\n"
                      "    if (area) det.append(area);\n", text)  # the card view draws capArea
        self.assertEqual(set(cap.ANSWERS), {"raise50", "raise100", "accept_unknown", "pause"})

    # A plain-object `h` (tag, attrs, kids) and `flat`, every text of a tree in order, for running the page's
    # drawing functions under node.
    DOM_STUBS = """
const h = (tag, attrs, ...kids) => ({tag, attrs: attrs || {}, kids,
  replaceChildren(...k) { this.kids = k; }});
const flat = n => (n === null || n === undefined || n === false) ? []
  : Array.isArray(n) ? n.flatMap(flat)
  : typeof n === 'object' ? [...(n.attrs && n.attrs.text != null ? [String(n.attrs.text)] : []), ...flat(n.kids)]
  : [String(n)];
const find = (n, tag) => (n === null || n === undefined || typeof n !== 'object') ? []
  : Array.isArray(n) ? n.flatMap(k => find(k, tag)) : [...(n.tag === tag ? [n] : []), ...find(n.kids, tag)];
"""

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_cap_buttons_post_their_own_answer_and_reload_the_list(self):
        out = self.run_access(page_functions("capStopNote", "capSection") + self.DOM_STUBS + """
const calls = [];
const post = async (path, body) => { calls.push(['post', path, body]); return {}; };
const poll = async () => { calls.push(['poll']); };
const loadList = async () => { calls.push(['loadList']); };
const loadCard = async id => { calls.push(['loadCard', id]); };
S.route = null;
const opts = ['raise50', 'raise100', 'pause'];
(async () => {
  const shown = find(capSection({id: 'C1', kind: 'cap'}, opts), 'button').map(b => [b.attrs.text, b.attrs.class]);
  const clicks = [];
  for (let i = 0; i < opts.length; i++) {
    calls.length = 0;
    const b = find(capSection({id: 'C1', kind: 'cap'}, opts), 'button')[i];
    await b.attrs.onclick();
    clicks.push(calls.slice());
  }
  console.log(JSON.stringify({shown, clicks}));
})();
""")
        self.assertEqual(out["shown"], [["[cap.raise50]", "btn"], ["[cap.raise100]", "btn"],
                                        ["[cap.pause]", "btn quiet"]])
        for opt, calls in zip(["raise50", "raise100", "pause"], out["clicks"]):
            with self.subTest(opt):
                self.assertEqual(calls[0], ["post", "/api/cap/answer", {"card_id": "C1", "option": opt}])
                self.assertIn(["loadList"], calls)
                self.assertEqual(len([c for c in calls if c[0] == "post"]), 1)

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_post_sends_json_the_server_accepts(self):
        # The cap buttons post through the page's own `post`: a POST with a JSON body and the JSON content type,
        # which the server needs (text/plain is 415, a non-JSON body is 400).
        out = self.run_access(page_functions("api", "post") + """
const sent = [];
globalThis.fetch = async (path, opts) => { sent.push([path, opts]); return {ok: true, json: async () => ({ok: 1})}; };
(async () => {
  const reply = await post('/api/cap/answer', {card_id: 'C1', option: 'raise50'});
  const [path, opts] = sent[0];
  console.log(JSON.stringify({reply, path, method: opts.method, type: opts.headers && opts.headers['Content-Type'],
                              body: JSON.parse(opts.body)}));
})();
""")
        self.assertEqual(out, {"reply": {"ok": 1}, "path": "/api/cap/answer", "method": "POST",
                               "type": "application/json", "body": {"card_id": "C1", "option": "raise50"}})

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_a_refused_answer_says_why_and_a_good_one_redraws_the_card_on_screen(self):
        # A 409 or 500 shows a label and the server's reason and turns the buttons back on, so the user can try
        # again; a good answer redraws the card only when that card is the one on screen.
        out = self.run_access(page_functions("capStopNote", "capSection") + self.DOM_STUBS + """
const calls = [];
let post = null;
const poll = async () => {}, loadList = async () => {};
const loadCard = async id => { calls.push(id); };
const fail = status => async () => { throw Object.assign(new Error('REASON'), {status}); };
(async () => {
  const runs = {};
  S.route = null;
  for (const [name, p] of [['stale', fail(409)], ['failed', fail(500)]]) {
    post = p;
    const sec = capSection({id: 'C1', kind: 'cap'}, ['raise50', 'pause']);
    const buttons = find(sec, 'button');
    await buttons[0].attrs.onclick();
    const status = sec.kids[2];
    runs[name] = {hidden: status.hidden, cls: status.className, text: flat(status.kids),
                  disabled: buttons.map(b => b.disabled)};
  }
  post = async () => ({});
  for (const [name, route] of [['here', {name: 'card', arg: 'C1'}], ['other', {name: 'card', arg: 'OTHER'}]]) {
    S.route = route;
    calls.length = 0;
    await find(capSection({id: 'C1', kind: 'cap'}, ['raise50']), 'button')[0].attrs.onclick();
    runs[name] = calls.slice();
  }
  console.log(JSON.stringify(runs));
})();
""")
        self.assertEqual(out["stale"], {"hidden": False, "cls": "warn", "text": ["[cap.stale]", "REASON"],
                                        "disabled": [False, False]})
        self.assertEqual(out["failed"], {"hidden": False, "cls": "warn", "text": ["[cap.failed] REASON"],
                                         "disabled": [False, False]})
        self.assertEqual(out["here"], ["C1"])
        self.assertEqual(out["other"], [])

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_an_answer_whose_workers_did_not_all_stop_says_so_in_a_neutral_line(self):
        # A 200 with workers that did not stop (`failed`), another stop under way (`busy`) or a worker list it could
        # not read (`stop_error`): the status box shows a neutral line (the card is answered), never "Not answered:".
        out = self.run_access(page_functions("capStopNote", "capNoteSection", "capSection") + self.DOM_STUBS + """
const replies = [{answered: 'pause', stopped: [], failed: ['w-1']},
                 {answered: 'pause', stopped: [], failed: [{card: 'c-9', role: 'impl', note: 'n'}]},
                 {answered: 'pause', stopped: [], failed: [], busy: true},
                 {answered: 'pause', paused: true, stopped: [], failed: [],
                  stop_error: '<data>/workers.json: cannot read the worker list'},
                 {answered: 'pause', stopped: ['w-1'], failed: []}, {answered: 'raise50'}];
let post = null;
const poll = async () => {}, loadList = async () => {}, loadCard = async () => {};
S.route = null;
(async () => {
  const seen = [];
  for (const reply of replies) {
    post = async () => reply;
    const sec = capSection({id: 'C1', kind: 'cap'}, ['pause']);
    await find(sec, 'button')[0].attrs.onclick();
    const status = sec.kids[2];
    const redrawn = S.capNote ? flat(capNoteSection(S.capNote.reply)) : null;
    seen.push({hidden: status.hidden !== false, cls: status.className || status.attrs.class,
               text: flat(status.kids), note: S.capNote && S.capNote.id, redrawn});
  }
  console.log(JSON.stringify(seen));
})();
""")
        failed, failed_dict, busy, stop_error, clean, raised = out
        self.assertEqual((failed["hidden"], failed["cls"]), (False, "capinfo"))
        self.assertEqual(failed["text"], ["[cap.not_stopped]", "w-1"])
        self.assertEqual(failed["note"], "C1")
        self.assertEqual(failed["redrawn"], ["[cap.title]", "[cap.not_stopped]", "w-1"])  # kept after the redraw
        self.assertEqual(failed_dict["text"], ["[cap.not_stopped]", "c-9 impl"])
        self.assertEqual((busy["hidden"], busy["cls"], busy["text"]), (False, "capinfo", ["[cap.stop_busy]"]))
        self.assertEqual((stop_error["hidden"], stop_error["cls"]), (False, "capinfo"))
        self.assertEqual(stop_error["text"], ["[cap.stop_error]", "<data>/workers.json: cannot read the worker list"])
        for name, quiet in (("clean", clean), ("raise", raised)):
            with self.subTest(name):
                self.assertEqual((quiet["hidden"], quiet["text"], quiet["note"]), (True, [], None))
        for got in (failed, busy, stop_error):
            self.assertNotIn("[cap.failed]", " ".join(got["text"]))
        text = PAGE.read_text(encoding="utf-8")
        labels = self.labels()
        for key in ("cap.not_stopped", "cap.stop_busy", "cap.stop_error"):
            with self.subTest(key):
                self.assertTrue(labels["en"][key])
                self.assertRegex(labels["zh"][key], r"[\u4e00-\u9fff]")
        self.assertEqual(labels["en"]["cap.not_stopped"],
                         "Answered, but these workers did not stop yet (the next check tries again):")
        self.assertEqual(labels["en"]["cap.stop_error"],
                         "Answered and paused, but the running workers could not be stopped:")

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_a_cap_note_is_dropped_once_the_page_leaves_its_card(self):
        # The note of a pause answer is for the card it was answered on: a lane, Settings or another card in between
        # drops it, so coming back to that card later draws no stale note; a redraw of the same card keeps it.
        out = self.run_access(page_functions("capStopNote", "capNoteSection", "show") + self.DOM_STUBS + """
const LANES = ['needs'];
let next = null;
const parseRoute = () => next;
const drawRail = () => {}, panes = () => {}, loadList = async () => {};
const $ = () => ({});
const drawn = [];
const redraw = () => drawn.push(S.capNote ? flat(capNoteSection(S.capNote.reply)) : []);
const drawCard = c => { if (c) redraw(); };
const loadCard = async () => redraw();  // the card redraw: the note under the card's answer section
const drawSources = async () => {}, drawTemplates = async () => {}, drawSettings = async () => {};
const note = {id: 'X', reply: {answered: 'pause', stopped: [], failed: ['w-1']}};
(async () => {
  const runs = {};
  for (const [name, away] of [['same', null], ['lane', {name: 'lane', arg: 'needs'}],
                              ['settings', {name: 'settings', arg: null}], ['other', {name: 'card', arg: 'Y'}]]) {
    S.capNote = note;
    drawn.length = 0;
    if (away) { next = away; await show(); }
    next = {name: 'card', arg: 'X'};
    await show();
    runs[name] = drawn.slice();
  }
  console.log(JSON.stringify(runs));
})();
""")
        self.assertEqual(out["same"], [["[cap.title]", "[cap.not_stopped]", "w-1"]])
        for name in ("lane", "settings", "other"):
            with self.subTest(name):
                self.assertNotIn("[cap.not_stopped]", json.dumps(out[name]))
                self.assertEqual(out[name][-1], [])

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_the_card_view_shows_the_buttons_or_its_own_answers_note(self):
        # capArea: the buttons on today's open cap card; after the page's answer, that answer's note; the card
        # opening again drops the note, so a later answer from elsewhere never shows the earlier answer's note.
        out = self.run_access(page_functions("capStopNote", "capNoteSection", "capSection", "capArea")
                              + self.DOM_STUBS + """
const post = async () => ({}), poll = async () => {}, loadList = async () => {}, loadCard = async () => {};
const card = {id: 'X', kind: 'cap', ask: {from: 'cap', options: ['raise50', 'pause']}};
const at = state => ({access: {cap: {card: state, card_id: 'X'}}});
const show = () => { const a = capArea(card); return a ? flat(a) : []; };
const note = {id: 'X', reply: {answered: 'pause', stopped: [], failed: ['w-1']}};
const runs = {};
S.state = at('open'); S.capNote = null; runs.open = show();
S.state = at('answered'); S.capNote = note; runs.answered = show();
S.state = at('open'); runs.reopened = show(); runs.noteAfterReopen = S.capNote;
S.state = at('answered'); runs.answeredElsewhere = show();
S.state = at('answered'); S.capNote = {id: 'Y', reply: note.reply}; runs.otherCardsNote = show();
console.log(JSON.stringify(runs));
""")
        self.assertIn("[cap.raise50]", out["open"])
        self.assertEqual(out["answered"], ["[cap.title]", "[cap.not_stopped]", "w-1"])
        self.assertIn("[cap.raise50]", out["reopened"])
        self.assertIsNone(out["noteAfterReopen"])
        self.assertEqual(out["answeredElsewhere"], [])
        self.assertEqual(out["otherCardsNote"], [])

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_the_settings_access_box_follows_the_state(self):
        # Settings left open: each tick redraws its Access box from the new state, so it never disagrees with the
        # top bar; other screens, and a tick whose route changed while it ran, leave the box alone.
        # An open choice box is not redrawn under the user (its state stays in S.choice, see the choice box tests).
        out = self.run_access(page_functions("capKey", "redrawAccess", "tick") + """
let states = [], onPoll = null;
const poll = async () => { S.state = states.shift(); if (onPoll) onPoll(); };
const loadList = async () => {}, loadCard = async () => {};
const replaced = [];
const box = {replaceWith: n => replaced.push(n)};
const $ = id => (id === 'access-set' ? box : null);
const accessSet = () => 'today ' + S.state.usage.gateway.today;
const st = today => ({access: {cap: {}}, usage: {gateway: {today}}});
(async () => {
  const runs = {};
  for (const [name, route, act, choice] of [['settings', {name: 'settings', arg: null}],
                                    ['lane', {name: 'lane', arg: 'needs'}],
                                    ['left', {name: 'settings', arg: null}, () => { S.route = {name: 'lane', arg: 'needs'}; }],
                                    ['choosing', {name: 'settings', arg: null}, null, {name: 'h', model: 'big'}]]) {
    S.route = route; S.state = st(8); states = [st(42)]; replaced.length = 0; onPoll = act || null;
    S.choice = choice || null;
    await tick();
    runs[name] = replaced.slice();
  }
  console.log(JSON.stringify(runs));
})();
""")
        self.assertEqual(out, {"settings": ["today 42"], "lane": [], "left": [], "choosing": []})

    # The Settings screen's profile switch and its choice box, drawn by accessSet from S.state and S.choice. `$`
    # hands out one 'access-set' holder whose replaceWith keeps the redrawn box in `shown`.
    SWITCH_STUBS = """
let shown = null, calls = [], post = null;
const holder = {replaceWith: n => { shown = n; }};
const $ = id => (id === 'access-set' ? holder : null);
const poll = async () => { calls.push(['poll']); };
const drawSettings = async () => { calls.push(['drawSettings']); };
const fail = status => async () => { throw Object.assign(new Error('REASON'), {status}); };
const buttonsOf = n => find(n, 'button').map(b => ({text: flat(b), pressed: b.attrs['aria-pressed'] || null,
                                                     disabled: !!b.attrs.disabled}));
const CHOICES = {profile: 'h', models: ['big', 'small', 'claude-opus-5-5[1m]', 'claude-opus-5-5'],
                 efforts: ['low', 'medium', 'high', 'xhigh', 'max'], recommended_model: 'claude-opus-5-5[1m]',
                 recommended_effort: 'high'};
S.route = {name: 'settings', arg: null};
S.state = {access: {profile: {name: 'g', mode: 'gateway', label: 'Team'}, cap: {}, profiles: [
  {name: 'g', label: 'Team', mode: 'gateway', needs_choice: false},
  {name: 'h', label: 'h', mode: 'gateway', needs_choice: true},
  {name: 'subscription', label: 'subscription', mode: 'subscription', needs_choice: false}],
  broken_profiles: [{name: 'x', error: 'BROKEN-E'}]}, usage: {mode: 'gateway', windows: [], gateway: {}}};
"""
    SWITCH_FUNCS = ("errBox", "redrawAccess", "onSettings", "afterSwitch", "switchProfile", "choiceBox", "accessSet")

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_settings_has_one_switch_button_per_loading_profile_and_the_active_one_is_pressed(self):
        out = self.run_access(page_functions(*self.SWITCH_FUNCS) + self.DOM_STUBS + self.SWITCH_STUBS + """
(async () => {
  const runs = {};
  S.choice = null; S.switchError = null; S.switching = false;
  const set = accessSet();
  runs.buttons = buttonsOf(set);
  runs.text = flat(set);
  // A click on another profile: the switch posts its name, then the state and Settings are drawn again.
  post = async (path, body) => { calls.push(['post', path, body]); return {active: 'subscription', was: 'g'}; };
  await find(set, 'button')[2].attrs.onclick();
  runs.ok = calls.slice();
  runs.okState = [S.choice, S.switchError, S.switching];
  // Refusals: a label in the page's language, the server's reason under it; the buttons work again.
  for (const [name, status] of [['refused', 409], ['unknown', 404], ['failed', 500], ['models', 502]]) {
    post = fail(status); shown = null;
    await find(accessSet(), 'button')[1].attrs.onclick();
    runs[name] = {box: find(shown, 'div').filter(d => d.attrs.class === 'warn' && d.attrs.role === 'status').map(flat),
                  disabled: buttonsOf(shown).map(b => b.disabled)};
  }
  // A switch still posting: every button is off, so a second click sends nothing.
  S.switchError = null; S.switching = true; calls.length = 0;
  runs.busy = buttonsOf(accessSet()).map(b => b.disabled);
  await switchProfile('subscription');
  runs.busyCalls = calls.slice();
  console.log(JSON.stringify(runs));
})();
""")
        self.assertEqual(out["buttons"], [
            {"text": ["Team", "g", "[access.current]"], "pressed": "true", "disabled": True},
            {"text": ["h", "[access.needs_choice]"], "pressed": "false", "disabled": False},
            {"text": ["subscription"], "pressed": "false", "disabled": False}])
        for t in ("[access.switch]", "[access.switch_note]", '[access.broken {"name":"x"}]', "BROKEN-E"):
            with self.subTest(t):
                self.assertIn(t, out["text"])
        self.assertNotIn("x", [b["text"][0] for b in out["buttons"]])  # a broken profile gets no button
        self.assertEqual(out["ok"], [["post", "/api/profile/use", {"name": "subscription"}], ["poll"],
                                     ["drawSettings"]])
        self.assertEqual(out["okState"], [None, None, False])
        self.assertEqual(out["refused"], {"box": [["[access.switch_refused]", "REASON"]],
                                          "disabled": [True, False, False]})
        self.assertEqual(out["unknown"]["box"], [["[access.switch_refused]", "REASON"]])
        self.assertEqual(out["failed"]["box"], [["[access.switch_failed]", "REASON"]])
        self.assertEqual(out["models"]["box"], [["[access.models_unavailable]", "REASON"]])
        self.assertEqual(out["busy"], [True, True, True])
        self.assertEqual(out["busyCalls"], [])
        text = PAGE.read_text(encoding="utf-8")
        self.assertNotIn("5 minutes", text)  # v1's "takes up to 5 minutes" note is not ported
        self.assertIn("takes effect at once", self.labels()["en"]["access.switch_note"])

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_an_unchosen_profile_opens_the_choice_box_that_posts_the_picks(self):
        out = self.run_access(page_functions(*self.SWITCH_FUNCS) + self.DOM_STUBS + self.SWITCH_STUBS + """
const selects = n => find(n, 'select').map(sel => ({name: sel.attrs.name,
  options: find(sel, 'option').map(o => o.attrs.value),
  selected: find(sel, 'option').filter(o => o.attrs.selected).map(o => o.attrs.value)}));
const box = n => find(n, 'div').filter(d => d.attrs.id === 'choice-box');
(async () => {
  const runs = {};
  S.choice = null; S.switchError = null; S.switching = false;
  post = async (path, body) => { calls.push(['post', path, body]); return {needs_choice: true, choices: CHOICES}; };
  await switchProfile('h');
  runs.calls = calls.slice();
  runs.boxes = box(shown).length;
  runs.selects = selects(shown);
  runs.text = flat(box(shown));
  // The user picks a model; a redraw (a tick, a return to Settings) keeps the box and the pick.
  find(shown, 'select')[0].attrs.onchange({target: {value: 'big'}});
  runs.redrawn = selects(accessSet());
  // Refused: the box stays open with the label and the server's text; its buttons work again.
  for (const [name, status] of [['refused', 409], ['models', 502]]) {
    post = fail(status); shown = null;
    await find(box(accessSet())[0], 'button')[0].attrs.onclick();
    runs[name] = {open: box(shown).length, err: find(box(shown), 'div').filter(d => d.attrs.class === 'warn').map(flat),
                  disabled: buttonsOf(box(shown)).map(b => b.disabled), picks: selects(shown).map(x => x.selected)};
  }
  // Choose and switch: the picks are posted, then the box closes and Settings is drawn again.
  calls.length = 0;
  post = async (path, body) => { calls.push(['post', path, body]); return {chosen: body, active: 'h', was: 'g'}; };
  await find(box(accessSet())[0], 'button')[0].attrs.onclick();
  runs.chose = calls.slice();
  runs.after = S.choice;
  // Cancel closes it without a post.
  post = async () => ({needs_choice: true, choices: CHOICES});
  await switchProfile('h');
  calls.length = 0;
  find(box(accessSet())[0], 'button')[1].attrs.onclick();
  runs.cancel = [S.choice, box(shown).length, calls.slice()];
  // A profile that is gone from the state (removed elsewhere) drops its box.
  await switchProfile('h');
  S.state.access.profiles = S.state.access.profiles.filter(q => q.name !== 'h');
  runs.gone = [box(accessSet()).length, S.choice];
  console.log(JSON.stringify(runs));
})();
""")
        self.assertEqual(out["calls"], [["post", "/api/profile/use", {"name": "h"}]])  # no switch drawn yet
        self.assertEqual(out["boxes"], 1)
        self.assertEqual(out["selects"], [
            {"name": "model", "options": ["big", "small", "claude-opus-5-5[1m]", "claude-opus-5-5"],
             "selected": ["claude-opus-5-5[1m]"]},
            {"name": "effort", "options": ["low", "medium", "high", "xhigh", "max"], "selected": ["high"]}])
        for t in ('[access.choice_title {"name":"h"}]', "[access.choice_model]", "[access.choice_effort]",
                  "[access.choice_submit]", "[access.choice_cancel]"):
            with self.subTest(t):
                self.assertIn(t, out["text"])
        self.assertEqual([x["selected"] for x in out["redrawn"]], [["big"], ["high"]])
        self.assertEqual(out["refused"], {"open": 1, "err": [["[access.switch_refused]", "REASON"]],
                                          "disabled": [False, False], "picks": [["big"], ["high"]]})
        self.assertEqual(out["models"]["err"], [["[access.models_unavailable]", "REASON"]])
        self.assertEqual(out["chose"], [["post", "/api/profile/choose",
                                         {"name": "h", "model": "big", "effort": "high"}], ["poll"], ["drawSettings"]])
        self.assertIsNone(out["after"])
        self.assertEqual(out["cancel"], [None, 0, []])
        self.assertEqual(out["gone"], [0, None])

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_a_reply_that_lands_after_settings_was_left_opens_no_box_and_shows_no_error(self):
        # The user clicks, then leaves Settings before the post answers: show() has dropped the box and the error,
        # so the late reply must not bring them back (they would reappear on the next visit to Settings).
        out = self.run_access(page_functions(*self.SWITCH_FUNCS) + self.DOM_STUBS + self.SWITCH_STUBS + """
const later = reply => async (path, body) => {
  calls.push(['post', path, body]);
  S.route = {name: 'lane', arg: 'needs'};  // left Settings while the post ran
  if (reply instanceof Error) throw reply;
  return reply;
};
const err = Object.assign(new Error('REASON'), {status: 409});
(async () => {
  const runs = {};
  S.choice = null; S.switchError = null; S.switching = false;
  post = later({needs_choice: true, choices: CHOICES});
  await switchProfile('h');
  runs.choice = [S.choice, S.switchError, S.switching];
  S.route = {name: 'settings', arg: null};
  post = later(err);
  await switchProfile('h');
  runs.refused = [S.choice, S.switchError, S.switching];
  // A switch that went through still reads the new state, but draws no Settings on another screen.
  S.route = {name: 'settings', arg: null}; calls.length = 0;
  post = later({active: 'subscription', was: 'g'});
  await switchProfile('subscription');
  runs.switched = calls.slice();
  // The choice box's own post, refused after Settings was left: no error is put back on the box.
  S.route = {name: 'settings', arg: null};
  post = async () => ({needs_choice: true, choices: CHOICES});
  await switchProfile('h');
  const c = S.choice;
  post = later(err);
  await find(find(accessSet(), 'div').filter(d => d.attrs.id === 'choice-box')[0], 'button')[0].attrs.onclick();
  runs.box = [c.error, c.busy];
  console.log(JSON.stringify(runs));
})();
""")
        self.assertEqual(out["choice"], [None, None, False])
        self.assertEqual(out["refused"], [None, None, False])
        self.assertEqual(out["switched"], [["post", "/api/profile/use", {"name": "subscription"}], ["poll"]])
        self.assertEqual(out["box"], [None, False])

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_leaving_settings_drops_the_choice_box_and_the_switch_error(self):
        out = self.run_access(page_functions("parseRoute", "show") + """
const LANES = ['needs', 'running', 'planned', 'done'];
const location = {hash: ''};
const noop = () => {}, later = async () => {};
const drawRail = noop, panes = noop, drawCard = noop;
const loadList = later, loadCard = later, drawSources = later, drawTemplates = later, drawSettings = later;
const $ = () => ({value: ''});
(async () => {
  const runs = {};
  for (const hash of ['#/settings', '#/lane/running', '#/card/X', '#/sources', '#/templates']) {
    location.hash = hash;
    S.capNote = null;
    S.choice = {name: 'h'}; S.switchError = {key: 'access.switch_refused', text: 'REASON'};
    await show();
    runs[hash] = [S.choice, S.switchError];
  }
  console.log(JSON.stringify(runs));
})();
""")
        self.assertEqual(out.pop("#/settings"), [{"name": "h"}, {"key": "access.switch_refused", "text": "REASON"}])
        for hash, kept in out.items():
            with self.subTest(hash):
                self.assertEqual(kept, [None, None])

    def test_every_new_access_label_has_an_en_and_a_zh_text(self):
        labels = self.labels()
        for key in ("access.switch", "access.switch_note", "access.current", "access.needs_choice",
                    "access.switch_refused", "access.switch_failed", "access.models_unavailable",
                    "access.choice_title", "access.choice_model", "access.choice_effort", "access.choice_submit",
                    "access.choice_cancel"):
            with self.subTest(key):
                self.assertTrue(labels["en"][key])
                self.assertRegex(labels["zh"][key], r"[\u4e00-\u9fff]")
        self.assertIn("{name}", labels["zh"]["access.choice_title"])

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_the_open_card_is_redrawn_when_the_cap_card_opens_again(self):
        # Answered on the page, the card is drawn without buttons; when spend passes the raised limit the same card
        # opens again, and the 10-second tick redraws the card on screen so its buttons come back.
        out = self.run_access(page_functions("capKey", "tick") + """
const cap = (card, raise_pct) => ({access: {cap: {card, card_id: 'X', raise_pct}}});
let states = [];
const poll = async () => { S.state = states.shift(); };
const loads = [];
let onList = null;  // what the user does while the list loads
const loadList = async () => { if (onList) onList(); }, loadCard = async id => { loads.push(id); };
(async () => {
  const runs = {};
  for (const [name, route, before, after, act] of [
      ['reopened', {name: 'card', arg: 'X'}, cap('answered', 50), cap('open', 50)],
      ['same', {name: 'card', arg: 'X'}, cap('open', 50), cap('open', 50)],
      ['lane', {name: 'lane', arg: 'needs'}, cap('answered', 50), cap('open', 50)],
      ['other card', {name: 'card', arg: 'Y'}, cap('answered', 50), cap('open', 50)],
      // The day ends while the cap card is on screen: its buttons must go (the card named before the poll).
      ['day over', {name: 'card', arg: 'X'}, cap('open', 0), {access: {cap: {card: null, card_id: null, raise_pct: 0}}}],
      // A new cap card opens while it is already on screen: its buttons must come (the card named after the poll).
      ['new card', {name: 'card', arg: 'Z'}, {access: {cap: {card: null, card_id: null, raise_pct: 0}}},
       {access: {cap: {card: 'open', card_id: 'Z', raise_pct: 0}}}],
      // The user clicks a lane while the list loads: the tick must not bring the cap card back.
      ['left during the list', {name: 'card', arg: 'X'}, cap('answered', 50), cap('open', 50),
       () => { S.route = {name: 'lane', arg: 'planned'}; }],
      // ...or clicks another card in the list: that card stays, the cap card is not pulled back over it.
      ['left for another card', {name: 'card', arg: 'X'}, cap('answered', 50), cap('open', 50),
       () => { S.route = {name: 'card', arg: 'Y'}; }]]) {
    S.route = route; S.state = before; states = [after]; loads.length = 0; onList = act || null;
    await tick();
    runs[name] = loads.slice();
  }
  console.log(JSON.stringify(runs));
})();
""")
        self.assertEqual(out, {"reopened": ["X"], "same": [], "lane": [], "other card": [], "day over": ["X"],
                               "new card": ["Z"], "left during the list": [],
                               "left for another card": []})

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_a_cap_state_that_cannot_be_read_is_a_red_line_and_a_settings_error(self):
        out = self.run_access(page_functions("drawAccess", "accessSet") + self.DOM_STUBS + """
const boxes = {};
const $ = id => (boxes[id] = boxes[id] || h('span', {id}));
const access = {profile: {name: 's', mode: 'subscription', label: 'Subscription'},
                cap: {card: null, card_id: null, paused: false, error: 'CAP-STATE-ERR-C'}};
S.state = {access, usage: {mode: 'subscription', windows: []}};
const alerts = accessAlerts(access);
drawAccess();
console.log(JSON.stringify({alerts, top: flat(boxes.access), set: flat(accessSet()),
                            none: accessAlerts({profile: null, cap: {error: null}})}));
""")
        self.assertEqual(out["alerts"], [{"cls": "keyerr", "label": "[access.cap_error]", "detail": "CAP-STATE-ERR-C",
                                          "href": "#/settings"}])
        self.assertIn("[access.cap_error]", out["top"])
        self.assertIn(" CAP-STATE-ERR-C", out["top"])
        self.assertIn("[access.cap_error]", out["set"])
        self.assertIn("CAP-STATE-ERR-C", out["set"])
        self.assertEqual(out["none"], [])
        labels = self.labels()
        self.assertEqual(labels["en"]["access.cap_error"],
                         "Daily cap state not readable; new gateway workers may wait")
        self.assertRegex(labels["zh"]["access.cap_error"], r"[\u4e00-\u9fff]")

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_the_top_bar_and_the_access_set_draw_every_text(self):
        out = self.run_access(page_functions("drawAccess", "accessSet") + self.DOM_STUBS + """
const boxes = {};
const $ = id => (boxes[id] = boxes[id] || h('span', {id}));
const gwUsage = {mode: 'gateway', windows: [], gateway: {today: 512, limit: 500, week: 900, month: 2300,
                 counted_at: null}};
S.state = {access: {profile: {name: 'g', mode: 'gateway', label: 'Team', model: 'big', effort: 'high'},
                    key_error: 'KEY-BROKEN-K', cap: {}}, usage: gwUsage};
drawAccess();
const top = flat(boxes.access);
S.state = {access: {profile: null, profile_error: 'PROFILE-ERR-P', error: 'FOLDER-ERR-F', cap: {},
                    key_error: 'KEY-SET-K',
                    broken_profiles: [{name: 'x', error: 'BROKEN-E'}]}, usage: gwUsage};
const set = flat(accessSet());
// The top bar's alerts are links: the open cap card, and Settings for a profile that does not work.
S.state = {access: {profile: null, profile_error: 'PROFILE-ERR-P', cap: {card: 'open', card_id: 'c-1'}}, usage: gwUsage};
drawAccess();
const links = find(boxes.access, 'a').map(a => [a.attrs.href, a.attrs.text]);
// Settings with a working gateway profile: the profile, its model and effort, and the page sessions' mode.
S.state = {access: {profile: {name: 'g', mode: 'gateway', label: 'Team', model: 'big', effort: 'high'}, cap: {}},
           usage: gwUsage};
const profileSet = flat(accessSet());
console.log(JSON.stringify({top, set, links, profileSet}));
""")
        top, sett = out["top"], out["set"]
        self.assertIn("[access.page_sessions]", top)  # the badge says page sessions stay on the subscription
        self.assertIn(["#/card/c-1", "[cap.reached]"], out["links"])
        self.assertIn(["#/settings", "[access.profile_error]"], out["links"])
        for t in ("[access.profile]", '[access.workers {"label":"Team"}]', "[access.model]", "big · high",
                  "[access.pages]", "[access.subscription]"):
            with self.subTest(t):
                self.assertIn(t, out["profileSet"])
        self.assertIn(" KEY-BROKEN-K", top)  # the alert's server text, after its label
        self.assertIn("[access.key_error]", top)
        self.assertIn('[usage.today_short {"spent":"$512.00","limit":"$500.00"}]', top)
        self.assertIn("big · high", top)  # the badge's model and effort
        self.assertIn('[access.workers {"label":"Team"}]', top)
        for t in ("PROFILE-ERR-P", "FOLDER-ERR-F", "[access.profile_error]", "KEY-SET-K", "BROKEN-E",
                  '[access.broken {"name":"x"}]', "$512.00 / $500.00", "[usage.today]"):
            with self.subTest(t):
                self.assertIn(t, sett)

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_usage_values_are_shown_in_full(self):
        out = self.run_access("""
const gw = (g) => ({mode: 'gateway', windows: [{kind: 'five_hour', percent: 42.4, resets_at: 'R'}], gateway: g});
console.log(JSON.stringify([
  money(512), money(500), money(2300), money(1234567.891), money(0), money(null), money(Number.MAX_VALUE),
  usageShort(gw({today: 512, limit: 500})), usageShort(gw({today: 2300, limit: null})),
  usageShort(gw({today: null, limit: 500})),
  usageShort({mode: 'subscription', windows: [{kind: 'seven_day', percent: 7}, {kind: 'five_hour', percent: 81.6}]}),
  usageShort({mode: 'subscription', windows: null}), usageShort({mode: null}),
  usageRows(gw({today: 512, week: 900, month: 2300, limit: 500, counted_at: null, unknown: true})),
  usageRows({mode: 'subscription', windows: [{kind: 'opus_week', percent: 3}], stale: true,
             credits_used_usd: 2300, credits_limit_usd: 5000}),
  usageRows({mode: 'subscription', windows: null})]));
""")
        self.assertEqual(out[:7], ["$512.00", "$500.00", "$2,300.00", "$1,234,567.89", "$0.00", "$?",
                                   "[usage.too_large]"])
        self.assertEqual(out[7], '[usage.today_short {"spent":"$512.00","limit":"$500.00"}]')
        self.assertEqual(out[8], '[usage.today_only {"spent":"$2,300.00"}]')
        self.assertEqual(out[9], "[usage.not_today]")
        self.assertEqual(out[10], '[usage.window_short {"kind":"[usage.five_hour]","pct":82}]')
        self.assertEqual(out[11:13], ["", ""])
        self.assertEqual([r["value"] for r in out[13]],
                         ["$512.00 / $500.00", "$900.00", "$2,300.00", "[usage.never]", "[usage.unknown]",
                          '[usage.resets {"pct":42,"when":"when:R"}]'])
        self.assertEqual(out[13][-1]["label"], '[usage.page_sessions {"kind":"[usage.five_hour]"}]')
        self.assertEqual([(r["label"], r["value"]) for r in out[14]],
                         [("opus_week", "3%"), ("[usage.credits]", "$2,300.00 / $5,000.00"), ("", "[usage.stale]")])
        self.assertEqual(out[15], [{"label": "[usage.windows]", "value": "[usage.no_windows]", "warn": True}])
        # A real count: "Counted" says how long ago, from the page's clock (five minutes here).
        counted = self.run_access("""
Date.now = () => Date.parse('2026-11-11T20:05:00Z');
const rows = usageRows({mode: 'gateway', windows: [], gateway: {today: 1, limit: 2, counted_at: '2026-11-11T15:00:00-05:00'}});
console.log(JSON.stringify(rows.filter(r => r.label === '[usage.counted]').map(r => r.value)));
""")
        self.assertEqual(counted, ["ago:300"])
        # The two window kinds Claude Code names get a label in the page's language; any other kind shows as it is.
        names = self.run_access("""
console.log(JSON.stringify(['seven_day', 'five_hour', 'opus_week'].map(kind => windowName({kind}))));
""")
        self.assertEqual(names, ["[usage.seven_day]", "[usage.five_hour]", "opus_week"])
        labels = self.labels()
        self.assertEqual(labels["en"]["usage.today_short"], "{spent} / {limit} today")
        css = re.search(r"<style>(.*?)</style>", PAGE.read_text(encoding="utf-8"), re.S).group(1)
        for sel in (".uval", ".access", ".abadge", ".status", ".kv dd"):
            for rule in re.findall(r"([^{}]*)\{([^}]*)\}", css):
                if sel in [x.strip() for x in rule[0].split(",")]:
                    with self.subTest(sel=sel, rule=rule[0]):
                        self.assertNotRegex(rule[1], r"overflow\s*:\s*hidden|text-overflow|(?<![-\w])width\s*:")

    def labels(self) -> dict:
        text = PAGE.read_text(encoding="utf-8")
        return json.loads(re.search(r'<script type="application/json" id="labels">(.*?)</script>', text,
                                    re.S).group(1))

    def test_the_pages_search_caps_match_the_servers(self):
        text = PAGE.read_text(encoding="utf-8")
        self.assertIn(f"const SEARCH_MAX_CHARS = {server.SEARCH_MAX_CHARS};", text)
        self.assertIn(f"const SEARCH_MAX_WORDS = {server.SEARCH_MAX_WORDS};", text)


# -- invisible text: a search q and a question text follow cards.blank ---------------------------------------------

class InvisibleTextTest(ServerCase):
    def setUp(self):
        super().setUp()
        for i, lane in enumerate(("needs", "running", "done")):
            self.add(title=f"card {i}", lane=lane, plain=plain("pelican"))
    def test_an_invisible_q_lists_what_no_q_lists(self):
        everything = self.ok("/api/cards")
        for q in ("%EF%BB%BF", "%EF%BB%BF%20%1C"):
            with self.subTest(q=q), mock.patch("assistant.store.Store.search",
                                               side_effect=AssertionError("the search ran")):
                self.assertEqual(self.ok(f"/api/cards?q={q}"), everything)

    def test_a_q_with_words_next_to_an_invisible_character_is_searched_as_it_is(self):
        for q, text in (("%EF%BB%BFT1", "\ufeffT1"), ("a%EF%BB%BF", "a\ufeff")):
            with self.subTest(q=q), mock.patch("assistant.store.Store.search", autospec=True,
                                               return_value=[]) as search:
                self.assertEqual(self.ok(f"/api/cards?q={q}")["total"], 0)
                self.assertEqual(search.call_args.args[1:], (text,))

    def test_a_question_of_only_invisible_characters_is_400_text_is_blank(self):
        card = self.add()
        for text in ("\ufeff", "\ufeff \x1c"):
            with self.subTest(text=text):
                before = tree(self.data)
                status, _, reply = self.ask(card["id"], text)
                self.assertEqual((status, reply), (400, {"error": "text is blank"}))
                self.assertEqual(tree(self.data), before)

    def test_a_question_with_words_next_to_an_invisible_character_is_saved_as_it_is(self):
        card = self.add()
        for text in ("\ufeffT1", "a\ufeff"):
            with self.subTest(text=text):
                status, _, reply = self.ask(card["id"], text)
                self.assertEqual(status, 201, reply)
                self.assertEqual(reply["question"]["text"], text)


if __name__ == "__main__":
    unittest.main()
