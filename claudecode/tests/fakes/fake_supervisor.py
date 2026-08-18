#!/usr/bin/env python3
"""In-process fake of the Home Assistant Supervisor proxy (`http://supervisor`).

Used by broker / runner / notifier / endpoint tests. It checks NOTHING about
auth; it records every request (including the `authorization` header) so tests
assert what was sent. Point code under test at it with
`CLAUDE_JOB_SUPERVISOR_URL = fake.url`.

    with FakeSupervisor() as sup:
        sup.route("GET", "/core/api/config", (200, {"time_zone": "UTC"}))
        sup.route("POST", "/core/api/states/", (500, {"message": "down"}), prefix=True)
        sup.route("GET", "/core/logs", FakeSupervisor.slow(3))
        ...
        sup.requests   # list of {method, path, query, headers, body, json}
        sup.states     # entity_id -> last POSTed payload

Route values are `(status, body[, headers])` where body is a dict/list (JSON),
str (text/plain) or bytes, or a callable `handler(req) -> that tuple`. A handler
may instead take over the socket entirely by accepting `(req, http_handler)`
(two positional params) and returning None — `stream()` does that.
"""
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG_TEXT = "2026-08-18 05:00:00.000 INFO (MainThread) [homeassistant.core] fake log line 1\n" \
           "2026-08-18 05:00:01.000 WARNING (MainThread) [homeassistant.components.x] fake log line 2\n" \
           "2026-08-18 05:00:02.000 ERROR (MainThread) [homeassistant.components.y] fake log line 3\n"

DEFAULT_SERVICES = [
    {"domain": "persistent_notification", "services": {"create": {}, "dismiss": {}}},
    {"domain": "notify", "services": {"notify": {}, "mobile_app_test_phone": {}, "mobile_app_test_tablet": {}}},
    {"domain": "homeassistant", "services": {"restart": {}}},
]

ADDON_SELF_INFO = {"hostname": "d7e97e69-claudecode", "version": "9.9.9-test",
                   "slug": "d7e97e69_claudecode", "state": "started", "name": "Claude Code"}


def _sv(data):
    """Supervisor-style success wrapper."""
    return (200, {"result": "ok", "data": data})


class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeSupervisor"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet
        pass

    # All verbs funnel into one dispatcher.
    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_PUT(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n > 0 else b""

    def _dispatch(self):
        fake = self.server.fake
        parsed = urllib.parse.urlsplit(self.path)
        body = self._read_body()
        try:
            js = json.loads(body) if body else None
        except ValueError:
            js = None
        req = {
            "method": self.command,
            "path": parsed.path,
            "query": parsed.query,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body,
            "json": js,
            "ts": time.time(),
        }
        fake._record(req)
        try:
            spec = fake._resolve(req)
            result = fake._materialize(spec, req, self)
            if result is None:      # handler took over the wire (stream)
                return
            self._send(*result)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # a broken test route should not kill the server thread
            try:
                self._send(500, {"result": "error", "message": f"fake_supervisor handler error: {exc!r}"})
            except Exception:
                self.close_connection = True

    def _send(self, status, body, headers=None):
        headers = dict(headers or {})
        if isinstance(body, (dict, list)) or body is None:
            data = json.dumps(body if body is not None else {}).encode()
            ctype = "application/json"
        elif isinstance(body, str):
            data = body.encode()
            ctype = "text/plain; charset=utf-8"
        else:
            data = bytes(body)
            ctype = "application/octet-stream"
        headers.setdefault("Content-Type", ctype)
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()


class FakeSupervisor:
    """Threaded localhost HTTP server with HA/Supervisor-flavoured defaults."""

    def __init__(self, bind: str = "127.0.0.1"):
        self._bind = bind
        self._lock = threading.Lock()
        self._requests = []
        self.states = {}
        self._routes = []      # list of (method, path, prefix: bool, spec) — newest wins
        self._server = None
        self._thread = None
        self.url = None

    # ---- lifecycle ---------------------------------------------------------
    def start(self):
        self._server = ThreadingHTTPServer((self._bind, 0), _Handler)
        self._server.daemon_threads = True
        self._server.fake = self
        self.url = f"http://{self._bind}:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05},
                                        name="FakeSupervisor", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False

    @property
    def port(self) -> int:
        return int(self.url.rsplit(":", 1)[1])

    # ---- request log -------------------------------------------------------
    @property
    def requests(self) -> list:
        with self._lock:
            return list(self._requests)

    def clear(self):
        with self._lock:
            self._requests.clear()

    def find(self, method=None, path_prefix="") -> list:
        """Recorded requests filtered by method and path prefix."""
        return [r for r in self.requests
                if (method is None or r["method"] == method) and r["path"].startswith(path_prefix)]

    def _record(self, req):
        with self._lock:
            self._requests.append(req)

    # ---- routing -----------------------------------------------------------
    def route(self, method, path_or_prefix, handler_or_tuple, prefix=False):
        """Override a route. Later registrations win over earlier ones and over defaults."""
        with self._lock:
            self._routes.append((method.upper(), path_or_prefix, bool(prefix), handler_or_tuple))

    def unroute(self, method, path_or_prefix):
        with self._lock:
            self._routes = [r for r in self._routes if not (r[0] == method.upper() and r[1] == path_or_prefix)]

    @staticmethod
    def slow(seconds, then=(200, {})):
        """Route helper: sleep, then answer `then`."""
        def handler(req):
            time.sleep(seconds)
            return then
        return handler

    @staticmethod
    def stream(chunk=b"x" * 65536, total=None, content_type="text/plain", chunked=False):
        """Route helper: write `chunk` repeatedly (forever if total is None) until the
        client goes away. No Content-Length unless `total` is given; optionally
        HTTP/1.1 chunked encoding. Never raises out of the handler on BrokenPipe."""
        def handler(req, h):
            h.close_connection = True
            h.send_response(200)
            h.send_header("Content-Type", content_type)
            if total is not None and not chunked:
                h.send_header("Content-Length", str(total))
            if chunked:
                h.send_header("Transfer-Encoding", "chunked")
            h.end_headers()
            sent = 0
            try:
                while total is None or sent < total:
                    piece = chunk if total is None else chunk[: total - sent]
                    if chunked:
                        h.wfile.write(b"%x\r\n" % len(piece) + piece + b"\r\n")
                    else:
                        h.wfile.write(piece)
                    h.wfile.flush()
                    sent += len(piece)
                if chunked:
                    h.wfile.write(b"0\r\n\r\n")
                    h.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return None
        return handler

    def _resolve(self, req):
        method, path = req["method"], req["path"]
        with self._lock:
            routes = list(reversed(self._routes))
        for m, p, is_prefix, spec in routes:
            if m == method and (path.startswith(p) if is_prefix else path == p):
                return spec
        return self._default(req)

    def _materialize(self, spec, req, handler):
        """Turn a route spec into (status, body[, headers]) or None (took over)."""
        if callable(spec):
            try:
                argc = spec.__code__.co_argcount
            except AttributeError:
                argc = 1
            out = spec(req, handler) if argc >= 2 else spec(req)
            return None if out is None else tuple(out)
        return tuple(spec)

    # ---- defaults ----------------------------------------------------------
    def _default(self, req):
        method, path = req["method"], req["path"]
        if method == "GET":
            if path in ("/core/api", "/core/api/"):
                return (200, {"message": "API running."})
            if path == "/core/api/config":
                return (200, {"time_zone": "Europe/Vienna", "version": "2026.8.1", "location_name": "Home"})
            if path == "/core/api/services":
                return (200, DEFAULT_SERVICES)
            if path == "/core/api/states":
                with self._lock:
                    return (200, list(self.states.values()))
            if path.startswith("/core/api/states/"):
                eid = path[len("/core/api/states/"):]
                with self._lock:
                    st = self.states.get(eid)
                return (200, st) if st is not None else (404, {"message": "Entity not found."})
            if path == "/core/api/error_log":
                return (200, LOG_TEXT)
            if path.startswith("/core/api/history/period") or path.startswith("/core/api/logbook"):
                return (200, [])
            if path in ("/core/logs", "/core/logs/latest", "/supervisor/logs", "/supervisor/logs/latest",
                        "/host/logs", "/host/logs/latest"):
                return (200, LOG_TEXT)
            if path == "/addons/self/info":
                return _sv(dict(ADDON_SELF_INFO))
            if path == "/core/info":
                return _sv({"version": "2026.8.1", "state": "running", "boot": True, "arch": "aarch64"})
            if path == "/core/stats":
                return _sv({"cpu_percent": 3.2, "memory_percent": 21.5, "memory_usage": 512000000})
            if path == "/supervisor/info":
                return _sv({"version": "2026.08.0", "healthy": True, "supported": True, "channel": "stable",
                            "arch": "aarch64", "addons": []})
            if path == "/supervisor/stats":
                return _sv({"cpu_percent": 1.0, "memory_percent": 5.0})
            if path == "/resolution/info":
                return _sv({"issues": [], "suggestions": [], "unhealthy": [], "unsupported": [], "checks": []})
            if path == "/os/info":
                return _sv({"version": "16.0", "board": "rpi4-64", "update_available": False})
            if path == "/host/info":
                return _sv({"hostname": "homeassistant", "operating_system": "Home Assistant OS 16.0",
                            "disk_free": 20.5, "disk_total": 58.0})
            if path == "/addons":
                return _sv({"addons": []})
            if path.startswith("/addons/") and path.endswith("/info"):
                slug = path[len("/addons/"):-len("/info")]
                return _sv({"slug": slug, "name": slug, "state": "started", "version": "1.0.0"})
            if path == "/backups":
                return _sv({"backups": [], "days_until_stale": 30})
            if path == "/backups/info":
                return _sv({"backups": [], "days_until_stale": 30})
        if method == "POST":
            if path.startswith("/core/api/states/"):
                eid = path[len("/core/api/states/"):]
                payload = req["json"] if isinstance(req["json"], dict) else {}
                stored = {"entity_id": eid, "state": payload.get("state"),
                          "attributes": payload.get("attributes", {}),
                          "last_changed": "2026-08-18T05:00:00+00:00", "last_updated": "2026-08-18T05:00:00+00:00"}
                with self._lock:
                    created = eid not in self.states
                    self.states[eid] = stored
                return (201 if created else 200, stored)
            if path.startswith("/core/api/services/"):
                return (200, [])
            if path == "/core/api/template":
                return (200, "rendered")
            if path == "/core/check":
                return _sv({})
        return (404, {"result": "error", "message": "not found"})


if __name__ == "__main__":  # manual poke: python3 fake_supervisor.py
    with FakeSupervisor() as s:
        print(s.url, flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
