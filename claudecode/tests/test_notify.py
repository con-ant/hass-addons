"""Tests for `claude-job-notify` (breakdown 2h, §3 U4; design §4.8; payloads per CONTRACTS A28).

Every test drives the real executable against a FakeSupervisor. Successive runs of one job are
simulated exactly as the runner would: the previous invocation's `last_notified` (from stdout)
is written back into the next state file under `notify.last_notified`.
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import time
import unittest
from unittest import mock

from testlib import HEALTH_CHECK_FM, NOTIFIER, ScratchRoot, run_cli
from fakes.fake_supervisor import DEFAULT_SERVICES, FakeSupervisor
import jobdef

JOB = "health-check"
SLUG = "health_check"
SERVICES_PREFIX = "/core/api/services/"
CRITICAL_DATA_KEYS = {"tag", "group", "channel", "push", "ttl", "priority", "importance"}


def services_with(notify_services: dict) -> list:
    """DEFAULT_SERVICES with the notify domain replaced."""
    return [d for d in DEFAULT_SERVICES if d["domain"] != "notify"] + [{"domain": "notify", "services": notify_services}]


class NotifyTestCase(unittest.TestCase):
    job_frontmatter = HEALTH_CHECK_FM      # notify: critical [mobile_critical, persistent], warning [mobile, persistent], ok [state_only]

    def setUp(self):
        self.s = ScratchRoot().start()
        self.sup = FakeSupervisor().start()
        self.s.with_supervisor(self.sup)
        self.s.setenv(CLAUDE_JOB_WEBHOOK_RETRY_S="0,0")
        self.s.write_job(JOB, self.job_frontmatter)
        self.last = None            # last_notified carried between invocations, like the runner does
        self.prev_status = None
        self.run_no = 0

    def tearDown(self):
        self.sup.stop()
        self.s.stop()

    # -- helpers ---------------------------------------------------------------------------------
    def channels_for(self, status):
        notify = self.job_frontmatter.get("notify", {})
        chosen = notify.get(status, jobdef.DEFAULT_NOTIFY[status])
        return [c for c in chosen if c != "state_only"]

    def state_for(self, status, headline, detail, channels, job=JOB, extra_result=None):
        self.run_no += 1
        result = {"run_id": f"run-20260818T0500{self.run_no:02d}Z-3f9a", "session_id": "s-1", "status": status,
                  "headline": headline, "detail": detail, "reason": None, "ended_at": "2026-08-18T05:01:12Z",
                  "cost_usd": 0.6312, "metrics": {"failing": 2}}
        result.update(extra_result or {})
        return {"schema": 1, "job": job, "slug": job.replace("-", "_"), "status": status, "enabled": True,
                "published": True, "updated_at": "2026-08-18T05:01:12Z", "description": "d", "stale_after": None,
                "run": None, "result": result, "prev_status": self.prev_status,
                "stats": {"run_count": self.run_no},
                "notify": {"channels": channels, "notify_status": None, "last_notified": self.last}}

    def notify(self, status, headline="2 integrations failing", *, detail="detail text", channels=None,
               dry_run=False, carry=True, job=JOB, extra_result=None, omit_channels=False):
        """Write the state file, run the notifier, return (rc, parsed stdout | None, stderr)."""
        chans = self.channels_for(status) if channels is None else channels
        state = self.state_for(status, headline, detail, chans, job=job, extra_result=extra_result)
        if omit_channels:
            del state["notify"]["channels"]
        path = self.s.write_state(job, state)
        self.sup.clear()
        argv = [sys.executable, NOTIFIER, job, "--state-file", path] + (["--dry-run"] if dry_run else [])
        rc, out, err = run_cli(argv, self.s.env, timeout=30)
        parsed = None
        if out:
            lines = out.splitlines()
            self.assertEqual(len(lines), 1, f"stdout must be exactly one line, got {out!r}")
            parsed = json.loads(lines[0])
            if carry and not dry_run:
                self.last = parsed.get("last_notified")
                self.prev_status = status
        return rc, parsed, err

    def posts(self, suffix=""):
        return self.sup.find("POST", SERVICES_PREFIX + suffix)

    def mobile_posts(self):
        return [r for r in self.posts("notify/") if r["path"].split("/")[-1].startswith("mobile_app_")]

    def persistent_creates(self):
        return self.posts("persistent_notification/create")

    def dismisses(self):
        return self.posts("persistent_notification/dismiss")


class TestModeMatrix(NotifyTestCase):
    def test_warning_sequence_full_quiet_full(self):
        rc, out, _ = self.notify("warning", "2 integrations failing")
        self.assertEqual(rc, 0)
        self.assertEqual(out["mode"], "full")
        self.assertEqual(out["notify_status"], "sent:persistent,mobile(2)")
        self.assertEqual(len(self.persistent_creates()), 1)
        self.assertEqual(len(self.mobile_posts()), 2)
        self.assertEqual(out["last_notified"]["consecutive_same"], 1)
        self.assertTrue(out["last_notified"]["persistent_active"])
        body = self.mobile_posts()[0]["json"]
        self.assertEqual(body["title"], "Warning · health-check")
        self.assertEqual(body["message"], "2 integrations failing")
        self.assertEqual(body["data"], {"tag": "claude_job_health_check", "group": "claude_jobs", "channel": "Claude Jobs"})

        # same status, same headline -> quiet: persistent refreshed, phones silent
        rc, out, _ = self.notify("warning", "2 integrations failing")
        self.assertEqual(out["mode"], "quiet")
        self.assertEqual(len(self.persistent_creates()), 1)
        self.assertEqual(self.mobile_posts(), [])
        self.assertEqual(out["notify_status"], "quiet:mobile,persistent")
        self.assertEqual(out["last_notified"]["consecutive_same"], 2)

        # headline changed -> full again
        rc, out, _ = self.notify("warning", "3 integrations failing")
        self.assertEqual(out["mode"], "full")
        self.assertEqual(len(self.mobile_posts()), 2)
        self.assertEqual(out["last_notified"]["consecutive_same"], 1)

        # warning -> critical: full, with exactly the A28 critical payload keys
        rc, out, err = self.notify("critical", "3 integrations failing")
        self.assertEqual(out["mode"], "full")
        self.assertEqual(out["notify_status"], "sent:persistent,mobile_critical(2)")
        body = self.mobile_posts()[0]["json"]
        self.assertEqual(set(body["data"]), CRITICAL_DATA_KEYS)
        self.assertEqual(body["data"]["push"], {"sound": {"name": "default", "critical": 1, "volume": 1.0}})
        self.assertEqual(body["data"]["ttl"], 0)
        self.assertEqual(body["data"]["priority"], "high")
        self.assertEqual(body["data"]["importance"], "high")
        self.assertEqual(body["data"]["channel"], "Claude Jobs Critical")
        self.assertEqual(body["title"], "CRITICAL · health-check")

    def test_critical_renag_every_3_pushes_at_repeat_1_and_4(self):
        pushes = []
        for _ in range(7):
            rc, out, _ = self.notify("critical", "disk 97% full")
            self.assertEqual(rc, 0)
            pushes.append(len(self.mobile_posts()))
            self.assertEqual(len(self.persistent_creates()), 1)     # persistent refreshed every time
        self.assertEqual(pushes, [2, 0, 0, 2, 0, 0, 2])
        self.assertEqual(self.last["consecutive_same"], 7)

    def test_renag_zero_disables(self):
        fm = dict(HEALTH_CHECK_FM, renag_every=0)
        self.s.write_job(JOB, fm)
        pushes = [len(self.mobile_posts()) for _ in range(5) if self.notify("critical", "x")[0] == 0]
        self.assertEqual(pushes, [2, 0, 0, 0, 0])

    def test_recovery_default_map_dismisses_without_mobile(self):
        self.notify("critical", "disk 97% full")
        self.assertTrue(self.last["persistent_active"])
        rc, out, _ = self.notify("ok", "all good")          # ok -> [state_only]
        self.assertEqual(rc, 0)
        self.assertEqual(out["mode"], "none")
        self.assertEqual(len(self.dismisses()), 1)
        self.assertEqual(self.dismisses()[0]["json"], {"notification_id": "claude_job_health_check"})
        self.assertEqual(self.mobile_posts(), [])
        self.assertEqual(self.persistent_creates(), [])
        self.assertEqual(out["notify_status"], "state_only;dismissed:persistent")
        self.assertFalse(out["last_notified"]["persistent_active"])
        # a second ok does not dismiss again
        rc, out, _ = self.notify("ok", "all good")
        self.assertEqual(self.dismisses(), [])
        self.assertEqual(out["notify_status"], "state_only")

    def test_recovery_with_notify_recovery_pushes_resolved(self):
        self.s.write_job(JOB, dict(HEALTH_CHECK_FM, notify_recovery=True))
        self.notify("critical", "disk 97% full")
        rc, out, _ = self.notify("ok", "disk back to 41%")
        self.assertEqual(out["mode"], "recovery")
        self.assertEqual(len(self.dismisses()), 1)
        mobile = self.mobile_posts()
        self.assertEqual(len(mobile), 2)
        self.assertEqual(mobile[0]["json"]["message"], "Resolved: disk back to 41%")
        self.assertEqual(mobile[0]["json"]["title"], "OK · health-check")
        self.assertNotIn("push", mobile[0]["json"]["data"])      # a resolution is not a critical alert
        self.assertEqual(out["notify_status"], "sent:mobile(2);dismissed:persistent")

    def test_recovery_with_persistent_in_ok_map_replaces_instead_of_dismissing(self):
        self.notify("warning", "x")
        rc, out, _ = self.notify("ok", "fine", channels=["persistent"])
        self.assertEqual(out["mode"], "recovery")
        self.assertEqual(len(self.persistent_creates()), 1)
        self.assertEqual(self.persistent_creates()[0]["json"]["title"], "OK · health-check")
        self.assertEqual(self.dismisses(), [])
        self.assertTrue(out["last_notified"]["persistent_active"])

    def test_error_twice_identical_sends_twice(self):
        for i in range(2):
            rc, out, _ = self.notify("error", "timed out after 600 s")
            self.assertEqual(rc, 0)
            self.assertEqual(out["mode"], "full", f"run {i}")
            self.assertEqual(len(self.mobile_posts()), 2, f"run {i}")
            self.assertEqual(len(self.persistent_creates()), 1)
            self.assertEqual(self.mobile_posts()[0]["json"]["title"], "Job error · health-check")

    def test_ok_after_error_dismiss_only(self):
        self.notify("error", "boom")
        rc, out, _ = self.notify("ok", "fine")
        self.assertEqual([r["path"] for r in self.posts()], [SERVICES_PREFIX + "persistent_notification/dismiss"])
        self.assertEqual(out["notify_status"], "state_only;dismissed:persistent")

    def test_infra_status_never_dismisses_an_earlier_finding(self):
        self.notify("warning", "2 integrations failing")
        self.assertTrue(self.last["persistent_active"])
        # warning -> skipped (state_only): the warning's sidebar note stays; no dismiss call
        rc, out, _ = self.notify("skipped", "concurrency_limit", extra_result={"reason": "concurrency_limit"})
        self.assertEqual(rc, 0)
        self.assertEqual(out["mode"], "none")
        self.assertEqual(self.dismisses(), [])
        self.assertEqual(self.posts(), [])
        self.assertTrue(out["last_notified"]["persistent_active"])
        self.assertEqual(out["notify_status"], "state_only")
        # aborted / error with a state_only override do not dismiss either
        for status in ("aborted", "error"):
            rc, out, _ = self.notify(status, "x", channels=[])
            self.assertEqual(self.dismisses(), [], status)
            self.assertTrue(out["last_notified"]["persistent_active"], status)
        # ... -> ok (a real new finding): now the note is dismissed
        rc, out, _ = self.notify("ok", "all good")
        self.assertEqual(len(self.dismisses()), 1)
        self.assertFalse(out["last_notified"]["persistent_active"])
        self.assertEqual(out["notify_status"], "state_only;dismissed:persistent")

    def test_warning_then_info_state_only_dismisses(self):
        self.notify("warning", "x")
        rc, out, _ = self.notify("info", "y", channels=[])
        self.assertEqual(len(self.dismisses()), 1)
        self.assertFalse(out["last_notified"]["persistent_active"])

    def test_recovery_sends_configured_ok_push_without_notify_recovery(self):   # review M2 = design §4.8
        self.notify("warning", "x")
        rc, out, _ = self.notify("ok", "fine again", channels=["mobile"])
        self.assertEqual(out["mode"], "recovery")
        self.assertEqual(len(self.mobile_posts()), 2)
        self.assertEqual(self.mobile_posts()[0]["json"]["message"], "Resolved: fine again")
        self.assertEqual(len(self.dismisses()), 1)
        self.assertEqual(out["notify_status"], "sent:mobile(2);dismissed:persistent")
        # the next identical ok is quiet
        rc, out, _ = self.notify("ok", "fine again", channels=["mobile"])
        self.assertEqual(out["mode"], "quiet")
        self.assertEqual(self.mobile_posts(), [])

    def test_silent_skip_keeps_last_notified_so_same_warning_stays_quiet(self):   # review M3
        self.notify("warning", "2 integrations failing")
        remembered = dict(self.last)
        rc, out, _ = self.notify("skipped", "concurrency_limit", extra_result={"reason": "concurrency_limit"})
        self.assertEqual(out["last_notified"], remembered)
        rc, out, _ = self.notify("warning", "2 integrations failing")
        self.assertEqual(out["mode"], "quiet")
        self.assertEqual(self.mobile_posts(), [])
        self.assertEqual(out["last_notified"]["consecutive_same"], 2)

    def test_silent_skip_between_problem_and_ok_still_counts_as_recovery(self):     # review M3
        self.s.write_job(JOB, dict(HEALTH_CHECK_FM, notify_recovery=True))
        self.notify("critical", "disk 97% full")
        self.notify("skipped", "concurrency_limit", extra_result={"reason": "concurrency_limit"})
        rc, out, _ = self.notify("ok", "disk back to 41%")
        self.assertEqual(out["mode"], "recovery")
        self.assertEqual(self.mobile_posts()[0]["json"]["message"], "Resolved: disk back to 41%")
        self.assertEqual(len(self.dismisses()), 1)

    def test_first_ever_silent_skip_reports_null_last_notified(self):
        rc, out, _ = self.notify("skipped", "disabled", extra_result={"reason": "disabled"})
        self.assertEqual(rc, 0)
        self.assertIsNone(out["last_notified"])

    def test_empty_headline_uses_status_word(self):                                    # review L2
        rc, out, _ = self.notify("warning", "")
        self.assertTrue(self.persistent_creates()[0]["json"]["message"].startswith("**Warning**\n\n"))
        self.assertEqual(self.mobile_posts()[0]["json"]["message"], "Warning")

    def test_skipped_default_map_makes_no_service_calls(self):
        rc, out, _ = self.notify("skipped", "concurrency_limit", extra_result={"reason": "concurrency_limit"})
        self.assertEqual(rc, 0)
        self.assertEqual(out["mode"], "none")
        self.assertEqual(out["notify_status"], "state_only")
        self.assertEqual(self.sup.requests, [])          # not even discovery: no push channel involved

    def test_first_ok_with_state_only_is_silent(self):
        rc, out, _ = self.notify("ok", "fine")
        self.assertEqual(out["mode"], "none")
        self.assertEqual(self.posts(), [])
        self.assertFalse(out["last_notified"]["persistent_active"])

    def test_channels_absent_in_state_are_re_resolved_from_job(self):
        rc, out, _ = self.notify("critical", "x", omit_channels=True)
        self.assertEqual(out["notify_status"], "sent:persistent,mobile_critical(2)")

    def test_job_file_gone_uses_defaults(self):
        os.unlink(self.s.jobs / f"{JOB}.md")
        rc, out, err = self.notify("warning", "x")
        self.assertEqual(rc, 0)
        self.assertEqual(out["mode"], "full")
        self.assertIn("no longer loads", err)


class TestDiscoveryAndFallbacks(NotifyTestCase):
    def test_no_mobile_targets_falls_back_to_notify_default(self):
        self.sup.route("GET", "/core/api/services", (200, services_with({"notify": {}})))
        rc, out, _ = self.notify("warning", "x")
        self.assertEqual(out["fallbacks"], ["mobile->notify_default"])
        self.assertEqual(out["notify_status"], "fallback:mobile->notify_default;sent:persistent,notify_default")
        nd = self.posts("notify/notify")
        self.assertEqual(len(nd), 1)
        self.assertEqual(nd[0]["json"], {"title": "Warning · health-check", "message": "x"})
        self.assertEqual(self.mobile_posts(), [])

    def test_no_mobile_and_no_notify_default_falls_back_to_persistent(self):
        self.sup.route("GET", "/core/api/services", (200, services_with({})))
        rc, out, _ = self.notify("warning", "x")
        self.assertEqual(out["fallbacks"], ["mobile->notify_default", "notify_default->persistent"])
        self.assertIn("->persistent", out["notify_status"])
        self.assertEqual([r["path"] for r in self.posts()], [SERVICES_PREFIX + "persistent_notification/create"])
        self.assertTrue(out["notify_status"].endswith("sent:persistent"))

    def test_mobile_critical_degrades_with_critical_title_prefix(self):
        self.sup.route("GET", "/core/api/services", (200, services_with({"notify": {}})))
        rc, out, _ = self.notify("error", "x", channels=["mobile_critical"])
        self.assertEqual(out["fallbacks"], ["mobile_critical->notify_default"])
        nd = self.posts("notify/notify")
        self.assertEqual(nd[0]["json"]["title"], "CRITICAL · Job error · health-check")
        # and one more hop down when notify.notify is missing too
        self.sup.route("GET", "/core/api/services", (200, services_with({})))
        rc, out, _ = self.notify("critical", "y", channels=["mobile_critical"])
        pc = self.persistent_creates()
        self.assertEqual(len(pc), 1)
        self.assertTrue(pc[0]["json"]["title"].startswith("CRITICAL · "))
        self.assertEqual(out["fallbacks"], ["mobile_critical->notify_default", "notify_default->persistent"])

    def test_services_as_list_is_handled(self):
        self.sup.route("GET", "/core/api/services",
                       (200, services_with([{"service": "mobile_app_listed"}, {"service": "notify"}])))
        rc, out, _ = self.notify("warning", "x")
        self.assertEqual([r["path"].split("/")[-1] for r in self.mobile_posts()], ["mobile_app_listed"])

    def test_discovery_failure_is_an_error_entry_and_falls_back(self):
        self.sup.route("GET", "/core/api/services", (500, {"message": "down"}))
        rc, out, _ = self.notify("warning", "x")
        self.assertEqual(rc, 0)
        self.assertEqual(out["errors"][0], {"channel": "discovery", "error": "HTTP 500"})
        self.assertEqual([r["path"] for r in self.posts()], [SERVICES_PREFIX + "persistent_notification/create"])
        self.assertIn("error:discovery:HTTP 500", out["notify_status"])

    def test_discovery_200_with_non_list_body(self):
        self.sup.route("GET", "/core/api/services", (200, {"not": "a list"}))
        rc, out, _ = self.notify("warning", "x")
        self.assertEqual(out["errors"][0], {"channel": "discovery", "error": "unexpected body"})

    def test_discovery_skipped_when_only_persistent_and_webhook(self):
        rc, out, _ = self.notify("info", "x")               # info -> [persistent]
        self.assertEqual([r["path"] for r in self.sup.requests], [SERVICES_PREFIX + "persistent_notification/create"])

    def test_configured_target_subset_and_missing_target(self):
        self.s.write_notify_config({"mobile": {"targets": ["mobile_app_test_tablet", "mobile_app_gone"]}})
        rc, out, _ = self.notify("warning", "x")
        self.assertEqual([r["path"].split("/")[-1] for r in self.mobile_posts()], ["mobile_app_test_tablet"])
        self.assertIn({"channel": "mobile", "error": "target mobile_app_gone not found"}, out["errors"])
        self.assertIn("sent:persistent,mobile(1)", out["notify_status"])
        self.assertIn("error:mobile:target mobile_app_gone not found", out["notify_status"])
        self.assertEqual(rc, 0)

    def test_android_channels_and_dashboard_path_from_config(self):
        self.s.write_notify_config({"android": {"channel": "Haus", "critical_channel": "Haus Alarm"},
                                    "dashboard_path": "/lovelace/claude"})
        self.notify("warning", "x")
        data = self.mobile_posts()[0]["json"]["data"]
        self.assertEqual(data["channel"], "Haus")
        self.assertEqual(data["url"], "/lovelace/claude")
        self.assertEqual(data["clickAction"], "/lovelace/claude")
        self.notify("critical", "y")
        data = self.mobile_posts()[0]["json"]["data"]
        self.assertEqual(data["channel"], "Haus Alarm")
        self.assertEqual(set(data), CRITICAL_DATA_KEYS | {"url", "clickAction"})

    def test_unknown_notify_yaml_keys_are_logged_not_fatal(self):
        self.s.write_notify_config({"bogus": 1, "tts": {}})
        rc, out, err = self.notify("warning", "x")
        self.assertEqual(rc, 0)
        self.assertIn("_notify.yaml: bogus: unknown key", err)
        self.assertEqual(len(self.mobile_posts()), 2)


class TestWebhook(NotifyTestCase):
    def setUp(self):
        super().setUp()
        self.hook_calls = []
        self.s.write_notify_config({"webhook": {"url": self.sup.url + "/hook", "headers": {"X-Test": "yes"},
                                                "timeout_s": 5}})

    def route_hook(self, statuses):
        remaining = list(statuses)

        def hook(req):
            self.hook_calls.append(req)
            return (remaining.pop(0) if remaining else 200, {"ok": True})
        self.sup.route("POST", "/hook", hook)

    def test_webhook_retries_then_succeeds(self):
        self.route_hook([500, 500, 200])
        rc, out, _ = self.notify("warning", "x", channels=["webhook", "persistent"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.hook_calls), 3)
        wh = [s for s in out["sent"] if s["channel"] == "webhook"][0]
        self.assertEqual(wh, {"channel": "webhook", "ok": True, "attempts": 3})
        self.assertEqual(out["errors"], [])
        body = self.hook_calls[-1]["json"]
        self.assertEqual(set(body), {"job", "slug", "status", "headline", "detail", "run_id", "ended_at",
                                     "cost_usd", "metrics", "entity_id", "prev_status"})
        self.assertEqual(body["entity_id"], "sensor.claude_job_health_check")
        self.assertEqual(body["status"], "warning")
        self.assertEqual(self.hook_calls[-1]["headers"]["x-test"], "yes")
        self.assertEqual(self.hook_calls[-1]["headers"]["content-type"], "application/json")
        self.assertNotIn("authorization", self.hook_calls[-1]["headers"])     # Supervisor token never leaves
        self.assertEqual(out["notify_status"], "sent:persistent,webhook")

    def test_webhook_gives_up_after_three_attempts_and_job_status_untouched(self):
        self.route_hook([500, 500, 500, 500])
        state_path = self.s.state_path(JOB)
        rc, out, _ = self.notify("warning", "x", channels=["webhook", "persistent"])
        before = state_path.read_text()
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.hook_calls), 3)
        self.assertIn({"channel": "webhook", "error": "HTTP 500"}, out["errors"])
        self.assertIn("error:webhook:HTTP 500", out["notify_status"])
        self.assertIn("sent:persistent", out["notify_status"])
        self.assertEqual(json.loads(before)["result"]["status"], "warning")
        self.assertEqual(state_path.read_text(), before)                        # notifier writes nothing

    def test_webhook_4xx_is_not_retried(self):
        self.route_hook([404])
        rc, out, _ = self.notify("warning", "x", channels=["webhook"])
        self.assertEqual(len(self.hook_calls), 1)
        self.assertEqual(out["errors"], [{"channel": "webhook", "error": "HTTP 404"}])

    def test_webhook_posts_even_in_quiet_mode(self):
        self.route_hook([])
        self.notify("warning", "x", channels=["webhook", "mobile", "persistent"])
        rc, out, _ = self.notify("warning", "x", channels=["webhook", "mobile", "persistent"])
        self.assertEqual(out["mode"], "quiet")
        self.assertEqual(len(self.hook_calls), 2)
        self.assertEqual(out["notify_status"], "sent:webhook;quiet:mobile,persistent")

    def test_deadline_caps_a_slow_webhook_and_dismiss_runs_first(self):           # review H1
        self.s.write_notify_config({"webhook": {"url": self.sup.url + "/hook", "timeout_s": 30}})
        self.notify("warning", "x")                          # leaves a sidebar note (persistent_active)
        self.sup.route("POST", "/hook", FakeSupervisor.slow(8, then=(200, {})))
        self.s.setenv(CLAUDE_JOB_NOTIFY_DEADLINE_S="3")
        t0 = time.monotonic()
        rc, out, _ = self.notify("ok", "fine", channels=["webhook"])
        elapsed = time.monotonic() - t0
        self.assertEqual(rc, 0)
        self.assertLess(elapsed, 6.5, out)
        paths = [r["path"] for r in self.sup.find("POST")]
        self.assertEqual(paths, [SERVICES_PREFIX + "persistent_notification/dismiss", "/hook"])
        self.assertIn("dismissed:persistent", out["notify_status"])
        self.assertIn("error:webhook:deadline", out["notify_status"])
        self.assertEqual([s for s in out["sent"] if s["channel"] == "webhook"], [{"channel": "webhook", "ok": False, "attempts": 1}])
        self.assertFalse(out["last_notified"]["persistent_active"])

    def test_malformed_retry_env_falls_back_to_default(self):                     # review L3
        self.route_hook([200])
        self.s.setenv(CLAUDE_JOB_WEBHOOK_RETRY_S="soon,later")
        rc, out, err = self.notify("warning", "x", channels=["webhook"])
        self.assertEqual(rc, 0)
        self.assertIn("ignoring malformed CLAUDE_JOB_WEBHOOK_RETRY_S", err)
        self.assertEqual(out["notify_status"], "sent:webhook")

    def test_webhook_channel_without_config_is_an_error_entry(self):
        os.unlink(self.s.jobs / "_notify.yaml")
        rc, out, _ = self.notify("warning", "x", channels=["webhook", "persistent"])
        self.assertEqual(rc, 0)
        self.assertEqual(out["errors"], [{"channel": "webhook", "error": "not configured in _notify.yaml"}])


class TestPayloadsAndCli(NotifyTestCase):
    def test_persistent_id_and_markdown_body(self):
        rc, out, _ = self.notify("warning", "2 integrations failing", detail="zha and hue are down")
        body = self.persistent_creates()[0]["json"]
        self.assertEqual(body["notification_id"], "claude_job_health_check")
        self.assertEqual(body["title"], "Warning · health-check")
        self.assertTrue(body["message"].startswith("**2 integrations failing**\n\nzha and hue are down\n\n_run run-"))
        self.assertIn("run-20260818T050001Z-3f9a", body["message"])
        self.assertIn("$0.63_", body["message"])
        self.assertEqual(self.persistent_creates()[0]["headers"]["authorization"], "Bearer test-supervisor-token")

    def test_title_and_message_truncation(self):
        long_job = "x" * 150
        # a name this long never loads as a job -> renag/recovery defaults; the notifier still notifies
        rc, out, _ = self.notify("warning", "h" * 1500, job=long_job, channels=["mobile", "persistent"])
        self.assertEqual(rc, 0)
        m = self.mobile_posts()[0]["json"]
        self.assertEqual(len(m["title"]), 100)
        self.assertTrue(m["title"].endswith("…"))
        self.assertEqual(len(m["message"]), 1000)
        self.assertTrue(m["message"].endswith("…"))
        self.assertLessEqual(len(self.persistent_creates()[0]["json"]["title"]), 100)

    def test_stdout_is_exactly_one_json_line(self):
        state = self.state_for("warning", "x", "d", ["mobile", "persistent"])
        path = self.s.write_state(JOB, state)
        rc, out, err = run_cli([sys.executable, NOTIFIER, JOB, "--state-file", path], self.s.env)
        self.assertEqual(rc, 0)
        self.assertEqual(out.count("\n"), 1)
        self.assertTrue(out.endswith("\n"))
        parsed = json.loads(out)
        self.assertEqual(set(parsed), {"notify_status", "mode", "sent", "fallbacks", "errors", "last_notified"})
        self.assertEqual(set(parsed["last_notified"]),
                         {"status", "headline_sha256", "at", "consecutive_same", "persistent_active", "critical_hint_shown"})

    def test_dry_run_makes_no_posts(self):
        rc, out, _ = self.notify("critical", "x", dry_run=True)
        self.assertEqual(rc, 0)
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["sent"], [])
        self.assertEqual(self.sup.find("POST"), [])
        self.assertEqual([p["channel"] for p in out["planned"]], ["persistent", "mobile_critical"])
        self.assertEqual(out["notify_status"], "sent:persistent,mobile_critical(2)")
        self.assertEqual(out["mode"], "full")

    def test_unreadable_state_file_exits_2_with_empty_stdout(self):
        rc, out, err = run_cli([sys.executable, NOTIFIER, JOB, "--state-file", self.s.state / "nope.json"], self.s.env)
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("[claude-job-notify]", err)
        bad = self.s.state / "bad.json"
        bad.write_text("{not json")
        rc, out, err = run_cli([sys.executable, NOTIFIER, JOB, "--state-file", bad], self.s.env)
        self.assertEqual((rc, out), (2, ""))
        noresult = self.s.write_state(JOB, {"schema": 1, "job": JOB, "status": "running", "result": None})
        rc, out, err = run_cli([sys.executable, NOTIFIER, JOB, "--state-file", noresult], self.s.env)
        self.assertEqual((rc, out), (2, ""))
        self.assertEqual(self.sup.requests, [])

    def test_usage_error_exits_2(self):
        rc, out, err = run_cli([sys.executable, NOTIFIER, JOB], self.s.env)
        self.assertEqual((rc, out), (2, ""))

    def test_android_hint_printed_once(self):
        rc, out, err = self.notify("critical", "x")
        self.assertIn("[claude-job-notify] Android: grant 'Override Do Not Disturb' to the 'Claude Jobs Critical' channel", err)
        self.assertTrue(out["last_notified"]["critical_hint_shown"])
        rc, out, err = self.notify("critical", "y")           # headline changed -> full push again
        self.assertEqual(len(self.mobile_posts()), 2)
        self.assertNotIn("Override Do Not Disturb", err)
        self.assertTrue(out["last_notified"]["critical_hint_shown"])
        rc, out, err = self.notify("ok", "z")
        self.assertTrue(out["last_notified"]["critical_hint_shown"])   # sticky

    def test_odd_state_values_do_not_crash(self):                                  # review M1 guards
        state = self.state_for("warning", "x", "d", ["mobile", "persistent"])
        state["notify"]["last_notified"] = "yesterday"                                # not a mapping -> never notified
        state["result"]["cost_usd"] = "n/a"
        state["result"]["headline"] = 12345
        path = self.s.write_state(JOB, state)
        rc, out, err = run_cli([sys.executable, NOTIFIER, JOB, "--state-file", path], self.s.env)
        self.assertEqual(rc, 0, err)
        out = json.loads(out)
        self.assertEqual(out["mode"], "full")
        self.assertEqual(out["last_notified"]["consecutive_same"], 1)
        self.assertIn("$0.00_", self.persistent_creates()[0]["json"]["message"])
        self.assertEqual(self.mobile_posts()[0]["json"]["message"], "12345")
        # consecutive_same garbage in an otherwise matching record
        state["notify"]["last_notified"] = dict(out["last_notified"], consecutive_same="many")
        path = self.s.write_state(JOB, state)
        rc, out, err = run_cli([sys.executable, NOTIFIER, JOB, "--state-file", path], self.s.env)
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["last_notified"]["consecutive_same"], 2)

    def test_internal_error_prints_one_line_without_last_notified_and_exits_0(self):   # review M1
        # No state-file content reaches the except branch any more (guards above), so drive it in-process
        # with plan() forced to raise; discovery is not needed for info -> [persistent].
        loader = importlib.machinery.SourceFileLoader("claude_job_notify", str(NOTIFIER))
        spec = importlib.util.spec_from_loader("claude_job_notify", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        path = self.s.write_state(JOB, self.state_for("info", "x", "d", ["persistent"]))
        restore = self.s.apply_to_process()
        try:
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(mod, "plan", side_effect=RuntimeError("boom")), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = mod.main([JOB, "--state-file", str(path)])
        finally:
            restore()
        self.assertEqual(rc, 0)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        parsed = json.loads(lines[0])
        self.assertEqual(parsed, {"notify_status": "error:internal:RuntimeError", "mode": "none", "sent": [],
                                  "fallbacks": [], "errors": [{"channel": "internal", "error": "boom"}]})
        self.assertNotIn("last_notified", parsed)
        self.assertIn("[claude-job-notify] internal error", err.getvalue())
        self.assertEqual(self.sup.find("POST"), [])

    def test_persistent_create_failure_is_reported_and_exit_0(self):
        self.sup.route("POST", SERVICES_PREFIX + "persistent_notification/create", (400, {"message": "bad"}))
        rc, out, _ = self.notify("info", "x")
        self.assertEqual(rc, 0)
        self.assertEqual(out["sent"], [{"channel": "persistent", "ok": False}])
        self.assertEqual(out["errors"], [{"channel": "persistent", "error": "HTTP 400"}])
        self.assertEqual(out["notify_status"], "error:persistent:HTTP 400")
        self.assertFalse(out["last_notified"]["persistent_active"])


if __name__ == "__main__":
    unittest.main()
