#!/usr/bin/env python3
"""Stand-in for `claude-job-notify` (breakdown 2h), used by runner tests.

Records `{"argv": [...], "state_file": <path or null>, "state": <parsed state file or null>,
"cwd": ..., "env_keys": [...], "ts": ...}` as one JSON line to $FAKE_NOTIFY_LOG, then prints
exactly one JSON line shaped like the real notifier's stdout and exits 0.

Knobs: FAKE_NOTIFY_EXIT=<n> forces the exit code (output still printed unless
FAKE_NOTIFY_SILENT=1); FAKE_NOTIFY_GARBAGE=1 prints non-JSON instead.
"""
import json
import os
import sys
import time


def find_state_file(argv: list):
    """Accept `--state-file PATH`, `--state-file=PATH`, or a lone positional *.json path."""
    for i, a in enumerate(argv):
        if a == "--state-file" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--state-file="):
            return a.split("=", 1)[1]
    for a in argv:
        if a.endswith(".json") and not a.startswith("-"):
            return a
    return None


def main(argv: list) -> int:
    state_file = find_state_file(argv)
    state = None
    if state_file and os.path.exists(state_file):
        try:
            with open(state_file) as f:
                state = json.load(f)
        except (OSError, ValueError) as exc:
            state = {"_error": repr(exc)}
    rec = {"argv": argv, "state_file": state_file, "state": state, "cwd": os.getcwd(),
           "env_keys": sorted(os.environ.keys()), "ts": time.time()}
    log = os.environ.get("FAKE_NOTIFY_LOG")
    if log:
        with open(log, "a") as f:
            f.write(json.dumps(rec) + "\n")

    status = (state or {}).get("status") if isinstance(state, dict) else None
    out = {
        "notify_status": "sent:persistent",
        "mode": "full",
        "sent": [{"channel": "persistent", "ok": True}],
        "fallbacks": [],
        "errors": [],
        "last_notified": {"status": status, "headline_sha256": "x", "at": "2026-01-01T00:00:00Z",
                          "consecutive_same": 1, "persistent_active": True, "critical_hint_shown": False},
    }
    if os.environ.get("FAKE_NOTIFY_GARBAGE") == "1":
        sys.stdout.write("this is not json\n")
    elif os.environ.get("FAKE_NOTIFY_SILENT") != "1":
        sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()
    return int(os.environ.get("FAKE_NOTIFY_EXIT") or 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
