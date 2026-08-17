# Claude Code for Home Assistant

Run [Claude Code](https://docs.anthropic.com/en/docs/claude-code) in your Home Assistant sidebar with full access to your configuration.

> Fork of [robsonfelix/robsonfelix-hass-addons](https://github.com/robsonfelix/robsonfelix-hass-addons). Fork-only releases use a `-con.N` suffix on the upstream version.

## Quick Start

```bash
claude "List all my automations"
claude "Create an automation to turn on lights at sunset"
claude "Why isn't my motion sensor automation working?"
```

## Setup

1. Add this repository to Home Assistant and install the "Claude Code" add-on.
2. Start it and open the Web UI from the sidebar.
3. Type `claude` and follow the authentication prompts. No API key goes in the
   add-on config — Claude Code stores its own credentials. See
   [Authenticating](#authenticating-claude-code-first-launch) for copy/paste tips.

With hass-mcp enabled (the default), Claude can query entity states, call
services, and debug automations directly.

**Shell aliases:** `c` = `claude` · `cc` = `claude --continue` ·
`ha-config` = cd to config dir · `ha-logs` = view HA logs

## Configuration Options

| Option | Description | Default |
|--------|-------------|---------|
| `enable_mcp` | HA integration via hass-mcp | true |
| `enable_playwright_mcp` | Playwright MCP (needs the [Playwright Browser](../playwright-browser/) add-on) | false |
| `playwright_cdp_host` | Override Playwright Browser hostname (auto-detected when blank) | "" |
| `terminal_font_size` | Font size (10–24) | 14 |
| `terminal_theme` | `dark` or `light` | dark |
| `working_directory` | Start directory | /homeassistant |
| `session_persistence` | tmux-backed persistent sessions | true |
| `auto_update_claude` | Update Claude Code on every start; rolls back automatically if the new release can't run. Writes to the container layer | false |
| `default_permission_mode` | `default`, `auto`, `acceptEdits`, `plan`, or `bypassPermissions`. `auto` needs Claude Code 2.1.83+ and a qualifying plan; falls back silently to `default` | auto |
| `auto_launch_claude` | On terminal open: `off`, `new` (`claude`), or `continue` (`claude --continue`) | continue |
| `enable_remote_control` | [Remote Control](https://code.claude.com/docs/en/remote-control) for all sessions. Requires claude.ai OAuth login (not API key) and a Pro/Max/Team/Enterprise plan. See [Security](#security) | false |
| `remote_control_session_prefix` | Prefix for Remote Control session names | HomeAssistant |
| `extra_npm_packages` | npm packages installed on every start to a persistent path — see below | `[]` |

### Playwright MCP

Set `enable_playwright_mcp: true` and install/start the
[Playwright Browser](../playwright-browser/) add-on — order doesn't matter, the
hostname is resolved each time the MCP server starts. Set `playwright_cdp_host`
only to pin a specific instance. Failures appear as `[playwright-launch]` lines
in Claude's MCP error log.

A fresh browser session isn't logged in to HA, so it lands on the login screen.
To let it through, add the `trusted_networks` auth provider to
`configuration.yaml` and restart HA:

```yaml
homeassistant:
  auth_providers:
    - type: homeassistant
    - type: trusted_networks
      trusted_networks:
        - 172.30.32.0/23   # HA add-on network
      trusted_users:
        172.30.32.0/23:
          - <your-user-id>  # Settings → People → click the user → ID
      allow_bypass_login: true
```

**Note:** this trusts every add-on on that subnet. For plain API access
(states, services, history), hass-mcp already works without any of this.

### Custom MCP servers (`extra_npm_packages`)

Manual `npm install -g` is wiped by add-on updates. Packages listed in
`extra_npm_packages` are re-installed on every start under
`/homeassistant/.claudecode/npm-global` (persistent, and its `bin` is on
`PATH`). Register the MCP server once — the registration itself persists:

```bash
claude mcp add-json filesystem '{"command":"mcp-server-filesystem","args":["/homeassistant"]}' -s user
```

Unpinned packages get the latest on each start; a bad package name logs a
`[WARN]` without stopping the add-on. To find a binary name:
`ls /homeassistant/.claudecode/npm-global/bin/`.

## File Locations

| Path | Access |
|------|--------|
| `/homeassistant` (HA config), `/share`, `/media` | read-write |
| `/ssl`, `/backup` | read-only |

## Customizing Claude's instructions

`~/.claude/CLAUDE.md` is regenerated on every start — don't edit it. Put your
own persistent instructions in `~/.claude/CLAUDE.user.md`: created once, never
overwritten, imported into every session.

## Sessions, scrolling, copy/paste

With `session_persistence: true` (default), tmux keeps your session — including
running Claude tasks — alive across browser refreshes and disconnects, with a
20,000-line scrollback. The trade-off: tmux captures the mouse, so copy/paste
works differently:

| Action | How |
|--------|-----|
| Copy (copy-mode) | Scroll up, select, copy — lands on your clipboard via OSC 52 (HTTPS/localhost only; wrapped lines copy as one line) |
| Copy (macOS) | `Option (⌥)`+drag |
| Copy (Windows/Linux) | `Shift`+drag |
| Paste | `Shift+Insert`, middle-click, or `Ctrl+Shift+V` |
| Scroll | Mouse wheel (auto-enters copy mode); `q` to exit |
| Detach | `Ctrl+b d` |

Set `session_persistence: false` for native scrolling and standard copy/paste,
at the cost of losing the session on refresh or restart. Or keep tmux and put
overrides in `/homeassistant/.claudecode/tmux.conf` (sourced last, survives
rebuilds), e.g. `set -g mouse off`.

### Authenticating Claude Code (first launch)

- **Click the login URL.** It's a real hyperlink (OSC 8) — clicking any row of
  the wrapped URL opens the complete URL, even on plain HTTP.
- On HTTPS/localhost, the CLI's **press `c` to copy** hint also works. On
  plain HTTP the browser blocks it — use the click.
- Avoid drag-selecting the URL: the login screen paints each row separately,
  so the selection picks up line breaks. If you need it as text, zoom the
  browser out until it fits on one line.
- Complete auth in the browser, copy the code, and paste it into the terminal
  with `Shift+Insert` or `Ctrl+Shift+V`.

## Security

- Claude Code's credentials live under `/homeassistant/.claudecode/`, which is
  part of your config directory and therefore **included in HA backups —
  treat backups as secrets**. (Versions before 1.2.65-con.1 also persisted the
  Supervisor token there; it is scrubbed on startup.)
- The add-on runs as root with `full_access`, Docker socket access, and the
  Supervisor API. Anything that can drive Claude Code here can control your
  Home Assistant host.
- **Remote Control:** anyone who can sign in to the linked Claude account can
  control this add-on — and thus your host. Leave `enable_remote_control` off
  unless you need it.

## Troubleshooting

**`claude` hangs or won't run (no AVX):** Claude Code 2.1.113+ is a
Bun-compiled binary whose JS engine requires AVX; VMs exposing the generic
`kvm64` CPU model (Proxmox default) can't run it. The build falls back to the
newest release that passes a smoke test, and startup logs a `[WARN]` when that
fallback is actually in effect. The real fix: set the Proxmox VM CPU type to
`host` (or `x86-64-v3`), then fully stop and start the VM. Verify with
`grep -o -m1 avx2 /proc/cpuinfo`.

**Authentication issues:** type `claude` and follow the prompts; for
copy/paste help see [Authenticating](#authenticating-claude-code-first-launch).

**hass-mcp not working:** check `enable_mcp: true`, review the add-on logs,
restart the add-on.

**Terminal not loading:** confirm the add-on is running, refresh the page,
check the add-on logs for ttyd errors.

**Configuration changes not applying:** restart the add-on after saving.

## Support

- [GitHub Issues (fork)](https://github.com/con-ant/hass-addons/issues)
- [Home Assistant Community](https://community.home-assistant.io/)
