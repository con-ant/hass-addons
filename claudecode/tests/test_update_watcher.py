"""claude-update-watcher: idle sessions move onto another release, and only then.

The watcher runs for real against stand-ins:

  claude   a small C program under <tmp>/lib/node_modules/@anthropic-ai/claude-code/bin
           (the watcher tells a Claude by that path in /proc/<pid>/exe). `--version`
           prints the release it was compiled with; anything else waits for a signal.
           "Installing" copies a build over it, which gives the path a new inode and
           leaves running processes on the deleted file - what `npm install -g` does,
           also when the release is the one already installed.
  tmux     prints the panes file, so a test says what each pane's activity times are.
           @BUSY@ in it becomes a number that grows with every call: a pane printing
           without pause. Its call counter is how a test waits for whole polls.
  date     `date +%s` answers from a file once a test has set the clock, so an hour
           passes in a step.
  claude-auto-launch   relaunches the pane's claude whenever it exits, and logs it.
"""
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

from testlib import BIN_DIR, wait_for

WATCHER = BIN_DIR / "claude-update-watcher"

FAKE_CLAUDE_C = r"""
#include <stdio.h>
#include <string.h>
#include <unistd.h>
int main(int argc, char **argv) {
  if (argc > 1 && !strcmp(argv[1], "--version")) { puts(VERSION " (Claude Code)"); return 0; }
  for (;;) pause();
}
"""
FAKE_TMUX = """#!/bin/bash
n=$(( $(cat "$FAKE_TMUX_CALLS" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$FAKE_TMUX_CALLS.tmp" && mv -f "$FAKE_TMUX_CALLS.tmp" "$FAKE_TMUX_CALLS"
sed "s/@BUSY@/$n/" "$FAKE_TMUX_PANES" 2>/dev/null
"""
FAKE_DATE = """#!/bin/bash
if [ "$1" = "+%s" ] && [ -s "$FAKE_NOW" ]; then cat "$FAKE_NOW"; else exec {date} "$@"; fi
"""
FAKE_LAUNCHER = """#!/bin/bash
while :; do
  claude run &
  wait $!
  echo "$?" >> "$FAKE_LAUNCH_LOG"
done
"""
OLD, CURRENT, NEW = "2.1.320", "2.1.323", "2.1.324"
PRERELEASE, NO_VERSION = "2.2.0-rc.1", "garbage"     # what `--version` may also answer with


@unittest.skipUnless(shutil.which("cc") and shutil.which("pgrep"), "needs a C compiler and pgrep")
class TestUpdateWatcher(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.builds = tempfile.mkdtemp(prefix="watcher-builds-")
        cls.addClassCleanup(shutil.rmtree, cls.builds, ignore_errors=True)
        src = os.path.join(cls.builds, "claude.c")
        with open(src, "w", encoding="utf-8") as f:
            f.write(FAKE_CLAUDE_C)
        for v in (OLD, CURRENT, NEW, PRERELEASE, NO_VERSION):
            subprocess.run(["cc", "-o", os.path.join(cls.builds, v), f'-DVERSION="{v}"', src], check=True)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="watcher-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        t = self.tmp
        self.exe = os.path.join(t, "lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe")
        os.makedirs(os.path.dirname(self.exe))
        fakebin = os.path.join(t, "bin")
        os.mkdir(fakebin)
        os.symlink(self.exe, os.path.join(fakebin, "claude"))
        self.launcher = os.path.join(fakebin, "claude-auto-launch")
        for name, body in (("tmux", FAKE_TMUX), ("date", FAKE_DATE.format(date=shutil.which("date"))),
                           ("claude-auto-launch", FAKE_LAUNCHER)):
            with open(os.path.join(fakebin, name), "w", encoding="utf-8") as f:
                f.write(body)
            os.chmod(os.path.join(fakebin, name), 0o755)
        self.panes_file = os.path.join(t, "panes.txt")
        self.calls = os.path.join(t, "tmux-calls")
        self.clock = os.path.join(t, "now")
        self.launch_log = os.path.join(t, "launches")
        self.log = os.path.join(t, "watcher.log")
        self.pane_state = os.path.join(t, "run/panes")
        self.rc_pidfile = os.path.join(t, "run/rc-server.pid")
        self.rc_mark = os.path.join(t, "run/rc-restart")
        self.cli_version = os.path.join(t, "data/cli-version")
        os.makedirs(os.path.join(self.pane_state, "0"))
        self.put(self.panes_file, "")
        self.env = dict(os.environ)
        self.env.update({
            "PATH": fakebin + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_TMUX_PANES": self.panes_file, "FAKE_TMUX_CALLS": self.calls,
            "FAKE_NOW": self.clock, "FAKE_LAUNCH_LOG": self.launch_log,
            "CLAUDE_UPDATE_POLL_SECS": "0.05",
            "CLAUDE_PANE_STATE_DIR": self.pane_state,
            "CLAUDE_PROC_STATE_DIR": os.path.join(t, "run/procs"),
            "CLAUDE_RC_SERVER_PIDFILE": self.rc_pidfile, "CLAUDE_RC_RESTART_MARK": self.rc_mark,
            "CLAUDE_CLI_VERSION_FILE": self.cli_version,
            "CLAUDE_CLI_BAD_FILE": os.path.join(t, "data/cli-bad"),
            "CLAUDE_CLI_VERIFIED_FILE": os.path.join(t, "run/cli-verified"),
        })
        for k in ("CLAUDE_UPDATE_IDLE_SECS", "CLAUDE_UPDATE_STALE_TURN_SECS", "CLAUDE_UPDATE_LIVE_POLLS"):
            self.env.pop(k, None)
        self.t0 = int(time.time())

    # ---- stand-ins -------------------------------------------------------------------------
    @staticmethod
    def put(path, text):
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(path + ".tmp", path)

    def install(self, version):
        """What npm does: a new file at the same path; processes keep the old, deleted one."""
        shutil.copy(os.path.join(self.builds, version), self.exe + ".new")
        os.replace(self.exe + ".new", self.exe)

    def start(self, argv, **kw):
        p = subprocess.Popen(argv, env=self.env, start_new_session=True, **kw)
        self.addCleanup(p.wait)
        self.addCleanup(lambda: self.killpg(p.pid))
        return p

    @staticmethod
    def killpg(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def start_pane(self):
        """A terminal pane: claude-auto-launch as its root, claude as the child. Returns the root."""
        root = self.start(["bash", self.launcher])
        self.assertTrue(wait_for(lambda: self.pane_claude(root)), "the pane's claude did not start")
        return root

    @staticmethod
    def pane_claude(root):
        out = subprocess.run(["pgrep", "-P", str(root.pid)], capture_output=True, text=True, check=False).stdout.split()
        return int(out[0]) if out else None

    def start_claude(self):
        """A Claude nobody supervises (or the Remote Control server, once its pid is in the pidfile)."""
        return self.start([os.path.join(self.tmp, "bin/claude"), "run"])

    def start_watcher(self, **env):
        out = open(self.log, "w", encoding="utf-8")   # pylint: disable=consider-using-with
        self.addCleanup(out.close)
        self.env.update(env)
        return self.start(["bash", str(WATCHER)], stdout=out, stderr=subprocess.STDOUT)

    def pane(self, root, output, typed=0):
        """The one pane tmux reports: when it last printed (epoch, or "@BUSY@") and was typed in."""
        self.put(self.panes_file, f"%0 {root.pid} {output} {typed}\n")

    def set_clock(self, epoch):
        self.put(self.clock, f"{epoch}\n")

    def stamp(self, name, epoch):
        self.put(os.path.join(self.pane_state, "0", name), f"{epoch}\n")

    # ---- observations ----------------------------------------------------------------------
    def polls(self, n=3):
        """Let the watcher finish `n` more rounds (the first may have begun before the last change)."""
        def count():
            with open(self.calls, encoding="utf-8") as f:
                return int(f.read() or 0)
        self.assertTrue(wait_for(lambda: os.path.exists(self.calls)), self.logged())
        target = count() + n
        self.assertTrue(wait_for(lambda: count() >= target), self.logged())

    def logged(self):
        with open(self.log, encoding="utf-8") as f:
            return f.read()

    def wait_log(self, text, count=1):
        self.assertTrue(wait_for(lambda: self.logged().count(text) >= count), f"{text!r} not in:\n{self.logged()}")

    def restarts(self):
        try:
            with open(self.launch_log, encoding="utf-8") as f:
                return len(f.read().split())
        except FileNotFoundError:
            return 0

    def assert_left_alone(self, root, pid):
        self.polls()
        self.assertEqual(self.restarts(), 0, self.logged())
        self.assertEqual(self.pane_claude(root), pid)
        self.assertNotIn("Restarting", self.logged())

    # ---- the release decides, not the file --------------------------------------------------
    def test_same_release_installed_again_restarts_nothing(self):
        self.install(CURRENT)
        root = self.start_pane()
        pid = self.pane_claude(root)
        self.pane(root, self.t0 - 2000)             # idle: quiet for longer than IDLE_SECS, no turn
        self.start_watcher()
        self.wait_log(f"Claude Code {CURRENT} is installed")
        for n in (1, 2):
            self.install(CURRENT)                   # the 30-minute reinstall
            self.wait_log(f"Claude Code {CURRENT} was installed again", count=n)
            self.assert_left_alone(root, pid)
        # Every Claude here is on that release: none of them is named. (Not "no [WARN]": the watcher
        # looks at every Claude on the machine, and a developer's own may be on another release.)
        self.assertNotIn(f"pid {pid} on", self.logged())
        with open(self.cli_version, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), CURRENT)

    def test_another_release_restarts_the_idle_session_once(self):
        self.install(CURRENT)
        root = self.start_pane()
        pid = self.pane_claude(root)
        self.pane(root, self.t0 - 2000)
        self.start_watcher()
        self.wait_log(f"Claude Code {CURRENT} is installed")
        self.install(NEW)
        self.wait_log(f"Restarting the Claude session in pane %0 (pid {pid}")
        self.assertTrue(wait_for(lambda: self.restarts() == 1 and self.pane_claude(root) not in (None, pid)))
        self.assertTrue(os.path.exists(os.path.join(self.pane_state, "0", "restart-for-update")))
        self.polls()
        self.assertEqual(self.restarts(), 1, self.logged())     # the relaunched one is on NEW: left alone

    def test_binary_the_watcher_never_checked_is_asked_for_its_release(self):
        # Installed again before the watcher's first look: the session's file is gone and unknown.
        self.install(CURRENT)
        root = self.start_pane()
        pid = self.pane_claude(root)
        self.install(CURRENT)
        self.pane(root, self.t0 - 2000)
        self.start_watcher()
        self.wait_log(f"Claude Code {CURRENT} is installed")
        self.assert_left_alone(root, pid)

    def test_unchecked_binary_on_an_older_release_is_restarted(self):
        self.install(OLD)
        root = self.start_pane()
        pid = self.pane_claude(root)
        self.install(CURRENT)
        self.pane(root, self.t0 - 2000)
        self.start_watcher()
        self.wait_log(f"Restarting the Claude session in pane %0 (pid {pid}")
        self.assertTrue(wait_for(lambda: self.restarts() == 1))

    def test_remote_control_server_follows_the_release_too(self):
        self.install(CURRENT)
        server = self.start_claude()
        self.put(self.rc_pidfile, f"{server.pid}\n")
        self.start_watcher(CLAUDE_UPDATE_IDLE_SECS="0")         # no sessions under it: idle at once
        self.wait_log(f"Claude Code {CURRENT} is installed")
        self.install(CURRENT)
        self.wait_log(f"Claude Code {CURRENT} was installed again")
        self.polls()
        self.assertIsNone(server.poll(), self.logged())
        self.assertFalse(os.path.exists(self.rc_mark))
        self.install(NEW)
        self.wait_log(f"Restarting the Remote Control server (pid {server.pid})")
        self.assertEqual(server.wait(timeout=10), -signal.SIGTERM)
        with open(self.rc_mark, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), str(server.pid))

    def test_the_claude_that_keeps_reinstalling_is_named_once(self):
        self.install(OLD)
        stray = self.start_claude()                 # started by hand: nothing restarts it
        self.assertTrue(wait_for(lambda: os.path.exists(f"/proc/{stray.pid}/exe")))
        self.install(CURRENT)
        self.start_watcher()
        self.wait_log(f"Claude Code {CURRENT} is installed")
        self.install(CURRENT)
        self.wait_log(f"pid {stray.pid} on {OLD} since")
        self.assertRegex(self.logged(), rf"pid {stray.pid} on {OLD} since [^;]*\(started by hand: nothing restarts it")
        self.install(CURRENT)
        self.wait_log(f"Claude Code {CURRENT} was installed again", count=2)
        self.polls()
        self.assertEqual(self.logged().count(f"pid {stray.pid} on {OLD}"), 1, self.logged())
        self.assertIsNone(stray.poll())

    def test_a_supervised_session_that_is_behind_is_named_as_one_the_addon_restarts(self):
        root, pid = self.old_pane_with_turn_in_flight(self.t0 - 1000)   # behind, and not idle yet
        self.install(CURRENT)
        self.wait_log(f"pid {pid} on {OLD} since")
        self.assertRegex(self.logged(), rf"pid {pid} on {OLD} since [^;]*\(terminal session, restarted once idle\)")
        self.assertEqual(self.pane_claude(root), pid)

    def test_a_prerelease_is_a_release_like_any_other(self):
        self.install(PRERELEASE)
        root = self.start_pane()
        pid = self.pane_claude(root)
        self.install(PRERELEASE)                    # the session's file is one the watcher never checked
        self.pane(root, self.t0 - 2000)
        self.start_watcher()
        self.wait_log(f"Claude Code {PRERELEASE} is installed")
        self.assert_left_alone(root, pid)

    def test_an_answer_without_a_version_falls_back_to_the_file(self):
        # Nothing to compare: the same file is left alone, another file is an update, as before con.39.
        self.install(NO_VERSION)
        root = self.start_pane()
        pid = self.pane_claude(root)
        self.pane(root, self.t0 - 2000)
        self.start_watcher()
        self.wait_log("version not recognised: garbage (Claude Code)")
        self.assert_left_alone(root, pid)
        self.install(NO_VERSION)
        self.wait_log(f"Restarting the Claude session in pane %0 (pid {pid}")
        self.assertNotIn("was installed again", self.logged())

    # ---- a turn in flight ------------------------------------------------------------------
    def old_pane_with_turn_in_flight(self, output):
        """A pane on OLD while CURRENT is installed, its last prompt (long ago) never answered."""
        self.install(OLD)
        root = self.start_pane()
        self.install(CURRENT)
        self.stamp("last-prompt", self.t0 - 50000)
        self.stamp("last-stop", self.t0 - 60000)
        self.pane(root, output)
        self.set_clock(self.t0)
        self.start_watcher()
        self.wait_log(f"Claude Code {CURRENT} is installed")
        return root, self.pane_claude(root)

    def test_updater_output_every_30_minutes_does_not_keep_an_interrupted_turn_alive(self):
        # The session that is behind prints "Auto-updating" at every reinstall. Each print is
        # under an hour after the last, which used to keep the turn "in flight" for good.
        root, pid = self.old_pane_with_turn_in_flight(self.t0 - 1000)
        self.assert_left_alone(root, pid)           # the watcher has known the pane for no time at all
        self.pane(root, self.t0 + 800)
        self.set_clock(self.t0 + 1800)              # 30 minutes on, printed 1000 s ago
        self.assert_left_alone(root, pid)
        self.pane(root, self.t0 + 2600)
        self.set_clock(self.t0 + 3600)              # an hour without a sign of life
        self.wait_log(f"Restarting the Claude session in pane %0 (pid {pid}")

    def test_output_poll_after_poll_is_a_turn_at_work(self):
        root, pid = self.old_pane_with_turn_in_flight("@BUSY@")
        self.set_clock(self.t0 + 3500)
        self.polls(8)                               # still printing, nearly an hour in (LIVE_POLLS is 5)
        self.pane(root, self.t0 + 3500)             # then silence
        self.polls()
        self.set_clock(self.t0 + 5000)              # 25 minutes of it: quiet, but the turn worked within the hour
        self.assert_left_alone(root, pid)
        self.set_clock(self.t0 + 7200)
        self.wait_log(f"Restarting the Claude session in pane %0 (pid {pid}")

    def test_a_prompt_or_a_key_is_a_sign_of_life(self):
        root, pid = self.old_pane_with_turn_in_flight(self.t0 - 1000)
        self.stamp("last-prompt", self.t0 + 3000)
        self.set_clock(self.t0 + 4000)              # prompted 1000 s ago, nothing printed since
        self.assert_left_alone(root, pid)
        self.pane(root, self.t0 - 1000, typed=self.t0 + 6000)
        self.set_clock(self.t0 + 7000)              # the prompt is over an hour old; a key was typed 1000 s ago
        self.assert_left_alone(root, pid)
        self.set_clock(self.t0 + 9600)
        self.wait_log(f"Restarting the Claude session in pane %0 (pid {pid}")

    def test_a_finished_turn_needs_only_the_idle_time(self):
        self.install(OLD)
        root = self.start_pane()
        pid = self.pane_claude(root)
        self.install(CURRENT)
        self.stamp("last-prompt", self.t0 - 500)
        self.stamp("last-stop", self.t0 - 400)
        self.pane(root, self.t0 - 400)
        self.set_clock(self.t0)
        self.start_watcher()
        self.wait_log(f"Claude Code {CURRENT} is installed")
        self.assert_left_alone(root, pid)           # quiet for 400 s only
        self.set_clock(self.t0 + 500)
        self.wait_log(f"Restarting the Claude session in pane %0 (pid {pid}")


if __name__ == "__main__":
    unittest.main()
