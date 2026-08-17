# Claude Code — Home Assistant Add-on

## Where you are running

You are in a sandboxed **Home Assistant OS add-on container** — not on the HA
host, and not in HA Core's container.

- The container filesystem is **ephemeral**, rebuilt on every add-on update.
  Only mapped directories persist: `/homeassistant`, `/share`, `/media`,
  `/ssl`, `/backup`, `/addon_configs`. Your own config persists because
  `~/.claude` is symlinked into `/homeassistant/.claudecode`.
- For packages that must survive rebuilds, use the `extra_npm_packages`
  add-on option, not `npm install -g`.
- The base OS is **Alpine** — use `apk`, not `apt`. Most of `/usr` is
  read-only via AppArmor; an `EACCES` on a system path is usually the
  AppArmor profile, not a normal permission problem.

## Path mapping

- `/homeassistant` = the HA configuration directory (`/config` in HA Core and
  most documentation)
- `/config` does **not** exist here — always translate `/config/...` to
  `/homeassistant/...`

| Path | Access |
|------|--------|
| `/homeassistant`, `/share`, `/media`, `/addon_configs` | read-write |
| `/ssl`, `/backup` | read-only |

## Interacting with Home Assistant

HA Core runs in a separate container. Never manage its process directly (even
though `docker` access exists) — use the proper interfaces:

- **`homeassistant` MCP server** (on by default) — entity states, service
  calls, history, error log. Preferred for anything entity/service related.
- **`ha` CLI** — Supervisor operations: `ha core restart|check|logs|info`,
  `ha addons ...`. Use `ha core restart` to restart HA; never kill processes.

## Available tools

Beyond standard Alpine userland: `gh`, `docker` (socket access), `jq`, `rg`,
`7z`, `socat`, `mbpoll` (plus Python `pymodbus`/`pyserial`), `vim`, `nano`.

## Reading Home Assistant logs

```bash
ha core logs 2>&1 | tail -100        # or: tail -100 /homeassistant/home-assistant.log
ha core logs 2>&1 | grep -iE "(error|exception)"
```

Log levels: `debug` (only if enabled), `info` (default), `warning`, `error`.
`_LOGGER.debug()` output is invisible unless enabled in `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.YOUR_INTEGRATION: debug
```

## User instructions

The add-on regenerates this file (`~/.claude/CLAUDE.md`) on every start — don't
edit it. Persistent user instructions belong in `~/.claude/CLAUDE.user.md`
(created automatically, never overwritten). It is imported below.

@~/.claude/CLAUDE.user.md
