# Changelog

All notable changes to this add-on will be documented in this file.

## [0.1.0] - 2026-08-19

### Added
- Initial release: Pomerium v0.33.1 (open source, all-in-one mode) as an
  identity-aware reverse proxy for Home Assistant and other add-ons
  (amd64, aarch64).
- Option-driven config generation: structured `routes` with per-route access
  rules (`allowed_users`, `allowed_domains`, `allow_any_authenticated_user`)
  compiled to Pomerium Policy Language. Routes without access rules deny all
  traffic by default.
- TLS via Let's Encrypt `autocert` (cache persisted in `/data`) or existing
  certificates from `/ssl` (`certfile`/`keyfile`).
- Hosted authenticate service by default (no IdP registration needed);
  optional self-hosted authenticate with `idp_*` options.
- `config_mode: file` escape hatch: a complete `pomerium.yaml` in the
  add-on config directory is used verbatim.
- Auto-generated, persisted `cookie_secret` so session cookies stay valid
  across restarts and updates; injected via environment in file mode when
  absent.
- Refuses to start while a route still uses the `example.com` placeholder.
- Design notes in `docs/DESIGN.md`.
