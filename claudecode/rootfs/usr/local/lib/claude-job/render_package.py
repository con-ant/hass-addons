#!/usr/bin/env python3
"""Render the Home Assistant side of Claude Jobs (design §4.10 "The generated package", §6.4;
breakdown 2l; CONTRACTS A26/A27).

Called by `claudecode-start` at every add-on boot when `enable_job_endpoint` is on:

    python3 /usr/local/lib/claude-job/render_package.py

It writes two files into the Home Assistant config directory (which must already exist — an unmounted
/homeassistant is never fabricated) and touches nothing else:

  * `/homeassistant/claudecode_jobs.yaml` (0600) — the package, rendered from
    `templates/claudecode_jobs.yaml.tmpl` with the add-on hostname, endpoint port and the endpoint
    bearer token substituted. Regenerated every boot so token rotation and hostname changes self-heal.
  * `/homeassistant/blueprints/automation/claudecode/schedule.yaml` (0644) — the static schedule
    blueprint, written only when absent or different.

Best effort around the write: on a git-backed config dir it adds the file to `.git/info/exclude`
*before* writing it and warns afterwards if the file is already tracked; and when the package content changed
materially it asks Core to reload `rest_command` and `rest` so a rotated token/hostname is live
without a restart. Exit codes: 0 written, 1 package written but blueprint/git guard failed,
2 nothing written (no/invalid token, invalid hostname, template problem).

The token is never printed; log lines go to stdout/stderr prefixed `[claude-job-package]`.
"""
import argparse
import json
import os
import pathlib
import re
import socket
import subprocess
import sys

_LIB = os.environ.get("CLAUDE_JOB_LIB_DIR") or str(pathlib.Path(__file__).resolve().parent)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import jobcommon as jc  # noqa: E402

TAG = "[claude-job-package]"
PACKAGE_FILE = "claudecode_jobs.yaml"
TEMPLATE_FILE = "claudecode_jobs.yaml.tmpl"
BLUEPRINT_TEMPLATE_FILE = "schedule.blueprint.yaml"
BLUEPRINT_REL_PATH = pathlib.Path("blueprints/automation/claudecode/schedule.yaml")
EXCLUDE_LINE = "/" + PACKAGE_FILE
TOKEN_RE = re.compile(r"^[0-9a-f]{%d}$" % jc.TOKEN_HEX_CHARS)
HOSTNAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
PLACEHOLDER_RE = re.compile(r"__[A-Z_]+__")
GENERATED_MARKER = "GENERATED"          # the one header line carrying the timestamp contains this word
GIT_TIMEOUT_S = 10
RELOAD_TIMEOUT_S = 10
RELOAD_DOMAINS = ("rest_command", "rest")   # never automation/template (A27)

EXIT_OK, EXIT_PARTIAL, EXIT_NOTHING = 0, 1, 2


# ---- logging (never pass the token to these) -------------------------------------------------
class Log:
    def __init__(self, quiet=False):
        self.quiet = quiet

    def info(self, msg):
        if not self.quiet:
            print(f"{TAG} {msg}", flush=True)

    def warn(self, msg):
        print(f"{TAG} [WARN] {msg}", flush=True)

    def error(self, msg):
        print(f"{TAG} {msg}", file=sys.stderr, flush=True)


# ---- Supervisor / Core HTTP ------------------------------------------------------------------
def ha_request(method: str, path: str, body=None, timeout: float = 5.0):
    """(status, body_bytes) via jobcommon; status 0 when the Supervisor is unreachable."""
    status, data = jc.ha_request(method, path, body, timeout=timeout)
    return int(status or 0), data or b""


# ---- inputs ----------------------------------------------------------------------------------
def read_token(path: pathlib.Path, log: Log):
    """The endpoint token, or None (with the reason already logged)."""
    try:
        token = path.read_text().strip()
    except OSError:
        token = ""
    if not token:
        log.error(f"no endpoint token at {path}; is enable_job_endpoint on?")
        return None
    if not TOKEN_RE.match(token):
        log.error(f"endpoint token at {path} is malformed (expected 64 lowercase hex characters); "
                  f"run 'claude-job token rotate' and restart the add-on")
        return None
    return token


def discover_hostname(log: Log) -> str:
    """Supervisor `GET /addons/self/info` .data.hostname, else the container hostname (ha-facts §G)."""
    status, body = ha_request("GET", "/addons/self/info", timeout=5)
    if 200 <= status < 300:
        try:
            host = json.loads(body.decode("utf-8", "replace")).get("data", {}).get("hostname")
        except (ValueError, AttributeError):
            host = None
        if isinstance(host, str) and host:
            log.info(f"hostname from Supervisor: {host}")
            return host
        why = "Supervisor /addons/self/info carried no data.hostname"
    elif status:
        why = f"Supervisor /addons/self/info answered HTTP {status}"
    else:
        why = "Supervisor unreachable"
    host = socket.gethostname().split(".")[0].lower()
    log.info(f"{why}; using the container hostname {host}")
    return host


# ---- rendering -------------------------------------------------------------------------------
def render(template: str, *, host: str, port: int, token: str, scan_interval: int,
           version: str, generated_at: str) -> str:
    """Plain str.replace — the template is full of Jinja braces and `$`, so no format()/Template."""
    out = template
    for placeholder, value in (
        ("__CLAUDECODE_HOST__", host),
        ("__CLAUDECODE_PORT__", str(int(port))),
        ("__CLAUDECODE_TOKEN__", token),
        ("__SCAN_INTERVAL__", str(int(scan_interval))),
        ("__ADDON_VERSION__", version),
        ("__GENERATED_AT__", generated_at),
    ):
        out = out.replace(placeholder, value)
    return out


def without_generated_line(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if GENERATED_MARKER not in line)


def material_change(out: pathlib.Path, rendered: str) -> bool:
    """True unless the existing file equals `rendered` ignoring the timestamp header line (A27)."""
    try:
        previous = out.read_text()
    except OSError:
        return True
    return without_generated_line(previous) != without_generated_line(rendered)


def atomic_write_text(path: pathlib.Path, text: str, mode: int):
    """`<path>.tmp.<pid>` in the same directory, created with `mode`, then os.replace (jobcommon)."""
    jc.atomic_write_text(str(path), text, mode=mode)


def install_blueprint(src: pathlib.Path, dest: pathlib.Path, log: Log):
    """Write-if-different (0644). A changed existing file needs automation.reload to be picked up (A27)."""
    content = src.read_text()
    try:
        existing = dest.read_text()
    except OSError:
        existing = None
    if existing == content:
        log.info(f"blueprint up to date: {dest}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(dest, content, 0o644)
    if existing is None:
        log.info(f"blueprint installed: {dest} (appears under Settings → Automations & scenes → Blueprints "
                 f"after a page refresh)")
    else:
        log.info(f"blueprint updated: {dest} — run automation.reload (Developer tools → YAML → Automations) "
                 f"to pick up the updated blueprint")


# ---- git guard (design §4.10 "Git-backed config guard") ---------------------------------------
def _git(ha_config: pathlib.Path, *args):
    """Run git against the config dir; returns CompletedProcess or None if git is unusable."""
    d = str(ha_config)
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    try:
        return subprocess.run(["git", "-C", d, "-c", f"safe.directory={d}", *args],
                              capture_output=True, text=True, timeout=GIT_TIMEOUT_S, env=env,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None


def resolve_exclude_path(ha_config: pathlib.Path):
    """`git rev-parse --git-path info/exclude` (handles `.git` files/worktrees); fallback `.git/info/exclude`."""
    cp = _git(ha_config, "rev-parse", "--git-path", "info/exclude")
    if cp is not None and cp.returncode == 0 and cp.stdout.strip():
        p = pathlib.Path(cp.stdout.strip())
        return p if p.is_absolute() else ha_config / p
    dotgit = ha_config / ".git"
    return dotgit / "info" / "exclude" if dotgit.is_dir() else None


def ensure_exclude_line(exclude: pathlib.Path) -> bool:
    """Append EXCLUDE_LINE once (creating dirs/file). Returns True if the file was modified."""
    try:
        text = exclude.read_text()
    except FileNotFoundError:
        text = ""
    if any(line.strip() == EXCLUDE_LINE for line in text.splitlines()):
        return False
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with open(exclude, "a") as f:
        if text and not text.endswith("\n"):
            f.write("\n")
        f.write(f"# Claude Code add-on: the generated package carries the job-endpoint token\n{EXCLUDE_LINE}\n")
    return True


def ensure_git_exclude(ha_config: pathlib.Path, out: pathlib.Path, log: Log) -> bool:
    """BEFORE the package is written: make sure git ignores it (M4 — closes the window an auto-commit
    watcher could use). Returns False only when a checkout exists but the entry could not be ensured."""
    if not (ha_config / ".git").exists():
        return True
    exclude = resolve_exclude_path(ha_config)
    if exclude is None:
        log.warn(f"{ha_config}/.git exists but its info/exclude could not be located (git missing?); "
                 f"make sure {out.name} is ignored by your config repository")
        return False
    try:
        if ensure_exclude_line(exclude):
            log.info(f"added '{EXCLUDE_LINE}' to {exclude} (keeps the token file out of git)")
    except OSError as exc:
        log.warn(f"could not add '{EXCLUDE_LINE}' to {exclude}: {exc}; "
                 f"make sure {out.name} is ignored by your config repository")
        return False
    return True


def warn_if_tracked(ha_config: pathlib.Path, out: pathlib.Path, log: Log):
    """AFTER the write: an already-tracked file is beyond any ignore mechanism — say so loudly."""
    if not (ha_config / ".git").exists():
        return
    cp = _git(ha_config, "ls-files", "--error-unmatch", "--", out.name)
    if cp is None:
        log.info("git not available; skipped the 'is the package tracked' check")
    elif cp.returncode == 0:
        log.warn(f"{out} is TRACKED by git: the endpoint token is in your repository history. "
                 f"Run 'claude-job token rotate', remove the file from history, and keep it ignored.")


# ---- reload (A27) ----------------------------------------------------------------------------
def reload_core_integrations(log: Log):
    for domain in RELOAD_DOMAINS:
        status, _ = ha_request("POST", f"/core/api/services/{domain}/reload", {}, timeout=RELOAD_TIMEOUT_S)
        if 200 <= status < 300:
            log.info(f"reloaded {domain} in Home Assistant")
        else:
            log.info(f"could not reload {domain} (HTTP {status or 'unreachable'}); expected until the package "
                     f"is included and Home Assistant restarted once — otherwise reload it under "
                     f"Developer tools → YAML")


# ---- main ------------------------------------------------------------------------------------
def parse_args(argv):
    ha_config = pathlib.Path(jc.HA_CONFIG_DIR)
    p = argparse.ArgumentParser(prog="render_package.py", description="Render /homeassistant/claudecode_jobs.yaml "
                                "and install the Claude Job schedule blueprint.")
    p.add_argument("--hostname", help="add-on hostname as Home Assistant resolves it (default: ask the Supervisor)")
    p.add_argument("--token-file", type=pathlib.Path, default=pathlib.Path(jc.TOKEN_FILE))
    p.add_argument("--port", type=int, default=jc.JOB_ENDPOINT_PORT)
    p.add_argument("--scan-interval", type=int, default=jc.JOB_ANCHOR_SCAN_INTERVAL_S,
                   help="seconds between GET /jobs polls of the anchor sensor")
    p.add_argument("--out", type=pathlib.Path, default=ha_config / PACKAGE_FILE)
    p.add_argument("--blueprint-out", type=pathlib.Path, default=ha_config / BLUEPRINT_REL_PATH)
    p.add_argument("--no-git-guard", action="store_true")
    p.add_argument("--no-reload", action="store_true", help="do not call rest_command.reload / rest.reload")
    p.add_argument("--quiet", action="store_true", help="only warnings and errors")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    log = Log(quiet=args.quiet)

    token = read_token(args.token_file, log)
    if token is None:
        return EXIT_NOTHING
    if not 1 <= args.port <= 65535 or args.scan_interval < 1:
        log.error("--port must be 1..65535 and --scan-interval a positive number of seconds")
        return EXIT_NOTHING
    host = args.hostname if args.hostname else discover_hostname(log)
    if not HOSTNAME_RE.match(host or ""):
        log.error(f"refusing to render: hostname {host!r} is not a plain lowercase DNS label; pass --hostname")
        return EXIT_NOTHING

    templates = pathlib.Path(jc.SHARE_DIR) / "templates"
    try:
        template = (templates / TEMPLATE_FILE).read_text()
    except OSError as exc:
        log.error(f"cannot read package template: {exc}")
        return EXIT_NOTHING
    rendered = render(template, host=host, port=args.port, token=token, scan_interval=args.scan_interval,
                      version=jc.addon_version(), generated_at=jc.now_iso())
    leftover = PLACEHOLDER_RE.search(rendered)
    if leftover:
        log.error(f"internal error: placeholder {leftover.group(0)} left in the rendered package; nothing written")
        return EXIT_NOTHING

    config_dir = args.out.parent
    if not config_dir.is_dir():
        log.error(f"{config_dir} does not exist (is the Home Assistant config directory mounted?); nothing written")
        return EXIT_NOTHING

    rc = EXIT_OK
    if not args.no_git_guard and not _advisory(lambda: ensure_git_exclude(config_dir, args.out, log), log):
        rc = EXIT_PARTIAL          # the entry must exist BEFORE the token file appears (M4)

    changed = material_change(args.out, rendered)
    try:
        atomic_write_text(args.out, rendered, 0o600)
    except OSError as exc:
        log.error(f"cannot write {args.out}: {exc}")
        return EXIT_NOTHING
    log.info(f"wrote {args.out} (host {host}, port {args.port}, "
             f"{'content changed' if changed else 'unchanged apart from the timestamp'})")

    try:
        install_blueprint(templates / BLUEPRINT_TEMPLATE_FILE, args.blueprint_out, log)
    except OSError as exc:
        log.warn(f"could not install the schedule blueprint at {args.blueprint_out}: {exc}")
        rc = EXIT_PARTIAL
    if not args.no_git_guard:
        _advisory(lambda: warn_if_tracked(config_dir, args.out, log), log)
    if changed and not args.no_reload:
        reload_core_integrations(log)
    return rc


def _advisory(step, log: Log) -> bool:
    """Run a best-effort git step; an unexpected exception is a warning, never a failed boot."""
    try:
        return step() is not False
    except Exception as exc:  # noqa: BLE001 - the git guard is advisory
        log.warn(f"git guard step skipped: {exc!r}")
        return False


if __name__ == "__main__":
    sys.exit(main())
