"""Claude subscription usage for Home Assistant (add-on option `enable_usage_sensors`).

The endpoint's tick polls the undocumented endpoint the CLI's own `/usage` screen reads,
`GET https://api.anthropic.com/api/oauth/usage` (bearer = the interactive session's OAuth
access token), and `GET /usage` on the job endpoint serves the last snapshot to a `rest:`
sensor block in the generated package. The subscription is account-wide, so what HA sees is
whole-account usage across every surface (Claude Code, chats, Cowork, ...), not this add-on's.

Rules this module lives by (all of them are load-bearing, see the README section):

* **Read the token, never refresh it.** `jc.oauth_credentials()` is a read-only, newest-wins
  view of the credential store; a second refresher would race the CLI and rotate the refresh
  token from under it. The access token lives ~8 h and is renewed only when the CLI runs, so
  a 401 is expected during quiet stretches: keep the last good values, flag them `stale`,
  carry `last_success`, and never publish zeros in their place.
* **Poll gently.** One request per `poll_interval_s` (default 300 s, add-on option
  `usage_poll_interval`), single flight, a short timeout, always on a helper thread so the
  tick and the HTTP handlers never wait on api.anthropic.com. Failures are logged on
  transition only (one line when an error kind starts, one when it clears), never per tick.
* **Only derived numbers leave this process.** The token never appears in the snapshot, the
  log, or the generated YAML; a network error's text is reduced to its class name.
* **The upstream shape is unstable.** Unknown keys are ignored, missing ones tolerated; a body
  with no recognizable usage window at all counts as malformed (kept-last-good + stale).

Python stdlib only. Env test seams: CLAUDE_JOB_USAGE_URL, _USAGE_POLL_INTERVAL_S, _USAGE_TIMEOUT_S.
"""
import http.client
import json
import math
import re
import threading
import time
import urllib.error
import urllib.request

import jobcommon as jc

COMPONENT = "claude-job-endpoint"
ERROR_KINDS = ("no_credentials", "unauthorized", "rate_limited", "unreachable", "http_error", "malformed_body")
MAX_BODY_BYTES = 256 * 1024                  # a usage document is a few KiB; anything bigger is not one
_SLUG_RE = re.compile(r"[^a-z0-9]+")
OPTION_ENABLED = "enable_usage_sensors"
OPTION_INTERVAL = "usage_poll_interval"
INTERVAL_MIN_S, INTERVAL_MAX_S = 60, 3600    # mirrors config.yaml `int(60,3600)`


def log(msg: str) -> None:
    jc.log(COMPONENT, "usage: " + msg)


# ---- add-on options ----------------------------------------------------------------------------
def option_enabled(options: dict | None = None) -> bool:
    opts = jc.addon_options() if options is None else options
    v = opts.get(OPTION_ENABLED, True)
    return bool(v) if isinstance(v, bool) else str(v).strip().lower() not in ("false", "0", "no", "off")


def option_interval(options: dict | None = None) -> int:
    """Poll cadence in seconds: the add-on option clamped to its schema, else the image constant."""
    opts = jc.addon_options() if options is None else options
    v = opts.get(OPTION_INTERVAL)
    try:
        n = int(v) if v is not None and not isinstance(v, bool) else int(jc.USAGE_POLL_INTERVAL_S)
    except (TypeError, ValueError):
        n = int(jc.USAGE_POLL_INTERVAL_S)
    return max(INTERVAL_MIN_S, min(INTERVAL_MAX_S, n))


# ---- parsing (pure; unit-tested in-process) -------------------------------------------------------
def _num(v):
    """Finite number -> float rounded to 1 dp; bools/strings/NaN -> None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return round(float(v), 1)


def _money(v):
    """Finite number -> float rounded to 2 dp (money), else None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return round(float(v), 2)


def _ts(v):
    """ISO-8601 (any offset/fraction) -> `YYYY-MM-DDTHH:MM:SS+00:00` (what HA's timestamp
    device class parses without ambiguity), or None."""
    dt = jc.parse_iso(v)
    return dt.replace(microsecond=0).isoformat() if dt is not None else None


def _str(v):
    return v if isinstance(v, str) and v else None


def _bool(v):
    return v if isinstance(v, bool) else None


def slug(text) -> str | None:
    """`"Claude Code"` -> `claude_code`; used to key the weekly breakdown by surface."""
    if not isinstance(text, str):
        return None
    s = _SLUG_RE.sub("_", text.strip().lower()).strip("_")
    return s or None


def _window(block, limit) -> dict:
    """One rate-limit window from its top-level block (`five_hour`/`seven_day`: utilization is a
    percent float) with the matching `limits[]` entry (percent int, severity, is_active) filling
    whatever the block lacks."""
    out = {"used_percent": None, "resets_at": None, "severity": None, "is_active": None}
    if isinstance(block, dict):
        out["used_percent"] = _num(block.get("utilization"))
        out["resets_at"] = _ts(block.get("resets_at"))
    if isinstance(limit, dict):
        if out["used_percent"] is None:
            out["used_percent"] = _num(limit.get("percent"))
        if out["resets_at"] is None:
            out["resets_at"] = _ts(limit.get("resets_at"))
        out["severity"] = _str(limit.get("severity"))
        out["is_active"] = _bool(limit.get("is_active"))
    return out


def _scoped(limit: dict) -> dict:
    scope = limit.get("scope") if isinstance(limit.get("scope"), dict) else {}
    model = scope.get("model") if isinstance(scope.get("model"), dict) else {}
    out = _window(None, limit)
    out["model"] = _str(model.get("display_name")) or _str(model.get("id")) or _str(model.get("name"))
    return out


def _breakdown(obj) -> dict:
    """`seven_day_breakdown` -> `{by_surface: {claude_code: 30.1, ...}, rows: [...], as_of, window_started_at}`."""
    out = {"by_surface": {}, "rows": [], "as_of": None, "window_started_at": None}
    if not isinstance(obj, dict):
        return out
    out["as_of"] = _ts(obj.get("as_of"))
    out["window_started_at"] = _ts(obj.get("window_started_at"))
    rows = obj.get("rows")
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        pct = _num(row.get("percent"))
        display = _str(row.get("display_name"))
        key = _str(row.get("key"))
        if pct is None or not (display or key):
            continue
        out["rows"].append({"key": key, "display_name": display or key, "percent": pct})
        s = slug(display) or slug(key)
        if s and s not in out["by_surface"]:
            out["by_surface"][s] = pct
    return out


def _extra_usage(obj) -> dict:
    src = obj if isinstance(obj, dict) else {}
    return {
        "is_enabled": _bool(src.get("is_enabled")),
        "monthly_limit": _money(src.get("monthly_limit")),
        "used_credits": _money(src.get("used_credits")),
        "utilization": _num(src.get("utilization")),
        "currency": _str(src.get("currency")),
        "disabled_reason": _str(src.get("disabled_reason")),
    }


def parse_usage(obj) -> dict | None:
    """Normalize one usage document. Returns None when it carries no usage window at all
    (malformed / not the document we expect); every other missing piece is just null."""
    if not isinstance(obj, dict):
        return None
    limits = {"session": None, "weekly_all": None, "weekly_scoped": []}
    raw_limits = obj.get("limits")
    for lim in raw_limits if isinstance(raw_limits, list) else []:
        if not isinstance(lim, dict):
            continue
        kind = lim.get("kind")
        if kind == "weekly_scoped":
            limits["weekly_scoped"].append(lim)
        elif kind in ("session", "weekly_all") and limits[kind] is None:
            limits[kind] = lim
    session = _window(obj.get("five_hour"), limits["session"])
    weekly = _window(obj.get("seven_day"), limits["weekly_all"])
    scoped_all = [_scoped(lim) for lim in limits["weekly_scoped"]]
    scoped_all = [s for s in scoped_all if s["used_percent"] is not None or s["model"]]
    if session["used_percent"] is None and weekly["used_percent"] is None and not scoped_all:
        return None
    # the binding per-model cap: highest percent (it can exceed weekly_all); attributes carry them all
    binding = max(scoped_all, key=lambda s: -1.0 if s["used_percent"] is None else s["used_percent"],
                  default=None)
    scoped = dict(binding) if binding else {"used_percent": None, "resets_at": None, "severity": None,
                                            "is_active": None, "model": None}
    bd = _breakdown(obj.get("seven_day_breakdown"))
    weekly.update({"breakdown": bd["by_surface"], "breakdown_rows": bd["rows"], "breakdown_as_of": bd["as_of"],
                   "breakdown_window_started_at": bd["window_started_at"]})
    return {
        "session": session,
        "weekly": weekly,
        "weekly_scoped": scoped,
        "weekly_scoped_all": scoped_all,
        "extra_usage": _extra_usage(obj.get("extra_usage")),
    }


# What GET /usage carries before the first successful poll (every window null: never zeros).
EMPTY = {
    "session": {"used_percent": None, "resets_at": None, "severity": None, "is_active": None},
    "weekly": {"used_percent": None, "resets_at": None, "severity": None, "is_active": None, "breakdown": {},
               "breakdown_rows": [], "breakdown_as_of": None, "breakdown_window_started_at": None},
    "weekly_scoped": {"used_percent": None, "resets_at": None, "severity": None, "is_active": None, "model": None},
    "weekly_scoped_all": [],
    "extra_usage": _extra_usage(None),
}


# ---- the HTTP call ---------------------------------------------------------------------------------
def fetch(token: str, timeout: float | None = None):
    """One GET. `(status, body_bytes)`; status None on transport trouble with the error's class
    name in the body (never the exception text: it could echo the URL or headers). Environment
    proxies are honored, as the CLI honors them."""
    hdrs = {
        "Authorization": "Bearer " + token,
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-code",
        "Accept": "application/json",
    }
    req = urllib.request.Request(jc.USAGE_URL, method="GET", headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=jc.USAGE_TIMEOUT_S if timeout is None else timeout) as resp:
            return resp.status, resp.read(MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as e:
        try:
            payload = e.read(MAX_BODY_BYTES + 1)
        except Exception:  # noqa: BLE001 - body is diagnostic only
            payload = b""
        return e.code, payload
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException, UnicodeError) as e:
        reason = getattr(e, "reason", None)
        inner = type(reason).__name__ if isinstance(reason, BaseException) else type(e).__name__
        return None, inner.encode()


# ---- the poller ------------------------------------------------------------------------------------
class UsagePoller:
    """Owns the snapshot. `maybe_poll()` is what the tick (and, lazily, GET /usage) calls: when a
    poll is due and none is in flight it starts one on a daemon thread and returns it, else None.
    `snapshot()` is what GET /usage serves. Thread-safe; nothing here blocks the caller."""

    def __init__(self, *, enabled: bool | None = None, interval_s: int | None = None, options: dict | None = None):
        opts = jc.addon_options() if options is None else options
        self.enabled = option_enabled(opts) if enabled is None else bool(enabled)
        self.interval_s = option_interval(opts) if interval_s is None else int(interval_s)
        self.lock = threading.Lock()                # guards every field below
        self.inflight = threading.Lock()            # single flight
        self.data = None                            # last GOOD parsed document
        self.last_success = None                    # ISO
        self.last_success_mono = None
        self.last_attempt = None                    # ISO
        self.last_attempt_mono = None
        self.last_error = None                      # one of ERROR_KINDS, or None after a success
        self.last_error_detail = None
        self.last_http_status = None
        self.logged_error = None                    # the error kind currently reported in the log
        self.polls = 0
        self.thread = None

    # -- scheduling --
    def due(self) -> bool:
        with self.lock:
            if not self.enabled:
                return False
            if self.last_attempt_mono is None:
                return True
            return time.monotonic() - self.last_attempt_mono >= self.interval_s

    def maybe_poll(self):
        """Start a background poll if one is due and none is running. Returns the Thread or None."""
        if not self.due():
            return None
        if not self.inflight.acquire(blocking=False):
            return None
        with self.lock:                               # claim the slot before the thread exists so a
            self.last_attempt_mono = time.monotonic()  # burst of callers cannot each start one
            self.last_attempt = jc.now_iso()
        t = threading.Thread(target=self._run_locked, name="usage-poll", daemon=True)
        self.thread = t
        t.start()
        return t

    def _run_locked(self):
        try:
            self.poll_once()
        except Exception as exc:  # noqa: BLE001 - a bug here must not take the thread down noisily every tick
            self._record_failure("unreachable", f"internal: {type(exc).__name__}", None)
        finally:
            self.inflight.release()

    # -- one poll, synchronous (tests and --tick-once call this directly) --
    def poll_once(self) -> bool:
        with self.lock:
            self.last_attempt = jc.now_iso()
            self.last_attempt_mono = time.monotonic()
        creds = None
        try:
            creds = jc.oauth_credentials()
        except Exception as exc:  # noqa: BLE001
            self._record_failure("no_credentials", f"credentials unreadable: {type(exc).__name__}", None)
            return False
        token = (creds or {}).get("accessToken")
        if not isinstance(token, str) or not token.strip():
            self._record_failure("no_credentials", "no claude.ai OAuth access token in the credential store "
                                 "(log in from the terminal)", None)
            return False
        status, body = fetch(token.strip())
        if status is None:
            self._record_failure("unreachable", body.decode("ascii", "replace")[:80], None)
            return False
        if status in (401, 403):
            self._record_failure("unauthorized", f"HTTP {status}: access token rejected (expired? it is renewed "
                                 "whenever the CLI runs in the terminal or a job)", status)
            return False
        if status == 429:
            self._record_failure("rate_limited", "HTTP 429", status)
            return False
        if not 200 <= status < 300:
            self._record_failure("http_error", f"HTTP {status}", status)
            return False
        if len(body) > MAX_BODY_BYTES:
            self._record_failure("malformed_body", f"body over {MAX_BODY_BYTES} bytes", status)
            return False
        try:
            obj = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._record_failure("malformed_body", "not JSON", status)
            return False
        parsed = parse_usage(obj)
        if parsed is None:
            self._record_failure("malformed_body", "no usage window in the response (shape changed?)", status)
            return False
        self._record_success(parsed, status)
        return True

    # -- state transitions (log on change only) --
    def _record_success(self, parsed: dict, status: int) -> None:
        with self.lock:
            self.data = parsed
            self.last_success = jc.now_iso()
            self.last_success_mono = time.monotonic()
            self.last_http_status = status
            self.last_error = None
            self.last_error_detail = None
            self.polls += 1
            recovered, self.logged_error = self.logged_error, None
        if recovered:
            log(f"recovered ({recovered} cleared); sensors are current again")

    def _record_failure(self, kind: str, detail: str, status) -> None:
        with self.lock:
            self.last_error = kind
            self.last_error_detail = detail
            self.last_http_status = status
            self.polls += 1
            had_data = self.data is not None
            first = self.logged_error != kind
            self.logged_error = kind
        if first:
            log(f"{kind}: {detail}; " + ("keeping the last good values (stale)" if had_data
                                          else "no values yet (sensors stay unavailable)")
                + f"; retrying every {self.interval_s}s, logged once per error kind")

    # -- what GET /usage serves --
    def snapshot(self) -> dict:
        with self.lock:
            now_mono = time.monotonic()
            data = self.data
            age = int(now_mono - self.last_success_mono) if self.last_success_mono is not None else None
            stale = data is not None and self.last_error is not None
            out = {
                "enabled": self.enabled,
                "ok": data is not None and self.last_error is None,
                "stale": stale,
                "has_data": data is not None,
                "last_success": self.last_success,
                "last_attempt": self.last_attempt,
                "last_error": self.last_error,
                "last_error_detail": self.last_error_detail,
                "last_http_status": self.last_http_status,
                "age_s": age,
                "poll_interval_s": self.interval_s,
                "polls": self.polls,
            }
        creds = None
        try:
            creds = jc.oauth_credentials() or {}
        except Exception:  # noqa: BLE001 - metadata only
            creds = {}
        expires = _epoch_ms(creds.get("expiresAt"))
        out["credential_expires_at"] = jc.iso(expires) if expires is not None else None
        out["subscription_type"] = _str(creds.get("subscriptionType"))
        out["rate_limit_tier"] = _str(creds.get("rateLimitTier"))
        out.update(json.loads(json.dumps(data if data is not None else EMPTY)))   # deep copy, never our dict
        out["generated_at"] = jc.now_iso()
        return out


def _epoch_ms(v):
    """`expiresAt` (ms epoch) -> aware datetime, or None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
        return None
    import datetime as _dt
    try:
        return _dt.datetime.fromtimestamp(float(v) / 1000.0, tz=_dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
