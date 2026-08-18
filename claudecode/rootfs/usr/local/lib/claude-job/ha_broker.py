#!/usr/bin/env python3
"""ha_broker.py -- per-run Supervisor token broker for Claude Jobs (design §4.5, §8 boundary 4;
breakdown 2i; CONTRACTS A3/A19-A25/A29).

The job's process tree never sees SUPERVISOR_TOKEN. The runner starts this broker on
127.0.0.1:<kernel port>, hands the job a per-run nonce, and points the `ha` CLI wrapper and
hass-mcp at it. The broker swaps nonce -> real token ONLY for requests that match the closed,
read-only ROUTES table below; everything else is 403. It also bounds every proxied call
(read timeout, byte cap, no streaming endpoints) and strips credential-bearing fields from the
responses that carry them: add-on `options` (/addons/<slug>/info, A21) and token-bearing entity
attributes (/core/api/states[/<id>] and /core/api/history/period..., A22). The ONE read route left
unscrubbed by design is POST /core/api/template (a template can read any attribute; accepted
residual, design §4.5 / §8 risk 5). Its REQUEST is pre-checked (CONTRACTS A32): the body must be
JSON `{"template": str[, "variables": {..}]}`; when hass-mcp is importable (production image) the
template must be EXACTLY one of hass-mcp's fixed templates (`app.areas._AREA_TEMPLATE`) -- that
exact allow-list is the real control, since no granted tool lets a job author a template. When
hass-mcp is absent the broker falls back to TEMPLATE_DENY_RE over the decoded template+variables,
which is a HEURISTIC only (Jinja string tricks bypass it) and says so on stderr at startup.

Threat model (the caller is the adversary):
  * the client is a possibly prompt-injected job holding the nonce; it will try any path,
    method, encoding trick or header to reach a mutating Supervisor/Core endpoint;
  * route matching here vs. path parsing upstream (aiohttp decodes %-escapes) must not differ,
    so paths with `%` (except %3A/%2B), `..`, `//`, `\\` or non-printable bytes are refused;
  * the nonce and the token must never be logged, echoed, or forwarded to the wrong side;
  * an allow-listed call must not pin the run: 30 s socket timeout, wall-clock body deadline,
    4 MiB cap, SSE/websocket/follow variants refused;
  * default deny: unknown method/path/shape -> 403, unparseable filtered body -> 502.

CLI:  ha_broker.py [--port-file PATH] [--bind 127.0.0.1]
Env:  SUPERVISOR_TOKEN (required), CLAUDE_JOB_BROKER_NONCE (required, >= 32 chars),
      CLAUDE_JOB_SUPERVISOR_URL (default http://supervisor),
      CLAUDE_JOB_BROKER_READ_TIMEOUT_S (30), CLAUDE_JOB_BROKER_CHECK_TIMEOUT_S (300),
      CLAUDE_JOB_BROKER_MAX_BYTES (4 MiB), CLAUDE_JOB_BROKER_TEMPLATE_ALLOWLIST=off (force the
      heuristic), CLAUDE_JOB_BROKER_TEMPLATE_ALLOWLIST_FILE (JSON list of allowed templates)
      -- test seams only.
Ready signal: exactly one stdout line {"event":"ready","port":N}; SIGTERM/SIGINT -> exit 0.
"""
import argparse
from collections import namedtuple
import hmac
import http.client
import json
import os
import re
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---- constants -----------------------------------------------------------------------------
LOG_PREFIX = "[ha-broker]"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


READ_TIMEOUT_S = _env_int("CLAUDE_JOB_BROKER_READ_TIMEOUT_S", 30)      # socket connect/read timeout
CHECK_TIMEOUT_S = _env_int("CLAUDE_JOB_BROKER_CHECK_TIMEOUT_S", 300)   # POST /core/check only
MAX_BYTES = _env_int("CLAUDE_JOB_BROKER_MAX_BYTES", 4 * 1024 * 1024)   # response cap
TEMPLATE_BODY_CAP = 16384                                              # POST /core/api/template
CHUNK = 64 * 1024
MIN_NONCE_LEN = 32
CLIENT_SOCKET_TIMEOUT_S = 20                                           # idle client connections
LOG_PATH_MAX = 200                                                     # denial log lines truncate the path
HEADER_VALUE_OK = re.compile(r"^[\x20-\x7e]{1,256}$")                   # forwarded header values
SUPERVISOR_URL = (os.environ.get("CLAUDE_JOB_SUPERVISOR_URL") or "http://supervisor").rstrip("/")

FORWARD_REQUEST_HEADERS = ("Accept", "Content-Type", "Range")   # client -> upstream, nothing else
PASS_RESPONSE_HEADERS = ("Content-Type", "Content-Range")       # upstream -> client (+Content-Length)

MSG_UNAUTHORIZED = {"message": "unauthorized"}
MSG_BLOCKED = {"message": "blocked by claude-job broker"}
MSG_TOO_LARGE = {"message": "request body too large"}
MSG_LENGTH = {"message": "content-length required"}
MSG_BAD_LENGTH = {"message": "invalid content-length"}
MSG_UPSTREAM_DOWN = {"message": "upstream unavailable"}
MSG_UPSTREAM_TIMEOUT = {"message": "upstream timeout"}
MSG_UPSTREAM_BAD = {"message": "upstream response not filterable"}
MSG_TEMPLATE_SECRET = {"message": "template references a secret-bearing attribute"}
MSG_TEMPLATE_SHAPE = {"message": "template body must be a JSON object {template: str, variables?: object}"}
MSG_TEMPLATE_NOT_ALLOWED = {"message": "template not in the hass-mcp allow-list"}
MSG_CHECK_BUSY = {"message": "check already running"}

# Whole request-target must be printable ASCII (no spaces/controls -> no header smuggling).
TARGET_OK = re.compile(r"^[\x21-\x7e]+$")
# Refused anywhere in the *path* before route matching. `%` is refused outright except the two
# escapes a timestamp segment may legitimately carry (%3A ':' and %2B '+'); neither decodes to
# anything path-structural, so this matcher and aiohttp upstream agree on segment boundaries.
PATH_DENY = re.compile(r"\.\.|//|\\|%(?!3[Aa]|2[Bb])|(?:^|/)follow(?:/|$)|websocket", re.IGNORECASE)


# ---- response post-processing (pure functions; CONTRACTS A21/A22) ---------------------------
ADDON_INFO_KEEP = frozenset((
    "name", "slug", "hostname", "description", "version", "version_latest", "update_available",
    "state", "boot", "build", "arch", "repository", "url", "detached", "available", "advanced",
    "stage", "startup", "watchdog", "auto_update", "ingress", "ingress_panel", "icon", "logo",
    "changelog",
))

STATE_ATTR_DROP = frozenset(("access_token", "entity_picture", "token", "still_image_url", "stream_source"))
STATE_ATTR_DROP_RE = re.compile(r"token|password|passwd|secret|api[_-]?key|credential", re.IGNORECASE)


# A32: request-side control for the one unscrubbed read route (POST /core/api/template).
TEMPLATE_DENY_RE = re.compile(
    r"access_token|entity_picture|stream_source|still_image_url|password|passwd|secret|api[_-]?key"
    r"|credential|\btoken\b", re.IGNORECASE)


def load_template_allowlist():
    """-> (frozenset_or_None, note). None = heuristic mode. Production: hass-mcp's fixed templates."""
    if (os.environ.get("CLAUDE_JOB_BROKER_TEMPLATE_ALLOWLIST") or "").lower() == "off":
        return None, "template allow-list disabled by env; using the deny-regex heuristic"
    path = os.environ.get("CLAUDE_JOB_BROKER_TEMPLATE_ALLOWLIST_FILE")
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                items = json.load(f)
            if not isinstance(items, list) or not all(isinstance(t, str) for t in items):
                raise ValueError("not a JSON list of strings")
            return frozenset(items), None
        except (OSError, ValueError) as e:
            return frozenset(), f"template allow-list file unusable ({type(e).__name__}); allowing no templates"
    try:
        from app.areas import _AREA_TEMPLATE       # hass-mcp, pip-installed in the image
    except Exception:                               # ImportError, or its config module objecting
        return None, "hass-mcp not importable; template route guarded by the deny-regex heuristic only"
    return frozenset((_AREA_TEMPLATE,)), None


ALLOWED_TEMPLATES, TEMPLATE_MODE_NOTE = load_template_allowlist()
_MODULE_DEFAULT = object()


def template_precheck(body: bytes, allowed=_MODULE_DEFAULT):
    """-> None when the template request may be forwarded, else (status, message_obj).
    `allowed`: frozenset = exact allow-list mode; None = deny-regex heuristic; default = module's."""
    if allowed is _MODULE_DEFAULT:
        allowed = ALLOWED_TEMPLATES
    try:
        obj = json.loads((body or b"").decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return 400, MSG_TEMPLATE_SHAPE
    if (not isinstance(obj, dict) or not isinstance(obj.get("template"), str)
            or not set(obj) <= {"template", "variables"}
            or not isinstance(obj.get("variables", {}), dict)):
        return 400, MSG_TEMPLATE_SHAPE
    template, variables = obj["template"], obj.get("variables") or {}
    if allowed is not None:
        if template not in allowed or variables:
            return 403, MSG_TEMPLATE_NOT_ALLOWED
        return None
    if TEMPLATE_DENY_RE.search(template) or TEMPLATE_DENY_RE.search(json.dumps(variables, ensure_ascii=False)):
        return 403, MSG_TEMPLATE_SECRET
    return None


def filter_addon_info(obj):
    """A21: keep only ADDON_INFO_KEEP inside the Supervisor envelope's `data` (drops `options`,
    `schema`, `network`, `privileged`, ...). Unexpected shapes fail closed (ValueError -> 502)."""
    if not isinstance(obj, dict):
        raise ValueError("addon info: not an object")
    data = obj.get("data")
    if isinstance(data, dict):
        obj["data"] = {k: v for k, v in data.items() if k in ADDON_INFO_KEEP}
    elif data is not None:
        raise ValueError("addon info: data is not an object")
    return obj


def _scrub_one_state(state):
    if isinstance(state, dict) and isinstance(state.get("attributes"), dict):
        attrs = state["attributes"]
        for key in list(attrs):
            k = str(key).lower()
            if k in STATE_ATTR_DROP or k.startswith("entity_picture") or STATE_ATTR_DROP_RE.search(k):
                del attrs[key]
    return state


def scrub_states(obj):
    """A22: delete token-bearing attributes from one state object, a list of them (/states), or a
    list of lists of them (/history/period)."""
    if isinstance(obj, list):
        for item in obj:
            if isinstance(item, list):
                for st in item:
                    _scrub_one_state(st)
            else:
                _scrub_one_state(item)
        return obj
    if isinstance(obj, dict):
        return _scrub_one_state(obj)
    raise ValueError("states: not an object or list")


# ---- the route table (design §4.5 + ha-facts §A.2; matched against the URL path only) --------
# ROUTES rows: (method, compiled_regex, body_cap, upstream_timeout_s, postprocess_fn_or_None)
Route = namedtuple("Route", "method regex body_cap timeout postprocess")


def _r(method, pattern, body_cap=0, timeout=READ_TIMEOUT_S, postprocess=None):
    return Route(method, re.compile(pattern), body_cap, timeout, postprocess)


ROUTES = [
    # Core REST via the Supervisor proxy
    _r("GET", r"^/core/api/?$"),
    _r("GET", r"^/core/api/config$"),
    _r("GET", r"^/core/api/states$", postprocess=scrub_states),
    _r("GET", r"^/core/api/states/[a-z0-9_]+\.[a-z0-9_]+$", postprocess=scrub_states),
    _r("GET", r"^/core/api/error_log$"),
    _r("GET", r"^/core/api/history/period(?:/[^/]+)?$", postprocess=scrub_states),
    _r("GET", r"^/core/api/logbook(?:/[^/]+)?$"),
    _r("GET", r"^/core/api/services$"),
    _r("POST", r"^/core/api/template$", body_cap=TEMPLATE_BODY_CAP),
    # Supervisor info/stats
    _r("GET", r"^/(?:core|supervisor|os|host)/info$"),
    _r("GET", r"^/(?:core|supervisor)/stats$"),
    _r("GET", r"^/resolution/info$"),
    _r("GET", r"^/addons$"),
    _r("GET", r"^/addons/[a-z0-9_.-]+/info$", postprocess=filter_addon_info),
    _r("GET", r"^/backups$"),
    _r("GET", r"^/backups/info$"),
    _r("GET", r"^/backups/[a-z0-9_.-]{1,64}/info$"),
    # Logs: finite forms only (plain, /latest, /boots/<id>); `follow` anywhere in a log path -> 403
    _r("GET", r"^/(?:core|supervisor|host)/logs(?:/latest|/boots/[A-Za-z0-9_-]{1,64})?$"),
    # The one safe POST besides template: config check (slow -> long timeout)
    _r("POST", r"^/core/check$", timeout=CHECK_TIMEOUT_S),
]

LOG_PATH_RE = re.compile(r"^/(?:core|supervisor|host)/logs(?:/|$)")
TEMPLATE_ROUTE = next(r for r in ROUTES if r.regex.pattern == r"^/core/api/template$")
CHECK_ROUTE = next(r for r in ROUTES if r.regex.pattern == r"^/core/check$")
CHECK_SLOT = threading.BoundedSemaphore(1)      # F6: at most one config check in flight per broker


def match_route(method: str, path: str):
    """Return the Route for (method, path) or None. Path pre-checks are the caller's job."""
    for r in ROUTES:
        if r.method == method and r.regex.match(path):
            return r
    return None


def is_log_path(path: str) -> bool:
    return bool(LOG_PATH_RE.match(path))


def path_is_sane(path: str) -> bool:
    if not path.startswith("/") or PATH_DENY.search(path):
        return False
    if is_log_path(path) and "follow" in path.lower():
        return False
    return True


# ---- upstream client -----------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx from upstream is passed through, never followed (the token must not travel)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def log(msg: str) -> None:
    try:
        sys.stderr.write(f"{LOG_PREFIX} {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


# ---- HTTP handler --------------------------------------------------------------------------
_REPLIED = object()     # sentinel: "an error response has already been written"


class BrokerHandler(BaseHTTPRequestHandler):
    server_version = "claude-job-broker"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = CLIENT_SOCKET_TIMEOUT_S           # StreamRequestHandler applies it to the client socket
    # set by main()
    nonce = b""
    token = ""

    # -- plumbing --
    def log_message(self, fmt, *args):      # silence the stdlib access log (2xx must be quiet)
        pass

    def log_error(self, fmt, *args):
        pass

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _deny_method(self):
        path, _ = self._split_target()
        if not self._authorized():
            return self._reply_json(401, MSG_UNAUTHORIZED, path)
        return self._reply_json(403, MSG_BLOCKED, path)

    do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = do_TRACE = do_CONNECT = _deny_method

    # -- helpers --
    def _authorized(self) -> bool:
        presented = None
        auth = self.headers.get("Authorization")
        if auth:
            parts = auth.split()
            if len(parts) == 2 and parts[0].lower() == "bearer":
                presented = parts[1]
        if presented is None:
            presented = self.headers.get("X-Supervisor-Token")
        if not presented:
            return False
        return hmac.compare_digest(presented.encode("utf-8", "replace"), self.nonce)

    def _reply_json(self, status: int, obj, log_path):
        body = json.dumps(obj).encode()
        if status >= 400:
            shown = "<bad-target>" if log_path is None else log_path
            if len(shown) > LOG_PATH_MAX:
                shown = shown[:LOG_PATH_MAX] + "...(truncated)"
            log(f"{status} {self.command} {shown}")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        self.close_connection = True
        return _REPLIED

    # -- main path: auth -> path checks -> table -> body -> upstream -> relay --
    def _handle(self):
        self.close_connection = True
        path, query = self._split_target()

        if not self._authorized():                  # 1. an unauthenticated caller learns nothing
            return self._reply_json(401, MSG_UNAUTHORIZED, path)
        if path is None or not path_is_sane(path):  # 2. encoding/traversal tricks
            return self._reply_json(403, MSG_BLOCKED, path)
        route = match_route(self.command, path)     # 3. the closed table
        if route is None:
            return self._reply_json(403, MSG_BLOCKED, path)
        body = self._read_client_body(route, path)  # 4. exact, capped body (replies itself on error)
        if body is _REPLIED:
            return
        if route is TEMPLATE_ROUTE:                 # 4b. A32: no secret-bearing attribute names
            verdict = template_precheck(body)
            if verdict is not None:
                return self._reply_json(verdict[0], verdict[1], path)
        if route is CHECK_ROUTE and not CHECK_SLOT.acquire(blocking=False):   # 4c. F6
            return self._reply_json(429, MSG_CHECK_BUSY, path)
        try:
            resp = self._open_upstream(route, path, query, body)   # 5. real token goes on here only
            if resp is _REPLIED:
                return
            try:
                self._relay(route, path, resp)      # 6. filtered or capped stream back
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
        finally:
            if route is CHECK_ROUTE:
                CHECK_SLOT.release()

    def _split_target(self):
        """-> (path, query); path is None when the request-target has spaces/controls/non-ASCII."""
        target = self.path or ""
        if not TARGET_OK.match(target):
            return None, ""
        parts = urllib.parse.urlsplit(target)
        if parts.scheme or parts.netloc:            # absolute-form request-target: refuse
            return target, ""
        return parts.path, parts.query

    def _read_client_body(self, route, path):
        """Read exactly Content-Length bytes (POST requires it; chunked refused; > cap -> 413 unread)."""
        if self.headers.get("Transfer-Encoding"):
            return self._reply_json(411, MSG_LENGTH, path)
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            if self.command == "POST":
                return self._reply_json(411, MSG_LENGTH, path)
            return None
        try:
            length = int(raw_len)
            if length < 0:
                raise ValueError
        except ValueError:
            return self._reply_json(400, MSG_BAD_LENGTH, path)
        if length > route.body_cap:
            return self._reply_json(413, MSG_TOO_LARGE, path)
        data = self.rfile.read(length) if length else b""
        return data if self.command == "POST" else None

    def _open_upstream(self, route, path, query, body):
        """Build the Supervisor request (3 forwarded headers + the real token) and open it."""
        headers = {}
        for name in FORWARD_REQUEST_HEADERS:
            val = self.headers.get(name)
            if val and HEADER_VALUE_OK.match(val):          # F5: printable ASCII, bounded, else dropped
                headers[name] = val
        if is_log_path(path) and "Accept" not in headers:
            headers["Accept"] = "text/plain"
        headers["Authorization"] = "Bearer " + self.token
        url = SUPERVISOR_URL + path + ("?" + query if query else "")
        req = urllib.request.Request(url, data=body, method=self.command, headers=headers)
        try:
            try:
                return _OPENER.open(req, timeout=route.timeout)
            except urllib.error.HTTPError as e:      # non-2xx is still a response: pass it through
                return e
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                return self._reply_json(504, MSG_UPSTREAM_TIMEOUT, path)
            return self._reply_json(502, MSG_UPSTREAM_DOWN, path)
        except (socket.timeout, TimeoutError):
            return self._reply_json(504, MSG_UPSTREAM_TIMEOUT, path)
        except (http.client.HTTPException, OSError, ValueError):
            return self._reply_json(502, MSG_UPSTREAM_DOWN, path)

    def _relay(self, route, path, resp):
        status = resp.status if getattr(resp, "status", None) else resp.getcode()
        ctype = resp.headers.get("Content-Type", "") or ""
        if ctype.strip().lower().startswith("text/event-stream"):
            return self._reply_json(403, MSG_BLOCKED, path)

        read1 = getattr(resp, "read1", None) or resp.read       # returns as soon as any bytes arrive
        deadline = time.monotonic() + route.timeout             # wall-clock bound for the whole body

        # Filtered routes: whole body -> parse -> filter -> re-serialize (2xx only)
        if route.postprocess is not None and 200 <= status < 300:
            try:
                buf = bytearray()
                while len(buf) <= MAX_BYTES:
                    if time.monotonic() > deadline:
                        return self._reply_json(504, MSG_UPSTREAM_TIMEOUT, path)
                    chunk = read1(min(CHUNK, MAX_BYTES + 1 - len(buf)))
                    if not chunk:
                        break
                    buf += chunk
                if len(buf) > MAX_BYTES:
                    raise ValueError("too large to filter")
                obj = route.postprocess(json.loads(bytes(buf).decode("utf-8")))
                out = json.dumps(obj).encode()
            except (socket.timeout, TimeoutError):
                return self._reply_json(504, MSG_UPSTREAM_TIMEOUT, path)
            except (ValueError, UnicodeDecodeError, http.client.HTTPException, OSError):
                return self._reply_json(502, MSG_UPSTREAM_BAD, path)
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(out)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return

        # Streamed pass-through, capped in bytes and wall-clock
        if status >= 500:
            log(f"{status} {self.command} {path} (upstream)")
        try:
            self.send_response(status)
            for name in PASS_RESPONSE_HEADERS:
                val = resp.headers.get(name)
                if val:
                    self.send_header(name, val)
            clen = resp.headers.get("Content-Length")
            if clen and clen.isdigit() and int(clen) <= MAX_BYTES:
                self.send_header("Content-Length", clen)
            self.send_header("Connection", "close")
            self.end_headers()
            sent = 0
            while sent < MAX_BYTES:
                if time.monotonic() > deadline:
                    log(f"body deadline hit after {sent} bytes {self.command} {path}")
                    break
                chunk = read1(min(CHUNK, MAX_BYTES - sent))
                if not chunk:
                    break
                self.wfile.write(chunk)
                sent += len(chunk)
            else:
                if resp.read(1):
                    log(f"truncated at {sent} bytes {self.command} {path}")
            self.wfile.flush()
        except (socket.timeout, TimeoutError):
            log(f"upstream timeout mid-body {self.command} {path}")
        except (BrokenPipeError, ConnectionResetError, http.client.HTTPException, OSError):
            pass


class BrokerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def handle_error(self, request, client_address):   # never dump tracebacks (could echo headers)
        log("handler error: " + type(sys.exc_info()[1]).__name__)


# ---- main ----------------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="claude-job Supervisor token broker")
    ap.add_argument("--port-file", help="write the bound port number here")
    ap.add_argument("--bind", default="127.0.0.1")
    args = ap.parse_args(argv)

    token = os.environ.get("SUPERVISOR_TOKEN") or ""
    nonce = os.environ.get("CLAUDE_JOB_BROKER_NONCE") or ""
    if not token:
        log("error: SUPERVISOR_TOKEN is not set")
        return 2
    if len(nonce) < MIN_NONCE_LEN:
        log(f"error: CLAUDE_JOB_BROKER_NONCE must be at least {MIN_NONCE_LEN} characters")
        return 2

    BrokerHandler.token = token
    BrokerHandler.nonce = nonce.encode("utf-8")
    if TEMPLATE_MODE_NOTE:
        log(TEMPLATE_MODE_NOTE)
    try:
        server = BrokerServer((args.bind, 0), BrokerHandler)
    except OSError as e:
        log(f"error: cannot bind {args.bind}: {e}")
        return 2
    port = server.server_address[1]

    if args.port_file:
        tmp = f"{args.port_file}.tmp.{os.getpid()}"
        try:
            with open(tmp, "w") as f:
                f.write(f"{port}\n")
            os.replace(tmp, args.port_file)
        except OSError as e:
            log(f"error: cannot write port file: {e}")
            server.server_close()
            return 2

    stop = threading.Event()

    def _on_signal(signum, frame):
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1},
                              name="ha-broker", daemon=True)
    thread.start()

    try:
        sys.stdout.write(json.dumps({"event": "ready", "port": port}, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    except (BrokenPipeError, OSError):
        pass

    try:
        while not stop.is_set():
            stop.wait(0.2)
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
