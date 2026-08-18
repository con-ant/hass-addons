"""Shared plumbing for the Claude Jobs executables (runner, notifier, endpoint, renderer).

Design of record: claudecode/docs/DESIGN-claude-jobs.md — §4.1 (layout on disk), §4.9
(CLI preflight), §4.10 (entity attributes, kill switch, cost rollup), §4.11 (image
constants), Appendix A.4 (durable writes), A.6 (log retention), A.7 (transcript retention).

Everything here is stdlib only. This module never imports `jobdef` (the dependency runs
the other way). Rules every caller relies on:

* Every path and interval is a module constant computed from `CLAUDE_JOB_*` environment
  overrides at import (see `reload_paths()`); production sets none of them, tests set all.
* Every JSON file write goes through `atomic_write_json` (tmp file `<path>.tmp.<pid>.…` in
  the same directory + `os.replace`); the `.tmp.` infix is what the endpoint's cleanup sweeps.
* Timestamps are ISO-8601 UTC `YYYY-MM-DDTHH:MM:SSZ`; money is rounded to 4 dp in files and
  2 dp in entity payloads.
* HA calls fail soft: `ha_request` returns `(None, b"")` on connection trouble, never raises.

Where to go to change things:
  * add/rename an entity attribute -> `entity_payload()` (per-job), `cost_entity_payload()`,
                                      `endpoint_error_payload()`; docs/JOBS.md "Entities"
  * move a directory or file       -> the path table in `_configure()` (one place; env-overridable)
  * change a bound or interval     -> the constants table below (§4.11); promotion to an add-on
                                      option is documented there
  * change what `GET /jobs` says per job -> `job_summary()` (the endpoint and `claude-job list` share it)
  * change the state-file shape    -> the runner writes it (`claude-job` finish()); readers here are
                                      `entity_payload()`/`job_summary()`, plus the endpoint's tick
"""
import datetime as _dt
import errno
import fcntl
import itertools
import http.client
import json
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:  # tz data may be absent on a stripped image; every caller has a UTC fallback
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


# ---- environment helpers ------------------------------------------------------------------
def _env(name: str, default):
    v = os.environ.get("CLAUDE_JOB_" + name)
    return default if v is None or v == "" else v


def _env_num(name: str, default, cast=int):
    v = os.environ.get("CLAUDE_JOB_" + name)
    if v is None or v == "":
        return default
    try:
        return cast(v)
    except ValueError:
        return default


# ---- path constants (breakdown §0 table; recomputed by reload_paths()) --------------------
# Declared here so static readers see them; values are assigned in _configure().
ROOT = HA_CONFIG_DIR = PERSIST_DIR = JOBS_DIR = STATE_DIR = LOGS_DIR = INBOX_DIR = ""
DISABLED_DIR = ACTIONS_DIR = DATA_DIR = PROJECT_DIR = TOKEN_FILE = OPTIONS_FILE = ""
RUN_DIR = PGID_DIR = PERRUN_DIR = SHARE_DIR = LIB_DIR = TRANSCRIPTS_DIR = ""
SUPERVISOR_URL = CLAUDE_BIN = TIMEOUT_BIN = RUNNER_BIN = NOTIFY_BIN = BROKER_SCRIPT = ""
BUILT_CLI_VERSION_FILE = ADDON_VERSION_FILE = ""
STOPPING_FILE = COST_FILE = COST_LOCK = GLOBAL_LOCK = PREFLIGHT_CACHE = ""


def _configure() -> None:
    """(Re)compute every path/binary constant from the current environment."""
    g = globals()
    root = _env("ROOT", "")
    lib = _env("LIB_DIR", str(pathlib.Path(__file__).resolve().parent))
    g["ROOT"] = root
    g["LIB_DIR"] = lib
    g["SHARE_DIR"] = _env("SHARE_DIR", str((pathlib.Path(lib) / "../../../share/claudecode").resolve()))
    g["HA_CONFIG_DIR"] = _env("HA_CONFIG_DIR", root + "/homeassistant")
    g["PERSIST_DIR"] = _env("PERSIST_DIR", root + "/homeassistant/.claudecode")
    g["JOBS_DIR"] = _env("JOBS_DIR", g["PERSIST_DIR"] + "/jobs")
    g["STATE_DIR"] = g["JOBS_DIR"] + "/state"
    g["LOGS_DIR"] = g["JOBS_DIR"] + "/logs"
    g["INBOX_DIR"] = g["STATE_DIR"] + "/inbox"
    g["DISABLED_DIR"] = g["STATE_DIR"] + "/disabled"
    g["ACTIONS_DIR"] = g["STATE_DIR"] + "/actions"
    g["DATA_DIR"] = _env("DATA_DIR", root + "/data/claude-jobs")
    g["PROJECT_DIR"] = g["DATA_DIR"] + "/project"
    g["TOKEN_FILE"] = g["DATA_DIR"] + "/token"
    g["OPTIONS_FILE"] = _env("OPTIONS_FILE", root + "/data/options.json")
    g["RUN_DIR"] = _env("RUN_DIR", root + "/run/claudecode")
    g["PGID_DIR"] = g["RUN_DIR"] + "/jobs"
    g["PERRUN_DIR"] = g["RUN_DIR"] + "/run"
    g["STOPPING_FILE"] = g["RUN_DIR"] + "/stopping"
    g["PREFLIGHT_CACHE"] = g["RUN_DIR"] + "/cli_preflight.json"
    # A.7 / cli-probe: the CLI names the transcript dir after the cwd with every
    # non-alphanumeric character replaced by "-" (/data/claude-jobs/project -> -data-claude-jobs-project).
    g["TRANSCRIPTS_DIR"] = _env(
        "TRANSCRIPTS_DIR", g["PERSIST_DIR"] + "/projects/" + re.sub(r"[^A-Za-z0-9]", "-", g["PROJECT_DIR"]))
    g["SUPERVISOR_URL"] = _env("SUPERVISOR_URL", "http://supervisor").rstrip("/")
    g["CLAUDE_BIN"] = _env("CLAUDE_BIN", "claude")
    g["TIMEOUT_BIN"] = _env("TIMEOUT_BIN", "timeout")
    g["RUNNER_BIN"] = _env("RUNNER_BIN", "claude-job")
    g["NOTIFY_BIN"] = _env("NOTIFY_BIN", "claude-job-notify")
    g["BROKER_SCRIPT"] = lib + "/ha_broker.py"
    g["BUILT_CLI_VERSION_FILE"] = _env("BUILT_VERSION_FILE", "/etc/claude-code-version")
    g["ADDON_VERSION_FILE"] = _env("ADDON_VERSION_FILE", "/etc/claudecode-addon-version")
    g["COST_FILE"] = g["STATE_DIR"] + "/_cost.json"
    g["COST_LOCK"] = g["STATE_DIR"] + "/_cost.lock"
    g["GLOBAL_LOCK"] = g["STATE_DIR"] + "/_global.lock"
    _configure_constants()
    g["_addon_version_cache"] = None


# ---- image constants (design §4.11 / breakdown 2m) ------------------------------------------
# Production values. Tests (only) may override any of them through the environment: the variable
# is CLAUDE_JOB_<NAME> with a leading "JOB_" dropped from NAME (JOB_GLOBAL_WAIT_S ->
# CLAUDE_JOB_GLOBAL_WAIT_S, TICK_INTERVAL_S -> CLAUDE_JOB_TICK_INTERVAL_S); see R20.
JOB_MAX_CONCURRENT = 1              # §4.3 step 5
JOB_GLOBAL_WAIT_S = 60              # §4.3 step 5: wait for the global semaphore
JOB_MAX_TIMEOUT = 3600              # frontmatter `timeout` ceiling
JOB_MIN_TIMEOUT = 30
JOB_DEFAULT_TIMEOUT = 600
JOB_MAX_COST_USD = 5.00             # frontmatter `max_cost_usd` ceiling
JOB_DEFAULT_COST_USD = 1.00
JOB_DEFAULT_MAX_TURNS = 50
JOB_MAX_TURNS_CAP = 200
JOB_DEFAULT_MODEL = "fable"         # overridden by add-on option job_default_model (jobdef.default_model)
JOB_ANCHOR_SCAN_INTERVAL_S = 60     # rendered into the package's rest: block
JOB_TRANSCRIPT_KEEP_DAYS = 30       # A.7
JOB_TRANSCRIPT_MAX_BYTES = 50 * 1024 * 1024
JOB_LOG_PRUNE_BYTES = 1024 * 1024   # A.6: runner prunes logs/<name>.jsonl above this ...
JOB_LOG_KEEP_LINES = 200            # ... keeping this many lines
JOB_LOG_BACKSTOP_BYTES = 2 * 1024 * 1024   # tick duty 4 threshold
JOB_ENDPOINT_PORT = 7682            # §4.9
JOB_ENDPOINT_BIND = "0.0.0.0"
JOB_ENDPOINT_BODY_CAP = 65536
JOB_INPUT_MAX_BYTES = 4096          # §4.2 input cap (compact JSON)
JOB_STOP_GRACE_S = 5                # A.1 (bash JOB_STOP_GRACE_SECS)
JOB_TERM_TRAP_BUDGET_S = 4          # A.2 total ...
JOB_TERM_CHILD_WAIT_S = 2           # ... of which waiting for the child
JOB_TERM_PUBLISH_TIMEOUT_S = 1.5    # ... and the abort-path publish timeout
JOB_KILL_GRACE_S = 15               # `timeout -k 15`
TICK_INTERVAL_S = 60                # §4.9 the tick
STUCK_MARGIN_S = 120                # A.3: running past deadline + this -> demote/warn
WATCHDOG_KILL_S = 180               # A.3: live pgid past deadline + this -> SIGKILL
CANARY_INTERVAL_S = 600             # tick duty 3
PRUNE_INTERVAL_S = 86400            # tick duty 4 ...
PRUNE_FIRST_DELAY_S = 600           # ... first pass this long after endpoint start
TMP_MAX_AGE_S = 3600                # tick duty 5
AUTH_RETRY_BASE_S = 10              # A.8
AUTH_RETRY_JITTER_S = 5
DETAIL_ATTR_CAP = 900               # §4.10 entity `detail` attribute cap
PREFLIGHT_CACHE_TTL_S = 3600        # 2f cli_preflight.json validity
NOTIFIER_TIMEOUT_S = 45             # runner waits this long for claude-job-notify

_TUNABLES = (
    "JOB_MAX_CONCURRENT", "JOB_GLOBAL_WAIT_S", "JOB_MAX_TIMEOUT", "JOB_MIN_TIMEOUT", "JOB_DEFAULT_TIMEOUT",
    "JOB_MAX_COST_USD", "JOB_DEFAULT_COST_USD", "JOB_DEFAULT_MAX_TURNS", "JOB_MAX_TURNS_CAP",
    "JOB_DEFAULT_MODEL", "JOB_ANCHOR_SCAN_INTERVAL_S", "JOB_TRANSCRIPT_KEEP_DAYS", "JOB_TRANSCRIPT_MAX_BYTES",
    "JOB_LOG_PRUNE_BYTES", "JOB_LOG_KEEP_LINES", "JOB_LOG_BACKSTOP_BYTES", "JOB_ENDPOINT_PORT",
    "JOB_ENDPOINT_BIND", "JOB_ENDPOINT_BODY_CAP", "JOB_INPUT_MAX_BYTES", "JOB_STOP_GRACE_S",
    "JOB_TERM_TRAP_BUDGET_S", "JOB_TERM_CHILD_WAIT_S", "JOB_TERM_PUBLISH_TIMEOUT_S", "JOB_KILL_GRACE_S",
    "TICK_INTERVAL_S", "STUCK_MARGIN_S", "WATCHDOG_KILL_S", "CANARY_INTERVAL_S", "PRUNE_INTERVAL_S",
    "PRUNE_FIRST_DELAY_S", "TMP_MAX_AGE_S", "AUTH_RETRY_BASE_S", "AUTH_RETRY_JITTER_S", "DETAIL_ATTR_CAP",
    "PREFLIGHT_CACHE_TTL_S", "NOTIFIER_TIMEOUT_S",
)
_TUNABLE_DEFAULTS = {name: globals()[name] for name in _TUNABLES}


def _number(text: str):
    """int for integral strings, float otherwise (tests may set fractional seconds)."""
    return int(text) if re.fullmatch(r"-?\d+", text) else float(text)


def _configure_constants() -> None:
    g = globals()
    for name in _TUNABLES:
        default = _TUNABLE_DEFAULTS[name]
        env_name = name[4:] if name.startswith("JOB_") else name
        if isinstance(default, str):
            g[name] = _env(env_name, default)
        else:
            g[name] = _env_num(env_name, default, _number)


ALLOWED_JOB_MODELS = ("fable", "opus", "sonnet", "haiku")
MODEL_ALIASES = {"claude-fable-5": "fable"}     # full IDs treated as the same model (§4.2)
CLI_PREFLIGHT_FLAGS = ("--setting-sources", "--settings", "--tools", "--json-schema", "--strict-mcp-config")
TOKEN_HEX_CHARS = 64
INCLUDE_MCP_IN_TOOLS_FLAG = False               # A6: --tools lists built-ins only
DETAIL_MAX = 8000

COST_ENTITY = "sensor.claude_jobs_cost_raw"
ENDPOINT_ENTITY = "sensor.claude_jobs_endpoint"
ICONS = {
    "running": "mdi:progress-clock",
    "ok": "mdi:check-circle-outline",
    "info": "mdi:information-outline",
    "warning": "mdi:alert-outline",
    "critical": "mdi:alert-octagon",
    "error": "mdi:alert-circle",
    "skipped": "mdi:debug-step-over",
    "aborted": "mdi:stop-circle-outline",
}
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

_configure()


def reload_paths() -> None:
    """Re-read every CLAUDE_JOB_* override from os.environ (tests call this after
    ScratchRoot.apply_to_process(); production never needs it)."""
    _configure()


# ---- small utilities ------------------------------------------------------------------------
def log(component: str, msg: str) -> None:
    """One diagnostic line on stderr: `[component] msg`."""
    try:
        sys.stderr.write(f"[{component}] {msg}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


def slug(name: str) -> str:
    return name.replace("-", "_")


def now_utc(now: _dt.datetime | None = None) -> _dt.datetime:
    """Current UTC time (aware). Honors CLAUDE_JOB_FAKE_NOW (ISO string) for tests."""
    if now is not None:
        return now.astimezone(_dt.timezone.utc) if now.tzinfo else now.replace(tzinfo=_dt.timezone.utc)
    fake = os.environ.get("CLAUDE_JOB_FAKE_NOW")
    if fake:
        parsed = parse_iso(fake)
        if parsed is not None:
            return parsed
    return _dt.datetime.now(_dt.timezone.utc)


def iso(dt: _dt.datetime) -> str:
    """Format an aware datetime as `YYYY-MM-DDTHH:MM:SSZ` (UTC)."""
    return dt.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso(now: _dt.datetime | None = None) -> str:
    return iso(now_utc(now))


def parse_iso(s) -> _dt.datetime | None:
    """Parse ISO-8601 (`Z` or offset, with or without fraction) into an aware UTC datetime.
    Returns None for anything unparseable."""
    if not isinstance(s, str) or not s:
        return None
    txt = s.strip()
    if txt.endswith("Z") or txt.endswith("z"):
        txt = txt[:-1] + "+00:00"
    try:
        dt = _dt.datetime.fromisoformat(txt)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc)


def new_run_id(now: _dt.datetime | None = None) -> str:
    """`run-<UTC stamp>-<4 hex>` (§4.9); matches jobdef.RUN_ID_RE."""
    return now_utc(now).strftime("run-%Y%m%dT%H%M%SZ-") + secrets.token_hex(2)


def _round_money(x, places=4) -> float:
    try:
        return round(float(x), places)
    except (TypeError, ValueError):
        return 0.0


# ---- files ----------------------------------------------------------------------------------
_tmp_counter = itertools.count()


def atomic_write_text(path: str, text: str, mode: int = 0o644) -> None:
    """Write `text` to a tmp file in the same directory (created with `mode`) then os.replace
    onto `path`. The tmp name `<path>.tmp.<pid>.<thread>.<n>` is unique per call so threads of
    one process (endpoint tick + request handlers) never collide; the `.tmp.` infix is what the
    tick's duty-5 sweeper matches. On any failure the tmp file is removed and the exception
    propagates."""
    path = str(path)
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}.{next(_tmp_counter)}"
    fd = None
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        os.fchmod(fd, mode)
        data = text.encode("utf-8")
        while data:
            n = os.write(fd, data)
            data = data[n:]
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(tmp, path)
    except BaseException:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: str, obj, mode: int = 0o644, indent: int | None = None) -> None:
    if indent is None:
        text = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    else:
        text = json.dumps(obj, indent=indent, ensure_ascii=False)
    atomic_write_text(path, text + "\n", mode)


def read_json(path: str, default=None):
    """Parsed JSON file, or `default` when missing/unreadable/invalid."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def read_text(path: str, default: str | None = None) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return default


def append_jsonl(path: str, obj) -> None:
    """Append one compact JSON line with a single O_APPEND write (A.4: torn tails tolerated)."""
    line = (json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    os.makedirs(os.path.dirname(str(path)) or ".", exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def prune_jsonl(path: str, max_bytes: int | None = None, keep_lines: int | None = None) -> bool:
    """A.6: when the file exceeds `max_bytes`, rewrite it (atomically) with only its last
    `keep_lines` complete lines (a torn, newline-less last line is dropped). Returns True when
    it pruned. Missing file -> False."""
    max_bytes = JOB_LOG_PRUNE_BYTES if max_bytes is None else max_bytes
    keep_lines = JOB_LOG_KEEP_LINES if keep_lines is None else keep_lines
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    if size <= max_bytes:
        return False
    try:
        with open(path, "rb") as f:
            lines = f.read().splitlines(keepends=True)
    except OSError:
        return False
    if lines and not lines[-1].endswith(b"\n"):
        lines.pop()                       # torn tail (A.4: tolerated, never repaired)
    tail = lines[-keep_lines:] if keep_lines > 0 else []
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        mode = 0o644
    atomic_write_text(path, b"".join(tail).decode("utf-8", "replace"), mode)
    return True


def prune_transcripts(directory: str | None = None, keep_days: float | None = None,
                      max_bytes: int | None = None, now: float | None = None) -> dict:
    """A.7: inside ONE directory (default TRANSCRIPTS_DIR) delete regular files older than
    `keep_days`, then oldest-first until the directory's regular files total <= `max_bytes`.
    Never recurses (the CLI's `memory/` subdir and sibling project dirs are left alone) and
    never follows symlinks. Returns {"deleted": n, "remaining_bytes": b}."""
    directory = TRANSCRIPTS_DIR if directory is None else str(directory)
    keep_days = JOB_TRANSCRIPT_KEEP_DAYS if keep_days is None else keep_days
    max_bytes = JOB_TRANSCRIPT_MAX_BYTES if max_bytes is None else max_bytes
    now = time.time() if now is None else now
    deleted = 0
    files = []
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return {"deleted": 0, "remaining_bytes": 0}
    for e in entries:
        try:
            if not e.is_file(follow_symlinks=False):
                continue
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        files.append((st.st_mtime, st.st_size, e.path))
    cutoff = now - keep_days * 86400
    kept = []
    for mtime, size, p in files:
        if mtime < cutoff:
            try:
                os.unlink(p)
                deleted += 1
                continue
            except OSError:
                pass
        kept.append((mtime, size, p))
    kept.sort()  # oldest first
    total = sum(s for _, s, _ in kept)
    while kept and total > max_bytes:
        mtime, size, p = kept.pop(0)
        try:
            os.unlink(p)
            deleted += 1
            total -= size
        except OSError:
            pass
    return {"deleted": deleted, "remaining_bytes": total}


# ---- locks (R4) -----------------------------------------------------------------------------
def _open_lock(path: str) -> int:
    os.makedirs(os.path.dirname(str(path)) or ".", exist_ok=True)
    return os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)


def flock_nb(path: str) -> int | None:
    """One non-blocking exclusive flock attempt. Returns the open fd (caller keeps it open
    for as long as it wants the lock; closing releases) or None if held elsewhere."""
    fd = _open_lock(path)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError as e:
        os.close(fd)
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return None
        raise


def flock_wait(path: str, seconds: float, should_abort=None, interval: float = 0.25) -> int | None:
    """Retry `flock_nb` every `interval` s until it succeeds, `seconds` elapse, or
    `should_abort()` returns True. Returns fd or None. Interruptible without an external
    `flock` process (design §4.3 step 5)."""
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        fd = flock_nb(path)
        if fd is not None:
            return fd
        if should_abort is not None and should_abort():
            return None
        if time.monotonic() >= deadline:
            return None
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())) or interval)


def write_lock_info(fd: int, info: dict) -> None:
    """Overwrite the lock file body with diagnostics (`{pid, pgid, run_id, started_at}`);
    never used for logic (§4.3 step 4)."""
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, json.dumps(info, separators=(",", ":")).encode("utf-8"))
    except OSError:
        pass


def read_lock_info(path: str) -> dict | None:
    """Holder diagnostics written by `write_lock_info`, or None."""
    obj = read_json(path, None)
    return obj if isinstance(obj, dict) else None


def unlock(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


# ---- add-on options / versions ----------------------------------------------------------------
def addon_options() -> dict:
    obj = read_json(OPTIONS_FILE, None)
    return obj if isinstance(obj, dict) else {}


def addon_option(key: str, default=None):
    return addon_options().get(key, default)


_addon_version_cache = None


def addon_version() -> str:
    """A15: /etc/claudecode-addon-version, else Supervisor /addons/self/info .data.version,
    else "unknown". Cached per process."""
    global _addon_version_cache
    if _addon_version_cache:
        return _addon_version_cache
    txt = read_text(ADDON_VERSION_FILE)
    v = (txt or "").strip()
    if not v:
        status, obj = ha_json("GET", "/addons/self/info", timeout=5)
        if status == 200 and isinstance(obj, dict):
            v = str((obj.get("data") or {}).get("version") or "").strip()
    _addon_version_cache = v or "unknown"
    return _addon_version_cache


def built_cli_version() -> str | None:
    """The CLI version baked into the image (/etc/claude-code-version), or None."""
    txt = read_text(BUILT_CLI_VERSION_FILE)
    v = (txt or "").strip().split()
    return v[0] if v else None


# ---- Home Assistant HTTP (via the Supervisor proxy) ------------------------------------------
# Supervisor and loopback calls must never be routed through an HTTP(S)_PROXY from the environment.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_PATH_SAFE = "/?&=:;,._-~%+@!$'()*[]"     # RFC 3986 reserved+unreserved; space/CR/LF/non-ASCII get %-encoded


def ha_request(method: str, path: str, body=None, timeout: float = 5, token: str | None = None,
               headers: dict | None = None, base_url: str | None = None):
    """`(status, body_bytes)`; status None on connection error/timeout/malformed URL — never
    raises. `body` may be a dict/list (sent as JSON), bytes, str, or None. Auth: Bearer `token`
    (default env SUPERVISOR_TOKEN; omitted when empty). Environment proxies are ignored."""
    try:
        url = (base_url.rstrip("/") if base_url else SUPERVISOR_URL) + urllib.parse.quote(str(path), safe=_PATH_SAFE)
        hdrs = {"Accept": "application/json, text/plain, */*"}
        data = None
        if isinstance(body, (dict, list)):
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        elif isinstance(body, str):
            data = body.encode("utf-8")
        elif isinstance(body, (bytes, bytearray)):
            data = bytes(body)
        tok = os.environ.get("SUPERVISOR_TOKEN", "") if token is None else token
        if tok:
            hdrs["Authorization"] = "Bearer " + tok
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=data, method=method.upper(), headers=hdrs)
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        try:
            payload = e.read()
        except Exception:  # noqa: BLE001 - body is diagnostic only
            payload = b""
        return e.code, payload
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException, UnicodeError):
        return None, b""


def ha_json(method: str, path: str, body=None, timeout: float = 5, **kw):
    """`ha_request` + JSON decode: `(status, obj|None)`."""
    status, raw = ha_request(method, path, body, timeout, **kw)
    if not raw:
        return status, None
    try:
        return status, json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return status, None


def ha_post_state(entity_id: str, state, attrs: dict, timeout: float = 5):
    """POST /core/api/states/<entity_id>; `(ok, http_status|None)` where ok = 200/201."""
    status, _ = ha_request("POST", f"/core/api/states/{entity_id}",
                           {"state": str(state), "attributes": attrs}, timeout=timeout)
    return status in (200, 201), status


# ---- entity payloads (breakdown 2d; the ONLY builders) ----------------------------------------
def truncate_detail(text, cap: int | None = None):
    """Cut `text` to <= `cap` chars at the last newline before the cap (hard cut if the
    first line alone is longer). Returns `(text, truncated: bool)`."""
    cap = DETAIL_ATTR_CAP if cap is None else cap
    text = "" if text is None else str(text)
    if len(text) <= cap:
        return text, False
    cut = text[:cap]
    nl = cut.rfind("\n")
    if nl > 0:
        cut = cut[:nl]
    return cut.rstrip("\n"), True


def entity_payload(state: dict):
    """`(entity_id, state_str, attributes)` for `sensor.claude_job_<slug>` from a state
    file dict (2b). `running` gets the minimal attribute set (§4.3 step 8); every other
    status the terminal set (§4.10). Never publishes input, action selections, denials."""
    job = str(state.get("job") or "")
    slg = str(state.get("slug") or slug(job))
    status = str(state.get("status") or "error")
    description = state.get("description") or ""
    friendly = description or job
    enabled = bool(state.get("enabled", True))
    result = state.get("result") if isinstance(state.get("result"), dict) else {}
    stats = state.get("stats") if isinstance(state.get("stats"), dict) else {}
    if status == "running":
        run = state.get("run") if isinstance(state.get("run"), dict) else {}
        attrs = {
            "job": job,
            "started_at": run.get("started_at"),
            "timeout_s": run.get("timeout_s"),
            "run_id": run.get("run_id"),
            "trigger": run.get("trigger"),
            "prev_status": result.get("status") if result else state.get("prev_status"),
            "enabled": enabled,
            "friendly_name": friendly,
            "icon": ICONS["running"],
        }
        return f"sensor.claude_job_{slg}", "running", attrs
    detail, truncated = truncate_detail(result.get("detail") or "")
    notify = state.get("notify") if isinstance(state.get("notify"), dict) else {}
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    attrs = {
        "job": job,
        "description": description,
        "headline": result.get("headline") or "",
        "detail": detail,
        "detail_truncated": truncated,
        "last_run": result.get("ended_at"),
        "started_at": result.get("started_at"),
        "duration_s": result.get("duration_s"),
        "cost_usd": _round_money(result.get("cost_usd"), 2),
        "run_count": int(stats.get("run_count") or 0),
        "model": result.get("model"),
        "enabled": enabled,
        "stale_after": state.get("stale_after"),
        "run_id": result.get("run_id"),
        "session_id": result.get("session_id"),
        "envelope_subtype": result.get("envelope_subtype"),
        "reason": result.get("reason"),
        "trigger": result.get("trigger"),
        "prev_status": state.get("prev_status"),
        "skipped_since_last": int(stats.get("skipped_since_last") or 0),
        "notify_status": notify.get("notify_status"),
        "attempts": result.get("attempts"),
        "metrics": metrics,
    }
    verrs = result.get("validation_errors")
    if isinstance(verrs, list) and verrs:
        attrs["validation_errors"] = [str(v) for v in verrs[:10]]
    attrs["friendly_name"] = friendly
    attrs["icon"] = ICONS.get(status, ICONS["error"])
    return f"sensor.claude_job_{slg}", status, attrs


def cost_entity_payload(cost: dict | None = None, now: _dt.datetime | None = None):
    """`(entity_id, state, attrs)` for sensor.claude_jobs_cost_raw (2d)."""
    c = cost_current(now=now) if cost is None else cost
    attrs = {
        "month": c.get("month"),
        "runs": int(c.get("runs") or 0),
        "time_zone": c.get("time_zone"),
        "month_start": c.get("month_start_iso"),
        "updated_at": c.get("updated_at") or now_iso(now),
        "unit_of_measurement": "USD",
        "friendly_name": "Claude Jobs cost this month (raw)",
        "icon": "mdi:cash",
    }
    return COST_ENTITY, _round_money(c.get("total_usd"), 2), attrs


def endpoint_error_payload(reason: str, *, missing=None, fast_crashes=None, since=None,
                           claude_version=None):
    """`(entity_id, "error", attrs)` for sensor.claude_jobs_endpoint (only ever `error`)."""
    attrs = {"reason": reason}
    if missing:
        attrs["missing"] = list(missing)
    if fast_crashes is not None:
        attrs["fast_crashes"] = int(fast_crashes)
    attrs["since"] = since or now_iso()
    if claude_version:
        attrs["claude_version"] = claude_version
    attrs["friendly_name"] = "Claude Jobs endpoint"
    attrs["icon"] = "mdi:server-off"
    return ENDPOINT_ENTITY, "error", attrs


# ---- cost accumulator (2e) --------------------------------------------------------------------
def _zone(tz: str | None):
    if tz and ZoneInfo is not None:
        try:
            return ZoneInfo(tz)
        except Exception:  # noqa: BLE001 - unknown key / missing tzdata
            pass
    return _dt.timezone.utc


def month_key(tz: str | None, now: _dt.datetime | None = None) -> str:
    return now_utc(now).astimezone(_zone(tz)).strftime("%Y-%m")


def month_start_iso(month: str, tz: str | None) -> str:
    """First instant of `month` (YYYY-MM) in `tz`, ISO-8601 with numeric offset (A23)."""
    try:
        y, m = (int(x) for x in month.split("-", 1))
    except (ValueError, AttributeError):
        d = now_utc().astimezone(_zone(tz))
        y, m = d.year, d.month
    return _dt.datetime(y, m, 1, tzinfo=_zone(tz)).isoformat()


def _valid_tz_or_utc(tz) -> str:
    """`tz` if it names a zone tzdata knows, else "UTC" (the design's fallback everywhere)."""
    return tz if isinstance(tz, str) and tz and _zone(tz) is not _dt.timezone.utc else "UTC"


def cost_read() -> dict | None:
    obj = read_json(COST_FILE, None)
    return obj if isinstance(obj, dict) else None


def _archive_cost_month(obj: dict) -> None:
    """Write logs/cost-<month>.json. If an archive for that month already exists (it should
    not), merge additively — every cost_add amount lives in exactly one object, so summing
    keeps the grand total honest and never discards the larger archive."""
    path = f"{LOGS_DIR}/cost-{obj['month']}.json"
    prev = read_json(path, None)
    if isinstance(prev, dict) and prev.get("month") == obj.get("month"):
        merged_by_job = dict(prev.get("by_job") or {})
        for k, v in (obj.get("by_job") or {}).items():
            merged_by_job[k] = _round_money(_round_money(merged_by_job.get(k)) + _round_money(v))
        obj = dict(obj, total_usd=_round_money(_round_money(prev.get("total_usd")) + _round_money(obj.get("total_usd"))),
                   runs=int(prev.get("runs") or 0) + int(obj.get("runs") or 0), by_job=merged_by_job,
                   merged_at=now_iso())
        log("claude-job", f"cost archive for {obj['month']} already existed; merged totals")
    atomic_write_json(path, obj, 0o644)


def cost_current(now: _dt.datetime | None = None) -> dict:
    """The current accounting month as `{month, time_zone, total_usd, runs, by_job,
    month_start_iso, updated_at, current}`. If `_cost.json` is absent or names an older
    month (in its own tz), totals are zero and `current` is False."""
    obj = cost_read() or {}
    tz = _valid_tz_or_utc(obj.get("time_zone"))
    cur = month_key(tz, now)
    if obj.get("month") == cur:
        by_job = obj.get("by_job") if isinstance(obj.get("by_job"), dict) else {}
        return {"month": cur, "time_zone": tz, "total_usd": _round_money(obj.get("total_usd")),
                "runs": int(obj.get("runs") or 0), "by_job": dict(by_job),
                "month_start_iso": month_start_iso(cur, tz), "updated_at": obj.get("updated_at"),
                "current": True}
    return {"month": cur, "time_zone": tz, "total_usd": 0.0, "runs": 0, "by_job": {},
            "month_start_iso": month_start_iso(cur, tz), "updated_at": obj.get("updated_at"),
            "current": False}


def cost_add(job: str, usd: float, tz: str | None, *, count_run: bool = True,
             now: _dt.datetime | None = None) -> dict:
    """Add `usd` for `job` to `_cost.json` under flock on `_cost.lock`; on month rollover
    archive the old object to logs/cost-<old>.json and reset. `count_run=False` adds cost
    without incrementing `runs` (breakdown 2e: runs count only when an envelope existed).
    Returns the new accumulator object.

    Month keying is robust against the A12 time-zone fallback: a caller passing "UTC"/None
    while the file already records HA's real zone keeps the file's zone, and a rollover happens
    only when the month changed under BOTH the caller's and the file's zone — so a run whose tz
    fetch failed near midnight on the 1st can neither reset the month nor clobber an archive."""
    tz = _valid_tz_or_utc(tz)
    amount = max(0.0, float(usd or 0))
    fd = flock_wait(COST_LOCK, 10)
    if fd is None:
        log("claude-job", "cost lock busy for 10 s; updating _cost.json without it")
    try:
        obj = cost_read()
        if obj:
            file_tz = _valid_tz_or_utc(obj.get("time_zone"))
            if tz == "UTC" and file_tz != "UTC":
                tz = file_tz                                   # fallback caller; trust the file
            if obj.get("month") and obj.get("month") != month_key(tz, now):
                if obj.get("month") != month_key(file_tz, now):
                    try:
                        _archive_cost_month(obj)               # genuine rollover under both zones
                    except OSError as e:
                        log("claude-job", f"could not archive cost month {obj.get('month')}: {e}")
                    obj = None
                else:
                    tz = file_tz                               # zones disagree about the month: keep the file's view
        cur = month_key(tz, now)
        if not obj or obj.get("month") != cur:
            obj = {"schema": 1, "month": cur, "time_zone": tz, "total_usd": 0.0, "runs": 0, "by_job": {}}
        obj["schema"] = 1
        obj["time_zone"] = tz
        obj["total_usd"] = _round_money(_round_money(obj.get("total_usd")) + amount)
        if count_run:
            obj["runs"] = int(obj.get("runs") or 0) + 1
        by_job = obj.get("by_job") if isinstance(obj.get("by_job"), dict) else {}
        by_job[job] = _round_money(_round_money(by_job.get(job)) + amount)
        obj["by_job"] = by_job
        obj["updated_at"] = now_iso(now)
        atomic_write_json(COST_FILE, obj, 0o644)
        return obj
    finally:
        unlock(fd)


# ---- kill switch (§4.10) ----------------------------------------------------------------------
def _check_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME_RE.match(name) or len(name) > 48:
        raise ValueError(f"invalid job name: {name!r}")
    return name


def flag_path(name: str) -> str:
    return f"{DISABLED_DIR}/{slug(_check_name(name))}"


def is_flag_disabled(name: str) -> bool:
    """True when the kill-switch flag file exists. The canonical spelling is
    `state/disabled/<slug>`; the hyphenated `<name>` spelling (what a human types) is
    honored too — names never contain `_` and slugs never contain `-`, so they cannot clash."""
    _check_name(name)
    return os.path.exists(f"{DISABLED_DIR}/{slug(name)}") or os.path.exists(f"{DISABLED_DIR}/{name}")


def state_path(name: str) -> str:
    return f"{STATE_DIR}/{_check_name(name)}.json"


def lock_path(name: str) -> str:
    return f"{STATE_DIR}/{_check_name(name)}.lock"


def log_path(name: str) -> str:
    return f"{LOGS_DIR}/{_check_name(name)}.jsonl"


def read_state(name: str) -> dict | None:
    obj = read_json(state_path(name), None)
    return obj if isinstance(obj, dict) else None


def set_enabled(name: str, enabled: bool, *, frontmatter_enabled: bool = True,
                publish_timeout: float = 5) -> dict:
    """Create/remove the flag file atomically, then — only if the job lock is free
    (LOCK_NB; a busy lock means a live runner will write a fresher file) — rewrite the state
    file's `enabled` with `published:false` and attempt one publish. Returns
    `{job, enabled, flag_disabled, enabled_frontmatter, state_written, published}` where
    `enabled` is the effective value (flag absent AND frontmatter not false)."""
    _check_name(name)
    os.makedirs(DISABLED_DIR, exist_ok=True)
    canonical = f"{DISABLED_DIR}/{slug(name)}"
    alt = f"{DISABLED_DIR}/{name}"
    if enabled:
        for p in (canonical, alt):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass
    else:
        atomic_write_text(canonical, "", 0o644)
    effective = (not is_flag_disabled(name)) and bool(frontmatter_enabled)
    out = {"job": name, "enabled": effective, "flag_disabled": is_flag_disabled(name),
           "enabled_frontmatter": bool(frontmatter_enabled), "state_written": False, "published": None}
    fd = flock_nb(lock_path(name))
    if fd is None:
        return out
    try:
        st = read_state(name)
        if st is None:
            return out
        st["enabled"] = effective
        st["published"] = False
        st["updated_at"] = now_iso()
        atomic_write_json(state_path(name), st, 0o644, indent=1)
        out["state_written"] = True
        eid, sval, attrs = entity_payload(st)
        ok, _status = ha_post_state(eid, sval, attrs, timeout=publish_timeout)
        out["published"] = ok
        if ok:
            st["published"] = True
            atomic_write_json(state_path(name), st, 0o644, indent=1)
    finally:
        unlock(fd)
    return out


# ---- per-job summary (shared by `claude-job list --json` and endpoint GET /jobs) --------------
def job_summary(name: str, job, state: dict | None, now: _dt.datetime | None = None,
                load_errors: list | None = None, mtime: float | None = None) -> dict:
    """The per-job dict of breakdown 2k `jobs[]`. `job` is duck-typed (a jobdef.JobDef or
    anything with .slug/.kind/.description/.enabled/.stale_after/.model/.mtime) or None when
    the definition failed to load; `state` is the parsed state file or None."""
    now_dt = now_utc(now)
    load_errors = list(load_errors or [])
    st = state if isinstance(state, dict) else {}
    result = st.get("result") if isinstance(st.get("result"), dict) else None
    stats = st.get("stats") if isinstance(st.get("stats"), dict) else {}
    enabled_fm = bool(getattr(job, "enabled", True)) if job is not None else True
    flag = is_flag_disabled(name)
    enabled = enabled_fm and not flag
    stale_after = getattr(job, "stale_after", None) if job is not None else st.get("stale_after")
    if mtime is None:
        mtime = getattr(job, "mtime", None) if job is not None else None
    if mtime is None:
        try:
            mtime = os.path.getmtime(f"{JOBS_DIR}/{name}.md")
        except OSError:
            mtime = None
    last_run = result.get("ended_at") if result else None
    if not last_run and mtime is not None:
        last_run = iso(_dt.datetime.fromtimestamp(mtime, _dt.timezone.utc))
    stale = False
    if stale_after and enabled and last_run:
        lr = parse_iso(last_run)
        if lr is not None:
            stale = (now_dt - lr).total_seconds() > float(stale_after)
    description = (getattr(job, "description", None) if job is not None else None) \
        or st.get("description") or ""
    model = getattr(job, "model", None) if job is not None else None
    if model is None and result:
        model = result.get("model")
    return {
        "name": name,
        "slug": getattr(job, "slug", None) or slug(name),
        "kind": getattr(job, "kind", None) or "job",
        "description": description,
        "status": st.get("status") or "never_run",
        "headline": (result or {}).get("headline") if result else None,
        "last_run": last_run,
        "enabled": enabled,
        "enabled_frontmatter": enabled_fm,
        "flag_disabled": flag,
        "stale": stale,
        "stale_after": stale_after,
        "model": model,
        "valid": job is not None and not load_errors,
        "errors": len(load_errors),
        "run_count": int(stats.get("run_count") or 0),
        "cost_usd_last": _round_money((result or {}).get("cost_usd"), 2) if result else None,
    }


# ---- endpoint token (§4.9) --------------------------------------------------------------------
def read_token() -> str | None:
    """The endpoint bearer token, or None when the file is missing/empty."""
    txt = read_text(TOKEN_FILE)
    tok = (txt or "").strip()
    return tok or None


def write_token(token: str | None = None) -> str:
    """Write a (new) 64-hex token to DATA_DIR/token, file 0600 in a 0700 dir. Returns it."""
    tok = token or secrets.token_hex(TOKEN_HEX_CHARS // 2)
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        os.chmod(DATA_DIR, 0o700)
    except OSError:
        pass
    atomic_write_text(TOKEN_FILE, tok + "\n", 0o600)
    return tok


def ensure_token() -> str:
    return read_token() or write_token()


# ---- CLI preflight (§4.9; cache per breakdown 2f) --------------------------------------------
def _claude_identity():
    """`(realpath, mtime)` of CLAUDE_BIN, or (None, None) when not found."""
    p = shutil.which(CLAUDE_BIN) if os.sep not in CLAUDE_BIN else (CLAUDE_BIN if os.path.exists(CLAUDE_BIN) else None)
    if not p:
        return None, None
    rp = os.path.realpath(p)
    try:
        return rp, os.path.getmtime(rp)
    except OSError:
        return rp, None


def _run_claude(args: list, timeout: float = 30) -> str | None:
    try:
        cp = subprocess.run([CLAUDE_BIN, *args], stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return (cp.stdout or "") + ("\n" + cp.stderr if cp.stderr else "")


def cli_preflight(force: bool = False) -> dict:
    """Grep `claude --help` for CLI_PREFLIGHT_FLAGS and read `claude --version`.
    `{"ok", "missing", "json_schema_fallback", "claude_version", "built_version", "cli_drift",
      "checked_at", "claude_realpath", "claude_realpath_mtime", "error"?}`.
    `ok` is False iff the binary is missing or any flag other than --json-schema is missing;
    --json-schema alone missing -> ok True + json_schema_fallback True (A16: runner refuses).
    Cached in RUN_DIR/cli_preflight.json for PREFLIGHT_CACHE_TTL_S or until the binary changes."""
    realpath, mtime = _claude_identity()
    if not force:
        cached = read_json(PREFLIGHT_CACHE, None)
        if isinstance(cached, dict) and cached.get("claude_realpath") == realpath \
                and cached.get("claude_realpath_mtime") == mtime and realpath is not None:
            at = parse_iso(cached.get("checked_at"))
            if at is not None and (now_utc() - at).total_seconds() < PREFLIGHT_CACHE_TTL_S:
                return cached
    built = built_cli_version()
    out = {"ok": False, "missing": [], "json_schema_fallback": False, "claude_version": None,
           "built_version": built, "cli_drift": False, "checked_at": now_iso(),
           "claude_realpath": realpath, "claude_realpath_mtime": mtime}
    if realpath is None:
        out["missing"] = list(CLI_PREFLIGHT_FLAGS)
        out["error"] = f"{CLAUDE_BIN}: not found"
    else:
        help_txt = _run_claude(["--help"])
        if help_txt is None:
            out["missing"] = list(CLI_PREFLIGHT_FLAGS)
            out["error"] = f"{CLAUDE_BIN} --help failed"
        else:
            present = set(re.findall(r"--[a-z][a-z0-9-]*", help_txt))
            out["missing"] = [f for f in CLI_PREFLIGHT_FLAGS if f not in present]
        ver_txt = _run_claude(["--version"]) or ""
        m = re.search(r"\d+\.\d+\.\d+[^\s]*", ver_txt)
        out["claude_version"] = m.group(0) if m else (ver_txt.strip().split() or [None])[0]
        hard = [f for f in out["missing"] if f != "--json-schema"]
        out["ok"] = not hard and help_txt is not None
        out["json_schema_fallback"] = "--json-schema" in out["missing"] and not hard
    out["cli_drift"] = bool(built and out["claude_version"] and built != out["claude_version"])
    try:
        if os.path.isdir(RUN_DIR):
            atomic_write_json(PREFLIGHT_CACHE, out, 0o600)
    except OSError:
        pass
    return out
