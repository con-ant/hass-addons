"""U1: jobcommon.py — paths, durable writes, pruning, locks, entity payloads, cost rollup,
kill switch, job summary, token, CLI preflight (design §4.1, §4.9, §4.10, A.4/A.6/A.7)."""
import datetime as dt
import importlib
import json
import os
import stat
import threading
import time
import types
import unittest
from pathlib import Path

from testlib import LIB_DIR, SHARE_DIR, ScratchRoot
from fakes.fake_supervisor import FakeSupervisor

import jobcommon as jc
import jobdef


def terminal_state(**over):
    st = {
        "schema": 1, "job": "health-check", "slug": "health_check", "status": "warning", "enabled": True,
        "published": True, "updated_at": "2026-08-18T05:01:12Z",
        "description": "Daily health check; reports anything a human should look at.", "stale_after": 93600,
        "run": None,
        "result": {
            "run_id": "run-20260818T050000Z-3f9a", "session_id": "6f0", "status": "warning",
            "headline": "[denied: Bash×1] 2 integrations failing since 03:12", "detail": "full text",
            "reason": None, "judgment_row": 11, "envelope_subtype": "success", "is_error": False, "exit_code": 0,
            "hard_killed": False, "started_at": "2026-08-18T05:00:00Z", "ended_at": "2026-08-18T05:00:42Z",
            "duration_s": 42.3, "cost_usd": 0.6312, "over_budget": False, "model": "fable", "trigger": "endpoint",
            "attempts": 1, "retried_auth": False, "claude_version": "2.1.233", "num_turns": 9,
            "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "ha addons"}}],
            "validation_errors": [], "metrics": {"failing_integrations": 2}, "actions_selected": ["restart_miele"],
            "input": {"date": "2026-08-17"}, "raw_tail": "secret raw", "result_tail": None,
        },
        "prev_status": "ok",
        "stats": {"run_count": 12, "skipped_since_last": 0, "last_skip_at": None, "first_seen": "2026-08-01T00:00:00Z"},
        "notify": {"channels": ["mobile", "persistent"], "notify_status": "sent:persistent,mobile(2)", "last_notified": None},
    }
    st.update(over)
    return st


class JcCase(unittest.TestCase):
    def setUp(self):
        self.s = ScratchRoot().start()
        self.s.apply_to_process()
        jc.reload_paths()

    def tearDown(self):
        self.s.stop()
        jc.reload_paths()


class TestPaths(JcCase):
    def test_env_overrides(self):
        s = self.s
        self.assertEqual(jc.JOBS_DIR, str(s.jobs))
        self.assertEqual(jc.STATE_DIR, str(s.state))
        self.assertEqual(jc.LOGS_DIR, str(s.logs))
        self.assertEqual(jc.INBOX_DIR, str(s.inbox))
        self.assertEqual(jc.DISABLED_DIR, str(s.disabled))
        self.assertEqual(jc.ACTIONS_DIR, str(s.state / "actions"))
        self.assertEqual(jc.DATA_DIR, str(s.data))
        self.assertEqual(jc.PROJECT_DIR, str(s.project))
        self.assertEqual(jc.TOKEN_FILE, str(s.data / "token"))
        self.assertEqual(jc.OPTIONS_FILE, str(s.options_file))
        self.assertEqual(jc.RUN_DIR, str(s.run_dir))
        self.assertEqual(jc.PGID_DIR, str(s.run_dir / "jobs"))
        self.assertEqual(jc.PERRUN_DIR, str(s.run_dir / "run"))
        self.assertEqual(jc.STOPPING_FILE, str(s.run_dir / "stopping"))
        self.assertEqual(jc.TRANSCRIPTS_DIR, str(s.transcripts))
        self.assertEqual(jc.SHARE_DIR, str(SHARE_DIR))
        self.assertEqual(jc.LIB_DIR, str(LIB_DIR))
        self.assertEqual(jc.BROKER_SCRIPT, str(LIB_DIR / "ha_broker.py"))
        self.assertEqual(jc.CLAUDE_BIN, str(s.claude_wrapper))
        self.assertEqual(jc.SUPERVISOR_URL, "http://supervisor")   # not set until with_supervisor()
        self.assertEqual(jc.COST_FILE, str(s.state / "_cost.json"))
        self.assertEqual(jc.GLOBAL_LOCK, str(s.state / "_global.lock"))
        self.assertEqual(jc.lock_path("health-check"), str(s.state / "health-check.lock"))
        self.assertEqual(jc.state_path("health-check"), str(s.state / "health-check.json"))
        self.assertEqual(jc.log_path("health-check"), str(s.logs / "health-check.jsonl"))
        with self.assertRaises(ValueError):
            jc.state_path("../etc/passwd")
        with self.assertRaises(ValueError):
            jc.is_flag_disabled("Health_Check")

    def test_production_defaults(self):
        saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("CLAUDE_JOB_")}
        try:
            jc.reload_paths()
            self.assertEqual(jc.HA_CONFIG_DIR, "/homeassistant")
            self.assertEqual(jc.JOBS_DIR, "/homeassistant/.claudecode/jobs")
            self.assertEqual(jc.INBOX_DIR, "/homeassistant/.claudecode/jobs/state/inbox")
            self.assertEqual(jc.PROJECT_DIR, "/data/claude-jobs/project")
            self.assertEqual(jc.TOKEN_FILE, "/data/claude-jobs/token")
            self.assertEqual(jc.OPTIONS_FILE, "/data/options.json")
            self.assertEqual(jc.PGID_DIR, "/run/claudecode/jobs")
            self.assertEqual(jc.JOB_CONFIG_DIR, "/data/claude-jobs/claude-config")
            self.assertEqual(jc.CREDENTIALS_FILE, "/homeassistant/.claudecode/.credentials.json")
            self.assertEqual(jc.TRANSCRIPTS_DIR, "/data/claude-jobs/claude-config/projects/-data-claude-jobs-project")
            self.assertEqual(jc.SUPERVISOR_URL, "http://supervisor")
            self.assertEqual((jc.CLAUDE_BIN, jc.TIMEOUT_BIN, jc.RUNNER_BIN, jc.NOTIFY_BIN),
                             ("claude", "timeout", "claude-job", "claude-job-notify"))
            self.assertEqual(jc.BUILT_CLI_VERSION_FILE, "/etc/claude-code-version")
            self.assertEqual(jc.ADDON_VERSION_FILE, "/etc/claudecode-addon-version")
            self.assertEqual(jc.SHARE_DIR, str(SHARE_DIR))   # relative to the source tree, not ROOT
            self.assertEqual((jc.JOB_MAX_TIMEOUT, jc.JOB_MAX_COST_USD, jc.JOB_GLOBAL_WAIT_S, jc.JOB_ENDPOINT_PORT,
                              jc.DETAIL_ATTR_CAP, jc.JOB_INPUT_MAX_BYTES, jc.TICK_INTERVAL_S, jc.STUCK_MARGIN_S,
                              jc.WATCHDOG_KILL_S, jc.CANARY_INTERVAL_S, jc.JOB_TRANSCRIPT_KEEP_DAYS),
                             (3600, 5.0, 60, 7682, 900, 4096, 60, 120, 180, 600, 30))
            os.environ["CLAUDE_JOB_ROOT"] = "/tmp/x"
            os.environ["CLAUDE_JOB_GLOBAL_WAIT_S"] = "1.5"
            jc.reload_paths()
            self.assertEqual(jc.JOBS_DIR, "/tmp/x/homeassistant/.claudecode/jobs")
            self.assertEqual(jc.RUN_DIR, "/tmp/x/run/claudecode")
            self.assertEqual(jc.JOB_GLOBAL_WAIT_S, 1.5)
        finally:
            os.environ.pop("CLAUDE_JOB_ROOT", None)
            os.environ.pop("CLAUDE_JOB_GLOBAL_WAIT_S", None)
            os.environ.update(saved)
            jc.reload_paths()

    def test_constants(self):
        self.assertEqual(jc.ALLOWED_JOB_MODELS, ("fable", "opus", "sonnet", "haiku"))
        self.assertEqual(jc.MODEL_ALIASES, {"claude-fable-5": "fable"})
        self.assertEqual(jc.CLI_PREFLIGHT_FLAGS, ("--setting-sources", "--settings", "--tools", "--json-schema", "--strict-mcp-config"))
        self.assertEqual(jc.ENDPOINT_ENTITY, "sensor.claude_jobs_endpoint")
        self.assertEqual(jc.COST_ENTITY, "sensor.claude_jobs_cost_raw")
        self.assertEqual(jc.ICONS["critical"], "mdi:alert-octagon")


class TestTimeAndIds(JcCase):
    def test_iso_helpers(self):
        d = jc.parse_iso("2026-08-18T05:01:12Z")
        self.assertEqual(d, dt.datetime(2026, 8, 18, 5, 1, 12, tzinfo=dt.timezone.utc))
        self.assertEqual(jc.parse_iso("2026-08-18T07:01:12+02:00"), d)
        self.assertEqual(jc.parse_iso("2026-08-18T05:01:12.250Z").microsecond, 250000)
        self.assertIsNone(jc.parse_iso("yesterday"))
        self.assertIsNone(jc.parse_iso(None))
        self.assertEqual(jc.iso(d), "2026-08-18T05:01:12Z")
        self.assertEqual(jc.now_iso(d), "2026-08-18T05:01:12Z")
        self.assertRegex(jc.now_iso(), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        os.environ["CLAUDE_JOB_FAKE_NOW"] = "2030-01-02T03:04:05Z"
        try:
            self.assertEqual(jc.now_iso(), "2030-01-02T03:04:05Z")
            self.assertTrue(jc.new_run_id().startswith("run-20300102T030405Z-"))
        finally:
            del os.environ["CLAUDE_JOB_FAKE_NOW"]

    def test_new_run_id(self):
        ids = {jc.new_run_id() for _ in range(20)}
        for rid in ids:
            self.assertRegex(rid, jobdef.RUN_ID_RE)
        self.assertGreater(len(ids), 1)


class TestFiles(JcCase):
    def test_atomic_write(self):
        p = self.s.state / "x.json"
        jc.atomic_write_json(p, {"a": 1}, mode=0o600)
        self.assertEqual(json.loads(p.read_text()), {"a": 1})
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        jc.atomic_write_json(p, {"a": 2}, mode=0o644, indent=1)
        self.assertEqual(jc.read_json(p), {"a": 2})
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o644)
        with self.assertRaises(TypeError):
            jc.atomic_write_json(p, {"a": object()})
        self.assertEqual(jc.read_json(p), {"a": 2})                              # original intact
        self.assertEqual([f for f in os.listdir(self.s.state) if ".tmp." in f], [])   # no tmp left behind

        class Boom(Exception):
            pass

        orig = os.replace
        try:
            os.replace = lambda *a, **k: (_ for _ in ()).throw(Boom())
            with self.assertRaises(Boom):
                jc.atomic_write_text(p, "zzz")
        finally:
            os.replace = orig
        self.assertEqual([f for f in os.listdir(self.s.state) if ".tmp." in f], [])
        self.assertEqual(jc.read_json(p), {"a": 2})
        deep = self.s.root / "new" / "dir" / "f.txt"
        jc.atomic_write_text(deep, "hi", 0o600)
        self.assertEqual(deep.read_text(), "hi")
        self.assertEqual(jc.read_json(self.s.root / "missing.json", {"d": 1}), {"d": 1})
        self.assertIsNone(jc.read_text(self.s.root / "missing.txt"))

    def test_atomic_write_is_thread_safe(self):
        p = self.s.state / "shared.json"
        errors = []

        def writer(tag):
            try:
                for i in range(200):
                    jc.atomic_write_json(p, {"tag": tag, "i": i, "pad": "x" * 500})
                    obj = jc.read_json(p)
                    if not isinstance(obj, dict):          # a torn/missing file would show up here
                        errors.append(("unparseable", tag, i))
            except Exception as e:  # noqa: BLE001
                errors.append((type(e).__name__, str(e)))

        threads = [threading.Thread(target=writer, args=(t,)) for t in ("a", "b", "c")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertIn(jc.read_json(p)["tag"], ("a", "b", "c"))
        self.assertEqual([f for f in os.listdir(self.s.state) if ".tmp." in f], [])

    def test_append_and_prune_jsonl(self):
        p = self.s.logs / "j.jsonl"
        for i in range(500):
            jc.append_jsonl(p, {"i": i, "pad": "x" * 100})
        lines = p.read_text().splitlines()
        self.assertEqual(len(lines), 500)
        self.assertEqual(json.loads(lines[-1])["i"], 499)
        size = p.stat().st_size
        self.assertFalse(jc.prune_jsonl(p, max_bytes=size, keep_lines=200))      # not over
        self.assertTrue(jc.prune_jsonl(p, max_bytes=size - 1, keep_lines=200))
        kept = self.s.read_jsonl(p)
        self.assertEqual([r["i"] for r in kept], list(range(300, 500)))
        self.assertTrue(p.read_text().endswith("\n"))
        # a torn (newline-less) last line is dropped, not completed
        with open(p, "a") as f:
            f.write('{"i": 500, "pa')
        self.assertTrue(jc.prune_jsonl(p, max_bytes=1, keep_lines=3))
        self.assertEqual(p.read_text().splitlines(), [json.dumps({"i": i, "pad": "x" * 100}, separators=(",", ":")) for i in (497, 498, 499)])
        self.assertFalse(jc.prune_jsonl(self.s.logs / "missing.jsonl"))
        self.assertEqual([f for f in os.listdir(self.s.logs) if ".tmp." in f], [])

    def test_prune_transcripts(self):
        d = self.s.transcripts
        now = time.time()
        (d / "memory").mkdir()
        (d / "memory" / "keep.md").write_text("m" * 5000)
        os.utime(d / "memory" / "keep.md", (now - 90 * 86400,) * 2)
        sibling = d.parent / "-root"
        sibling.mkdir()
        (sibling / "old.jsonl").write_text("s")
        os.utime(sibling / "old.jsonl", (now - 90 * 86400,) * 2)
        for name, age_days, size in (("a.jsonl", 40, 1000), ("b.jsonl", 31, 1000), ("c.jsonl", 10, 3000),
                                     ("d.jsonl", 5, 3000), ("e.jsonl", 1, 3000)):
            f = d / name
            f.write_text("x" * size)
            os.utime(f, (now - age_days * 86400,) * 2)
        r = jc.prune_transcripts(keep_days=30, max_bytes=10 ** 9, now=now)
        self.assertEqual(r["deleted"], 2)
        self.assertEqual(sorted(os.listdir(d)), ["c.jsonl", "d.jsonl", "e.jsonl", "memory"])
        r = jc.prune_transcripts(keep_days=30, max_bytes=6000, now=now)          # oldest-first to the cap
        self.assertEqual(sorted(os.listdir(d)), ["d.jsonl", "e.jsonl", "memory"])
        self.assertEqual(r, {"deleted": 1, "remaining_bytes": 6000})
        self.assertTrue((d / "memory" / "keep.md").exists())
        self.assertTrue((sibling / "old.jsonl").exists())
        self.assertEqual(jc.prune_transcripts(self.s.root / "nope"), {"deleted": 0, "remaining_bytes": 0})

    def test_purge_tool_results(self):
        d = self.s.transcripts
        (d / "keep.jsonl").write_text("transcript")                       # transcripts stay
        sess = d / "0e3a-session"
        (sess / "tool-results").mkdir(parents=True)
        (sess / "tool-results" / "r1.txt").write_text("x" * 100)
        (sess / "tool-results" / "r2.txt").write_text("y")
        (sess / "tool-results" / "pdf-abc").mkdir()                       # the CLI nests subdirs in here
        (sess / "tool-results" / "pdf-abc" / "page1.png").write_bytes(b"p")
        busy = d / "busy-session"
        (busy / "tool-results").mkdir(parents=True)
        (busy / "tool-results" / "r.txt").write_text("z")
        (busy / "other.txt").write_text("keep me")                        # session dir not emptied -> stays
        self.assertEqual(jc.purge_tool_results(), 4)
        self.assertFalse(sess.exists())                                   # emptied session dir removed
        self.assertFalse((busy / "tool-results").exists())
        self.assertTrue((busy / "other.txt").exists())
        self.assertTrue((d / "keep.jsonl").exists())
        self.assertEqual(jc.purge_tool_results(), 0)                      # idempotent
        self.assertEqual(jc.purge_tool_results(self.s.root / "nope"), 0)

    def test_ensure_and_reconcile_job_credentials(self):
        # every fixture carries an explicit expiresAt: the mtime tie-breaker is not portable
        # across kernels (pre-6.13 back-to-back writes can share an mtime), and expiresAt is
        # the branch that matters in production anyway
        def creds(token, expires):
            return '{"claudeAiOauth": {"accessToken": "%s", "expiresAt": %d}}' % (token, expires)
        real = Path(jc.CREDENTIALS_FILE)
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_text(creds("old", 1000))
        link = Path(jc.JOB_CONFIG_DIR) / ".credentials.json"
        jc.ensure_job_config()
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), str(real))
        self.assertEqual(link.read_text(), creds("old", 1000))
        self.assertEqual(stat.S_IMODE(os.stat(jc.JOB_CONFIG_DIR).st_mode), 0o700)
        jc.ensure_job_config()                                            # idempotent, link untouched
        self.assertTrue(link.is_symlink())
        # a mid-run refresh replaced the symlink with a regular file (tmp+rename): reconcile
        # moves the fresh content back to the real store and restores the symlink
        link.unlink()
        link.write_text(creds("fresh", 1500))
        self.assertTrue(jc.reconcile_job_credentials())
        self.assertTrue(link.is_symlink())
        self.assertEqual(real.read_text(), creds("fresh", 1500))
        self.assertEqual(stat.S_IMODE(os.stat(real).st_mode), 0o600)
        self.assertFalse(jc.reconcile_job_credentials())                  # nothing to do now
        # ensure_job_config() reconciles a leftover file before re-linking, too
        link.unlink()
        link.write_text(creds("fresher", 1800))
        jc.ensure_job_config()
        self.assertTrue(link.is_symlink())
        self.assertEqual(real.read_text(), creds("fresher", 1800))
        # newest wins, not job-leftover wins: a stale leftover (earlier expiresAt than the
        # shared store — e.g. the terminal re-logged-in after a hard-killed run) is DISCARDED
        real.write_text(creds("new", 2000))
        link.unlink()
        link.write_text(creds("stale", 1000))
        self.assertFalse(jc.reconcile_job_credentials())
        self.assertTrue(link.is_symlink())
        self.assertIn('"new"', real.read_text())
        # and a leftover with a LATER expiresAt still wins even if the store was written after it
        link.unlink()
        link.write_text(creds("renewed", 3000))
        real.write_text(creds("new", 2000))
        self.assertTrue(jc.reconcile_job_credentials())
        self.assertIn('"renewed"', real.read_text())
        self.assertTrue(link.is_symlink())
        # restore_link=False (the mid-run sync): content lands in the store, the regular file
        # stays put — the running CLI never sees an unlink->symlink ENOENT window
        link.unlink()
        link.write_text(creds("midrun", 4000))
        self.assertTrue(jc.reconcile_job_credentials(restore_link=False))
        self.assertFalse(link.is_symlink())
        self.assertEqual(real.read_text(), creds("midrun", 4000))
        self.assertFalse(jc.reconcile_job_credentials(restore_link=False))  # synced; nothing newer
        self.assertFalse(jc.reconcile_job_credentials())                  # end-of-run: relink only
        self.assertTrue(link.is_symlink())
        self.assertEqual(real.read_text(), creds("midrun", 4000))


class TestLocks(JcCase):
    def test_flock_nb_and_wait_abort(self):
        path = jc.lock_path("health-check")
        fd = jc.flock_nb(path)
        self.assertIsNotNone(fd)
        jc.write_lock_info(fd, {"pid": os.getpid(), "run_id": "run-x"})
        self.assertEqual(jc.read_lock_info(path)["run_id"], "run-x")
        self.assertIsNone(jc.flock_nb(path))                       # second open file description blocks
        t0 = time.monotonic()
        self.assertIsNone(jc.flock_wait(path, 0.6))
        self.assertGreaterEqual(time.monotonic() - t0, 0.5)
        calls = []
        t0 = time.monotonic()
        self.assertIsNone(jc.flock_wait(path, 30, should_abort=lambda: calls.append(1) or len(calls) >= 2))
        self.assertLess(time.monotonic() - t0, 2)
        self.assertEqual(len(calls), 2)
        threading.Timer(0.3, lambda: jc.unlock(fd)).start()
        t0 = time.monotonic()
        fd2 = jc.flock_wait(path, 5, should_abort=lambda: False)
        self.assertIsNotNone(fd2)
        self.assertLess(time.monotonic() - t0, 2)
        jc.unlock(fd2)
        fd3 = jc.flock_nb(path)
        self.assertIsNotNone(fd3)
        jc.unlock(fd3)
        self.assertTrue(os.path.exists(path))                       # lock files are never unlinked


class TestEntityPayload(JcCase):
    def test_running(self):
        st = terminal_state(status="running", run={"run_id": "run-20260818T060000Z-aaaa", "pid": 1, "pgid": 1,
                                                  "started_at": "2026-08-18T06:00:00Z", "deadline": "2026-08-18T06:10:00Z",
                                                  "timeout_s": 600, "trigger": "manual", "attempt": 1, "model": "fable"})
        eid, state, attrs = jc.entity_payload(st)
        self.assertEqual((eid, state), ("sensor.claude_job_health_check", "running"))
        self.assertEqual(attrs, {"job": "health-check", "started_at": "2026-08-18T06:00:00Z", "timeout_s": 600,
                                 "run_id": "run-20260818T060000Z-aaaa", "trigger": "manual", "prev_status": "warning",
                                 "enabled": True, "friendly_name": st["description"], "icon": "mdi:progress-clock"})

    def test_terminal(self):
        st = terminal_state()
        eid, state, attrs = jc.entity_payload(st)
        self.assertEqual((eid, state), ("sensor.claude_job_health_check", "warning"))
        self.assertEqual(attrs, {
            "job": "health-check", "description": st["description"],
            "headline": "[denied: Bash×1] 2 integrations failing since 03:12", "detail": "full text", "detail_truncated": False,
            "last_run": "2026-08-18T05:00:42Z", "started_at": "2026-08-18T05:00:00Z", "duration_s": 42.3, "cost_usd": 0.63,
            "run_count": 12, "model": "fable", "enabled": True, "stale_after": 93600, "run_id": "run-20260818T050000Z-3f9a",
            "session_id": "6f0", "envelope_subtype": "success", "reason": None, "trigger": "endpoint", "prev_status": "ok",
            "skipped_since_last": 0, "notify_status": "sent:persistent,mobile(2)", "attempts": 1,
            "metrics": {"failing_integrations": 2}, "friendly_name": st["description"], "icon": "mdi:alert-outline"})
        dumped = json.dumps(attrs)
        for banned in ("actions_selected", "restart_miele", "2026-08-17", "permission_denials", "secret raw", "input"):
            self.assertNotIn(banned, dumped)
        st["result"]["validation_errors"] = [f"e{i}" for i in range(15)]
        st["status"] = "error"
        _, state, attrs = jc.entity_payload(st)
        self.assertEqual(state, "error")
        self.assertEqual(attrs["validation_errors"], [f"e{i}" for i in range(10)])
        self.assertEqual(attrs["icon"], "mdi:alert-circle")
        for status, icon in jc.ICONS.items():
            if status != "running":
                self.assertEqual(jc.entity_payload(terminal_state(status=status))[2]["icon"], icon)
        # never-ran skipped/disabled: no result at all
        eid, state, attrs = jc.entity_payload({"job": "energy-report", "status": "skipped", "enabled": False,
                                               "description": "d", "result": None, "stats": {}})
        self.assertEqual((eid, state, attrs["headline"], attrs["detail"], attrs["run_count"], attrs["enabled"], attrs["metrics"]),
                         ("sensor.claude_job_energy_report", "skipped", "", "", 0, False, {}))

    def test_detail_truncation_at_line_boundary(self):
        lines = [("line %03d " % i) + "x" * 40 for i in range(40)]      # 49 chars + \n each = 50
        detail = "\n".join(lines)
        self.assertGreater(len(detail), 900)
        st = terminal_state()
        st["result"]["detail"] = detail
        attrs = jc.entity_payload(st)[2]
        self.assertTrue(attrs["detail_truncated"])
        self.assertLessEqual(len(attrs["detail"]), 900)
        self.assertEqual(attrs["detail"], "\n".join(lines[:18]))          # 18*50-1 = 899 chars, cut at the newline
        self.assertFalse(attrs["detail"].endswith("\n"))
        one_line = "y" * 2000
        st["result"]["detail"] = one_line
        attrs = jc.entity_payload(st)[2]
        self.assertEqual(attrs["detail"], "y" * 900)                       # no newline: hard cut
        exact = "z" * 900
        self.assertEqual(jc.truncate_detail(exact), (exact, False))
        self.assertEqual(jc.truncate_detail(None), ("", False))

    def test_cost_and_endpoint_payloads(self):
        eid, state, attrs = jc.cost_entity_payload(now=dt.datetime(2026, 8, 18, tzinfo=dt.timezone.utc))
        self.assertEqual((eid, state), ("sensor.claude_jobs_cost_raw", 0.0))
        self.assertEqual((attrs["month"], attrs["runs"], attrs["unit_of_measurement"], attrs["friendly_name"], attrs["icon"]),
                         ("2026-08", 0, "USD", "Claude Jobs cost this month (raw)", "mdi:cash"))
        eid, state, attrs = jc.endpoint_error_payload("cli_preflight_failed", missing=["--tools"], claude_version="2.1.240")
        self.assertEqual((eid, state, attrs["reason"], attrs["missing"], attrs["friendly_name"], attrs["icon"]),
                         ("sensor.claude_jobs_endpoint", "error", "cli_preflight_failed", ["--tools"], "Claude Jobs endpoint", "mdi:server-off"))
        self.assertEqual(jc.endpoint_error_payload("respawn_degraded", fast_crashes=5)[2]["fast_crashes"], 5)


class TestCost(JcCase):
    def test_accumulate_and_rollover(self):
        aug = dt.datetime(2026, 8, 18, 12, 0, tzinfo=dt.timezone.utc)
        c = jc.cost_add("health-check", 0.63121, "Europe/Vienna", now=aug)
        self.assertEqual((c["month"], c["time_zone"], c["total_usd"], c["runs"], c["by_job"]),
                         ("2026-08", "Europe/Vienna", 0.6312, 1, {"health-check": 0.6312}))
        self.assertEqual(stat.S_IMODE(os.stat(jc.COST_FILE).st_mode), 0o644)
        jc.cost_add("energy-report", 0.10, "Europe/Vienna", now=aug)
        c = jc.cost_add("health-check", 0.0, "Europe/Vienna", now=aug, count_run=False)
        self.assertEqual((c["total_usd"], c["runs"], c["by_job"]), (0.7312, 2, {"health-check": 0.6312, "energy-report": 0.1}))
        cur = jc.cost_current(now=aug)
        self.assertEqual((cur["month"], cur["total_usd"], cur["runs"], cur["current"], cur["month_start_iso"]),
                         ("2026-08", 0.7312, 2, True, "2026-08-01T00:00:00+02:00"))
        # 23:30 UTC on Aug 31 is already September in Vienna -> rollover keyed to HA's tz
        edge = dt.datetime(2026, 8, 31, 23, 30, tzinfo=dt.timezone.utc)
        self.assertFalse(jc.cost_current(now=edge)["current"])
        self.assertEqual(jc.cost_current(now=edge)["total_usd"], 0.0)
        self.assertEqual(jc.cost_entity_payload(now=edge)[1], 0.0)
        c = jc.cost_add("health-check", 0.5, "Europe/Vienna", now=edge)
        self.assertEqual((c["month"], c["total_usd"], c["runs"], c["by_job"]), ("2026-09", 0.5, 1, {"health-check": 0.5}))
        archived = jc.read_json(self.s.logs / "cost-2026-08.json")
        self.assertEqual((archived["month"], archived["total_usd"], archived["runs"]), ("2026-08", 0.7312, 2))
        self.assertEqual(jc.cost_current(now=edge)["month_start_iso"], "2026-09-01T00:00:00+02:00")
        # unknown/fallback tz: the file's valid zone wins (C1), so no bogus rollover back to August
        c = jc.cost_add("x", 1, "Mars/Olympus", now=edge)
        self.assertEqual((c["month"], c["time_zone"], c["total_usd"]), ("2026-09", "Europe/Vienna", 1.5))
        self.assertFalse((self.s.logs / "cost-2026-09.json").exists())
        # a fresh file written by a fallback caller records UTC honestly
        os.unlink(jc.COST_FILE)
        c = jc.cost_add("x", 1, None, now=edge)
        self.assertEqual((c["month"], c["time_zone"]), ("2026-08", "UTC"))

    def test_tz_fallback_flap_at_month_boundary(self):
        """C1: Vienna -> UTC fallback -> Vienna at 22:30Z on Jul 31 (= Aug 1 00:30 local) keeps one
        August accumulator, never touches the real July archive, and totals stay monotone."""
        july = {"schema": 1, "month": "2026-07", "time_zone": "Europe/Vienna", "total_usd": 3.0, "runs": 5,
                "by_job": {"health-check": 3.0}, "updated_at": "2026-07-31T20:00:00Z"}
        jc.atomic_write_json(self.s.logs / "cost-2026-07.json", july)
        t = dt.datetime(2026, 7, 31, 22, 30, tzinfo=dt.timezone.utc)
        a = jc.cost_add("health-check", 1.0, "Europe/Vienna", now=t)
        self.assertEqual((a["month"], a["total_usd"]), ("2026-08", 1.0))
        b = jc.cost_add("health-check", 1.0, "UTC", now=t + dt.timedelta(minutes=5))       # broker tz fetch failed
        self.assertEqual((b["month"], b["time_zone"], b["total_usd"], b["runs"]), ("2026-08", "Europe/Vienna", 2.0, 2))
        c = jc.cost_add("health-check", 1.0, "Europe/Vienna", now=t + dt.timedelta(minutes=10))
        self.assertEqual((c["month"], c["total_usd"], c["runs"]), ("2026-08", 3.0, 3))
        self.assertEqual(jc.read_json(self.s.logs / "cost-2026-07.json"), july)             # untouched
        self.assertEqual(sorted(f for f in os.listdir(self.s.logs) if f.startswith("cost-")), ["cost-2026-07.json"])
        # HA's zone genuinely changed (Vienna -> UTC config): no rollover until both zones agree it is a new month
        d = jc.cost_add("health-check", 1.0, "America/New_York", now=t + dt.timedelta(minutes=15))   # still Jul 31 in NY
        self.assertEqual((d["month"], d["time_zone"], d["total_usd"]), ("2026-08", "Europe/Vienna", 4.0))

    def test_archive_is_merged_never_clobbered(self):
        """C1(c): if an archive for the month somehow exists already, rollover merges additively."""
        jc.atomic_write_json(self.s.logs / "cost-2026-08.json",
                             {"schema": 1, "month": "2026-08", "time_zone": "UTC", "total_usd": 3.0, "runs": 4, "by_job": {"a": 3.0}})
        jc.atomic_write_json(jc.COST_FILE, {"schema": 1, "month": "2026-08", "time_zone": "UTC", "total_usd": 1.0,
                                            "runs": 1, "by_job": {"a": 0.5, "b": 0.5}})
        sep = dt.datetime(2026, 9, 2, tzinfo=dt.timezone.utc)
        c = jc.cost_add("a", 0.25, "UTC", now=sep)
        self.assertEqual((c["month"], c["total_usd"]), ("2026-09", 0.25))
        arch = jc.read_json(self.s.logs / "cost-2026-08.json")
        self.assertEqual((arch["total_usd"], arch["runs"], arch["by_job"]), (4.0, 5, {"a": 3.5, "b": 0.5}))
        self.assertIn("merged_at", arch)

    def test_cost_lock_busy_still_writes(self):
        fd = jc.flock_nb(jc.COST_LOCK)
        os.environ["CLAUDE_JOB_FAKE_NOW"] = "2026-08-18T00:00:00Z"
        try:
            orig = jc.flock_wait
            jc.flock_wait = lambda path, seconds, should_abort=None, interval=0.25: None   # simulate the 10 s expiry
            c = jc.cost_add("a", 0.5, "UTC")
            self.assertEqual(c["total_usd"], 0.5)
        finally:
            jc.flock_wait = orig
            jc.unlock(fd)
            del os.environ["CLAUDE_JOB_FAKE_NOW"]

    def test_fake_now_env(self):
        os.environ["CLAUDE_JOB_FAKE_NOW"] = "2027-02-03T00:00:00Z"
        try:
            self.assertEqual(jc.cost_add("a", 0.25, "UTC")["month"], "2027-02")
            self.assertEqual(jc.cost_current()["month_start_iso"], "2027-02-01T00:00:00+00:00")
        finally:
            del os.environ["CLAUDE_JOB_FAKE_NOW"]


class TestHaAndEnable(JcCase):
    def setUp(self):
        super().setUp()
        self.sup = FakeSupervisor().start()
        self.s.with_supervisor(self.sup)
        self.s.apply_to_process()
        jc.reload_paths()

    def tearDown(self):
        self.sup.stop()
        super().tearDown()

    def test_ha_request_and_post_state(self):
        status, body = jc.ha_request("GET", "/core/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["time_zone"], "Europe/Vienna")
        req = self.sup.requests[-1]
        self.assertEqual(req["headers"]["authorization"], "Bearer test-supervisor-token")
        self.assertEqual(jc.ha_json("GET", "/core/api/config")[1]["time_zone"], "Europe/Vienna")
        ok, status = jc.ha_post_state("sensor.claude_job_x", "ok", {"a": 1})
        self.assertEqual((ok, status), (True, 201))
        self.assertEqual(self.sup.states["sensor.claude_job_x"]["attributes"], {"a": 1})
        self.assertEqual(jc.ha_post_state("sensor.claude_job_x", "ok", {"a": 2}), (True, 200))
        self.assertEqual(jc.ha_request("GET", "/core/api/states/sensor.nope")[0], 404)
        self.sup.route("POST", "/core/api/states/", (500, {"message": "down"}), prefix=True)
        self.assertEqual(jc.ha_post_state("sensor.claude_job_y", "ok", {}), (False, 500))
        status, body = jc.ha_request("GET", "/x", base_url="http://127.0.0.1:1", timeout=2)
        self.assertEqual((status, body), (None, b""))
        self.assertEqual(jc.ha_request("GET", "/core/api/config", token="other")[0], 200)
        self.assertEqual(self.sup.requests[-1]["headers"]["authorization"], "Bearer other")
        # C2: never raises — malformed base URL, unencodable path pieces, header injection attempts
        self.sup.unroute("POST", "/core/api/states/")
        self.assertEqual(jc.ha_request("GET", "/x", base_url="not a url"), (None, b""))
        self.assertEqual(jc.ha_request("GET", "/x", base_url="ftp//:::"), (None, b""))
        status, _ = jc.ha_request("POST", "/core/api/states/sensor.x y", {"state": "1", "attributes": {}})
        self.assertEqual(status, 201)                                          # space was %-encoded, not raised
        self.assertEqual(self.sup.requests[-1]["path"], "/core/api/states/sensor.x%20y")
        status, _ = jc.ha_request("GET", "/core/api/config\r\nX-Evil: 1")
        self.assertIn(status, (200, 404))
        self.assertNotIn("x-evil", self.sup.requests[-1]["headers"])
        status, _ = jc.ha_request("GET", "/core/api/history/period/2026-08-17T00:00:00+02:00?filter_entity_id=a,b&minimal_response")
        self.assertEqual(status, 200)
        self.assertEqual(self.sup.requests[-1]["path"], "/core/api/history/period/2026-08-17T00:00:00+02:00")
        self.assertEqual(self.sup.requests[-1]["query"], "filter_entity_id=a,b&minimal_response")

    def test_environment_proxies_are_ignored(self):
        os.environ["http_proxy"] = os.environ["HTTP_PROXY"] = "http://127.0.0.1:1"
        os.environ["no_proxy"] = os.environ["NO_PROXY"] = ""
        try:
            self.assertEqual(jc.ha_request("GET", "/core/api/config")[0], 200)
        finally:
            for k in ("http_proxy", "HTTP_PROXY", "no_proxy", "NO_PROXY"):
                os.environ.pop(k, None)

    def test_addon_version(self):
        self.assertEqual(jc.addon_version(), "9.9.9-test")
        self.s.addon_version_file.unlink()
        jc.reload_paths()
        self.assertEqual(jc.addon_version(), "9.9.9-test")             # from fake /addons/self/info
        self.assertTrue(self.sup.find("GET", "/addons/self/info"))
        self.sup.route("GET", "/addons/self/info", (500, {}))
        jc.reload_paths()
        self.assertEqual(jc.addon_version(), "unknown")
        self.assertEqual(jc.built_cli_version(), "2.1.233")

    def test_set_enabled_and_flags(self):
        name = "health-check"
        self.assertFalse(jc.is_flag_disabled(name))
        self.s.write_state(name, terminal_state())
        r = jc.set_enabled(name, False)
        self.assertEqual((r["enabled"], r["flag_disabled"], r["state_written"], r["published"]), (False, True, True, True))
        self.assertTrue((self.s.disabled / "health_check").exists())
        self.assertTrue(jc.is_flag_disabled(name))
        st = self.s.read_state(name)
        self.assertEqual((st["enabled"], st["published"]), (False, True))
        posted = self.sup.states["sensor.claude_job_health_check"]
        self.assertEqual((posted["state"], posted["attributes"]["enabled"]), ("warning", False))
        r = jc.set_enabled(name, True)
        self.assertEqual((r["enabled"], r["flag_disabled"]), (True, False))
        self.assertFalse(jc.is_flag_disabled(name))
        self.assertTrue(self.sup.states["sensor.claude_job_health_check"]["attributes"]["enabled"])
        # frontmatter says disabled: flag removed but effective enabled stays False
        r = jc.set_enabled(name, True, frontmatter_enabled=False)
        self.assertEqual((r["enabled"], r["flag_disabled"], r["enabled_frontmatter"]), (False, False, False))
        # the human spelling of the flag is honored and cleared by enable
        self.s.set_flag_disabled(name)
        self.assertTrue(jc.is_flag_disabled(name))
        jc.set_enabled(name, True)
        self.assertFalse(jc.is_flag_disabled(name))
        self.assertFalse((self.s.disabled / name).exists())
        # never-ran job: flag only, no state, no POST
        before = len(self.sup.requests)
        r = jc.set_enabled("energy-report", False)
        self.assertEqual((r["state_written"], r["published"]), (False, None))
        self.assertEqual(len(self.sup.requests), before)
        self.assertIsNone(self.s.read_state("energy-report"))
        # busy lock: flag flips, state untouched
        fd = jc.flock_nb(jc.lock_path(name))
        try:
            r = jc.set_enabled(name, False)
            self.assertEqual((r["flag_disabled"], r["state_written"]), (True, False))
            self.assertTrue(self.s.read_state(name)["enabled"])
        finally:
            jc.unlock(fd)
        # HA down: state written with published false
        self.sup.route("POST", "/core/api/states/", (502, {}), prefix=True)
        r = jc.set_enabled(name, True)
        self.assertEqual((r["state_written"], r["published"]), (True, False))
        self.assertFalse(self.s.read_state(name)["published"])


class TestJobSummary(JcCase):
    def job(self, **kw):
        d = dict(slug="health_check", kind="job", description="Daily", enabled=True, stale_after=93600, model="fable",
                 mtime=time.time())
        d.update(kw)
        return types.SimpleNamespace(**d)

    def test_stale_and_fields(self):
        now = dt.datetime(2026, 8, 18, 12, 0, tzinfo=dt.timezone.utc)
        st = terminal_state()
        st["result"]["ended_at"] = "2026-08-17T11:00:00Z"        # 25 h ago < 26 h
        s = jc.job_summary("health-check", self.job(), st, now)
        self.assertEqual(s, {"name": "health-check", "slug": "health_check", "kind": "job", "description": "Daily",
                             "status": "warning", "headline": st["result"]["headline"], "last_run": "2026-08-17T11:00:00Z",
                             "enabled": True, "enabled_frontmatter": True, "flag_disabled": False, "stale": False,
                             "stale_after": 93600, "model": "fable", "valid": True, "errors": 0, "run_count": 12,
                             "cost_usd_last": 0.63})
        st["result"]["ended_at"] = "2026-08-17T09:00:00Z"        # 27 h ago
        self.assertTrue(jc.job_summary("health-check", self.job(), st, now)["stale"])
        self.assertFalse(jc.job_summary("health-check", self.job(stale_after=None), st, now)["stale"])
        # disabled jobs are never stale
        self.assertFalse(jc.job_summary("health-check", self.job(enabled=False), st, now)["stale"])
        self.s.set_flag_disabled("health-check")
        s = jc.job_summary("health-check", self.job(), st, now)
        self.assertEqual((s["enabled"], s["flag_disabled"], s["stale"]), (False, True, False))
        self.s.set_flag_disabled("health-check", False)
        # never ran: last_run from the file mtime
        old = now.timestamp() - 2 * 86400
        s = jc.job_summary("health-check", self.job(mtime=old), None, now)
        self.assertEqual((s["status"], s["last_run"], s["stale"], s["headline"], s["run_count"], s["cost_usd_last"]),
                         ("never_run", jc.iso(dt.datetime.fromtimestamp(old, dt.timezone.utc)), True, None, 0, None))
        self.assertFalse(jc.job_summary("health-check", self.job(mtime=now.timestamp() - 60), None, now)["stale"])
        # definition failed to load: state supplies description/stale_after, mtime from the file on disk
        p = self.s.write_job("health-check", {"description": "x", "tools": ["nope"]})
        os.utime(p, (old, old))
        s = jc.job_summary("health-check", None, None, now, load_errors=["tools[0]: unknown tool"])
        self.assertEqual((s["valid"], s["errors"], s["kind"], s["slug"], s["stale"], s["stale_after"]), (False, 1, "job", "health_check", False, None))
        s = jc.job_summary("health-check", None, st, now, load_errors=["e1", "e2"])
        self.assertEqual((s["description"], s["stale_after"], s["model"], s["stale"], s["errors"]), (st["description"], 93600, "fable", True, 2))
        # works with a real JobDef too
        self.s.write_job("health-check", {"description": "Daily", "tools": ["Bash(ha core info)"], "stale_after": 93600})
        real = jobdef.load("health-check")
        self.assertTrue(jc.job_summary("health-check", real, st, now)["stale"])


class TestToken(JcCase):
    def test_modes_and_rotation(self):
        self.assertIsNone(jc.read_token())
        os.chmod(self.s.data, 0o755)
        tok = jc.ensure_token()
        self.assertRegex(tok, r"^[0-9a-f]{64}$")
        self.assertEqual(jc.read_token(), tok)
        self.assertEqual(stat.S_IMODE(os.stat(jc.TOKEN_FILE).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(jc.DATA_DIR).st_mode), 0o700)
        self.assertEqual(jc.ensure_token(), tok)
        new = jc.write_token()
        self.assertNotEqual(new, tok)
        self.assertEqual(jc.read_token(), new)
        self.assertEqual(jc.write_token("ab" * 32), "ab" * 32)
        self.assertEqual((self.s.data / "token").read_text(), "ab" * 32 + "\n")
        (self.s.data / "token").write_text("  \n")
        self.assertIsNone(jc.read_token())


class TestPreflight(JcCase):
    def test_against_fake_claude(self):
        r = jc.cli_preflight(force=True)
        self.assertEqual((r["ok"], r["missing"], r["json_schema_fallback"], r["claude_version"], r["built_version"], r["cli_drift"]),
                         (True, [], False, "2.1.233", "2.1.233", False))
        self.assertEqual(r["claude_realpath"], os.path.realpath(self.s.claude_wrapper))
        cached = jc.read_json(jc.PREFLIGHT_CACHE)
        self.assertEqual(cached["ok"], True)
        self.assertEqual(stat.S_IMODE(os.stat(jc.PREFLIGHT_CACHE).st_mode), 0o600)
        # cache is used while fresh (the hidden flag is not noticed without force)...
        os.environ["FAKE_CLAUDE_HIDE_FLAG"] = "--tools,--settings"
        self.assertTrue(jc.cli_preflight()["ok"])
        # ...but force re-probes
        r = jc.cli_preflight(force=True)
        self.assertEqual((r["ok"], r["missing"], r["json_schema_fallback"]), (False, ["--settings", "--tools"], False))
        os.environ["FAKE_CLAUDE_HIDE_FLAG"] = "--json-schema"
        r = jc.cli_preflight(force=True)
        self.assertEqual((r["ok"], r["missing"], r["json_schema_fallback"]), (True, ["--json-schema"], True))
        os.environ["FAKE_CLAUDE_HIDE_FLAG"] = ""
        os.environ["FAKE_CLAUDE_VERSION"] = "2.1.240"
        r = jc.cli_preflight(force=True)
        self.assertEqual((r["ok"], r["claude_version"], r["cli_drift"]), (True, "2.1.240", True))
        # binary mtime change invalidates the cache without force
        os.environ["FAKE_CLAUDE_VERSION"] = "2.1.241"
        later = time.time() + 10
        os.utime(os.path.realpath(self.s.claude_wrapper), (later, later))
        self.assertEqual(jc.cli_preflight()["claude_version"], "2.1.241")
        # missing binary
        os.environ["CLAUDE_JOB_CLAUDE_BIN"] = str(self.s.bin / "no-such-claude")
        jc.reload_paths()
        r = jc.cli_preflight(force=True)
        self.assertEqual((r["ok"], r["claude_version"]), (False, None))
        self.assertEqual(r["missing"], list(jc.CLI_PREFLIGHT_FLAGS))
        self.assertIn("not found", r["error"])


class TestImportHygiene(unittest.TestCase):
    def test_jobcommon_does_not_import_jobdef(self):
        src = (LIB_DIR / "jobcommon.py").read_text()
        self.assertNotIn("import jobdef", src)
        self.assertNotIn("from jobdef", src)
        importlib.reload(jc)
        self.assertTrue(callable(jc.reload_paths))


if __name__ == "__main__":
    unittest.main()
