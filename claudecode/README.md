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
| `auto_update_claude` | Auto-update Claude Code on startup | true |
| `default_permission_mode` | Default Claude Code permission mode: `default`, `auto`, `acceptEdits`, `plan`, or `bypassPermissions`. `auto` uses a classifier to approve/deny prompts (Claude Code v2.1.83+, Max/Team/Enterprise/API plan, Sonnet 4.6+/Opus 4.6+); falls back silently to `default` if requirements aren't met | auto |
| `auto_launch_claude` | Auto-run Claude when the terminal opens: `off` (bash prompt), `new` (`claude`), or `continue` (`claude --continue`). Drops to a login shell when Claude exits | continue |
| `enable_remote_control` | Pass `--remote-control` to all Claude launches so [Remote Control](https://code.claude.com/docs/en/remote-control) is on by default. Requires Claude Code v2.1.51+, claude.ai OAuth login (not API key), and a Pro/Max/Team/Enterprise plan — leave off if you authenticate with an API key | false |
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

### Copy and Paste in tmux

Since tmux captures mouse events, copy/paste works differently:

| Action | How to do it |
|--------|--------------|
| **Copy** | Hold `Ctrl+Shift` while selecting text with mouse |
| **Paste** | `Shift+Insert` or middle-click |
| **Alternative paste** | `Ctrl+Shift+V` (browser dependent) |

**Note**: Regular right-click paste and simple mouse selection won't work because tmux intercepts these events for scrolling.

#### Authenticating Claude Code (first launch)

The authentication URL can be long and may wrap across multiple lines. To handle this:

1. **Zoom out** your browser (`Ctrl + -` or `Cmd + -`) until the URL fits on a single line
2. **Click the link** — it should open in a new tab
3. Complete authentication in the browser and **copy the auth code**
4. Click back on the terminal and **paste** with `Shift+Insert` or `Ctrl+Shift+V`

If clicking the link doesn't work, hold `Ctrl+Shift` while selecting the URL with your mouse to copy it, then paste it into your browser's address bar.

### Scrolling and Session Persistence Trade-offs

**With tmux (`session_persistence: true`):**
- ✅ Session survives browser refresh/disconnect
- ✅ Can detach and reattach to running sessions
- ✅ Long-running Claude tasks continue in background
- ✅ Mouse wheel scrolling works (enters copy mode automatically)
- ✅ 20,000 line scrollback buffer
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
- The Supervisor token is automatically managed and not exposed
- File access is limited to mapped directories
- The add-on runs in an isolated container

## Troubleshooting

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
