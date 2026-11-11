"""The local page's server (design 9.1): one page and its JSON, on 127.0.0.1 only.

`make_server(data, ...)` returns a `ThreadingHTTPServer` bound to `127.0.0.1:<port>` (4317 by default, 0 for a free
port); `ap2 serve` runs it. Routes:

- `GET /`: the page, `builtin/page/local.html`, with a strict Content-Security-Policy.
- `GET /api/state`: the mode, languages, lane counts, projects, the gate's heartbeat and the page's URL.
- `GET /api/cards?lane=&kind=&parent=&q=&limit=`: card rows, in lane order; `GET /api/cards/<id>`: the full card,
  with its template's view, source block, children, links and activity.
- `GET /api/sources[/<name>]`, `GET /api/templates[/<kind>]`, `GET /api/settings`: the Manage screens.
- `POST /api/cards/<id>/questions`: save a question about a card (design 9.4). The only write.

Every request must name this server in its `Host` header (`127.0.0.1:<port>` or `localhost:<port>`), so a page on
another site whose name points at 127.0.0.1 (DNS rebinding) cannot read cards. The one POST also refuses another
site's `Origin` and any body that is not `application/json`, so a plain HTML form elsewhere cannot post. No route
runs a process, calls Jira, Slack, GitHub or `claude`, or writes outside the data folder; the GETs write nothing
(a search may build `index.sqlite`, the store's own cache, as `ap2 search` does).
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import stat
import sys
from collections import Counter
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from typing import Callable, Optional
from urllib.parse import parse_qs, unquote

from assistant import cards, config, datadir, gate, index, templates
from assistant.cards import CardError
from assistant.config import ConfigError
from assistant.sources import Loader, SourceError
from assistant.store import LOCK, Store

try:
    import fcntl
except ImportError:  # not POSIX: reads then take no lock
    fcntl = None

HOST = "127.0.0.1"
PORT = 4317
PAGE = Path(__file__).resolve().parent / "builtin" / "page" / "local.html"

LIST_LIMIT = 500        # rows a list returns when `limit` is not given
LIST_MAX = 5000         # the largest `limit`
LATE_SECONDS = 180      # the heartbeat turns red after 3 minutes without a gate run (design 7.4)
QUESTION_MAX = 2000     # characters in one question
BODY_MAX = 64 * 1024    # bytes in a request body
DRAIN_MAX = 1024 * 1024  # a refused body up to this size is read and dropped, so the reply is not cut by a reset
GATE_MAX = 1024 * 1024  # the largest gate.json read
ASKED_BY = "page"

CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
       "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
LANE_RANK = {lane: i for i, lane in enumerate(cards.LANES)}
PRI_RANK = {pri: i for i, pri in enumerate(cards.PRIS)}
DIGITS = re.compile(r"[0-9]{1,9}\Z", re.ASCII)
VIEW_WITHOUT_TEMPLATE = ("children", "links", "questions", "activity")


class HTTPError(Exception):
    def __init__(self, status: int, message: str, allow: Optional[str] = None):
        super().__init__(message)
        self.status, self.message, self.allow = status, message, allow


def _no_constant(name):
    raise ValueError(f"{name} is not JSON")


# --- reading the store without writing -------------------------------------------------------------------------

class _ReadStore(Store):
    """The store for reads: a shared hold of the store lock when its file exists, and no lock file made when it
    does not (no store write has happened yet), so a GET on a fresh data folder creates nothing."""

    @contextlib.contextmanager
    def _locked(self):
        fd = None
        if fcntl is not None:
            try:
                fd = os.open(str(self.data / LOCK), os.O_RDONLY)
            except FileNotFoundError:
                fd = None
        if fd is None:
            yield
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            yield
        finally:
            os.close(fd)


def _rank(table: dict, value, missing: int) -> int:
    return table.get(value, missing) if isinstance(value, str) else missing


def order(found: list) -> list:
    """Lane order; in Needs you, Running and Planned by `pri`, then the newest `updated_at`; History newest first."""
    newest = sorted(found, key=lambda c: (str(c.get("updated_at") or ""), str(c.get("id"))), reverse=True)
    return sorted(newest, key=lambda c: (_rank(LANE_RANK, c.get("lane"), len(LANE_RANK)),
                                         0 if c.get("lane") == "done" else _rank(PRI_RANK, c.get("pri"), len(PRI_RANK))))


def row(card: dict, langs: tuple) -> dict:
    """A card as a list row: what the lists, the rail and the phone page need, nothing more."""
    plain = card.get("plain") if isinstance(card.get("plain"), dict) else {}
    where = plain.get("where") if isinstance(plain.get("where"), dict) else {}
    ask = card.get("ask")
    return {"id": card.get("id"), "title": card.get("title"), "kind": card.get("kind"), "lane": card.get("lane"),
            "pri": card.get("pri"), "feed": card.get("feed"), "parent": card.get("parent"),
            "origin": card.get("origin"), "ask_type": ask.get("type") if isinstance(ask, dict) else None,
            "plain_where": {lang: where[lang] for lang in langs if isinstance(where.get(lang), str)},
            "updated_at": card.get("updated_at")}


def source_block(card: dict) -> Optional[dict]:
    """The source block (design 5.4) from the card's `source` field as the engine writes it. The link is kept
    only when it is an http or https URL; any other (a hand-edited `javascript:` link) becomes null with
    `link_refused`, so the page never draws it."""
    src = card.get("source")
    if not isinstance(src, dict):
        return None
    record = src.get("record") if isinstance(src.get("record"), dict) else {}
    out = {"name": src.get("name"), "author": record.get("author"), "place": record.get("place"),
           "at": record.get("at"), "text": record.get("text"), "link": None, "record": src.get("record")}
    link = src.get("link")
    if isinstance(link, str) and link[:8].lower().startswith(("http://", "https://")):
        out["link"] = link
    elif link is not None:
        out["link_refused"] = True
    return out


def heartbeat(data: Path, now: datetime) -> dict:
    """The gate's last run from `<data>/gate.json` (design 7.4): late at 180 seconds or more, or when there is no
    usable time (an `error` then says why)."""
    path = data / gate.HEARTBEAT
    out = {"last_run_at": None, "age_seconds": None, "late": True}
    try:
        with open(path, "rb") as f:
            raw = f.read(GATE_MAX + 1)
    except FileNotFoundError:
        return out
    except OSError as e:
        return {**out, "error": f"{path}: cannot read: {e}"}
    try:
        if len(raw) > GATE_MAX:
            raise ValueError(f"larger than {GATE_MAX} bytes")
        obj = json.loads(raw.decode("utf-8"), parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as e:
        return {**out, "error": f"{path}: not JSON: {e}"}
    last = obj.get("last_run_at") if isinstance(obj, dict) else None
    try:
        at = cards.parse_stamp(last)
    except (TypeError, ValueError):
        return {**out, "error": f"{path}: no readable last_run_at (found {last!r})"}
    age = int((now - at).total_seconds())
    if age < 0:
        return {**out, "last_run_at": last, "age_seconds": age,
                "error": f"{path}: last_run_at is ahead of the clock by {-age} s; the clock may have gone back"}
    return {"last_run_at": last, "age_seconds": age, "late": age >= LATE_SECONDS}


def disk_use(data: Path) -> dict:
    """Bytes in each data-folder folder and in `index.sqlite`, counted with lstat: a link counts as itself and is
    never followed. What cannot be read counts as 0."""
    return {name: _size(data / name) for name in datadir.FOLDERS + (index.FILE,)}


def _size(path: Path) -> int:
    try:
        st = os.lstat(path)
    except OSError:
        return 0
    total = st.st_size
    stack = [str(path)] if stat.S_ISDIR(st.st_mode) else []
    while stack:
        try:
            entries = os.scandir(stack.pop())
        except OSError:
            continue
        with entries:
            for e in entries:
                try:
                    s = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                total += s.st_size
                if stat.S_ISDIR(s.st_mode):
                    stack.append(e.path)
    return total


# --- the server ------------------------------------------------------------------------------------------------

class Server(ThreadingHTTPServer):
    daemon_threads = True

    data: Path
    cfg: Optional[config.Config]
    plugin_dir: Optional[Path]
    clock: Callable[[], datetime]
    log_stream = None
    url: str

    def server_bind(self):
        # HTTPServer.server_bind looks the address up by name (socket.getfqdn); this server has no name to find.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = HOST, self.server_address[1]

    def handle_error(self, request, client_address):
        e = sys.exc_info()[1]
        self.log(f"{client_address[0]} error: {type(e).__name__}: {e}")

    def log(self, message: str) -> None:
        """One line on the log stream (stderr by default), the server's UTC time first (design 7.4)."""
        stream = self.log_stream
        if stream is None:
            return
        text = "".join(ch if ch.isprintable() else f"\\x{ord(ch):02x}" for ch in str(message))
        with contextlib.suppress(OSError, ValueError):
            stream.write(f"{cards.stamp(self.clock())} {text}\n")
            stream.flush()


def make_server(data, port: int = PORT, cfg: Optional[config.Config] = None, plugin_dir: Optional[Path] = None,
                clock: Callable[[], datetime] = cards.now_utc) -> Server:
    """The server for the data folder `data`, bound to 127.0.0.1:`port` (0 takes a free port), not yet serving.
    `cfg` None reads `config.yaml` on every request; `plugin_dir` is the built-in sources folder. Raises
    ConfigError when the port cannot be used."""
    try:
        srv = Server((HOST, port), Handler)
    except (OSError, OverflowError) as e:
        raise ConfigError(f"cannot listen on {HOST}:{port}: {e}") from e
    srv.data = Path(data)
    srv.cfg = cfg
    srv.plugin_dir = Path(plugin_dir) if plugin_dir is not None else None
    srv.clock = clock
    srv.log_stream = sys.stderr
    srv.url = f"http://{HOST}:{srv.server_address[1]}/"
    return srv


class Handler(BaseHTTPRequestHandler):
    server: Server
    protocol_version = "HTTP/1.0"  # one request per connection
    server_version = "ap2"
    sys_version = ""
    timeout = 30  # seconds a slow client may take per read

    def version_string(self):
        return self.server_version

    # -- plumbing ------------------------------------------------------------------------------------------------

    def do_GET(self):
        self._handle()

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = do_TRACE = do_CONNECT = do_GET

    def _handle(self) -> None:
        self._body_read = False
        allow = None
        try:
            self._check_host()
            status, payload = self._route()
        except HTTPError as e:
            status, payload, allow = e.status, {"error": e.message}, e.allow
        if status == 204:
            self._send(status, b"", None)
        elif isinstance(payload, bytes):
            self._send(status, payload, "text/html; charset=utf-8")
        else:
            self._send_json(status, payload, allow)

    def send_error(self, code, message=None, explain=None):
        """The base class calls this for a request it cannot parse and for an unknown method: reply in JSON with
        the same headers as every other reply (and 403 when the Host header is not this server)."""
        self.close_connection = True
        self._body_read = getattr(self, "_body_read", False)
        if getattr(self, "headers", None) is not None:
            try:
                self._check_host()
            except HTTPError as e:
                code, message = e.status, e.message
        if message is None:
            message = self.responses.get(code, ("error",))[0]
        self._send_json(code, {"error": message})

    def _check_host(self) -> None:
        port = self.server.server_address[1]
        hosts = self.headers.get_all("Host") or []
        if len(hosts) != 1 or hosts[0].strip().lower() not in (f"{HOST}:{port}", f"localhost:{port}"):
            raise HTTPError(403, f"refused: the Host header must be {HOST}:{port} or localhost:{port}")

    def _own_origins(self) -> tuple:
        port = self.server.server_address[1]
        return (f"http://{HOST}:{port}", f"http://localhost:{port}")

    def _drain(self) -> None:
        """Read and drop a small body nobody read, so closing the connection does not reset the reply."""
        if self._body_read or self.headers is None:
            return
        self._body_read = True
        length = self.headers.get("Content-Length", "")
        if DIGITS.match(length.strip()) and int(length) <= DRAIN_MAX:
            with contextlib.suppress(OSError):
                self.rfile.read(int(length))

    def _send_json(self, status: int, payload, allow: Optional[str] = None) -> None:
        try:
            text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            body = text.encode("utf-8")
        except UnicodeEncodeError:  # e.g. a file name that is not UTF-8 in an error
            body = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("ascii")
        self._send(status, body, "application/json; charset=utf-8", [("Allow", allow)] if allow else [])

    def _send(self, status: int, body: bytes, ctype: str, extra=()) -> None:
        if getattr(self, "headers", None) is not None:
            self._drain()
        self.send_response(status)
        if ctype is not None:
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        for key, value in extra:
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_request(self, code="-", size="-"):
        """2xx and 3xx replies are not logged (the page polls every 10 seconds); errors are."""
        try:
            failed = int(code) >= 400
        except (TypeError, ValueError):
            failed = True
        if failed:
            self.log_message('"%s" %s', self.requestline, code)

    def log_message(self, format, *args):
        self.server.log(f"{self.client_address[0]} {format % args}")

    # -- routes --------------------------------------------------------------------------------------------------

    def _route(self):
        raw_path, _, raw_query = self.path.split("#", 1)[0].partition("?")
        if not raw_path.startswith("/"):
            raise HTTPError(404, "no such page")
        parts = [unquote(p, errors="surrogateescape") for p in raw_path.split("/")[1:]]
        query = parse_qs(raw_query, keep_blank_values=True, errors="surrogateescape")
        method = self.command
        if len(parts) == 4 and parts[:2] == ["api", "cards"] and parts[3] == "questions":
            if method != "POST":
                raise HTTPError(405, f"{method} is not allowed here; questions are saved with POST", allow="POST")
            return self.ask(parts[2])
        if method != "GET":
            raise HTTPError(405, f"{method} is not allowed: this page is read-only but for saving a question",
                            allow="GET")
        if raw_path == "/":
            return 200, self.page()
        if raw_path == "/favicon.ico":  # browsers ask for it on every open; no icon, and no error line in the log
            return 204, b""
        if not parts or parts[0] != "api":
            raise HTTPError(404, f"no such page: {raw_path}")
        rest = parts[1:]
        if rest == ["state"]:
            return 200, self.state()
        if rest == ["cards"]:
            return 200, self.card_list(query)
        if len(rest) == 2 and rest[0] == "cards":
            return 200, self.card(rest[1])
        if rest == ["sources"]:
            return 200, self.source_list()
        if len(rest) == 2 and rest[0] == "sources":
            return 200, self.source(rest[1])
        if rest == ["templates"]:
            return 200, self.template_list()
        if len(rest) == 2 and rest[0] == "templates":
            return 200, self.template(rest[1])
        if rest == ["settings"]:
            return 200, self.settings()
        raise HTTPError(404, f"no such route: {raw_path}")

    # -- helpers for the routes ----------------------------------------------------------------------------------

    def cfg(self) -> config.Config:
        if self.server.cfg is not None:
            return self.server.cfg
        try:
            return config.load(self.server.data)
        except ConfigError as e:
            raise HTTPError(500, str(e)) from e

    def reader(self, cfg: config.Config) -> _ReadStore:
        return _ReadStore(self.server.data, cfg=cfg, clock=self.server.clock)

    @staticmethod
    def _store_call(fn, *args, **kwargs):
        """A store read; a damaged card file or a store that cannot be used is a 500 naming it."""
        try:
            return fn(*args, **kwargs)
        except CardError as e:
            raise HTTPError(500, str(e)) from e

    def page(self) -> bytes:
        try:
            return PAGE.read_bytes()
        except OSError as e:
            raise HTTPError(500, f"{PAGE}: cannot read the page: {e}") from e

    # -- GET /api/state ------------------------------------------------------------------------------------------

    def state(self) -> dict:
        cfg = self.cfg()
        found = self._store_call(self.reader(cfg).list)
        lanes = {lane: 0 for lane in cards.LANES}
        children = {}
        for c in found:
            if isinstance(c.get("lane"), str) and c["lane"] in lanes:
                lanes[c["lane"]] += 1
            if isinstance(c.get("parent"), str):
                children.setdefault(c["parent"], []).append(c)
        projects = [{"id": c["id"], "title": c.get("title"),
                     "done": sum(1 for k in children.get(c["id"], []) if k.get("lane") == "done"),
                     "total": len(children.get(c["id"], []))}
                    for c in found if c.get("kind") == "project"]
        return {"mode": cfg.mode, "primary_language": cfg.primary_language,
                "secondary_language": cfg.secondary_language, "languages": list(cards.languages(cfg)),
                "lanes": lanes, "running": lanes["running"], "projects": projects,
                "heartbeat": heartbeat(self.server.data, self.server.clock()), "usage": None, "url": self.server.url}

    # -- GET /api/cards ------------------------------------------------------------------------------------------

    @staticmethod
    def _one(query: dict, key: str) -> Optional[str]:
        values = query.get(key)
        if not values or values[0] == "":
            return None
        try:
            cards.check_unicode(values[0], key)
        except CardError as e:
            raise HTTPError(400, str(e)) from e
        return values[0]

    def card_list(self, query: dict) -> dict:
        lane, kind, parent, q = (self._one(query, k) for k in ("lane", "kind", "parent", "q"))
        limit = self._one(query, "limit")
        if lane is not None and lane not in cards.LANES:
            raise HTTPError(400, f"lane must be one of {', '.join(cards.LANES)}, not {lane!r}")
        if parent is not None:
            try:
                cards.check_id(parent)
            except CardError as e:
                raise HTTPError(400, f"parent: {e}") from e
        if limit is None:
            n = LIST_LIMIT
        elif DIGITS.match(limit) and 1 <= int(limit) <= LIST_MAX:
            n = int(limit)
        else:
            raise HTTPError(400, f"limit must be a whole number from 1 to {LIST_MAX}, not {limit!r}")
        cfg = self.cfg()
        if q is not None and q.strip():
            found = [c for c in self._store_call(Store(self.server.data, cfg=cfg, clock=self.server.clock).search, q)
                     if (lane is None or c.get("lane") == lane) and (kind is None or c.get("kind") == kind)
                     and (parent is None or c.get("parent") == parent)]
        else:
            found = self._store_call(self.reader(cfg).list, lane=lane, kind=kind, parent=parent)
        langs = cards.languages(cfg)
        ordered = order(found)
        return {"total": len(ordered), "cards": [row(c, langs) for c in ordered[:n]]}

    # -- GET /api/cards/<id> -------------------------------------------------------------------------------------

    def card(self, card_id: str) -> dict:
        try:
            cards.check_id(card_id)
        except CardError as e:
            raise HTTPError(404, str(e)) from e
        cfg = self.cfg()
        store = self.reader(cfg)
        if not store.path(card_id).exists():
            raise HTTPError(404, f"no card {card_id}")
        card = self._store_call(store.get, card_id)
        langs = cards.languages(cfg)
        out = {"card": card, "template": None, "source_block": source_block(card)}
        try:
            t = templates.catalog(self.server.data).get(card.get("kind")) if isinstance(card.get("kind"), str) else None
        except ConfigError as e:  # a bad user template: the card is still served
            t = None
            out["template_error"] = str(e)
        if t is not None:
            view = list(t.data["view"])
            out["template"] = {"kind": t.kind, "title": t.data.get("title"), "origin": t.origin, "view": view}
            # Design 9.4: every full card has the Ask box, so `questions` is added when the template leaves it out.
            out["view"] = view + ([] if "questions" in view else ["questions"])
        else:
            out["view"] = (["source_block"] if out["source_block"] is not None else []) + list(VIEW_WITHOUT_TEMPLATE)
        out["children"] = [row(c, langs) for c in order(self._store_call(store.list, parent=card_id))]
        links = []
        for link in card.get("links") if isinstance(card.get("links"), list) else []:
            if not (isinstance(link, str) and cards.CARD_ID.match(link)) or not store.path(link).exists():
                links.append({"id": link, "missing": True})
                continue
            try:
                links.append(row(store.get(link), langs))
            except CardError as e:  # a damaged linked card does not hide this one
                links.append({"id": link, "missing": True, "error": str(e)})
        out["links"] = links
        out["activity"] = card.get("audit")
        return out

    # -- POST /api/cards/<id>/questions --------------------------------------------------------------------------

    def ask(self, card_id: str):
        origin = self.headers.get("Origin")
        if origin is not None and origin.strip().lower() not in self._own_origins():
            raise HTTPError(403, f"refused: a question can only be posted from this page, not from {origin!r}")
        try:
            cards.check_id(card_id)
        except CardError as e:
            raise HTTPError(404, str(e)) from e
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise HTTPError(415, "a question is posted as application/json: {\"text\": \"...\"}")
        length = self.headers.get("Content-Length")
        if length is None:
            raise HTTPError(411, "a question needs a Content-Length")
        if not DIGITS.match(length.strip()):
            raise HTTPError(400, f"Content-Length is not a number: {length!r}")
        if int(length) > BODY_MAX:
            raise HTTPError(413, f"the body is larger than {BODY_MAX} bytes")
        raw = self.rfile.read(int(length))
        self._body_read = True
        if len(raw) < int(length):
            raise HTTPError(400, "the body is shorter than its Content-Length")
        try:
            body = json.loads(raw.decode("utf-8"), parse_constant=_no_constant)
        except (UnicodeDecodeError, ValueError, RecursionError) as e:
            raise HTTPError(400, f"the body is not JSON: {e}") from e
        if not isinstance(body, dict):
            raise HTTPError(400, "the body must be a JSON object: {\"text\": \"...\"}")
        text = body.get("text")
        if not isinstance(text, str):
            raise HTTPError(400, "text missing" if text is None else f"text must be text, not {type(text).__name__}")
        if not text.strip():
            raise HTTPError(400, "text is blank")
        if len(text) > QUESTION_MAX:
            raise HTTPError(400, f"text is longer than {QUESTION_MAX} characters ({len(text)})")
        try:
            cards.check_unicode(text, "text")
        except CardError as e:
            raise HTTPError(400, str(e)) from e
        store = Store(self.server.data, cfg=self.cfg(), clock=self.server.clock)
        if not store.path(card_id).exists():
            raise HTTPError(404, f"no card {card_id}")
        self._store_call(store.get, card_id)  # a damaged card file: 500, as a GET says
        try:
            card = store.ask_about(card_id, text, by=ASKED_BY)
        except CardError as e:
            if isinstance(e.__cause__, (OSError, sqlite3.Error)):
                raise HTTPError(500, str(e)) from e
            if str(e) == f"no card {card_id}":
                raise HTTPError(404, str(e)) from e
            raise HTTPError(409, str(e)) from e
        return 201, {"question": card["questions"][-1], "card": card}

    # -- GET /api/sources, /api/templates, /api/settings ---------------------------------------------------------

    def _loader(self) -> Loader:
        try:
            return Loader(self.server.data, self.server.plugin_dir)
        except SourceError as e:
            raise HTTPError(500, str(e)) from e

    def _feeds(self) -> Counter:
        found = self._store_call(self.reader(self.cfg()).list)
        return Counter(c["feed"] for c in found if isinstance(c.get("feed"), str))

    def source_list(self) -> list:
        loader = self._loader()
        try:
            loaded = loader.load_all()
        except SourceError as e:
            raise HTTPError(500, str(e)) from e
        feeds = self._feeds()
        return [{**s.summary(), "cards": feeds.get(s.name, 0)} for s in loaded]

    def source(self, name: str) -> dict:
        loader = self._loader()
        if name not in loader.names():
            raise HTTPError(404, f"no source named {name!r}")
        try:
            src = loader.load(name)
        except SourceError as e:
            raise HTTPError(500, str(e)) from e
        return {**src.summary(), "cards": self._feeds().get(src.name, 0), "data": src.data,
                "files": [str(p) for p in src.files]}

    def _catalog(self) -> dict:
        try:
            return templates.catalog(self.server.data)
        except ConfigError as e:
            raise HTTPError(500, str(e)) from e

    def template_list(self) -> list:
        return [templates.summary(t) for t in self._catalog().values()]

    def template(self, kind: str) -> dict:
        cat = self._catalog()
        if kind not in cat:
            raise HTTPError(404, f"no template for kind {kind!r}")
        used = self._store_call(self.reader(self.cfg()).list, kind=kind)
        return {**templates.describe(cat[kind]), "cards": len(used)}

    def settings(self) -> dict:
        return {"config": self.cfg().as_dict(), "data_dir": str(self.server.data), "url": self.server.url,
                "disk": disk_use(self.server.data)}
