# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""HTTP: the page, the JSON API, and the access rules.

Access follows forgectrl's panel (auth.c): the Host header must be an
address literal or localhost (no DNS names - the rebinding vehicle), a
cross-site Sec-Fetch-Site is refused, an Origin must itself be a literal,
and every state-changing call needs the bearer token that is generated on
first start, stored 0600 under the data directory, and embedded in the
page. Read-only calls need the origin checks only.

Routes
  GET  /                    the page
  GET  /state               campaign + per-test state + running run.
                            Carries an ETag; If-None-Match on an
                            unchanged state gets a 304, which is what an
                            idle page polls for.
  GET  /catalog             test definitions (title, steps, covers...)
  GET  /bench               bench tool listing
  GET  /result?test&ts      one full result record (log, evidence)
  GET  /log                 the raw JSONL
  GET  /journal             the daemon's journal (daemon.log tail)
  GET  /export/acceptance.json | .md   the last export
  POST /start {test, ack_live, ignore_requires}   start an acceptance test
  POST /batch {group, ack_live, ignore_requires}  run everything a queue
                            still owes, in prerequisite order
  POST /batch/stop          cancel what is still queued
  POST /bench/start {tool, args, ack_live}
  POST /answer {prompt_id, value}
  POST /abort
  POST /invalidate {reason}
  POST /reset {reason}
  POST /export              build + save the artifact, returns it
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import urllib.parse
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import artifact as _artifact
from . import page as _page
from .log import data_dir

JOURNAL_TAIL_BYTES = 256 << 10


def journal_tail(path=None, max_bytes=JOURNAL_TAIL_BYTES):
    """The last max_bytes of the daemon's journal (daemon.log, written by
    the init script from the daemon's stderr), or a note that there is
    none yet."""
    path = path or os.path.join(data_dir(), "daemon.log")
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()
            return f.read()
    except OSError:
        return b"(no journal yet: %s)\n" % path.encode()

TOKEN_HEX = 32
_LITERAL_RX = re.compile(r"^[0-9.]+$")


def load_token(path=None):
    path = path or os.path.join(data_dir(), "token")
    try:
        with open(path, "r", encoding="utf-8") as f:
            tok = f.read().strip()
        if len(tok) == TOKEN_HEX and all(c in "0123456789abcdef" for c in tok):
            return tok
    except OSError:
        pass
    tok = secrets.token_hex(TOKEN_HEX // 2)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(tok + "\n")
    return tok


def host_is_literal(h):
    if not h:
        return False
    if h.startswith("["):
        return True
    host = h.split(":", 1)[0]
    if host == "localhost":
        return True
    return bool(_LITERAL_RX.match(host))


def origin_ok(headers):
    if not host_is_literal(headers.get("Host")):
        return False
    sfs = headers.get("Sec-Fetch-Site")
    if sfs and sfs not in ("same-origin", "none"):
        return False
    origin = headers.get("Origin")
    if origin and origin != "null":
        p = urllib.parse.urlsplit(origin)
        if not host_is_literal(p.netloc):
            return False
    return True


class App:
    """What the handler needs: the runner, the token, the export dir."""

    def __init__(self, runner, token, export_dir=None):
        self.runner = runner
        self.token = token
        self.export_dir = export_dir or os.path.join(data_dir(), "export")

    def token_ok(self, given):
        return bool(given) and hmac.compare_digest(given, self.token)


class Handler(BaseHTTPRequestHandler):
    server_version = "forgetest"
    # Keep-alive: the page polls, so a handshake and a fresh thread per
    # request is pure overhead. Idle connections are dropped after the
    # timeout rather than holding a thread forever.
    protocol_version = "HTTP/1.1"
    timeout = 30
    app = None  # set on the server class

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):
        if os.environ.get("FORGETEST_HTTP_LOG"):
            sys.stderr.write("forgetest: %s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, sort_keys=True).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.responded = True
        self.send_response(status)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_304(self, etag):
        self.responded = True
        self.send_response(304)
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _deny(self, status, msg):
        # One response per request: on a kept-alive connection a second
        # one would desynchronize the stream.
        if getattr(self, "responded", False):
            return
        # A refused POST still has its body in the socket. Leaving it
        # there would make the next read on this connection take the
        # body for a request line, so the connection ends here.
        # send_header sets close_connection from this.
        extra = None if getattr(self, "body_read", True) else {"Connection": "close"}
        self._send(status, {"error": msg}, extra=extra)

    def _read_ok(self):
        if not origin_ok(self.headers):
            self._deny(403, "request origin refused")
            return False
        return True

    def _write_ok(self, query):
        if not origin_ok(self.headers):
            self._deny(403, "request origin refused")
            return False
        tok = self.headers.get("X-ForgeFIRM-Token") or (query.get("token") or [None])[0]
        if not self.app.token_ok(tok):
            self._deny(403, "authentication required")
            return False
        return True

    def _body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            self.body_read = True
            return {}
        if n > 1 << 20:
            raise ValueError("body too large")
        raw = self.rfile.read(n)
        self.body_read = True
        ctype = self.headers.get("Content-Type", "")
        if "json" in ctype:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        # form-encoded fallback
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8")).items()}

    # -- GET ---------------------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        path = url.path
        query = urllib.parse.parse_qs(url.query)
        r = self.app.runner
        self.responded = False
        if not self._read_ok():
            return
        try:
            if path == "/":
                self._send(200, _page.render(self.app.token), "text/html")
            elif path == "/state":
                # Conditional: an idle page polls an unchanged state, and
                # a 304 spares both ends the payload and the re-render.
                state, _ = r.state()
                body = json.dumps(state, sort_keys=True).encode("utf-8")
                etag = '"%s"' % hashlib.sha256(body).hexdigest()[:32]
                if self.headers.get("If-None-Match") == etag:
                    self._send_304(etag)
                else:
                    self._send(200, body, extra={"ETag": etag})
            elif path == "/catalog":
                self._send(200, {"tests": [t.describe() for t in r.tests()],
                                 "catalog_hash": r.catalog_hash})
            elif path == "/bench":
                b = r.bench
                self._send(200, {"tools": b.listing() if b else [], "tool_dir": b.tool_dir() if b else None})
            elif path == "/result":
                test = (query.get("test") or [""])[0]
                ts = (query.get("ts") or [""])[0]
                rec = None
                for x in reversed(r.log.read()):
                    if x.get("t") == "result" and x.get("test") == test and (not ts or x.get("ts") == ts):
                        rec = x
                        break
                if rec is None:
                    self._deny(404, "no such result")
                else:
                    self._send(200, rec)
            elif path == "/log":
                self._send(200, r.log.raw(), "text/plain")
            elif path == "/journal":
                self._send(200, journal_tail(), "text/plain")
            elif path in ("/export/acceptance.json", "/export/acceptance.md"):
                fn = os.path.join(self.app.export_dir, os.path.basename(path))
                if not os.path.exists(fn):
                    self._deny(404, "nothing exported yet")
                    return
                with open(fn, "rb") as f:
                    data = f.read()
                ctype = "application/json" if path.endswith(".json") else "text/markdown"
                self._send(200, data, ctype, {"Content-Disposition": "attachment; filename=%s"
                                              % os.path.basename(path)})
            else:
                self._deny(404, "not found")
        except Exception as e:  # noqa: BLE001
            self._deny(500, "%s: %s" % (type(e).__name__, e))

    # -- POST -----------------------------------------------------------------
    def do_POST(self):
        url = urllib.parse.urlsplit(self.path)
        path = url.path
        query = urllib.parse.parse_qs(url.query)
        r = self.app.runner
        self.responded = False
        self.body_read = False
        if not self._write_ok(query):
            return
        try:
            body = self._body_json()
        except ValueError as e:
            self._deny(400, "bad body: %s" % e)
            return
        try:
            if path == "/start":
                ok, msg = r.start_test(str(body.get("test", "")), ack_live=bool(body.get("ack_live")),
                                       ignore_requires=bool(body.get("ignore_requires")))
                self._send(200 if ok else 409, {"ok": ok, "message": msg})
            elif path == "/batch":
                ok, msg, order = r.start_batch(str(body.get("group", "")),
                                               ack_live=bool(body.get("ack_live")),
                                               ignore_requires=bool(body.get("ignore_requires")))
                self._send(200 if ok else 409, {"ok": ok, "message": msg, "order": order})
            elif path == "/batch/stop":
                ok, msg = r.stop_batch()
                self._send(200 if ok else 409, {"ok": ok, "message": msg})
            elif path == "/bench/start":
                args = body.get("args") or {}
                if not isinstance(args, dict):
                    self._deny(400, "args must be an object")
                    return
                ok, msg = r.start_bench(str(body.get("tool", "")), args, ack_live=bool(body.get("ack_live")))
                self._send(200 if ok else 409, {"ok": ok, "message": msg})
            elif path == "/answer":
                ok, msg = r.answer(str(body.get("prompt_id", "")), str(body.get("value", "")))
                self._send(200 if ok else 409, {"ok": ok, "message": msg})
            elif path == "/abort":
                ok, msg = r.abort()
                self._send(200 if ok else 409, {"ok": ok, "message": msg})
            elif path == "/invalidate":
                ok, msg = r.invalidate(str(body.get("reason", "")))
                self._send(200 if ok else 400, {"ok": ok, "message": msg})
            elif path == "/reset":
                ok, msg = r.reset(str(body.get("reason", "")))
                self._send(200 if ok else 409, {"ok": ok, "message": msg})
            elif path == "/export":
                art = r.export()
                os.makedirs(self.app.export_dir, exist_ok=True)
                with open(os.path.join(self.app.export_dir, "acceptance.json"), "w", encoding="utf-8") as f:
                    f.write(_artifact.to_json(art))
                with open(os.path.join(self.app.export_dir, "acceptance.md"), "w", encoding="utf-8") as f:
                    f.write(_artifact.to_markdown(art))
                self._send(200, {"ok": True, "authorized": art["authorized"], "sha256": art["sha256"],
                                 "counts": art["counts"]})
            else:
                self._deny(404, "not found")
        except Exception as e:  # noqa: BLE001
            self._deny(500, "%s: %s" % (type(e).__name__, e))


def make_server(app, host="::", port=8090):
    handler = type("ForgetestHandler", (Handler,), {"app": app})
    ThreadingHTTPServer.allow_reuse_address = True
    # An IPv6 bind address (the default "::") gives one dual-stack socket
    # that serves IPv4 clients too; a dotted address keeps the IPv4 family.
    server_cls = ThreadingHTTPServer
    if ":" in host:
        server_cls = type("ForgetestServer6", (ThreadingHTTPServer,),
                          {"address_family": socket.AF_INET6})
    srv = server_cls((host, port), handler)
    srv.daemon_threads = True
    return srv
