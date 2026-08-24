"""U3 — the runner `claude-job` (breakdown §3 U3 test list; design §4.3/§4.6/A.2/A.8).

Every test drives the real executable against a ScratchRoot volume, the FakeSupervisor and the
REAL broker; `claude` is fakes/fake_claude.py behind a per-test wrapper that bakes the scenario
into the child's environment (the runner's `env -i` strips the test process's FAKE_* variables).
Assertions are on `(state.status, result.reason, result.judgment_row, exit code, entity POST)`.
"""
import fcntl
import importlib.machinery
import importlib.util
import json
import os
import shlex
import signal
import stat
import subprocess
import sys
import time
import unittest

from testlib import (FAKE_CLAUDE, HEALTH_CHECK_FM, MINIMAL_FM, RUNNER, SHARE_DIR, ScratchRoot,
                     pgroup_dead, run_cli, wait_for)
from fakes.fake_supervisor import FakeSupervisor
import jobdef  # LIB_DIR is on sys.path via testlib

JOB = "health-check"
ENTITY = "sensor.claude_job_health_check"
STATES = "/core/api/states/"
CHILD_ENV_KEYS = {"HOME", "PATH", "TERM", "LANG", "LC_ALL", "TZ", "CLAUDE_CONFIG_DIR",
                  "CLAUDE_JOB_BROKER_PORT", "CLAUDE_JOB_BROKER_NONCE"}


def load_runner_module():
    """Import the extension-less executable as a module to unit-test its pure functions."""
    loader = importlib.machinery.SourceFileLoader("claude_job_runner", str(RUNNER))
    spec = importlib.util.spec_from_loader("claude_job_runner", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class RunnerCase(unittest.TestCase):
    """Scratch volume + fake supervisor + fake claude, torn down per test."""

    def setUp(self):
        self.s = ScratchRoot().start()
        self.sup = FakeSupervisor().start()
        self.s.with_supervisor(self.sup)
        self.s.setenv(CLAUDE_JOB_AUTH_RETRY_BASE_S="0", CLAUDE_JOB_AUTH_RETRY_JITTER_S="0")
        self.scenario("success")
        self.s.write_job(JOB, HEALTH_CHECK_FM, "Check the house.\n")

    def tearDown(self):
        self.sup.stop()
        self.s.stop()

    # -- helpers ------------------------------------------------------------------------------
    def scenario(self, name, **extra):
        """Rewrite bin/claude so the fake sees its knobs even behind the runner's `env -i`."""
        env = {k: v for k, v in self.s.env.items() if k.startswith("FAKE_CLAUDE_")}
        env.update({"FAKE_CLAUDE_SCENARIO": name}, **{k: str(v) for k, v in extra.items()})
        self.s.env.update(env)
        lines = ["#!/bin/sh"] + [f"export {k}={shlex.quote(v)}" for k, v in sorted(env.items())]
        lines.append(f"exec {shlex.quote(sys.executable)} {shlex.quote(str(FAKE_CLAUDE))} \"$@\"")
        self.s.claude_wrapper.write_text("\n".join(lines) + "\n")
        self.s.claude_wrapper.chmod(0o755)

    def cli(self, *argv, timeout=60, env=None):
        return run_cli([sys.executable, RUNNER, *argv], env or self.s.env, timeout=timeout)

    def run_job(self, *extra, name=JOB, verb="run", timeout=60, env=None):
        return self.cli(verb, name, *extra, timeout=timeout, env=env)

    def spawn_job(self, *extra, name=JOB, verb="run", env=None):
        return subprocess.Popen([sys.executable, str(RUNNER), verb, name, *extra], env=env or self.s.env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def state(self, name=JOB):
        return self.s.read_state(name)

    def result(self, name=JOB):
        return (self.state(name) or {}).get("result") or {}

    def posts(self, entity=ENTITY):
        return [r["json"] for r in self.sup.find("POST", STATES + entity)]

    def last_post(self, entity=ENTITY):
        posts = self.posts(entity)
        self.assertTrue(posts, f"no POST to {entity}")
        return posts[-1]

    def hold_lock(self, path, body=None):
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if body is not None:
            os.ftruncate(fd, 0)
            os.write(fd, json.dumps(body).encode())
        self.addCleanup(os.close, fd)
        return fd

    def assertOutcome(self, rc, want_rc, status, reason, row, name=JOB):
        st = self.state(name)
        self.assertIsNotNone(st, "no state file")
        got = (rc, st["status"], st["result"].get("reason"), st["result"].get("judgment_row"))
        self.assertEqual(got, (want_rc, status, reason, row))
        self.assertEqual(self.last_post()["state"], status)


# ---------------------------------------------------------------------------------------------
class TestJudgmentRows(RunnerCase):
    def test_row13_clean_success_full_contract(self):
        rc, out, err = self.run_job()
        self.assertOutcome(rc, 0, "ok", None, 13)
        st, res = self.state(), self.result()
        # stdout summary line (2g)
        self.assertRegex(out.strip(), r"^health-check: ok — all good \(run-\d{8}T\d{6}Z-[0-9a-f]{4} · [\d.]+s · \$0\.12 · row 13\)$")
        # state file (2b)
        self.assertEqual((st["schema"], st["job"], st["slug"], st["enabled"], st["published"], st["run"]),
                         (1, JOB, "health_check", True, True, None))
        self.assertEqual(st["description"], HEALTH_CHECK_FM["description"])
        self.assertEqual(st["stale_after"], 93600)
        self.assertEqual(st["stats"]["run_count"], 1)
        self.assertEqual(st["notify"], {"channels": [], "notify_status": "skipped_no_notifier", "last_notified": None})
        for k, v in {"status": "ok", "headline": "all good", "detail": "d", "envelope_subtype": "success",
                     "is_error": False, "exit_code": 0, "hard_killed": False, "cost_usd": 0.12,
                     "over_budget": False, "model": "opus", "model_resolved": "claude-opus-5",
                     "trigger": "manual", "attempts": 1, "retried_auth": False, "claude_version": "2.1.233",
                     "num_turns": 4, "metrics": {"n": 1}, "input": None}.items():
            self.assertEqual(res[k], v, k)
        self.assertRegex(res["run_id"], r"^run-\d{8}T\d{6}Z-[0-9a-f]{4}$")
        self.assertTrue(res["session_id"])
        # entity POSTs: running first (minimal attrs), then terminal (2d)
        posts = self.posts()
        self.assertEqual([p["state"] for p in posts], ["running", "ok"])
        self.assertEqual(set(posts[0]["attributes"]), {"job", "started_at", "timeout_s", "run_id", "trigger",
                                                        "prev_status", "enabled", "friendly_name", "icon"})
        attrs = posts[1]["attributes"]
        self.assertEqual((attrs["headline"], attrs["metrics"], attrs["cost_usd"], attrs["run_count"],
                          attrs["notify_status"]), ("all good", {"n": 1}, 0.12, 1, "skipped_no_notifier"))
        self.assertNotIn("input", attrs)
        self.assertNotIn("actions_selected", attrs)
        auth = {r["headers"].get("authorization") for r in self.sup.find("POST", STATES)}
        self.assertEqual(auth, {"Bearer test-supervisor-token"})
        # cost rollup + cost_raw entity (2e)
        cost = self.s.read_json(self.s.state / "_cost.json")
        self.assertEqual((cost["runs"], cost["total_usd"], cost["by_job"], cost["time_zone"]),
                         (1, 0.12, {JOB: 0.12}, "Europe/Vienna"))
        self.assertEqual(self.last_post("sensor.claude_jobs_cost_raw")["state"], "0.12")
        # the fake claude saw exactly the §4.4 cage
        calls = self.s.fake_claude_calls()
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(c["cwd"], str(self.s.project))
        self.assertTrue(c["stdin_is_devnull"])
        # PWD comes from the wrapper's /bin/sh; PYTHON*/TYPEGUARD_* from this box's interpreter start-up.
        env_keys = {k for k in c["env_keys"] if not k.startswith(("FAKE_CLAUDE_", "PYTHON", "TYPEGUARD_"))} - {"PWD"}
        # proxy/CA variables pass through only when the add-on itself has them (jobdef.NETWORK_ENV_PASSTHROUGH)
        env_keys -= set(jobdef.NETWORK_ENV_PASSTHROUGH)
        self.assertEqual(env_keys, CHILD_ENV_KEYS)
        self.assertEqual(c["env"]["TZ"], "Europe/Vienna")
        self.assertEqual(c["env"]["CLAUDE_CONFIG_DIR"], str(self.s.job_config))
        self.assertRegex(c["env"]["CLAUDE_JOB_BROKER_PORT"], r"^\d+$")
        self.assertEqual(len(c["env"]["CLAUDE_JOB_BROKER_NONCE"]), 64)
        self.assertNotIn("SUPERVISOR_TOKEN", c["env"])
        p = c["parsed"]
        run_files = str(self.s.run_dir / "run")
        self.assertEqual((p["model"], p["output_format"], p["permission_mode"], p["setting_sources"], p["tools"],
                          p["max_turns"], p["max_budget_usd"], p["strict_mcp_config"]),
                         ("opus", "json", "dontAsk", "", "Bash,Read,Grep,Glob", "50", "1.50", True))
        self.assertEqual(p["settings"], f"{run_files}/{JOB}.settings.json")
        self.assertEqual(p["mcp_config"], f"{run_files}/{JOB}.mcp.json")
        self.assertEqual(json.loads(p["json_schema"])["properties"]["status"]["enum"], ["ok", "info", "warning", "critical"])
        self.assertEqual(p["append_system_prompt"], (SHARE_DIR / "job-contract.md").read_text())
        self.assertIn("Check the house.", p["prompt"])
        self.assertNotIn("<job-input>", p["prompt"])
        # JSONL run record with elided argv (2c)
        rec = [ln for ln in self.s.job_log(JOB) if ln["event"] == "run"][-1]
        self.assertEqual((rec["judgment_row"], rec["state"], rec["exit"], rec["attempts"], rec["denials"]), (13, "ok", 0, 1, 0))
        argv = rec["argv"]
        self.assertRegex(argv[argv.index("-p") + 1], r"^<prompt sha256=[0-9a-f]{12}… len=\d+>$")
        self.assertRegex(argv[argv.index("--json-schema") + 1], r"^<schema len=\d+>$")
        self.assertRegex(argv[argv.index("--append-system-prompt") + 1], r"^<contract len=\d+>$")
        self.assertEqual(argv[:2], ["env", "-i"])
        self.assertIn("CLAUDE_JOB_BROKER_NONCE=<redacted>", argv)
        self.assertFalse(any(a.startswith("CLAUDE_JOB_BROKER_NONCE=") and a != "CLAUDE_JOB_BROKER_NONCE=<redacted>" for a in argv))
        self.assertEqual(rec["envelope"]["subtype"], "success")
        # cleanup (step 14): nothing left under RUN_DIR
        self.assertEqual(list((self.s.run_dir / "run").iterdir()), [])
        self.assertEqual(list((self.s.run_dir / "jobs").iterdir()), [])
        self.assertNotIn("[claude-job]", err)              # a clean run has nothing to say (the broker may)

    def test_job_config_dir_and_tool_results_lifecycle(self):
        """Staging builds the job-only CLAUDE_CONFIG_DIR (credentials symlink, 0700) and sweeps
        spooled tool-results left by a hard-killed run; finish() purges the run's own."""
        leftover = self.s.transcripts / "dead-session" / "tool-results"
        leftover.mkdir(parents=True)
        (leftover / "old.txt").write_text("stale spool")
        creds = self.s.persist / ".credentials.json"
        creds.parent.mkdir(parents=True, exist_ok=True)
        creds.write_text('{"claudeAiOauth": {"accessToken": "tok"}}')
        rc, _, _ = self.run_job()
        self.assertEqual(rc, 0)
        link = self.s.job_config / ".credentials.json"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(str(link)), str(creds))
        self.assertEqual(stat.S_IMODE(os.stat(self.s.job_config).st_mode), 0o700)
        self.assertFalse(leftover.exists())

    def test_row13_contract_status_verbatim_and_json_stdout(self):
        self.scenario("success:critical", FAKE_CLAUDE_HEADLINE="pump dead")
        rc, out, _ = self.run_job("--json", "--trigger", "endpoint")
        self.assertOutcome(rc, 0, "critical", None, 13)
        res = json.loads(out)
        self.assertEqual((res["status"], res["headline"], res["trigger"]), ("critical", "pump dead", "endpoint"))
        self.assertEqual(self.state()["notify"]["channels"], ["mobile_critical", "persistent"])

    def test_row11_denials_escalate_and_prefix(self):
        self.scenario("denial")
        rc, _, _ = self.run_job()
        self.assertOutcome(rc, 0, "warning", None, 11)
        res = self.result()
        self.assertTrue(res["headline"].startswith("[denied: Bash×1] all good"), res["headline"])
        self.assertIn("\n\n---\nDenied tool calls:\n- Bash: ", res["detail"])
        self.assertEqual(res["permission_denials"][0]["tool_name"], "Bash")
        self.assertEqual(self.last_post()["attributes"]["headline"], res["headline"])
        self.assertNotIn("permission_denials", self.last_post()["attributes"])

    def test_row11_never_downgrades(self):
        self.scenario("denial", FAKE_CLAUDE_SO=json.dumps({"status": "critical", "headline": "x"}))
        rc, _, _ = self.run_job()
        self.assertOutcome(rc, 0, "critical", None, 11)

    def test_row12_over_budget(self):
        self.scenario("success", FAKE_CLAUDE_COST="2.5")
        rc, _, _ = self.run_job()
        self.assertOutcome(rc, 0, "warning", None, 12)
        self.assertEqual(self.result()["headline"], "[over budget] all good")
        self.assertTrue(self.result()["over_budget"])

    def test_row10_schema_revalidation(self):
        self.scenario("bad_so")
        rc, _, _ = self.run_job()
        self.assertOutcome(rc, 1, "error", "schema", 10)
        self.assertTrue(self.result()["headline"].startswith("result failed schema validation: status:"), self.result()["headline"])

    def test_row9_no_structured_output(self):
        self.scenario("no_so")
        rc, _, _ = self.run_job()
        self.assertOutcome(rc, 1, "error", "no_result", 9)
        self.assertEqual(self.result()["headline"], "completed without submitting a result")
        self.assertIn("could not produce", self.result()["result_tail"])

    def test_row8_is_error(self):
        self.scenario("is_error")
        rc, _, _ = self.run_job()
        self.assertOutcome(rc, 1, "error", "run_failed", 8)
        self.assertEqual(self.result()["headline"], "run failed: boom")
        self.assertEqual((self.result()["attempts"], self.result()["retried_auth"]), (1, False))
        self.assertEqual(len(self.s.fake_claude_calls()), 1)

    def test_row8_auth_retry_once_then_success(self):
        self.scenario("auth_error")
        rc, _, _ = self.run_job()
        self.assertOutcome(rc, 0, "ok", None, 13)
        self.assertEqual(len(self.s.fake_claude_calls()), 2)
        self.assertEqual((self.result()["attempts"], self.result()["retried_auth"]), (2, True))
        events = [ln["event"] for ln in self.s.job_log(JOB)]
        self.assertEqual(events, ["auth_retry", "run"])
        self.assertEqual(self.s.read_json(self.s.state / "_cost.json")["runs"], 1)

    def test_row7_budget_cap(self):
        self.scenario("budget")
        rc, _, _ = self.run_job()
        self.assertOutcome(rc, 1, "error", "max_budget", 7)
        self.assertEqual(self.result()["headline"], "stopped at budget cap $1.50")

    def test_row6_max_turns(self):
        self.scenario("max_turns")
        rc, _, _ = self.run_job()
        self.assertOutcome(rc, 1, "error", "max_turns", 6)
        self.assertEqual(self.result()["headline"], "hit max turns (50) before finishing")
        self.assertEqual(self.result()["envelope_subtype"], "error_max_turns")

    def test_row5_garbage_stdout(self):
        self.scenario("garbage")
        rc, _, _ = self.run_job()
        self.assertOutcome(rc, 1, "error", "no_envelope", 5)
        self.assertEqual(self.result()["headline"], "no result envelope (exit 1)")
        self.assertIn("not json", self.result()["raw_tail"])
        self.assertIsNone(self.s.read_json(self.s.state / "_cost.json"))   # no envelope, no cost run

    def test_row5_silent_exit(self):
        self.scenario("exit:3")
        rc, _, _ = self.run_job()
        self.assertOutcome(rc, 1, "error", "no_envelope", 5)
        self.assertEqual(self.result()["exit_code"], 3)

    def test_row3_timeout(self):
        self.scenario("sleep:30")
        self.s.use_fast_timeout(seconds=1, kill_after=1)
        self.s.write_job(JOB, dict(HEALTH_CHECK_FM, timeout=30))
        t0 = time.monotonic()
        rc, _, _ = self.run_job()
        self.assertLess(time.monotonic() - t0, 10)
        self.assertOutcome(rc, 1, "error", "timeout", 3)
        self.assertEqual(self.result()["headline"], "timed out after 30s")
        self.assertEqual(self.result()["exit_code"], 124)

    def test_row4_external_kill_of_the_claude_group(self):
        self.scenario("sleep:30")
        proc = self.spawn_job()
        pgid_file = self.s.run_dir / "jobs" / f"{JOB}.pgid"
        self.assertTrue(wait_for(lambda: "child_pgid" in (self.s.read_json(pgid_file) or {}), timeout=15))
        body = self.s.read_json(pgid_file)
        self.assertEqual({"pgid", "pid", "run_id", "job", "phase", "started_at", "deadline", "child_pgid"}, set(body))
        self.assertEqual(body["phase"], "running")
        self.assertTrue(wait_for(lambda: self.s.fake_claude_calls(), timeout=10))
        os.killpg(body["child_pgid"], signal.SIGKILL)
        out, err = proc.communicate(timeout=20)
        self.assertOutcome(proc.returncode, 1, "aborted", "killed", 4)
        self.assertRegex(self.result()["headline"], r"^killed externally after \d+s$")
        self.assertEqual((self.result()["exit_code"], self.result()["hard_killed"]), (137, True))
        self.assertFalse(pgid_file.exists())


# ---------------------------------------------------------------------------------------------
class TestAbort(RunnerCase):
    def test_term_during_run_aborts_within_budget(self):
        self.scenario("ignore_term:30")                  # claude ignores TERM → runner must KILL the group
        self.s.use_fake_notify()                          # must NOT be called on the TERM path (A17)
        proc = self.spawn_job()
        pgid_file = self.s.run_dir / "jobs" / f"{JOB}.pgid"
        self.assertTrue(wait_for(lambda: self.s.fake_claude_calls(), timeout=15))
        self.assertTrue(wait_for(lambda: "child_pgid" in (self.s.read_json(pgid_file) or {}), timeout=5))
        child_pgid = self.s.read_json(pgid_file)["child_pgid"]
        t0 = time.monotonic()
        os.kill(proc.pid, signal.SIGTERM)
        proc.communicate(timeout=20)
        took = time.monotonic() - t0
        self.assertLess(took, 4.5, f"abort took {took:.1f}s")
        self.assertOutcome(proc.returncode, 143, "aborted", "addon_stopping", 2)
        self.assertRegex(self.result()["headline"], r"^aborted after \d+s: add-on stopping$")
        self.assertEqual(self.state()["notify"]["notify_status"], "skipped_aborting")
        self.assertEqual(self.s.fake_notify_calls(), [])
        self.assertFalse(pgid_file.exists())
        self.assertEqual(list((self.s.run_dir / "run").iterdir()), [])
        self.assertTrue(pgroup_dead(child_pgid))         # gone, or only unreaped zombies left
        self.assertEqual([ln["event"] for ln in self.s.job_log(JOB)], ["run"])

    def test_stop_path_signalling_child_and_runner_together_is_row2(self):
        """integration HIGH #1: claude's group dies from the stop path's TERM a moment before ours arrives."""
        self.scenario("sleep:30")
        self.s.use_fake_notify()
        proc = self.spawn_job()
        pgid_file = self.s.run_dir / "jobs" / f"{JOB}.pgid"
        self.assertTrue(wait_for(lambda: self.s.fake_claude_calls(), timeout=15))
        self.assertTrue(wait_for(lambda: "child_pgid" in (self.s.read_json(pgid_file) or {}), timeout=5))
        t0 = time.monotonic()
        os.killpg(self.s.read_json(pgid_file)["child_pgid"], signal.SIGTERM)
        time.sleep(0.05)
        os.kill(proc.pid, signal.SIGTERM)
        proc.communicate(timeout=20)
        self.assertLess(time.monotonic() - t0, 4.0)
        self.assertOutcome(proc.returncode, 143, "aborted", "addon_stopping", 2)
        self.assertEqual(self.state()["notify"]["notify_status"], "skipped_aborting")
        self.assertEqual(self.s.fake_notify_calls(), [])
        self.assertEqual(self.result()["exit_code"], 143)

    def test_child_killed_without_a_stop_signal_stays_row4_promptly(self):
        self.scenario("sleep:30")
        proc = self.spawn_job()
        pgid_file = self.s.run_dir / "jobs" / f"{JOB}.pgid"
        self.assertTrue(wait_for(lambda: "child_pgid" in (self.s.read_json(pgid_file) or {}), timeout=15))
        self.assertTrue(wait_for(lambda: self.s.fake_claude_calls(), timeout=10))
        os.killpg(self.s.read_json(pgid_file)["child_pgid"], signal.SIGTERM)
        proc.communicate(timeout=20)
        self.assertOutcome(proc.returncode, 1, "aborted", "killed", 4)
        self.assertEqual((self.result()["exit_code"], self.result()["hard_killed"]), (143, False))

    def start_queued_runner(self):
        """A runner parked in step 5 behind a held global lock; returns (proc, pgid file body)."""
        self.hold_lock(self.s.state / "_global.lock", {"job": "other", "run_id": "run-x"})
        self.s.setenv(CLAUDE_JOB_GLOBAL_WAIT_S="30")
        proc = self.spawn_job()
        pgid_file = self.s.run_dir / "jobs" / f"{JOB}.pgid"
        lock = self.s.state / f"{JOB}.lock"
        self.assertTrue(wait_for(lambda: (self.s.read_json(lock) or {}).get("pid") == proc.pid, timeout=15))
        self.assertTrue(wait_for(lambda: self.s.read_json(pgid_file), timeout=1))   # findable by the stop path already
        body = self.s.read_json(pgid_file)
        self.assertEqual((body["phase"], body["pgid"], body["run_id"][:4]), ("waiting", proc.pid, "run-"))
        self.assertNotIn("child_pgid", body)
        self.assertIsNone(self.state())                   # nothing `running` yet, so the tick has nothing to judge
        return proc, body

    def assert_queued_abort(self, proc, t0):
        proc.communicate(timeout=20)
        self.assertLess(time.monotonic() - t0, 4.0)
        self.assertOutcome(proc.returncode, 143, "aborted", "addon_stopping", 2)
        self.assertFalse((self.s.run_dir / "jobs" / f"{JOB}.pgid").exists())
        self.assertEqual(self.s.fake_claude_calls(), [])
        self.assertEqual(self.result()["cost_usd"], 0.0)

    def test_int_during_global_wait_aborts(self):
        proc, _ = self.start_queued_runner()
        t0 = time.monotonic()
        os.kill(proc.pid, signal.SIGINT)                  # A14: INT ≡ TERM
        self.assert_queued_abort(proc, t0)

    def test_stop_path_term_to_the_waiting_pgid_aborts(self):
        proc, body = self.start_queued_runner()
        t0 = time.monotonic()
        os.killpg(body["pgid"], signal.SIGTERM)           # what signal_job_runs does with the pgid file
        self.assert_queued_abort(proc, t0)

    def test_stopping_file_during_global_wait_aborts(self):
        """No signal at all: the queued runner sees RUN_DIR/stopping and takes the same aborted/143 path."""
        proc, _ = self.start_queued_runner()
        t0 = time.monotonic()
        (self.s.run_dir / "stopping").write_text("")
        self.assert_queued_abort(proc, t0)


# ---------------------------------------------------------------------------------------------
class TestGates(RunnerCase):
    def test_nesting_guard_detaches_run(self):
        env = dict(self.s.env, CLAUDECODE="1", CLAUDE_CODE_ENTRYPOINT="cli")
        rc, out, err = self.run_job(env=env)
        self.assertEqual(rc, 0, err)
        reply = json.loads(out)
        self.assertEqual((reply["accepted"], reply["job"], reply["detached"]), (True, JOB, True))
        self.assertIn("started in the background", err)
        self.assertTrue(wait_for(lambda: (self.state() or {}).get("status") == "ok", timeout=30))
        self.assertTrue(wait_for(lambda: not (self.s.run_dir / "jobs" / f"{JOB}.pgid").exists(), timeout=10))
        self.assertEqual(self.result()["run_id"], reply["run_id"])
        self.assertNotIn("CLAUDECODE", self.s.fake_claude_calls()[0]["env"])

    def test_force_run_under_markers_refuses(self):
        rc, out, err = self.run_job(verb="force-run", env=dict(self.s.env, CLAUDE_CODE_SESSION_ID="x"))
        self.assertEqual((rc, out), (3, ""))
        self.assertIn("refusing: force-run bypasses the disabled gate", err)
        self.assertIsNone(self.state())

    def test_stopping_file_refuses_without_publish(self):
        (self.s.run_dir / "stopping").write_text("")
        rc, out, err = self.run_job()
        self.assertEqual(rc, 3)
        self.assertIn("add-on is stopping; not starting", err)
        self.assertIsNone(self.state())
        self.assertEqual(self.posts(), [])

    def test_disabled_flag_after_a_run_keeps_last_result(self):
        self.scenario("success:warning")
        self.assertEqual(self.run_job()[0], 0)
        self.s.set_flag_disabled(JOB)
        n_calls, n_posts = len(self.s.fake_claude_calls()), len(self.posts())
        rc, out, err = self.run_job()
        self.assertEqual(rc, 3, err)
        st = self.state()
        self.assertEqual((st["status"], st["enabled"], st["published"]), ("warning", False, True))
        self.assertEqual(len(self.s.fake_claude_calls()), n_calls)
        self.assertEqual(len(self.posts()), n_posts + 1)
        self.assertEqual((self.last_post()["state"], self.last_post()["attributes"]["enabled"]), ("warning", False))
        self.assertEqual(self.s.job_log(JOB)[-1]["event"], "skipped_disabled")
        self.assertEqual(self.s.job_log(JOB)[-1]["gate"], "flag")

    def test_disabled_frontmatter_never_ran_publishes_skipped(self):
        self.s.write_job(JOB, dict(HEALTH_CHECK_FM, enabled=False))
        rc, _, err = self.run_job()
        self.assertOutcome(rc, 3, "skipped", "disabled", None)
        self.assertEqual(self.state()["enabled"], False)
        self.assertEqual(self.state()["stats"]["run_count"], 0)
        self.assertEqual(self.s.job_log(JOB)[0]["gate"], "frontmatter")
        self.assertEqual(self.s.fake_claude_calls(), [])

    def test_force_run_bypasses_the_gate(self):
        self.s.write_job(JOB, dict(HEALTH_CHECK_FM, enabled=False))
        self.s.set_flag_disabled(JOB)
        rc, _, err = self.run_job(verb="force-run")
        self.assertOutcome(rc, 0, "ok", None, 13)
        self.assertEqual(self.result()["trigger"], "manual_forced")
        self.assertEqual(self.state()["enabled"], False)

    def test_overlap_writes_no_entity_and_counts(self):
        self.hold_lock(self.s.state / f"{JOB}.lock", {"pid": 4242, "run_id": "run-20260101T000000Z-aaaa"})
        rc, out, err = self.run_job("--json")
        self.assertEqual(rc, 3, err)
        self.assertEqual(json.loads(out)["reason"], "overlap")
        self.assertIsNone(self.state())
        self.assertEqual(self.posts(), [])
        self.assertEqual((self.s.state / f"{JOB}.skipcount").read_text().strip(), "1")
        line = self.s.job_log(JOB)[-1]
        self.assertEqual((line["event"], line["holder"]["pid"]), ("skipped_overlap", 4242))
        self.assertEqual(self.s.fake_claude_calls(), [])

    def test_overlap_and_refusals_never_touch_a_live_runs_files(self):
        """A1: a skipper (overlap / stopping / detaching parent) must leave run/<name>.* and the inbox file alone."""
        live_out = self.s.run_dir / "run" / f"{JOB}.out"
        live_pgid = self.s.run_dir / "jobs" / f"{JOB}.pgid"
        inbox = self.s.inbox / "run-20260818T050000Z-3f9a.json"
        live_out.write_text("partial envelope")
        live_pgid.write_text(json.dumps({"pgid": 1, "run_id": "run-20260101T000000Z-aaaa"}))
        inbox.write_text(json.dumps({"input": {}}))
        self.hold_lock(self.s.state / f"{JOB}.lock", {"pid": 4242, "run_id": "run-20260101T000000Z-aaaa"})
        self.assertEqual(self.run_job("--input-file", str(inbox))[0], 3)                       # overlap
        (self.s.run_dir / "stopping").write_text("")
        self.assertEqual(self.run_job("--input-file", str(inbox))[0], 3)                       # stopping
        (self.s.run_dir / "stopping").unlink()
        rc, out, _ = self.run_job("--input-file", str(inbox), env=dict(self.s.env, CLAUDECODE="1"))   # detach
        self.assertEqual(rc, 0)
        self.assertTrue(wait_for(lambda: [l for l in self.s.job_log(JOB) if l["event"] == "skipped_overlap"
                                          and l["run_id"] == json.loads(out)["run_id"]], timeout=15))
        for f in (live_out, live_pgid, inbox):
            self.assertTrue(f.exists(), f)
        self.assertEqual(live_out.read_text(), "partial envelope")
        self.assertEqual((self.s.state / f"{JOB}.skipcount").read_text().strip(), "2")   # stopping refuses before the lock

    def test_second_trigger_mid_flight_does_not_break_the_live_run(self):
        self.scenario("sleep:2")
        proc = self.spawn_job()
        self.assertTrue(wait_for(lambda: self.s.fake_claude_calls(), timeout=15))
        rc, _, err = self.run_job()
        self.assertEqual(rc, 3, err)
        proc.communicate(timeout=30)
        self.assertOutcome(proc.returncode, 0, "ok", None, 13)
        self.assertEqual(self.state()["stats"]["skipped_since_last"], 1)

    def test_detached_run_receives_the_inbox_input(self):
        """A2: the detaching parent must not consume the inbox file before the child reads it."""
        fm = dict(MINIMAL_FM, input={"date": {"type": "string"}})
        self.s.write_job("energy-report", fm, "Report.\n")
        inbox = self.s.inbox / "run-20260818T050000Z-3f9a.json"
        inbox.write_text(json.dumps({"input": {"date": "2026-08-17"}}))
        rc, out, err = self.run_job("--input-file", str(inbox), name="energy-report", env=dict(self.s.env, CLAUDECODE="1"))
        self.assertEqual(rc, 0, err)
        self.assertTrue(wait_for(lambda: (self.state("energy-report") or {}).get("status") == "ok", timeout=30), self.state("energy-report"))
        self.assertEqual(self.result("energy-report")["input"], {"date": "2026-08-17"})
        self.assertTrue(wait_for(lambda: not inbox.exists(), timeout=10))       # the child (the run's owner) consumed it

    def test_next_run_folds_the_skip_counter(self):
        (self.s.state / f"{JOB}.skipcount").write_text("2\n")
        rc, _, _ = self.run_job()
        self.assertEqual(rc, 0)
        self.assertEqual(self.state()["stats"]["skipped_since_last"], 2)
        self.assertEqual(self.last_post()["attributes"]["skipped_since_last"], 2)
        self.assertFalse((self.s.state / f"{JOB}.skipcount").exists())
        self.run_job()
        self.assertEqual(self.state()["stats"]["skipped_since_last"], 0)

    def test_concurrency_limit_publishes_skipped(self):
        self.hold_lock(self.s.state / "_global.lock", {"job": "energy-report", "run_id": "run-20260101T000000Z-bbbb"})
        self.s.setenv(CLAUDE_JOB_GLOBAL_WAIT_S="1")
        t0 = time.monotonic()
        rc, _, err = self.run_job()
        self.assertLess(time.monotonic() - t0, 10)
        self.assertOutcome(rc, 3, "skipped", "concurrency_limit", None)
        self.assertEqual(self.result()["headline"], "concurrency limit: another job is running")
        self.assertEqual(self.result()["holder"]["job"], "energy-report")
        self.assertEqual(self.s.fake_claude_calls(), [])
        self.assertEqual(self.s.job_log(JOB)[-1]["event"], "skipped_concurrency")
        self.assertEqual(list((self.s.run_dir / "jobs").iterdir()), [])

    def test_ha_down_persists_unpublished_and_exits_zero(self):
        self.sup.route("POST", STATES, (500, {"message": "down"}), prefix=True)
        rc, _, err = self.run_job()
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.state()["status"], self.state()["published"]), ("ok", False))
        self.assertIn("the tick will retry", err)


# ---------------------------------------------------------------------------------------------
class TestValidation(RunnerCase):
    def test_invalid_definition_is_row1_exit2_no_tokens(self):
        self.s.write_job(JOB, dict(HEALTH_CHECK_FM, tools=["Write", "Bash(ha addons restart x)"]))
        rc, out, err = self.run_job()
        self.assertOutcome(rc, 2, "error", "invalid_definition", 1)
        res = self.result()
        self.assertTrue(res["headline"].startswith("invalid definition: tools[0]:"), res["headline"])
        self.assertEqual(len(res["validation_errors"]), 2)
        self.assertEqual(res["cost_usd"], 0.0)
        self.assertEqual(self.last_post()["attributes"]["validation_errors"], res["validation_errors"])
        self.assertEqual(self.s.fake_claude_calls(), [])
        self.assertEqual(self.s.job_log(JOB)[-1]["argv"], None)
        self.assertEqual(list((self.s.run_dir / "jobs").iterdir()), [])     # the early (waiting) pgid file is gone

    def test_structurally_broken_file_still_publishes(self):
        self.s.write_raw_job(f"{JOB}.md", "no frontmatter here\n")
        rc, _, err = self.run_job()
        self.assertOutcome(rc, 2, "error", "invalid_definition", 1)
        self.assertIn("frontmatter", self.result()["headline"])

    def test_unknown_job_exit2_nothing_written(self):
        rc, _, err = self.run_job(name="nope")
        self.assertEqual(rc, 2)
        self.assertIn("unknown job: nope", err)
        self.assertIsNone(self.state("nope"))
        self.assertEqual(self.sup.requests, [])

    def test_invalid_input_file_is_consumed_and_rejected(self):
        inbox = self.s.inbox / "run-20260818T050000Z-3f9a.json"
        inbox.write_text(json.dumps({"run_id": "run-20260818T050000Z-3f9a", "job": JOB, "input": {"date": "x"}}))
        rc, _, err = self.run_job("--input-file", str(inbox), "--run-id", "run-20260818T050000Z-3f9a")
        self.assertOutcome(rc, 2, "error", "invalid_input", 1)
        self.assertEqual(self.result()["headline"], "invalid input: input: this job takes no input")
        self.assertFalse(inbox.exists())
        self.assertEqual(self.result()["run_id"], "run-20260818T050000Z-3f9a")

    def test_input_reaches_the_prompt(self):
        fm = dict(MINIMAL_FM, input={"date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
                                    "days": {"type": "integer", "default": 7}})
        self.s.write_job("energy-report", fm, "Report.\n")
        rc, _, err = self.run_job("--input", "date=2026-08-17", name="energy-report")
        self.assertEqual(rc, 0, err)
        prompt = self.s.fake_claude_calls()[0]["parsed"]["prompt"]
        self.assertTrue(prompt.startswith("<job-input>\n"), prompt[:80])
        self.assertIn('{"date":"2026-08-17","days":7}', prompt)
        self.assertEqual(self.result("energy-report")["input"], {"date": "2026-08-17", "days": 7})
        self.assertEqual(self.s.fake_claude_calls()[0]["parsed"]["tools"], "Bash,Read,Grep,Glob")
        rc, _, _ = self.run_job("--input", "date=yesterday", name="energy-report")
        self.assertEqual((rc, self.result("energy-report")["reason"]), (2, "invalid_input"))

    def test_input_value_parsing_rule(self):
        import jobdef as m
        got = {k: m.parse_input_value(v) for k, v in
               {"n": "3", "f": "2.5", "flag": "true", "date": "2026-08-17", "q": '"x y"', "obj": '{"a":1}', "nul": "null", "e": ""}.items()}
        self.assertEqual(got, {"n": 3, "f": 2.5, "flag": True, "date": "2026-08-17", "q": "x y", "obj": '{"a":1}', "nul": "null", "e": ""})
        self.assertEqual(m.parse_input_pairs(["a=1", "bad"]), ({"a": 1}, ["input: expected KEY=VALUE, got 'bad'"]))

    def test_bad_run_id_is_usage_error(self):
        rc, _, err = self.run_job("--run-id", "nope")
        self.assertEqual(rc, 2)
        self.assertIsNone(self.state())

    def test_preflight_missing_flag_refuses(self):
        self.scenario("success", FAKE_CLAUDE_HIDE_FLAG="--tools")
        rc, _, err = self.run_job()
        self.assertOutcome(rc, 1, "error", "cli_preflight_failed", None)
        self.assertIn("--tools", self.result()["headline"])
        self.assertEqual(self.s.fake_claude_calls(), [])

    def test_preflight_json_schema_missing_refuses(self):
        self.scenario("success", FAKE_CLAUDE_HIDE_FLAG="--json-schema")
        rc, _, err = self.run_job()
        self.assertOutcome(rc, 1, "error", "no_structured_output_support", None)
        self.assertEqual(self.result()["cost_usd"], 0.0)
        self.assertEqual(self.s.fake_claude_calls(), [])


# ---------------------------------------------------------------------------------------------
class TestRobustness(RunnerCase):
    def test_persistence_failure_in_finish_is_exit_1(self):
        """A3: if the terminal state cannot be written nothing is durable → runner_crash, exit 1, --json still answers."""
        self.scenario("sleep:1")
        proc = self.spawn_job("--json")
        state_file = self.s.state_path(JOB)
        self.assertTrue(wait_for(lambda: (self.state() or {}).get("status") == "running", timeout=15))
        state_file.unlink()
        state_file.mkdir()                                # os.replace(tmp, <dir>) fails for everyone, root included
        out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 1, err)
        self.assertIn("could not persist the result", err)
        reply = json.loads(out)
        self.assertEqual((reply["status"], reply["reason"]), ("error", "runner_crash"))
        self.assertEqual(list((self.s.run_dir / "run").iterdir()), [])
        self.assertFalse((self.s.run_dir / "jobs" / f"{JOB}.pgid").exists())

    def test_corrupt_previous_state_does_not_crash_a_run(self):
        self.s.state_path(JOB).write_text("{not json")
        rc, _, err = self.run_job()
        self.assertOutcome(rc, 0, "ok", None, 13)
        self.s.write_state(JOB, {"schema": 1, "job": JOB, "status": "ok", "result": "garbage", "stats": [], "notify": 5,
                                 "run": "x", "prev_status": {"a": 1}})
        rc, _, err = self.run_job()
        self.assertOutcome(rc, 0, "ok", None, 13)
        st = self.state()
        self.assertEqual((st["stats"]["run_count"], st["prev_status"], st["notify"]["last_notified"]), (1, None, None))

    def test_missing_supervisor_token_degrades_to_unpublished(self):
        self.sup.route("POST", STATES, lambda req: (201, {}) if req["headers"].get("authorization") else (401, {"message": "no auth"}), prefix=True)
        env = dict(self.s.env)
        del env["SUPERVISOR_TOKEN"]
        rc, out, err = self.run_job(env=env)
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.state()["status"], self.state()["published"]), ("ok", False))
        self.assertIn("SUPERVISOR_TOKEN is not set", err)
        self.assertEqual(err.count("SUPERVISOR_TOKEN is not set"), 1)

    def test_auth_retry_extends_deadline_in_state_and_pgid_file(self):
        self.scenario("auth_error")
        self.s.setenv(CLAUDE_JOB_AUTH_RETRY_BASE_S="3")
        proc = self.spawn_job()
        pgid_file = self.s.run_dir / "jobs" / f"{JOB}.pgid"
        self.assertTrue(wait_for(lambda: [l for l in self.s.job_log(JOB) if l["event"] == "auth_retry"], timeout=20))
        time.sleep(0.3)                                   # inside the 3 s pause now
        st, pg = self.state(), self.s.read_json(pgid_file)
        self.assertEqual((st["status"], st["run"]["attempt"]), ("running", 2))
        self.assertEqual(st["run"]["deadline"], pg["deadline"])
        self.assertNotIn("child_pgid", pg)               # A13: the dead first attempt's group is not advertised
        import jobcommon
        extended = (jobcommon.parse_iso(pg["deadline"]) - jobcommon.parse_iso(st["run"]["started_at"])).total_seconds()
        self.assertGreaterEqual(extended, 600 + 15 + 3 - 1)
        first_group = self.s.fake_claude_calls()[0]["pgid"]
        self.assertTrue(wait_for(lambda: (self.s.read_json(pgid_file) or {}).get("child_pgid") not in (None, first_group), timeout=15))
        proc.communicate(timeout=30)
        self.assertOutcome(proc.returncode, 0, "ok", None, 13)
        self.assertEqual((self.result()["attempts"], self.result()["retried_auth"]), (2, True))

    def test_global_lock_is_released_before_the_notifier(self):
        """A4: a slow notifier must not make the next job hit the concurrency limit."""
        self.s.use_fake_notify().setenv(FAKE_NOTIFY_SLEEP="3")
        slow = self.s.bin / "slow-notify"
        slow.write_text(f"#!/bin/sh\nsleep 3\nexec {shlex.quote(str(self.s.fake_notify_wrapper))} \"$@\"\n")
        slow.chmod(0o755)
        self.s.setenv(CLAUDE_JOB_NOTIFY_BIN=str(slow), CLAUDE_JOB_GLOBAL_WAIT_S="1")
        self.s.write_job("energy-report", dict(MINIMAL_FM, description="Energy"), "E.\n")
        first = self.spawn_job()
        self.assertTrue(wait_for(lambda: self.s.job_log(JOB) and self.s.job_log(JOB)[-1]["event"] == "run", timeout=20))
        rc, _, err = self.run_job(name="energy-report")           # runs while the first job sits in its notifier
        first.communicate(timeout=30)
        self.assertEqual((rc, self.state("energy-report")["status"]), (0, "ok"), err)
        self.assertEqual(self.state()["notify"]["notify_status"], "sent:persistent")


# ---------------------------------------------------------------------------------------------
class TestNotifier(RunnerCase):
    def test_notifier_present_updates_state_and_reposts(self):
        self.s.use_fake_notify()
        rc, _, err = self.run_job()
        self.assertEqual(rc, 0, err)
        call = self.s.fake_notify_calls()[0]
        self.assertEqual(call["argv"], [JOB, "--state-file", str(self.s.state_path(JOB))])
        self.assertEqual(call["state"]["notify"]["channels"], [])
        self.assertIsNone(call["state"]["notify"]["notify_status"])
        st = self.state()
        self.assertEqual(st["notify"]["notify_status"], "sent:persistent")
        self.assertEqual(st["notify"]["last_notified"]["status"], "ok")
        self.assertEqual([p["state"] for p in self.posts()], ["running", "ok", "ok"])
        self.assertEqual(self.last_post()["attributes"]["notify_status"], "sent:persistent")

    def test_notifier_garbage_is_recorded_not_fatal(self):
        self.s.use_fake_notify().setenv(FAKE_NOTIFY_GARBAGE="1")
        rc, _, err = self.run_job()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.state()["notify"]["notify_status"], "error:notifier_failed")

    def test_notifier_absent(self):
        rc, _, _ = self.run_job()
        self.assertEqual(self.state()["notify"]["notify_status"], "skipped_no_notifier")
        self.assertEqual(len(self.posts()), 2)


# ---------------------------------------------------------------------------------------------
class TestOtherVerbs(RunnerCase):
    def setUp(self):
        super().setUp()
        self.s.write_job("energy-report", dict(MINIMAL_FM, description="Energy", model="sonnet"), "E.\n")
        self.s.write_job("broken", dict(MINIMAL_FM, tools=["Write"]), "B.\n")
        self.s.write_raw_job("Bad_Name.md", "---\ndescription: x\n---\nbody\n")
        self.s.write_notify_config({"mobile": {"targets": []}})

    def test_list_json_and_table(self):
        self.s.write_state(JOB, {"schema": 1, "job": JOB, "status": "warning",
                                 "result": {"status": "warning", "ended_at": "2026-08-18T05:00:00Z", "headline": "h"}})
        rc, out, err = self.cli("list", "--json")
        self.assertEqual(rc, 0, err)
        doc = json.loads(out)
        by_name = {j["name"]: j for j in doc["jobs"]}
        self.assertEqual(set(by_name), {JOB, "energy-report", "broken"})
        self.assertEqual((by_name[JOB]["status"], by_name[JOB]["slug"], by_name[JOB]["valid"]), ("warning", "health_check", True))
        self.assertEqual((by_name["broken"]["valid"], by_name["broken"]["errors"]), (False, 1))
        self.assertEqual(by_name["energy-report"]["model"], "sonnet")
        self.assertIn("Bad_Name.md", {i["file"] for i in doc["ignored"]})
        rc, out, _ = self.cli("list")
        lines = out.splitlines()
        self.assertEqual(lines[0].split(), ["NAME", "KIND", "ENABLED", "STATUS", "LAST_RUN", "STALE", "MODEL", "DESCRIPTION"])
        self.assertTrue(any(l.startswith("broken") and " invalid " in l for l in lines), out)
        self.assertIn("ignored files:", out)
        self.assertEqual(self.sup.requests, [])

    def test_validate(self):
        rc, out, _ = self.cli("validate", JOB)
        self.assertEqual((rc, out.strip()), (0, f"{JOB}: OK"))
        rc, out, _ = self.cli("validate", "broken", "--json")
        doc = json.loads(out)
        self.assertEqual((rc, doc["valid"], len(doc["errors"])), (2, False, 1))
        rc, out, _ = self.cli("validate", "--all", "--json")
        doc = json.loads(out)
        self.assertEqual((rc, doc["valid"], len(doc["jobs"])), (2, False, 3))
        rc, out, _ = self.cli("validate", "broken")
        self.assertIn("broken: 1 error(s), 0 warning(s)\n  error: tools[0]:", out)
        self.assertEqual(self.cli("validate")[0], 2)

    def test_dry_run_prints_sections_and_spends_nothing(self):
        rc, out, err = self.cli("dry-run", JOB)
        self.assertEqual(rc, 0, err)
        expected = ["validation", "preflight", "settings.json", "mcp.json", "result schema", "tools",
                    "notify channels", "prompt", "command"]
        positions = [out.index(f"## {t}\n") for t in expected]        # the contract inside `command` has its own ##s
        self.assertEqual(positions, sorted(positions))
        self.assertTrue(out.startswith("## validation\nOK"), out[:60])
        self.assertIn("<nonce>", out)
        self.assertIn("127.0.0.1:<port>/core", out)
        self.assertIn("CLAUDE_JOB_BROKER_PORT=<port>", out)
        self.assertIn("'TZ=<ha time zone>'", out)
        self.assertIn("Read(//homeassistant/**)", out)
        self.assertEqual(self.sup.requests, [])
        self.assertEqual(self.s.fake_claude_calls(), [])
        self.assertIsNone(self.state())
        self.assertEqual(list((self.s.run_dir / "run").iterdir()), [])
        self.assertEqual(self.cli("dry-run", "broken")[0], 2)

    def test_enable_disable(self):
        rc, out, _ = self.cli("disable", JOB)
        self.assertEqual((rc, out.strip()), (0, f"{JOB}: disabled (flag file created)"))
        self.assertTrue((self.s.disabled / "health_check").exists())
        rc, out, _ = self.cli("enable", "health_check")           # slug spelling accepted
        self.assertEqual((rc, out.strip()), (0, f"{JOB}: enabled"))
        self.assertFalse((self.s.disabled / "health_check").exists())
        self.s.write_job(JOB, dict(HEALTH_CHECK_FM, enabled=False))
        rc, out, _ = self.cli("enable", JOB)
        self.assertEqual(out.strip(), f"{JOB}: flag removed, but frontmatter says enabled: false")
        self.assertEqual(self.cli("disable", "nope")[0], 2)

    def test_token_show_rotate_ensure(self):
        token_file = self.s.data / "token"
        rc, out, _ = self.cli("token", "show")
        self.assertEqual(rc, 2)
        self.assertIn("no token yet", out)
        rc, out, _ = self.cli("token", "ensure")
        self.assertEqual((rc, out.strip()), (0, str(token_file)))
        tok = token_file.read_text().strip()
        self.assertRegex(tok, r"^[0-9a-f]{64}$")
        self.assertNotIn(tok, out)
        self.assertEqual(stat.S_IMODE(token_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.s.data.stat().st_mode), 0o700)
        rc, out, _ = self.cli("token", "ensure")                    # idempotent
        self.assertEqual((rc, token_file.read_text().strip()), (0, tok))
        rc, out, _ = self.cli("token", "show")
        self.assertEqual((rc, out.strip()), (0, tok))
        rc, out, _ = self.cli("token", "rotate")
        self.assertEqual(rc, 0)
        self.assertIn("token rotated", out)
        new = token_file.read_text().strip()
        self.assertNotEqual(new, tok)
        self.assertNotIn(new, out)
        token_file.write_text("garbage\n")
        rc, out, _ = self.cli("token", "ensure")                    # malformed → replaced
        self.assertRegex(token_file.read_text().strip(), r"^[0-9a-f]{64}$")

    def test_version(self):
        rc, out, _ = self.cli("--version")
        self.assertEqual((rc, out.strip()), (0, "claude-job 9.9.9-test"))


# ---------------------------------------------------------------------------------------------
class TestJudgePure(unittest.TestCase):
    """judge() is a pure function; the rows that are awkward to reach through fakes live here."""

    @classmethod
    def setUpClass(cls):
        cls.m = load_runner_module()
        cls.schema = {"type": "object", "additionalProperties": False, "required": ["status", "headline"],
                      "properties": {"status": {"type": "string", "enum": ["ok", "info", "warning", "critical"]},
                                     "headline": {"type": "string", "minLength": 1, "maxLength": 120},
                                     "detail": {"type": "string"}, "metrics": {"type": "object"}}}

    def judge(self, **kw):
        args = dict(exit_code=0, elapsed=5.0, timeout_s=600, stdout="", stderr="", max_turns=50,
                    max_cost_usd=1.0, schema=self.schema, auth_retried=False)
        args.update(kw)
        return self.m.judge(**args)

    def env(self, **kw):
        base = {"type": "result", "subtype": "success", "is_error": False, "session_id": "s", "total_cost_usd": 0.5,
                "num_turns": 3, "permission_denials": [], "modelUsage": {"claude-fable-5": {}},
                "result": "{}", "structured_output": {"status": "ok", "headline": "fine"}}
        base.update(kw)
        return json.dumps(base)

    def test_login_expired_wording_after_retry(self):
        env = self.env(subtype="error_during_execution", is_error=True, errors=["401 Unauthorized: OAuth token expired"])
        first = self.judge(exit_code=1, stdout=env)
        self.assertEqual((first.row, first.reason, first.extra["auth_shaped"]), (8, "run_failed", True))
        second = self.judge(exit_code=1, stdout=env, auth_retried=True)
        self.assertEqual((second.row, second.reason, second.headline),
                         (8, "login_expired", "claude.ai login expired — run /login in the terminal"))

    def test_exit_codes_only_matter_without_envelope(self):
        self.assertEqual(self.judge(exit_code=124, stdout=self.env()).row, 13)      # envelope wins
        self.assertEqual(self.judge(exit_code=137, elapsed=700).row, 3)             # KILLed past the timeout
        self.assertTrue(self.judge(exit_code=137, elapsed=700).extra["hard_killed"])
        self.assertEqual(self.judge(exit_code=143, elapsed=5).row, 4)               # signalled early
        self.assertEqual(self.judge(exit_code=1, elapsed=5).row, 5)
        self.assertEqual(self.m.normalize_exit(-9), 137)
        self.assertEqual(self.m.normalize_exit(-15), 143)

    def test_envelope_tolerates_leading_noise_and_denial_counts(self):
        denials = [{"tool_name": "Bash", "tool_input": {"command": "ha x"}}] * 2 + [{"tool_name": "Read", "tool_input": {}}]
        v = self.judge(stdout="warning: noise\n" + self.env(permission_denials=denials))
        self.assertEqual((v.row, v.status, v.headline), (11, "warning", "[denied: Bash×2, Read×1] fine"))
        self.assertEqual(v.extra["model_resolved"], "claude-fable-5")

    def test_row11_and_row12_prefixes_stack_and_over_budget_is_recorded(self):
        denials = [{"tool_name": "Bash", "tool_input": {"command": "ha x"}}]
        v = self.judge(stdout=self.env(permission_denials=denials, total_cost_usd=9.0))
        self.assertEqual((v.row, v.status, v.headline, v.extra["over_budget"]), (11, "warning", "[denied: Bash×1] [over budget] fine", True))
        many = [{"tool_name": "Bash", "tool_input": {}}] * 25
        v = self.judge(stdout=self.env(permission_denials=many))
        self.assertTrue(v.headline.startswith("[denied: Bash×25] "))          # counted from the raw list …
        self.assertEqual(len(v.extra["permission_denials"]), 20)             # … stored capped

    def test_schema_violation_with_mixed_paths(self):
        schema = dict(self.schema, properties=dict(self.schema["properties"], metrics={"type": "object", "additionalProperties": {"type": "number"}}))
        v = self.judge(stdout=self.env(structured_output={"status": "ok", "headline": "h", "metrics": {"a": "x"}}), schema=schema)
        self.assertEqual((v.row, v.reason), (10, "schema"))
        self.assertIn("metrics/a:", v.headline)

    def test_jsonl_line_cap(self):
        big = {"type": "result", "subtype": "success", "result": "r" * 20000, "structured_output": {"detail": "d" * 20000}}
        rec = self.m.fit_line({"event": "run", "envelope": dict(big), "headline": "h"})
        self.assertEqual((rec["envelope"], rec["envelope_dropped"]), (None, True))
        rec = self.m.fit_line({"event": "run", "envelope": {"type": "result", "result": "r" * 20000, "x": 1}, "headline": "h"})
        self.assertEqual(rec["envelope"], {"type": "result", "x": 1})
        self.assertNotIn("envelope_dropped", rec)
        small = {"event": "run", "envelope": {"result": "ok"}, "headline": "h"}
        self.assertEqual(self.m.fit_line(dict(small)), small)

    def test_headline_cap_and_argv_elision(self):
        v = self.judge(stdout=self.env(subtype="error_during_execution", is_error=True, errors=["x" * 500]))
        self.assertEqual(v.headline, "run failed: " + "x" * 120)
        argv = self.m.elide_argv(["CLAUDE_JOB_BROKER_NONCE=abc", "claude", "-p", "PROMPT", "--json-schema", "{}",
                                  "--append-system-prompt", "C", "--model", "fable"])
        self.assertEqual((argv[0], argv[-2:]), ("CLAUDE_JOB_BROKER_NONCE=<redacted>", ["--model", "fable"]))
        argv = argv[1:]
        self.assertTrue(argv[2].startswith("<prompt sha256=") and argv[4] == "<schema len=2>" and argv[6] == "<contract len=1>")


if __name__ == "__main__":
    unittest.main()
