#!/usr/bin/env python3
"""Stand-in for the `claude-job` runner, used by endpoint tests (breakdown 2k spawn contract).

Appends one JSON line to $FAKE_RUNNER_LOG:
  {"argv": [...], "env_keys": [...], "cwd": ..., "inbox": <parsed --input-file JSON or null>,
   "sid": ..., "pgid": ..., "pid": ..., "ppid": ..., "ts": ...}
Then sleeps $FAKE_RUNNER_SLEEP seconds (if set) and exits int($FAKE_RUNNER_EXIT or 0).
"""
import json
import os
import sys
import time


def main(argv: list) -> int:
    inbox = None
    if "--input-file" in argv:
        i = argv.index("--input-file")
        path = argv[i + 1] if i + 1 < len(argv) else None
        if path and os.path.exists(path):
            try:
                with open(path) as f:
                    inbox = json.load(f)
            except (OSError, ValueError) as exc:
                inbox = {"_error": repr(exc)}
    rec = {
        "argv": argv,
        "env_keys": sorted(os.environ.keys()),
        "cwd": os.getcwd(),
        "inbox": inbox,
        "sid": os.getsid(0),
        "pgid": os.getpgrp(),
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "ts": time.time(),
    }
    log = os.environ.get("FAKE_RUNNER_LOG")
    if log:
        with open(log, "a") as f:
            f.write(json.dumps(rec) + "\n")
    if os.environ.get("FAKE_RUNNER_SLEEP"):
        time.sleep(float(os.environ["FAKE_RUNNER_SLEEP"]))
    return int(os.environ.get("FAKE_RUNNER_EXIT") or 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
