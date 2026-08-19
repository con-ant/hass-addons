"""Shared helpers for the Claude Jobs test-suite (design §11 test discipline, breakdown §0/§3 U0).

Import this first in every test module:

    from testlib import ScratchRoot, run_cli, wait_for, BIN_DIR, LIB_DIR, SHARE_DIR, FAKES_DIR
    from fakes.fake_supervisor import FakeSupervisor

Importing it puts `rootfs/usr/local/lib/claude-job` on sys.path so `import jobcommon, jobdef`
work in-process, and puts the tests dir on sys.path so `fakes.*` imports work under
`python3 -m unittest discover -s claudecode/tests`.

`ScratchRoot` builds a throw-away volume layout mirroring the add-on's (breakdown §0 path table)
and an environment dict (`.env`) whose CLAUDE_JOB_* overrides point every path/binary constant of
`jobcommon` into it. Nothing here touches the network, HA, or real tokens.
"""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

# ---- source-tree constants (contract: other units import these names) -----------------------
REPO = Path(__file__).resolve().parents[2]          # /root/src/hass-addons
ADDON = REPO / "claudecode"
BIN_DIR = ADDON / "rootfs/usr/local/bin"
LIB_DIR = ADDON / "rootfs/usr/local/lib/claude-job"
SHARE_DIR = ADDON / "rootfs/usr/share/claudecode"
TESTS_DIR = Path(__file__).resolve().parent
FAKES_DIR = TESTS_DIR / "fakes"

for _p in (str(TESTS_DIR), str(LIB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

FAKE_CLAUDE = FAKES_DIR / "fake_claude.py"
FAKE_RUNNER = FAKES_DIR / "fake_runner.py"
FAKE_NOTIFY = FAKES_DIR / "fake_notify.py"
FAKE_HA = FAKES_DIR / "fake_ha.py"
FAST_TIMEOUT = FAKES_DIR / "fast_timeout.sh"

RUNNER = BIN_DIR / "claude-job"
NOTIFIER = BIN_DIR / "claude-job-notify"
ENDPOINT = BIN_DIR / "claude-job-endpoint"
BROKER = LIB_DIR / "ha_broker.py"
HA_WRAPPER = LIB_DIR / "bin" / "ha"

SUPERVISOR_TOKEN = "test-supervisor-token"
BUILT_CLI_VERSION = "2.1.233"
ADDON_VERSION = "9.9.9-test"

# ---- reusable frontmatter --------------------------------------------------------------------
MINIMAL_FM = {"description": "t", "tools": ["Bash(ha core info)"]}

# Design §4.2 example minus `input:`/`actions:` (= shipped health-check.md frontmatter, A11).
HEALTH_CHECK_FM = {
    "description": "Daily health check; reports anything a human should look at.",
    "timeout": 600,
    "max_cost_usd": 1.50,
    "max_turns": 50,
    "stale_after": 93600,
    "paths": ["/homeassistant/**"],
    "tools": [
        "Bash(ha core check)",
        "Bash(ha core logs:*)",
        "Bash(ha supervisor info)",
        "Bash(ha resolution info)",
        "mcp__homeassistant__get_error_log",
        "mcp__homeassistant__list_automations",
    ],
    "notify": {
        "critical": ["mobile_critical", "persistent"],
        "warning": ["mobile", "persistent"],
        "ok": ["state_only"],
    },
}


# ---- process helpers -------------------------------------------------------------------------
def run_cli(argv, env, timeout=60, cwd=None, input=None):
    """Run a command (list; Paths allowed) and return (returncode, stdout, stderr) as text.
    stdin is /dev/null unless `input` is given."""
    argv = [str(a) for a in argv]
    kw = {"stdin": subprocess.DEVNULL} if input is None else {"input": input}
    cp = subprocess.run(argv, env=env, cwd=str(cwd) if cwd else None, capture_output=True,
                        text=True, timeout=timeout, **kw)
    return cp.returncode, cp.stdout, cp.stderr


def wait_for(predicate, timeout=10, interval=0.05) -> bool:
    """Poll `predicate()` until truthy or `timeout` seconds pass. Returns the final truthiness."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            if predicate():
                return True
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def read_jsonl(path) -> list:
    """Parse a JSONL file, skipping blank/unparseable lines. Missing file -> []."""
    out = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except FileNotFoundError:
        pass
    return out


# ---- scratch volume --------------------------------------------------------------------------
class ScratchRoot:
    """A throw-away add-on filesystem + environment.

    Usable as a context manager (`with ScratchRoot() as s:`) or via `.start()` / `.stop()` from
    setUp/tearDown. After start():

      paths : root, ha_config, persist, jobs, state, logs, inbox, disabled, data, project,
              run_dir, transcripts, bin (scratch dir first on PATH), home, fakelogs,
              options_file, fake_claude_log, fake_runner_log, fake_notify_log, fake_ha_log
      env   : dict to hand to subprocesses (see module doc); mutate via setenv()/helpers.
    """

    def __init__(self, prefix="claudejob-"):
        self._prefix = prefix
        self.root = None
        self.env = {}

    # -- lifecycle --
    def start(self):
        self.root = Path(tempfile.mkdtemp(prefix=self._prefix)).resolve()
        r = self.root
        self.ha_config = r / "homeassistant"
        self.persist = self.ha_config / ".claudecode"
        self.jobs = self.persist / "jobs"
        self.state = self.jobs / "state"
        self.logs = self.jobs / "logs"
        self.inbox = self.state / "inbox"
        self.disabled = self.state / "disabled"
        self.data = r / "data" / "claude-jobs"
        self.project = self.data / "project"
        self.run_dir = r / "run" / "claudecode"
        self.transcripts = self.persist / "projects" / str(self.project).replace("/", "-")
        self.bin = r / "bin"
        self.home = r / "home"
        self.fakelogs = r / "fakelogs"
        self.etc = r / "etc"
        for d in (self.logs, self.inbox, self.disabled, self.project, self.run_dir / "jobs",
                  self.run_dir / "run", self.transcripts, self.bin, self.home / ".claude",
                  self.fakelogs, self.etc, r / "data"):
            d.mkdir(parents=True, exist_ok=True)
        os.chmod(self.run_dir, 0o700)

        self.options_file = r / "data" / "options.json"
        self.options_file.write_text(json.dumps({"job_default_model": "opus", "enable_job_endpoint": True}))
        self.built_version_file = self.etc / "claude-code-version"
        self.built_version_file.write_text(BUILT_CLI_VERSION + "\n")
        self.addon_version_file = self.etc / "claudecode-addon-version"
        self.addon_version_file.write_text(ADDON_VERSION + "\n")

        self.fake_claude_log = self.fakelogs / "claude.jsonl"
        self.fake_runner_log = self.fakelogs / "runner.jsonl"
        self.fake_notify_log = self.fakelogs / "notify.jsonl"
        self.fake_ha_log = self.fakelogs / "ha.jsonl"

        # Wrappers so PATH lookup / exec of a bare name works regardless of +x bits in the checkout.
        self.claude_wrapper = self.make_wrapper("claude", FAKE_CLAUDE)
        self.runner_wrapper = self.make_wrapper("claude-job", RUNNER)
        self.fake_runner_wrapper = self.make_wrapper("fake-claude-job", FAKE_RUNNER)
        self.notify_wrapper = self.make_wrapper("claude-job-notify-real", NOTIFIER)
        self.fake_notify_wrapper = self.make_wrapper("fake-claude-job-notify", FAKE_NOTIFY)
        self.fake_ha_wrapper = self.make_wrapper("real-ha", FAKE_HA)
        self.fast_timeout_wrapper = self.make_wrapper("fast-timeout", FAST_TIMEOUT, interpreter="/bin/bash")

        self.env = {
            # jobcommon path overrides (breakdown §0 table)
            "CLAUDE_JOB_ROOT": str(r),
            "CLAUDE_JOB_HA_CONFIG_DIR": str(self.ha_config),
            "CLAUDE_JOB_PERSIST_DIR": str(self.persist),
            "CLAUDE_JOB_JOBS_DIR": str(self.jobs),
            "CLAUDE_JOB_DATA_DIR": str(self.data),
            "CLAUDE_JOB_RUN_DIR": str(self.run_dir),
            "CLAUDE_JOB_TRANSCRIPTS_DIR": str(self.transcripts),
            "CLAUDE_JOB_SHARE_DIR": str(SHARE_DIR),
            "CLAUDE_JOB_LIB_DIR": str(LIB_DIR),
            "CLAUDE_JOB_OPTIONS_FILE": str(self.options_file),
            "CLAUDE_JOB_BUILT_VERSION_FILE": str(self.built_version_file),
            "CLAUDE_JOB_ADDON_VERSION_FILE": str(self.addon_version_file),
            # binaries
            "CLAUDE_JOB_CLAUDE_BIN": str(self.claude_wrapper),
            "CLAUDE_JOB_TIMEOUT_BIN": "timeout",
            "CLAUDE_JOB_RUNNER_BIN": str(self.runner_wrapper),
            "CLAUDE_JOB_NOTIFY_BIN": str(self.bin / "claude-job-notify-absent"),   # does NOT exist by default
            "CLAUDE_JOB_REAL_HA": str(self.fake_ha_wrapper),
            # process basics
            "SUPERVISOR_TOKEN": SUPERVISOR_TOKEN,
            "HOME": str(self.home),
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "TERM": "dumb",
            "PYTHONDONTWRITEBYTECODE": "1",
            # fakes
            "FAKE_CLAUDE_LOG": str(self.fake_claude_log),
            "FAKE_CLAUDE_SCENARIO": "success",
            "FAKE_CLAUDE_AUTH_ONCE": str(self.fakelogs / "claude-auth-once"),
            "FAKE_RUNNER_LOG": str(self.fake_runner_log),
            "FAKE_NOTIFY_LOG": str(self.fake_notify_log),
            "FAKE_HA_LOG": str(self.fake_ha_log),
        }
        self._restore_env = None
        return self

    def stop(self):
        if self._restore_env:
            self._restore_env()
            self._restore_env = None
        if self.root and self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
        self.root = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False

    # -- env manipulation --
    def setenv(self, **kv):
        """Update self.env; a value of None removes the key."""
        for k, v in kv.items():
            if v is None:
                self.env.pop(k, None)
            else:
                self.env[k] = str(v)
        return self

    def with_supervisor(self, fake):
        """Point CLAUDE_JOB_SUPERVISOR_URL at a started FakeSupervisor."""
        self.env["CLAUDE_JOB_SUPERVISOR_URL"] = fake.url
        return self

    def use_fake_notify(self):
        self.env["CLAUDE_JOB_NOTIFY_BIN"] = str(self.fake_notify_wrapper)
        return self

    def use_real_notify(self):
        self.env["CLAUDE_JOB_NOTIFY_BIN"] = str(self.notify_wrapper)
        return self

    def use_no_notify(self):
        self.env["CLAUDE_JOB_NOTIFY_BIN"] = str(self.bin / "claude-job-notify-absent")
        return self

    def use_fake_runner(self):
        self.env["CLAUDE_JOB_RUNNER_BIN"] = str(self.fake_runner_wrapper)
        return self

    def use_real_runner(self):
        self.env["CLAUDE_JOB_RUNNER_BIN"] = str(self.runner_wrapper)
        return self

    def use_fast_timeout(self, seconds=2, kill_after=2):
        """Swap CLAUDE_JOB_TIMEOUT_BIN for the shim that enforces `seconds` regardless of argv."""
        self.env["CLAUDE_JOB_TIMEOUT_BIN"] = str(self.fast_timeout_wrapper)
        self.env["FAST_TIMEOUT_S"] = str(seconds)
        self.env["FAST_TIMEOUT_KILL_S"] = str(kill_after)
        return self

    def apply_to_process(self):
        """os.environ.update(self.env) for in-process tests of jobcommon/jobdef.
        Returns restore(); stop() also calls it. Callers that need module-level constants
        re-evaluated should `importlib.reload(jobcommon)` after this."""
        saved = dict(os.environ)
        os.environ.update(self.env)

        def restore():
            os.environ.clear()
            os.environ.update(saved)
        self._restore_env = restore
        return restore

    # -- files --
    def make_wrapper(self, name, target, interpreter=sys.executable) -> Path:
        """Write bin/<name> = `#!/bin/sh\\nexec <interpreter> <target> "$@"` (mode 755)."""
        p = self.bin / name
        p.write_text("#!/bin/sh\nexec {} {} \"$@\"\n".format(shlex.quote(str(interpreter)),
                                                              shlex.quote(str(target))))
        p.chmod(0o755)
        return p

    def write_job(self, name, frontmatter: dict, body: str = "Do the thing.\n") -> Path:
        """jobs/<name>.md = '---\\n' + yaml + '---\\n' + body. `name` may include '.md'."""
        fname = name if name.endswith(".md") else f"{name}.md"
        p = self.jobs / fname
        fm = yaml.safe_dump(frontmatter, sort_keys=False, default_flow_style=False)
        p.write_text(f"---\n{fm}---\n{body}")
        return p

    def write_raw_job(self, filename, text) -> Path:
        """Write jobs/<filename> verbatim (for malformed-frontmatter tests)."""
        p = self.jobs / filename
        p.write_text(text)
        return p

    def write_notify_config(self, cfg: dict) -> Path:
        p = self.jobs / "_notify.yaml"
        p.write_text(yaml.safe_dump(cfg, sort_keys=False))
        return p

    def state_path(self, name) -> Path:
        return self.state / f"{name}.json"

    def write_state(self, name, obj: dict) -> Path:
        p = self.state_path(name)
        p.write_text(json.dumps(obj, indent=1))
        os.chmod(p, 0o644)
        return p

    def read_state(self, name):
        try:
            return json.loads(self.state_path(name).read_text())
        except FileNotFoundError:
            return None

    def read_json(self, path, default=None):
        try:
            return json.loads(Path(path).read_text())
        except (FileNotFoundError, ValueError):
            return default

    def read_jsonl(self, path) -> list:
        return read_jsonl(path)

    def job_log(self, name) -> list:
        """Parsed logs/<name>.jsonl."""
        return read_jsonl(self.logs / f"{name}.jsonl")

    def fake_claude_calls(self) -> list:
        return read_jsonl(self.fake_claude_log)

    def fake_runner_calls(self) -> list:
        return read_jsonl(self.fake_runner_log)

    def fake_notify_calls(self) -> list:
        return read_jsonl(self.fake_notify_log)

    def fake_ha_calls(self) -> list:
        return read_jsonl(self.fake_ha_log)

    def set_flag_disabled(self, name, disabled=True) -> Path:
        """Create/remove state/disabled/<name> (the runtime kill-switch flag file)."""
        p = self.disabled / name
        if disabled:
            p.write_text("")
        elif p.exists():
            p.unlink()
        return p

    def write_token(self, token="ab" * 32) -> Path:
        """data/claude-jobs/token (mode 600), as `claude-job token rotate` would."""
        p = self.data / "token"
        p.write_text(token + "\n")
        os.chmod(p, 0o600)
        return p


def scratch_root() -> ScratchRoot:
    """Functional spelling: `with scratch_root() as s:`."""
    return ScratchRoot()
