"""Boot the startup script (rootfs/usr/local/bin/claudecode-start) in a scratch directory.

The script's paths come from `CLAUDECODE_START_*` overrides; `claude`, `ttyd`, `npm` and
the stdin runner are fakes on PATH that record what they were asked to do. tmux is real,
on a private socket (TMUX_TMPDIR). Checked: the persistent dir and settings.json the boot
leaves behind, the MCP registrations and the ttyd command line, an unresponsive `claude`,
the update-then-rollback path, a baked claude that only install-claude.sh can replace, the EXIT trap
that still starts a terminal when a step dies, and the stop path (SIGTERM reaches Claude under tmux, exit 143).
"""
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from testlib import BIN_DIR, SUPERVISOR_TOKEN, wait_for

START = BIN_DIR / "claudecode-start"
VERSION = "2.1.300"
BROKEN_VERSION = "9.9.9"   # the fake npm installs it, then the fake claude stops answering

FAKES = {
    "claude": f"""#!/bin/bash
echo "$*" >> "$FAKE_DIR/claude.log"
case "$1" in
  --version)
    [ -e "$FAKE_DIR/claude-slow" ] && sleep 2
    [ -e "$FAKE_DIR/claude-broken" ] && exit 132
    echo "{VERSION} (Claude Code)" ;;
  remote-control)
    echo $$ > "$FAKE_DIR/rc.pid"
    trap 'echo TERM > "$FAKE_DIR/rc.signal"; exit 143' TERM
    while :; do sleep 0.2; done ;;
esac
""",
    "npm": f"""#!/bin/bash
echo "$*" >> "$FAKE_DIR/npm.log"
case "$1" in
  view) echo "$FAKE_LATEST" ;;
  install)
    case "$*" in
      *@{BROKEN_VERSION}*) touch "$FAKE_DIR/claude-broken" ;;
      *@{VERSION}*) rm -f "$FAKE_DIR/claude-broken" ;;
    esac ;;
esac
""",
    "ttyd": """#!/bin/bash
printf '%s\\n' "$@" > "$FAKE_DIR/ttyd.args"
env > "$FAKE_DIR/ttyd.env"
echo "$PWD" > "$FAKE_DIR/ttyd.cwd"
if [ -e "$FAKE_DIR/ttyd-stay" ]; then
  trap 'exit 0' TERM
  while :; do sleep 0.2; done
fi
""",
    "stdin-runner.sh": """#!/bin/bash
echo $$ > "$FAKE_DIR/stdin.pid"
trap 'echo TERM > "$FAKE_DIR/stdin.signal"; exit 0' TERM
while :; do sleep 0.2; done
""",
    "claude-auto-launch": "#!/bin/bash\nexit 0\n",
    # The real one searches npm for the newest release that runs here; the fake
    # "finds" one only when the test says so.
    "install-claude.sh": """#!/bin/bash
echo "called" >> "$FAKE_DIR/install.log"
[ -e "$FAKE_DIR/install-fixes" ] || exit 1   # like the real one: non-zero when nothing runs
rm -f "$FAKE_DIR/claude-broken"
""",
}

HA_TOOLS = ["mcp__homeassistant__get_version", "mcp__homeassistant__get_entity", "Read(/homeassistant/**)",
            "Glob(/config/**)", "Grep(/homeassistant/**)"]


class Boot:
    """A scratch add-on: fakes on PATH, options.json, the work and home dirs, the script's output."""

    def __init__(self, options, latest=VERSION):
        self.root = Path(tempfile.mkdtemp(prefix="claudecode-start-"))
        self.bin = self.root / "bin"
        self.fake = self.root / "fake"
        self.work = self.root / "homeassistant"
        self.home = self.root / "root"
        for d in (self.bin, self.fake, self.work, self.home, self.root / "tmux"):
            d.mkdir()
        for name, body in FAKES.items():
            p = self.bin / name
            p.write_text(body)
            p.chmod(0o755)
        self.options = self.root / "options.json"
        self.options.write_text(json.dumps(options))
        self.addon_md = self.root / "CLAUDE.addon.md"
        self.addon_md.write_text("# add-on instructions\n")
        self.log = self.root / "boot.log"
        self.persist = self.work / ".claudecode"
        self.env = dict(os.environ,
                        PATH=f"{self.bin}:{os.environ['PATH']}",
                        TMUX_TMPDIR=str(self.root / "tmux"),
                        FAKE_DIR=str(self.fake), FAKE_LATEST=latest,
                        SUPERVISOR_TOKEN=SUPERVISOR_TOKEN,
                        CLAUDECODE_START_OPTIONS=str(self.options),
                        CLAUDECODE_START_WORK_DIR=str(self.work),
                        CLAUDECODE_START_HOME=str(self.home),
                        CLAUDECODE_START_ADDON_MD=str(self.addon_md),
                        CLAUDECODE_START_AUTO_LAUNCH=str(self.bin / "claude-auto-launch"),
                        CLAUDECODE_START_STDIN_RUNNER=str(self.bin / "stdin-runner.sh"),
                        CLAUDECODE_START_INSTALL_CLAUDE=str(self.bin / "install-claude.sh"))
        self.env.pop("TMUX", None)
        self.proc = None

    def seed_settings(self, text):
        self.persist.mkdir(exist_ok=True)
        (self.persist / "settings.json").write_text(text)

    def start(self):
        self.logf = self.log.open("w")
        self.proc = subprocess.Popen(["bash", str(START)], env=self.env, cwd=str(self.root),
                                     stdin=subprocess.PIPE, stdout=self.logf, stderr=subprocess.STDOUT)
        return self

    def run(self, timeout=30):
        self.start()
        self.proc.wait(timeout=timeout)
        return self.proc.returncode

    def output(self):
        return self.log.read_text() if self.log.exists() else ""

    def logged(self, text):
        return wait_for(lambda: text in self.output(), timeout=20)

    def settings(self):
        return json.loads((self.persist / "settings.json").read_text())

    def fake_log(self, name):
        p = self.fake / name
        return p.read_text().splitlines() if p.exists() else []

    def ttyd_env(self):
        return dict(line.split("=", 1) for line in self.fake_log("ttyd.env") if "=" in line)

    def tmux(self, *args):
        return subprocess.run(["tmux", *args], env=self.env, capture_output=True, text=True, check=False)

    def close(self):
        if self.proc:
            if self.proc.poll() is None:
                self.proc.kill()
                self.proc.wait()
            self.proc.stdin.close()
            self.logf.close()
        self.tmux("kill-server")
        for pidfile in ("stdin.pid", "rc.pid"):
            try:
                os.kill(int((self.fake / pidfile).read_text()), signal.SIGKILL)
            except (OSError, ValueError):
                pass
        shutil.rmtree(self.root, ignore_errors=True)


class TestBoot(unittest.TestCase):
    def setUp(self):
        self.boots = []

    def tearDown(self):
        for b in self.boots:
            b.close()

    def boot(self, options, **kw):
        b = Boot(options, **kw)
        self.boots.append(b)
        return b

    def test_full_boot(self):
        """Everything on: the persistent dir, settings.json, MCP registrations and the ttyd command."""
        b = self.boot({"terminal_font_size": 16, "terminal_theme": "light", "enable_playwright_mcp": True,
                       "playwright_cdp_host": "pw.local", "enable_remote_control": True,
                       "remote_control_session_prefix": "Haus", "auto_launch_claude": "continue"})
        b.seed_settings(json.dumps({"permissions": {"allow": ["Bash(ls)"]}, "other": 1,
                                    "mcpServers": {"homeassistant": {"env": {"HASS_TOKEN": "old-token"}}}}))
        self.assertEqual(b.run(), 0, b.output())
        out = b.output()
        self.assertIn("step: configure MCP servers", out)
        self.assertIn("Startup finished in", out)
        self.assertIn("Removed a Supervisor token", out)
        self.assertNotIn("[WARN]", out)
        self.assertNotIn("[ERROR]", out)

        # persistent dir wired under $HOME
        self.assertEqual(os.readlink(b.home / ".claude"), str(b.persist))
        self.assertEqual(os.readlink(b.home / ".config" / "claude-code"), str(b.persist / "config"))
        self.assertEqual(os.readlink(b.home / ".claude.json"), str(b.persist / ".claude.json"))
        self.assertEqual((b.persist / "CLAUDE.md").read_text(), b.addon_md.read_text())
        self.assertTrue((b.persist / "CLAUDE.user.md").exists())

        s = b.settings()
        self.assertEqual(s["other"], 1)
        self.assertNotIn("HASS_TOKEN", json.dumps(s))
        self.assertTrue(s["remoteControlAtStartup"])
        self.assertIn("Bash(ls)", s["permissions"]["allow"])
        for tool in HA_TOOLS:
            self.assertIn(tool, s["permissions"]["allow"])
        self.assertFalse((b.persist / "settings.json.tmp").exists())

        claude = b.fake_log("claude.log")
        self.assertIn("mcp remove homeassistant -s user", claude)
        self.assertIn("mcp remove playwright -s user", claude)
        self.assertIn('mcp add-json homeassistant {"command":"hass-mcp"} -s user', claude)
        pw = [l for l in claude if l.startswith("mcp add-json playwright ")]
        self.assertEqual(len(pw), 1, claude)
        self.assertEqual(json.loads(pw[0].split(" ", 3)[3].rsplit(" -s user", 1)[0]),
                         {"command": "/usr/local/bin/playwright-mcp-launch", "env": {"PLAYWRIGHT_CDP_HOST": "pw.local"}})
        self.assertEqual(b.fake_log("npm.log"), [])   # auto_update_claude off

        args = b.fake_log("ttyd.args")
        self.assertIn("fontSize=16", args)
        theme = json.loads([a for a in args if a.startswith("theme=")][0][len("theme="):])
        self.assertEqual(theme["background"], "#eff1f5")
        self.assertEqual(args[-6:], ["tmux", "new-session", "-A", "-s", "claude", str(b.bin / "claude-auto-launch")])
        self.assertEqual(b.fake_log("ttyd.cwd"), [str(b.work)])
        env = b.ttyd_env()
        self.assertEqual(env["AUTO_LAUNCH_ARGS"], "--continue --remote-control")
        self.assertEqual(env["CLAUDE_FLAGS"], "--remote-control")
        self.assertEqual(env["CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX"], "Haus")
        self.assertEqual(env["HA_TOKEN"], SUPERVISOR_TOKEN)
        self.assertEqual(env["HA_URL"], "http://supervisor/core")
        self.assertTrue((b.fake / "stdin.pid").exists())

    def test_baked_cli_that_does_not_run_is_reinstalled(self):
        """The image is prebuilt on CI (which has AVX); a baked claude that fails here is replaced by
        install-claude.sh before anything else needs it, and the boot carries on as a normal one."""
        b = self.boot({"enable_remote_control": True})
        (b.fake / "claude-broken").touch()
        (b.fake / "install-fixes").touch()
        self.assertEqual(b.run(), 0, b.output())
        out = b.output()
        self.assertIn("step: baked Claude Code smoke test", out)
        self.assertIn("does not run on this host", out)
        self.assertEqual(b.fake_log("install.log"), ["called"])
        self.assertIn(f"claude CLI responds: {VERSION}", out)
        self.assertNotIn("[ERROR]", out)
        self.assertIn('mcp add-json homeassistant {"command":"hass-mcp"} -s user', b.fake_log("claude.log"))
        self.assertTrue(b.settings()["remoteControlAtStartup"])

    def test_claude_not_responding(self):
        """A claude that crashes: MCP setup and auto-launch are skipped with the reason, the terminal still starts.
        A settings.json that does not parse is moved aside, not fed to every jq write."""
        b = self.boot({"auto_launch_claude": "new"})
        b.seed_settings("not json {")
        (b.fake / "claude-broken").touch()
        self.assertEqual(b.run(), 0, b.output())
        out = b.output()
        self.assertEqual(b.fake_log("install.log"), ["called"])   # the re-install was tried first
        self.assertIn("No working Claude Code release found", out)
        self.assertIn("SIGILL (exit 132)", out)
        self.assertIn("MCP is enabled but was skipped", out)
        self.assertIn("settings.json is not usable", out)
        self.assertEqual(b.settings(), {})
        self.assertTrue(list(b.persist.glob("settings.json.corrupt.*")))
        self.assertFalse([l for l in b.fake_log("claude.log") if l.startswith("mcp ")])
        self.assertEqual(b.fake_log("ttyd.args")[-5:], ["tmux", "new-session", "-A", "-s", "claude"])
        self.assertEqual(b.ttyd_env()["AUTO_LAUNCH_ARGS"], "")

    def test_no_tmux_plain_shell(self):
        b = self.boot({"session_persistence": False, "enable_mcp": False})
        self.assertEqual(b.run(), 0, b.output())
        self.assertIn("MCP disabled", b.output())
        self.assertEqual(b.fake_log("ttyd.args")[-2:], ["bash", "--login"])
        self.assertNotIn("remoteControlAtStartup", b.settings())
        self.assertNotIn("CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX", b.ttyd_env())

    def test_update_rollback(self):
        """auto_update_claude installs the newest release; when that one does not run, the previous one comes back."""
        b = self.boot({"auto_update_claude": True}, latest=BROKEN_VERSION)
        self.assertEqual(b.run(), 0, b.output())
        self.assertEqual(b.fake_log("npm.log"), [
            "view @anthropic-ai/claude-code version",
            f"install -g @anthropic-ai/claude-code@{BROKEN_VERSION}",
            f"install -g @anthropic-ai/claude-code@{VERSION}",
        ])
        out = b.output()
        self.assertIn(f"Updating Claude Code {VERSION} -> {BROKEN_VERSION}", out)
        self.assertIn(f"does not run here; rolling back to {VERSION}", out)
        self.assertIn(f"claude CLI responds: {VERSION}", out)

    def test_update_already_current(self):
        b = self.boot({"auto_update_claude": True}, latest=VERSION)
        self.assertEqual(b.run(), 0, b.output())
        self.assertEqual(b.fake_log("npm.log"), ["view @anthropic-ai/claude-code version"])
        self.assertIn(f"Claude Code {VERSION} (already current)", b.output())

    def test_step_dies_terminal_still_starts(self):
        """The EXIT trap: a fatal signal during a step (bash runs the EXIT trap on one) still ends in ttyd."""
        b = self.boot({})
        (b.fake / "claude-slow").touch()
        b.start()
        self.assertTrue(b.logged("step: probe claude CLI"), b.output())
        b.proc.send_signal(signal.SIGUSR1)
        b.proc.wait(timeout=20)
        out = b.output()
        self.assertIn("Startup aborted with exit", out)
        self.assertIn("during step 'probe claude CLI'", out)
        self.assertTrue((b.fake / "ttyd.args").exists(), out)

    def test_stop_reaches_claude(self):
        """SIGTERM: the Remote Control server's claude (under tmux) and the stdin runner get TERM, tmux and ttyd
        are torn down, the script exits 143 - instead of ttyd being the one signalled and the rest SIGKILLed."""
        b = self.boot({"enable_remote_control": True, "remote_control_server": True,
                       "remote_control_permission_mode": "acceptEdits"})
        (b.fake / "ttyd-stay").touch()
        b.start()
        self.assertTrue(b.logged("Starting web terminal"), b.output())
        self.assertIn("Remote Control server started (permission mode: acceptEdits)", b.output())
        self.assertTrue(wait_for(lambda: (b.fake / "rc.pid").exists()), b.output())
        self.assertIn("--permission-mode acceptEdits", "\n".join(b.fake_log("claude.log")))
        self.assertEqual(b.tmux("list-sessions", "-F", "#{session_name}").stdout.split(), ["rc"])
        t0 = time.monotonic()
        b.proc.send_signal(signal.SIGTERM)
        self.assertEqual(b.proc.wait(timeout=20), 143, b.output())
        self.assertLess(time.monotonic() - t0, 10)
        self.assertEqual(b.fake_log("rc.signal"), ["TERM"])
        self.assertEqual(b.fake_log("stdin.signal"), ["TERM"])
        self.assertNotEqual(b.tmux("has-session").returncode, 0, "tmux server still running")
        self.assertIn("Stop requested", b.output())


if __name__ == "__main__":
    unittest.main()
