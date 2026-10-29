# Claude Code for Home Assistant

Run [Claude Code](https://docs.anthropic.com/en/docs/claude-code), Anthropic's AI-powered coding assistant, directly in your Home Assistant sidebar with full access to your configuration.

> Fork of [robsonfelix/robsonfelix-hass-addons](https://github.com/robsonfelix/robsonfelix-hass-addons). Fork-only releases use a `-con.N` suffix on the upstream version (e.g. `1.2.63-con.1`).

## Quick Start

```bash
claude "List all my automations"
claude "Turn off all lights in the living room"
claude "Create an automation to turn on lights at sunset"
claude "Why isn't my motion sensor automation working?"
```

## Requirements

- Home Assistant OS or Supervised installation
- [Anthropic account](https://console.anthropic.com/) (authentication handled in terminal)

## Features

- **Web Terminal**: Access Claude Code through a browser-based terminal
- **Config Access**: Read and write Home Assistant configuration files
- **hass-mcp Integration**: Direct control of HA entities and services
- **Session Persistence**: Optional tmux integration to preserve sessions across page refreshes
- **Customizable Theme**: Choose between dark and light terminal themes
- **Multi-Architecture**: Supports amd64, aarch64, armv7, armhf, and i386
- **Secure Authentication**: Claude Code handles its own authentication securely
- **Git-backed config**: create an empty private repository and ask Claude to set it up — see [Version-controlling your configuration](#version-controlling-your-configuration-git)
- **Scheduled jobs**: Unattended, read-only Claude runs (health checks, reports) that Home Assistant triggers on a schedule and that report back as entities and notifications — see [Scheduled jobs](#scheduled-jobs-claude-jobs)

## Setup

### 1. Install the Add-on

1. Add the repository to Home Assistant
2. Install the "Claude Code" add-on
3. Start the add-on
4. Open the Web UI from the sidebar

### 2. Authenticate with Claude Code

On first launch, Claude Code will prompt you to authenticate:

1. Open the terminal from the HA sidebar
2. Type `claude` to start
3. Follow the authentication prompts
4. Your credentials are stored securely by Claude Code

**Note**: The add-on does NOT require you to enter API keys in the configuration. Claude Code handles authentication itself, storing credentials securely in its own configuration directory. This is more secure than storing keys in Home Assistant's add-on config.

## Using Claude Code

### Basic Usage

Once authenticated, Claude Code is ready to help with:

- Editing Home Assistant YAML configurations
- Creating automations and scripts
- Debugging configuration issues
- Writing custom integrations

### Home Assistant Integration

With hass-mcp enabled, Claude can:

- Query entity states: "What's the temperature in the living room?"
- Control devices: "Turn off all lights in the bedroom"
- List services: "What services are available for climate control?"
- Debug automations: "Why didn't my morning routine trigger?"

### Example Commands

```bash
# Start interactive session
claude

# One-off commands
claude "Add a new automation that turns on the porch light at sunset"
claude "Check my configuration.yaml for errors"
claude "List all unavailable entities"

# Continue previous conversation
claude --continue
```

### Keyboard Shortcuts

| Shortcut | Command |
|----------|---------|
| `c` | `claude` |
| `cc` | `claude --continue` |
| `ha-config` | Navigate to config directory |
| `ha-logs` | View Home Assistant logs |

## Configuration Options

Options are on the add-on's **Configuration** tab: **Settings → Apps → Claude Code → Configuration**, or directly at `/config/app/<slug>/config` on your Home Assistant (the slug is the add-on's ID, shown in the address bar of its page). From the web terminal, the gear in the top right corner takes you there. Most options take effect after the add-on restarts.

| Option | Description | Default |
|--------|-------------|---------|
| `enable_mcp` | Enable HA integration via hass-mcp | true |
| `enable_playwright_mcp` | Enable Playwright MCP (requires the [Playwright Browser](../playwright-browser/) add-on) | false |
| `playwright_cdp_host` | Override Playwright Browser hostname (auto-detected from Supervisor when blank) | "" |
| `terminal_font_size` | Font size (10-24) | 14 |
| `terminal_theme` | dark or light | dark |
| `working_directory` | Start directory | /homeassistant |
| `session_persistence` | Use tmux for persistent sessions | true |
| `auto_update_claude` | Install the newest release of `claude_update_channel` on startup (rolls back automatically if the new release cannot run on this host). Rarely needed: Claude Code updates itself in the background, and updates survive restarts. Writes to the container layer — see changelog 1.2.63-con.10 | false |
| `claude_update_channel` | Which Claude Code releases to follow: `stable` (typically about a week behind, skipping releases with major regressions) or `latest` (every release as it ships). Applies to background updates, `auto_update_claude` and the release brought back after a restart. See [Claude Code releases and updates](#claude-code-releases-and-updates) | stable |
| `apply_claude_updates` | Claude Code installs its own updates in the background, but a running session keeps the old release until it restarts. With this on, an idle session moves onto the installed update by itself and resumes the same conversation. See [Updates to running sessions](#updates-to-running-sessions) | true |
| `default_permission_mode` | Default Claude Code permission mode: `default`, `auto`, `acceptEdits`, `plan`, or `bypassPermissions`. `auto` uses a classifier to approve/deny prompts (Claude Code v2.1.83+, Max/Team/Enterprise/API plan, Sonnet 4.6+/Opus 4.6+); falls back silently to `default` if requirements aren't met | auto |
| `auto_launch_claude` | Auto-run Claude when the terminal opens: `off` (bash prompt), `new` (`claude`), or `continue` (`claude --continue`). Drops to a login shell when Claude exits. With `enable_remote_control` on, the session is started at container boot instead (see Remote Control below) | continue |
| `enable_remote_control` | Turn on [Remote Control](https://code.claude.com/docs/en/remote-control) for all Claude sessions (sets `remoteControlAtStartup` in settings.json and passes `--remote-control` to auto-launches) so you can view and steer sessions from claude.ai/code or the Claude mobile app. Requires Claude Code v2.1.51+, claude.ai OAuth login (not API key), and a Pro/Max/Team/Enterprise plan. See the security note below | false |
| `remote_control_session_prefix` | Prefix for auto-generated Remote Control session names shown in claude.ai/code and the mobile app (e.g. `HomeAssistant-graceful-unicorn`). With `remote_control_server` on, it is also the title of the server's own session. Applies when `enable_remote_control` or `remote_control_server` is on | HomeAssistant |
| `remote_control_server` | Also run a Remote Control server, so new sessions can be started on this box from claude.ai/code or the Claude app. See [Remote Control](#remote-control) | false |
| `extra_npm_packages` | List of npm package specs to install at every container start under `/homeassistant/.claudecode/npm-global` (persistent across add-on rebuilds). Bin dir is on `PATH`. Use to add custom MCP servers without forking the Dockerfile. | `[]` |
| `job_default_model` | Model alias used by scheduled jobs (`~/.claude/jobs/*.md`) that do not set `model:` themselves: `fable`, `opus`, `sonnet` or `haiku`. `fable` is the most capable and the most expensive; a job can pin a cheaper model in its own frontmatter. See [Scheduled jobs](#scheduled-jobs-claude-jobs) | opus |
| `enable_job_endpoint` | Start the job trigger endpoint (port 7682, add-on network only, bearer token), generate its token on first start, and write the `claudecode_jobs.yaml` package into your config directory on every start. Off = jobs can still be run from the terminal with `claude-job`, but Home Assistant cannot trigger or watch them | false |
| `enable_usage_sensors` | With `enable_job_endpoint` on, poll your **Claude subscription usage** (5-hour session, weekly, weekly per-model, extra usage, weekly breakdown by surface) and expose it as `sensor.claude_usage_*` in the generated package. Whole-account figures. The login token is only read, never refreshed — see [Claude subscription usage](#claude-subscription-usage) | true |
| `usage_poll_interval` | Seconds between polls of `api.anthropic.com` for the usage figures (and Home Assistant's polls of the add-on for them): 60–3600 | 300 |
| `usage_keepalive_job` | Optional: the name of a job (the shipped `usage-keepalive`) the endpoint runs at most once an hour while the claude.ai access token has *expired*, so the CLI itself renews it and the usage sensors stay fresh on an idle install. One small haiku call per run. Empty = off | "" |
| `debug_logging` | Trace every startup command into the add-on log (`bash -x`, timestamped). Turn on when the add-on fails to boot and the log doesn't say why; the boot log also always shows a `step:` marker per startup step, so the last one printed names where it stopped. Turn off afterwards — very verbose | false |

### Playwright MCP setup

If you set `enable_playwright_mcp: true`:

1. Install and start the [Playwright Browser](../playwright-browser/) add-on. Order doesn't matter — the hostname is resolved every time Claude spawns the MCP server, so installing Playwright Browser after Claude Code is fine (no restart needed).
2. To pin the hostname (e.g. force a specific Playwright Browser instance), set `playwright_cdp_host` to the value reported by `curl -s -H "Authorization: Bearer $SUPERVISOR_TOKEN" http://supervisor/addons | jq -r '.data.addons[] | select(.slug | test("playwright-browser")) | .hostname'`. When set, this overrides auto-detection.

If Claude reports the playwright MCP server failing to start, check the `[playwright-launch]` lines in Claude's MCP error log — the wrapper exits with a clear message if Playwright Browser isn't installed.

#### Authenticating the browser to Home Assistant

A fresh Playwright session has no HA login cookies, so navigating to `http://homeassistant:8123/` lands on the login screen. You won't see the dashboard until the browser is authenticated.

The cleanest fix is HA's `trusted_networks` auth provider — it skips the login flow for requests coming from the add-on Docker subnet (where Playwright Browser runs). Add to `configuration.yaml`:

```yaml
homeassistant:
  auth_providers:
    - type: homeassistant
    - type: trusted_networks
      trusted_networks:
        - 172.30.32.0/23   # HA add-on network
      trusted_users:
        172.30.32.0/23:
          - <your-user-id>  # from Settings > People (Users) > click the user > ID at the bottom
      allow_bypass_login: true
```

Restart HA. Now every request from the playwright-browser container is silently authenticated as that user — Claude can navigate the dashboard, take screenshots, exercise frontend JS, etc.

**Security note:** this trusts *every* add-on on that subnet. Fine on a single-user HA where you control all the add-ons; reconsider if you run untrusted add-ons.

If you only need API access (entity state, service calls, history), the existing `hass-mcp` server already handles that with the `SUPERVISOR_TOKEN` — no browser, no trusted_networks.

### Custom MCP servers (`extra_npm_packages`)

Add-on updates rebuild the container and wipe anything you `npm install -g` manually. `extra_npm_packages` re-installs a list of packages on every container start, to a persistent path on the HA volume.

**Install:**

1. Add packages to the option in the add-on Configuration tab:
   ```yaml
   extra_npm_packages:
     - "@modelcontextprotocol/server-filesystem"
     - "some-other-mcp@1.4.2"
     - "@vendor/mcp"
   ```
2. Restart the add-on. Watch the log for `[INFO] Installing extra_npm_packages: ...`.
3. Open a terminal in Claude Code and register the MCP server:
   ```bash
   claude mcp remove filesystem -s user 2>/dev/null
   claude mcp add-json filesystem '{"command":"mcp-server-filesystem","args":["/homeassistant"]}' -s user
   ```
   The binary lives at `/homeassistant/.claudecode/npm-global/bin/<bin-name>` and that directory is on `PATH`, so referencing it by name works.

The MCP registration itself is persistent (stored in `~/.claude/settings.json`, which is symlinked to the HA volume), so step 3 is one-time.

**Caveats:**
- Re-installs on every container start; unpinned packages get the latest each time.
- A bad package name logs `[WARN]` but doesn't stop the add-on.
- Don't know the binary name? Run `ls /homeassistant/.claudecode/npm-global/bin/` after install.

## File Locations

| Path | Description | Access |
|------|-------------|--------|
| `/homeassistant` | HA configuration directory | read-write |
| `/share` | Shared folder | read-write |
| `/media` | Media folder | read-write |
| `/ssl` | SSL certificates | read-only |
| `/backup` | Backups | read-only |
| `/homeassistant/.claudecode/jobs` | Claude Jobs: definitions (`*.md`), `state/`, `logs/` (= `~/.claude/jobs`) | read-write |

## Customizing Claude's instructions

The add-on writes `~/.claude/CLAUDE.md` on every start with container-specific
context (path mapping, how to reach Home Assistant, log commands). **It is
regenerated each restart — don't edit it directly.**

For your own persistent instructions, edit **`~/.claude/CLAUDE.user.md`**. The
add-on creates this file once and never overwrites it; it's imported by
`~/.claude/CLAUDE.md`, so anything you put there loads into every Claude Code
session and survives restarts and add-on updates. Good for project
conventions, frequently-referenced entity IDs, or reminders about your setup.

## Version-controlling your configuration (git)

Keeping `/homeassistant` in a private git repository gives every change Claude makes a
diff, a rollback and an off-box copy. To set it up:

1. Create an **empty, private** repository on GitHub (or any git host).
2. Create a **fine-grained personal access token** for that one repository, with
   *Contents: read and write*.
3. Ask Claude: *"Set up git backup of my config to &lt;repository URL&gt;"*, and give it
   the token when it asks.

Claude does the rest from `/usr/share/claudecode/git/README.md`: a whitelist-style
`.gitignore` (`.storage/` and `.claudecode/` are full of tokens and password hashes, so
only known-safe files there are tracked), a pre-commit hook that refuses credentials,
git-ignored storage dashboards and files that do not parse. It shows you what the first
commit would contain before making it. After that, Claude commits each change it makes
with a descriptive message; an optional weekly `shell_command` backup catches UI edits.
Put your own rules for that workflow in `CLAUDE.user.md`. The jobs package token is kept
out of git automatically — see [docs/JOBS.md](docs/JOBS.md#git-backed-config-directories).

`ha-ws` (in the terminal) sends commands to Home Assistant's WebSocket API, for changes
that must not be made by editing `.storage/` while HA runs:
`ha-ws '{"type": "config/entity_registry/update", "entity_id": "light.a", "new_entity_id": "light.b"}'`.

## Scheduled jobs (Claude Jobs)

A **job** is an unattended, read-only Claude run — a Markdown file in
`~/.claude/jobs/` (frontmatter: model, tools, bounds, notify map; body: the
prompt) that the add-on executes headlessly, isolated from your terminal
session, and turns into a `sensor.claude_job_<name>` entity plus a notification
chosen by severity. Home Assistant owns the schedule; the add-on executes; a
person acts on the result. Two examples ship: `health-check` and `energy-report`.

```bash
claude-job list                  # definitions and their last result
claude-job validate --all        # check every job file
claude-job dry-run health-check  # exactly what would run, spends nothing
claude-job run health-check      # run it now; result in ~/.claude/jobs/state/health-check.json
```

The full reference — writing jobs, the `claude-job` command, what "read-only"
means, result states, notifications, triggering from Home Assistant, cost —
is in [`docs/JOBS.md`](docs/JOBS.md); the design and its security argument are
in [`docs/DESIGN-claude-jobs.md`](docs/DESIGN-claude-jobs.md).

## Claude subscription usage

With `enable_job_endpoint: true` (and `enable_usage_sensors` left on) the add-on also polls
the same usage figures the Claude Code `/usage` screen shows — your **whole claude.ai
subscription**, every surface that shares it (Claude Code here and elsewhere, chats, Cowork,
…) — every `usage_poll_interval` seconds, and the generated `claudecode_jobs.yaml` package
turns them into registry entities:

| Entity | What it is |
|---|---|
| `sensor.claude_usage_session` | 5-hour window used, % (attributes `resets_at`, `severity`, `is_active`) |
| `sensor.claude_usage_weekly` | 7-day window used, % (all models); attribute `breakdown` = `{claude_code, chats, cowork, other}` — each surface's **share of this week's usage** (they sum to 100, not % of the cap), plus `breakdown_rows` as the API reports them |
| `sensor.claude_usage_weekly_model` | the per-model 7-day cap (attribute `model`, currently "Fable"); it can run ahead of the all-model figure |
| `sensor.claude_usage_session_reset` · `_weekly_reset` · `_weekly_model_reset` | when each window resets (`device_class: timestamp`) |
| `sensor.claude_usage_weekly_claude_code` · `_chats` · `_cowork` · `_other` | the weekly breakdown by surface as plain sensors: each surface's share of this week's usage in % ("Claude Code 96 %" means 96 % of what was used this week, not 96 % of the limit); each `unavailable` while the API reports no row of that name |
| `sensor.claude_usage_extra_used` · `sensor.claude_usage_extra_spend` | extra (pay-as-you-go) usage: % of the monthly limit, and the amount spent this month in major units (the API reports cents; 241 → 2.41). Attributes `is_enabled`, `monthly_limit`, `currency`, `disabled_reason` |
| `sensor.claude_usage_last_success` | when the figures were last fetched; attributes `stale`, `last_error`, `last_error_detail`, `age_s`, `credential_expires_at`, `subscription_type` |
| `binary_sensor.claude_usage_stale` | **on** when the numbers above are not fresh: the last poll failed and the last good values are being shown, or nothing has been fetched yet |

Percent sensors are `state_class: measurement`, so history graphs and statistics work. The
poll is gentle (one request per interval, single flight, 10 s timeout, never on the job
tick's own thread) and failures are logged once per kind, not every tick.

**How the login token is handled — read this once.** The poll authenticates with the
claude.ai access token the Claude Code CLI keeps in its credential store. The add-on only
*reads* it — it never refreshes it, because the refresh token rotates and a second
refresher would race the CLI and could log the add-on out. The CLI renews the token only
when it makes a model call, and an access token lives about eight hours; on an install where
the terminal session sits idle, the token therefore expires, `api.anthropic.com` answers 401,
and the add-on keeps serving the **last good values** with `binary_sensor.claude_usage_stale`
on and `last_error: unauthorized` (the values never drop to zero). Anything that makes the CLI
call the model heals it: typing in the terminal, or any scheduled job. If you want the sensors
fresh on an otherwise idle install, use the shipped `usage-keepalive` job — one haiku turn,
no tools, ~nothing per run — either on a schedule (the "Claude Job · run on a schedule"
blueprint, every 6 hours is plenty) or automatically via `usage_keepalive_job:
usage-keepalive`, which runs it at most hourly and only while the token has actually
expired (a 401 on an unexpired token means the login was revoked: run `/login` in the
terminal). Existing installs get the job file with `cp
/usr/share/claudecode/jobs/usage-keepalive.md ~/.claude/jobs/`. The token itself never
reaches Home Assistant, the package file, or the add-on log; only the derived numbers do.

Turn the feature off with `enable_usage_sensors: false`: the next start renders the package
without the usage block (delete the orphaned entities under Settings → Devices & services
→ Entities). The endpoint answers `GET /usage` with `404 usage_disabled`. The upstream
endpoint is undocumented and its shape may change; unknown fields are ignored and a body
without any usage window is treated like a failed poll (last good values, stale).

## Session Persistence

When `session_persistence` is enabled, the add-on uses tmux to maintain your terminal session. This means:

- Your session survives browser refreshes
- You can disconnect and reconnect without losing context
- Claude Code conversations are preserved
- With `enable_remote_control` on, the session is created at boot so Remote Control works without a browser

### Claude Code releases and updates

`claude_update_channel` picks which releases Claude Code follows: `stable`, the default, or `latest`. The add-on writes it to `autoUpdatesChannel` in settings.json at every start, so change it in the add-on options, not in settings.json.

- **The image.** Each build installs the current stable release. Nothing is pinned: a rebuild or add-on update picks up whatever the channel serves then, and reuses the cached install when the channel has not moved. The build cannot read add-on options; to build from another channel or hold back a version, set the `CLAUDE_CODE_VERSION` build argument in `build.yaml` under `args:` (`stable`, `latest`, or an exact version such as `2.1.308`).
- **While running.** Claude Code updates itself in the background on the chosen channel.
- **Across restarts.** Every restart recreates the container, which used to throw that update away. Now the add-on remembers the release in use and reinstalls it at the next start from npm's cache in `/data/npm-cache`, without the network. That takes a few seconds. A release that does not run after the restart is rolled back to the image's release and not tried again. The add-on log shows each step.
- **Changing the channel.** The next start drops the remembered release and starts from the image's release; Claude Code then follows the new channel.
- **The cache.** It keeps only the packages of the release in use (about 130 MB) and is left out of Home Assistant backups. Claude Code itself still runs from the container, not from `/data`, and the AppArmor profile is unchanged. `npx` keeps the packages it runs in the same cache; the add-on clears that area at every start, so as before nothing `npx` fetched outlives a restart. Anything else npm cached, such as `extra_npm_packages`, is dropped at the next start once it is more than 30 days old, and fetched again when needed.

### Updates to running sessions

Claude Code updates itself in the background, but each running session keeps executing the release it started with. On an unattended box nobody restarts it, so the session falls behind indefinitely. With `apply_claude_updates` on, the default, the add-on moves an idle session onto the installed release:

- **When it counts as idle.** No turn is running, and nothing has been printed or typed in the pane for 15 minutes. Hooks in `settings.json` record when each turn starts and ends. An interrupted turn records no end, so a pane quiet for an hour counts as idle anyway. A permission question left unanswered for an hour looks the same: it is dropped by the restart, and you ask again in the resumed conversation.
- **What happens.** The session gets SIGTERM and relaunches straight away on the same conversation, with the same flags. The add-on log says which pane moved and onto which release.
- **What is left alone.** A release only gets moved onto once it answers `claude --version`, so one that cannot run on this host never takes a working session down. Sessions you started by hand from a shell are left alone too, because nothing would relaunch them.

The same hooks make a crash relaunch resume the pane's own conversation rather than the most recent one in the directory, which matters when several tmux windows run Claude. Claude Jobs never load these hooks.

Needs `session_persistence` and `auto_launch_claude` set to `continue` or `new`. Existing installs get the three hooks written into `settings.json` at the next start; if you have your own `SessionStart`, `UserPromptSubmit` or `Stop` hooks, ours sit beside them and yours keep running. Turning the option off removes the add-on's hooks at the next start; your own hooks stay.

### tmux Commands

If you're new to tmux:

| Key | Action |
|-----|--------|
| `Ctrl+b d` | Detach from session (keeps it running) |
| `Ctrl+b [` | Enter scroll/copy mode (use arrow keys) |
| Mouse wheel | Scroll up/down (auto-enters copy mode) |
| `q` | Exit scroll/copy mode |

### Customizing tmux

Anything you put in `/homeassistant/.claudecode/tmux.conf` is loaded last and overrides the defaults. That file lives in your config directory, so it survives restarts, rebuilds and reinstalls:

```bash
echo 'set -g mouse off' > /homeassistant/.claudecode/tmux.conf
```

Turning the mouse off restores native browser selection and copy/paste while keeping persistent sessions. Alternatively set `session_persistence: false` to drop tmux entirely.

### Copy and Paste in tmux

Since tmux captures mouse events, copy/paste works differently:

| Action | How to do it |
|--------|--------------|
| **Copy (tmux copy-mode)** | Scroll up (enters copy-mode), select, then copy — lands directly on your clipboard¹ |
| **Copy (macOS)** | Hold `Option (⌥)` while selecting text with the mouse |
| **Copy (Windows/Linux)** | Hold `Shift` while selecting text with the mouse |
| **Paste** | `Shift+Insert` or middle-click |
| **Alternative paste** | `Ctrl+Shift+V` (browser dependent) |

¹ Clipboard integration (OSC 52) requires accessing Home Assistant over **HTTPS or localhost** — browsers block programmatic clipboard writes on plain HTTP. On plain HTTP, use the modifier-key gestures below instead. A bonus of OSC 52: copy-mode copies wrapped lines as one logical line, so long URLs come out intact.

Holding the modifier makes the browser terminal handle the selection natively instead of passing the mouse to tmux; the selection is copied to your clipboard automatically the moment you release the button — watch for the brief ✂ icon as confirmation.

**Note**: Regular right-click paste and simple mouse selection won't work because tmux intercepts these events for scrolling.

#### Authenticating Claude Code (first launch)

The simplest path works everywhere, including plain HTTP: **click the URL**. The login screen prints it as a real hyperlink (OSC 8), so even though it spans several rows, clicking *any* row opens the **complete** URL in a new tab after a confirmation dialog.

If you access Home Assistant over **HTTPS** (or localhost), the CLI's own **press `c` to copy** hint also works — the login URL lands directly on your clipboard via OSC 52, ready to paste into a new tab. On plain HTTP the browser blocks programmatic clipboard writes, so `c` does nothing there — use the click.

Avoid selecting the login URL with `Shift`/`Option`-drag: the login screen paints each row as a separate line, so the selection stitches the fragments together with line breaks. If you really need the URL as text, **zoom out** your browser (`Ctrl + -` or `Cmd + -`) until it fits on a single line first.

Either way: complete authentication in the browser, **copy the auth code**, click back on the terminal, and **paste** it with `Shift+Insert` or `Ctrl+Shift+V`.

### Scrolling and Session Persistence Trade-offs

**With tmux (`session_persistence: true`):**
- ✅ Session survives browser refresh/disconnect
- ✅ Can detach and reattach to running sessions
- ✅ Long-running Claude tasks continue in background
- ✅ Mouse wheel scrolling works (enters copy mode automatically)
- ✅ 20,000 line scrollback buffer
- ✅ Copy by holding `Shift` (Windows/Linux) or `Option ⌥` (macOS) while selecting
- ✅ Copy-mode copies and `claude`'s "press `c` to copy" go straight to your clipboard (HTTPS/localhost only)
- ✅ Hyperlinks (OSC 8), like the wrapped `/login` URL, open complete with a single click
- ⚠️ Use middle-click or Shift+Insert to paste (right-click paste may not work)

**Without tmux (`session_persistence: false`):**
- ✅ Native browser scrolling
- ✅ Simpler terminal behavior
- ✅ Standard copy/paste behavior
- ❌ Session lost on browser refresh
- ❌ Session lost if add-on restarts

**Recommendation:**
- Use `session_persistence: true` (default) if you run long tasks or need to survive disconnects
- Use `session_persistence: false` if you need standard copy/paste behavior

## Security

### Authentication
- **No API keys in add-on config**: Claude Code handles authentication itself
- Credentials are stored securely in Claude Code's own directory (`~/.claude/`)
- This is more secure than storing keys in Home Assistant's configuration

### Container Security
- The Supervisor token is passed in via the environment and is not written to disk. Versions before 1.2.65-con.1 persisted it into `settings.json` inside your config directory, which is included in Home Assistant backups; 1.2.65-con.1 scrubs it on startup
- Claude Code's credentials live in `/homeassistant/.claudecode/`, part of your config directory and therefore included in backups. Treat HA backups as secrets
- File access is limited to mapped directories
- The add-on runs as root with `full_access`, Docker socket access, and the Supervisor API. Anything that can drive Claude Code here can control your Home Assistant host

### Remote Control

With `enable_remote_control`, sessions can be driven from `claude.ai/code` or the Claude mobile app (requires a Pro, Max, Team, or Enterprise subscription; API keys are not supported).

The Claude session is started when the container boots, not when you open the web terminal, so Remote Control is available after a restart without anyone opening the add-on. This requires `session_persistence: true` (tmux holds the session) and `auto_launch_claude` set to `continue` or `new`; opening the web terminal attaches to the running session. Stopping or restarting the add-on sends Claude a SIGTERM and gives it up to 10s to exit before the container goes down. Set `auto_launch_claude: off` if you do not want Claude running unattended.

With `remote_control_server` on, the add-on also runs `claude remote-control` beside the terminal session. `enable_remote_control` lets you steer the one terminal session. The server lets you start new sessions on this box from claude.ai/code or the Claude app:
- **Session names.** The session the server creates at start is titled with `remote_control_session_prefix` (`HomeAssistant` by default), so it is easy to find in the session list. Sessions started on demand are named `<prefix>-adjective-noun`.
- **Where new sessions run.** They work in `/homeassistant` as root, with `default_permission_mode`. With `auto` or `bypassPermissions`, a session started from the app can change your Home Assistant configuration unattended.
- **Session links.** Each `[rc-server]` session link in the add-on log opens a live session on this box; treat the log accordingly.
- **What the log shows.** The server reports its connection, the environment link and each session that attaches. Look for lines prefixed `[rc-server]`.
- **Stops and restarts.** Stopping the add-on stops the server's sessions as gently as the terminal's. With `apply_claude_updates`, the server moves onto Claude Code updates once all its sessions are idle, and they reattach.
- **Resuming after a restart.** Sessions started this way share the terminal's folder, so with `apply_claude_updates` on the terminal resumes its own last conversation, not the newest one in the folder.
- **Seeing them.** These sessions run without a terminal, so the web terminal cannot show them: open them in claude.ai/code or the app. A badge left of the terminal's settings gear says how many are running ("2 app sessions", with a pulsing dot while one is working); clicking it lists them, each linking to its page on claude.ai/code. The badge reads `sensor.claude_remote_control_sessions`, which the [generated package](#scheduled-jobs-claude-jobs) provides when `enable_job_endpoint` is on: the state is the number of sessions, and its attributes give `server` (`off`, `running` or `stopped`), `busy` and the `sessions` list. The session the server creates at start counts only once it has taken a prompt. The sessions are found through the session hooks, so without `apply_claude_updates` the list stays empty (the server still reports `running`).

**Security note:** this add-on runs as root with full access to your Home Assistant host. Anyone who can sign in to the linked Claude account can control it. Leave this off unless you need it.

## Troubleshooting

### Add-on won't start, or `claude` hangs (no AVX)

Claude Code 2.1.113+ ships as a Bun-compiled native binary whose JavaScript engine requires the AVX CPU instruction set. On hosts without it — typically VMs exposing the generic `kvm64` CPU model (Proxmox default) — every `claude` invocation hangs or dies, including `claude --version`.

Mitigations already built in:
- The image build smoke-tests the Claude Code release it installs. If that release cannot run on the build host, it falls back to the newest earlier release that does, so a rebuild on an AVX-less host still produces a working add-on.
- A release brought back after a restart is probed too. One that crashes is rolled back to the image's release and not tried again; one that merely times out is rolled back for that boot only.
- At startup, all `claude` calls are wrapped in timeouts, so a broken binary can't block the terminal from starting; a `[WARN]` in the log tells you AVX is missing.

The real fix is at the hypervisor: on Proxmox set the VM CPU type to `host` (or `x86-64-v3`), then fully stop and start the VM. Verify inside the add-on with `grep -o -m1 avx2 /proc/cpuinfo`.

### Authentication issues

Claude Code manages its own authentication. If you have issues:
1. Type `claude` to start the authentication flow
2. Follow the prompts to log in or enter your API key
3. Credentials are saved automatically for future sessions

**Can't copy the URL or paste the auth code?** The terminal uses tmux, which changes how copy/paste works. See [Copy and Paste in tmux](#copy-and-paste-in-tmux) for instructions.

### hass-mcp not working

1. Verify `enable_mcp` is true in configuration
2. Check add-on logs for connection errors
3. Restart the add-on after configuration changes

### Terminal not loading

1. Check that the add-on is running (green indicator)
2. Try refreshing the page
3. Check browser console for errors
4. Review add-on logs for ttyd errors

A **blank white panel** in Chrome with `WebAssembly is not defined` in the
console means the browser has the V8 JIT disabled (managed devices:
`DefaultJavaScriptJitSetting` policy; or the "JavaScript optimisation and
security" site setting), which removes WebAssembly entirely. Since
1.2.65-con.18 the terminal works without WebAssembly (only sixel image
support, which is off anyway, needs it) — older versions need a browser with
WebAssembly available.

### Session not persisting

1. Ensure `session_persistence` is set to true
2. The session is named "claude" - it will auto-attach on reconnect

### Configuration changes not applying

After changing configuration:
1. Save the configuration
2. Restart the add-on completely

## Support

- [GitHub Issues (fork)](https://github.com/con-ant/hass-addons/issues)
- [Home Assistant Community](https://community.home-assistant.io/)
