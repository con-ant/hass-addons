# Pomerium Add-on — Design

Design notes for the `pomerium` Home Assistant add-on: what it is, how the
pieces are laid out, how network access works, and how configuration is
managed. Written before implementation; kept up to date as the source of
truth for structural decisions.

## Goals

- Put an identity-aware reverse proxy (SSO) in front of Home Assistant and
  other add-ons, so they can be reached from the internet without a VPN and
  without exposing each service's port individually.
- Zero-config-file quick start: a working proxy from add-on options alone,
  using Pomerium's hosted authenticate service (no identity-provider
  registration needed).
- Full-power escape hatch: advanced users can supply a complete
  `pomerium.yaml` and bypass the option-driven generation entirely.
- Safe by default: a route with no access policy denies everything rather
  than allowing "any logged-in user" (which, with the hosted authenticate
  service, would effectively be the whole internet).

## Non-goals

- Split-service (proxy/authorize/databroker on separate hosts) deployment.
  A single-node HA install wants Pomerium's "all-in-one" mode; clustering
  belongs on real infrastructure, not an add-on.
- Managing DNS records or router port-forwarding — documented, not automated.
- Pomerium Zero / Enterprise (cloud-managed control plane). The add-on runs
  plain open-source Pomerium from a local config file. Zero could be added
  later as an alternative mode (`pomerium zero` + token option).

## Layout

Follows the existing add-ons in this repo (`auto-monocle`, `playwright-browser`):

```
pomerium/
  config.yaml          # add-on manifest: options schema, ports, maps
  build.yaml           # per-arch HA Debian base images + OCI labels
  Dockerfile           # multi-stage: pomerium binary from official image
  run.sh               # config generation + exec pomerium
  README.md            # add-on store page
  DOCS.md              # "Documentation" tab: setup guide, examples
  CHANGELOG.md         # per-add-on version history
  docs/DESIGN.md       # this file
  translations/en.yaml # option labels/descriptions for the UI
```

Simple enough to keep the flat `run.sh` style rather than `claudecode`'s
`rootfs/` tree — there is exactly one process and one script.

## Runtime architecture

### Image build

Multi-stage Dockerfile:

1. `FROM pomerium/pomerium:v0.33.1 AS pomerium` — the official image, pinned.
   It is multi-arch (amd64, arm64v8), so each architecture's local Supervisor
   build pulls the right binary.
2. `FROM ${BUILD_FROM}` — the HA **Debian** base (from `build.yaml`),
   into which `/bin/pomerium` is copied. `ARG BUILD_FROM` is declared before
   the first `FROM` so the second `FROM` can see it.

Why not run the official image directly (as `playwright-browser` does with
the MS image)? Pomerium's image is distroless: no package manager, no `jq`,
no way to read `/data/options.json` and generate config at startup. The HA
base gives us bash/jq/s6.

Why Debian and not the (smaller) Alpine base? The `pomerium` executable is a
static Go binary, but at startup it extracts an embedded **Envoy** binary and
execs it, and that Envoy is dynamically linked against glibc (upstream ships
on distroless *debian12* for the same reason). On Alpine/musl Envoy fails
with `fork/exec .../envoy: no such file or directory` and the proxy never
listens. The Dockerfile also re-declares upstream's `ENV
AUTOCERT_DIR=/data/autocert` so file-mode configs inherit a persistent
certificate cache.

Only `amd64` and `aarch64` are declared (same as the other add-ons here, and
matching what the upstream image ships).

### Process model

`init: false` → the HA base's s6-overlay `/init` runs `CMD /run.sh`, which
`exec`s the `pomerium` binary — pomerium becomes the supervised process and
receives SIGTERM directly for clean shutdown. All-in-one mode (proxy +
authorize + databroker in one process, in-memory storage) — the correct
shape for a single-node deployment.

`startup: services` so the proxy comes up before `application`-class add-ons
that might sit behind it; `boot: auto` so it survives host reboots.

A Docker `HEALTHCHECK` curls Pomerium's `/ping` endpoint on the internal
HTTPS listener (`-k` because the cert may be self-signed until autocert
completes). It only probes in `options` mode, where the listener is always
`:443`; in `file` mode the user chooses the address/scheme, so the check is a
no-op rather than flagging a working proxy as unhealthy (which Supervisor's
watchdog would restart).

## Network design

```
                         Internet
                            │
              DNS: *.example.com → home public IP
                            │
                Router forwards 443 (+80) → HA host
                            │
             ┌──────────────▼──────────────┐
             │   Pomerium add-on container │
             │   :443 HTTPS  (:80 redirect)│
             └──────────────┬──────────────┘
                            │  hassio internal network (172.30.32.0/23)
         ┌──────────────────┼─────────────────────┐
         ▼                  ▼                     ▼
  homeassistant:8123   local-someaddon:1234   a0d7b954-vscode:1337
   (HA core)            (other add-ons, no host ports needed)
```

- **Inbound**: the container listens on `:443` (HTTPS) and optionally `:80`
  (HTTP→HTTPS redirect + ACME HTTP-01), published to the same host ports by
  default. Both are remappable/disable-able in the add-on's Network panel if
  the host's 443/80 are taken. `host_network` was considered and rejected:
  port publishing gives the same result with more user control and fewer
  collision surprises.
- **Upstreams**: routes point at services over the Supervisor's internal
  Docker network — HA core is `http://homeassistant:8123`, add-ons are
  reachable by their hostname (shown on each add-on's info page, e.g.
  `local-myaddon` or `a0d7b954-vscode`). Because Pomerium reaches them
  internally, backend add-ons do **not** need host port mappings at all —
  Pomerium becomes the single exposed entry point.
- **Websockets**: required by HA's frontend; generated routes default
  `allow_websockets: true`.
- **HA as upstream**: HA must trust the proxy —
  `http.use_x_forwarded_for: true` and `trusted_proxies: [172.30.32.0/23]`
  (the hassio network) in `configuration.yaml`. Documented in DOCS.md;
  the add-on cannot (and should not) edit HA's config itself, so it does not
  request `homeassistant_config` access.
- **No ingress**: Pomerium *is* the entry point and has no admin UI;
  HA ingress does not apply.

### TLS

Two mutually exclusive modes, chosen by the `autocert` option:

- `autocert: true` (default): Pomerium obtains and renews Let's Encrypt
  certificates itself (TLS-ALPN on 443, HTTP-01 via the port-80 redirect
  listener). The cert cache lives in `/data/autocert`, which persists across
  restarts and updates. `autocert_staging` flips to the LE staging CA for
  testing without burning rate limits.
- `autocert: false`: use existing certificates from the shared `/ssl`
  directory (`certfile`/`keyfile` options, relative to `/ssl`) — for users
  already running the Let's Encrypt or NGINX add-ons. `/ssl` is mapped
  read-only.

## Config management

Pomerium wants one YAML file. The add-on supports two modes via
`config_mode`:

### `options` mode (default)

`run.sh` generates `/data/pomerium.yaml` from `/data/options.json` on every
start (jq builds the structure and its JSON output is written as-is —
JSON is valid YAML, so no conversion step or `yq` dependency is needed).
Options changes take effect on add-on restart, like every other add-on. The
script runs under `umask 077`, so the generated file is `0600` from the
moment it is created since it embeds secrets.

Option → config mapping:

- Global: TLS mode (above), `http_redirect` → `http_redirect_addr: :80`,
  `log_level`, `authenticate_service_url`, and the four `idp_*` options.
  Empty optional strings are omitted entirely, so Pomerium's defaults apply
  — with no authenticate/IdP settings, Pomerium uses its hosted
  authenticate service (`authenticate.pomerium.app`), which is the
  zero-registration quick start.
- Routes: a structured list (`from`, `to`, `allow_websockets`,
  `preserve_host_header`, `pass_identity_headers`, plus access control).
  Access control fields compile to Pomerium Policy Language rather than the
  legacy route-level shorthand fields:

  ```yaml
  policy:
    allow:
      or:
        - email: {is: user@example.com}   # allowed_users
        - domain: {is: example.com}       # allowed_domains
        - authenticated_user: true        # allow_any_authenticated_user
  ```

  A route with no access fields gets **no** `policy` block → Pomerium denies
  all traffic to it. `run.sh` logs a loud warning instead of silently
  allowing, because "any authenticated user" on the hosted authenticate
  service means *anyone with a Google/GitHub/etc. account* — that must be an
  explicit opt-in (`allow_any_authenticated_user: true`).

### `file` mode

Power users set `config_mode: file` and drop a complete `pomerium.yaml`
into the add-on's config directory (`addon_config:rw`, i.e.
`/addon_configs/<repo>_pomerium/pomerium.yaml` on the host, `/config/`
inside the container). The file is passed to Pomerium verbatim — no merging.
Merging generated + user config was considered and rejected: deep-merge
semantics for lists (routes) are ambiguous and debugging a half-merged
config is worse than owning the whole file.

### Secrets

- `cookie_secret` (session cookie encryption key): generated once from
  `/dev/urandom`, persisted at `/data/cookie_secret`, reused forever, so the
  user never handles it and existing cookies stay decryptable across
  restarts and updates. Note this does *not* make sessions themselves
  survive a restart: all-in-one mode keeps session records in the in-memory
  databroker, so after a restart users are sent through the authenticate
  flow again (near-transparent with the hosted service while its own cookie
  is valid; a full IdP round-trip when self-hosting). A persistent
  databroker backend would be needed to change that.
  In `file` mode, if the user's file does not set a non-empty
  `cookie_secret`/`cookie_secret_file`, the managed one is injected via the
  `COOKIE_SECRET` environment variable (env vars take precedence over the
  file in Pomerium, so it is only exported when the file has none).
- `idp_client_secret` uses the `password` schema type so the UI masks it.
- Secrets are never printed to the add-on log.

## Security considerations

- Default-deny route policies (see above).
- The add-on requests no HA/Supervisor API access, no `docker_api`, no
  `full_access` — it is a plain network service. Only `/ssl` (ro),
  `/addon_config` (rw), and its private `/data`.
- Generated config is root-only (`0600`) inside the container's private
  `/data` volume.
- Pinned upstream version (`v0.33.1`); bumping it is an explicit add-on
  release with a changelog entry.

## Alternatives considered

| Alternative | Verdict |
|---|---|
| Run official distroless image directly | No shell/jq → can't consume `/data/options.json`; rejected |
| `host_network: true` | Port publishing achieves the same with per-user remapping in the UI; rejected |
| Merge user file over generated config | Ambiguous list-merge semantics; replaced by explicit `options`/`file` modes |
| Route-level `allowed_users`/`allowed_domains` shorthand in generated config | Compiled to explicit PPL instead — the stable, documented form |
| Implicit allow-any-authenticated default | Dangerous with hosted authenticate (whole internet can log in); default-deny + explicit opt-in |
| Pomerium Zero mode | Deferred; could be added as a third `config_mode` later |

## Future work

- Optional metrics listener (`metrics_address`) + docs for scraping.
- Supervisor `watchdog` URL once the `/ping` behavior is validated in the field.
- More PPL criteria in the structured schema (claims, groups) if requested.
- Pomerium Zero (`config_mode: zero` + token) for cloud-managed policy.
