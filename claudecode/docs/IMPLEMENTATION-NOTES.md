# Claude Jobs — implementation notes

Companion to [`DESIGN-claude-jobs.md`](DESIGN-claude-jobs.md) (the design of record).
This file records what the code does **differently** from the design text, and why,
so a reviewer can check each deviation instead of rediscovering it. Anything not
listed here is meant to be as designed; if it is not, that is a bug.

User documentation lives in [`JOBS.md`](JOBS.md). These notes cover the whole delivery
series (design PRs 1–4: runner, notifier, endpoint, HA package); v1.1 (the action chain)
is not built.

## Layout as built

| Piece | Path in the image | Language |
|---|---|---|
| runner (`claude-job run/force-run/dry-run/list/validate/enable/disable/token`) | `/usr/local/bin/claude-job` | Python 3 |
| definition parser/validator, settings/schema/prompt/argv composition | `/usr/local/lib/claude-job/jobdef.py` | Python 3 |
| paths, constants (§4.11), atomic writes, HA states publish, cost rollup, locks, preflight | `/usr/local/lib/claude-job/jobcommon.py` | Python 3 |
| token broker | `/usr/local/lib/claude-job/ha_broker.py` | Python 3 (stdlib only) |
| `ha` CLI wrapper (first on the job's `PATH`) | `/usr/local/lib/claude-job/bin/ha` | bash |
| notifier | `/usr/local/bin/claude-job-notify` | Python 3 |
| trigger endpoint + the tick | `/usr/local/bin/claude-job-endpoint` | Python 3 (stdlib only) |
| HA package/blueprint renderer | `/usr/local/lib/claude-job/render_package.py` | Python 3 |
| image policy | `/usr/share/claudecode/{job-policy.json, job-contract.md, job-frontmatter.schema.json, job-ha-allowlist}` | data |
| shipped examples | `/usr/share/claudecode/jobs/{health-check,energy-report}.md` (seeded into the jobs dir once) | data |
| package/blueprint templates | `/usr/share/claudecode/templates/` | data |
| boot/stop integration | `claudecode-start` (`setup_job_dirs`, `cli_preflight_log`, `allow_job_commands`, `ensure_job_token`, `render_job_package`, `start_job_endpoint`, `stop_job_spawners`/`signal_job_runs`/`reap_job_runs`, `on_stop`) | bash |
| tests (dev box, no HA, no tokens) | `claudecode/tests/` — `python3 -m unittest discover -s claudecode/tests -p 'test_*.py'` | Python 3 |

The design sketched the runner in shell idiom (`exec setsid`, `flock -w`, traps).
It is Python because busybox `flock` has no `-w`, and because the judgment table,
schema re-validation and state files are JSON work; `cmd_run()` still reads as the
design's fourteen numbered steps and `finish()` is the single exit funnel. GNU
`timeout -k 15` and `env -i` stay in the argv exactly as §4.4 shows, so `dry-run`
prints a copy-pasteable command and exit codes 124/137 keep their meaning.

Python dependencies are `pyyaml` and `jsonschema` via **pip** (the design said
`apk py3-*`; the base image's Python 3.13 lives in `/usr/local`, so Alpine's
packages would be invisible to it).

## Deviations from the design text

1. **Nesting guard (§4.3 step 2, §7).** Under the interactive session's markers
   (`CLAUDECODE`, `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_ENTRYPOINT`) `claude-job run`
   does not refuse: it starts the run **detached** (new session, markers stripped,
   stdio to `/dev/null`) and prints `{"accepted": true, "job", "run_id", "detached": true}`.
   A refusing guard would have made the design's own `Bash(claude-job run:*)` allow
   rule useless, and a foreground 600 s run exceeds the Bash tool's timeout anyway.
   `force-run` under the markers refuses (exit 3). Other verbs ignore the markers.
2. **Exit codes (§4.3 step 14).** `0` contract status · `1` runner-judged `error`
   (rows 3–10), external kill (row 4), preflight refusal, runner crash · `2` invalid
   definition/input/usage · `3` skipped/disabled/overlap/stopping · `143` aborted by
   TERM/INT/HUP. The design listed only 0/2/3/143.
3. **Process groups (§4.7, A.1).** GNU `timeout` makes itself a process-group leader,
   so the claude tree is *not* in the runner's group. The pgid file therefore carries
   `pgid` (runner) **and** `child_pgid` (timeout + claude + MCP servers). The stop path
   TERMs only the runner group (the runner relays TERM to claude, waits ≤ 2 s, then
   KILLs the child group and publishes `aborted`); `reap_job_runs` KILLs both groups
   after `JOB_STOP_GRACE_SECS`. If the claude group is signalled directly anyway (e.g.
   `stop_sessions` for a terminal-started run), the runner waits 0.5 s for its own
   TERM before judging, so the outcome is still `aborted`, not "killed externally".
4. **`CLAUDE_JOB_BROKER_PORT`** carries the bare port; the wrapper builds
   `--endpoint http://127.0.0.1:$PORT`. (Design §4.4 put `127.0.0.1:` inside the value.)
5. **Composed settings** additionally carry `"sandbox": {"autoAllowBashIfSandboxed": false}`
   — a CLI build with sandboxing force-enabled was observed auto-allowing Bash under
   `dontAsk`; the key is inert where sandboxing is off. The deny list itself is §4.4
   byte-for-byte.
6. **Child environment (§4.4)** passes through proxy/CA variables when the add-on has
   them (`HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY` and lower-case forms, `SSL_CERT_FILE`,
   `SSL_CERT_DIR`, `NODE_EXTRA_CA_CERTS`) so a job can reach the API from behind the
   same proxy the interactive session uses. Nothing else is added to the allowlist.
7. **Broker route table (§4.5)** adds what the allow-listed `ha` verbs actually call:
   `GET /supervisor/stats`, `/backups`, `/backups/info`, `/backups/<slug>/info`,
   `/(core|supervisor|host)/logs/latest` and `/logs/boots/<id>` (finite); `POST /core/check`
   gets a 300 s upstream timeout (it runs `check_config` in a container) and is
   single-flight. `Range`/`Accept` are forwarded on log routes (`--lines`).
   Hardening beyond the design: `/addons/<slug>/info` responses are re-serialised to a
   fixed set of non-secret keys (sibling add-ons keep passwords in `options`);
   token-bearing entity attributes (`access_token`, `entity_picture*`, `token`,
   `stream_source`, `still_image_url`, `/(token|password|secret|api_key|credential)/i`)
   are stripped from `/core/api/states[/<id>]` and history responses;
   `POST /core/api/template` accepts only hass-mcp's own fixed area-map template when
   hass-mcp is importable (regex heuristic otherwise) — template *output* is not
   filtered, which is the design's accepted residual (§8 risk 5). Any `%`-escape other
   than `%3A`/`%2B`, `..`, `\`, non-ASCII targets, SSE/websocket and every `follow`
   variant are refused before matching. The `ha` wrapper also refuses
   `--endpoint/--api-token/--config` and follow flags hidden in short-flag clusters.
8. **`job-ha-allowlist`** accepts both `ha addons …` and `ha apps …` (CLI 5.x renamed
   the verb; `addons` is a deprecated alias). Validator Bash-arg charset is
   `[A-Za-z0-9 _.:=/@+-]`; `paths:` entries may not contain `{ } ,` or `..` anywhere.
9. **Add-on options (§4.11).** `job_default_model` is `list(fable|opus|sonnet|haiku)`
   rather than `str` (a dropdown; the allow-list is aliases-only anyway).
10. **Kill-switch flag file** is `state/disabled/<slug>`; the `<name>` spelling is also
    honoured on read, and `enable` clears both. All other files are keyed by name.
11. **`GET /jobs`** adds `cost_month_start` (feeds the template sensor's `last_reset`),
    `preflight {ok, missing}`, `built_claude_version`, `endpoint_version`,
    `generated_at`; a job whose state file is unreadable appears with
    `status: "invalid_state"` and counts as failed. The endpoint answers `411` to a POST
    without `Content-Length` (or chunked) and `408` to a short body.
12. **The tick, duty 1 (A.3)** decides liveness by the job's flock (free ⇔ no live
    runner) rather than by probing process groups, demotes under that lock after
    re-reading the state, and SIGKILLs at +180 s only groups named by a pgid file whose
    `run_id` matches. `POST /republish` arms `force_republish` (cleared only by a fully
    successful pass); a pass stops at the first transport failure.
13. **HA package (§4.10)** follows facts verified against HA Core source rather than the
    design's sketch: the alarm uses a *template* trigger (`| int(0) > 0 for 5 min`) plus
    a state trigger on `unavailable`/`unknown` and a `homeassistant: start` backstop
    (`numeric_state` never arms from `unavailable`, and persistent notifications do not
    survive a Core restart); a `claudecode_jobs_alarm_clear` automation dismisses it;
    `notify.notify` is the *last* alarm action because `continue_on_error` does not cover
    `ServiceNotFound`; `sensor.claude_jobs_monthly_cost` is `device_class: monetary`,
    `state_class: total` with `last_reset` (monetary forbids `total_increasing`), unit
    `USD`. The blueprint has `job`, `at`, `weekdays`, `job_input` inputs and
    `min_version: 2025.8.0` (time-trigger `weekday:`). After a material re-render the
    add-on reloads `rest_command` and `rest` itself; the `.git/info/exclude` entry is
    written *before* the token-bearing file.
14. **Notifier (§4.8).** Overall deadline 40 s (inside the runner's 45 s wait); send
    order persistent → pushes → dismiss → webhook; recovery to `ok` sends the job's `ok`
    mapping plus the dismiss (`notify_recovery: true` adds one "Resolved:" push when the
    ok map has no phone channel); `skipped`/`aborted`/`error` never dismiss an earlier
    finding's sidebar note; an add-on-stop `aborted` is not notified at all
    (`notify_status: skipped_aborting`) to keep the ≤ 4 s abort budget.
15. **The `--json-schema` MCP fallback (§4.6)** is not built. The seam
    (`get_structured_result()`) exists; a CLI without `--json-schema` makes the runner
    refuse with `error` / `no_structured_output_support` at zero cost.
16. **Auth retry (A.8)** applies the regex to `errors[]` joined with `result` (error
    envelopes carry `errors`, not `result`, on current CLIs).
17. **hass-mcp is pinned** (`0.6.0`) in the Dockerfile: the broker imports its fixed
    template for the allow-list and the design's tool/transport map was verified
    against that version.
18. **AppArmor** gains `/run/claudecode/** rwk` (nothing under `/run` was writable) and
    `/usr/local/lib/python3*/** mr`; bytecode for `/usr/local/lib/claude-job` is
    compiled at build time because `/usr/local` is read-only at runtime.
19. **Docs.** There is no `DOCS.md` in this add-on; user documentation is
    `docs/JOBS.md`, linked from `README.md`.

## Verified here vs. on the install

The test suite is a dev-box tool and cannot run *inside* the add-on container: it writes
small shell wrappers into a scratch directory and executes them, and under the add-on's
AppArmor profile no writable path is executable (`/tmp`, `/data`, `/homeassistant` all
refuse). Install-side verification is the §13 probe list, run by hand. The profile's bare
`network,` rule covers both listeners (broker on 127.0.0.1, endpoint on the hassio bridge);
whether the added `/run/claudecode/**` and `python3*/** mr` lines behave as intended is one
of those probes.

The test suite (≈ 320 tests) drives every component with a fake Supervisor, a fake
`claude` that emits real envelope shapes, and the real broker; an end-to-end test goes
endpoint → runner → broker → notifier, and a stop-path test checks the `aborted`
contract. One run of the real CLI (haiku) through the runner's exact invocation
confirmed the flag composition, `structured_output` judgment and entity publish on a
dev box. The §13 install-side rows (AppArmor in practice, hass-mcp tools through the
broker, `ha` verbs, teardown timing under the Supervisor, phones) remain to be run on
the reference install per PR, as the design says.
