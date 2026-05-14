# Claude Code — Home Assistant Add-on

## Where you are running

You are running inside a **Docker container** that is a **Home Assistant OS
add-on** — NOT on the HA host, and NOT inside Home Assistant Core's container.
You are in a separate, sandboxed container.

- The container's own filesystem (`/root`, `/usr`, `/etc`, globally installed
  packages) is **ephemeral** — rebuilt from scratch on every add-on update.
  Don't expect changes there to persist.
- Only **mapped directories** persist across rebuilds: `/homeassistant`,
  `/share`, `/media`, `/ssl`, `/backup`, `/addon_configs`. Your own config
  persists because `~/.claude` is symlinked into `/homeassistant/.claudecode`.
- To install packages that survive rebuilds, use the `extra_npm_packages`
  add-on option instead of `npm install -g` directly.
- The base OS is **Alpine Linux** — use `apk`, not `apt`. Most of `/usr` is
  read-only via an AppArmor profile; an `EACCES` on a system path is usually
  the AppArmor profile, not a normal permission problem.

## Path mapping

Paths here differ from HA Core and most documentation:

- `/homeassistant` = the HA configuration directory (equivalent to `/config`
  in HA Core)
- `/config` does **not** exist — always use `/homeassistant`
- When the user or docs mention `/config/...`, translate to `/homeassistant/...`

## Available paths

| Path | Description | Access |
|------|-------------|--------|
| `/homeassistant` | HA configuration | read-write |
| `/share` | Shared folder | read-write |
| `/media` | Media files | read-write |
| `/addon_configs` | Per-add-on config directories | read-write |
| `/ssl` | SSL certificates | read-only |
| `/backup` | Backups | read-only |

## Interacting with Home Assistant

You cannot touch HA Core's process directly — it runs in a different
container. To interact with HA:

- **`homeassistant` MCP server** — query entity states, call services, read
  history and the error log. Preferred for anything entity/service related.
- **`ha` CLI** — Supervisor-level operations: `ha core restart`,
  `ha core check`, `ha core logs`, `ha core info`, `ha addons ...`.
- To **restart or reload** HA, use `ha core restart` or the MCP server / API.
  Never try to kill or signal processes.

## Reading Home Assistant logs

**Log levels (most to least verbose):** `debug` (only if enabled in
`configuration.yaml`), `info` (default), `warning`, `error`.

```bash
# Recent core logs
ha core logs 2>&1 | tail -100

# Filter by keyword
ha core logs 2>&1 | grep -i keyword

# Errors only
ha core logs 2>&1 | grep -iE "(error|exception)"

# Or read the log file directly
tail -100 /homeassistant/home-assistant.log
```

**Enable debug logging for an integration** — add to `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.YOUR_INTEGRATION: debug
```

`_LOGGER.debug()` calls are invisible unless the logger level is `debug`. Use
`_LOGGER.info()` / `_LOGGER.warning()` for logs that should always appear.

## User instructions

The add-on regenerates this file (`~/.claude/CLAUDE.md`) on every start, so
don't edit it directly — changes are lost on restart. For your own persistent
instructions, edit `~/.claude/CLAUDE.user.md` (created automatically, never
overwritten by the add-on). It is imported below.

@~/.claude/CLAUDE.user.md
