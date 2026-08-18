#!/usr/bin/env python3
"""Stand-in for the real `/usr/local/bin/ha` CLI (target of CLAUDE_JOB_REAL_HA), used to test
the bash `ha` wrapper (breakdown 2j): prints its argv as a JSON list on stdout and appends
`{"argv": [...], "env": {...CLAUDE_JOB_* only...}, "ts": ...}` to $FAKE_HA_LOG. Exit 0
(or int($FAKE_HA_EXIT))."""
import json
import os
import sys
import time

argv = sys.argv[1:]
sys.stdout.write(json.dumps(argv) + "\n")
log = os.environ.get("FAKE_HA_LOG")
if log:
    env = {k: v for k, v in os.environ.items() if k.startswith("CLAUDE_JOB_")}
    with open(log, "a") as f:
        f.write(json.dumps({"argv": argv, "env": env, "ts": time.time()}) + "\n")
sys.exit(int(os.environ.get("FAKE_HA_EXIT") or 0))
