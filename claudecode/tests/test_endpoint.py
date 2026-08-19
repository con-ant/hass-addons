"""Tests for `claude-job-endpoint` (breakdown 2k / §3 U5, CONTRACTS A23 + A30).

Every HTTP test starts the real endpoint as a subprocess on a free localhost port with
`--no-tick` (pure request/response behaviour), pointed at a FakeSupervisor and at fake_runner
via CLAUDE_JOB_RUNNER_BIN. Tick tests run `--tick-once` (one synchronous pass, no server) so
timing is deterministic; the live-cadence class runs the tick at a 1 s interval.
Nothing here needs Home Assistant, the network, or a real `claude`.
"""
import fcntl
import http.client
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import time
import unittest

from testlib import ScratchRoot, run_cli, wait_for, ENDPOINT, HEALTH_CHECK_FM, MINIMAL_FM, \
    SUPERVISOR_TOKEN, ADDON_VERSION, BUILT_CLI_VERSION
from fakes.fake_supervisor import FakeSupervisor
import jobcommon as jc
import jobdef

TOKEN = "cd" * 32
OTHER_TOKEN = "ef" * 32
ACTION_FM = {"description": "restart thing", "kind": "action", "tools": ["Bash(ha core restart)"]}
INPUT_FM = dict(MINIMAL_FM, description="energy", min_interval=0,
                input={"date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$",
                                "description": "day"}})


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def iso_ago(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds))


def request(port, method, path, token=TOKEN, body=None, headers=None, raw_body=None, timeout=15):
    """-> (status, headers dict lower-cased, body bytes). `body` dict -> JSON; `raw_body` bytes verbatim."""
    hdrs = {}
    if token is not None:
        hdrs["Authorization"] = f"Bearer {token}"
    data = None
    if raw_body is not None:
        data = raw_body
    elif body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


def rjson(port, method, path, **kw):
    status, hdrs, body = request(port, method, path, **kw)
    return status, (json.loads(body) if body else None)


def raw_exchange(port, data: bytes, timeout=10) -> bytes:
    """Send bytes verbatim, read until the server closes. For framing / malformed-request tests."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(data)
        chunks = []
        while True:
            try:
                b = sock.recv(65536)
            except (ConnectionResetError, socket.timeout):
                break
            if not b:
                break
            chunks.append(b)
        return b"".join(chunks)


def hold_flock(path):
    """Take the job lock like a live runner would; returns the fd (os.close releases)."""
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


class EndpointProc:
    """The endpoint under test as a subprocess; stderr captured to a file."""

    def __init__(self, scratch, args=(), extra_env=None):
        self.scratch = scratch
        self.port = free_port()
        self.errlog = scratch.fakelogs / f"endpoint-{self.port}.err"
        env = dict(scratch.env, **(extra_env or {}))
        self.errfile = open(self.errlog, "wb")
        self.proc = subprocess.Popen(
            [sys.executable, str(ENDPOINT), "--bind", "127.0.0.1", "--port", str(self.port), *args],
            env=env, stdin=subprocess.DEVNULL, stdout=self.errfile, stderr=self.errfile)
        ok = wait_for(lambda: request(self.port, "GET", "/health", token=None, timeout=1)[0] == 200, timeout=15)
        if not ok:
            self.stop()
            raise RuntimeError("endpoint did not come up:\n" + self.stderr())

    def stderr(self) -> str:
        try:
            return self.errlog.read_text(errors="replace")
        except OSError:
            return ""

    def stop(self, sig=signal.SIGTERM, timeout=5):
        if self.proc.poll() is None:
            self.proc.send_signal(sig)
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.errfile.close()
        return self.proc.returncode


class EndpointCase(unittest.TestCase):
    """Scratch root + FakeSupervisor + token + fake runner; subclasses add jobs and start."""
    endpoint_args = ("--no-tick",)
    endpoint_env = {}

    @classmethod
    def setUpClass(cls):
        cls.s = ScratchRoot().start()
        cls.sup = FakeSupervisor().start()
        cls.s.with_supervisor(cls.sup).use_fake_runner()
        cls.s.write_token(TOKEN)
        cls.seed()
        cls.ep = EndpointProc(cls.s, cls.endpoint_args, cls.endpoint_env)

    @classmethod
    def seed(cls):
        pass

    @classmethod
    def tearDownClass(cls):
        try:
            cls.ep.stop()
        finally:
            cls.sup.stop()
            cls.s.stop()

    # helpers
    def get(self, path, **kw):
        return rjson(self.ep.port, "GET", path, **kw)

    def post(self, path, body=None, **kw):
        return rjson(self.ep.port, "POST", path, body=body, **kw)

    def runner_calls_for(self, job):
        return [c for c in self.s.fake_runner_calls() if len(c["argv"]) > 1 and c["argv"][1] == job]

    def terminal_state(self, name, status="ok", published=True, **result):
        res = {"run_id": "run-20260818T050000Z-aaaa", "session_id": "s-1", "status": status,
               "headline": f"{name} headline", "detail": f"# {name}\ndetail text", "reason": None,
               "judgment_row": 13, "started_at": iso_ago(120), "ended_at": iso_ago(60), "duration_s": 60.0,
               "cost_usd": 0.25, "model": "fable", "trigger": "endpoint", "attempts": 1, "metrics": {}}
        res.update(result)
        return {"schema": 1, "job": name, "slug": jobdef.slug(name), "status": status, "enabled": True,
                "published": published, "updated_at": res["ended_at"], "description": f"{name} desc",
                "stale_after": None, "run": None, "result": res, "prev_status": None,
                "stats": {"run_count": 3, "skipped_since_last": 0, "last_skip_at": None, "first_seen": iso_ago(999)},
                "notify": {"channels": [], "notify_status": None, "last_notified": None}}


# ---------------------------------------------------------------------------------------------
class TestHTTP(EndpointCase):
    @classmethod
    def seed(cls):
        s = cls.s
        s.write_job("health-check", dict(HEALTH_CHECK_FM, min_interval=0))
        s.write_job("energy-report", INPUT_FM)
        s.write_job("rate-limited", dict(MINIMAL_FM, min_interval=300))
        s.write_job("no-limit", dict(MINIMAL_FM, min_interval=0))
        s.write_job("cap-test", dict(MINIMAL_FM, min_interval=0))
        s.write_job("flag-off", MINIMAL_FM)
        s.write_job("fm-off", dict(MINIMAL_FM, enabled=False))
        s.write_job("restart-thing", ACTION_FM)
        s.write_job("a-b", dict(MINIMAL_FM, min_interval=0))
        s.write_job("toggle-me", dict(MINIMAL_FM, min_interval=0))
        s.write_job("never-ran", MINIMAL_FM)
        s.write_job("framing", dict(INPUT_FM, min_interval=0))
        s.write_raw_job("broken-def.md", "no frontmatter here\n")
        s.set_flag_disabled("flag-off")

    # -- /health --
    def test_health_is_unauthenticated_and_token_free(self):
        status, hdrs, body = request(self.ep.port, "GET", "/health", token=None)
        self.assertEqual(status, 200)
        self.assertNotIn(TOKEN.encode(), body)
        self.assertNotIn(SUPERVISOR_TOKEN.encode(), body)
        h = json.loads(body)
        self.assertEqual(h["status"], "ok")
        self.assertEqual((h["jobs_dir"], h["token_set"], h["cli_preflight"], h["stopping"]), (True, True, True, False))
        self.assertEqual(h["version"], ADDON_VERSION)
        self.assertEqual(h["claude_version"], BUILT_CLI_VERSION)
        self.assertIsInstance(h["uptime_s"], int)
        self.assertTrue(hdrs["content-type"].startswith("application/json"))
        self.assertEqual(hdrs["server"], "claude-job-endpoint")

    def test_cost_file_seeded_in_ha_time_zone_at_start(self):
        cost = self.s.read_json(self.s.state / "_cost.json")
        self.assertIsNotNone(cost, "endpoint start seeds state/_cost.json from GET /core/api/config")
        self.assertEqual((cost["schema"], cost["time_zone"], cost["total_usd"], cost["runs"], cost["by_job"]),
                         (1, "Europe/Vienna", 0.0, 0, {}))
        self.assertEqual(cost["month"], jc.month_key("Europe/Vienna"))
        self.assertTrue(self.sup.find("GET", "/core/api/config"))
        j = self.get("/jobs")[1]
        self.assertEqual(j["cost_month_usd"], 0.0)
        self.assertRegex(j["cost_month_start"], r"^\d{4}-\d{2}-01T00:00:00\+0[12]:00$")   # Vienna, never +00:00

    # -- auth --
    def test_401_matrix(self):
        routes = [("GET", "/jobs"), ("GET", "/jobs/health-check/detail"), ("POST", "/jobs/no-limit/run"),
                  ("POST", "/jobs/no-limit/enable"), ("POST", "/jobs/no-limit/disable"),
                  ("POST", "/republish"), ("POST", "/action")]
        for method, path in routes:
            for tok, hdr in ((None, None), (OTHER_TOKEN, None), ("", None), (None, {"Authorization": f"Basic {TOKEN}"}),
                             (None, {"Authorization": TOKEN})):
                status, body = rjson(self.ep.port, method, path, token=tok, headers=hdr, body={})
                self.assertEqual(status, 401, (method, path, tok, hdr))
                self.assertEqual(body, {"error": "unauthorized"})
        self.assertEqual(self.runner_calls_for("no-limit"), [])

    def test_token_rotation_is_live_and_missing_token_is_503(self):
        self.assertEqual(self.get("/jobs")[0], 200)
        self.s.write_token(OTHER_TOKEN)
        try:
            self.assertEqual(self.get("/jobs")[0], 401)
            self.assertEqual(self.get("/jobs", token=OTHER_TOKEN)[0], 200)
            os.unlink(self.s.data / "token")
            self.assertEqual(self.get("/jobs", token=OTHER_TOKEN), (503, {"error": "no_token"}))
            h = self.get("/health", token=None)[1]
            self.assertEqual((h["status"], h["token_set"]), ("degraded", False))
        finally:
            self.s.write_token(TOKEN)
        self.assertEqual(self.get("/jobs")[0], 200)

    # -- routing --
    def test_unknown_routes_and_methods(self):
        self.assertEqual(self.get("/nope"), (404, {"error": "not_found"}))
        self.assertEqual(self.get("/jobs/health-check"), (404, {"error": "not_found"}))
        self.assertEqual(self.post("/action", {}), (404, {"error": "reserved_v1_1"}))
        self.assertEqual(rjson(self.ep.port, "PUT", "/jobs", body={}), (405, {"error": "method_not_allowed"}))
        self.assertEqual(self.get("/republish")[0], 405)
        self.assertEqual(self.post("/jobs", {})[0], 405)
        status, _h, body = request(self.ep.port, "HEAD", "/health", token=None)
        self.assertEqual((status, body), (405, b""))

    def test_malformed_request_lines_get_json_errors_not_empty_replies(self):
        reply = raw_exchange(self.ep.port, b"GARBAGE\r\n\r\n")
        self.assertIn(b'{"error":"bad_request","code":400}', reply)
        reply = raw_exchange(self.ep.port, b"GET /health WHAT/1.1\r\n\r\n")
        self.assertIn(b'"code":400', reply)
        reply = raw_exchange(self.ep.port, b"GET /" + b"a" * 70000 + b" HTTP/1.1\r\n\r\n")
        self.assertTrue(reply.startswith(b"HTTP/1.") and b" 414 " in reply.split(b"\r\n", 1)[0], reply[:120])
        self.assertIn(b'"code":414', reply)
        self.assertEqual(self.get("/health", token=None)[1]["status"], "ok")
        err = self.ep.stderr()
        self.assertNotIn("Traceback", err)
        self.assertNotIn("AttributeError", err)
        self.assertIn("] 400 - -", err)

    def test_post_bodies_must_be_content_length_framed_411(self):
        before = len(self.runner_calls_for("framing"))
        auth = f"Authorization: Bearer {TOKEN}\r\n".encode()
        body = b'{"input":{"date":"zzzz"}}'
        chunked = (b"POST /jobs/framing/run HTTP/1.1\r\nHost: x\r\n" + auth +
                   b"Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n" +
                   b"%x\r\n" % len(body) + body + b"\r\n0\r\n\r\n")
        reply = raw_exchange(self.ep.port, chunked)
        self.assertIn(b" 411 ", reply.split(b"\r\n", 1)[0])
        self.assertIn(b'{"error":"length_required"}', reply)
        no_cl = b"POST /jobs/framing/run HTTP/1.1\r\nHost: x\r\n" + auth + b"\r\n"
        reply = raw_exchange(self.ep.port, no_cl)
        self.assertIn(b" 411 ", reply.split(b"\r\n", 1)[0])
        reply = raw_exchange(self.ep.port, b"POST /republish HTTP/1.1\r\nHost: x\r\n" + auth + b"\r\n")
        self.assertIn(b" 411 ", reply.split(b"\r\n", 1)[0])
        # 401 still precedes framing checks; explicit Content-Length: 0 is the way to say "no body"
        reply = raw_exchange(self.ep.port, b"POST /jobs/framing/run HTTP/1.1\r\nHost: x\r\n"
                                           b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n")
        self.assertIn(b" 401 ", reply.split(b"\r\n", 1)[0])
        self.assertEqual(request(self.ep.port, "POST", "/jobs/framing/run", headers={"Content-Length": "0"})[0], 202)
        self.assertTrue(wait_for(lambda: len(self.runner_calls_for("framing")) == before + 1))
        for bad_cl in ("+2", "0x10", "1_0", "-1", "abc"):
            reply = raw_exchange(self.ep.port, b"POST /jobs/framing/run HTTP/1.1\r\nHost: x\r\n" + auth +
                                 b"Content-Length: " + bad_cl.encode() + b"\r\n\r\n{}")
            self.assertIn(b" 400 ", reply.split(b"\r\n", 1)[0], bad_cl)
        time.sleep(0.3)
        self.assertEqual(len(self.runner_calls_for("framing")), before + 1)

    def test_short_body_times_out_with_408_not_500(self):
        auth = f"Authorization: Bearer {TOKEN}\r\n".encode()
        with socket.create_connection(("127.0.0.1", self.ep.port), timeout=10) as sock:
            sock.sendall(b"POST /jobs/framing/run HTTP/1.1\r\nHost: x\r\n" + auth + b"Content-Length: 100\r\n\r\n{")
            sock.shutdown(socket.SHUT_WR)          # body ends 99 bytes short
            reply = sock.recv(65536)
        self.assertIn(b" 408 ", reply.split(b"\r\n", 1)[0])
        self.assertIn(b'{"error":"request_timeout"}', reply)

    def test_name_token_rules(self):
        self.assertEqual(self.post("/jobs/zzz/run", {}), (404, {"error": "unknown_job"}))
        self.assertEqual(self.post("/jobs/Health-Check/run", {}), (404, {"error": "unknown_job"}))
        self.assertEqual(self.post("/jobs/../health-check/run", {})[0], 404)
        self.assertEqual(self.post("/jobs/%2e%2e/run", {})[0], 404)
        self.assertEqual(self.post("/jobs/health-check.md/run", {})[0], 404)
        self.assertEqual(self.post("/jobs/" + "a" * 49 + "/run", {})[0], 404)
        self.assertEqual(self.post("/jobs/_notify/run", {})[0], 404)
        # slug spelling maps to the hyphenated name
        status, body = self.post("/jobs/a_b/run", {})
        self.assertEqual((status, body["accepted"], body["job"]), (202, True, "a-b"))

    # -- run --
    def test_run_spawns_runner_detached_with_exact_argv(self):
        status, body = self.post("/jobs/health-check/run", {})
        self.assertEqual(status, 202, body)
        self.assertEqual((body["accepted"], body["job"]), (True, "health-check"))
        rid = body["run_id"]
        self.assertRegex(rid, jobdef.RUN_ID_RE.pattern)
        self.assertTrue(wait_for(lambda: self.runner_calls_for("health-check")))
        call = self.runner_calls_for("health-check")[0]
        self.assertEqual(call["argv"], ["run", "health-check", "--run-id", rid, "--trigger", "endpoint"])
        self.assertEqual(call["sid"], call["pid"], "runner must be a session leader (start_new_session)")
        self.assertEqual(call["pgid"], call["pid"])
        self.assertEqual(call["cwd"], "/")
        self.assertIn("SUPERVISOR_TOKEN", call["env_keys"])
        self.assertIsNone(call["inbox"])
        # absent body works too
        status, _h, raw = request(self.ep.port, "POST", "/jobs/no-limit/run")
        self.assertEqual(status, 202, raw)

    def test_run_with_input_writes_inbox_file(self):
        status, body = self.post("/jobs/energy-report/run", {"input": {"date": "2026-08-17"}})
        self.assertEqual(status, 202, body)
        rid = body["run_id"]
        self.assertTrue(wait_for(lambda: self.runner_calls_for("energy-report")))
        call = self.runner_calls_for("energy-report")[-1]
        inbox_path = str(self.s.inbox / f"{rid}.json")
        self.assertEqual(call["argv"], ["run", "energy-report", "--run-id", rid, "--trigger", "endpoint",
                                        "--input-file", inbox_path])
        self.assertEqual(set(call["inbox"]), {"run_id", "job", "input", "received_at"})
        self.assertEqual((call["inbox"]["run_id"], call["inbox"]["job"], call["inbox"]["input"]),
                         (rid, "energy-report", {"date": "2026-08-17"}))
        self.assertEqual(stat.S_IMODE(os.stat(inbox_path).st_mode), 0o600)
        # empty input object -> no inbox file
        status, body = self.post("/jobs/energy-report/run", {"input": {}})
        self.assertEqual(status, 202)
        self.assertTrue(wait_for(lambda: len(self.runner_calls_for("energy-report")) >= 2))
        self.assertNotIn("--input-file", self.runner_calls_for("energy-report")[-1]["argv"])

    def test_invalid_input_422(self):
        before = len(self.runner_calls_for("health-check") + self.runner_calls_for("energy-report"))
        status, body = self.post("/jobs/health-check/run", {"input": {"date": "2026-08-17"}})
        self.assertEqual((status, body["error"]), (422, "invalid_input"))
        self.assertEqual(body["errors"], ["input: this job takes no input"])
        status, body = self.post("/jobs/energy-report/run", {"input": {"date": "yesterday"}})
        self.assertEqual((status, body["error"]), (422, "invalid_input"))
        self.assertTrue(any(e.startswith("input.date:") for e in body["errors"]), body)
        status, body = self.post("/jobs/energy-report/run", {"input": {"bogus": 1}})
        self.assertEqual(status, 422)
        status, body = self.post("/jobs/energy-report/run", {"input": "2026-08-17"})
        self.assertEqual((status, body["error"]), (422, "invalid_input"))
        time.sleep(0.3)
        self.assertEqual(len(self.runner_calls_for("health-check") + self.runner_calls_for("energy-report")), before)

    def test_kind_action_422(self):
        self.assertEqual(self.post("/jobs/restart-thing/run", {}),
                         (422, {"error": "not_triggerable", "reason": "kind_action"}))
        self.assertEqual(self.post("/jobs/restart_thing/run", {})[0], 422)
        self.assertEqual(self.runner_calls_for("restart-thing"), [])

    def test_disabled_gates_answer_200(self):
        self.assertEqual(self.post("/jobs/flag-off/run", {}),
                         (200, {"accepted": False, "job": "flag-off", "reason": "disabled", "gate": "flag"}))
        self.assertEqual(self.post("/jobs/fm-off/run", {}),
                         (200, {"accepted": False, "job": "fm-off", "reason": "disabled", "gate": "frontmatter"}))
        self.assertEqual(self.runner_calls_for("flag-off") + self.runner_calls_for("fm-off"), [])

    def test_rate_limit(self):
        status, body = self.post("/jobs/rate-limited/run", {})
        self.assertEqual(status, 202, body)
        status, body = self.post("/jobs/rate-limited/run", {})
        self.assertEqual((status, body["accepted"], body["reason"]), (200, False, "rate_limited"))
        self.assertTrue(250 <= body["retry_after_s"] <= 300, body)
        self.assertEqual(body["job"], "rate-limited")
        for _ in range(3):
            self.assertEqual(self.post("/jobs/no-limit/run", {})[0], 202)
        self.assertTrue(wait_for(lambda: len(self.runner_calls_for("rate-limited")) == 1))

    def test_body_cap_413_and_bad_json_400(self):
        big = b" " * 65535 + b"{}"          # 65537 bytes declared -> 413 without reading
        status, _h, body = request(self.ep.port, "POST", "/jobs/cap-test/run", raw_body=big)
        self.assertEqual((status, json.loads(body)), (413, {"error": "too_large"}))
        status, _h, body = request(self.ep.port, "POST", "/jobs/cap-test/run", raw_body=b"x",
                                   headers={"Content-Length": "65537"})
        self.assertEqual(status, 413)
        ok_body = b" " * 65534 + b"{}"      # exactly the cap -> accepted
        self.assertEqual(request(self.ep.port, "POST", "/jobs/cap-test/run", raw_body=ok_body)[0], 202)
        for bad in (b"{not json", b"[1]", b'"str"'):
            status, _h, body = request(self.ep.port, "POST", "/jobs/cap-test/run", raw_body=bad)
            self.assertEqual((status, json.loads(body)), (400, {"error": "bad_json"}), bad)
        # 413 also guards the other POST routes; 401 still wins over 413
        self.assertEqual(request(self.ep.port, "POST", "/republish", raw_body=big)[0], 413)
        self.assertEqual(request(self.ep.port, "POST", "/republish", raw_body=big, token=OTHER_TOKEN)[0], 401)

    def test_stopping_flag_503(self):
        flag = self.s.run_dir / "stopping"
        flag.write_text("")
        try:
            self.assertEqual(self.post("/jobs/no-limit/run", {}), (503, {"error": "stopping"}))
            h = self.get("/health", token=None)[1]
            self.assertEqual((h["status"], h["stopping"]), ("degraded", True))
            # stopping is checked before the disabled gate
            self.assertEqual(self.post("/jobs/flag-off/run", {})[0], 503)
        finally:
            flag.unlink()
        self.assertEqual(self.get("/health", token=None)[1]["status"], "ok")

    def test_broken_definition_still_spawns_runner_as_single_authority(self):
        status, body = self.post("/jobs/broken-def/run", {"input": {"whatever": 1}})
        self.assertEqual(status, 202, body)
        self.assertTrue(wait_for(lambda: self.runner_calls_for("broken-def")))

    # -- detail --
    def test_detail_markdown_and_204(self):
        self.s.write_state("health-check", self.terminal_state("health-check", "warning",
                                                                detail="# Report\n\n* item"))
        status, hdrs, body = request(self.ep.port, "GET", "/jobs/health_check/detail")
        self.assertEqual((status, body.decode()), (200, "# Report\n\n* item"))
        self.assertTrue(hdrs["content-type"].startswith("text/markdown"))
        status, hdrs, body = request(self.ep.port, "GET", "/jobs/never-ran/detail")
        self.assertEqual((status, body), (204, b""))
        self.assertEqual(request(self.ep.port, "GET", "/jobs/zzz/detail")[0], 404)

    # -- enable / disable --
    def test_enable_disable_toggle_flag_state_and_publish(self):
        self.s.write_state("toggle-me", self.terminal_state("toggle-me", "ok"))
        self.sup.clear()
        self.assertEqual(self.post("/jobs/toggle_me/disable", {}), (200, {"job": "toggle-me", "enabled": False}))
        self.assertTrue((self.s.disabled / "toggle_me").exists() or (self.s.disabled / "toggle-me").exists())
        st = self.s.read_state("toggle-me")
        self.assertEqual((st["enabled"], st["published"]), (False, True))
        posts = self.sup.find("POST", "/core/api/states/sensor.claude_job_toggle_me")
        self.assertEqual(len(posts), 1)
        self.assertEqual((posts[0]["json"]["state"], posts[0]["json"]["attributes"]["enabled"]), ("ok", False))
        self.assertEqual(self.post("/jobs/toggle-me/run", {})[1]["reason"], "disabled")
        self.assertEqual(self.post("/jobs/toggle-me/enable", {}), (200, {"job": "toggle-me", "enabled": True}))
        self.assertFalse((self.s.disabled / "toggle_me").exists() or (self.s.disabled / "toggle-me").exists())
        self.assertTrue(self.s.read_state("toggle-me")["enabled"])
        self.assertEqual(len(self.sup.find("POST", "/core/api/states/sensor.claude_job_toggle_me")), 2)
        self.assertEqual(self.post("/jobs/toggle-me/run", {})[0], 202)
        # enable on a frontmatter-disabled job: flag removed, still disabled, reason given
        self.s.set_flag_disabled("fm-off")
        self.assertEqual(self.post("/jobs/fm-off/enable", {}),
                         (200, {"job": "fm-off", "enabled": False, "reason": "frontmatter"}))
        self.assertFalse((self.s.disabled / "fm-off").exists() or (self.s.disabled / "fm_off").exists())
        self.assertEqual(self.post("/jobs/zzz/enable", {})[0], 404)

    # -- republish --
    def test_republish_counts_state_files_plus_cost_raw(self):
        self.s.write_state("health-check", self.terminal_state("health-check", "warning"))
        self.s.write_state("never-ran", self.terminal_state("never-ran", "ok", published=False))
        n_states = len([f for f in os.listdir(self.s.state) if f.endswith(".json") and not f.startswith("_")])
        self.sup.clear()
        status, body = self.post("/republish", {})
        self.assertEqual((status, body), (200, {"republished": n_states + 1, "failed": 0}))
        posts = self.sup.find("POST", "/core/api/states/")
        self.assertEqual(len(posts), n_states + 1)
        self.assertEqual(len(self.sup.find("POST", "/core/api/states/sensor.claude_jobs_cost_raw")), 1)
        self.assertTrue(self.s.read_state("never-ran")["published"])
        # HA down -> failures counted, nothing raised
        self.sup.route("POST", "/core/api/states/", (502, {"message": "down"}), prefix=True)
        try:
            status, body = self.post("/republish", {})
            self.assertEqual((status, body), (200, {"republished": 0, "failed": n_states + 1}))
        finally:
            self.sup.unroute("POST", "/core/api/states/")

    # -- logging --
    def test_zz_log_has_only_non_2xx_lines_and_never_the_token(self):
        request(self.ep.port, "POST", "/jobs/health-check/run", token=OTHER_TOKEN, body={})
        self.assertTrue(wait_for(lambda: "401 POST /jobs/health-check/run" in self.ep.stderr()))
        err = self.ep.stderr()
        self.assertIn("[claude-job-endpoint] 401 POST /jobs/health-check/run", err)
        self.assertNotIn(TOKEN, err)
        self.assertNotIn(OTHER_TOKEN, err)
        self.assertNotIn(SUPERVISOR_TOKEN, err)
        self.assertNotIn("] 200 ", err)
        self.assertNotIn("] 202 ", err)


# ---------------------------------------------------------------------------------------------
class TestSummary(EndpointCase):
    @classmethod
    def seed(cls):
        s = cls.s
        old = time.time() - 3600
        p = s.write_job("stale-one", dict(MINIMAL_FM, stale_after=60))
        os.utime(p, (old, old))
        s.write_job("failing", dict(MINIMAL_FM, stale_after=86400))
        p = s.write_job("disabled-one", dict(MINIMAL_FM, stale_after=60))
        os.utime(p, (old, old))
        s.set_flag_disabled("disabled-one")
        s.write_job("fine", dict(HEALTH_CHECK_FM, description="Fine job"))
        s.write_job("restart-x", ACTION_FM)
        s.write_raw_job("_notify.yaml", "mobile: {}\n")

    def setUp(self):
        s = self.s
        s.write_state("failing", self.terminal_state("failing", "error", headline="boom", ended_at=iso_ago(30)))
        s.write_state("fine", self.terminal_state("fine", "ok", cost_usd=0.6312, ended_at=iso_ago(30)))
        month = time.strftime("%Y-%m", time.gmtime())   # Vienna and UTC agree except ~2 h a month; fine for a test
        (s.state / "_cost.json").write_text(json.dumps({
            "schema": 1, "month": month, "time_zone": "Europe/Vienna", "total_usd": 14.2031, "runs": 31,
            "by_job": {"fine": 14.2031}, "updated_at": iso_ago(30)}))

    def test_golden_summary(self):
        status, j = self.get("/jobs")
        self.assertEqual(status, 200)
        self.assertEqual(j["attention"], 2)
        self.assertEqual(j["worst_status"], "error")
        self.assertEqual(j["stale_jobs"], ["stale_one"])
        self.assertEqual(j["failed_jobs"], ["failing"])
        self.assertEqual(j["disabled_jobs"], ["disabled_one"])
        self.assertEqual((j["running"], j["job_count"]), (0, 4))
        self.assertEqual(j["cost_month_usd"], 14.2)
        self.assertRegex(j["cost_month_start"], r"^\d{4}-\d{2}-01T00:00:00[+-]\d{2}:\d{2}$")
        self.assertEqual(j["preflight"], {"ok": True, "missing": []})
        self.assertEqual((j["cli_preflight"], j["cli_missing"], j["cli_drift"]), (True, [], False))
        self.assertEqual((j["claude_version"], j["built_claude_version"]), (BUILT_CLI_VERSION, BUILT_CLI_VERSION))
        self.assertEqual((j["addon_version"], j["endpoint_version"]), (ADDON_VERSION, ADDON_VERSION))
        self.assertRegex(j["generated_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        names = [x["name"] for x in j["jobs"]]
        self.assertEqual(sorted(names), ["disabled-one", "failing", "fine", "stale-one"])   # action job excluded
        by = {x["name"]: x for x in j["jobs"]}
        self.assertEqual((by["fine"]["slug"], by["fine"]["status"], by["fine"]["kind"]), ("fine", "ok", "job"))
        self.assertEqual((by["fine"]["description"], by["fine"]["model"], by["fine"]["valid"]), ("Fine job", "opus", True))
        self.assertEqual((by["fine"]["run_count"], by["fine"]["cost_usd_last"], by["fine"]["stale_after"]), (3, 0.63, 93600))
        self.assertEqual(by["fine"]["headline"], "fine headline")
        self.assertEqual((by["stale-one"]["status"], by["stale-one"]["stale"], by["stale-one"]["enabled"]),
                         ("never_run", True, True))
        self.assertEqual((by["disabled-one"]["enabled"], by["disabled-one"]["stale"]), (False, False))
        self.assertEqual(by["failing"]["headline"], "boom")
        for key in ("name", "slug", "kind", "description", "status", "headline", "last_run", "enabled", "stale",
                    "stale_after", "model", "valid", "run_count", "cost_usd_last"):
            self.assertIn(key, by["fine"])
        top = {k: v for k, v in j.items() if k != "jobs"}
        self.assertLess(len(json.dumps(top)), 4096)

    def test_stale_cost_month_reads_zero_and_running_counts(self):
        (self.s.state / "_cost.json").write_text(json.dumps({
            "schema": 1, "month": "2020-01", "time_zone": "Europe/Vienna", "total_usd": 99.0, "runs": 3,
            "by_job": {}, "updated_at": "2020-01-31T00:00:00Z"}))
        running = self.terminal_state("fine", "ok")
        running.update(status="running", run={"run_id": "run-20260818T060000Z-bbbb", "pid": 1, "pgid": 1,
                                              "started_at": iso_ago(5), "deadline": iso_ago(-600),
                                              "timeout_s": 600, "trigger": "endpoint", "attempt": 1, "model": "fable"})
        self.s.write_state("fine", running)
        j = self.get("/jobs")[1]
        self.assertEqual(j["cost_month_usd"], 0.0)
        self.assertRegex(j["cost_month_start"], r"^\d{4}-\d{2}-01T00:00:00")
        self.assertEqual(j["running"], 1)
        self.assertEqual(j["worst_status"], "error")

    def test_worst_status_ranking(self):
        self.s.write_state("failing", self.terminal_state("failing", "warning"))
        self.assertEqual(self.get("/jobs")[1]["worst_status"], "warning")
        self.s.write_state("fine", self.terminal_state("fine", "critical"))
        self.assertEqual(self.get("/jobs")[1]["worst_status"], "critical")
        self.s.write_state("fine", self.terminal_state("fine", "aborted"))
        self.s.write_state("failing", self.terminal_state("failing", "info"))
        j = self.get("/jobs")[1]
        self.assertEqual((j["worst_status"], j["attention"], j["failed_jobs"]), ("aborted", 1, []))


# ---------------------------------------------------------------------------------------------
class TestPreflightFailed(EndpointCase):
    endpoint_env = {"FAKE_CLAUDE_HIDE_FLAG": "--settings"}

    @classmethod
    def seed(cls):
        cls.s.write_job("health-check", dict(HEALTH_CHECK_FM, min_interval=0))
        cls.s.write_job("flag-off", MINIMAL_FM)
        cls.s.set_flag_disabled("flag-off")

    def test_preflight_failure_surfaces_everywhere(self):
        posts = self.sup.find("POST", "/core/api/states/sensor.claude_jobs_endpoint")
        self.assertEqual(len(posts), 1, self.ep.stderr())
        attrs = posts[0]["json"]["attributes"]
        self.assertEqual(posts[0]["json"]["state"], "error")
        self.assertEqual((attrs["reason"], attrs["missing"]), ("cli_preflight_failed", ["--settings"]))
        self.assertEqual((attrs["friendly_name"], attrs["icon"]), ("Claude Jobs endpoint", "mdi:server-off"))
        h = self.get("/health", token=None)[1]
        self.assertEqual((h["status"], h["cli_preflight"]), ("degraded", False))
        j = self.get("/jobs")[1]
        self.assertEqual((j["cli_preflight"], j["preflight"]), (False, {"ok": False, "missing": ["--settings"]}))
        self.assertEqual(self.post("/jobs/health-check/run", {}),
                         (503, {"error": "cli_preflight_failed", "missing": ["--settings"]}))
        # the disabled gate is answered before the preflight refusal
        self.assertEqual(self.post("/jobs/flag-off/run", {})[1]["reason"], "disabled")
        self.assertEqual(self.s.fake_runner_calls(), [])
        self.assertIn("cli preflight FAILED", self.ep.stderr())


class TestJsonSchemaOnlyMissingStillServes(EndpointCase):
    endpoint_env = {"FAKE_CLAUDE_HIDE_FLAG": "--json-schema"}

    @classmethod
    def seed(cls):
        cls.s.write_job("no-limit", dict(MINIMAL_FM, min_interval=0))

    def test_json_schema_only_is_not_a_refusal(self):
        self.assertEqual(self.sup.find("POST", "/core/api/states/sensor.claude_jobs_endpoint"), [])
        self.assertEqual(self.get("/health", token=None)[1]["status"], "ok")
        self.assertEqual(self.post("/jobs/no-limit/run", {})[0], 202)
        self.assertEqual(self.get("/jobs")[1]["preflight"], {"ok": True, "missing": ["--json-schema"]})


# ---------------------------------------------------------------------------------------------
class TestLifecycle(unittest.TestCase):
    def test_sigterm_exits_0_fast_and_sigint_too(self):
        with ScratchRoot() as s, FakeSupervisor() as sup:
            s.with_supervisor(sup).use_fake_runner().write_token(TOKEN)
            for sig in (signal.SIGTERM, signal.SIGINT):
                ep = EndpointProc(s, ("--no-tick",))
                t0 = time.monotonic()
                rc = ep.stop(sig=sig, timeout=5)
                self.assertEqual(rc, 0, ep.stderr())
                self.assertLess(time.monotonic() - t0, 2.0)

    def test_tick_thread_first_pass_publishes_at_start_and_sigterm_still_fast(self):
        with ScratchRoot() as s, FakeSupervisor() as sup:
            s.with_supervisor(sup).use_fake_runner().write_token(TOKEN)
            s.write_job("fine", MINIMAL_FM)
            case = EndpointCase()
            case.s = s
            s.write_state("fine", case.terminal_state("fine", "ok", published=True))
            ep = EndpointProc(s, (), {"CLAUDE_JOB_TICK_INTERVAL_S": "1"})
            try:
                # republish trigger 1: everything is POSTed at start regardless of the flag
                self.assertTrue(wait_for(lambda: sup.find("POST", "/core/api/states/sensor.claude_job_fine")))
                self.assertTrue(wait_for(lambda: sup.find("POST", "/core/api/states/sensor.claude_jobs_cost_raw")))
                n_fine = len(sup.find("POST", "/core/api/states/sensor.claude_job_fine"))
                # a later unpublished write is picked up by the next 1 s pass; published files are not re-sent
                st = case.terminal_state("fine", "warning", published=False, ended_at=iso_ago(1))
                s.write_state("fine", st)
                self.assertTrue(wait_for(
                    lambda: len(sup.find("POST", "/core/api/states/sensor.claude_job_fine")) == n_fine + 1
                    and s.read_state("fine")["published"] is True, timeout=6))
                time.sleep(2.2)
                self.assertEqual(len(sup.find("POST", "/core/api/states/sensor.claude_job_fine")), n_fine + 1)
            finally:
                t0 = time.monotonic()
                self.assertEqual(ep.stop(), 0, ep.stderr())
                self.assertLess(time.monotonic() - t0, 2.0)

    def test_republish_partial_failure_keeps_force_armed_until_a_full_pass(self):
        with ScratchRoot() as s, FakeSupervisor() as sup:
            s.with_supervisor(sup).use_fake_runner().write_token(TOKEN)
            case = EndpointCase()
            case.s = s
            for name in ("alpha", "beta"):
                s.write_job(name, MINIMAL_FM)
                s.write_state(name, case.terminal_state(name, "ok", published=True))
            ep = EndpointProc(s, (), {"CLAUDE_JOB_TICK_INTERVAL_S": "1"})
            try:
                beta = "/core/api/states/sensor.claude_job_beta"
                self.assertTrue(wait_for(lambda: sup.find("POST", beta)))       # start-up forced pass, succeeds
                time.sleep(1.3)                                                # ... and clears the flag
                sup.route("POST", beta, (500, {"message": "core still booting"}))
                sup.clear()
                status, body = rjson(ep.port, "POST", "/republish", body={})
                self.assertEqual((status, body), (200, {"republished": 2, "failed": 1}))   # alpha + cost ok, beta failed
                self.assertTrue(s.read_state("beta")["published"], "flag on disk untouched by a failed POST")
                sup.unroute("POST", beta)
                n = len(sup.find("POST", beta))
                # force stayed armed -> the next 1 s tick re-sends everything although published:true
                self.assertTrue(wait_for(lambda: len(sup.find("POST", beta)) > n, timeout=5))
                m = len(sup.find("POST", beta))
                time.sleep(2.2)                                                # full success cleared it: quiet again
                self.assertEqual(len(sup.find("POST", beta)), m)
            finally:
                self.assertEqual(ep.stop(), 0, ep.stderr())


# ---------------------------------------------------------------------------------------------
class TickCase(unittest.TestCase):
    """`--tick-once` passes against a scratch root; no HTTP server involved."""

    def setUp(self):
        self.s = ScratchRoot().start()
        self.sup = FakeSupervisor().start()
        self.s.with_supervisor(self.sup).use_fake_runner().write_token(TOKEN)
        self.helper = EndpointCase()
        self.helper.s = self.s

    def tearDown(self):
        self.sup.stop()
        self.s.stop()

    def tick(self, **env):
        rc, out, err = run_cli([sys.executable, ENDPOINT, "--tick-once"], dict(self.s.env, **env), timeout=30)
        self.assertEqual(rc, 0, err)
        return err

    def running_state(self, name, deadline_ago, run_id="run-20260818T050000Z-abcd", prev="ok"):
        st = self.helper.terminal_state(name, prev, published=True)
        st.update(status="running", updated_at=iso_ago(deadline_ago + 600),
                  run={"run_id": run_id, "pid": 999999, "pgid": 999999, "started_at": iso_ago(deadline_ago + 600),
                       "deadline": iso_ago(deadline_ago), "timeout_s": 600, "trigger": "endpoint",
                       "attempt": 1, "model": "fable"})
        self.s.write_job(name, MINIMAL_FM)
        self.s.write_state(name, st)
        return st

    def write_pgid(self, name, pgid, run_id="run-20260818T050000Z-abcd", child=None):
        p = self.s.run_dir / "jobs" / f"{name}.pgid"
        obj = {"pgid": pgid, "pid": pgid, "run_id": run_id, "job": name, "started_at": iso_ago(700),
               "deadline": iso_ago(100)}
        if child is not None:
            obj["child_pgid"] = child
        p.write_text(json.dumps(obj))
        return p


class TestTickDemote(TickCase):
    def test_stuck_running_without_pgid_is_demoted_to_error_lost(self):
        self.running_state("lost-job", deadline_ago=200)
        self.tick()
        st = self.s.read_state("lost-job")
        self.assertEqual((st["status"], st["run"], st["published"]), ("error", None, True))
        r = st["result"]
        self.assertEqual((r["status"], r["reason"], r["judgment_row"], r["headline"]),
                         ("error", "lost", 0, "runner died without reporting"))
        self.assertEqual((r["run_id"], r["trigger"], r["model"], r["cost_usd"], r["attempts"]),
                         ("run-20260818T050000Z-abcd", "endpoint", "fable", 0.0, 1))
        self.assertIsNotNone(r["ended_at"])
        self.assertGreater(r["duration_s"], 700)
        self.assertEqual((st["prev_status"], st["stats"]["run_count"]), ("ok", 4))
        events = [e for e in self.s.job_log("lost-job") if e.get("event") == "demoted_lost"]
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["run_id"], events[0]["by"]), ("run-20260818T050000Z-abcd", "tick"))
        posts = self.sup.find("POST", "/core/api/states/sensor.claude_job_lost_job")
        self.assertTrue(posts)
        self.assertEqual((posts[-1]["json"]["state"], posts[-1]["json"]["attributes"]["reason"]), ("error", "lost"))

    def test_within_margin_is_left_alone(self):
        self.running_state("young", deadline_ago=100)
        self.tick()
        self.assertEqual(self.s.read_state("young")["status"], "running")
        posts = self.sup.find("POST", "/core/api/states/sensor.claude_job_young")
        self.assertTrue(all(p["json"]["state"] == "running" for p in posts))
        self.assertEqual([e for e in self.s.job_log("young") if e.get("event") == "demoted_lost"], [])

    def test_stale_pgid_file_for_another_run_does_not_protect(self):
        self.running_state("other", deadline_ago=200)
        sleeper = subprocess.Popen(["sleep", "30"], start_new_session=True)
        try:
            p = self.write_pgid("other", sleeper.pid, run_id="run-20200101T000000Z-0000")
            self.tick()
            self.assertEqual(self.s.read_state("other")["status"], "error")
            self.assertIsNone(sleeper.poll(), "a foreign run's process group must not be killed")
            self.assertTrue(p.exists(), "a foreign run's pgid file is not ours to remove")
        finally:
            sleeper.kill()
            sleeper.wait()

    def test_lock_busy_warns_then_watchdog_kills_then_demotes_once_lock_frees(self):
        sleeper = subprocess.Popen(["sleep", "60"], start_new_session=True)
        lock_fd = None
        try:
            self.running_state("live-job", deadline_ago=130)
            lock_fd = hold_flock(self.s.state / "live-job.lock")        # a live runner holds the job lock
            pgid_file = self.write_pgid("live-job", sleeper.pid, child=sleeper.pid)
            err = self.tick()
            self.assertIsNone(sleeper.poll())
            self.assertEqual(self.s.read_state("live-job")["status"], "running")
            self.assertIn("past its deadline", err)
            self.assertEqual([e for e in self.s.job_log("live-job") if e.get("event") == "watchdog_kill"], [])
            # past deadline+180 with the lock still busy -> SIGKILL to the run's group(s)
            self.running_state("live-job", deadline_ago=190)
            err = self.tick()
            self.assertTrue(wait_for(lambda: sleeper.poll() is not None, timeout=5))
            self.assertEqual(sleeper.returncode, -signal.SIGKILL)
            kills = [e for e in self.s.job_log("live-job") if e.get("event") == "watchdog_kill"]
            self.assertEqual(len(kills), 1)
            self.assertEqual((kills[0]["pgid"], kills[0]["by"], kills[0]["run_id"]),
                             (sleeper.pid, "tick", "run-20260818T050000Z-abcd"))
            self.assertIn("watchdog SIGKILL", err)
            self.assertEqual(self.s.read_state("live-job")["status"], "running")
            # group gone but lock still busy -> only a warning, no second JSONL line
            err = self.tick()
            self.assertEqual(len([e for e in self.s.job_log("live-job") if e.get("event") == "watchdog_kill"]), 1)
            self.assertIn("nothing to kill", err)
            # the (killed) runner's lock frees -> next pass demotes and drops the stale pgid file
            os.close(lock_fd)
            lock_fd = None
            self.tick()
            st = self.s.read_state("live-job")
            self.assertEqual((st["status"], st["result"]["reason"], st["published"]), ("error", "lost", True))
            self.assertFalse(pgid_file.exists(), "stale pgid file of the demoted run is removed")
            self.assertEqual(len(self.sup.find("POST", "/core/api/states/sensor.claude_job_live_job")),
                             len([p for p in self.sup.find("POST", "/core/api/states/sensor.claude_job_live_job")]))
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
            if sleeper.poll() is None:
                sleeper.kill()
            sleeper.wait()

    def test_free_lock_means_dead_even_if_a_named_group_is_alive(self):
        """The runner holds the flock for its whole life; a free lock is proof it is gone. Groups named
        by the pgid file are NOT killed on this path (they may be recycled ids); `timeout` bounds them."""
        sleeper = subprocess.Popen(["sleep", "30"], start_new_session=True)
        try:
            self.running_state("orphan", deadline_ago=200)
            self.write_pgid("orphan", 999999, child=sleeper.pid)
            self.tick()
            st = self.s.read_state("orphan")
            self.assertEqual((st["status"], st["result"]["reason"]), ("error", "lost"))
            self.assertIsNone(sleeper.poll())
            self.assertEqual([e for e in self.s.job_log("orphan") if e.get("event") == "watchdog_kill"], [])
        finally:
            sleeper.kill()
            sleeper.wait()

    def test_busy_lock_without_pgid_file_is_never_demoted(self):
        self.running_state("held", deadline_ago=500)
        fd = hold_flock(self.s.state / "held.lock")
        try:
            err = self.tick()
        finally:
            os.close(fd)
        self.assertEqual(self.s.read_state("held")["status"], "running")
        self.assertIn("nothing to kill", err)
        self.assertEqual(self.s.job_log("held"), [])

    def test_corrupt_running_state_is_skipped_and_others_still_demoted(self):
        bad = self.running_state("bad-run", deadline_ago=200)
        bad["stats"]["run_count"] = "banana"
        self.s.write_state("bad-run", bad)
        self.running_state("good-run", deadline_ago=200)
        err = self.tick()
        self.assertEqual(self.s.read_state("good-run")["status"], "error")
        self.assertIn("bad-run", err)
        self.assertNotIn("Traceback", err)


class TestTickPublish(TickCase):
    def test_unpublished_is_posted_and_flag_flipped(self):
        self.s.write_job("pend", MINIMAL_FM)
        self.s.write_state("pend", self.helper.terminal_state("pend", "warning", published=False))
        self.tick()
        self.assertTrue(self.s.read_state("pend")["published"])
        posts = self.sup.find("POST", "/core/api/states/sensor.claude_job_pend")
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["json"]["state"], "warning")
        self.assertEqual(posts[0]["headers"]["authorization"], f"Bearer {SUPERVISOR_TOKEN}")
        cost = self.sup.find("POST", "/core/api/states/sensor.claude_jobs_cost_raw")
        self.assertEqual(len(cost), 1)
        self.assertEqual(cost[0]["json"]["attributes"]["unit_of_measurement"], "USD")

    def test_failing_post_leaves_published_false(self):
        self.s.write_job("pend", MINIMAL_FM)
        self.s.write_state("pend", self.helper.terminal_state("pend", "warning", published=False))
        self.sup.route("POST", "/core/api/states/", (500, {"message": "down"}), prefix=True)
        err = self.tick()
        self.assertFalse(self.s.read_state("pend")["published"])
        self.assertIn("publish sensor.claude_job_pend failed", err)

    def test_busy_job_lock_skips_the_flag_flip(self):
        self.s.write_job("busy", MINIMAL_FM)
        self.s.write_state("busy", self.helper.terminal_state("busy", "ok", published=False))
        fd = os.open(str(self.s.state / "busy.lock"), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.tick()
        finally:
            os.close(fd)
        self.assertEqual(len(self.sup.find("POST", "/core/api/states/sensor.claude_job_busy")), 1)
        self.assertFalse(self.s.read_state("busy")["published"], "lock holder owns the file; flip skipped")

    def test_corrupt_state_file_is_skipped_not_fatal(self):
        for name in ("aaa", "zzz"):
            self.s.write_job(name, MINIMAL_FM)
        bad = self.helper.terminal_state("aaa", "ok", published=False)
        bad["stats"]["run_count"] = "banana"
        self.s.write_state("aaa", bad)
        self.s.write_state("zzz", self.helper.terminal_state("zzz", "ok", published=False))
        err = self.tick()
        self.assertTrue(self.s.read_state("zzz")["published"], err)
        self.assertEqual(len(self.sup.find("POST", "/core/api/states/sensor.claude_job_zzz")), 1)
        self.assertEqual(len(self.sup.find("POST", "/core/api/states/sensor.claude_jobs_cost_raw")), 1)
        self.assertIn("state/aaa.json cannot be published", err)
        self.assertNotIn("Traceback", err)

    def test_no_supervisor_token_fails_soft(self):
        self.s.write_job("pend", MINIMAL_FM)
        self.s.write_state("pend", self.helper.terminal_state("pend", "ok", published=False))
        self.sup.route("POST", "/core/api/states/", (401, {"message": "unauthorized"}), prefix=True)
        self.tick(SUPERVISOR_TOKEN="")
        self.assertFalse(self.s.read_state("pend")["published"])


class TestCorruptStateHTTP(EndpointCase):
    @classmethod
    def seed(cls):
        cls.s.write_job("aaa", MINIMAL_FM)
        cls.s.write_job("zzz", dict(MINIMAL_FM, stale_after=86400))

    def test_get_jobs_reports_invalid_state_and_republish_isolates_it(self):
        bad = self.terminal_state("aaa", "ok", published=False)
        bad["stats"]["run_count"] = "banana"
        self.s.write_state("aaa", bad)
        self.s.write_state("zzz", self.terminal_state("zzz", "ok", published=False))
        status, j = self.get("/jobs")
        self.assertEqual(status, 200)
        by = {x["name"]: x for x in j["jobs"]}
        self.assertEqual(by["aaa"]["status"], "invalid_state")
        self.assertEqual((by["aaa"]["valid"], by["aaa"]["slug"], by["aaa"]["stale"]), (False, "aaa", False))
        self.assertTrue(by["aaa"]["headline"].startswith("state file unreadable"))
        self.assertEqual(set(by["aaa"]), set(by["zzz"]), "same keys as a normal jobs[] row")
        self.assertEqual((j["failed_jobs"], j["attention"], j["worst_status"], j["job_count"]),
                         (["aaa"], 1, "invalid_state", 2))
        self.sup.clear()
        self.assertEqual(self.post("/republish", {}), (200, {"republished": 2, "failed": 1}))   # zzz + cost_raw
        self.assertTrue(self.s.read_state("zzz")["published"])
        self.assertIn("state/aaa.json cannot be published", self.ep.stderr())
        self.assertNotIn("Traceback", self.ep.stderr())


class TestHAUnreachable(EndpointCase):
    endpoint_env = {"CLAUDE_JOB_SUPERVISOR_URL": "http://127.0.0.1:1"}

    @classmethod
    def seed(cls):
        for i in range(5):
            cls.s.write_job(f"job-{i}", MINIMAL_FM)

    def test_publish_pass_stops_at_first_transport_failure(self):
        for i in range(5):
            self.s.write_state(f"job-{i}", self.terminal_state(f"job-{i}", "ok", published=False))
        t0 = time.monotonic()
        status, body = self.post("/republish", {})
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertEqual((status, body), (200, {"republished": 0, "failed": 6}))
        err = self.ep.stderr()
        self.assertEqual(err.count("publish pass stopped with 5 state file(s)"), 1, err)
        self.assertFalse(any(self.s.read_state(f"job-{i}")["published"] for i in range(5)))
        # no HA -> no cost seeding, summary still answers (UTC month start)
        self.assertFalse((self.s.state / "_cost.json").exists())
        status, j = self.get("/jobs")
        self.assertEqual((status, j["cost_month_usd"]), (200, 0.0))
        self.assertTrue(j["cost_month_start"].endswith("+00:00"))
        self.assertEqual(self.get("/health", token=None)[1]["status"], "ok")


class TestSpawnFailed(EndpointCase):
    @classmethod
    def seed(cls):
        cls.s.write_job("slow-cadence", dict(INPUT_FM, min_interval=300))
        not_exec = cls.s.bin / "not-executable"
        not_exec.write_text("#!/bin/sh\nexit 0\n")
        os.chmod(not_exec, 0o644)
        cls.endpoint_env = {"CLAUDE_JOB_RUNNER_BIN": str(not_exec)}

    def test_spawn_failure_is_500_and_releases_the_rate_slot(self):
        status, body = self.post("/jobs/slow-cadence/run", {"input": {"date": "2026-08-17"}})
        self.assertEqual((status, body), (500, {"error": "spawn_failed"}))
        self.assertEqual(list(self.s.inbox.iterdir()), [], "inbox file of a failed spawn is removed")
        status, body = self.post("/jobs/slow-cadence/run", {})
        self.assertEqual((status, body), (500, {"error": "spawn_failed"}), "slot released: not rate_limited")
        self.assertIn("cannot start", self.ep.stderr())


class TestCanary(EndpointCase):
    endpoint_env = {"CLAUDE_JOB_CANARY_INTERVAL_S": "0"}

    @classmethod
    def seed(cls):
        cls.s.write_job("one", MINIMAL_FM)
        cls.s.write_job("two", MINIMAL_FM)

    def test_canary_404_forces_full_republish_from_get_jobs(self):
        self.s.write_state("one", self.terminal_state("one", "ok", published=True))
        self.s.write_state("two", self.terminal_state("two", "info", published=True))
        self.sup.clear()
        self.assertEqual(self.get("/jobs")[0], 200)     # canary GET -> 404 (never posted) -> republish all
        gets = self.sup.find("GET", "/core/api/states/sensor.claude_jobs_cost_raw")
        self.assertEqual(len(gets), 1)
        self.assertTrue(wait_for(lambda: len(self.sup.find("POST", "/core/api/states/")) == 3, timeout=5))
        self.assertEqual(len(self.sup.find("POST", "/core/api/states/sensor.claude_job_one")), 1)
        self.assertEqual(len(self.sup.find("POST", "/core/api/states/sensor.claude_job_two")), 1)
        self.assertEqual(len(self.sup.find("POST", "/core/api/states/sensor.claude_jobs_cost_raw")), 1)
        # cost_raw now exists in the fake -> next canary finds it -> no republish
        self.sup.clear()
        self.assertEqual(self.get("/jobs")[0], 200)
        self.assertEqual(len(self.sup.find("GET", "/core/api/states/sensor.claude_jobs_cost_raw")), 1)
        self.assertEqual(self.sup.find("POST", "/core/api/states/"), [])


class TestTickPrune(TickCase):
    def seed_files(self):
        s = self.s
        big = s.logs / "big.jsonl"
        line = json.dumps({"event": "run", "pad": "x" * 1000}) + "\n"
        with open(big, "w") as f:
            for i in range(3100):          # ~3.2 MiB > 2 MiB backstop
                f.write(line)
        small = s.logs / "small.jsonl"
        small.write_text(line * 5)
        now = time.time()
        old_t = s.transcripts / "old.jsonl"
        old_t.write_text("old transcript\n")
        os.utime(old_t, (now - 40 * 86400, now - 40 * 86400))
        fresh_t = s.transcripts / "fresh.jsonl"
        fresh_t.write_text("fresh transcript\n")
        sub = s.transcripts / "memory"
        sub.mkdir()
        (sub / "keep.md").write_text("not a transcript\n")
        return big, small, old_t, fresh_t, sub

    def test_prune_waits_for_first_delay_then_prunes_logs_and_transcripts(self):
        big, small, old_t, fresh_t, sub = self.seed_files()
        self.tick()                                             # default first delay 600 s -> nothing pruned
        self.assertGreater(big.stat().st_size, 2 * 1024 * 1024)
        self.assertTrue(old_t.exists())
        self.tick(CLAUDE_JOB_PRUNE_FIRST_DELAY_S="0")
        with open(big) as f:
            self.assertEqual(sum(1 for _ in f), 200)
        self.assertEqual(small.read_text().count("\n"), 5)
        self.assertFalse(old_t.exists())
        self.assertTrue(fresh_t.exists())
        self.assertTrue((sub / "keep.md").exists())

    def test_transcript_dir_trimmed_oldest_first_to_byte_cap(self):
        now = time.time()
        files = []
        for i, age in enumerate((30, 20, 10)):
            p = self.s.transcripts / f"t{i}.jsonl"
            p.write_text("y" * 600)
            os.utime(p, (now - age, now - age))
            files.append(p)
        self.tick(CLAUDE_JOB_PRUNE_FIRST_DELAY_S="0", CLAUDE_JOB_TRANSCRIPT_MAX_BYTES="1000")
        self.assertEqual([p.exists() for p in files], [False, False, True])


class TestTickTmpCleanup(TickCase):
    def test_old_tmp_and_inbox_files_removed_fresh_kept(self):
        s = self.s
        old = time.time() - 2 * 3600
        victims = [s.state / "x.json.tmp.123", s.logs / "y.jsonl.tmp.9", s.inbox / "run-20200101T000000Z-0000.json",
                   s.disabled / "z.tmp.4"]
        keepers = [s.state / "fresh.json.tmp.1", s.inbox / "run-20990101T000000Z-ffff.json", s.disabled / "someflag",
                   s.logs / "keep.jsonl"]
        for p in victims + keepers:
            p.write_text("{}")
        for p in victims:
            os.utime(p, (old, old))
        os.utime(keepers[2], (old, old))          # old but not a tmp/inbox file -> kept
        os.utime(keepers[3], (old, old))
        self.tick()
        self.assertEqual([p.exists() for p in victims], [False] * len(victims))
        self.assertEqual([p.exists() for p in keepers], [True] * len(keepers))


if __name__ == "__main__":
    unittest.main()
