"""U2: the token broker (`ha_broker.py`) and the `ha` CLI wrapper (breakdown 2i/2j, §3 U2;
CONTRACTS A3, A19-A25, A29). The broker is the security boundary, so most tests assert what
did NOT reach the fake Supervisor."""
import http.client
import importlib.util
import json
import os
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from testlib import (BROKER, HA_WRAPPER, SUPERVISOR_TOKEN, ScratchRoot, run_cli, wait_for)
from fakes.fake_supervisor import ADDON_SELF_INFO, FakeSupervisor

NONCE = "n0nce-" + "ab" * 16          # 38 chars
WRONG_NONCE = "n0nce-" + "cd" * 16


def load_broker_module():
    spec = importlib.util.spec_from_file_location("ha_broker_under_test", str(BROKER))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class BrokerProc:
    """Start ha_broker.py as the runner would and read its ready line."""

    def __init__(self, supervisor_url, extra_env=None, nonce=NONCE, token=SUPERVISOR_TOKEN, port_file=None):
        self.tmp = tempfile.mkdtemp(prefix="broker-")
        self.port_file = port_file or os.path.join(self.tmp, "port")
        self.stderr_path = os.path.join(self.tmp, "stderr")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONDONTWRITEBYTECODE": "1"}
        if token is not None:
            env["SUPERVISOR_TOKEN"] = token
        if nonce is not None:
            env["CLAUDE_JOB_BROKER_NONCE"] = nonce
        if supervisor_url:
            env["CLAUDE_JOB_SUPERVISOR_URL"] = supervisor_url
        env.update(extra_env or {})
        self._stderr = open(self.stderr_path, "wb")
        self.proc = subprocess.Popen([sys.executable, str(BROKER), "--port-file", self.port_file],
                                     env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=self._stderr)
        self.port = None
        self.ready_line = None

    def wait_ready(self, timeout=5.0):
        r, _, _ = select.select([self.proc.stdout], [], [], timeout)
        if not r:
            raise AssertionError("broker printed no ready line within %ss (rc=%s, stderr=%s)"
                                 % (timeout, self.proc.poll(), self.stderr()))
        line = self.proc.stdout.readline().decode()
        self.ready_line = line
        obj = json.loads(line)
        assert obj["event"] == "ready", line
        self.port = int(obj["port"])
        return self

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def stderr(self) -> str:
        try:
            self._stderr.flush()
            with open(self.stderr_path, "rb") as f:
                return f.read().decode(errors="replace")
        except OSError:
            return ""

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(3)
        try:
            self.proc.stdout.close()
        except Exception:
            pass
        self._stderr.close()


def http_call(base, method, path, body=None, headers=None, auth="bearer", nonce=NONCE, timeout=10):
    """-> (status, headers(dict lower), body bytes). auth: 'bearer' | 'x' | None."""
    h = dict(headers or {})
    if auth == "bearer":
        h["Authorization"] = "Bearer " + nonce
    elif auth == "x":
        h["X-Supervisor-Token"] = nonce
    req = urllib.request.Request(base + path, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


def raw_request(port, request_bytes, timeout=10):
    """Send bytes verbatim (for paths urllib would mangle) and return (status, body)."""
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.sendall(request_bytes)
        resp = http.client.HTTPResponse(s, method="GET")
        resp.begin()
        return resp.status, resp.read()
    finally:
        s.close()


def raw_get(port, path, nonce=NONCE):
    return raw_request(port, (f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                              f"Authorization: Bearer {nonce}\r\nConnection: close\r\n\r\n").encode())


# =============================================================================================
class TestModuleSurface(unittest.TestCase):
    """The route table and filters are importable data/pure functions."""

    @classmethod
    def setUpClass(cls):
        cls.m = load_broker_module()

    def test_routes_table_shape_and_no_mutations(self):
        m = self.m
        self.assertIsInstance(m.ROUTES, list)
        self.assertGreaterEqual(len(m.ROUTES), 18)
        for method, regex, cap, timeout, post in m.ROUTES:
            self.assertIn(method, ("GET", "POST"))
            self.assertTrue(hasattr(regex, "match"))
            self.assertIsInstance(cap, int)
            self.assertGreater(timeout, 0)
        posts = sorted(r.regex.pattern for r in m.ROUTES if r.method == "POST")
        self.assertEqual(posts, [r"^/core/api/template$", r"^/core/check$"])
        self.assertEqual(m.match_route("POST", "/core/api/template").body_cap, 16384)
        self.assertEqual(m.match_route("POST", "/core/check").timeout, m.CHECK_TIMEOUT_S)
        self.assertEqual(m.match_route("GET", "/core/info").timeout, m.READ_TIMEOUT_S)
        for bad in ("/core/restart", "/host/reboot", "/backups/new/full", "/addons/x/start",
                    "/core/api/services/light/turn_on", "/supervisor/update", "/os/update"):
            self.assertIsNone(m.match_route("POST", bad), bad)
            self.assertIsNone(m.match_route("GET", bad), bad)

    def test_path_sanity(self):
        ok = self.m.path_is_sane
        for p in ("/core/info", "/core/api/states/sensor.follow_me", "/core/logs/boots/abc",
                  "/core/api/history/period/2026-08-18T05%3A00%3A00%2B02%3A00"):
            self.assertTrue(ok(p), p)
        for p in ("/core/api/states/../config", "//core/info", "/core/api/%2e%2e/config",
                  "/addons/%2E%2E%2Fcore%2Fapi/info", "/core/logs/boots/%2Ffollow", "/core/logs/follow",
                  "/core/logs/boots/abc/follow", "/core/logs/boots/xfollowx", "/core/websocket",
                  "/core/api/websocket", "core/info", "/core/api/states/a%00b", "/x\\y", "/core/info%3F"):
            self.assertFalse(ok(p), p)

    def test_filter_addon_info(self):
        f = self.m.filter_addon_info
        out = f({"result": "ok", "data": {"name": "SSH", "slug": "core_ssh", "version": "1.0", "state": "started",
                                          "options": {"password": "hunter2"}, "schema": [], "network": {},
                                          "privileged": ["NET_ADMIN"], "hostname": "core-ssh"}})
        self.assertEqual(out["result"], "ok")
        self.assertEqual(set(out["data"]), {"name", "slug", "version", "state", "hostname"})
        self.assertNotIn("hunter2", json.dumps(out))
        with self.assertRaises(ValueError):
            f(["not", "an", "object"])
        with self.assertRaises(ValueError):
            f({"data": "string"})
        self.assertEqual(f({"result": "error", "message": "x"}), {"result": "error", "message": "x"})

    def test_scrub_states(self):
        s = self.m.scrub_states
        cam = {"entity_id": "camera.front", "state": "idle",
               "attributes": {"access_token": "abc", "entity_picture": "/api/camera_proxy/camera.front?token=abc",
                              "entity_picture_local": "/x?token=abc", "friendly_name": "Front", "Stream_Source": "rtsp://u:p@h",
                              "my_api-key": "k", "wifi_Password": "p", "client_secret": "s", "brightness": 3}}
        out = s([cam, {"entity_id": "sensor.x", "state": "1", "attributes": {"unit_of_measurement": "W"}}, "junk"])
        self.assertEqual(out[0]["attributes"], {"friendly_name": "Front", "brightness": 3})
        self.assertEqual(out[1]["attributes"], {"unit_of_measurement": "W"})
        single = s({"entity_id": "sensor.y", "state": "2", "attributes": {"refresh_token": "t", "a": 1}})
        self.assertEqual(single["attributes"], {"a": 1})
        hist = s([[{"entity_id": "camera.c", "state": "idle", "attributes": {"access_token": "x", "friendly_name": "C"}},
                   {"entity_id": "camera.c", "state": "recording", "attributes": {"token": "y"}}], []])
        self.assertEqual(hist, [[{"entity_id": "camera.c", "state": "idle", "attributes": {"friendly_name": "C"}},
                                 {"entity_id": "camera.c", "state": "recording", "attributes": {}}], []])
        self.assertIs(self.m.match_route("GET", "/core/api/history/period/2026-08-18T00:00:00Z").postprocess, s)
        self.assertIsNone(self.m.match_route("GET", "/core/api/logbook").postprocess)
        self.assertIsNone(self.m.match_route("POST", "/core/api/template").postprocess)
        with self.assertRaises(ValueError):
            s("string")


    def test_template_precheck_heuristic_mode(self):
        pre = self.m.template_precheck
        SECRET = (403, {"message": "template references a secret-bearing attribute"})
        SHAPE = 400

        def body(tpl, **kw):
            return json.dumps(dict(template=tpl, **kw)).encode()
        self.assertIsNone(pre(body("{{ states('sensor.x') }} tokens_used {{ state_attr('sensor.x','unit') }}"), allowed=None))
        self.assertIsNone(pre(body("{{ area_name(e) }}", variables={"e": "light.k"}), allowed=None))
        for bad in ("{{ state_attr('camera.door','access_token') }}", "{{ states.camera.door.attributes.entity_picture }}",
                    "{{ x.Stream_Source }}", "still_image_url", "{{ s.attributes['wifi_password'] }}", "passwd",
                    "client_SECRET", "{{ a['api-key'] }}", "{{ a.apikey }}", "credentials", "{{ a.token }}"):
            self.assertEqual(pre(body(bad), allowed=None), SECRET, bad)
        # JSON \u escapes are decoded before the regex runs (was a bypass on raw bytes)
        escaped = b'{"template": "{{ state_attr(\'camera.door\', \'acc\\u0065ss\\u005ftoken\') }}"}'
        self.assertIn("\\u005f", escaped.decode())
        self.assertEqual(pre(escaped, allowed=None), SECRET)
        # variables are scanned too
        self.assertEqual(pre(body("{{ state_attr(e, k) }}", variables={"e": "camera.door", "k": "access_token"}), allowed=None), SECRET)
        # shape: must be an object with a string template and optional dict variables, nothing else
        for shapeless in (b"", b"[]", b'"{{ 1 }}"', b"{}", b'{"template": 1}', b'{"template": "x", "variables": []}',
                          b'{"template": "x", "extra": 1}', b"not json", b"\xff\xfe{}"):
            self.assertEqual(pre(shapeless, allowed=None)[0], SHAPE, shapeless)

    def test_template_precheck_allowlist_mode(self):
        pre = self.m.template_precheck
        area = "{%- for s in states -%}{{ s.entity_id ~ '\x1f' ~ (area_name(s.entity_id) or '') }}\n{%- endfor -%}"
        allowed = frozenset((area,))
        NOT_ALLOWED = (403, {"message": "template not in the hass-mcp allow-list"})
        self.assertIsNone(pre(json.dumps({"template": area}).encode(), allowed=allowed))
        self.assertEqual(pre(json.dumps({"template": area + " "}).encode(), allowed=allowed), NOT_ALLOWED)
        self.assertEqual(pre(json.dumps({"template": "{{ 1 }}"}).encode(), allowed=allowed), NOT_ALLOWED)
        self.assertEqual(pre(json.dumps({"template": area, "variables": {"a": 1}}).encode(), allowed=allowed), NOT_ALLOWED)
        self.assertEqual(pre(b'{"template": 1}', allowed=allowed)[0], 400)
        self.assertEqual(pre(json.dumps({"template": area}).encode(), allowed=frozenset()), NOT_ALLOWED)

    def test_template_allowlist_loader_seams(self):
        load = self.m.load_template_allowlist
        saved = {k: os.environ.pop(k, None) for k in ("CLAUDE_JOB_BROKER_TEMPLATE_ALLOWLIST", "CLAUDE_JOB_BROKER_TEMPLATE_ALLOWLIST_FILE")}
        try:
            os.environ["CLAUDE_JOB_BROKER_TEMPLATE_ALLOWLIST"] = "off"
            allowed, note = load()
            self.assertIsNone(allowed)
            self.assertIn("heuristic", note)
            del os.environ["CLAUDE_JOB_BROKER_TEMPLATE_ALLOWLIST"]
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                json.dump(["T1", "T2"], f)
            os.environ["CLAUDE_JOB_BROKER_TEMPLATE_ALLOWLIST_FILE"] = f.name
            self.assertEqual(load(), (frozenset(("T1", "T2")), None))
            with open(f.name, "w") as fh:
                fh.write("{not json")
            allowed, note = load()
            self.assertEqual(allowed, frozenset())        # unusable file fails CLOSED
            self.assertIn("unusable", note)
            os.unlink(f.name)
            del os.environ["CLAUDE_JOB_BROKER_TEMPLATE_ALLOWLIST_FILE"]
            allowed, note = load()                          # production default: import hass-mcp
            try:
                from app.areas import _AREA_TEMPLATE        # noqa: F401
                self.assertEqual((allowed, note), (frozenset((_AREA_TEMPLATE,)), None))
            except ImportError:
                self.assertEqual(allowed, frozenset())        # fail closed: no hass-mcp -> no templates
                self.assertIn("hass-mcp not importable", note)
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v
                else:
                    os.environ.pop(k, None)

    def test_hardening_constants(self):
        self.assertEqual(self.m.BrokerHandler.timeout, 20)
        self.assertEqual(self.m.LOG_PATH_MAX, 200)
        self.assertTrue(self.m.HEADER_VALUE_OK.match("entries=:-49:"))
        self.assertIsNone(self.m.HEADER_VALUE_OK.match("a" * 257))
        self.assertIsNone(self.m.HEADER_VALUE_OK.match("x\x00y"))
        self.assertIsNotNone(self.m.match_route("GET", "/backups/c1a07617/info"))
        self.assertIsNone(self.m.match_route("GET", "/backups/c1a07617/download"))
        self.assertIsNone(self.m.match_route("POST", "/backups/c1a07617/restore/full"))


# =============================================================================================
class TestStartupAndShutdown(unittest.TestCase):
    def test_missing_token_exits_2(self):
        b = BrokerProc("http://127.0.0.1:9", token=None)
        try:
            self.assertEqual(b.proc.wait(5), 2)
            self.assertIn("SUPERVISOR_TOKEN", b.stderr())
        finally:
            b.stop()

    def test_short_nonce_exits_2(self):
        b = BrokerProc("http://127.0.0.1:9", nonce="tooshort")
        try:
            self.assertEqual(b.proc.wait(5), 2)
            self.assertIn("CLAUDE_JOB_BROKER_NONCE", b.stderr())
            self.assertNotIn("tooshort", b.stderr())
        finally:
            b.stop()

    def test_ready_line_port_file_and_sigterm(self):
        with FakeSupervisor() as sup:
            b = BrokerProc(sup.url)
            try:
                t0 = time.monotonic()
                b.wait_ready(5)
                self.assertLess(time.monotonic() - t0, 5)
                self.assertEqual(json.loads(b.ready_line), {"event": "ready", "port": b.port})
                self.assertEqual(b.ready_line.strip(), '{"event":"ready","port":%d}' % b.port)
                with open(b.port_file) as f:
                    self.assertEqual(f.read().strip(), str(b.port))
                st, _, body = http_call(b.url, "GET", "/core/api/config")
                self.assertEqual(st, 200)
                self.assertEqual(json.loads(body)["time_zone"], "Europe/Vienna")
                t0 = time.monotonic()
                b.proc.send_signal(signal.SIGTERM)
                rc = b.proc.wait(2)
                self.assertLess(time.monotonic() - t0, 1.0)
                self.assertEqual(rc, 0)
            finally:
                b.stop()


# =============================================================================================
class TestProxy(unittest.TestCase):
    """One broker + one fake Supervisor for the whole class (fast); tests clear the request log."""

    @classmethod
    def setUpClass(cls):
        cls.sup = FakeSupervisor().start()
        # heuristic template mode via the explicit seam: hass-mcp is not importable on a dev box,
        # and the production fallback for that is "allow no templates" (fail closed)
        cls.b = BrokerProc(cls.sup.url, extra_env={"CLAUDE_JOB_BROKER_TEMPLATE_ALLOWLIST": "off"}).wait_ready()

    @classmethod
    def tearDownClass(cls):
        cls.b.stop()
        cls.sup.stop()

    def setUp(self):
        self.sup.clear()
        self.assertIsNone(self.b.proc.poll(), "broker died: " + self.b.stderr())

    def call(self, method, path, **kw):
        return http_call(self.b.url, method, path, **kw)

    def assert_no_secret_leak_upstream(self):
        for r in self.sup.requests:
            self.assertEqual(r["headers"].get("authorization"), "Bearer " + SUPERVISOR_TOKEN, r["path"])
            for k, v in r["headers"].items():
                self.assertNotIn(NONCE, v, (r["path"], k))
            self.assertNotIn("x-supervisor-token", r["headers"])

    # ---- auth ----
    def test_auth_matrix(self):
        st, _, body = self.call("GET", "/core/info", auth=None)
        self.assertEqual((st, json.loads(body)), (401, {"message": "unauthorized"}))
        st, _, _ = self.call("GET", "/core/info", nonce=WRONG_NONCE)
        self.assertEqual(st, 401)
        st, _, _ = self.call("GET", "/core/info", auth="x", nonce=WRONG_NONCE)
        self.assertEqual(st, 401)
        st, _, _ = self.call("GET", "/core/info", headers={"Authorization": "Basic " + NONCE}, auth=None)
        self.assertEqual(st, 401)
        # unauthenticated + forbidden path is still 401 (table not revealed) and mutations never 2xx
        st, _, _ = self.call("POST", "/core/restart", body=b"", auth=None)
        self.assertEqual(st, 401)
        self.assertEqual(self.sup.requests, [])
        st, _, body = self.call("GET", "/core/info")
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["data"]["version"], "2026.8.1")
        st, _, _ = self.call("GET", "/core/info", auth="x")
        self.assertEqual(st, 200)
        self.assertEqual(len(self.sup.requests), 2)
        self.assert_no_secret_leak_upstream()
        self.assertNotIn(NONCE, self.b.stderr())
        self.assertNotIn(SUPERVISOR_TOKEN, self.b.stderr())

    # ---- the allow-list forwards with the real token ----
    def test_every_allowed_route_forwards_with_real_token(self):
        log_before = self.b.stderr()
        self.sup.states["sensor.power"] = {"entity_id": "sensor.power", "state": "5", "attributes": {}}
        self.sup.route("GET", "/core/logs/boots/abc", (200, "boot log\n"))
        self.sup.route("GET", "/backups/c1a07617/info", (200, {"result": "ok", "data": {"slug": "c1a07617", "protected": True}}))
        cases = [
            ("GET", "/core/api/"), ("GET", "/core/api"), ("GET", "/core/api/config"), ("GET", "/core/api/states"),
            ("GET", "/core/api/states/sensor.power"), ("GET", "/core/api/error_log"),
            ("GET", "/core/api/history/period"), ("GET", "/core/api/history/period/2026-08-18T00:00:00+00:00"),
            ("GET", "/core/api/logbook"), ("GET", "/core/api/logbook/2026-08-18T00:00:00Z"),
            ("GET", "/core/api/services"), ("POST", "/core/api/template"),
            ("GET", "/core/info"), ("GET", "/supervisor/info"), ("GET", "/os/info"), ("GET", "/host/info"),
            ("GET", "/core/stats"), ("GET", "/supervisor/stats"), ("GET", "/resolution/info"),
            ("GET", "/addons"), ("GET", "/addons/core_ssh/info"), ("GET", "/addons/self/info"),
            ("GET", "/backups"), ("GET", "/backups/info"), ("GET", "/backups/c1a07617/info"),
            ("GET", "/core/logs"), ("GET", "/core/logs/latest"), ("GET", "/core/logs/boots/abc"),
            ("GET", "/supervisor/logs"), ("GET", "/host/logs"), ("GET", "/host/logs/latest"),
            ("POST", "/core/check"),
        ]
        for method, path in cases:
            body = b'{"template": "{{ 1 + 1 }}"}' if path.endswith("template") else (b"" if method == "POST" else None)
            hdrs = {"Content-Type": "application/json"} if body else {}
            st, h, out = self.call(method, path, body=body, headers=hdrs)
            self.assertEqual(st, 200, (method, path, out[:200]))
        got = [(r["method"], r["path"]) for r in self.sup.requests]
        self.assertEqual(got, cases)
        self.assert_no_secret_leak_upstream()
        tpl = self.sup.find("POST", "/core/api/template")[0]
        self.assertEqual(tpl["json"], {"template": "{{ 1 + 1 }}"})
        self.assertEqual(tpl["headers"].get("content-type"), "application/json")
        self.assertEqual(self.b.stderr(), log_before, "2xx must not log")
        self.sup.unroute("GET", "/core/logs/boots/abc")
        self.sup.unroute("GET", "/backups/c1a07617/info")

    def test_query_string_passthrough(self):
        st, _, _ = self.call("GET", "/core/api/history/period?filter_entity_id=sensor.x&minimal_response")
        self.assertEqual(st, 200)
        r = self.sup.requests[0]
        self.assertEqual((r["path"], r["query"]), ("/core/api/history/period", "filter_entity_id=sensor.x&minimal_response"))

    def test_upstream_error_status_passes_through(self):
        st, h, body = self.call("GET", "/core/api/states/sensor.nope")
        self.assertEqual(st, 404)
        self.assertEqual(json.loads(body), {"message": "Entity not found."})
        self.sup.route("POST", "/core/check", (400, {"result": "error", "message": "Invalid config"}))
        try:
            st, _, body = self.call("POST", "/core/check", body=b"")
            self.assertEqual((st, json.loads(body)["message"]), (400, "Invalid config"))
        finally:
            self.sup.unroute("POST", "/core/check")

    # ---- denials ----
    def test_mutations_and_unknown_paths_are_403_without_upstream_call(self):
        denied = [
            ("POST", "/core/api/services/light/turn_on"), ("POST", "/core/restart"), ("POST", "/core/stop"),
            ("POST", "/host/reboot"), ("POST", "/backups/new/full"), ("POST", "/addons/core_ssh/restart"),
            ("POST", "/backups/c1a07617/restore/full"), ("GET", "/backups/c1a07617/download"), ("DELETE", "/backups/c1a07617"),
            ("POST", "/core/api/states/sensor.x"), ("GET", "/core/api/states/sensor.x/extra"),
            ("GET", "/addons/core_ssh/options"), ("GET", "/addons/core_ssh/logs"), ("GET", "/supervisor/options"),
            ("GET", "/core/api/stream"), ("GET", "/core/websocket"), ("GET", "/core/api/websocket"),
            ("GET", "/core/logs/follow"), ("GET", "/core/logs/boots/abc/follow"), ("GET", "/core/logs/latest/follow"),
            ("GET", "/host/logs/identifiers/x"), ("GET", "/host/logs/boots"), ("GET", "/dns/logs"),
            ("GET", "/core/api/config/extra"), ("GET", "/core/api/events"), ("GET", "/core/api/camera_proxy/camera.x"),
            ("POST", "/core/api/template/x"), ("GET", "/core/api/template"), ("GET", "/core/check"),
            ("GET", "/info"), ("GET", "/auth"), ("GET", "/"), ("GET", "/addons/Core_SSH/info"),
        ]
        for method, path in denied:
            st, _, body = self.call(method, path, body=(b"{}" if method == "POST" else None))
            self.assertEqual(st, 403, (method, path))
            self.assertEqual(json.loads(body), {"message": "blocked by claude-job broker"})
        self.assertEqual(self.sup.requests, [], "nothing denied may reach the Supervisor")
        # F7: the echoed path in a denial line is bounded
        st, _ = raw_get(self.b.port, "/core/api/services/x/" + "A" * 5000)
        self.assertEqual(st, 403)
        err = self.b.stderr()
        longest = max(len(line) for line in err.splitlines())
        self.assertLess(longest, 300)
        self.assertIn("...(truncated)", err)
        self.assertIn("[ha-broker] 403 POST /core/api/services/light/turn_on", err)
        self.assertIn("[ha-broker] 403 GET /core/logs/follow", err)

    def test_traversal_and_percent_encoding_are_403(self):
        port = self.b.port
        for path in ("/core/api/states/../config", "/core//info", "/core/api/states//sensor.x", "/core/api/%2e%2e/config",
                     "/core/api/%2E%2E/config", "/addons/%2E%2E%2Fcore%2Fapi%2Fservices%2Flight%2Fturn_on/info",
                     "/core/logs/boots/..%2f..%2fsupervisor%2frestart", "/core/api/states/sensor%2Ex",
                     "/core/logs%2Ffollow", "/core/api/config%3Fx=1", "/core/api/config%23", "/core/info/%252e%252e/x"):
            st, body = raw_get(port, path)
            self.assertEqual(st, 403, path)
        # absolute-form request target
        st, _ = raw_get(port, f"http://127.0.0.1:{port}/core/info")
        self.assertEqual(st, 403)
        self.assertEqual(self.sup.requests, [])
        # a leading `//` is collapsed to `/` by the stdlib server itself before we see it, and the
        # normalized path is what goes upstream -- no matcher/upstream differential either way
        st, _ = raw_get(port, "//core/info")
        self.assertIn(st, (200, 403))
        self.assertTrue(all(r["path"] == "/core/info" for r in self.sup.requests))

    def test_other_methods_are_403(self):
        for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
            st, _, _ = self.call(method, "/core/api/config", body=(b"{}" if method in ("PUT", "PATCH") else None))
            self.assertEqual(st, 403, method)
        st, _, _ = self.call("PUT", "/core/api/config", body=b"{}", auth=None)
        self.assertEqual(st, 401)
        self.assertEqual(self.sup.requests, [])

    def test_template_body_cap(self):
        ok_body = b'{"template": "' + b"a" * (16384 - 16) + b'"}'
        self.assertEqual(len(ok_body), 16384)
        st, _, out = self.call("POST", "/core/api/template", body=ok_body, headers={"Content-Type": "application/json"})
        self.assertEqual((st, out), (200, b"rendered"))
        self.assertEqual(len(self.sup.requests[0]["body"]), 16384)
        self.sup.clear()
        big = ok_body[:-2] + b'a"}'
        self.assertEqual(len(big), 16385)
        st, _, out = self.call("POST", "/core/api/template", body=big, headers={"Content-Type": "application/json"})
        self.assertEqual(st, 413)
        # a GET carrying a body is refused too (cap 0)
        st, _ = raw_request(self.b.port, (f"GET /core/api/config HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {NONCE}\r\n"
                                          f"Content-Length: 2\r\nConnection: close\r\n\r\nhi").encode())
        self.assertEqual(st, 413)
        # POST without Content-Length -> 411; chunked on any route but /core/check -> 411; garbage length -> 400
        st, _ = raw_request(self.b.port, (f"POST /core/check HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {NONCE}\r\n"
                                          f"Connection: close\r\n\r\n").encode())
        self.assertEqual(st, 411)
        st, _ = raw_request(self.b.port, (f"POST /core/api/template HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {NONCE}\r\n"
                                          f"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n3\r\nabc\r\n0\r\n\r\n").encode())
        self.assertEqual(st, 411)
        st, _ = raw_request(self.b.port, (f"POST /core/check HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {NONCE}\r\n"
                                          f"Content-Length: nope\r\nConnection: close\r\n\r\n").encode())
        self.assertEqual(st, 400)
        self.assertEqual(self.sup.requests, [])

    def test_chunked_core_check_is_proxied(self):
        """The real `ha` CLI (go-resty) POSTs /core/check chunked with an empty body and no
        Content-Length; the broker decodes it and forwards with an explicit Content-Length."""
        st, body = raw_request(self.b.port, (
            f"POST /core/check HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {NONCE}\r\n"
            f"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n0\r\n\r\n").encode())
        self.assertEqual(st, 200, body)
        r = self.sup.requests[-1]
        self.assertEqual((r["method"], r["path"], r["body"]), ("POST", "/core/check", b""))
        self.assertNotIn("transfer-encoding", r["headers"])
        self.assertEqual(r["headers"].get("content-length"), "0")
        self.sup.clear()
        # a chunk that exceeds the route's body cap (0 for /core/check) -> 413, nothing forwarded
        st, _ = raw_request(self.b.port, (
            f"POST /core/check HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {NONCE}\r\n"
            f"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n3\r\nabc\r\n0\r\n\r\n").encode())
        self.assertEqual(st, 413)
        # malformed chunk framing -> 400
        st, _ = raw_request(self.b.port, (
            f"POST /core/check HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {NONCE}\r\n"
            f"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\nzz\r\n\r\n").encode())
        self.assertEqual(st, 400)
        # chunked + Content-Length together (smuggling shape) -> 411
        st, _ = raw_request(self.b.port, (
            f"POST /core/check HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {NONCE}\r\n"
            f"Transfer-Encoding: chunked\r\nContent-Length: 0\r\nConnection: close\r\n\r\n0\r\n\r\n").encode())
        self.assertEqual(st, 411)
        self.assertEqual(self.sup.requests, [])

    # ---- A32: template request pre-check (heuristic mode via the env seam set in setUpClass) ----
    def test_template_body_precheck(self):
        hdr = {"Content-Type": "application/json"}
        ok = json.dumps({"template": "{{ states('sensor.x') }} / {{ state_attr('sensor.x','unit_of_measurement') }}"}).encode()
        st, _, out = self.call("POST", "/core/api/template", body=ok, headers=hdr)
        self.assertEqual((st, out), (200, b"rendered"))
        self.assertEqual(len(self.sup.requests), 1)
        self.assertEqual(self.sup.requests[0]["body"], ok, "body forwarded byte-for-byte")
        self.sup.clear()
        for tpl in ("{{ state_attr('camera.door','access_token') }}", "{{ states.camera.door.attributes.entity_picture }}",
                    "{{ states.sensor.x.attributes.api_key }}", "{{ x | selectattr('token') }}", "{{ a.PASSWORD }}"):
            st, _, out = self.call("POST", "/core/api/template", body=json.dumps({"template": tpl}).encode(), headers=hdr)
            self.assertEqual((st, json.loads(out)), (403, {"message": "template references a secret-bearing attribute"}), tpl)
        escaped = b'{"template": "{{ state_attr(\'camera.door\', \'acc\\u0065ss\\u005ftoken\') }}"}'
        st, _, out = self.call("POST", "/core/api/template", body=escaped, headers=hdr)
        self.assertEqual(st, 403)
        for shapeless in (b'"{{ 1 }}"', b"[1]", b'{"template": 2}', b"{{ 1 }}", b"\xff\xfe" + ok):
            st, _, out = self.call("POST", "/core/api/template", body=shapeless, headers=hdr)
            self.assertEqual(st, 400, shapeless)
        self.assertEqual(self.sup.requests, [], "refused templates never reach upstream")
        self.assertIn("[ha-broker] 403 POST /core/api/template", self.b.stderr())

    # ---- A21: add-on info credential oracle ----
    def test_addon_info_is_filtered(self):
        secretful = {"name": "SSH", "slug": "core_ssh", "state": "started", "version": "9.0", "version_latest": "9.1",
                     "update_available": True, "options": {"password": "hunter2", "authorized_keys": ["k"]},
                     "schema": [{"name": "password"}], "network": {"22/tcp": 2222}, "privileged": ["NET_ADMIN"],
                     "host_network": True, "hassio_role": "manager", "translations": {}}
        self.sup.route("GET", "/addons/core_ssh/info", (200, {"result": "ok", "data": secretful}))
        self.sup.route("GET", "/addons/self/info", (200, {"result": "ok", "data": dict(ADDON_SELF_INFO, options={"api_key": "sk-x"})}))
        listing = {"result": "ok", "data": {"addons": [{"slug": "core_ssh", "name": "SSH", "state": "started"}]}}
        self.sup.route("GET", "/addons", (200, listing))
        try:
            st, h, body = self.call("GET", "/addons/core_ssh/info")
            self.assertEqual(st, 200)
            obj = json.loads(body)
            self.assertEqual(obj["result"], "ok")
            self.assertEqual(obj["data"], {"name": "SSH", "slug": "core_ssh", "state": "started", "version": "9.0",
                                           "version_latest": "9.1", "update_available": True})
            self.assertNotIn(b"hunter2", body)
            self.assertEqual(h["content-type"], "application/json")
            self.assertEqual(int(h["content-length"]), len(body))
            st, _, body = self.call("GET", "/addons/self/info")
            self.assertEqual(st, 200)
            obj = json.loads(body)
            self.assertNotIn("options", obj["data"])
            self.assertNotIn(b"sk-x", body)
            self.assertEqual(obj["data"]["hostname"], "d7e97e69-claudecode")
            self.assertEqual(obj["data"]["version"], "9.9.9-test")
            st, _, body = self.call("GET", "/addons")
            self.assertEqual((st, json.loads(body)), (200, listing))
            # unparseable upstream on a filtered route -> 502, body not leaked
            self.sup.route("GET", "/addons/core_ssh/info", (200, "password: hunter2 (not json)"))
            st, _, body = self.call("GET", "/addons/core_ssh/info")
            self.assertEqual(st, 502)
            self.assertNotIn(b"hunter2", body)
            # upstream error envelope passes through unfiltered (non-2xx)
            self.sup.route("GET", "/addons/nope/info", (400, {"result": "error", "message": "Addon nope does not exist"}))
            st, _, body = self.call("GET", "/addons/nope/info")
            self.assertEqual((st, json.loads(body)["message"]), (400, "Addon nope does not exist"))
        finally:
            for p in ("/addons/core_ssh/info", "/addons/self/info", "/addons", "/addons/nope/info"):
                self.sup.unroute("GET", p)

    # ---- A22: token-bearing entity attributes ----
    def test_states_are_scrubbed(self):
        cam = {"entity_id": "camera.front", "state": "idle", "last_changed": "x", "last_updated": "x",
               "attributes": {"access_token": "deadbeef" * 8, "friendly_name": "Front door",
                              "entity_picture": "/api/camera_proxy/camera.front?token=" + "deadbeef" * 8,
                              "supported_features": 2}}
        sensor = {"entity_id": "sensor.weather", "state": "sunny", "last_changed": "x", "last_updated": "x",
                  "attributes": {"api_key": "sk-live-123", "temperature": 21.5, "attribution": "by X"}}
        self.sup.states.clear()
        self.sup.states["camera.front"] = cam
        self.sup.states["sensor.weather"] = sensor
        try:
            st, h, body = self.call("GET", "/core/api/states")
            self.assertEqual(st, 200)
            self.assertNotIn(b"deadbeef", body)
            self.assertNotIn(b"sk-live-123", body)
            states = {s["entity_id"]: s for s in json.loads(body)}
            self.assertEqual(states["camera.front"]["attributes"], {"friendly_name": "Front door", "supported_features": 2})
            self.assertEqual(states["sensor.weather"]["attributes"], {"temperature": 21.5, "attribution": "by X"})
            self.assertEqual(states["camera.front"]["state"], "idle")
            self.assertEqual(int(h["content-length"]), len(body))
            st, _, body = self.call("GET", "/core/api/states/camera.front")
            self.assertEqual(st, 200)
            one = json.loads(body)
            self.assertEqual(one["attributes"], {"friendly_name": "Front door", "supported_features": 2})
            self.assertNotIn(b"deadbeef", body)
            # history carries full state objects too (list of lists)
            self.sup.route("GET", "/core/api/history/period", (200, [[cam, dict(cam, state="recording")], [sensor]]), prefix=True)
            st, _, body = self.call("GET", "/core/api/history/period/2026-08-18T00:00:00+00:00?filter_entity_id=camera.front,sensor.weather")
            self.assertEqual(st, 200)
            hist = json.loads(body)
            self.assertEqual([len(x) for x in hist], [2, 1])
            self.assertEqual(hist[0][1]["state"], "recording")
            self.assertEqual(hist[0][0]["attributes"], {"friendly_name": "Front door", "supported_features": 2})
            self.assertEqual(hist[1][0]["attributes"], {"temperature": 21.5, "attribution": "by X"})
            self.assertNotIn(b"deadbeef", body)
            self.assertNotIn(b"sk-live-123", body)
            self.sup.route("GET", "/core/api/history/period", (200, "token=deadbeef nope"), prefix=True)
            st, _, body = self.call("GET", "/core/api/history/period")
            self.assertEqual(st, 502)
            self.assertNotIn(b"deadbeef", body)
            # non-JSON upstream on the states route -> 502
            self.sup.route("GET", "/core/api/states", (200, "access_token=deadbeef"))
            st, _, body = self.call("GET", "/core/api/states")
            self.assertEqual(st, 502)
            self.assertNotIn(b"deadbeef", body)
        finally:
            self.sup.unroute("GET", "/core/api/states")
            self.sup.unroute("GET", "/core/api/history/period")
            self.sup.states.clear()

    # ---- streaming shapes ----
    def test_sse_upstream_is_refused(self):
        self.sup.route("GET", "/core/api/error_log", (200, b"data: hello\n\n", {"Content-Type": "text/event-stream"}))
        try:
            st, _, body = self.call("GET", "/core/api/error_log")
            self.assertEqual(st, 403)
            self.assertNotIn(b"hello", body)
        finally:
            self.sup.unroute("GET", "/core/api/error_log")

    # ---- log routes: Accept default + Range forwarding ----
    def test_log_headers(self):
        st, h, body = self.call("GET", "/core/logs", headers={"Range": "entries=:-49:", "Accept": "text/x-log"})
        self.assertEqual(st, 200)
        self.assertIn(b"fake log line 3", body)
        r = self.sup.requests[-1]
        self.assertEqual((r["headers"].get("range"), r["headers"].get("accept")), ("entries=:-49:", "text/x-log"))
        self.assertTrue(h["content-type"].startswith("text/plain"))
        self.sup.clear()
        # urllib adds no Accept by itself when we pass none -> broker injects text/plain
        st, _ = raw_get(self.b.port, "/host/logs/boots/b1f9")
        self.assertEqual(st, 404)          # fake has no default for boots -> its 404 passes through
        r = self.sup.requests[-1]
        self.assertEqual((r["path"], r["headers"].get("accept")), ("/host/logs/boots/b1f9", "text/plain"))
        self.assertNotIn("range", r["headers"])
        # F5: header values with controls / obs-fold / oversize are dropped, not relayed
        self.sup.clear()
        st, _ = raw_request(self.b.port, (f"GET /core/logs HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {NONCE}\r\n"
                                          f"Range: entries=:-9:\x00X\r\nAccept: {'a' * 300}\r\nConnection: close\r\n\r\n").encode())
        self.assertEqual(st, 200)
        r = self.sup.requests[-1]
        self.assertNotIn("range", r["headers"])
        self.assertEqual(r["headers"].get("accept"), "text/plain")
        self.sup.clear()
        st, _ = raw_request(self.b.port, (f"GET /core/logs HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {NONCE}\r\n"
                                          f"Range: entries=:-9:\r\n X-Injected: 1\r\nConnection: close\r\n\r\n").encode())
        self.assertIn(st, (200, 400))
        for r in self.sup.requests:
            self.assertNotIn("x-injected", r["headers"])
            self.assertNotIn("X-Injected", r["headers"].get("range", ""))
        # non-log route: no Accept injected, unrelated client headers dropped
        self.sup.clear()
        st, _, _ = self.call("GET", "/core/info", headers={"X-Evil": "1", "Cookie": "a=b", "X-Hassio-Key": "k"})
        self.assertEqual(st, 200)
        r = self.sup.requests[-1]
        for k in ("x-evil", "cookie", "x-hassio-key", "range"):
            self.assertNotIn(k, r["headers"])


# =============================================================================================
class TestTimeoutsCapsAndUpstreamFailures(unittest.TestCase):
    def test_read_timeout_504_and_check_route_long_timeout(self):
        with FakeSupervisor() as sup:
            sup.route("GET", "/core/info", FakeSupervisor.slow(2, then=(200, {"result": "ok", "data": {}})))
            sup.route("POST", "/core/check", FakeSupervisor.slow(2, then=(200, {"result": "ok", "data": {}})))
            b = BrokerProc(sup.url, extra_env={"CLAUDE_JOB_BROKER_READ_TIMEOUT_S": "1",
                                               "CLAUDE_JOB_BROKER_CHECK_TIMEOUT_S": "5"}).wait_ready()
            try:
                t0 = time.monotonic()
                st, _, body = http_call(b.url, "GET", "/core/info")
                dt = time.monotonic() - t0
                self.assertEqual((st, json.loads(body)), (504, {"message": "upstream timeout"}))
                self.assertLess(dt, 1.9)
                self.assertIn("[ha-broker] 504 GET /core/info", b.stderr())
                t0 = time.monotonic()
                st, _, body = http_call(b.url, "POST", "/core/check", body=b"")
                self.assertEqual(st, 200, body)
                self.assertGreaterEqual(time.monotonic() - t0, 1.9)
            finally:
                b.stop()

    def test_response_cap_truncates_and_closes(self):
        cap = 64 * 1024
        with FakeSupervisor() as sup:
            sup.route("GET", "/core/api/error_log", FakeSupervisor.stream(chunk=b"y" * 8192, total=None))
            sup.route("GET", "/core/logs", FakeSupervisor.stream(chunk=b"z" * 5000, total=None, chunked=True))
            sup.route("GET", "/supervisor/logs", FakeSupervisor.stream(chunk=b"w" * 1000, total=cap + 5000))
            b = BrokerProc(sup.url, extra_env={"CLAUDE_JOB_BROKER_MAX_BYTES": str(cap),
                                               "CLAUDE_JOB_BROKER_READ_TIMEOUT_S": "5"}).wait_ready()
            try:
                for path in ("/core/api/error_log", "/core/logs", "/supervisor/logs"):
                    s = socket.create_connection(("127.0.0.1", b.port), timeout=10)
                    s.sendall((f"GET {path} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {NONCE}\r\n"
                               f"Connection: close\r\n\r\n").encode())
                    t0 = time.monotonic()
                    data = b""
                    while True:
                        chunk = s.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                        self.assertLess(len(data), cap + 4096, "broker must stop at the cap")
                    s.close()
                    self.assertLess(time.monotonic() - t0, 8, path)
                    head, _, body = data.partition(b"\r\n\r\n")
                    self.assertTrue(head.startswith(b"HTTP/1.1 200"), (path, head[:80]))
                    self.assertLessEqual(len(body), cap, path)
                    self.assertGreater(len(body), cap // 2, path)
                self.assertTrue(wait_for(lambda: b.stderr().count("truncated at %d bytes" % cap) >= 3, timeout=3), b.stderr())
                # broker still healthy afterwards
                st, _, _ = http_call(b.url, "GET", "/core/api/config")
                self.assertEqual(st, 200)
            finally:
                b.stop()

    def test_slow_drip_upstream_is_cut_at_the_route_deadline(self):
        def drip(req, h):                       # 1 byte every 0.25 s for ~10 s, valid framing
            h.close_connection = True
            h.send_response(200)
            h.send_header("Content-Type", "application/json" if "states" in req["path"] else "text/plain")
            h.end_headers()
            try:
                h.wfile.write(b"[")
                h.wfile.flush()
                for _ in range(40):
                    time.sleep(0.25)
                    h.wfile.write(b" ")
                    h.wfile.flush()
                h.wfile.write(b"]")
                h.wfile.flush()
            except OSError:
                pass
            return None
        with FakeSupervisor() as sup:
            sup.route("GET", "/core/api/error_log", drip)
            sup.route("GET", "/core/api/states", drip)
            b = BrokerProc(sup.url, extra_env={"CLAUDE_JOB_BROKER_READ_TIMEOUT_S": "2"}).wait_ready()
            try:
                t0 = time.monotonic()
                s_ = socket.create_connection(("127.0.0.1", b.port), timeout=10)
                s_.sendall((f"GET /core/api/error_log HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {NONCE}\r\n"
                            f"Connection: close\r\n\r\n").encode())
                data = b""
                while True:
                    chunk = s_.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                s_.close()
                dt = time.monotonic() - t0
                self.assertTrue(data.startswith(b"HTTP/1.1 200"), data[:60])
                self.assertLess(dt, 3.5, "streamed body must be cut near the 2 s deadline, took %.1f" % dt)
                self.assertIn("body deadline hit", b.stderr())
                t0 = time.monotonic()
                st, _, body = http_call(b.url, "GET", "/core/api/states")
                dt = time.monotonic() - t0
                self.assertEqual((st, json.loads(body)), (504, {"message": "upstream timeout"}))
                self.assertLess(dt, 3.5, "filtered body must be cut near the 2 s deadline, took %.1f" % dt)
            finally:
                b.stop()

    def test_only_one_config_check_in_flight(self):
        import threading
        with FakeSupervisor() as sup:
            sup.route("POST", "/core/check", FakeSupervisor.slow(1.5, then=(200, {"result": "ok", "data": {}})))
            b = BrokerProc(sup.url).wait_ready()
            try:
                first = {}
                t = threading.Thread(target=lambda: first.update(zip(("st", "h", "body"), http_call(b.url, "POST", "/core/check", body=b""))))
                t.start()
                self.assertTrue(wait_for(lambda: sup.find("POST", "/core/check"), timeout=3))
                st, _, body = http_call(b.url, "POST", "/core/check", body=b"")
                self.assertEqual((st, json.loads(body)), (429, {"message": "check already running"}))
                t.join(5)
                self.assertEqual(first["st"], 200)
                st, _, _ = http_call(b.url, "POST", "/core/check", body=b"")
                self.assertEqual(st, 200, "slot released after the first check finished")
                self.assertEqual(len(sup.find("POST", "/core/check")), 2)
            finally:
                b.stop()

    def test_template_allowlist_mode_end_to_end(self):
        area = "{%- for s in states -%}{{ s.entity_id ~ '\x1f' ~ (area_name(s.entity_id) or '') }}\n{%- endfor -%}"
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump([area], f)
        try:
            with FakeSupervisor() as sup:
                b = BrokerProc(sup.url, extra_env={"CLAUDE_JOB_BROKER_TEMPLATE_ALLOWLIST_FILE": f.name}).wait_ready()
                try:
                    hdr = {"Content-Type": "application/json"}
                    st, _, out = http_call(b.url, "POST", "/core/api/template", body=json.dumps({"template": area}).encode(), headers=hdr)
                    self.assertEqual((st, out), (200, b"rendered"))
                    self.assertEqual(sup.requests[-1]["json"], {"template": area})
                    for other in ("{{ states('sensor.x') }}", area + "\n", "{{ now() }}"):
                        st, _, out = http_call(b.url, "POST", "/core/api/template", body=json.dumps({"template": other}).encode(), headers=hdr)
                        self.assertEqual((st, json.loads(out)), (403, {"message": "template not in the hass-mcp allow-list"}), other)
                    self.assertEqual(len(sup.find("POST", "/core/api/template")), 1)
                    self.assertNotIn("heuristic", b.stderr())
                finally:
                    b.stop()
                # default mode on this box (no hass-mcp): the broker fails CLOSED - it says so once at
                # startup and refuses every template, even an innocuous one
                b = BrokerProc(sup.url).wait_ready()
                try:
                    no_hass_mcp = importlib.util.find_spec("app") is None
                    self.assertEqual("allowing no templates" in b.stderr(), no_hass_mcp)
                    if no_hass_mcp:
                        st, _, out = http_call(b.url, "POST", "/core/api/template",
                                               body=json.dumps({"template": "{{ states('sensor.x') }}"}).encode(), headers=hdr)
                        self.assertEqual(st, 403)
                    self.assertNotIn(NONCE, b.stderr())
                finally:
                    b.stop()
        finally:
            os.unlink(f.name)

    def test_upstream_unreachable_is_502(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
        s.close()
        b = BrokerProc(f"http://127.0.0.1:{dead_port}", extra_env={"CLAUDE_JOB_BROKER_READ_TIMEOUT_S": "2"}).wait_ready()
        try:
            st, _, body = http_call(b.url, "GET", "/core/api/config")
            self.assertEqual((st, json.loads(body)), (502, {"message": "upstream unavailable"}))
            self.assertIn("[ha-broker] 502 GET /core/api/config", b.stderr())
        finally:
            b.stop()

    def test_redirect_is_not_followed(self):
        with FakeSupervisor() as sup:
            sup.route("GET", "/core/info", (302, b"", {"Location": sup.url + "/core/restart-via-redirect"}))
            b = BrokerProc(sup.url).wait_ready()
            try:
                st, _, _ = http_call(b.url, "GET", "/core/info")
                self.assertEqual(st, 302)
                self.assertEqual([r["path"] for r in sup.requests], ["/core/info"])
            finally:
                b.stop()


# =============================================================================================
@unittest.skipIf(os.path.exists("/usr/local/bin/ha"), "a real /usr/local/bin/ha shadows the CLAUDE_JOB_REAL_HA test seam (F8)")
class TestHaWrapper(unittest.TestCase):
    """bash wrapper: strips follow/boot flags, points the real CLI (here fake_ha) at the broker."""

    def setUp(self):
        self.s = ScratchRoot().start()
        self.env = dict(self.s.env, CLAUDE_JOB_BROKER_PORT="41233", CLAUDE_JOB_BROKER_NONCE=NONCE)

    def tearDown(self):
        self.s.stop()

    def run_ha(self, *args, env=None):
        rc, out, err = run_cli(["/bin/bash", HA_WRAPPER, *args], env or self.env, timeout=10)
        return rc, (json.loads(out) if out.strip().startswith("[") else out), err

    def test_follow_and_boot_flags_are_stripped(self):
        prefix = ["--endpoint", "http://127.0.0.1:41233", "--api-token", NONCE]
        rc, argv, err = self.run_ha("core", "logs", "-f", "-n", "50")
        self.assertEqual((rc, argv), (0, prefix + ["core", "logs", "-n", "50"]), err)
        rc, argv, _ = self.run_ha("core", "logs", "-b", "1", "--boot=2", "--follow", "--lines", "10")
        self.assertEqual(argv, prefix + ["core", "logs", "--lines", "10"])
        rc, argv, _ = self.run_ha("host", "logs", "--boot", "abc", "-b-1", "--follow=true", "-v")
        self.assertEqual(argv, prefix + ["host", "logs", "-v"])
        rc, argv, _ = self.run_ha("addons", "info", "core_ssh")
        self.assertEqual(argv, prefix + ["addons", "info", "core_ssh"])
        rc, argv, _ = self.run_ha()
        self.assertEqual((rc, argv), (0, prefix))
        rc, argv, _ = self.run_ha("core", "logs", "-f=true", "-vn", "5", "-n50", "--lines=3")
        self.assertEqual(argv, prefix + ["core", "logs", "-vn", "5", "-n50", "--lines=3"])
        rc, argv, _ = self.run_ha("host", "logs", "-tfoo", "-v")
        self.assertEqual(argv, prefix + ["host", "logs", "-tfoo", "-v"])
        calls = self.s.fake_ha_calls()
        self.assertEqual(len(calls), 7)
        self.assertEqual(calls[0]["env"].get("CLAUDE_JOB_BROKER_PORT"), "41233")

    def test_short_flag_clusters_hiding_follow_or_boot_are_refused(self):
        for cluster in ("-fn", "-vf", "-fv", "-fb1", "-vfn", "-vb"):
            rc, out, err = self.run_ha("core", "logs", cluster, "50")
            self.assertEqual(rc, 1, cluster)
            self.assertIn("combined short flags containing -f/-b are not supported", err)
            self.assertEqual(out, "")
        self.assertEqual(self.s.fake_ha_calls(), [])
        # `-bf` is pflag for boot="f": a glued boot value, stripped like `-b1`
        rc, argv, _ = self.run_ha("core", "logs", "-bf", "-n", "5")
        self.assertEqual((rc, argv[4:]), (0, ["core", "logs", "-n", "5"]))

    def test_real_cli_path_wins_over_env_seam(self):
        text = HA_WRAPPER.read_text()
        self.assertIn('REAL=/usr/local/bin/ha\nif [[ ! -x "$REAL" ]]; then\n    REAL="${CLAUDE_JOB_REAL_HA:-$REAL}"', text)

    def test_endpoint_token_config_flags_are_refused(self):
        for bad in (["--endpoint", "http://supervisor"], ["--endpoint=http://supervisor"], ["--api-token", "x"],
                    ["--api-token=x"], ["--config", "/tmp/x"], ["--config=/tmp/x"]):
            rc, out, err = self.run_ha("core", "info", *bad)
            self.assertEqual(rc, 1, bad)
            self.assertIn("ha: --endpoint/--api-token/--config are not permitted inside a job", err)
            self.assertEqual(out, "")
            rc, out, err = self.run_ha(*bad, "core", "info")
            self.assertEqual(rc, 1, bad)
        self.assertEqual(self.s.fake_ha_calls(), [])

    def test_supervisor_env_fallbacks_are_unset(self):
        probe = self.s.bin / "env-probe"
        probe.write_text("#!/bin/sh\nenv\n")
        probe.chmod(0o755)
        env = dict(self.env, CLAUDE_JOB_REAL_HA=str(probe), SUPERVISOR_ENDPOINT="supervisor",
                   SUPERVISOR_API_TOKEN="real-token", SUPERVISOR_TOKEN="real-token")
        rc, out, err = run_cli(["/bin/bash", HA_WRAPPER, "core", "info"], env, timeout=10)
        self.assertEqual(rc, 0, err)
        self.assertIn("CLAUDE_JOB_BROKER_PORT=41233", out)
        for var in ("SUPERVISOR_ENDPOINT=", "SUPERVISOR_API_TOKEN=", "SUPERVISOR_TOKEN="):
            self.assertNotIn(var, out)
        self.assertNotIn("real-token", out)

    def test_missing_env_is_an_error(self):
        for drop in ("CLAUDE_JOB_BROKER_PORT", "CLAUDE_JOB_BROKER_NONCE"):
            env = dict(self.env)
            env.pop(drop)
            rc, out, err = self.run_ha("core", "info", env=env)
            self.assertEqual(rc, 1, drop)
            self.assertIn("ha: no job broker in this environment", err)
            self.assertEqual(out, "")
        env = dict(self.env, CLAUDE_JOB_BROKER_PORT="127.0.0.1:1")
        rc, _, err = self.run_ha("core", "info", env=env)
        self.assertEqual(rc, 1)
        self.assertEqual(self.s.fake_ha_calls(), [])

    def test_wrapper_against_live_broker(self):
        """End to end: wrapper argv -> (a curl-like stand-in for the CLI) -> broker -> fake Supervisor."""
        with FakeSupervisor() as sup:
            b = BrokerProc(sup.url).wait_ready()
            try:
                # emulate what `ha core logs -n 3` does on the wire (ha-facts §B): Bearer + Range
                st, _, body = http_call(b.url, "GET", "/core/logs", headers={"Range": "entries=:-2:", "Accept": "text/plain"})
                self.assertEqual(st, 200)
                self.assertEqual(sup.requests[-1]["headers"]["range"], "entries=:-2:")
                env = dict(self.env, CLAUDE_JOB_BROKER_PORT=str(b.port))
                rc, argv, _ = self.run_ha("core", "logs", "-n", "3", "-f", env=env)
                self.assertEqual(argv[:2], ["--endpoint", f"http://127.0.0.1:{b.port}"])
            finally:
                b.stop()


if __name__ == "__main__":
    unittest.main()
