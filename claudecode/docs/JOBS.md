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
/data/claude-jobs/claude-config/          # job-only CLAUDE_CONFIG_DIR (credentials symlinked to the
                                          # interactive session's; NOT in the HA config backup)
├── projects/-data-claude-jobs-project/   # run transcripts; kept 30 days / 50 MiB
│   └── <session>/tool-results/           # spooled large tool outputs; readable by the job, purged after each run
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

model: opus
  # optional · default: the add-on option job_default_model (ships "opus").
  # Must be one of: fable, opus, sonnet, haiku (aliases only).

timeout: 600
  # optional · seconds · default 600 · 30..3600. Wall clock for the Claude process;
  # on expiry it gets SIGTERM, then SIGKILL 15 s later, and the run is `error`.

max_cost_usd: 1.50
  # optional · default 1.00 · 0.01..5.00. A HARD mid-run stop: the run ends as
  # `error` "stopped at budget cap". On opus even a trivial run costs ~$0.08
  # (~$0.15 on fable), so values below that fail immediately.

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
  - Bash(ha supervisor info:*)
  - Bash(ha resolution info:*)
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
ha core check          ha supervisor info *     ha os info *      ha addons          ha backups
ha core info *         ha supervisor stats *    ha host info *    ha addons info *   ha backups info *
ha core stats *        ha supervisor logs *     ha host logs *    ha apps
ha core logs *         ha resolution info *                       ha apps info *
```

The info/stats/resolution verbs accept extra arguments so a job rule may end in `:*` and the
model may add `--raw-json` (read-only; JSON output is much easier for the model to use).
`ha core check` stays exact. Note the runtime sandbox is still an **exact/prefix match on the
job's own rules**: a job granted `Bash(ha supervisor info)` (exact) may not add flags, and no
job may ever append `2>&1`, `;`, or pipes — the shipped prompts and the job contract say so
explicitly, because every denied variation costs the run a turn.

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
  `get_history`/`get_statistics` instead.
- `mcp__homeassistant__get_error_log` is **dead on current HA Core** (2026.x removed
  `GET /api/error_log`, and hass-mcp's fallback path is not reachable either): it returns a
  404 error object on every call. `ha core logs --lines N` is the log source — the validator
  warns when a job still grants `get_error_log`.

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
| `health-check.md` | default (`opus`) | `ha core check`, resolution/supervisor info, recent error log, failing or unavailable integrations and entities, automations with errors; status by severity, counts in the headline; `stale_after: 93600`, `max_cost_usd: 1.50`, `paths: [/homeassistant/**]` |
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
shrunk to what the frontmatter implies (`Bash` only with a `Bash(ha …)` rule; write, edit,
web and sub-agent tools do not exist for the run — `Read,Grep,Glob` are always present, see
the spooling note below); the only settings that apply are one file
the runner composes from an image-shipped deny baseline plus the job's own `tools:`/`paths:`
(your interactive "always allow" grants, `settings.json`, every `CLAUDE.md` and your MCP
registrations are all excluded); and the deny baseline — which beats any allow rule —
covers the add-on's own credentials and job state (`/homeassistant/.claudecode/**`),
`secrets.yaml`, `.storage/`, `.cloud/`, the recorder database, backups, `.git/`
directories, `/ssl`, `/root`, `/proc`, the generated `claudecode_jobs.yaml`, and — item
by item — everything under `/data`: the endpoint token, `options.json`, and the job config
dir's credentials, transcripts and CLI state. `/data` is enumerated rather than covered by
one glob so that exactly one subtree stays readable (next paragraph).
The job runs in an empty working directory with a scrubbed environment (only `HOME`,
`PATH`, locale, `TZ`, `CLAUDE_CONFIG_DIR`, the two broker variables, and — if the add-on
itself has them — proxy/CA settings such as `HTTPS_PROXY`, `NO_PROXY`, `SSL_CERT_FILE`,
`NODE_EXTRA_CA_CERTS`).

**Large tool output (spooling).** Claude Code persists any tool result beyond a few KB to a
file under `<config dir>/projects/<cwd-slug>/<session>/tool-results/` and hands the model a
~2 KB preview plus the path; the CLI has no supported knob to raise that threshold or turn
it off (evaluated for this release — none exists). So jobs run with `CLAUDE_CONFIG_DIR`
pointed at a job-only config dir, `/data/claude-jobs/claude-config`, and every composed
settings file carries one fixed allow rule for
`…/claude-config/projects/**/tool-results/**` — that is how a job can read the full
`ha core logs --lines 400` or an 800 KB `get_history` result instead of a 120-line preview.
Nothing else widens: the config dir's `.credentials.json` (a **symlink** to the interactive
session's file, so a token refresh writes through to the one real store — never a copy),
its `*.jsonl` transcripts and all other CLI state are individually deny-listed, and the
runner purges the tool-results directory at the end of every run (and sweeps leftovers of
hard-killed runs before the next one).

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

`opus` is the default by decision — top-tier capability at half of `fable`'s per-token
price — so cadence and per-job `model:` are the levers. Estimates, to be treated as lower
bounds until your own `logs/<name>.jsonl` says otherwise (every run records `cost_usd`):

| Job | Cadence | Model | Est. per run | Est. per month |
|---|---|---|---|---|
| health-check | daily | opus | $0.20 – $0.60+ | $6 – $18+ |
| energy-report | daily | opus | $0.15 – $0.45+ | $4.50 – $13.50+ |
| energy-report | daily | sonnet (as shipped) | a fraction of the above | |

Guards: `max_cost_usd` stops a run mid-flight; `max_turns` bounds loops; `min_interval`
stops a misfiring automation from multiplying runs; only one job runs at a time. A
mechanical report rarely needs opus's judgment — set `model: sonnet` or `model: haiku` in
its frontmatter, or change `job_default_model` for everything that does not pin a model.
The month's running total is kept in `state/_cost.json` (month boundaries in Home
Assistant's time zone) and published as `sensor.claude_jobs_cost_raw`.
The statistics-grade entity is the package's `sensor.claude_jobs_monthly_cost`
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
`GET /jobs` (and with the package, `sensor.claude_jobs_attention`) carries
`cli_drift: true` whenever the running CLI version differs from the one the image was
built with, plus `preflight: {"ok": …, "missing": […]}`.

## Logs, transcripts and retention

| What | Where | Retention |
|---|---|---|
| Last result (full `detail`, reason, cost, denials, validation errors) | `~/.claude/jobs/state/<name>.json` | overwritten by each run |
| Run history (one JSON line per run or skip: argv, exit code, envelope, judgment, cost) | `~/.claude/jobs/logs/<name>.jsonl` | pruned to the last 200 lines once over 1 MiB (daily backstop at 2 MiB) |
| Full CLI transcript of each run | `/data/claude-jobs/claude-config/projects/-data-claude-jobs-project/` | files older than 30 days deleted, then oldest-first down to 50 MiB |
| Spooled large tool outputs | `…/-data-claude-jobs-project/<session>/tool-results/` | purged at the end of every run |
| Monthly cost | `state/_cost.json`, archived to `logs/cost-<YYYY-MM>.json` at month rollover | kept |
| Endpoint / respawn diagnostics | the add-on log | Supervisor's rotation |

All of `~/.claude/jobs/` is inside your Home Assistant backups, which is why the bounds
exist. Transcripts contain everything the job read; since they moved to the job-only
config dir under `/data` (so the job can read back its own spooled tool output — see
"What read-only means here") they are **no longer part of the HA config backup** and do
not survive an add-on uninstall.

## Notifications

After the entity is published, the result is handed to a small notifier that maps the
result state to channels and calls Home Assistant services through the Supervisor. It is
the only component that knows about phones; the job itself has no way to notify anyone.

| Channel | Mechanism | Notes |
|---|---|---|
| `persistent` | `persistent_notification.create` with `notification_id: claude_job_<slug>` | the same id **replaces** the sidebar notification in place; dismissed automatically when the job returns to `ok`. Message = headline, full `detail` (Markdown), run id, time and cost |
| `state_only` | nothing beyond the entity | `[state_only]` and `[]` mean the same |
| `mobile` | `notify.mobile_app_*` — every registered phone, or the `_notify.yaml` subset | title `Warning · <job>` (≤ 100 chars), message = headline (≤ 1000 chars); `tag: claude_job_<slug>` replaces the previous push for that job, `group: claude_jobs` threads them; tapping opens `dashboard_path` if configured |
| `mobile_critical` | same targets, one payload for both platforms | iOS critical alert (`push.sound.critical: 1`, plays through silent mode); Android `ttl: 0`, `priority: high`, channel "Claude Jobs Critical". See the Android note below |
| `notify_default` | `notify.notify` `{title, message}` | your default notify group; exists only once some notify platform is configured |
| `webhook` | `POST` the result as JSON to the URL in `_notify.yaml`; always sent last | body `{job, slug, status, headline, detail, run_id, ended_at, cost_usd, metrics, entity_id, prev_status}`; connection errors and 5xx are retried after 2 s and 5 s (4xx is not), within the notifier's 40 s overall budget (`error:webhook:deadline` when it runs out); a failing webhook never changes the job's state. Slack/Telegram/ntfy without the add-on knowing any of them |

**Fallback chain**, per send: `mobile` with no usable phone (none registered, or none of
the configured `targets` exists) → `notify_default` → (service absent) → `persistent`.
`mobile_critical` degrades down the same chain with the title prefixed `CRITICAL ·` — the
critical flag is lost, the word is not. Every hop is recorded in the entity's
`notify_status` attribute.

**Android: one manual step per phone.** Android does not let an app bypass Do Not
Disturb on its own. The first `mobile_critical` push creates a notification channel named
**"Claude Jobs Critical"** on the phone; open the Home Assistant app's notification
settings (Android Settings → Apps → Home Assistant → Notifications → Claude Jobs Critical)
and enable **Override Do Not Disturb** once. Until you do, critical results arrive as
ordinary high-priority notifications. The add-on log prints a reminder the first time it
sends a critical push for a job. iOS critical alerts need no setup beyond allowing
"Critical Alerts" for the Home Assistant app when iOS asks.

### `_notify.yaml`

Optional; without it every registered phone is a target and the channels above work with
their defaults. A commented example is refreshed at every start as
`~/.claude/jobs/_notify.example.yaml` (pristine copy: `/usr/share/claudecode/notify.example.yaml`):

```bash
cp /usr/share/claudecode/notify.example.yaml ~/.claude/jobs/_notify.yaml
```

```yaml
# ~/.claude/jobs/_notify.yaml — every key optional
mobile:
  targets: [mobile_app_pixel_8, mobile_app_iphone]   # default: all notify.mobile_app_* services
android:
  channel: Claude Jobs                 # channel for mobile pushes
  critical_channel: Claude Jobs Critical   # channel for mobile_critical (the one to grant DND override)
webhook:
  url: https://ntfy.sh/my-topic        # required before any job may name the `webhook` channel
  headers: {Authorization: "Bearer …"} # sent verbatim
  timeout_s: 10                        # 1..30 per attempt; all sends share one 40 s budget
dashboard_path: /lovelace/claude       # tapping a push opens this view (iOS url / Android clickAction)
severities:                            # install-wide per-state override, same shape as a job's notify:
  warning: [persistent]
# tts: / file:  — reserved names; accepted and ignored with a warning
```

Resolution order per state: the job's `notify:` → `_notify.yaml` `severities:` → the
built-in defaults ("Result states" table). Unknown keys in `_notify.yaml` are reported by
`claude-job validate` as warnings and otherwise ignored. **`webhook.headers` are plaintext
secrets on the volume**: jobs cannot read them (the whole jobs directory is in the deny
baseline), but the interactive session, anyone with file access, and your HA backups can.

### When you get notified again (dedupe and re-nag)

The notifier remembers the last state and a hash of the last headline per job:

- Severity changed (either direction) or the headline text changed → **full** notification
  on every mapped channel.
- Same state, same headline as last time → **quiet**: the persistent notification is
  refreshed in place, phones are not buzzed again. Confirmation is not news. This is why a
  job should put the number in its headline: "3 integrations failing" becoming "4" is a
  new headline and pushes again.
- Unchanged `critical` → re-pushed to phones every `renag_every` consecutive repeats
  (frontmatter, default 3, `0` = never re-nag).
- Back to `ok` after anything worse (**recovery**) → the earlier finding's sidebar
  notification is **dismissed** and the job's `ok` mapping is sent, with any phone channel
  in it delivering `Resolved: <headline>` (as an ordinary push, never a critical alert).
  With the default `ok: [state_only]` that means dismiss only; `notify_recovery: true`
  adds one `Resolved:` push to the phones when the `ok` mapping has no phone channel of
  its own.
- `error`, `skipped` and `aborted` never dedupe — infrastructure failures always follow
  their mapping — and they never dismiss an earlier finding's sidebar note (`error`/
  `aborted` replace it when `persistent` is in their mapping; `skipped` leaves it
  standing). Exception: a run aborted by an add-on stop or restart publishes the `aborted`
  entity but sends nothing (`notify_status: skipped_aborting`); it was almost certainly you
  restarting it.
- `webhook` is a data feed: it receives every result regardless of the above, after the
  other channels.

### Reading `notify_status`

The entity's `notify_status` attribute says exactly what happened, as `;`-separated
tokens: `sent:persistent,mobile(2)` (sent, with the number of phones),
`quiet:mobile,persistent` (deduplicated repeat: sidebar refreshed silently, pushes
suppressed), `fallback:mobile->notify_default`, `dismissed:persistent`,
`error:webhook:HTTP 500`, `error:webhook:deadline` (40 s budget exhausted),
`error:mobile:target mobile_app_x not found`, `state_only`, `skipped_aborting` (add-on was
stopping), `skipped_no_notifier`, `error:notifier_failed` (the notifier crashed or did not
answer within 45 s; the result itself is unaffected).

## Triggering jobs from Home Assistant

Set **`enable_job_endpoint: true`** and restart the add-on. At start it then:

- generates a bearer token once into `/data/claude-jobs/token` (64 hex characters, mode
  0600, outside your config directory and therefore outside HA backups and every job's
  reach). `claude-job token show` prints it in the terminal; `claude-job token rotate`
  replaces it — the endpoint reads the file on every request, so rotation is live;
- starts `claude-job-endpoint` on port **7682** under a supervising loop that restarts it
  if it dies. The port is not published to the host: it is reachable only on Home
  Assistant's internal add-on network — from HA Core, from other add-ons, and from inside
  this container — as `http://<add-on hostname>:7682`. The hostname is derived from the
  repository URL, `d7e97e69-claudecode` for this repository; confirm yours by running
  `hostname` in the add-on terminal. Every route except `/health` requires
  `Authorization: Bearer <token>`.

With the option off, `claude-job` in the terminal keeps working; only the endpoint, the
token and (next section) the generated package are gated.

| Route | Returns |
|---|---|
| `GET /health` (no auth) | `{"status": "ok" / "degraded", "jobs_dir", "token_set", "uptime_s", "version", "claude_version", "cli_preflight", "stopping"}` — booleans and versions only, never the token. `degraded` = CLI preflight failed, jobs directory missing, no token, or the add-on is stopping |
| `POST /jobs/<name>/run`, body `{}` or `{"input": {…}}` | `202 {"accepted": true, "job", "run_id"}` — the run was spawned; the *result* arrives via the entity, not this response. Benign declines are **200** so a scheduled trigger never errors its automation trace: `{"accepted": false, "reason": "disabled", "gate": "flag" / "frontmatter"}`, `{"accepted": false, "reason": "rate_limited", "retry_after_s": N}`. Errors: `401`, `404 unknown_job`, `400 bad_json`, `411 length_required` (POST bodies need a `Content-Length`; chunked is refused), `413` (body > 64 KiB), `422 invalid_input` (with `errors`) or `not_triggerable` (a `kind: action` file), `503 stopping` / `cli_preflight_failed` (with `missing`) / `no_token` |
| `GET /jobs` | the summary the anchor sensor polls: `attention` (= stale + failed count), `worst_status`, `stale_jobs`, `failed_jobs` (state `error`, plus any job whose state file is unreadable — listed with `status: "invalid_state"`), `disabled_jobs` (slugs), `running`, `job_count`, `cost_month_usd`, `cost_month_start`, `claude_version`, `built_claude_version`, `cli_drift`, `preflight`, `endpoint_version` (the add-on version), and `jobs: [{name, slug, description, status, headline, last_run, enabled, stale, stale_after, model, valid, run_count, cost_usd_last}]` |
| `GET /jobs/<name>/detail` | `text/markdown`: the untruncated `detail` of the last run (`204` if the job never finished one) |
| `POST /jobs/<name>/enable` · `/disable` | `{"job", "enabled": true / false}`; idempotent; `enable` on a job whose frontmatter says `enabled: false` removes the flag but answers `{"enabled": false, "reason": "frontmatter"}` |
| `POST /republish` (body `{}`) | `{"republished": N, "failed": M}` — re-posts every job entity from `state/*.json` plus the cost entity (used after an HA restart, which erases them) |

`<name>` accepts the hyphenated job name or its underscore slug. Every `POST` needs a
body with a `Content-Length` — with curl always pass `-d '{}'` (a bare `-X POST` gets
`411`). Quick check from the add-on terminal:

```bash
curl -s http://localhost:7682/health | jq
curl -s -H "Authorization: Bearer $(claude-job token show)" http://localhost:7682/jobs | jq '.attention, .jobs[].name'
curl -s -H "Authorization: Bearer $(claude-job token show)" -d '{}' http://localhost:7682/jobs/health-check/run
curl -s -H "Authorization: Bearer $(claude-job token show)" \
     -d '{"input": {"date": "2026-08-17"}}' http://localhost:7682/jobs/energy-report/run
```

### A schedule, with its off-switch (by hand)

With the generated package (see [Setting up the Home Assistant package](#setting-up-the-home-assistant-package-one-line-once)) none of this is needed. If you prefer to keep the endpoint token in `secrets.yaml` and wire the trigger by hand instead of including the package, this is the manual alternative — restart Home Assistant once after adding it (`rest_command` is only loaded at startup the first time; later edits need just Developer tools → YAML → *RESTful Command* reload):

```yaml
# secrets.yaml
claude_job_auth: "Bearer <paste the output of: claude-job token show>"
```

```yaml
# configuration.yaml — merge into an existing rest_command: block if you have one.
# Use exactly these two names: the generated package defines the same commands,
# so automations written against either keep working.
rest_command:
  claude_job_run:
    url: "http://d7e97e69-claudecode:7682/jobs/{{ job }}/run"
    method: post
    content_type: "application/json"
    headers:
      Authorization: !secret claude_job_auth
    payload: "{{ {'input': (input | default({}, true))} | to_json }}"
    timeout: 10
  claude_job_set_enabled:
    url: "http://d7e97e69-claudecode:7682/jobs/{{ job }}/{{ 'enable' if (enabled | default(true) | bool(true)) else 'disable' }}"
    method: post
    content_type: "application/json"
    headers:
      Authorization: !secret claude_job_auth
    payload: "{}"
    timeout: 10
```

Then create the schedule as an ordinary automation (Settings → Automations & scenes →
Create automation → ⋮ → Edit in YAML):

```yaml
alias: "Claude Jobs · health-check every morning"
mode: single
triggers:
  - trigger: time
    at: "07:15:00"
actions:
  - action: rest_command.claude_job_run
    continue_on_error: true    # add-on down = connection refused, which would otherwise abort the trace
    data:
      job: health-check
      # input: {date: "2026-08-17"}   # only for jobs that declare input:
```

**Pausing it — two layers, both honored:**

- *Schedule gate:* toggle the automation off in the UI. Free, native, and what you will
  usually reach for. Nothing triggers, nothing runs.
- *Hard gate:* `claude-job disable health-check` in the terminal, or from HA
  Developer tools → Actions: `rest_command.claude_job_set_enabled` with
  `data: {job: health-check, enabled: false}`. This creates the flag file
  `state/disabled/health_check`, which the runner checks on **every** path — schedule,
  run-now button, `curl`, terminal. A triggered-but-disabled job costs nothing: the
  endpoint answers `{"accepted": false, "reason": "disabled"}` without spawning, the entity
  keeps its last result with `enabled: false`, and the job appears in the anchor's
  `disabled_jobs`. Undo with `claude-job enable <name>` (or `enabled: true` in the action).
  Frontmatter `enabled: false` is honored the same way ("born disabled"); only
  `claude-job force-run` at the terminal overrides either.

### Rate limit, overlap, concurrency

- Per job, accepted triggers closer together than `min_interval` (default 60 s) are
  declined with `rate_limited` — cost protection against a misfiring automation, not
  security. The counter lives in memory and resets whenever the endpoint or add-on
  restarts. Terminal runs bypass it.
- There is deliberately no "already running" HTTP error: the runner's own lock decides. A
  trigger that overlaps a live run of the same job is logged as `skipped_overlap`, changes
  no entity, and shows up as `skipped_since_last` on the next result.
- Only one job runs at a time; others wait up to 60 s for the slot, then publish
  `skipped` / `concurrency_limit`. Stagger daily schedules by a few minutes rather than
  stacking them all at 07:00.

### `aborted`, and how stuck states heal

Stopping or restarting the add-on now signals in-flight job runs first: each receives
SIGTERM, stops its Claude process, publishes `aborted — add-on stopping` and exits within
about 4 s, while the interactive session shuts down in parallel; stragglers are killed
after 5 s. The whole stop sequence stays under 27 s, inside the Supervisor's 30 s limit. An
`aborted` entity is expected after every restart that interrupted a run — even one whose
Claude process had just finished, since a result judged mid-shutdown is not trustworthy;
the raw envelope stays in `logs/<name>.jsonl` — and is healed by the next run; no
notification is sent for it, and no cost is added to the month for it.

If a run dies without reporting at all (container killed, host power loss), its entity
would otherwise say `running` forever. The endpoint's background tick (every 60 s, and
once at every start) demotes any `running` state that is more than 2 minutes past its
deadline (`started_at + timeout`) with no live process to `error` — "runner died without
reporting" — and kills a process that is somehow still alive 3 minutes past its deadline.
The same tick re-posts results that could not be published because HA was down
(`published: false` in the state file) and, at most every 10 minutes, notices that HA
restarted and re-posts every job entity.

### `sensor.claude_jobs_endpoint`

This entity exists only while something is wrong with the endpoint itself; nothing ever
sets it to `ok`. `state: error` with attribute `reason`:

| `reason` | Meaning | Fix |
|---|---|---|
| `cli_preflight_failed` | the installed `claude --help` no longer lists one of the flags the job cage depends on (`missing` names them); every trigger gets `503` and no tokens are spent | usually an auto-updated CLI: set `auto_update_claude: false` and restart (the image's bundled CLI is known-good), or update the add-on |
| `respawn_degraded` | the endpoint crashed 5 times in a row within 10 s of starting; the loop now retries every 300 s (`fast_crashes` counts them) | read the add-on log for the Python traceback; report it |

Because it is a plain state-machine entity it disappears at the next Home Assistant
restart once the cause is fixed.

## Setting up the Home Assistant package (one line, once)

With `enable_job_endpoint: true`, every add-on start writes two files into your config
directory, with this add-on's hostname, port and token already filled in:

- `/homeassistant/claudecode_jobs.yaml` — a Home Assistant *package*: the REST commands,
  the anchor sensor, the cost sensor and three automations described below. Regenerated on
  every start; **do not edit it** (changes are overwritten). It contains the endpoint
  token — see the git note below.
- `/homeassistant/blueprints/automation/claudecode/schedule.yaml` — the scheduling
  blueprint (written once, re-installed whenever it differs from the shipped copy — so
  edits to that exact file are overwritten; duplicate it under another name to customise).
  It needs Home Assistant 2025.8 or newer (weekday selection on the time trigger).

Add exactly this to `configuration.yaml` and restart Home Assistant **once**:

```yaml
homeassistant:
  packages:
    claudecode_jobs: !include claudecode_jobs.yaml
```

(If you already organise packages with `packages: !include_dir_named packages`, do not add
a second `packages:` key; instead make the file visible inside that directory, e.g.
`ln -s ../claudecode_jobs.yaml /homeassistant/packages/claudecode_jobs.yaml`.) If you set
up the interim `rest_command:` block from the previous release, delete it first — the
package defines the same command names and Home Assistant rejects duplicate keys.

The restart is needed only the first time, because `rest`, `rest_command` and `template`
are not loaded until something configures them. After that the add-on keeps the file
current by itself: when a start re-renders it with different content (rotated token,
changed hostname) it also asks Home Assistant to reload RESTful commands and REST
sensors. Automations and the template sensor are not reloaded automatically; after an
add-on *update* that changed those parts, reload Automations/Template entities from
Developer tools → YAML or restart HA. You can re-render by hand at any time with
`python3 /usr/local/lib/claude-job/render_package.py` (add `--no-reload` to skip the
reload calls).

Honest statement: **without the package there is no anchor sensor and therefore no
alarm.** Jobs still run and per-job entities still appear, but those live only in Home
Assistant's state machine — they vanish at every HA restart until the add-on re-posts
them, and nothing would notice a job that silently stopped running.

## Entities

`claude_job_<slug>` entities are per job; `claude_jobs_*` entities are platform-level.

| Entity | Comes from | Purpose |
|---|---|---|
| `sensor.claude_job_<slug>` | posted by the add-on after every run; re-posted after HA restarts (on the `homeassistant` start event, and by a 10-minute canary) | the job's last result: state, `headline`, `detail` (900 chars), cost, timing, `enabled`, `notify_status`, `metrics` — dashboards and history. Best-effort across HA restarts (a gap of seconds to ≤ 10 min) |
| `sensor.claude_jobs_attention` | package REST sensor, polls `GET /jobs` every 60 s; registry-backed (`unique_id`), so it survives restarts | **the alarm anchor.** State = number of stale + failed jobs. `unavailable` = the endpoint is unreachable, which is itself the alarm condition. Attributes: `worst_status`, `stale_jobs`, `failed_jobs`, `disabled_jobs`, `running`, `job_count`, `cost_month_usd`, `cost_month_start`, `cli_drift`, `claude_version`, `preflight`, `endpoint_version` |
| `sensor.claude_jobs_cost_raw` | posted by the add-on | month-to-date spend (USD, HA's time zone); doubles as the add-on's "did HA restart and lose my entities?" canary. Long-term statistics are **not promised** for it |
| `sensor.claude_jobs_monthly_cost` | package template sensor mirroring the anchor's `cost_month_usd`; `device_class: monetary`, `state_class: total`, unit `USD`, `last_reset` = start of the month | the statistics-grade cost entity — use this one in the Energy-style statistics graphs and for budget automations |
| `sensor.claude_jobs_endpoint` | posted by the add-on only when the endpoint itself is broken | see "Triggering jobs from Home Assistant" |

A job is **stale** when it is enabled, declares `stale_after`, and its last *finished* run
(or, if it never ran, its file's modification time) is older than that — so a new daily
job you forgot to schedule raises the alarm about a day after you wrote it, and a job that
is running right now but has not finished within `stale_after` still counts. **Failed**
means state `error` (or a state file the endpoint cannot read, reported as
`invalid_state`); `warning` and `critical` are findings, i.e. successful runs, and notify
on their own. A job file too broken to parse is counted too — a broken file needs
attention.

**The shipped alarm** (the package automation "Claude Jobs · attention alarm") watches
only the anchor: attention above 0 for 5 minutes (message lists `stale_jobs` and
`failed_jobs`), or the anchor `unavailable`/`unknown` for 10 minutes (add-on stopped,
endpoint dead, stale token or hostname in the file). It re-checks 10½ minutes after every
Home Assistant start, because sidebar notifications do not survive a restart. Actions: a
persistent notification `claude_jobs_alarm`, then `notify.notify` (skipped harmlessly if
you have no notify platform — the trace shows an error for that last step only). A
companion automation dismisses the notification once attention has been back at 0 for 2
minutes, and a third one asks the add-on to re-post the per-job entities whenever Home
Assistant starts. All three are ordinary automations you can disable in the UI.

## Scheduling a job (the blueprint)

Settings → Automations & scenes → Blueprints → **"Claude Job · run on a schedule"** →
fill in the job name (`health-check`), a time, optionally the weekdays and an input
object → Save. The result is a normal automation whose own on/off toggle is the job's
schedule switch; the flag file (`claude-job disable`, or the disable button below) remains
the hard switch that every trigger path honors. One automation per job and time; for
several times a day create several, a few minutes apart from other jobs.

The blueprint appears in the list on a page refresh as soon as the file exists — no
restart. If an add-on update rewrites it, the add-on log says so; run Developer tools →
YAML → *Automations* reload to pick up the new version. The package also contains a
commented-out example schedule for those who prefer YAML; it is commented out because a
live daily run on the default model is a spending decision only you should make.

## Run now, and a dashboard

There are no button entities; "run now" is a stock dashboard button calling the package's
`rest_command.claude_job_run`. From Developer tools → Actions the same thing is:

```yaml
action: rest_command.claude_job_run
data:
  job: health-check
  # input: {date: "2026-08-17"}
```

An example view built from stock cards — a Markdown card listing every
`sensor.claude_job_*` with a footer comparing the count against the anchor's `job_count`
(so a re-post gap is visible rather than silent), an entities card for the anchor and the
monthly cost, and per-job run/disable buttons:

## Example dashboard (stock cards only)

Three stock cards make a complete Claude Jobs view; nothing here needs a custom card or HACS. Paste
each block into a dashboard via *Edit dashboard → Add card → Manual* (or into a YAML-mode view's
`cards:` list).

**1 · All jobs at a glance — a Markdown card.** It enumerates every `sensor.claude_job_*` entity the
add-on has published, and its footer compares that count with the anchor's `job_count` so a
republish gap after a Home Assistant restart is visible instead of silent (the entities come back
within a minute of the add-on noticing; `rest_command.claude_job_republish` forces it).

```yaml
type: markdown
title: Claude Jobs
content: |
  {%- set icon = {'ok': 'mdi:check-circle', 'info': 'mdi:information', 'warning': 'mdi:alert',
                  'critical': 'mdi:alert-octagon', 'error': 'mdi:close-circle', 'skipped': 'mdi:debug-step-over',
                  'aborted': 'mdi:stop-circle', 'running': 'mdi:progress-clock'} -%}
  {%- set jobs = states.sensor | selectattr('entity_id', 'match', 'sensor.claude_job_')
                 | sort(attribute='entity_id') | list -%}
  | | Job | Headline | Last run | Cost |
  |---|---|---|---|---:|
  {% for s in jobs -%}
  {% set a = s.attributes -%}
  {% set last = (as_timestamp(a.last_run, default=0) | timestamp_custom('%a %d %b %H:%M', true, default='—')) if a.last_run else '—' -%}
  | <ha-icon icon="{{ icon.get(s.state, 'mdi:help-circle') }}"></ha-icon> {{ s.state }} | {{ a.job | default(s.name, true) }}{{ ' *(disabled)*' if a.enabled is defined and not a.enabled else '' }} | {{ a.headline | default('—', true) }} | {{ last }} | {{ '$%.2f' % (a.cost_usd | float(0)) }} |
  {% endfor %}

  {% set expected = state_attr('sensor.claude_jobs_attention', 'job_count') | int(-1) -%}
  {{ jobs | count }} of {{ expected if expected >= 0 else '?' }} jobs shown
  {%- if expected >= 0 and jobs | count != expected %} — **some per-job entities are missing** (Home Assistant restarted and the add-on has not republished yet; it will within a minute, or run `rest_command.claude_job_republish`){% endif %}.
  {% set month = states('sensor.claude_jobs_monthly_cost') -%}
  This month: {{ '$' ~ month if month | is_number else '—' }}.
```

(Each table row must stay on one line — Markdown tables break on line wraps, which is why the card
uses a literal `|` block and Jinja `-%}` whitespace control.)

**2 · The platform sensors — an Entities card.** `sensor.claude_jobs_attention` is the alarm anchor
(its state is the number of jobs needing attention; `unavailable` means the endpoint is down) and
`sensor.claude_jobs_monthly_cost` is the statistics-grade spend sensor.

```yaml
type: entities
title: Claude Jobs · platform
entities:
  - entity: sensor.claude_jobs_attention
    name: Jobs needing attention
  - type: attribute
    entity: sensor.claude_jobs_attention
    attribute: worst_status
    name: Worst status
  - type: attribute
    entity: sensor.claude_jobs_attention
    attribute: claude_version
    name: Claude Code CLI
  - entity: sensor.claude_jobs_monthly_cost
    name: Spend this month
```

**3 · Per-job buttons — Button cards.** "Run now" and the hard kill switch are five lines of
*dashboard* config per job (edit them in the UI; no `configuration.yaml` change, no script or button
entities). Both call the package's generic `rest_command`s; replace `health-check` with your job's
file name (without `.md`).

```yaml
type: horizontal-stack
cards:
  - type: button
    name: Run health-check now
    icon: mdi:play-circle
    tap_action:
      action: perform-action              # dashboards older than 2024.8 spell this `action: call-service` + `service:`
      perform_action: rest_command.claude_job_run
      data:
        job: health-check
        # input: {date: "2026-08-17"}     # only for jobs that declare `input:` in their frontmatter
  - type: button
    name: Disable health-check
    icon: mdi:pause-circle
    tap_action:
      action: perform-action
      perform_action: rest_command.claude_job_set_enabled
      data:
        job: health-check
        enabled: false
      confirmation:
        text: Disable the health-check job? Scheduled triggers will be declined until it is enabled again.
  - type: button
    name: Enable health-check
    icon: mdi:play-pause
    tap_action:
      action: perform-action
      perform_action: rest_command.claude_job_set_enabled
      data:
        job: health-check
        enabled: true
  - type: entity
    entity: sensor.claude_job_health_check   # the job's result entity: state + headline attribute
    name: Last result
```

The disable button creates the add-on's flag file (`state/disabled/<job>`), which every trigger path
honours; the job's entity keeps its last result and shows `enabled: false`. The schedule
automation's own on/off toggle remains the everyday "schedule switch".


## Token rotation

```bash
claude-job token rotate     # new token, live immediately for the endpoint
```

then restart the add-on: the start re-renders `claudecode_jobs.yaml` with the new token
and reloads RESTful commands and REST sensors in Home Assistant, so nothing 401s. Without a
restart, run `python3 /usr/local/lib/claude-job/render_package.py` in the terminal for the
same effect. Until one of the two happens, triggers and the anchor poll fail with 401 and
the anchor goes `unavailable` (→ alarm after 10 minutes).

## Git-backed config directories

The rendered `claudecode_jobs.yaml` contains the bearer token, and it lives in
`/homeassistant` — inside your HA backups and inside any git repository you keep your
configuration in. The token is useless off your host (the endpoint is reachable only on the
internal add-on network) and one command rotates it, but it should still not be published.
What the add-on does about it:

- If `/homeassistant/.git` exists, the renderer adds `/claudecode_jobs.yaml` to
  `.git/info/exclude` (repo-local, untracked, same effect as `.gitignore`; your own
  `.gitignore` is never touched).
- If the file is **already tracked** — committed before that guard existed — no ignore rule
  helps. The add-on log then warns at every start:
  `claudecode_jobs.yaml is TRACKED by git: the endpoint token is in your repository history`.
  Fix: `claude-job token rotate`, `git rm --cached claudecode_jobs.yaml`, purge the file
  from history if the repository was ever pushed anywhere, restart the add-on.

Related, and not specific to jobs: if your config repository's remote URL embeds
credentials (`https://user:token@…` in `.git/config`), move them to a credential helper
outside the working tree. Jobs cannot read `.git/` (deny baseline), but the interactive
session and anything else with file access can.

## Monitoring Home Assistant itself (dead-man check)

Everything above runs *inside* Home Assistant, so none of it can tell you that Home
Assistant is down. The robust pattern is an external dead-man switch: a service such as
healthchecks.io (or Uptime Kuma, or an ntfy/cron pair on another machine) that alerts when
it *stops* hearing from you. Create a check there with a 10-minute period and a few
minutes' grace, then let HA ping it:

```yaml
rest_command:
  deadman_ping:
    url: "https://hc-ping.com/<your-check-uuid>"
    method: get
    timeout: 10

automation:
  - id: deadman_ping
    alias: "Dead-man ping · Home Assistant is alive"
    mode: single
    triggers:
      - trigger: time_pattern
        minutes: "/10"
    actions:
      - action: rest_command.deadman_ping
        continue_on_error: true
```

Silence — HA down, host down, network down — is what raises the alert, which is the one
property no in-HA automation can have. No add-on machinery is involved.

## Troubleshooting jobs

Where to look, in order: the entity's state and attributes (`reason`, `notify_status`,
`detail`); `~/.claude/jobs/state/<name>.json` (untruncated result, `validation_errors`,
`permission_denials`); `~/.claude/jobs/logs/<name>.jsonl` (one line per run: exit code,
judgment row, envelope, cost); the run transcript under
`/data/claude-jobs/claude-config/projects/-data-claude-jobs-project/`; the add-on log
(`[claude-job]`, `[claude-job-endpoint]`, `[claude-job-notify]`, `[ha-broker]` lines); and
`curl -s http://localhost:7682/health` from the add-on terminal.

| Symptom | Meaning / what to do |
|---|---|
| `sensor.claude_jobs_attention` is `unavailable` | HA cannot reach the endpoint: add-on stopped, `enable_job_endpoint` off, token rotated or hostname changed without a re-render (restart the add-on), or the endpoint is degraded (`/health`). The alarm fires after 10 min by design |
| `sensor.claude_jobs_attention` does not exist at all | the `packages:` include is missing, HA was not restarted after adding it, or HA rejected the package — Settings → System → Logs, search `claudecode_jobs` (a duplicate `rest_command` name from the interim setup is the usual cause) |
| job entity `error`, headline `runner died without reporting` (`reason: lost`) | the run's process disappeared without writing a result — container killed, host reboot, out of memory. Demoted by the tick; the next run heals it. Check the add-on log around `started_at` |
| `error` · `claude.ai login expired — run /login in the terminal` | the OAuth login is gone (one automatic retry already happened). Open the terminal, start `claude`, run `/login` |
| `error` · `stopped at budget cap $1.00` (`reason: max_budget`) | the run hit `max_cost_usd` mid-flight. Raise it (≤ 5.00), narrow the prompt, or use a cheaper `model:` |
| `error` · `hit max turns (50) before finishing` | raise `max_turns` (≤ 200) or narrow the job; keep `timeout ≥ max_turns × 10 s` |
| `error` · `timed out after 600s` | no result at all was produced. Raise `timeout` (≤ 3600) or find the slow tool call in the transcript |
| `error` · `completed without submitting a result` | the model finished without using the result tool — usually a `tools:` list too narrow to do the job, or a prompt that invites conversation. Read the transcript |
| `error` · `invalid definition: …` (`cost_usd: 0`) | run `claude-job validate <name>`; all errors are listed in the `validation_errors` attribute |
| `error` · `reason: cli_preflight_failed` | see `sensor.claude_jobs_endpoint` above; typically an auto-updated CLI |
| `warning` (or higher) with a `[denied: Bash×2]` prefix | the job attempted tool calls outside its `tools:`; the result is still valid, the prefix names the gap and `detail` lists the denied calls. Add the rule if the allow-list permits it, or adjust the prompt |
| `[over budget]` prefix | the final cost exceeded `max_cost_usd` slightly (the cap stops the *next* turn); informational |
| `skipped` · `concurrency_limit` | another job held the single run slot for more than 60 s; stagger the schedules |
| `aborted` | the add-on was stopped or restarted mid-run (or the process was killed externally); the next run heals it; no notification is sent for add-on stops |
| per-job entities missing after an HA restart | expected briefly; the package's start automation re-posts them about 30 s after start, the canary within 10 min. `rest_command.claude_job_republish` (Developer tools → Actions) forces it |
| `GET /jobs` lists a job with `status: "invalid_state"` | its `state/<name>.json` is corrupt (counts as failed); the next run rewrites it, or delete the file |
| every `curl -X POST` to the endpoint answers `411` | send a body: `-d '{}'` |
| a run was triggered but nothing happened | check the automation trace: `{"accepted": false, "reason": "disabled"}` (flag file or `enabled: false`), `"rate_limited"` (`min_interval`), or connection refused (endpoint down). `claude-job list` shows ENABLED and STATUS |
| no phone notification | read the entity's `notify_status` (`quiet:` = deduplicated repeat, `fallback:` = no phone target found, `error:` = service call failed); check `_notify.yaml` `mobile.targets`; for critical alerts on Android grant the DND override |
| blueprint not in the list | refresh the page; the file must exist at `/homeassistant/blueprints/automation/claudecode/schedule.yaml` (written only with `enable_job_endpoint: true`) |
| add-on log: `claudecode_jobs.yaml is TRACKED by git` | see "Git-backed config directories": rotate the token and remove the file from the repository history |
