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

| Option | Description | Default |
|--------|-------------|---------|
| `enable_mcp` | Enable HA integration via hass-mcp | true |
| `enable_playwright_mcp` | Enable Playwright MCP (requires the [Playwright Browser](../playwright-browser/) add-on) | false |
| `playwright_cdp_host` | Override Playwright Browser hostname (auto-detected from Supervisor when blank) | "" |
| `terminal_font_size` | Font size (10-24) | 14 |
| `terminal_theme` | dark or light | dark |
| `working_directory` | Start directory | /homeassistant |
| `session_persistence` | Use tmux for persistent sessions | true |
| `auto_update_claude` | Auto-update Claude Code on startup (rolls back automatically if the new release cannot run on this host). Writes to the container layer — see changelog 1.2.63-con.10 | false |
| `default_permission_mode` | Default Claude Code permission mode: `default`, `auto`, `acceptEdits`, `plan`, or `bypassPermissions`. `auto` uses a classifier to approve/deny prompts (Claude Code v2.1.83+, Max/Team/Enterprise/API plan, Sonnet 4.6+/Opus 4.6+); falls back silently to `default` if requirements aren't met | auto |
| `auto_launch_claude` | Auto-run Claude when the terminal opens: `off` (bash prompt), `new` (`claude`), or `continue` (`claude --continue`). Drops to a login shell when Claude exits | continue |
| `enable_remote_control` | Turn on [Remote Control](https://code.claude.com/docs/en/remote-control) for all Claude sessions (sets `remoteControlAtStartup` in settings.json and passes `--remote-control` to auto-launches) so you can view and steer sessions from claude.ai/code or the Claude mobile app. Requires Claude Code v2.1.51+, claude.ai OAuth login (not API key), and a Pro/Max/Team/Enterprise plan. See the security note below | false |
| `remote_control_session_prefix` | Prefix for auto-generated Remote Control session names shown in claude.ai/code and the mobile app (e.g. `HomeAssistant-graceful-unicorn`). Only applies when `enable_remote_control` is on | HomeAssistant |
| `extra_npm_packages` | List of npm package specs to install at every container start under `/homeassistant/.claudecode/npm-global` (persistent across add-on rebuilds). Bin dir is on `PATH`. Use to add custom MCP servers without forking the Dockerfile. | `[]` |

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

## Customizing Claude's instructions

The add-on writes `~/.claude/CLAUDE.md` on every start with container-specific
context (path mapping, how to reach Home Assistant, log commands). **It is
regenerated each restart — don't edit it directly.**

For your own persistent instructions, edit **`~/.claude/CLAUDE.user.md`**. The
add-on creates this file once and never overwrites it; it's imported by
`~/.claude/CLAUDE.md`, so anything you put there loads into every Claude Code
session and survives restarts and add-on updates. Good for project
conventions, frequently-referenced entity IDs, or reminders about your setup.

## Session Persistence

When `session_persistence` is enabled, the add-on uses tmux to maintain your terminal session. This means:

- Your session survives browser refreshes
- You can disconnect and reconnect without losing context
- Claude Code conversations are preserved

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

**Security note:** this add-on runs as root with full access to your Home Assistant host. Anyone who can sign in to the linked Claude account can control it. Leave this off unless you need it.

## Troubleshooting

### Add-on won't start, or `claude` hangs (no AVX)

Claude Code 2.1.113+ ships as a Bun-compiled native binary whose JavaScript engine requires the AVX CPU instruction set. On hosts without it — typically VMs exposing the generic `kvm64` CPU model (Proxmox default) — every `claude` invocation hangs or dies, including `claude --version`.

Mitigations already built in:
- The image build installs the newest Claude Code release that actually runs on the build host (`install-claude.sh` smoke-tests candidates), so a rebuild on an AVX-less host still produces a working add-on.
- At startup, all `claude` calls are wrapped in timeouts, so a broken binary can't block the terminal from starting; a `[WARN]` in the log tells you when the add-on is actually running a pre-2.1.113 release because AVX is missing. The warning only appears on x86 hosts where the build really did fall back — aarch64 CPUs never list an `avx` flag and don't need one, and a host whose build verified a current release is left alone.
- With `auto_update_claude: true` on such a host, the update is skipped (with an `[INFO]` line) when the latest release still needs AVX, instead of installing it, watching it fail, and rolling back on every start.

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
