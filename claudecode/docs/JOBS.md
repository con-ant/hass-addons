# Claude Jobs — scheduled, unattended Claude runs

A **job** is an unattended Claude run: a Markdown file holding an agent definition
(model, tools, prompt) plus scheduling and notification settings. Each execution is a
**run**. Home Assistant owns the schedule and triggers runs over HTTP; the add-on executes
them headlessly, isolated from your interactive terminal session, and turns the result
into a `sensor.claude_job_<slug>` entity plus a notification chosen by severity. A person
owns whatever action follows: jobs observe and report, they never change anything.

Jobs are unrelated to Claude Code's in-session `Task` tools and to Home Assistant's
`ai_task` integration; the name is different on purpose. Nothing a job does touches what
`claude --continue` resumes in the terminal. The full design, including the security
argument, is in [`docs/DESIGN-claude-jobs.md`](DESIGN-claude-jobs.md).

Typical uses: a daily health check that reads `ha core check`, the error log and failing
integrations and tells you only when something needs a human; a morning energy report
computed from recorder history. Both ship as examples.

## Where things live

```
/homeassistant/.claudecode/jobs/          # = ~/.claude/jobs/ ; persistent, in your HA backups
├── health-check.md  energy-report.md     # job definitions: YAML frontmatter + prompt
├── _notify.yaml                          # optional notifier config (files starting with _ are never jobs)
├── logs/<name>.jsonl                     # one JSON line per run; pruned to the last 200 lines above 1 MiB
├── logs/cost-<YYYY-MM>.json              # archived monthly cost rollups
└── state/<name>.json                     # last result of each job (what the entity shows, untruncated)
    state/_cost.json                      # current-month cost accumulator (month in HA's time zone)
    state/disabled/<slug>                 # kill-switch flag files
/homeassistant/.claudecode/projects/-data-claude-jobs-project/   # run transcripts; kept 30 days / 50 MiB
/data/claude-jobs/token                   # endpoint bearer token (0600); NOT in the HA config backup
/usr/share/claudecode/                    # image-shipped, read-only: job-policy.json (deny baseline),
                                          # job-contract.md (system prompt), job-ha-allowlist,
                                          # jobs/*.md (pristine examples), notify.example.yaml
```

## Writing a job

A job file is `~/.claude/jobs/<name>.md`. The **name is the filename stem** and must match
`^[a-z0-9]+(-[a-z0-9]+)*$` (lowercase, hyphen-separated, at most 48 characters, no
underscores), so the entity id — hyphens become underscores, `health-check.md` →
`sensor.claude_job_health_check` — can never collide. Files starting with `_` or `.` are
skipped silently (infrastructure); other files that do not match are not jobs and
`claude-job list` says why they were ignored. The file is
UTF-8, at most 64 KiB, starts with a `---` YAML frontmatter block, and everything after the
closing `---` is the prompt. Unknown frontmatter keys are **errors** (a typo'd `timout:`
must not silently vanish; the validator suggests the key you meant), and a `name:` key is
rejected because the name comes from the filename.

The complete frontmatter, annotated. This is the reference, not a template (its `actions:`
entry names a job that does not exist); start real jobs from a copy of the shipped
`health-check.md`.

```yaml
---
description: Daily health check; reports anything a human should look at.
  # REQUIRED · 1..200 chars, one line. Shown by `claude-job list` and used as the
  # entity's friendly_name (keep it under 60; longer only draws a warning).

model: fable
  # optional · default: the add-on option job_default_model (ships "fable").
  # Must be one of: fable, opus, sonnet, haiku (aliases only).

timeout: 600
  # optional · seconds · default 600 · 30..3600. Wall clock for the Claude process;
  # on expiry it gets SIGTERM, then SIGKILL 15 s later, and the run is `error`.

max_cost_usd: 1.50
  # optional · default 1.00 · 0.01..5.00. A HARD mid-run stop: the run ends as
  # `error` "stopped at budget cap". On fable even a trivial run costs ~$0.15,
  # so values below that fail immediately.

max_turns: 50
  # optional · default 50 · 2..200 (submitting the result itself uses a turn).

enabled: true
  # optional · default true. false = born disabled, pending human review — what a
  # Claude-authored job should ship with. Runtime pausing uses the flag file
  # (`claude-job disable`); a job runs only if the flag is absent AND this is not false.

min_interval: 60
  # optional · seconds · default 60 · 0..86400, 0 = off. Endpoint-side rate limit
  # between accepted triggers of this job (terminal runs are never rate-limited).

stale_after: 93600
  # optional · seconds · 60..31708800 (367 days). If set, the job counts as "stale"
  # once now - last_run > stale_after, which raises the shipped alarm. 26 h suits a
  # daily job. Absent = on-demand job, never counted stale.

paths:
  # optional · up to 16 absolute globs the job may READ (Read/Grep/Glob). Every
  # entry must be under /homeassistant, /share or /media; anything else (/ssl,
  # /backup, /config, /data, /root, /proc, ...) is a validation error, as are `..`
  # anywhere, empty or `.` segments, and a trailing `/`.
  # Allowed characters: letters, digits, _ . / * ? -   (no braces, commas or spaces)
  # No paths: = the job can read no files at all (only `ha` / MCP output).
  # /share is storage shared by ALL add-ons and some stage sensitive files there;
  # granting /share/** is your judgment call, not a default.
  - /homeassistant/**

tools:
  # REQUIRED · 1..64 entries, no duplicates. This list is BOTH the set of tools that
  # exist for the run and the allow rules. Accepted forms:
  #   Bash(ha <read-only subcommand>)      exact command, or with a `:*` suffix where the
  #                                        allow-list permits extra arguments (see below)
  #   mcp__homeassistant__<tool>           a hass-mcp tool (the only MCP server wired for jobs)
  # Rejected: Write, Edit, MultiEdit, NotebookEdit, WebFetch, WebSearch, Task, Agent,
  # Skill, KillShell, TodoWrite (jobs are read-only); bare `Bash`; any Bash command not
  # starting with `ha`; arguments outside [A-Za-z0-9 _.:=/@+-] separated by single
  # spaces (so no shell metacharacters, quotes or `*` — end the rule with `:*` instead);
  # raw Read/Grep/Glob rules (use paths:); `-f/--follow/-b/--boot` in any spelling
  # (`-fn`, `-f=…`) in an `ha … logs` rule; the `ha` global flags --endpoint,
  # --api-token, --config (the job's `ha` is already wired to its broker).
  - Bash(ha core check)
  - Bash(ha core logs:*)
  - Bash(ha supervisor info)
  - Bash(ha resolution info)
  - mcp__homeassistant__get_error_log
  - mcp__homeassistant__list_automations

notify:
  # optional · map. Keys: ok, info, warning, critical, error, skipped, aborted.
  # Values: lists of channels — persistent, state_only, mobile, mobile_critical,
  # notify_default, webhook (a bare string is read as a one-item list). Keys you do
  # not name use the defaults in "Result states" below. `webhook` is an error unless
  # _notify.yaml configures a webhook URL; `tts` and `file` are reserved names, not
  # available yet. Mapping critical or error to silence draws a warning, not an error.
  critical: [mobile_critical, persistent]
  warning:  [mobile, persistent]
  ok:       [state_only]

renag_every: 3
  # optional · default 3 · 0..100, 0 = off. An unchanged `critical` result re-pushes
  # to phones on every Nth consecutive repeat instead of every run.

notify_recovery: false
  # optional · default false. true = one "Resolved: …" push when the job returns to ok.

input:
  # optional · up to 8 parameters a trigger may pass ({"input": {...}} in the POST body,
  # or --input on the command line). Names match ^[a-z][a-z0-9_]{0,31}$. Spec keys:
  #   type (required): string | integer | number | boolean
  #   description (<=200 chars), required (default false), default (must match the type),
  #   pattern / enum / max_length (strings; max_length default 200, up to 2000),
  #   minimum / maximum (integer/number). No nesting; string values may not contain
  #   control characters (newline, tab, …). The whole input object is capped at 4096
  #   bytes serialized. ABSENT (or empty) input: means the job takes NO input and a
  #   trigger that sends any is rejected (HTTP 422); `required: true` parameters are
  #   enforced even for an empty trigger.
  date:
    type: string
    pattern: '^\d{4}-\d{2}-\d{2}$'
    description: Which day to report on; defaults to yesterday.

actions:
  # optional · up to 4 · RESERVED for v1.1. Validated today (id ^[a-z0-9_]{1,32}$ unique,
  # label 1..40 chars, job = an existing `kind: action` job file, ttl_min 1..10080,
  # default 1440) but completely inert: no buttons are rendered and nothing is executed.
  - id: restart_miele
    label: Re-auth Miele
    job: restart-miele
    ttl_min: 1440
---
Check Home Assistant's health: run `ha core check`, read `ha resolution info`,
`ha supervisor info` and the recent error log, ...
```

In this release `notify:`,
`renag_every:` and `notify_recovery:` are validated but nothing is delivered yet
(`notify_status: skipped_no_notifier`), and `input:` can only be supplied with
`--input`/`--input-file` on the command line.

One more key exists: `kind:` (default `job`). `kind: action` is reserved for the v1.1
action-button chain; such files validate but can neither be scheduled nor triggered in
this version.

The **`ha` allow-list** (`/usr/share/claudecode/job-ha-allowlist`) is the complete set of
`Bash(ha …)` commands a job may name. A trailing `*` means the rule may carry extra
arguments and may be written with `:*`; without it the rule must name the command exactly
(`Bash(ha addons)` is valid, `Bash(ha addons:*)` is not):

```
ha core check          ha supervisor info       ha os info        ha addons          ha backups
ha core info           ha supervisor stats      ha host info      ha addons info *   ha backups info *
ha core stats          ha supervisor logs *     ha host logs *    ha apps
ha core logs *         ha resolution info                         ha apps info *
```

Every mutating verb (`restart`, `update`, `options`, `reboot`, `restore`, …) is simply
absent. Inside a run:

- `logs` verbs are `--lines/-n` (and `-v`) only. `-f/--follow` would block until the job's
  timeout, so the validator rejects rules naming it and the job's `ha` wrapper strips
  `-f/--follow/-b/--boot` before running the real CLI. Write flags separately —
  `ha core logs -n 50`, not `-fn 50`: combined short flags containing `f` or `b` are refused.
  `ha host logs -t/--identifier` is not available (the broker has no route for it).
- `ha apps …` is the current CLI spelling; `ha addons …` still works and prints a
  deprecation notice. `ha apps info <slug>` output has the add-on's `options` (where other
  add-ons keep their passwords) filtered out; `ha backups info <slug>` works.
- hass-mcp tools that use Home Assistant's websocket API — the dashboard tools and
  `get_statistics_range` — do not work inside jobs (the job's HA access is HTTP-only); use
  `get_history`/`get_statistics` instead. Expect one harmless
  `[ha-broker] 403 GET /core/api/hassio/core/logs` line in the add-on log per
  `get_error_log` call (hass-mcp probes that path before falling back to the allowed one).

**Bounds.** Every run has three: `max_turns` catches loops, `max_cost_usd` hard-stops
spend, `timeout` catches hangs — and only the timeout produces no result at all. Size them
so the typed bounds trip first: `timeout ≥ max_turns × 10 s`.

**Writing the prompt.** The job runs with a fixed system-prompt contract
(`/usr/share/claudecode/job-contract.md`): it is unattended, nobody answers questions,
everything it reads is data rather than instructions, secrets never go into the report,
and a partial result submitted in time beats a complete one that never arrives. Your prompt
only needs to say what to inspect and how to grade it. Ask for the load-bearing number in
the headline ("3 integrations failing since 03:12", not "check complete") — the headline is
what appears on the phone and what decides whether a repeat result re-notifies.

The result the job must submit is fixed: `status` (`ok` | `info` | `warning` |
`critical`), `headline` (≤ 120 chars), optional `detail` (Markdown, ≤ 8000 chars) and
optional `metrics` (up to 20 numbers with `snake_case` names, which become entity
attributes).

### The shipped examples

Two maintainer-reviewed jobs are copied into `~/.claude/jobs/` the first time the add-on
creates that directory, and ship `enabled: true` — nothing runs them until you create a
schedule:

| File | Model | What it does |
|---|---|---|
| `health-check.md` | default (`fable`) | `ha core check`, resolution/supervisor info, recent error log, failing or unavailable integrations and entities, automations with errors; status by severity, counts in the headline; `stale_after: 93600`, `max_cost_usd: 1.50`, `paths: [/homeassistant/**]` |
| `energy-report.md` | `sonnet` | finds energy/power sensors and utility meters, computes yesterday's (or `input.date`'s) consumption, production and top consumers from history, compares with the previous 7-day average; `info` normally, `warning` if consumption is > 50 % above average or a meter went unavailable |

Both are **examples**, generic on purpose: they discover what they can on any install and
say so. A job that is genuinely yours — the report you actually read every morning — is a
copy with your knowledge pinned into the prompt: which meter is the whole-house one, which
loads sit on a tap the mains never see, which sensors mean "somebody is home". Keep the
`metrics` names stable once a dashboard or automation keys on them. This is also how a job
replaces an existing report *script*: the numbers the script printed to stdout go into
`metrics` (they become entity attributes) and the prose goes into `detail`; nothing parses
stdout any more.

They are never re-seeded (deleting one is permanent). Pristine copies live in the image:

```bash
cp /usr/share/claudecode/jobs/health-check.md ~/.claude/jobs/
claude-job validate health-check
```

## The `claude-job` command

One command, used identically by you at the terminal and by the endpoint:

```
claude-job list [--json]                         # NAME KIND ENABLED STATUS LAST_RUN STALE MODEL DESCRIPTION, plus ignored files
claude-job validate <name> [--json]              # all errors and warnings, not just the first
claude-job validate --all [--json]
claude-job dry-run <name> [--input k=v …]        # prints validation, the composed settings.json and mcp.json,
                                                 # the result schema, tools, resolved notify channels per state,
                                                 # the assembled prompt and the exact command line. Spends nothing.
claude-job run <name> [--input k=v … | --input-file f.json] [--json]
claude-job force-run <name> [same options]       # the ONLY form that bypasses a disabled job; recorded as manual_forced
claude-job disable <name> | enable <name>        # create / remove the flag file state/disabled/<slug>
claude-job token show | token rotate             # endpoint bearer token (see "Triggering from Home Assistant")
claude-job --version
```

`--input date=2026-08-17` values are parsed as YAML scalars (`n=3` is an integer,
`flag=true` a boolean). `run` prints one summary line
(`health-check: warning — <headline> (run-… · 42.3s · $0.63 · row 11)`); with `--json` it
prints the `result` object that is also stored in `state/<name>.json`. `validate` and
`dry-run` exit 2 when the definition is invalid; `validate --json` emits
`{"name", "valid", "errors": [...], "warnings": [...]}` so a Claude-authored job can check
itself.

| Exit code | Meaning |
|---|---|
| `0` | the run completed with a finding (`ok`/`info`/`warning`/`critical` — a critical finding is a *successful* run), or a non-run verb succeeded |
| `1` | the run ended in `error` after validation passed (timeout, budget, max turns, no/invalid result, login expired, CLI preflight refused, broker failed to start), ended `aborted` because the Claude process was killed externally, or the runner itself crashed |
| `2` | invalid definition or input, unknown job, bad `--run-id`, usage error |
| `3` | skipped: another run of this job is in flight (overlap), the job is disabled, the concurrency wait expired, the add-on is stopping, or `force-run` was refused inside Claude |
| `143` | the runner itself received SIGTERM/SIGINT/SIGHUP (add-on stop, Ctrl-C) and published `aborted` |

With `--json`, `run` prints one JSON object on every path: the full `result` on completion,
or `{"run_id", "job", "status", "reason", "headline"}` when it stopped early (`overlap`,
`disabled`, `addon_stopping`, `unknown_job`, …).

**From inside the interactive Claude session** the following are pre-approved (no
permission prompt): `claude-job list`, `validate`, `dry-run`, `run`, `disable`.
`enable`, `force-run` and `token …` always prompt — disabling is fail-safe, re-enabling
undoes a human decision, and `force-run` refuses outright when invoked from inside Claude.
When Claude (rather than you at a shell prompt) runs `claude-job run <name>`, the run is
started **in the background** and the command returns immediately with
`{"accepted": true, "job": "<name>", "run_id": "run-…", "detached": true}`; follow it with
`claude-job list` or `~/.claude/jobs/state/<name>.json`. From a plain shell prompt `run`
stays in the foreground until the run finishes (Ctrl-C aborts it cleanly: the entity reads
`aborted`).

Runs are serialized: one Claude job process at a time across all jobs. A second job
triggered while one is running waits up to 60 s for the slot, then is recorded as
`skipped` (`concurrency_limit`). A second trigger of the *same* job while it is running
changes nothing visible — it is logged as `skipped_overlap` and the next result carries
`skipped_since_last: N`.

## What "read-only" means here

Read-only is enforced by construction, not by asking nicely. The run's tool universe is
shrunk to what the frontmatter implies (`Bash` and/or `Read,Grep,Glob`; write, edit, web
and sub-agent tools do not exist for the run); the only settings that apply are one file
the runner composes from an image-shipped deny baseline plus the job's own `tools:`/`paths:`
(your interactive "always allow" grants, `settings.json`, every `CLAUDE.md` and your MCP
registrations are all excluded); and the deny baseline — which beats any allow rule —
covers the add-on's own credentials and job state (`/homeassistant/.claudecode/**`),
`secrets.yaml`, `.storage/`, `.cloud/`, the recorder database, backups, `.git/`
directories, `/ssl`, `/data`, `/root`, `/proc` and the generated `claudecode_jobs.yaml`.
The job runs in an empty working directory with a scrubbed environment (only `HOME`,
`PATH`, locale, `TZ`, the two broker variables, and — if the add-on itself has them —
proxy/CA settings such as `HTTPS_PROXY`, `NO_PROXY`, `SSL_CERT_FILE`, `NODE_EXTRA_CA_CERTS`).

The Supervisor token never enters the job's process tree. Each run gets a short-lived
localhost broker that holds the token and forwards only a fixed table of read-only
Supervisor/Core API routes (info, stats, logs without follow, states, history, error log,
template rendering, `core check`); the job's `ha` command and hass-mcp talk to that broker
with a per-run nonce that is worthless afterwards. There is no route for restart, service
calls or any other mutation. The broker additionally strips other add-ons' `options` from
add-on info, removes token-bearing entity attributes (`access_token`, `entity_picture`,
`stream_source`, anything named like a password/token/API key) from states and history,
and accepts only hass-mcp's own fixed template query on the template-rendering route (any
other template is refused; template *output* is not filtered). Honest limits: the
*interactive* session is not a boundary (it is root in the same
container and can edit anything, including job files); and a job that reads
attacker-influenced text (logs, entity names) can be talked into writing misleading text
into its own headline — the deny baseline is what keeps that channel free of secrets.

## Result states

| State | Set by | Meaning | Default notification |
|---|---|---|---|
| `running` | runner | a run is in flight (attributes: `started_at`, `timeout_s`, `run_id`, `trigger`) | — |
| `ok` | the job | nothing for a human to look at | `state_only` (entity only) |
| `info` | the job | worth reading, no action needed | `persistent` |
| `warning` | the job, or escalation | a human should look; also what a clean result is raised to when a tool call was denied (`[denied: Bash×1] …`) or the run went over budget (`[over budget] …`) | `mobile, persistent` |
| `critical` | the job | look now | `mobile_critical, persistent` |
| `error` | runner | the run or the definition is broken: invalid definition/input (costs $0), timed out, budget cap, max turns, run failed, login expired, finished without submitting a result, or the runner died | `mobile, persistent` |
| `skipped` | runner | declined to start with nothing in flight: `concurrency_limit`, or a disabled job that has never run | `state_only` |
| `aborted` | runner | the platform interrupted the run: the add-on was stopped or restarted (this wins even if Claude had just finished — the raw envelope is still in the log), or the Claude process was killed externally (`reason: killed`); heals on the next run | `persistent` (an add-on stop itself sends nothing) |

A disabled job that is triggered keeps its last result state and only flips the `enabled:
false` attribute. Entity attributes on a finished run: `headline`, `detail` (first 900
characters, cut at a line boundary; the full text is in `state/<name>.json`),
`last_run`, `duration_s`, `cost_usd`, `run_count`, `model`, `enabled`, `stale_after`,
`run_id`, `session_id`, `reason`, `trigger`, `prev_status`, `skipped_since_last`,
`notify_status`, `attempts`, `metrics`. If the claude.ai login has expired the run is
retried once after ~10 s (a concurrent refresh from the terminal usually fixes it); a
second failure reads `claude.ai login expired — run /login in the terminal`.

**Add-on restarts abort in-flight runs** — no survival, no resume. A 600 s run cannot fit
any shutdown grace window, and re-running at the next schedule against fresh data is more
correct than resuming against stale data. The entity reads `aborted` until the next run.

## Cost

`fable` is the default by decision — the most capable model — so cadence and per-job
`model:` are the levers. Estimates, to be treated as lower bounds until your own
`logs/<name>.jsonl` says otherwise (every run records `cost_usd`):

| Job | Cadence | Model | Est. per run | Est. per month |
|---|---|---|---|---|
| health-check | daily | fable | $0.40 – $1.20+ | $12 – $36+ |
| energy-report | daily | fable | $0.30 – $0.90+ | $9 – $27+ |
| energy-report | daily | sonnet (as shipped) | a fraction of the above | |

Guards: `max_cost_usd` stops a run mid-flight; `max_turns` bounds loops; `min_interval`
stops a misfiring automation from multiplying runs; only one job runs at a time. A
mechanical report rarely needs fable's judgment — set `model: sonnet` or `model: haiku` in
its frontmatter, or change `job_default_model` for everything that does not pin a model.
The month's running total is kept in `state/_cost.json` (month boundaries in Home
Assistant's time zone) and published as `sensor.claude_jobs_cost_raw`.
(see "Entities" below).

## Claude Code updates and `cli_drift`

The isolation above is delivered by specific Claude Code flags (`--setting-sources`,
`--settings`, `--tools`, `--json-schema`, `--strict-mcp-config`), verified against the CLI
version the add-on image was built with. At every start, and before every run, a preflight
checks that the installed `claude --help` still lists them; if one is missing, jobs
**refuse to run** (`error`, reason `cli_preflight_failed`, $0) rather than run with a
weaker cage. With `auto_update_claude: true` the CLI can change at any boot without an
add-on release having re-verified it: flag *removal* still fails closed and loud, but a
change in a flag's *behaviour* under the same name would not be caught — you trade the
per-release verification for freshness. Every run records `claude_version` in the log
and the state file, so a regression is at least attributable.
`cli_drift: true` whenever the running CLI version differs from the one the image was
built with, plus `preflight: {"ok": …, "missing": […]}`.

## Logs, transcripts and retention

| What | Where | Retention |
|---|---|---|
| Last result (full `detail`, reason, cost, denials, validation errors) | `~/.claude/jobs/state/<name>.json` | overwritten by each run |
| Run history (one JSON line per run or skip: argv, exit code, envelope, judgment, cost) | `~/.claude/jobs/logs/<name>.jsonl` | pruned to the last 200 lines once over 1 MiB (daily backstop at 2 MiB) |
| Full CLI transcript of each run | `/homeassistant/.claudecode/projects/-data-claude-jobs-project/` | files older than 30 days deleted, then oldest-first down to 50 MiB |
| Monthly cost | `state/_cost.json`, archived to `logs/cost-<YYYY-MM>.json` at month rollover | kept |
| Endpoint / respawn diagnostics | the add-on log | Supervisor's rotation |

All of `~/.claude/jobs/` and the transcripts are inside your Home Assistant backups, which
is why the bounds exist. Transcripts contain everything the job read.
