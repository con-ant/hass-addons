#!/bin/sh
# Install the newest @anthropic-ai/claude-code that actually runs in this image.
#
# Since 2.1.113 the CLI is a Bun-compiled native binary whose JavaScriptCore
# requires AVX. On a host without it (e.g. a VM exposing the generic kvm64 CPU
# model) those releases hang or die with SIGILL, while <= 2.1.112 - the last JS
# entrypoint - still runs. "latest" therefore cannot be trusted blindly, and a
# hardcoded version goes stale. Binary-search the published versions for the
# newest one that answers `claude --version`, and fail the build if none does.
#
# The real fix for an AVX-less host is at the hypervisor (Proxmox CPU type
# "host"); this only keeps the add-on buildable until that happens.
set -eu

SMOKE_TIMEOUT="${SMOKE_TIMEOUT:-30}"
VERSIONS=/tmp/claude-versions

smoke() { timeout "$SMOKE_TIMEOUT" claude --version < /dev/null > /dev/null 2>&1; }
try() { npm i -g --no-fund --no-audit "@anthropic-ai/claude-code@$1" > /dev/null 2>&1 && smoke; }
at() { sed -n "${1}p" "$VERSIONS"; }

# Stable releases only, oldest -> newest. Search is bounded below at 2.x.
npm view @anthropic-ai/claude-code versions --json \
  | jq -r '.[]' \
  | grep -E '^2\.[0-9]+\.[0-9]+$' > "$VERSIONS"

hi=$(wc -l < "$VERSIONS")
[ "$hi" -gt 0 ] || { echo "[install] no candidate versions found"; exit 1; }

# Fast path: if the newest release works, take it and skip the search.
newest=$(at "$hi")
if try "$newest"; then
  echo "$newest" > /etc/claude-code-version
  echo "[install] claude-code $newest (latest, verified)"
  exit 0
fi
echo "[install] latest ($newest) fails its smoke test; searching for newest working release"

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
echo "$best" > /etc/claude-code-version
echo "[install] claude-code $best (newest working; $newest is broken upstream)"
