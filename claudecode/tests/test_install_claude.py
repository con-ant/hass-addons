"""install-claude.sh: install the asked-for release, or the newest earlier one that runs.

Fake `npm` and `claude` on PATH: npm "installs" by recording a version, and the fake
claude answers `--version` only up to FAKE_CLAUDE_MAX_OK, hanging above it the way a
Bun-compiled release hangs on a CPU without AVX. SMOKE_TIMEOUT=1 keeps each hang short.
"""
import json
import os
import shutil
import tempfile
import unittest

from testlib import ADDON, run_cli

SCRIPT = ADDON / "install-claude.sh"

FAKE_NPM = """#!/bin/bash
echo "$*" >> "$FAKE_NPM_LOG"
case "$1 $3" in
  "view versions") cat "$FAKE_NPM_VERSIONS" ;;                      # npm view <pkg> versions --json
  "view version")  # npm view <pkg>@<tag-or-version> version
    spec="${2##*@}"
    jq -r --arg s "$spec" '.[$s] // empty' "$FAKE_NPM_TAGS"
    jq -r --arg s "$spec" 'map(select(. == $s))[0] // empty' "$FAKE_NPM_VERSIONS" ;;
  "i "*|"install "*)
    for a in "$@"; do case "$a" in @anthropic-ai/claude-code@*) echo "${a##*@}" > "$FAKE_NPM_INSTALLED" ;; esac; done ;;
esac
"""
FAKE_CLAUDE = """#!/bin/bash
v=$(cat "$FAKE_NPM_INSTALLED" 2>/dev/null)
newest=$(printf '%s\\n%s\\n' "$v" "$FAKE_CLAUDE_MAX_OK" | sort -V | tail -1)
if [ "$newest" = "$FAKE_CLAUDE_MAX_OK" ]; then echo "$v (Claude Code)"; else sleep 30; fi
"""
VERSIONS = ["1.0.80", "2.0.0", "2.1.100", "2.1.112", "2.1.113", "2.1.200", "2.1.300", "2.1.400", "2.2.0-beta.1"]


class TestInstallClaude(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="install-claude-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        fakebin = os.path.join(self.tmp, "bin")
        os.mkdir(fakebin)
        for name, body in (("npm", FAKE_NPM), ("claude", FAKE_CLAUDE)):
            with open(os.path.join(fakebin, name), "w", encoding="utf-8") as f:
                f.write(body)
            os.chmod(os.path.join(fakebin, name), 0o755)
        self.versions = os.path.join(self.tmp, "versions.json")
        self.tags = os.path.join(self.tmp, "tags.json")
        self.manifest = os.path.join(self.tmp, "manifest.json")      # absent unless a test writes it
        self.version_file = os.path.join(self.tmp, "claude-code-version")
        self.installed = os.path.join(self.tmp, "installed")
        self.npm_log = os.path.join(self.tmp, "npm.log")
        with open(self.versions, "w", encoding="utf-8") as f:
            json.dump(VERSIONS, f)
        with open(self.tags, "w", encoding="utf-8") as f:
            json.dump({"stable": "2.1.300", "latest": "2.1.400"}, f)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_CODE_")}  # a dev box may set CLAUDE_CODE_VERSION
        self.env.update({
            "PATH": fakebin + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_NPM_VERSIONS": self.versions, "FAKE_NPM_TAGS": self.tags,
            "FAKE_NPM_INSTALLED": self.installed, "FAKE_NPM_LOG": self.npm_log,
            "FAKE_CLAUDE_MAX_OK": "9.9.9",
            "CLAUDE_CODE_MANIFEST": self.manifest, "CLAUDE_CODE_VERSION_FILE": self.version_file,
            "SMOKE_TIMEOUT": "1",
        })

    def run_install(self, **env):
        self.env.update(env)
        return run_cli([SCRIPT], self.env, timeout=120)

    def installs(self):
        with open(self.npm_log, encoding="utf-8") as f:
            return [l.split("@anthropic-ai/claude-code@")[1] for l in f.read().splitlines() if "claude-code@" in l
                    and l.startswith(("i ", "install "))]

    def recorded(self):
        with open(self.version_file, encoding="utf-8") as f:
            return f.read().strip()

    def test_manifest_names_the_version_and_it_runs(self):
        with open(self.manifest, "w", encoding="utf-8") as f:
            json.dump({"name": "@anthropic-ai/claude-code", "version": "2.1.300"}, f)
        rc, out, err = self.run_install()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.installs(), ["2.1.300"])
        self.assertEqual(self.recorded(), "2.1.300")
        self.assertIn("claude-code 2.1.300 (stable, verified)", out)

    def test_without_a_manifest_the_tag_is_resolved_with_npm_view(self):
        rc, out, _ = self.run_install(CLAUDE_CODE_VERSION="latest")
        self.assertEqual(rc, 0)
        self.assertEqual(self.installs(), ["2.1.400"])
        self.assertEqual(self.recorded(), "2.1.400")
        self.assertIn("(latest, verified)", out)

    def test_exact_version_spec(self):
        rc, _, _ = self.run_install(CLAUDE_CODE_VERSION="2.1.112")
        self.assertEqual(rc, 0)
        self.assertEqual(self.recorded(), "2.1.112")

    def test_falls_back_to_the_newest_earlier_release_that_runs(self):
        rc, out, _ = self.run_install(FAKE_CLAUDE_MAX_OK="2.1.112")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.recorded(), "2.1.112")
        self.assertIn("fails its smoke test", out)
        self.assertIn("claude-code 2.1.112 (newest working; 2.1.300 from stable does not run here)", out)
        tried = self.installs()
        self.assertEqual(tried[0], "2.1.300")          # the channel's release first
        self.assertEqual(tried[-1], "2.1.112")         # the winner is installed last
        self.assertNotIn("2.1.400", tried)             # never ahead of the channel
        self.assertNotIn("1.0.80", tried)              # 2.x only
        self.assertNotIn("2.2.0-beta.1", tried)        # stable releases only

    def test_no_release_runs(self):
        rc, out, _ = self.run_install(FAKE_CLAUDE_MAX_OK="0.0.1")
        self.assertEqual(rc, 1)
        self.assertIn("no working claude-code version found", out)
        self.assertFalse(os.path.exists(self.version_file))

    def test_unresolvable_tag(self):
        rc, out, _ = self.run_install(CLAUDE_CODE_VERSION="nope")
        self.assertEqual(rc, 1)
        self.assertIn("cannot resolve @anthropic-ai/claude-code@nope", out)
        self.assertEqual(self.installs(), [])


if __name__ == "__main__":
    unittest.main()
