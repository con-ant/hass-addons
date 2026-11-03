#!/bin/sh
# Install Claude Code from the release channel (or at the exact version) the
# build asks for, falling back to the newest earlier release that runs here.
#
# CLAUDE_CODE_VERSION is an npm dist-tag or an exact version:
#   stable   the stable channel (default): typically about a week behind
#            latest, skipping releases with major regressions
#   latest   every release as soon as it ships
#   2.1.308  that version exactly
#
# The Dockerfile ADDs the registry manifest for that tag to
# CLAUDE_CODE_MANIFEST before running this. The manifest is the Docker cache
# key for the install layer (it changes exactly when the tag moves), and it
# names the version to install, so what gets installed is what the cache key
# says. Without it (running by hand) the tag is resolved with npm view.
#
# Since 2.1.113 the CLI is a Bun-compiled native binary whose JavaScriptCore
# requires AVX. On a host without it (e.g. a VM exposing the generic kvm64 CPU
# model) those releases hang or die with SIGILL, while <= 2.1.112 - the last JS
# entrypoint - still runs. The channel's release therefore cannot be trusted
# blindly: smoke-test it, and if it fails binary-search the earlier stable
# releases for the newest one that answers `claude --version`. Fail the build
# if none does.
#
# The real fix for an AVX-less host is at the hypervisor (Proxmox CPU type
# "host"); this only keeps the add-on buildable until that happens.
set -eu

SPEC="${CLAUDE_CODE_VERSION:-stable}"
MANIFEST="${CLAUDE_CODE_MANIFEST:-/tmp/claude-code-manifest.json}"
SMOKE_TIMEOUT="${SMOKE_TIMEOUT:-30}"
VERSION_FILE="${CLAUDE_CODE_VERSION_FILE:-/etc/claude-code-version}"   # tests point this elsewhere
VERSIONS=/tmp/claude-versions

smoke() { timeout "$SMOKE_TIMEOUT" claude --version < /dev/null > /dev/null 2>&1; }
try() { npm i -g --no-fund --no-audit "@anthropic-ai/claude-code@$1" > /dev/null 2>&1 && smoke; }
at() { sed -n "${1}p" "$VERSIONS"; }

# Resolve the tag to a version: from the manifest the Dockerfile fetched, or
# from the registry when there is none.
if [ -s "$MANIFEST" ]; then
  target=$(jq -r '.version // empty' "$MANIFEST")
else
  target=$(npm view "@anthropic-ai/claude-code@$SPEC" version 2>/dev/null | tail -n 1)
fi
[ -n "$target" ] || { echo "[install] cannot resolve @anthropic-ai/claude-code@$SPEC"; exit 1; }

# Fast path: the channel's release runs here. Take it and skip the search.
if try "$target"; then
  echo "$target" > "$VERSION_FILE"
  echo "[install] claude-code $target ($SPEC, verified)"
  exit 0
fi
echo "[install] claude-code $target ($SPEC) fails its smoke test; searching for the newest earlier release that runs"

# Stable releases only, oldest -> newest, no newer than the target: the
# fallback must not move ahead of the channel. (If the target is not in the
# list, which no dist-tag should cause, search all of them.)
npm view @anthropic-ai/claude-code versions --json \
  | jq -r '.[]' \
  | grep -E '^2\.[0-9]+\.[0-9]+$' > "$VERSIONS"
if grep -qxF "$target" "$VERSIONS"; then
  sed -i "/^$(printf '%s' "$target" | sed 's/\./\\./g')\$/q" "$VERSIONS"
fi

hi=$(wc -l < "$VERSIONS")
[ "$hi" -gt 0 ] || { echo "[install] no candidate versions found"; exit 1; }

lo=1
best=""
while [ "$lo" -le "$hi" ]; do
  mid=$(( (lo + hi) / 2 ))
  v=$(at "$mid")
  if try "$v"; then
    best="$v"
    lo=$(( mid + 1 ))
  else
    hi=$(( mid - 1 ))
  fi
done

[ -n "$best" ] || { echo "[install] no working claude-code version found"; exit 1; }

npm i -g --no-fund --no-audit "@anthropic-ai/claude-code@$best" > /dev/null 2>&1
smoke || { echo "[install] $best passed during search but fails now"; exit 1; }
echo "$best" > "$VERSION_FILE"
echo "[install] claude-code $best (newest working; $target from $SPEC does not run here)"
