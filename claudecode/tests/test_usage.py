"""Tests for `usage.py` — the Claude subscription usage poller behind `GET /usage`.

In-process against a ScratchRoot (credential store) and a FakeSupervisor standing in for
api.anthropic.com (`CLAUDE_JOB_USAGE_URL` points at it). Nothing here touches the network,
a real token, or the CLI. The HTTP route and the tick integration live in test_endpoint.py.
"""
import io
import json
import os
import sys
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from testlib import ScratchRoot, TESTS_DIR
from fakes.fake_supervisor import FakeSupervisor
import jobcommon as jc
import usage

FIXTURE = TESTS_DIR / "fixtures" / "usage_response.json"
RECORDED = json.loads(FIXTURE.read_text())
USAGE_PATH = "/fake/api/oauth/usage"
TOKEN = "test-oauth-token"          # what ScratchRoot writes into the credential store


class UsageCase(unittest.TestCase):
    def setUp(self):
        self.s = ScratchRoot().start()
        self.sup = FakeSupervisor().start()
        self.s.setenv(CLAUDE_JOB_USAGE_URL=self.sup.url + USAGE_PATH, CLAUDE_JOB_USAGE_TIMEOUT_S="5")
        self.s.apply_to_process()
        jc.reload_paths()
        self.sup.route("GET", USAGE_PATH, (200, RECORDED))

    def tearDown(self):
        self.sup.stop()
        self.s.stop()
        jc.reload_paths()

    def poller(self, **kw):
        kw.setdefault("enabled", True)
        kw.setdefault("interval_s", 60)
        return usage.UsagePoller(**kw)

    def usage_requests(self):
        return self.sup.find("GET", USAGE_PATH)

    def write_creds(self, token=TOKEN, expires_ms=None, **extra):
        obj = {"claudeAiOauth": dict({"accessToken": token, "refreshToken": "rt-secret",
                                      "expiresAt": expires_ms if expires_ms is not None else int(time.time() * 1000) + 3600_000,
                                      "subscriptionType": "max", "rateLimitTier": "default_claude_max_5x"}, **extra)}
        self.s.credentials_file.write_text(json.dumps(obj))
        return obj


# ---- the parser (pure) ---------------------------------------------------------------------------
class TestParse(unittest.TestCase):
    def test_recorded_fixture(self):
        d = usage.parse_usage(RECORDED)
        self.assertIsNotNone(d)
        self.assertEqual(d["session"], {"used_percent": 12.5, "resets_at": "2026-09-17T15:00:00+00:00",
                                        "severity": "info", "is_active": True})
        w = d["weekly"]
        self.assertEqual((w["used_percent"], w["resets_at"], w["severity"], w["is_active"]),
                         (41.3, "2026-09-21T07:00:00+00:00", "info", True))
        self.assertEqual(w["breakdown"], {"claude_code": 30.1, "chats": 6.0, "cowork": 4.2, "other": 1.0})
        self.assertEqual([r["display_name"] for r in w["breakdown_rows"]], ["Claude Code", "Chats", "Cowork", "Other"])
        self.assertEqual(w["breakdown_rows"][0], {"key": "claude_code", "display_name": "Claude Code", "percent": 30.1})
        self.assertEqual((w["breakdown_as_of"], w["breakdown_window_started_at"]),
                         ("2026-09-17T12:34:56+00:00", "2026-09-14T07:00:00+00:00"))
        self.assertEqual(d["weekly_scoped"], {"used_percent": 57.0, "resets_at": "2026-09-21T07:00:00+00:00",
                                              "severity": "warning", "is_active": True, "model": "Fable"})
        self.assertEqual(d["weekly_scoped_all"], [d["weekly_scoped"]])
        self.assertEqual(d["extra_usage"], {"is_enabled": True, "monthly_limit": 50.0, "used_credits": 7.25,
                                            "utilization": 14.5, "currency": "USD", "disabled_reason": None})
        # nothing but derived numbers/strings: no token-shaped or unknown keys leak through
        self.assertEqual(sorted(d), ["extra_usage", "session", "weekly", "weekly_scoped", "weekly_scoped_all"])
        self.assertNotIn("_fixture_note", json.dumps(d))

    def test_limits_fill_in_for_missing_blocks_and_highest_scoped_cap_wins(self):
        obj = {"limits": [
            {"kind": "session", "percent": 80, "resets_at": "2026-09-17T15:00:00Z", "severity": "warning"},
            {"kind": "weekly_all", "percent": 20, "resets_at": "2026-09-21T07:00:00Z"},
            {"kind": "weekly_scoped", "percent": 30, "scope": {"model": {"display_name": "Opus"}}},
            {"kind": "weekly_scoped", "percent": 65, "scope": {"model": {"display_name": "Fable"}}, "is_active": False},
        ]}
        d = usage.parse_usage(obj)
        self.assertEqual((d["session"]["used_percent"], d["session"]["severity"]), (80.0, "warning"))
        self.assertEqual(d["weekly"]["used_percent"], 20.0)
        self.assertEqual((d["weekly_scoped"]["model"], d["weekly_scoped"]["used_percent"], d["weekly_scoped"]["is_active"]),
                         ("Fable", 65.0, False))
        self.assertEqual([s["model"] for s in d["weekly_scoped_all"]], ["Opus", "Fable"])
        self.assertEqual(d["weekly"]["breakdown"], {})
        self.assertEqual(d["extra_usage"]["is_enabled"], None)

    def test_blocks_beat_limits_and_bad_values_become_null(self):
        obj = {"five_hour": {"utilization": 33.333, "resets_at": "garbage"},
               "seven_day": {"utilization": "50", "resets_at": None},
               "limits": [{"kind": "session", "percent": 99, "resets_at": "2026-09-17T15:00:00Z"},
                          {"kind": "weekly_all", "percent": True}],
               "seven_day_breakdown": {"rows": [{"display_name": "Claude Code", "percent": float("nan")}]}}
        d = usage.parse_usage(obj)
        self.assertEqual(d["session"]["used_percent"], 33.3)                     # block wins over limits.percent
        self.assertEqual(d["session"]["resets_at"], "2026-09-17T15:00:00+00:00")  # limits fill the bad block value
        self.assertIsNone(d["weekly"]["used_percent"])                          # "50" (string) and True are not numbers
        self.assertEqual(d["weekly"]["breakdown"], {})                            # NaN row dropped
        self.assertEqual(d["weekly"]["breakdown_rows"], [])

    def test_malformed_documents_are_none(self):
        for bad in (None, [], "x", 3, {}, {"five_hour": None, "seven_day": None, "limits": None},
                    {"five_hour": {"utilization": "n/a"}, "limits": [{"kind": "weekly_scoped"}]},
                    {"limits": ["session"]}, {"unrelated": {"utilization": 5}}):
            self.assertIsNone(usage.parse_usage(bad), bad)

    def test_slug(self):
        self.assertEqual(usage.slug("Claude Code"), "claude_code")
        self.assertEqual(usage.slug("  Chats/Projects -- beta "), "chats_projects_beta")
        self.assertIsNone(usage.slug(""))
        self.assertIsNone(usage.slug(None))

    def test_options(self):
        self.assertTrue(usage.option_enabled({}))
        self.assertTrue(usage.option_enabled({"enable_usage_sensors": True}))
        self.assertFalse(usage.option_enabled({"enable_usage_sensors": False}))
        self.assertFalse(usage.option_enabled({"enable_usage_sensors": "false"}))
        self.assertEqual(usage.option_interval({}), 300)
        self.assertEqual(usage.option_interval({"usage_poll_interval": 120}), 120)
        self.assertEqual(usage.option_interval({"usage_poll_interval": 5}), 60)        # clamped to the schema
        self.assertEqual(usage.option_interval({"usage_poll_interval": 99999}), 3600)
        self.assertEqual(usage.option_interval({"usage_poll_interval": "abc"}), 300)
        self.assertEqual(usage.option_interval({"usage_poll_interval": True}), 300)


# ---- the credential reader (jobcommon) --------------------------------------------------------------
class TestCredentials(UsageCase):
    def test_newest_lineage_wins_and_nothing_is_written(self):
        self.write_creds("store-token", expires_ms=2000)
        before = self.s.credentials_file.read_text()
        self.assertEqual(jc.oauth_credentials()["accessToken"], "store-token")
        # a mid-run refresh leftover in the job config dir that is NEWER wins ...
        leftover = Path(jc.JOB_CONFIG_DIR) / ".credentials.json"
        leftover.parent.mkdir(parents=True, exist_ok=True)
        leftover.write_text(json.dumps({"claudeAiOauth": {"accessToken": "job-token", "expiresAt": 3000}}))
        self.assertEqual(jc.oauth_credentials()["accessToken"], "job-token")
        # ... an OLDER one does not; and reading reconciles nothing (no write-back, no relink)
        leftover.write_text(json.dumps({"claudeAiOauth": {"accessToken": "stale-token", "expiresAt": 1000}}))
        self.assertEqual(jc.oauth_credentials()["accessToken"], "store-token")
        self.assertEqual(self.s.credentials_file.read_text(), before)
        self.assertFalse(leftover.is_symlink())
        # unreadable / missing / no block
        self.s.credentials_file.write_text("{not json")
        leftover.unlink()
        self.assertIsNone(jc.oauth_credentials())
        self.s.credentials_file.write_text('{"somethingElse": 1}')
        self.assertIsNone(jc.oauth_credentials())
        self.s.credentials_file.unlink()
        self.assertIsNone(jc.oauth_credentials())


# ---- the poller against a fake api.anthropic.com -----------------------------------------------------
class TestPoller(UsageCase):
    def test_success_snapshot_headers_and_no_token_in_output(self):
        self.write_creds(expires_ms=1_800_000_000_000)          # 2027-01-15T08:00:00Z
        p = self.poller()
        before = p.snapshot()
        self.assertEqual((before["enabled"], before["ok"], before["stale"], before["has_data"]), (True, False, False, False))
        self.assertIsNone(before["session"]["used_percent"])
        self.assertIsNone(before["last_success"])
        self.assertTrue(p.poll_once())
        reqs = self.usage_requests()
        self.assertEqual(len(reqs), 1)
        h = reqs[0]["headers"]
        self.assertEqual(h["authorization"], f"Bearer {TOKEN}")
        self.assertEqual(h["anthropic-beta"], "oauth-2025-04-20")
        self.assertEqual(h["user-agent"], "claude-code")
        snap = p.snapshot()
        self.assertEqual((snap["ok"], snap["stale"], snap["has_data"], snap["last_error"]), (True, False, True, None))
        self.assertRegex(snap["last_success"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(snap["last_success"], snap["last_attempt"])
        self.assertEqual(snap["age_s"], 0)
        self.assertEqual((snap["poll_interval_s"], snap["polls"], snap["last_http_status"]), (60, 1, 200))
        self.assertEqual(snap["session"]["used_percent"], 12.5)
        self.assertEqual(snap["weekly"]["breakdown"]["claude_code"], 30.1)
        self.assertEqual(snap["weekly_scoped"]["model"], "Fable")
        self.assertEqual(snap["extra_usage"]["used_credits"], 7.25)
        self.assertEqual(snap["credential_expires_at"], "2027-01-15T08:00:00Z")
        self.assertEqual((snap["subscription_type"], snap["rate_limit_tier"]), ("max", "default_claude_max_5x"))
        text = json.dumps(snap)
        for secret in (TOKEN, "rt-secret", "refreshToken", "accessToken"):
            self.assertNotIn(secret, text)
        # the snapshot is a copy: mutating it never touches the poller's own data
        snap["session"]["used_percent"] = 999
        self.assertEqual(p.snapshot()["session"]["used_percent"], 12.5)

    def test_401_keeps_last_good_values_and_flags_stale(self):
        self.write_creds(expires_ms=1000)                          # long expired by its own clock
        p = self.poller()
        self.assertTrue(p.poll_once())
        good = p.snapshot()
        self.sup.route("GET", USAGE_PATH, (401, {"error": {"type": "authentication_error"}}))
        time.sleep(0.01)
        self.assertFalse(p.poll_once())
        snap = p.snapshot()
        self.assertEqual((snap["ok"], snap["stale"], snap["has_data"], snap["last_error"], snap["last_http_status"]),
                         (False, True, True, "unauthorized", 401))
        self.assertIn("access token rejected", snap["last_error_detail"])
        self.assertEqual(snap["last_success"], good["last_success"])              # unchanged
        self.assertNotEqual(snap["last_attempt"], None)
        self.assertEqual(snap["session"], good["session"])                        # the last good values, not zeros
        self.assertEqual(snap["weekly"], good["weekly"])
        self.assertEqual(snap["extra_usage"], good["extra_usage"])
        self.assertEqual(snap["credential_expires_at"], "1970-01-01T00:00:01Z")
        # recovery clears the flag
        self.sup.route("GET", USAGE_PATH, (200, RECORDED))
        self.assertTrue(p.poll_once())
        snap = p.snapshot()
        self.assertEqual((snap["ok"], snap["stale"], snap["last_error"]), (True, False, None))

    def test_401_without_any_good_values_publishes_nothing(self):
        self.write_creds()
        self.sup.route("GET", USAGE_PATH, (401, "nope"))
        p = self.poller()
        self.assertFalse(p.poll_once())
        snap = p.snapshot()
        self.assertEqual((snap["ok"], snap["stale"], snap["has_data"], snap["last_error"]), (False, False, False, "unauthorized"))
        for window in ("session", "weekly", "weekly_scoped"):
            self.assertIsNone(snap[window]["used_percent"])
            self.assertIsNone(snap[window]["resets_at"])
        self.assertEqual(snap["weekly"]["breakdown"], {})
        self.assertIsNone(snap["extra_usage"]["used_credits"])
        self.assertIsNone(snap["last_success"])
        self.assertIsNone(snap["age_s"])

    def test_malformed_bodies(self):
        self.write_creds()
        p = self.poller()
        self.assertTrue(p.poll_once())
        cases = [
            (200, b"<html>maintenance</html>", "not JSON"),
            (200, {"unrelated": 1}, "no usage window"),
            (200, [1, 2, 3], "no usage window"),
            (200, b"\xff\xfe", "not JSON"),
            (200, b"x" * (usage.MAX_BODY_BYTES + 10), "over"),
        ]
        for status, body, needle in cases:
            self.sup.route("GET", USAGE_PATH, (status, body))
            self.assertFalse(p.poll_once(), needle)
            snap = p.snapshot()
            self.assertEqual((snap["last_error"], snap["stale"], snap["ok"]), ("malformed_body", True, False), needle)
            self.assertIn(needle, snap["last_error_detail"])
            self.assertEqual(snap["session"]["used_percent"], 12.5, "last good values survive a malformed body")

    def test_other_failures(self):
        self.write_creds()
        p = self.poller()
        self.sup.route("GET", USAGE_PATH, (429, {"error": "rate"}))
        self.assertFalse(p.poll_once())
        self.assertEqual(p.snapshot()["last_error"], "rate_limited")
        self.sup.route("GET", USAGE_PATH, (503, "down"))
        self.assertFalse(p.poll_once())
        self.assertEqual((p.snapshot()["last_error"], p.snapshot()["last_error_detail"]), ("http_error", "HTTP 503"))
        self.sup.route("GET", USAGE_PATH, (403, "forbidden"))
        self.assertFalse(p.poll_once())
        self.assertEqual(p.snapshot()["last_error"], "unauthorized")
        # transport failure: nothing listening at the URL
        os.environ["CLAUDE_JOB_USAGE_URL"] = "http://127.0.0.1:9/nothing"
        jc.reload_paths()
        self.assertFalse(p.poll_once())
        snap = p.snapshot()
        self.assertEqual(snap["last_error"], "unreachable")
        self.assertNotIn("127.0.0.1", snap["last_error_detail"])     # class name only, never the URL/exception text
        self.assertNotIn(TOKEN, snap["last_error_detail"])
        # no credentials at all
        os.environ["CLAUDE_JOB_USAGE_URL"] = self.sup.url + USAGE_PATH
        jc.reload_paths()
        self.s.credentials_file.write_text('{"claudeAiOauth": {"accessToken": "   "}}')
        self.assertFalse(p.poll_once())
        self.assertEqual(p.snapshot()["last_error"], "no_credentials")
        self.s.credentials_file.unlink()
        self.assertFalse(p.poll_once())
        self.assertEqual(p.snapshot()["last_error"], "no_credentials")
        self.assertIsNone(p.snapshot()["credential_expires_at"])
        self.assertIsNone(p.snapshot()["subscription_type"])

    def test_failures_are_logged_once_per_error_kind_and_recovery_once(self):
        self.write_creds()
        p = self.poller()
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.sup.route("GET", USAGE_PATH, (401, "no"))
            for _ in range(5):
                p.poll_once()
            self.sup.route("GET", USAGE_PATH, (503, "down"))
            for _ in range(3):
                p.poll_once()
            self.sup.route("GET", USAGE_PATH, (200, RECORDED))
            for _ in range(3):
                p.poll_once()
        lines = [l for l in buf.getvalue().splitlines() if "usage:" in l]
        self.assertEqual(len(lines), 3, lines)
        self.assertIn("unauthorized", lines[0])
        self.assertIn("no values yet", lines[0])
        self.assertIn("http_error", lines[1])
        self.assertIn("recovered (http_error cleared)", lines[2])
        for line in lines:
            self.assertNotIn(TOKEN, line)
        self.assertEqual(len(self.usage_requests()), 11)

    def test_maybe_poll_is_due_gated_single_flight_and_off_thread(self):
        self.write_creds()
        gate = threading.Event()

        def slow(req):
            gate.wait(5)
            return (200, RECORDED)
        self.sup.route("GET", USAGE_PATH, slow)
        p = self.poller(interval_s=60)
        t0 = time.monotonic()
        t = p.maybe_poll()
        self.assertIsNotNone(t)
        self.assertLess(time.monotonic() - t0, 1.0, "maybe_poll must return without waiting on the request")
        self.assertTrue(t.is_alive())
        self.assertIsNone(p.maybe_poll(), "a second call while one is in flight starts nothing")
        self.assertIsNone(p.maybe_poll())
        gate.set()
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(p.snapshot()["session"]["used_percent"], 12.5)
        self.assertEqual(len(self.usage_requests()), 1)
        self.assertIsNone(p.maybe_poll(), "not due again until interval_s has passed")
        p.last_attempt_mono -= 61
        t = p.maybe_poll()
        self.assertIsNotNone(t)
        t.join(5)
        self.assertEqual(len(self.usage_requests()), 2)

    def test_disabled_poller_never_polls(self):
        self.write_creds()
        p = self.poller(enabled=False)
        self.assertIsNone(p.maybe_poll())
        self.assertFalse(p.due())
        self.assertEqual(self.usage_requests(), [])
        self.assertFalse(p.snapshot()["enabled"])

    def test_options_drive_the_defaults(self):
        self.s.options_file.write_text(json.dumps({"enable_job_endpoint": True, "enable_usage_sensors": False,
                                                   "usage_poll_interval": 900}))
        p = usage.UsagePoller()
        self.assertEqual((p.enabled, p.interval_s), (False, 900))
        self.s.options_file.write_text(json.dumps({"enable_job_endpoint": True}))
        p = usage.UsagePoller()
        self.assertEqual((p.enabled, p.interval_s), (True, 300))

    def test_internal_error_in_a_poll_releases_single_flight(self):
        self.write_creds()
        p = self.poller()
        saved = usage.fetch
        usage.fetch = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            t = p.maybe_poll()
            t.join(5)
        finally:
            usage.fetch = saved
        snap = p.snapshot()
        self.assertEqual(snap["last_error"], "unreachable")
        self.assertIn("internal: RuntimeError", snap["last_error_detail"])
        self.assertFalse(p.inflight.locked())
        p.last_attempt_mono -= 61
        t = p.maybe_poll()
        t.join(5)
        self.assertTrue(p.snapshot()["ok"])


if __name__ == "__main__":
    sys.exit(unittest.main())
