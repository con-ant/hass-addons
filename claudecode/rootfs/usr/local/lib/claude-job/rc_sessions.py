"""The Remote Control server's sessions, for GET /sessions and sensor.claude_remote_control_sessions.

`claude remote-control` (remote_control_server) spawns one headless Claude per session started from
claude.ai/code or the Claude app (`claude --print --sdk-url ... --session-id cse_...`, its children).
They have no terminal, so the web terminal cannot show them; this lists them so the terminal's badge
and Home Assistant can say they exist and whether one is working.

/proc cannot be listed in this container (AppArmor), so the sessions are found through the state
claude-session-hook keeps per Claude process outside tmux: RUN_DIR/procs/<pid>/ (created at
SessionStart; last-prompt / last-stop epochs per turn). A pid counts while it is alive, descends from
the server's pid (RUN_DIR/rc-server.pid; any depth, like the update watcher's server_idle(), so a release
that spawns sessions through an intermediate process still counts them) and has taken a prompt: the
server pre-creates one session at start, which nobody may ever open, and counting it would make the
sensor read 1 on every box with the server on. Without apply_claude_updates the hooks are not
installed and the list stays empty while the server itself still reports as running.

Since con.33 the server's own session is known exactly, so the "taken a prompt" rule is only the
fallback: the server records it in ~/.claude/projects/<cwd key>/bridge-pointer.json ({"sessionId":
"session_<ID>", "environmentId": ...}; the key is its working directory with every character outside
[A-Za-z0-9-] made "-", read from /proc/<server>/cwd), and the session whose cse_<ID> matches is listed
with server_session true and counted only once someone has prompted it. Every other session counts
from the start, prompted or not. With no pointer (an older or a newer CLI, a server that has not
written it yet) unprompted sessions are left out as before.

Titles (con.33): the server's status screen lists each attached session as
"https://claude.ai/code/session_<ID>?from=cli <title>"; the log filter in claudecode-start writes the
title to RUN_DIR/rc-titles/<ID> whenever it changes (status words such as "Attached" are not titles).
Each session carries it as `title`, or null.
"""
import json
import os
import re
import time

# A turn whose prompt is this old with no stop is taken as abandoned (claude-update-watcher's
# CLAUDE_UPDATE_STALE_TURN_SECS default), not as one still in flight.
STALE_TURN_S = 3600
# Ancestors walked from a session towards the server before giving up (a session is a direct child today).
MAX_DEPTH = 8


def _read_int(path):
    try:
        with open(path) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def _stat(pid, proc):
    """(ppid, starttime in clock ticks since boot) from /proc/<pid>/stat; comm may hold spaces."""
    with open(f"{proc}/{pid}/stat") as f:
        rest = f.read().rsplit(")", 1)[1].split()
    return int(rest[1]), int(rest[19])


def _descends(pid, spid, ppid, proc):
    """True when `pid`, whose parent is `ppid`, has the server `spid` among its ancestors."""
    for _ in range(MAX_DEPTH):
        if ppid == spid:
            return True
        if ppid <= 1:
            return False
        try:
            ppid = _stat(ppid, proc)[0]
        except (OSError, IndexError, ValueError):
            return False
    return False


def _server_session(spid, proc, claude_dir):
    """The <ID> of the server's own session (bridge-pointer.json of its working directory), or None."""
    try:
        cwd = os.readlink(f"{proc}/{spid}/cwd")
        key = re.sub(r"[^A-Za-z0-9-]", "-", cwd)
        with open(os.path.join(claude_dir, "projects", key, "bridge-pointer.json")) as f:
            sid = json.load(f).get("sessionId") or ""
    except (OSError, ValueError, AttributeError):
        return None
    return sid[len("session_"):] if sid.startswith("session_") and len(sid) > 8 else None


def _title(run_dir, sid):
    """The title the log filter recorded for this session, or None."""
    if not sid or not sid.startswith("cse_") or not re.fullmatch(r"[A-Za-z0-9]+", sid[4:]):
        return None
    try:
        with open(f"{run_dir}/rc-titles/{sid[4:]}") as f:
            t = f.read().strip()
    except OSError:
        return None
    return t[:200] or None


def _boot_time(proc):
    try:
        with open(f"{proc}/stat") as f:
            for line in f:
                if line.startswith("btime "):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


def _session_id(pid, proc):
    try:
        with open(f"{proc}/{pid}/cmdline", "rb") as f:
            argv = f.read().decode(errors="replace").split("\0")
    except OSError:
        return None
    for i, arg in enumerate(argv[:-1]):
        if arg == "--session-id":
            return argv[i + 1] or None
    return None


def snapshot(run_dir, enabled, proc="/proc", now=None, claude_dir=None):
    """{"server": off|running|stopped, "server_pid", "count", "busy", "sessions": [...]}.

    Each session: pid, id (the cse_ id the app knows it by), title (or null), server_session (the
    server's own), state busy|idle, started, last_prompt, last_stop (epoch seconds or null). `count`
    leaves out the server's own session until it has been prompted. No running "idle for" figure:
    the snapshot only changes when a session or a turn does, so the sensor's attributes do not write a recorder row every poll."""
    now = int(now if now is not None else time.time())
    out = {"server": "off", "server_pid": None, "count": 0, "busy": 0, "sessions": []}
    if not enabled:
        return out
    spid = _read_int(f"{run_dir}/rc-server.pid")
    if not spid or not os.path.exists(f"{proc}/{spid}"):
        out["server"] = "stopped"
        return out
    out["server"], out["server_pid"] = "running", spid
    own = _server_session(spid, proc, claude_dir or os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude"))
    boot, hz = _boot_time(proc), os.sysconf("SC_CLK_TCK")
    try:
        pids = sorted(int(d) for d in os.listdir(f"{run_dir}/procs") if d.isdigit())
    except OSError:
        pids = []
    for pid in pids:
        try:
            ppid, start_ticks = _stat(pid, proc)
        except (OSError, IndexError, ValueError):
            continue                                  # exited; the update watcher prunes its state dir
        if not _descends(pid, spid, ppid, proc):
            continue
        lp = _read_int(f"{run_dir}/procs/{pid}/last-prompt")
        sid = _session_id(pid, proc)
        mine = bool(own) and sid == f"cse_{own}"
        if not lp and not own:
            continue                                  # never prompted and no pointer to tell: maybe the server's own
        ls = _read_int(f"{run_dir}/procs/{pid}/last-stop")
        started = boot + start_ticks // hz if boot else None
        busy = lp > ls and now - lp < STALE_TURN_S
        out["sessions"].append({
            "pid": pid, "id": sid, "title": _title(run_dir, sid), "server_session": mine, "state": "busy" if busy else "idle",
            "started": started, "last_prompt": lp or None, "last_stop": ls or None,
        })
    out["count"] = sum(1 for s in out["sessions"] if not s["server_session"] or s["last_prompt"])
    out["busy"] = sum(1 for s in out["sessions"] if s["state"] == "busy")
    return out
