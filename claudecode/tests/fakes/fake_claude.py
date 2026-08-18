#!/usr/bin/env python3
"""Stand-in for the `claude` CLI used by the Claude Jobs test-suite.

Mirrors the headless envelope shapes recorded by the cli-probe (see
reports/cli-probe.md and cliprobe/env*.json): one JSON object on stdout with
`"type": "result"`; success envelopes carry `result` (string) and
`structured_output` (object); error envelopes carry `errors` and OMIT
`result`/`structured_output`, exit 1.

Modes
-----
--version            -> "2.1.233 (Claude Code)"          (FAKE_CLAUDE_VERSION)
--help               -> plausible help text listing the preflight flags,
                        minus any flag named in FAKE_CLAUDE_HIDE_FLAG (comma list)
-p <prompt> ...      -> append one JSON line to $FAKE_CLAUDE_LOG describing the
                        invocation, then act per $FAKE_CLAUDE_SCENARIO:

  success[:status]   success envelope, structured_output.status = status|ok
  denial             success + one Bash entry in permission_denials
  no_so              success, structured_output: null
  bad_so             structured_output {"status":"bogus","headline":"x"}
  max_turns          error_max_turns, exit 1
  budget             error_max_budget_usd, exit 1
  is_error           error_during_execution, errors ["boom"], exit 1
  auth_error         error_during_execution "OAuth token has expired..." the
                     FIRST time (marker file $FAKE_CLAUDE_AUTH_ONCE), then success
  garbage            prints "not json", exit 1
  sleep:<s>          sleep s seconds, then success
  ignore_term:<s>    ignore SIGTERM, sleep s seconds, then success
  exit:<n>           no output, exit n

Modifiers (env): FAKE_CLAUDE_COST (0.12), FAKE_CLAUDE_TURNS (4),
FAKE_CLAUDE_MODEL_ID (derived from --model), FAKE_CLAUDE_SO (JSON, verbatim
structured_output), FAKE_CLAUDE_HEADLINE ("all good").
"""
import json
import os
import signal
import sys
import time
import uuid

VERSION_LINE = "{v} (Claude Code)"

# (flag, rest-of-line) pairs; order and wording loosely follow the real --help.
HELP_FLAGS = [
    ("--add-dir <directories...>", "Additional directories to allow tool access to"),
    ("--allowedTools, --allowed-tools <tools...>", "Comma or space-separated list of tool names to allow"),
    ("--append-system-prompt <prompt>", "Append a system prompt to the default system prompt"),
    ("--disallowedTools, --disallowed-tools <tools...>", "Comma or space-separated list of tool names to deny"),
    ("-h, --help", "Display help for command"),
    ("--json-schema <schema>", "JSON Schema for structured output validation"),
    ("--max-budget-usd <amount>", "Maximum dollar amount to spend on API calls (only works with --print)"),
    ("--mcp-config <configs...>", "Load MCP servers from JSON files or strings (space-separated)"),
    ("--model <model>", "Model for the current session. Provide an alias (e.g. 'fable', 'opus', or 'sonnet') or a full name"),
    ("--no-session-persistence", "Disable session persistence (only works with --print)"),
    ("--output-format <format>", 'Output format (only works with --print): "text", "json", or "stream-json"'),
    ("--permission-mode <mode>", 'Permission mode (choices: "acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan")'),
    ("-p, --print", "Print response and exit (useful for pipes)"),
    ("--session-id <uuid>", "Use a specific session ID for the conversation"),
    ("--setting-sources <sources>", "Comma-separated list of setting sources to load (user, project, local)."),
    ("--settings <file-or-json>", "Path to a settings JSON file or a JSON string"),
    ("--strict-mcp-config", "Only use MCP servers from --mcp-config, ignoring all other MCP configurations"),
    ("--system-prompt <prompt>", "System prompt to use for the session"),
    ('--tools <tools...>', 'Specify the list of available tools from the built-in set. Use "" to disable all tools'),
    ("--verbose", "Override verbose mode setting from config"),
    ("-v, --version", "Output the version number"),
]

MODEL_IDS = {
    "fable": "claude-fable-5",
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5-20251001",
}

# Flags whose value we echo into the log record under "parsed".
PARSED_FLAGS = {
    "-p": "prompt", "--print": "prompt", "--model": "model", "--json-schema": "json_schema",
    "--settings": "settings", "--mcp-config": "mcp_config", "--max-turns": "max_turns",
    "--max-budget-usd": "max_budget_usd", "--tools": "tools", "--output-format": "output_format",
    "--permission-mode": "permission_mode", "--setting-sources": "setting_sources",
    "--append-system-prompt": "append_system_prompt",
}


def print_help() -> None:
    hidden = {f.strip() for f in os.environ.get("FAKE_CLAUDE_HIDE_FLAG", "").split(",") if f.strip()}
    print("Usage: claude [options] [command] [prompt]\n")
    print("Claude Code - starts an interactive session by default, use -p/--print for\nnon-interactive output\n")
    print("Options:")
    for flag, desc in HELP_FLAGS:
        names = {part.split()[0] for part in flag.split(", ")}
        if names & hidden:
            continue
        print(f"  {flag:<38}{desc}")


def parse_argv(argv: list) -> dict:
    """Very small flag scanner: records the value following each known flag."""
    parsed = {}
    positional = []
    i = 0
    while i < len(argv):
        a = argv[i]
        key, eq, val = a.partition("=")
        if key in PARSED_FLAGS:
            if not eq:
                nxt = argv[i + 1] if i + 1 < len(argv) else None
                if key in ("-p", "--print") and (nxt is None or nxt.startswith("--")):
                    i += 1          # `-p` used as a bare switch; prompt is positional
                    continue
                val = nxt
                i += 1
            parsed[PARSED_FLAGS[key]] = val
        elif not a.startswith("-"):
            positional.append(a)
        elif a == "--strict-mcp-config":
            parsed["strict_mcp_config"] = True
        elif a == "--no-session-persistence":
            parsed["no_session_persistence"] = True
        i += 1
    if "prompt" not in parsed and positional:
        parsed["prompt"] = positional[-1]
    return parsed


def stdin_is_devnull() -> bool:
    try:
        st, null = os.fstat(0), os.stat("/dev/null")
        return (st.st_dev, st.st_ino) == (null.st_dev, null.st_ino) or (
            os.major(st.st_rdev), os.minor(st.st_rdev)) == (os.major(null.st_rdev), os.minor(null.st_rdev))
    except OSError:
        return False


def log_invocation(argv: list, parsed: dict) -> None:
    path = os.environ.get("FAKE_CLAUDE_LOG")
    if not path:
        return
    try:
        isatty = os.isatty(0)
    except OSError:
        isatty = False
    rec = {
        "argv": argv,
        "parsed": parsed,
        "env_keys": sorted(os.environ.keys()),
        "env": dict(os.environ),
        "cwd": os.getcwd(),
        "stdin_isatty": isatty,
        "stdin_is_devnull": stdin_is_devnull(),
        "pid": os.getpid(),
        "pgid": os.getpgrp(),
        "ts": time.time(),
    }
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")


def model_id(parsed: dict) -> str:
    forced = os.environ.get("FAKE_CLAUDE_MODEL_ID")
    if forced:
        return forced
    m = parsed.get("model") or "fable"
    return MODEL_IDS.get(m, m)


def base_envelope(parsed: dict, cost: float, turns: int) -> dict:
    """Keys common to success and error envelopes, in the real emission order."""
    mid = model_id(parsed)
    return {
        "type": "result",  # moved to front for readability; consumers key on it, not order
        "is_error": False,
        "duration_api_ms": 1697,
        "num_turns": turns,
        "stop_reason": "tool_use",
        "session_id": str(uuid.uuid4()),
        "total_cost_usd": cost,
        "usage": {
            "input_tokens": 10, "cache_creation_input_tokens": 7354, "cache_read_input_tokens": 0,
            "output_tokens": 154, "output_tokens_details": {"thinking_tokens": 80},
            "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
            "service_tier": "standard",
            "cache_creation": {"ephemeral_1h_input_tokens": 7354, "ephemeral_5m_input_tokens": 0},
            "inference_geo": "not_available", "iterations": [], "speed": "standard",
        },
        "modelUsage": {
            mid: {"inputTokens": 10, "outputTokens": 154, "cacheReadInputTokens": 0,
                  "cacheCreationInputTokens": 7354, "webSearchRequests": 0, "costUSD": cost,
                  "contextWindow": 200000, "maxOutputTokens": 32000,
                  "canonicalModel": mid, "provider": "firstParty"},
        },
        "permission_denials": [],
        "terminal_reason": "completed",
        "fast_mode_state": "off",
        "fast_mode_disabled_reason": "sdk_opt_in_required",
        "subtype": "success",
        "duration_ms": 2050,
        "uuid": str(uuid.uuid4()),
    }


def success_envelope(parsed: dict, status: str = "ok", so=..., denials=None) -> dict:
    cost = float(os.environ.get("FAKE_CLAUDE_COST", "0.12"))
    turns = int(os.environ.get("FAKE_CLAUDE_TURNS", "4"))
    env = base_envelope(parsed, cost, turns)
    if so is ...:
        so = {"status": status, "headline": os.environ.get("FAKE_CLAUDE_HEADLINE", "all good"),
              "detail": "d", "metrics": {"n": 1}}
    if os.environ.get("FAKE_CLAUDE_SO"):
        so = json.loads(os.environ["FAKE_CLAUDE_SO"])
    env["permission_denials"] = denials or []
    env["api_error_status"] = None
    env["result"] = json.dumps(so) if so is not None else "I could not produce a structured result."
    env["structured_output"] = so
    env["ttft_ms"], env["ttft_stream_ms"], env["time_to_request_ms"] = 1805, 1022, 160
    return env


def error_envelope(parsed: dict, subtype: str, errors: list, terminal_reason: str) -> dict:
    cost = float(os.environ.get("FAKE_CLAUDE_COST", "0.12"))
    turns = int(os.environ.get("FAKE_CLAUDE_TURNS", "4"))
    env = base_envelope(parsed, cost, turns)
    env["is_error"] = True
    env["subtype"] = subtype
    env["terminal_reason"] = terminal_reason
    env["errors"] = errors
    # NOTE: no "result", "structured_output", "api_error_status", "ttft_*" keys (cli-probe probe 2).
    return env


def emit(env: dict, code: int) -> int:
    sys.stdout.write(json.dumps(env) + "\n")
    sys.stdout.flush()
    return code


DENIAL = {"tool_name": "Bash", "tool_use_id": "toolu_013MSkszDzEophTXb1HKuhwc",
          "tool_input": {"command": "ha addons", "description": "List add-ons"}}


def run_print_mode(argv: list) -> int:
    scenario = os.environ.get("FAKE_CLAUDE_SCENARIO", "success")
    name, _, arg = scenario.partition(":")
    if name == "ignore_term":           # install before logging so a waiter on the log cannot race us
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    parsed = parse_argv(argv)
    log_invocation(argv, parsed)

    if name == "success":
        return emit(success_envelope(parsed, arg or "ok"), 0)
    if name == "denial":
        return emit(success_envelope(parsed, "ok", denials=[DENIAL]), 0)
    if name == "no_so":
        return emit(success_envelope(parsed, so=None), 0)
    if name == "bad_so":
        return emit(success_envelope(parsed, so={"status": "bogus", "headline": "x"}), 0)
    if name == "max_turns":
        n = parsed.get("max_turns") or "1"
        return emit(error_envelope(parsed, "error_max_turns",
                                   [f"Reached maximum number of turns ({n})"], "max_turns"), 1)
    if name == "budget":
        b = parsed.get("max_budget_usd") or "0.05"
        return emit(error_envelope(parsed, "error_max_budget_usd",
                                   [f"Exceeded USD budget ({b})"], "max_budget_usd"), 1)
    if name == "is_error":
        return emit(error_envelope(parsed, "error_during_execution", ["boom"], "completed"), 1)
    if name == "auth_error":
        marker = os.environ.get("FAKE_CLAUDE_AUTH_ONCE")
        if marker and not os.path.exists(marker):
            with open(marker, "w") as f:
                f.write("1\n")
            return emit(error_envelope(parsed, "error_during_execution",
                                       ["OAuth token has expired. Please run /login"], "completed"), 1)
        return emit(success_envelope(parsed, "ok"), 0)
    if name == "garbage":
        sys.stdout.write("not json\n")
        sys.stdout.flush()
        return 1
    if name == "sleep":
        time.sleep(float(arg or "1"))
        return emit(success_envelope(parsed, "ok"), 0)
    if name == "ignore_term":
        deadline = time.monotonic() + float(arg or "1")
        while time.monotonic() < deadline:
            time.sleep(0.05)
        return emit(success_envelope(parsed, "ok"), 0)
    if name == "exit":
        return int(arg or "0")
    sys.stderr.write(f"fake_claude: unknown scenario {scenario!r}\n")
    return 2


def main(argv: list) -> int:
    if "--version" in argv or "-v" in argv:
        print(VERSION_LINE.format(v=os.environ.get("FAKE_CLAUDE_VERSION", "2.1.233")))
        return 0
    if "--help" in argv or "-h" in argv:
        print_help()
        return 0
    if "-p" in argv or "--print" in argv:
        return run_print_mode(argv)
    sys.stderr.write("fake_claude: interactive mode is not simulated; pass -p\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
