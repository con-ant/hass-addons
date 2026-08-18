#!/bin/bash
# Test shim for GNU `timeout` (CLAUDE_JOB_TIMEOUT_BIN). The runner invokes
#   timeout --signal=TERM -k 15 <secs> <cmd...>
# This shim swallows those leading options and the duration, then execs the real
# timeout with a short limit so the "timeout" judgment row can be exercised fast:
#   timeout --signal=TERM -k ${FAST_TIMEOUT_KILL_S:-2} ${FAST_TIMEOUT_S:-2} <cmd...>
# Exit codes are therefore the real ones: 124 on expiry, 137 after the -k KILL.
set -u
while [ $# -gt 0 ]; do
    case "$1" in
        --signal=*|--kill-after=*|--preserve-status|--foreground|-v|--verbose) shift ;;
        -s|-k|--signal|--kill-after) shift; [ $# -gt 0 ] && shift ;;
        -s*|-k*) shift ;;
        --) shift; break ;;
        -*) shift ;;
        *) shift; break ;;   # the original duration; everything after it is the command
    esac
done
if [ $# -eq 0 ]; then
    echo "fast_timeout: no command given" >&2
    exit 125
fi
REAL_TIMEOUT="${FAST_TIMEOUT_REAL:-$(command -v timeout)}"
exec "$REAL_TIMEOUT" --signal=TERM -k "${FAST_TIMEOUT_KILL_S:-2}" "${FAST_TIMEOUT_S:-2}" "$@"
