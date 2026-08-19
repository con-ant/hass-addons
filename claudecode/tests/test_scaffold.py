"""Self-test of the test scaffolding (U0): proves testlib + fakes behave as the other units expect."""
import json
import os
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

from testlib import (BIN_DIR, FAKE_CLAUDE, FAKES_DIR, HEALTH_CHECK_FM, LIB_DIR, MINIMAL_FM,
                     SHARE_DIR, ScratchRoot, run_cli, wait_for)
from fakes.fake_supervisor import FakeSupervisor

PREFLIGHT_FLAGS = ("--setting-sources", "--settings", "--tools", "--json-schema", "--strict-mcp-config")


def http(method, url, body=None, headers=None, timeout=5):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class TestConstants(unittest.TestCase):
    def test_paths_point_into_rootfs(self):
        self.assertTrue(str(BIN_DIR).endswith("claudecode/rootfs/usr/local/bin"))
        self.assertTrue(str(LIB_DIR).endswith("claudecode/rootfs/usr/local/lib/claude-job"))
        self.assertTrue(str(SHARE_DIR).endswith("claudecode/rootfs/usr/share/claudecode"))
        self.assertIn(str(LIB_DIR), sys.path)
        self.assertEqual(MINIMAL_FM["tools"], ["Bash(ha core info)"])
        self.assertNotIn("input", HEALTH_CHECK_FM)
        self.assertNotIn("actions", HEALTH_CHECK_FM)


class TestScratchRoot(unittest.TestCase):
    def test_tree_env_and_helpers(self):
        with ScratchRoot() as s:
            for d in (s.jobs, s.state, s.logs, s.inbox, s.disabled, s.project, s.run_dir / "jobs",
                      s.run_dir / "run", s.transcripts, s.bin, s.home / ".claude"):
                self.assertTrue(d.is_dir(), d)
            self.assertEqual(s.env["CLAUDE_JOB_JOBS_DIR"], str(s.jobs))
            self.assertEqual(s.env["SUPERVISOR_TOKEN"], "test-supervisor-token")
            self.assertTrue(s.env["PATH"].startswith(str(s.bin) + os.pathsep))
            self.assertFalse(os.path.exists(s.env["CLAUDE_JOB_NOTIFY_BIN"]))
            self.assertEqual(json.loads(s.options_file.read_text())["job_default_model"], "opus")
            self.assertEqual(s.built_version_file.read_text().strip(), "2.1.233")
            self.assertTrue(str(s.transcripts).endswith("-data-claude-jobs-project"))

            p = s.write_job("health-check", HEALTH_CHECK_FM, "Check things.\n")
            text = p.read_text()
            self.assertTrue(text.startswith("---\ndescription: Daily health check"))
            self.assertIn("\n---\nCheck things.\n", text)
            s.write_state("health-check", {"schema": 1, "status": "ok"})
            self.assertEqual(s.read_state("health-check")["status"], "ok")
            self.assertIsNone(s.read_state("nope"))
            self.assertTrue(s.write_notify_config({"mobile": {"targets": ["x"]}}).name == "_notify.yaml")

            s.use_fake_notify()
            self.assertTrue(os.path.exists(s.env["CLAUDE_JOB_NOTIFY_BIN"]))
            restore = s.apply_to_process()
            self.assertEqual(os.environ["CLAUDE_JOB_ROOT"], str(s.root))
            restore()
            self.assertNotEqual(os.environ.get("CLAUDE_JOB_ROOT"), str(s.root))
            root = s.root
        self.assertFalse(root.exists())

    def test_start_stop_style(self):
        s = ScratchRoot().start()
        try:
            self.assertTrue(s.jobs.is_dir())
        finally:
            s.stop()


class TestFakeSupervisor(unittest.TestCase):
    def test_defaults_and_recording(self):
        with FakeSupervisor() as sup:
            st, body = http("GET", sup.url + "/core/api/config", headers={"Authorization": "Bearer abc"})
            self.assertEqual(st, 200)
            self.assertEqual(json.loads(body)["time_zone"], "Europe/Vienna")
            self.assertEqual(sup.requests[-1]["headers"]["authorization"], "Bearer abc")

            st, body = http("GET", sup.url + "/core/api/services")
            domains = {d["domain"] for d in json.loads(body)}
            self.assertEqual(domains, {"persistent_notification", "notify", "homeassistant"})

            st, body = http("GET", sup.url + "/addons/self/info")
            self.assertEqual(json.loads(body)["data"]["hostname"], "d7e97e69-claudecode")

            st, _ = http("GET", sup.url + "/core/api/states/sensor.x")
            self.assertEqual(st, 404)
            payload = json.dumps({"state": "ok", "attributes": {"a": 1}}).encode()
            st, _ = http("POST", sup.url + "/core/api/states/sensor.x", payload,
                         {"Content-Type": "application/json"})
            self.assertEqual(st, 201)
            st, _ = http("POST", sup.url + "/core/api/states/sensor.x", payload,
                         {"Content-Type": "application/json"})
            self.assertEqual(st, 200)
            st, body = http("GET", sup.url + "/core/api/states/sensor.x")
            self.assertEqual((st, json.loads(body)["attributes"]), (200, {"a": 1}))
            self.assertEqual(sup.states["sensor.x"]["state"], "ok")
            self.assertEqual(sup.find("POST", "/core/api/states/")[0]["json"]["attributes"], {"a": 1})

            st, body = http("POST", sup.url + "/core/api/services/notify/mobile_app_test_phone", b"{}")
            self.assertEqual((st, json.loads(body)), (200, []))
            st, body = http("POST", sup.url + "/core/api/template", b'{"template":"x"}')
            self.assertEqual((st, body), (200, b"rendered"))
            st, body = http("GET", sup.url + "/core/logs/latest?lines=3")
            self.assertEqual(st, 200)
            self.assertEqual(sup.requests[-1]["query"], "lines=3")
            st, body = http("GET", sup.url + "/nope")
            self.assertEqual((st, json.loads(body)["result"]), (404, "error"))

    def test_route_override_slow_and_stream(self):
        with FakeSupervisor() as sup:
            sup.route("GET", "/core/api/config", (200, {"time_zone": "UTC"}))
            self.assertEqual(json.loads(http("GET", sup.url + "/core/api/config")[1])["time_zone"], "UTC")
            sup.route("POST", "/core/api/states/", (500, {"message": "down"}), prefix=True)
            self.assertEqual(http("POST", sup.url + "/core/api/states/sensor.y", b"{}")[0], 500)
            sup.route("GET", "/core/api/error_log", lambda req: (418, "teapot " + req["query"]))
            self.assertEqual(http("GET", sup.url + "/core/api/error_log?q=1"), (418, b"teapot q=1"))

            sup.route("GET", "/supervisor/info", FakeSupervisor.slow(0.4, then=(200, {"ok": 1})))
            t0 = time.monotonic()
            self.assertEqual(http("GET", sup.url + "/supervisor/info")[0], 200)
            self.assertGreaterEqual(time.monotonic() - t0, 0.35)

            sup.route("GET", "/host/logs", FakeSupervisor.stream(chunk=b"y" * 65536))
            with urllib.request.urlopen(sup.url + "/host/logs", timeout=5) as r:
                got = r.read(200000)          # read a bit, then hang up mid-stream
            self.assertGreaterEqual(len(got), 65536)
            # server survives the client going away
            self.assertEqual(http("GET", sup.url + "/core/api/")[0], 200)

            sup.route("GET", "/core/logs", FakeSupervisor.stream(chunk=b"z" * 1000, total=2500))
            with urllib.request.urlopen(sup.url + "/core/logs", timeout=5) as r:
                self.assertEqual(len(r.read()), 2500)


class TestFakeClaude(unittest.TestCase):
    def setUp(self):
        self.s = ScratchRoot().start()

    def tearDown(self):
        self.s.stop()

    def claude(self, *args, **env):
        e = dict(self.s.env, **{k: str(v) for k, v in env.items()})
        return run_cli([self.s.env["CLAUDE_JOB_CLAUDE_BIN"], *args], e, cwd=self.s.project)

    def test_version_and_help(self):
        rc, out, _ = self.claude("--version")
        self.assertEqual((rc, out.strip()), (0, "2.1.233 (Claude Code)"))
        rc, out, _ = self.claude("--help")
        self.assertEqual(rc, 0)
        for flag in PREFLIGHT_FLAGS + ("--max-budget-usd", "--permission-mode", "--mcp-config",
                                       "--append-system-prompt", "--output-format", "--model"):
            self.assertIn(f"  {flag}", out, flag)
        rc, out, _ = self.claude("--help", FAKE_CLAUDE_HIDE_FLAG="--json-schema,--tools")
        self.assertNotIn("--json-schema", out)
        self.assertNotIn("  --tools", out)
        self.assertIn("--settings", out)

    def test_success_envelope_and_log(self):
        rc, out, err = self.claude("-p", "hello", "--output-format", "json", "--model", "haiku",
                                   "--json-schema", '{"type":"object"}', "--settings", "/x/s.json",
                                   "--max-turns", "50", "--max-budget-usd", "1.5", "--tools", "",
                                   "--strict-mcp-config", "--mcp-config", "/x/m.json",
                                   FAKE_CLAUDE_SCENARIO="success:warning")
        self.assertEqual(rc, 0, err)
        env = json.loads(out)
        for k in ("type", "subtype", "is_error", "num_turns", "session_id", "total_cost_usd", "usage",
                  "modelUsage", "permission_denials", "result", "structured_output", "terminal_reason",
                  "uuid", "duration_ms"):
            self.assertIn(k, env)
        self.assertEqual((env["type"], env["subtype"], env["is_error"]), ("result", "success", False))
        self.assertEqual(env["structured_output"]["status"], "warning")
        self.assertEqual(json.loads(env["result"]), env["structured_output"])
        self.assertEqual(list(env["modelUsage"]), ["claude-haiku-4-5-20251001"])
        self.assertEqual(env["modelUsage"]["claude-haiku-4-5-20251001"]["costUSD"], 0.12)
        self.assertEqual(env["total_cost_usd"], 0.12)

        calls = self.s.fake_claude_calls()
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(c["argv"][:2], ["-p", "hello"])
        self.assertEqual(c["parsed"]["prompt"], "hello")
        self.assertEqual(c["parsed"]["model"], "haiku")
        self.assertEqual(c["parsed"]["tools"], "")
        self.assertEqual(c["parsed"]["max_budget_usd"], "1.5")
        self.assertEqual(c["parsed"]["mcp_config"], "/x/m.json")
        self.assertTrue(c["parsed"]["strict_mcp_config"])
        self.assertEqual(os.path.realpath(c["cwd"]), os.path.realpath(self.s.project))
        self.assertTrue(c["stdin_is_devnull"])
        self.assertFalse(c["stdin_isatty"])
        self.assertIn("FAKE_CLAUDE_SCENARIO", c["env_keys"])
        self.assertEqual(c["env"]["HOME"], str(self.s.home))

    def test_modifiers(self):
        rc, out, _ = self.claude("-p", "x", "--model", "fable", FAKE_CLAUDE_COST="0.5", FAKE_CLAUDE_TURNS="9",
                                 FAKE_CLAUDE_SO='{"status":"ok","headline":"h","actions":["a"]}')
        env = json.loads(out)
        self.assertEqual(env["num_turns"], 9)
        self.assertEqual(env["modelUsage"]["claude-fable-5"]["costUSD"], 0.5)
        self.assertEqual(env["structured_output"]["actions"], ["a"])

    def test_error_scenarios(self):
        rc, out, _ = self.claude("-p", "x", "--max-turns", "3", FAKE_CLAUDE_SCENARIO="max_turns")
        env = json.loads(out)
        self.assertEqual((rc, env["subtype"], env["is_error"]), (1, "error_max_turns", True))
        self.assertEqual(env["errors"], ["Reached maximum number of turns (3)"])
        self.assertNotIn("result", env)
        self.assertNotIn("structured_output", env)

        rc, out, _ = self.claude("-p", "x", FAKE_CLAUDE_SCENARIO="budget")
        self.assertEqual((rc, json.loads(out)["subtype"]), (1, "error_max_budget_usd"))
        rc, out, _ = self.claude("-p", "x", FAKE_CLAUDE_SCENARIO="is_error")
        self.assertEqual((rc, json.loads(out)["errors"]), (1, ["boom"]))
        rc, out, _ = self.claude("-p", "x", FAKE_CLAUDE_SCENARIO="garbage")
        self.assertEqual((rc, out.strip()), (1, "not json"))
        rc, out, _ = self.claude("-p", "x", FAKE_CLAUDE_SCENARIO="exit:7")
        self.assertEqual((rc, out), (7, ""))

    def test_success_variants(self):
        rc, out, _ = self.claude("-p", "x", FAKE_CLAUDE_SCENARIO="denial")
        env = json.loads(out)
        self.assertEqual(env["permission_denials"][0]["tool_name"], "Bash")
        self.assertIn("command", env["permission_denials"][0]["tool_input"])
        rc, out, _ = self.claude("-p", "x", FAKE_CLAUDE_SCENARIO="no_so")
        env = json.loads(out)
        self.assertEqual((rc, env["subtype"], env["structured_output"]), (0, "success", None))
        rc, out, _ = self.claude("-p", "x", FAKE_CLAUDE_SCENARIO="bad_so")
        self.assertEqual(json.loads(out)["structured_output"], {"status": "bogus", "headline": "x"})

    def test_auth_error_once(self):
        rc1, out1, _ = self.claude("-p", "x", FAKE_CLAUDE_SCENARIO="auth_error")
        rc2, out2, _ = self.claude("-p", "x", FAKE_CLAUDE_SCENARIO="auth_error")
        self.assertEqual(rc1, 1)
        self.assertIn("OAuth token has expired", json.loads(out1)["errors"][0])
        self.assertEqual((rc2, json.loads(out2)["subtype"]), (0, "success"))
        self.assertEqual(len(self.s.fake_claude_calls()), 2)

    def test_sleep_and_ignore_term(self):
        t0 = time.monotonic()
        rc, out, _ = self.claude("-p", "x", FAKE_CLAUDE_SCENARIO="sleep:0.5")
        self.assertEqual((rc, json.loads(out)["subtype"]), (0, "success"))
        self.assertGreaterEqual(time.monotonic() - t0, 0.45)

        env = dict(self.s.env, FAKE_CLAUDE_SCENARIO="ignore_term:1.5")
        before = len(self.s.fake_claude_calls())
        p = subprocess.Popen([sys.executable, str(FAKE_CLAUDE), "-p", "x"], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        # the log line is written after SIG_IGN is installed, so it doubles as a readiness signal
        self.assertTrue(wait_for(lambda: len(self.s.fake_claude_calls()) > before, timeout=5))
        p.terminate()
        out, _ = p.communicate(timeout=10)
        self.assertEqual(p.returncode, 0)                    # TERM was ignored
        self.assertEqual(json.loads(out)["subtype"], "success")


class TestOtherFakes(unittest.TestCase):
    def setUp(self):
        self.s = ScratchRoot().start()

    def tearDown(self):
        self.s.stop()

    def test_fake_runner(self):
        s = self.s
        inbox = s.inbox / "run-1.json"
        inbox.write_text(json.dumps({"run_id": "run-1", "job": "j", "input": {"date": "2026-08-17"}}))
        s.use_fake_runner()
        rc, _, _ = run_cli([s.env["CLAUDE_JOB_RUNNER_BIN"], "run", "j", "--run-id", "run-1", "--trigger",
                            "endpoint", "--input-file", inbox], s.env, cwd="/")
        self.assertEqual(rc, 0)
        rec = s.fake_runner_calls()[0]
        self.assertEqual(rec["argv"][:3], ["run", "j", "--run-id"])
        self.assertEqual(rec["inbox"]["input"], {"date": "2026-08-17"})
        self.assertEqual(rec["cwd"], "/")
        for k in ("sid", "pgid", "pid"):
            self.assertIsInstance(rec[k], int)
        rc, _, _ = run_cli([s.env["CLAUDE_JOB_RUNNER_BIN"], "run", "j"], dict(s.env, FAKE_RUNNER_EXIT="3"))
        self.assertEqual(rc, 3)

    def test_fake_notify(self):
        s = self.s
        st = s.write_state("j", {"schema": 1, "job": "j", "status": "warning"})
        s.use_fake_notify()
        rc, out, _ = run_cli([s.env["CLAUDE_JOB_NOTIFY_BIN"], "--job", "j", "--state-file", st], s.env)
        self.assertEqual(rc, 0)
        line = json.loads(out.strip())
        self.assertEqual(line["notify_status"], "sent:persistent")
        self.assertEqual(line["last_notified"]["status"], "warning")
        rec = s.fake_notify_calls()[0]
        self.assertEqual(rec["state"]["status"], "warning")
        self.assertEqual(rec["argv"], ["--job", "j", "--state-file", str(st)])
        rc, out, _ = run_cli([s.env["CLAUDE_JOB_NOTIFY_BIN"], "--state-file", st],
                             dict(s.env, FAKE_NOTIFY_GARBAGE="1", FAKE_NOTIFY_EXIT="5"))
        self.assertEqual(rc, 5)
        self.assertRaises(ValueError, json.loads, out)

    def test_fake_ha(self):
        s = self.s
        rc, out, _ = run_cli([s.env["CLAUDE_JOB_REAL_HA"], "--endpoint", "http://127.0.0.1:1",
                              "--api-token", "n", "core", "info"], s.env)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), ["--endpoint", "http://127.0.0.1:1", "--api-token", "n", "core", "info"])
        self.assertEqual(s.fake_ha_calls()[0]["argv"][-2:], ["core", "info"])

    def test_fast_timeout(self):
        s = self.s
        s.use_fast_timeout(seconds=1, kill_after=1)
        t0 = time.monotonic()
        rc, _, _ = run_cli([s.env["CLAUDE_JOB_TIMEOUT_BIN"], "--signal=TERM", "-k", "15", "600",
                            "sleep", "30"], s.env, timeout=20)
        self.assertEqual(rc, 124)
        self.assertLess(time.monotonic() - t0, 4)
        # passes through a fast command's own exit code
        rc, out, _ = run_cli([s.env["CLAUDE_JOB_TIMEOUT_BIN"], "--signal=TERM", "-k", "15", "600",
                              "sh", "-c", "echo hi; exit 3"], s.env)
        self.assertEqual((rc, out.strip()), (3, "hi"))
        # -k escalation: TERM ignored -> KILL -> 137
        rc, _, _ = run_cli([s.env["CLAUDE_JOB_TIMEOUT_BIN"], "--signal=TERM", "-k", "15", "600",
                            sys.executable, str(FAKE_CLAUDE), "-p", "x"],
                           dict(s.env, FAKE_CLAUDE_SCENARIO="ignore_term:30"), timeout=20)
        # GNU timeout signals its whole process group (itself included) unless --foreground, so a
        # Python parent sees returncode -9 where a shell would report 137. The runner must map both.
        self.assertIn(rc, (137, -9))


if __name__ == "__main__":
    unittest.main()
