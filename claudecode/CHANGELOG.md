# Changelog

All notable changes to this project will be documented in this file.

## [1.2.65-con.4] - 2026-08-17

### Fixed
- **Auto-launch now supervises Claude instead of running it once.** The wrapper
  ran `claude $AUTO_LAUNCH_ARGS` and fell through to `exec bash --login` on any
  exit. That is correct when the user types `/exit`, but it also meant a crash
  or a dropped session quietly ended Remote Control: the add-on stayed
  "started", ingress still served a terminal, and nothing reconnected until
  somebody opened the web terminal and relaunched by hand — which defeats the
  point of `auto_launch_claude: continue` on an unattended box. A non-zero exit
  now relaunches with `--continue`, so the machine recovers its own session; a
  clean exit still drops to the login shell exactly as before. Backoff doubles
  from 2s to a 60s ceiling and resets once a run has lasted a minute, so a
  genuine crash loop backs off while a long-lived session that dies once comes
  back promptly. Relaunch always adds `--continue`, including when
  `auto_launch_claude` is `new` — after an unexpected exit the conversation
  that just died is the one worth resuming.
  Relaunching is decided on the exit status, not merely on "non-zero": status 0
  (`/exit`), 130 (SIGINT, Ctrl-C), 143 (SIGTERM, the supervisor stopping us) and
  129 (SIGHUP, ttyd's configured close signal) are all somebody meaning it, and
  hand over to the login shell. Without that, Ctrl-C could never reach a shell -
  it would relaunch on the backoff forever - and a shutdown would spend its grace
  period relaunching. 137 (SIGKILL) is deliberately treated as a crash: an OOM
  kill is exactly the case worth recovering from, and during a real container
  kill the loop dies with the container anyway.
  SIGINT is trapped rather than fatal. In a fast crash loop the wrapper spends
  nearly all its time in the backoff sleep, which is exactly when someone reaches
  for Ctrl-C; an untrapped SIGINT there kills the script outright, so it never
  reaches the login-shell handover and the respawn relaunches the failing claude -
  leaving no shell precisely when a shell is what you need to recover (downgrade
  the CLI, `/login`, repair settings). A `trap 'interrupted=1' INT` plus a check
  after the sleep routes that to the same handover as the 130 case. While claude
  itself runs the terminal is in raw mode, so the trap only fires during the sleep
  and the exit-status paths are unaffected.
  The loop also gives up on a claude that cannot start at all: five consecutive
  runs that each died within 5s means it is failing deterministically (an unknown
  flag after a CLI update, a corrupted install, an incompatible runtime), and no
  amount of relaunching helps - but a shell does. It logs an ERROR and drops to
  the login shell. Real crashes are long runs that die occasionally and never
  accumulate consecutive fast failures, so unattended recovery is unaffected.
  Without the cap the loop was unescapable in exactly the case where a shell is
  needed to fix it.
  Verified with a stubbed `claude` across every exit path: 0, 129, 130 and 143
  reach the login shell without relaunching; 1 and 137 relaunch with `--continue`
  at 2s, 4s, 8s. The Ctrl-C case was reproduced by delivering SIGINT to the
  wrapper's process group mid-sleep, as a terminal does: without the trap the
  login shell is never reached, with it the handover happens. The give-up cap
  was verified in both directions: five instant failures reach the shell in
  about a second; runs of 6s each that die twice relaunch normally and never
  trip it.

### Added
- **Pre-flight login check with a reason in the log.** The claude.ai OAuth
  *refresh* token does not slide forward when the access token renews — an
  access-token refresh moves `expiresAt` while leaving `refreshTokenExpiresAt`
  untouched — so the login lapses on a fixed date even on a box that never stops
  running. Once it has, `claude --continue --remote-control` opens an
  interactive login prompt that a Remote Control client cannot answer: the
  session simply never connects and the log says nothing. The wrapper now reads
  `refreshTokenExpiresAt` from `/root/.claude/.credentials.json` and logs an
  ERROR when it has passed (naming `/login` as the fix) or a WARN inside five
  days. Nothing is emitted unless that field is present and numeric, so
  `ANTHROPIC_API_KEY` installs, a missing file, and a half-written file stay
  silent — verified against `{}`, malformed JSON, a string-typed field, an
  already-expired timestamp, and a live credentials file.
  The check runs before EVERY launch, not only the first. On an unattended box
  the token lapses mid-supervision, weeks after the wrapper started, and the
  relaunch that follows is exactly the launch that strands; a one-shot check at
  start would have passed back then and said nothing now. Verified by letting
  the token expire during a stubbed run and then crashing it: the ERROR is
  printed before the relaunch. And "expired" means a negative day count, not
  zero: `floor()` turns 12 remaining hours into 0, which the previous `-le 0`
  read as expired and reported as an ERROR on the one day it matters most that
  the message be trusted. Zero now reads "expires today".

## [1.2.65-con.3] - 2026-08-17

### Fixed
- **The wrapped `/login` URL is now clickable and opens as one complete
  link.** The login screen turned out to be exactly the full-screen-UI case
  documented as a limitation in 1.2.65-con.2: the CLI hard-wraps the OAuth
  URL into separate rows (real newlines, no wrap metadata), so the link
  detector, `Shift`/`Option`-drag selection, and cmd-click could each see
  only one row's fragment — and on plain HTTP the `c` (OSC 52) copy hint is
  blocked by the browser, leaving no working path to log in. But the CLI
  prints every one of those rows wrapped in an OSC 8 hyperlink whose
  metadata carries the *complete* URL, and the xterm.js 5.5 client already
  activates OSC 8 links out of the box. The only missing piece was tmux:
  it stores pane hyperlinks but only re-emits them when the outer terminal
  declares the `hyperlinks` terminal-feature, which its default `xterm*`
  feature list lacks — so tmux was silently stripping the links. One
  `terminal-features` override in `.tmux.conf` fixes it (tmux ≥ 3.4; the
  Alpine 3.21 base ships 3.5a). Clicking any row of the wrapped URL now
  opens the complete OAuth URL in a new tab (after the terminal's
  link-confirmation dialog) — a navigation, not a clipboard write, so it
  works on plain HTTP too. Verified end-to-end by replaying a captured
  real `/login` byte stream (Claude Code 2.1.233) through the shipped ttyd
  1.7.7 binary + the built web client in a headless Chromium: without the
  override, clicking opened only the truncated first-row fragment
  (reproducing the report); with it, clicking the first row and clicking a
  continuation row each opened the full URL, and tmux OSC 52 copies still
  landed on the clipboard. Drag-selection across the login URL's rows still
  stitches fragments with line breaks (they are separate lines; nothing to
  join) — the README now points to the click instead.

## [1.2.65-con.2] - 2026-08-14

### Added
- **Working clipboard integration (OSC 52) in the web terminal.** The web
  client embedded in the ttyd 1.7.7 binary bundles xterm.js 5.4 without the
  clipboard addon, so OSC 52 writes were silently dropped — the Claude CLI's
  "press `c` to copy" hint did nothing, and tmux copies never reached the
  system clipboard. ttyd now serves a newer web client (xterm.js 5.5 +
  `@xterm/addon-clipboard`) via `--index`; the 1.7.7 binary is unchanged.
  The client is compiled in a Dockerfile build stage from a pinned ttyd main
  commit (2922cb8), with yarn honoring the upstream lockfile — the build is
  reproducible (byte-identical output across runs).
  tmux is configured with `set-clipboard on` plus an `Ms` terminal-override
  pinned to the `c` selection (tmux fills `%p1` with `""` for its own copies
  and `c` for programs; the clipboard addon drops anything that isn't exactly
  `c`, so the override prints `%p1` with zero precision and hardcodes `c`).
  Verified against the shipped 1.7.7 binary with a headless Chromium: typing,
  resize, window title, program-initiated OSC 52 (the "press `c`" path), and
  tmux buffer/copy-mode copies all work — including end-to-end from a full
  amd64 image build (both clipboard paths driven in a browser against ttyd
  running out of the built container). Because tmux stores wrapped output
  as one logical line, copy-mode copies of the wrapped login URL come out
  intact — the problem PR #15 worked around, now solved at the root.
  The newer client also helps plain-HTTP setups: its link detector follows
  URLs across soft-wrapped rows, so a wrapped login URL printed as flowing
  output is clickable as one complete link, and a Shift/Option-drag
  selection over it copies as one unbroken line via `execCommand` — both
  verified with the async clipboard API unavailable, as it is on plain HTTP.
  Limitations, also verified: OSC 52 itself needs HTTPS or localhost —
  on plain HTTP the write is dropped harmlessly and the drag gestures
  remain the copy path; and rows painted by full-screen UIs with per-row
  cursor positioning carry no wrap metadata, so click and drag there see
  one row at a time (covered by `c`/OSC 52 on HTTPS, or the browser
  zoom-out fallback). The main-branch client on a 1.7.7 server is an
  unreleased pairing; the build stage and `--index` flag should be dropped
  when the next ttyd release ships. README updated (copy table,
  authentication flow, trade-offs list).

### Fixed
- **GitHub CLI authentication now persists across restarts and rebuilds.**
  `github-cli` is installed in the image, but `~/.config/gh` lived in the
  container's ephemeral filesystem, so `gh auth login` had to be repeated after
  every restart or add-on update. It is now symlinked into
  `/homeassistant/.claudecode/gh`, following the same pattern already used for
  `~/.claude`, `~/.claude.json`, and `~/.config/claude-code`.
- Copying text from the web terminal was impossible on macOS whenever the program in the pane enabled mouse tracking - which is the normal state of this add-on: tmux runs with `mouse on`, and Claude Code's fullscreen renderer captures the mouse too. ttyd's copy path is client-side (a native xterm.js selection is auto-copied with a brief scissors overlay), but xterm.js only allows bypassing application mouse-tracking with Shift+drag on Windows/Linux; on macOS the gesture is Option+drag and it is additionally gated behind the `macOptionClickForcesSelection` client option, which defaults to false. Mac users therefore had no working copy gesture at all. ttyd now starts with `-t macOptionClickForcesSelection=true`, so Option+drag makes a native selection that lands on the system clipboard. Verified against the shipped ttyd 1.7.7 binary and web client. These gestures remain the copy path for plain-HTTP setups now that this release also ships the full OSC 52 fix (see above)
  (ported from upstream robsonfelix/robsonfelix-hass-addons#37 by @ahalekelly)

### Changed
- README copy/paste instructions updated to match: `Option (⌥)`+drag on macOS, `Shift`+drag on Windows/Linux (the previously documented `Ctrl+Shift` gesture never worked on macOS)

## [1.2.65-con.1] - 2026-08-13

Imports the worthwhile fixes from upstream v1.2.64 and v1.2.65
(robsonfelix/robsonfelix-hass-addons), adapted to this fork's feature set.

### Security
- **Supervisor token no longer written to disk.** The `update_mcp_token()`
  shell function (run by the `c`/`cc` aliases and the auto-launch wrapper)
  wrote `$SUPERVISOR_TOKEN` into `settings.json`, which lives under
  `/homeassistant/.claudecode/` and therefore ships inside every Home
  Assistant backup. hass-mcp never read that key — it reads `HA_TOKEN` from
  the environment, which the add-on already exports. The function is removed
  and any token persisted by older versions is scrubbed from `settings.json`
  on startup.

### Fixed
- **AVX-less hosts (upstream #24):** Claude Code 2.1.113+ is a Bun-compiled
  native binary that requires AVX; on VMs exposing the generic kvm64 CPU model
  every `claude` invocation hangs, which used to block startup. The build now
  uses upstream's `install-claude.sh` to install the newest release that
  passes a smoke test, startup wraps every `claude` call in a timeout behind a
  `claude --version` health gate (the terminal always starts, MCP setup and
  auto-launch are skipped with a `[WARN]`/`[ERROR]` instead of hanging), and a
  startup warning plus README section explain the hypervisor-level fix.
- `auto_update_claude` now verifies the updated CLI still runs and rolls back
  to the build-time version (recorded in `/etc/claude-code-version`) if not.
- `enable_mcp: false` and `session_persistence: false` were silently ignored:
  jq's `// true` default treats an explicit `false` like "unset". Now read
  with an explicit null check (upstream fix).
- Pre-authorized MCP tools now apply on the very first start: `settings.json`
  is bootstrapped before the MCP configuration step instead of after it
  (previously the allowlist merge silently failed until the second start).
- Build robustness (upstream #19/#23): shell/tmux config and helper scripts
  moved from Dockerfile heredocs (which require BuildKit heredoc support) to
  `COPY rootfs/`; ttyd and Home Assistant CLI downloads retry on transient
  network errors; the `ha` CLI is pinned to 5.2.0 instead of `latest`.

### Added
- `remote_control_session_prefix` option (upstream #34): sets
  `CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX` (default "HomeAssistant") for
  Remote Control session names. `enable_remote_control` now also writes
  `remoteControlAtStartup: true` into `settings.json` (and removes it when
  disabled), so manually started `claude` sessions get Remote Control too, in
  addition to the existing `--remote-control` flag on auto-launches.
- French translation (upstream #27), extended to the Remote Control options;
  Remote Control entries added to the en/es/pt-BR translations.
- tmux user overrides: `/homeassistant/.claudecode/tmux.conf` is sourced last
  (survives rebuilds), e.g. `set -g mouse off` to restore native copy/paste.
- README: Remote Control blast-radius security note, honest Container
  Security section (root, `full_access`, backups contain credentials), AVX
  troubleshooting section, tmux customization section.

## [1.2.63-con.10] - 2026-06-05

### Changed
- Image size: the global npm installs (`@anthropic-ai/claude-code` and
  `@playwright/mcp`) now run `npm cache clean --force` and remove `/root/.npm`
  and `/tmp` in the SAME `RUN` layer, so ~100MB+ of npm cache is no longer baked
  into the image. Claude Code is still installed at build time as the latest
  published version (rebuilding the add-on picks up new releases).
- `auto_update_claude` now defaults to `false`. The on-boot `npm update -g
  @anthropic-ai/claude-code` re-extracted the ~240MB native binary into the
  container's writable layer on every start (~694MB of runtime bloat). It is now
  opt-in; the CLI shipped in the image is used as-is unless you turn it back on.

### Added
- `.dockerignore` to keep the build context to just the Dockerfile and
  `CLAUDE.addon.md` (excludes docs, assets, translations, and metadata).

## [1.2.63-con.9] - 2026-05-14

### Fixed
- Playwright Browser hostname discovery: the launcher read `.hostname` from the Supervisor `/addons` list endpoint, but that endpoint never returns `hostname` (it only lives on `/addons/{slug}/info`), so auto-detect always failed. It now reads `.slug` from the list and derives the hostname locally by replacing underscores with hyphens — HA add-on hostnames are deterministic, so no extra permissions or per-add-on info access are needed. The `playwright_cdp_host` override still takes precedence.

## [1.2.63-con.8] - 2026-05-10

### Added
- `~/.claude/CLAUDE.user.md` — a user-managed instructions file, created once on first start and never overwritten by the add-on. It's imported by the add-on-managed `~/.claude/CLAUDE.md`, so anything you put there loads into every Claude Code session and survives restarts and add-on rebuilds.

### Changed
- The add-on-managed `~/.claude/CLAUDE.md` is now a static `CLAUDE.addon.md` file copied into the image, instead of a ~45-line escaped `printf` inside the Dockerfile `CMD`. Easier to maintain and review.
- Rewrote `CLAUDE.md` content: added a "Where you are running" section so Claude Code knows it's in a sandboxed HAOS add-on container (ephemeral filesystem, Alpine, AppArmor, mapped vs. non-persistent paths), and an "Interacting with Home Assistant" section covering the `homeassistant` MCP server and `ha` CLI. Existing path-mapping and log sections retained.

## [1.2.63-con.7] - 2026-05-10

### Added
- `extra_npm_packages` option: list of npm package specs to install on every container start under `/homeassistant/.claudecode/npm-global` (persistent across add-on rebuilds). Bin directory is on `PATH`. Use to add custom MCP servers without forking the Dockerfile.

### Documentation
- README: new "Authenticating the browser to Home Assistant" subsection covering HA `trusted_networks` setup. Playwright sessions start unauthenticated; without this, Claude only sees the HA login screen.
- README: new "Custom MCP servers (`extra_npm_packages`)" section documenting install workflow, MCP registration, and caveats.

## [1.2.63-con.6] - 2026-05-10

### Fixed
- `claude update` and the `auto_update_claude` startup hook were both failing with `EACCES` because the AppArmor profile granted only read+exec on `/usr/local/**`. npm needs write+lock to rename the existing package dir into a `.claude-code-*` shadow before swapping in the new version. The startup version was hidden by `2>/dev/null` in the CMD. Profile now grants `rwlk` on `/usr/local/lib/node_modules/**` and `/usr/local/bin/**`.

## [1.2.63-con.5] - 2026-05-10

### Fixed
- Playwright Browser hostname is now resolved each time the MCP server starts, not once at container boot. The previous startup-time auto-detect failed silently if Playwright Browser wasn't running yet, and the literal `playwright-browser` fallback never resolves on HA's network (real hostnames look like `<repo-id>-playwright-browser`)
- Installing Playwright Browser after Claude Code no longer requires restarting Claude Code
- `@playwright/mcp` binary name is `playwright-mcp`, not `mcp` — fixes `/opt/playwright-mcp/bin/mcp: not found` reported in MCP server output (regression introduced in 1.2.63-con.4)
- Playwright wrapper and binary are now in `/usr/local/bin` (`playwright-mcp-launch` and `playwright-mcp` symlink). Claude Code's `auto` permission classifier was flagging `/opt` paths as unusual, blocking manual Bash-tool tests of the binary. The npm install still lives under `/opt/playwright-mcp` to avoid the `/usr/local/bin/mcp` collision with `hass-mcp`
- AppArmor profile now grants `ixmr` on `/opt/playwright-mcp/**`. AppArmor resolves symlinks before checking, so executing `/usr/local/bin/playwright-mcp` was being denied at the `/opt/...` target without this rule

### Changed
- New `/opt/playwright-mcp/launch.sh` wrapper does the discovery on each MCP spawn. The `playwright_cdp_host` add-on option is forwarded as the `PLAYWRIGHT_CDP_HOST` env var on the MCP server config; the wrapper prefers it over auto-detect when set
- Removed the startup-time auto-detect block in the CMD; the wrapper handles it dynamically

## [1.2.63-con.4] - 2026-05-10

### Fixed
- Playwright MCP server failing to start with a misleading "Permission denied" error: the previous `npx --no-install @playwright/mcp` approach relied on an npx cache that wasn't actually populated at build time. Now installs `@playwright/mcp` to `/opt/playwright-mcp` and invokes `/opt/playwright-mcp/bin/mcp` by absolute path — no PATH conflict with `hass-mcp`'s `/usr/local/bin/mcp`, no npx surprises
- Playwright Browser hostname auto-detect fallback was silent. When detection fails it now logs explicit guidance ("restart this add-on after installing playwright-browser, or set `playwright_cdp_host`") instead of just falling through to a literal that won't resolve

### Documentation
- README notes that Playwright Browser must be installed *and running* before claudecode starts; restart claudecode if installed afterward

## [1.2.63-con.3] - 2026-05-10

### Fixed
- Supervisor warning "App has full device access, and selective device access in the configuration": dropped `uart: true` since `full_access: true` (added in 1.2.61) already grants UART access
- Supervisor warning "App config 'arch' uses deprecated values ['armv7', 'armhf', 'i386']": removed those archs from `config.yaml`, leaving `amd64` and `aarch64`

## [1.2.63-con.2] - 2026-05-10

### Added
- `auto_launch_claude` option (`off` | `new` | `continue`, default `continue`): runs `claude` (or `claude --continue`) automatically when the terminal opens, falling back to a bash login shell when Claude exits
- `enable_remote_control` option (default `false`): when true, appends `--remote-control` to all Claude launches (auto-launch + the `c`/`cc` aliases) so [Claude Code Remote Control](https://code.claude.com/docs/en/remote-control) is on by default. Requires Claude Code v2.1.51+, claude.ai OAuth login (not API key), and a Pro/Max/Team/Enterprise plan
- `/usr/local/bin/claude-auto-launch` wrapper script: refreshes the MCP token, runs Claude with runtime flags, then drops to bash on exit

## [1.2.63-con.1] - 2026-05-10

### Added
- `default_permission_mode` option to set Claude Code's default permission mode on startup
- Defaults to `auto` (classifier-based approve/deny, requires Claude Code v2.1.83+ on Max/Team/Enterprise/API plan with Sonnet 4.6+/Opus 4.6+); falls back silently to `default` if requirements aren't met
- All five modes selectable: `default`, `auto`, `acceptEdits`, `plan`, `bypassPermissions`
- Applied unconditionally (independent of `enable_mcp`) by patching `~/.claude/settings.json` at container start

## [1.2.63] - 2026-02-23

### Fixed
- Build failure due to `/usr/local/bin/mcp` conflict between hass-mcp (pip) and @playwright/mcp (npm)
- Switched from `npm install -g @playwright/mcp` to npx cache approach (pre-cache during build, `npx --no-install` at runtime)

### Changed
- Playwright Browser add-on: added aarch64 (ARM64) architecture support

## [1.2.62] - 2026-01-26

### Fixed
- MCP token now auto-updates when starting Claude via `c` or `cc` aliases
- Fixes "HTTP error: 500" when SUPERVISOR_TOKEN changes after addon restart

## [1.2.61] - 2026-01-16

### Added
- Full hardware access (`full_access: true`) for Docker socket mounting

## [1.2.60] - 2026-01-16

### Added
- Docker CLI (`docker` command) to use the Docker API

## [1.2.59] - 2026-01-16

### Added
- Docker API access (`docker_api: true`)

## [1.2.58] - 2026-01-16

### Added
- UART/serial port access (`uart: true`)

## [1.2.57] - 2026-01-16

### Added
- `socat` for bidirectional data transfer between channels

## [1.2.56] - 2026-01-16

### Added
- `pyserial` Python library for serial port communication

## [1.2.55] - 2026-01-16

### Fixed
- Removed `unrar` package (not available in Alpine 3.21)
- Use `7z x file.rar` instead for RAR extraction

## [1.2.54] - 2026-01-16

### Added
- Archive tools: `p7zip` for 7-Zip and RAR archives (use `7z` command)
- Modbus tools: `mbpoll` command line Modbus master, `pymodbus` Python library
- Useful for industrial automation and device communication tasks

## [1.2.53] - 2026-01-15

### Added
- Auto-detect Playwright Browser hostname using Supervisor API
- No need to manually configure `playwright_cdp_host` anymore
- Finds any add-on with slug ending in `playwright-browser`

## [1.2.52] - 2026-01-15

### Fixed
- Changed Playwright CDP endpoint from `ws://` to `http://` protocol
- Playwright auto-discovers the WebSocket path via `/json/version`
- Fixes 404 Not Found error when connecting to Chrome CDP

## [1.2.51] - 2026-01-15

### Added
- Configurable `playwright_cdp_host` option for custom Playwright Browser hostname
- Useful when default hostname doesn't resolve (e.g., use `1016f397-playwright-browser`)

## [1.2.50] - 2026-01-15

### Fixed
- Playwright MCP CDP endpoint hostname corrected to `playwright-browser` (was `local-playwright-browser`)
- Fixes "ENOTFOUND local-playwright-browser" connection error

## [1.2.49] - 2026-01-15

### Added
- GitHub CLI (`gh`) for GitHub operations (PRs, issues, repos, etc.)

## [1.2.48] - 2026-01-15

### Changed
- Playwright MCP now connects to external "Playwright Browser" add-on via CDP
- Removed Chromium from this add-on (keeps image small ~100MB vs ~2GB)
- Alpine + Chromium sandbox issues resolved by using separate Ubuntu-based add-on

### Note
- Requires "Playwright Browser" add-on to be installed and running for browser automation

## [1.2.47] - 2026-01-15

### Fixed
- Created `/usr/local/bin/chromium-wrapper` script that always passes `--no-sandbox`
- Playwright MCP config now points to wrapper script
- Should resolve EACCES and sandbox errors when running as root

## [1.2.46] - 2026-01-15

### Fixed
- Playwright MCP now uses config file with system Chromium
- Added `--no-sandbox` and `--disable-dev-shm-usage` flags for container compatibility
- Uses `/usr/bin/chromium-browser` instead of downloaded Chromium

## [1.2.45] - 2026-01-15

### Fixed
- MCP servers now configured at user scope (`-s user`) instead of project scope
- MCPs are now globally available regardless of working directory

## [1.2.44] - 2026-01-14

### Added
- Playwright MCP server for browser automation (opt-in via `enable_playwright_mcp`)
- Headless Chromium browser pre-installed
- Allows Claude to navigate web pages, fill forms, click elements, and take screenshots

## [1.2.43] - 2026-01-14

### Changed
- Upgraded `hassio_role` from `homeassistant` to `manager`
- Enables access to other add-ons' logs via `ha addons logs <slug>`

## [1.2.42] - 2026-01-14

### Added
- `hassio_role: homeassistant` permission for reading core logs
- Fixes 403 error when using `ha core logs`

## [1.2.41] - 2026-01-14

### Added
- Installed Home Assistant CLI (`ha` command) in the container
- `ha core logs`, `ha core restart`, etc. now available

## [1.2.40] - 2026-01-14

### Fixed
- Updated log commands in CLAUDE.md (`ha` CLI not available in add-on containers)
- Now uses `/homeassistant/home-assistant.log` and Supervisor API

## [1.2.39] - 2026-01-14

### Added
- CLAUDE.md now includes Home Assistant logging instructions
  - Log levels explanation (debug, info, warning, error)
  - Commands to read and filter logs
  - How to enable debug logging for integrations

## [1.2.38] - 2026-01-14

### Added
- Pre-authorized read-only hass-mcp tools (no confirmation needed):
  - `get_version`, `get_entity`, `list_entities`, `search_entities_tool`
  - `domain_summary_tool`, `list_automations`, `get_history`, `get_error_log`
- Pre-authorized file read operations:
  - `Read`, `Glob`, `Grep` for `/homeassistant/**`, `/config/**`, `/share/**`, `/media/**`
- Write operations still require confirmation: `entity_action`, `call_service_tool`, `restart_ha`

## [1.2.37] - 2026-01-14

### Added
- Auto-generated `~/.claude/CLAUDE.md` with path mapping instructions
- Claude Code now knows `/config` → `/homeassistant` translation

## [1.2.36] - 2026-01-14

### Fixed
- Reverted `/config` symlink that caused 502 startup errors

## [1.2.35] - 2026-01-14

### Added
- Symlink `/config` → `/homeassistant` for HA path compatibility (reverted in 1.2.36)

## [1.2.34] - 2026-01-14

### Added
- Auto-update Claude Code option (`auto_update_claude`) - checks for updates on startup
- Keeps Claude Code current without requiring add-on version bumps

## [1.2.33] - 2026-01-14

### Added
- Brazilian Portuguese translation (pt-BR)
- Spanish translation (es)

## [1.2.32] - 2026-01-14

### Fixed
- Added .profile to source .bashrc (tmux login shells need this for aliases)

## [1.2.31] - 2026-01-14

### Fixed
- Version bump to force rebuild (1.2.30 may have been cached before alias fix)

## [1.2.30] - 2026-01-14

### Changed
- Reorganized documentation: DOCS.md renamed to README.md
- Simplified root README.md
- Added Quick Start and Requirements sections

### Fixed
- Added .bashrc with aliases (`c`, `cc`, `ha-config`, `ha-logs`) - they were documented but not working

### Removed
- Deleted unused run.sh (Dockerfile CMD has everything inline)

## [1.2.29] - 2026-01-14

### Fixed
- hass-mcp expects `HA_URL` not `HA_HOST`

## [1.2.28] - 2026-01-14

### Changed
- Export HA_TOKEN/HA_HOST as environment variables instead of baking into MCP config
- hass-mcp now reads token from inherited environment (cleaner approach)

## [1.2.27] - 2026-01-14

### Fixed
- hass-mcp expects `HA_TOKEN` and `HA_HOST` (not `HASS_TOKEN`/`HASS_HOST`)

## [1.2.26] - 2026-01-14

### Fixed
- MCP now configured using `claude mcp add-json` command (proper Claude Code API)
- Previous settings.json approach was not recognized by Claude Code

### Documentation
- Added detailed copy/paste instructions for tmux mode (Ctrl+Shift to select, Shift+Insert to paste)

## [1.2.25] - 2026-01-14

### Fixed
- MCP configuration was never created - hass-mcp integration now works
- Added MCP setup to Dockerfile CMD (run.sh was not being executed)
- `/mcp` command now shows Home Assistant MCP server when `enable_mcp: true`

## [1.2.24] - 2026-01-14

### Added
- Improved tmux mouse wheel scrolling support
- Disable alternate screen buffer for better scrollback (`smcup@:rmcup@`)
- Mouse wheel bindings for scrolling in tmux copy mode

### Note
- Mouse scrolling now enabled; use middle-click or Shift+Insert to paste

## [1.2.23] - 2026-01-14

### Added
- Configure tmux with 20,000 line scrollback buffer (`history-limit`)
- Use `Ctrl+b [` then arrow keys/Page Up/Down to scroll in tmux

## [1.2.22] - 2026-01-14

### Added
- Increased terminal scrollback buffer to 20,000 lines (xterm.js)

## [1.2.21] - 2026-01-14

### Reverted
- Removed tmux mouse mode (breaks paste functionality)

### Documentation
- Added section explaining scrolling and session persistence trade-offs

## [1.2.20] - 2026-01-14

### Fixed
- Persist /root/.claude.json file (stores theme/onboarding state)
- Enable tmux mouse support for scroll wheel (`set -g mouse on`)

## [1.2.19] - 2026-01-14

### Fixed
- Store Claude Code data in /homeassistant/.claudecode (truly persistent)
- Survives addon uninstall/reinstall/rebuild
- Symlink ~/.claude and ~/.config/claude-code to HA config directory

## [1.2.18] - 2026-01-14

### Changed
- Sidebar icon changed to mdi:brain

### Fixed
- Persist both ~/.claude and ~/.config/claude-code directories
- Ensures all Claude Code auth and config survives restarts

## [1.2.17] - 2026-01-14

### Fixed
- Persist Claude Code authentication across restarts
- Symlink /root/.claude to /data/claude for persistent storage
- Restored config reading for font size, theme, and session persistence

## [1.2.16] - 2026-01-14

### Fixed
- Restored config reading for font size, theme, and session persistence
- ttyd now applies terminal_font_size, terminal_theme, and session_persistence settings

## [1.2.15] - 2026-01-14

### Fixed
- Refined AppArmor profile with focused permissions for HA config access
- Added dac_read_search capability for directory listing
- Full access to /homeassistant, /share, /media, /config directories
- Read-only access to system files, SSL, backups

## [1.2.14] - 2026-01-14

### Fixed
- Add /etc/** read permissions to AppArmor profile
- Fixes "bash: /etc/profile: Permission denied" error

## [1.2.13] - 2026-01-14

### Fixed
- Add PTY permissions to AppArmor profile (sys_tty_config, /dev/ptmx, /dev/pts/*)
- Fixes "pty_spawn: Permission denied" error when spawning terminal

## [1.2.12] - 2026-01-14

### Fixed
- Use static ttyd binary from GitHub releases instead of Alpine package
- Fixes "failed to load evlib_uv" libwebsockets error

## [1.2.11] - 2026-01-14

### Changed
- Simplified startup: run ttyd directly in CMD without script file
- Minimal configuration for debugging startup issues

## [1.2.10] - 2026-01-14

### Fixed
- Create run.sh inline via heredoc to avoid file permission issues

## [1.2.9] - 2026-01-14

### Fixed
- Add .gitattributes to enforce LF line endings for shell scripts
- Force Docker cache bust for permission fixes

## [1.2.8] - 2026-01-14

### Changed
- Use Docker's tini init system (`init: true`) instead of s6-overlay
- Simplified entrypoint configuration

## [1.2.7] - 2026-01-14

### Fixed
- Use bash instead of bashio in s6-overlay run script
- Add chmod +x /init to fix permission issues

## [1.2.6] - 2026-01-14

### Changed
- Properly configure s6-overlay v3 service structure
- Add service files in /etc/s6-overlay/s6-rc.d/ttyd

## [1.2.5] - 2026-01-14

### Changed
- Attempted switch to pure Alpine base image (reverted due to HA format requirements)

## [1.2.4] - 2026-01-14

### Fixed
- Set `init: false` for s6-overlay v3 compatibility

## [1.2.3] - 2026-01-14

### Fixed
- Force bash entrypoint to bypass s6-overlay init issues

## [1.2.2] - 2026-01-14

### Fixed
- Remove s6-overlay dependency, use plain bash with jq
- Fixes "/init: Permission denied" startup error

## [1.2.1] - 2026-01-14

### Fixed
- Corrected hass-mcp package name (was homeassistant-mcp)
- Upgraded to Python 3.13 base image for hass-mcp compatibility

## [1.2.0] - 2026-01-14

### Changed
- **Security improvement**: Removed API key from add-on config - Claude Code now handles authentication itself
- Simplified Dockerfile - use Alpine's ttyd package instead of architecture-specific downloads
- Removed model selection from config (Claude Code manages this)

### Fixed
- Docker build failure due to BUILD_ARCH variable not being passed correctly

## [1.1.0] - 2026-01-14

### Added
- Model selection option (sonnet, opus, haiku)
- Terminal font size configuration (10-24px)
- Terminal theme selection (dark/light)
- Session persistence using tmux
- s6-overlay service definitions for better process management
- Shell aliases and shortcuts (c, cc, ha-config, ha-logs)
- Welcome banner with configuration info
- Health check for container monitoring

### Changed
- Upgraded to Python 3.12 Alpine base image
- Improved architecture-specific ttyd binary installation
- Enhanced run.sh with better configuration handling
- Better error messages and validation

### Fixed
- Proper ingress base path handling

## [1.0.0] - 2026-01-14

### Added
- Initial release
- Web terminal interface using ttyd
- Claude Code integration via npm package
- Home Assistant MCP server integration for entity/service access
- Read-write access to Home Assistant configuration
- Multi-architecture support (amd64, aarch64, armv7, armhf, i386)
- Ingress support for seamless sidebar integration
