"""Cross-unit end-to-end tests: the seams no single unit owns.

1. endpoint POST /run (with input) -> real claude-job -> real broker -> fake claude -> real
   claude-job-notify -> state / entities / GET /jobs / detail / cleanup.
2. disable / enable / rate-limit round trip through the endpoint with the real runner.
3./4. the stop-path contract (design §4.7, Appendix A.1/A.2): a runner whose process group is
   TERMed (alone, or just after its claude child group as `stop_sessions` may do) publishes
   `aborted` / judgment_row 2, exits 143, never calls the notifier, and is done well inside 5 s.
"""
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

from testlib import ENDPOINT, FAKE_CLAUDE, HEALTH_CHECK_FM, RUNNER, ScratchRoot, pgroup_dead, wait_for
from fakes.fake_supervisor import FakeSupervisor

TOKEN = "cd" * 32
JOB = "energy-report"
INPUT_FM = dict(HEALTH_CHECK_FM, input={"date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$",
                                                 "description": "day to report on"}})


def bake_claude(s: ScratchRoot, scenario: str, **extra) -> None:
    """Rewrite bin/claude so fake_claude sees its knobs behind the runner's `env -i`."""
    env = {k: v for k, v in s.env.items() if k.startswith("FAKE_CLAUDE_")}
    env.update({"FAKE_CLAUDE_SCENARIO": scenario}, **{k: str(v) for k, v in extra.items()})
    lines = ["#!/bin/sh"] + [f"export {k}={shlex.quote(v)}" for k, v in sorted(env.items())]
    lines.append(f'exec {shlex.quote(sys.executable)} {shlex.quote(str(FAKE_CLAUDE))} "$@"')
    s.claude_wrapper.write_text("\n".join(lines) + "\n")
    s.claude_wrapper.chmod(0o755)


def leftovers(s: ScratchRoot) -> list:
    return sorted(os.listdir(s.run_dir / "jobs")) + sorted(os.listdir(s.run_dir / "run"))


class EndpointCase(unittest.TestCase):
    """Scratch volume + fake Supervisor + a real endpoint (no tick) wired to the REAL runner and notifier."""

    def setUp(self):
        self.s = ScratchRoot().start()
        self.sup = FakeSupervisor().start()
        self.s.with_supervisor(self.sup).use_real_runner().use_real_notify()
        self.s.write_job(JOB, INPUT_FM, "Report on the energy use.\n")
        self.s.write_token(TOKEN)
        bake_claude(self.s, "success:warning", FAKE_CLAUDE_COST="0.12")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.ep = subprocess.Popen([sys.executable, str(ENDPOINT), "--bind", "127.0.0.1", "--port", str(self.port),
                                    "--no-tick"], env=self.s.env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        self.assertTrue(wait_for(lambda: self.req("GET", "/health", auth=False)[0] == 200, timeout=15))

    def tearDown(self):
        self.ep.terminate()
        self.ep.communicate(timeout=5)
        self.sup.stop()
        self.s.stop()

    def req(self, method, path, body=None, auth=True):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {TOKEN}"} if auth else {}
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method, data=data, headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()
        except OSError:
            return None, ""

    def wait_run_done(self, status):
        """Terminal state written, notifier folded in, and every per-run file gone (step 14 ran)."""
        def done():
            st = self.s.read_state(JOB) or {}
            return (st.get("status") == status and (st.get("notify") or {}).get("notify_status") is not None
                    and not leftovers(self.s))
        self.assertTrue(wait_for(done, timeout=30), f"run did not finish: {self.s.read_state(JOB)} {leftovers(self.s)}")
        return self.s.read_state(JOB)

    # -- 1 ---------------------------------------------------------------------------------------
    def test_run_with_input_through_every_component(self):
        code, body = self.req("POST", f"/jobs/{JOB}/run", {"input": {"date": "2026-08-17"}})
        self.assertEqual(code, 202, body)
        run_id = json.loads(body)["run_id"]
        st = self.wait_run_done("warning")
        self.assertTrue(st["published"])
        self.assertEqual(st["result"]["run_id"], run_id)
        self.assertEqual(st["result"]["trigger"], "endpoint")
        self.assertEqual(st["result"]["input"], {"date": "2026-08-17"})
        self.assertEqual(st["result"]["judgment_row"], 13)
        self.assertTrue(st["notify"]["notify_status"].startswith("sent:"), st["notify"])
        self.assertEqual(st["notify"]["last_notified"]["status"], "warning")
        # the input reached the prompt, the inbox hand-off was consumed
        calls = self.s.fake_claude_calls()
        self.assertEqual(len(calls), 1)
        self.assertIn("2026-08-17", calls[0]["parsed"]["prompt"])
        self.assertEqual(os.listdir(self.s.inbox), [])
        # HA saw: running + terminal entity, cost_raw, the notifier's service calls
        paths = [r["path"] for r in self.sup.find("POST")]
        self.assertIn("/core/api/states/sensor.claude_job_energy_report", paths)
        self.assertIn("/core/api/states/sensor.claude_jobs_cost_raw", paths)
        self.assertTrue(any(p.startswith("/core/api/services/") for p in paths), paths)
        self.assertEqual(self.sup.states["sensor.claude_job_energy_report"]["state"], "warning")
        # GET /jobs and /detail agree with the state file
        code, body = self.req("GET", "/jobs")
        summary = json.loads(body)
        job = [j for j in summary["jobs"] if j["name"] == JOB][0]
        self.assertEqual((job["status"], job["last_run"], job["run_count"]), ("warning", st["result"]["ended_at"], 1))
        self.assertEqual(summary["worst_status"], "warning")
        self.assertAlmostEqual(summary["cost_month_usd"], 0.12, places=2)
        code, detail = self.req("GET", f"/jobs/{JOB}/detail")
        self.assertEqual((code, detail), (200, st["result"]["detail"]))

    # -- 2 ---------------------------------------------------------------------------------------
    def test_disable_enable_rate_limit_round_trip(self):
        code, body = self.req("POST", f"/jobs/{JOB}/disable", {})
        self.assertEqual((code, json.loads(body)), (200, {"job": JOB, "enabled": False}))
        self.assertEqual(os.listdir(self.s.disabled), ["energy_report"])
        code, body = self.req("POST", f"/jobs/{JOB}/run", {})
        self.assertEqual((code, json.loads(body).get("reason"), json.loads(body).get("gate")), (200, "disabled", "flag"))
        # the real runner honours the same flag from the terminal (exit 3, nothing spawned)
        rc = subprocess.run([sys.executable, str(RUNNER), "run", JOB], env=self.s.env, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=30).returncode
        self.assertEqual(rc, 3)
        self.assertEqual(self.s.fake_claude_calls(), [])
        code, body = self.req("POST", f"/jobs/{JOB}/enable", {})
        self.assertEqual((code, json.loads(body)), (200, {"job": JOB, "enabled": True}))
        self.assertEqual(os.listdir(self.s.disabled), [])
        code, body = self.req("POST", f"/jobs/{JOB}/run", {})
        self.assertEqual(code, 202, body)
        code, body = self.req("POST", f"/jobs/{JOB}/run", {})
        self.assertEqual((code, json.loads(body).get("reason")), (200, "rate_limited"))
        self.assertGreaterEqual(json.loads(body)["retry_after_s"], 1)
        st = self.wait_run_done("warning")
        self.assertTrue(st["enabled"])
        self.assertEqual(len(self.s.fake_claude_calls()), 1)      # the rate-limited trigger spawned nothing


class StopPathCase(unittest.TestCase):
    """The lead's bash TERMs the runner's process group (from RUN_DIR/jobs/<name>.pgid); the runner
    must relay to claude, publish `aborted` (row 2), skip the notifier and exit 143 within the budget."""

    def setUp(self):
        self.s = ScratchRoot().start()
        self.sup = FakeSupervisor().start()
        self.s.with_supervisor(self.sup).use_fake_notify()
        self.s.write_job("health-check", HEALTH_CHECK_FM, "Check the house.\n")
        bake_claude(self.s, "sleep:30")

    def tearDown(self):
        self.sup.stop()
        self.s.stop()

    def start_run(self):
        proc = subprocess.Popen([sys.executable, str(RUNNER), "run", "health-check"], env=self.s.env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        pgid_file = self.s.run_dir / "jobs" / "health-check.pgid"
        read = lambda: json.loads(pgid_file.read_text()) if pgid_file.exists() else {}   # noqa: E731
        self.assertTrue(wait_for(lambda: read().get("child_pgid"), timeout=20), "claude never spawned")
        self.assertTrue(wait_for(lambda: self.s.fake_claude_calls(), timeout=10))     # claude is really up
        info = read()
        self.assertEqual(info["pgid"], proc.pid)
        return proc, info

    def assert_aborted(self, proc, t0):
        try:
            rc = proc.wait(10)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
        elapsed = time.monotonic() - t0
        out, err = proc.communicate(timeout=5)
        res = (self.s.read_state("health-check") or {}).get("result") or {}
        self.assertEqual((res.get("status"), res.get("reason"), res.get("judgment_row"), rc),
                         ("aborted", "addon_stopping", 2, 143), err[-600:])
        self.assertLess(elapsed, 5.0)
        self.assertEqual(self.s.fake_notify_calls(), [])                # A17: no notifier on the TERM path
        self.assertEqual((self.s.read_state("health-check") or {}).get("notify", {}).get("notify_status"),
                         "skipped_aborting")
        self.assertEqual(leftovers(self.s), [])                          # pgid file + run files cleaned
        self.assertEqual(self.sup.states["sensor.claude_job_health_check"]["state"], "aborted")

    # -- 3 ---------------------------------------------------------------------------------------
    def test_term_runner_group_only(self):
        proc, info = self.start_run()
        t0 = time.monotonic()
        os.killpg(info["pgid"], signal.SIGTERM)          # what signal_job_runs does with .pgid
        self.assert_aborted(proc, t0)
        self.assertTrue(pgroup_dead(info["child_pgid"]))  # the runner took claude's group down with it

    # -- 4 ---------------------------------------------------------------------------------------
    def test_term_child_group_then_runner_group(self):
        proc, info = self.start_run()
        t0 = time.monotonic()
        os.killpg(info["child_pgid"], signal.SIGTERM)    # stop_sessions reaching claude first ...
        time.sleep(0.05)
        os.killpg(info["pgid"], signal.SIGTERM)          # ... then the runner: still an add-on stop, not row 4
        self.assert_aborted(proc, t0)


if __name__ == "__main__":
    unittest.main()
