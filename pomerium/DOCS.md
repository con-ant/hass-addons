# Pomerium Add-on Documentation

This add-on runs [Pomerium](https://www.pomerium.com/) (open source,
all-in-one mode) as an identity-aware reverse proxy in front of Home
Assistant and other add-ons. Every request must pass login and your access
rules before it reaches a backend.

## How traffic flows

```
Internet ── DNS (home.example.com → your public IP)
   │
Router ── forward 443 (+80) → Home Assistant host
   │
Pomerium add-on (:443 HTTPS, :80 redirect)
   │              internal add-on network
   ├── http://homeassistant:8123      (Home Assistant)
   ├── http://local-myaddon:8080      (a locally built add-on)
   └── http://a0d7b954-vscode:1337    (a store add-on)
```

Backends are reached over the Supervisor's internal Docker network, so they
do **not** need their ports published on the host. Pomerium is the only
thing exposed.

## Setup

### 1. DNS and port forwarding

- Create a DNS record per route (e.g. `home.example.com`) pointing at your
  public IP (or a wildcard `*.example.com`).
- Forward TCP 443 to your Home Assistant host. Also forward TCP 80 if you
  use `autocert` (recommended) or the HTTP→HTTPS redirect.
- If 443/80 are already taken on your host, remap or disable them in the
  add-on's **Network** section — but Let's Encrypt requires the proxy to be
  reachable from the internet on 443 (and 80 for HTTP-01).

### 2. Add-on options

Minimal working configuration:

```yaml
autocert: true
routes:
  - from: "https://home.example.com"
    to: "http://homeassistant:8123"
    allow_websockets: true
    allowed_users:
      - you@gmail.com
```

With no identity-provider options set, Pomerium uses its hosted
authenticate service (`authenticate.pomerium.app`): users sign in with
Google, GitHub, Microsoft, or Apple, and your route rules decide who gets
through. Nothing to register.

### 3. Tell Home Assistant to trust the proxy

Home Assistant rejects proxied requests unless the proxy is trusted. Add to
`configuration.yaml` and restart Home Assistant:

```yaml
http:
  use_x_forwarded_for: true
  trusted_proxies:
    - 172.30.32.0/23   # the internal add-on network
```

### 4. Start and sign in

Start the add-on, watch the log for the route summary, then open your
`from` URL and sign in. The add-on refuses to start while any route still
uses the shipped `example.com` placeholder. The first Let's Encrypt issuance
can take up to a minute; until then you may see a self-signed certificate
warning.

## Routes and access rules

Each entry in `routes`:

| Field | Default | Meaning |
|---|---|---|
| `from` | — | Public URL, `https://host.example.com` |
| `to` | — | Backend URL on the internal network |
| `allow_websockets` | `true` | Required for the Home Assistant frontend |
| `preserve_host_header` | `false` | Send the original `Host` to the backend |
| `pass_identity_headers` | `false` | Add `X-Pomerium-Claim-*` headers (user identity) for the backend |
| `allowed_users` | `[]` | Emails allowed on this route |
| `allowed_domains` | `[]` | Email domains allowed (e.g. `example.com`) |
| `allow_any_authenticated_user` | `false` | Allow anyone who can log in — see warning |

A route with **no** access fields denies all traffic. This is deliberate:
with the hosted authenticate service, "any authenticated user" means anyone
on the internet with a Google/GitHub/Microsoft/Apple account, so it must be
an explicit opt-in. The add-on log warns about routes that deny everything.

Finding a backend address: Home Assistant core is always
`http://homeassistant:8123`. For an add-on, use its hostname — shown on the
add-on's info page (e.g. `a0d7b954-vscode`) — plus the port from its
documentation. Host port mappings are not needed for routed add-ons.

## TLS options

- `autocert: true` (default) — Pomerium obtains and renews Let's Encrypt
  certificates for every `from` domain. The certificate cache is kept in
  the add-on's private data and survives restarts and updates. Set
  `autocert_staging: true` while testing to avoid Let's Encrypt rate limits
  (browsers will show a warning; switch it off when your setup works).
- `autocert: false` — use existing certificates from `/ssl`, e.g. from the
  Let's Encrypt add-on:

  ```yaml
  autocert: false
  certfile: fullchain.pem
  keyfile: privkey.pem
  ```

`http_redirect: true` (default) listens on port 80 and redirects to HTTPS;
it also serves ACME HTTP-01 challenges for autocert.

## Using your own identity provider

To self-host authentication instead of using `authenticate.pomerium.app`,
register an OAuth/OIDC client with your provider and set:

```yaml
authenticate_service_url: "https://authenticate.example.com"  # must also be a DNS name → your IP
idp_provider: google        # google | github | azure | okta | auth0 | oidc | ...
idp_client_id: "..."
idp_client_secret: "..."
idp_provider_url: ""        # issuer URL - see below
```

`idp_provider_url` can stay empty only for providers with a built-in issuer
(google, github, azure, apple, gitlab, onelogin). It is **required** for
okta, auth0, cognito, ping, generic `oidc`, and any other provider whose
issuer is specific to your tenant (e.g. `https://YOUR-ORG.okta.com`,
`https://YOUR-TENANT.auth0.com/`).

The authenticate URL's domain needs DNS and (with autocert) is issued a
certificate automatically. Provider-specific setup (redirect URLs, scopes)
is in the [Pomerium identity provider docs](https://www.pomerium.com/docs/identity-providers).

## Advanced: full config file mode

If the options above are too limiting (custom PPL policies, headers,
timeouts, mTLS, …), set `config_mode: file` and write a complete
[Pomerium configuration](https://www.pomerium.com/docs/reference) to
`pomerium.yaml` in the add-on's config directory — on the host that is
`/addon_configs/<repo>_pomerium/pomerium.yaml` (visible via the Samba/SSH
add-ons). The file is used verbatim; add-on options other than
`config_mode` are ignored.

If your file does not set `cookie_secret` (or `cookie_secret_file`), the
add-on injects the one it manages via the environment, so you don't have to
handle key material. If it uses `autocert: true` without an `autocert_dir`,
certificates are cached in the add-on's persistent data automatically.

## Secrets and state

- `cookie_secret` (session cookie encryption) is generated once and
  persisted in the add-on's private data — you never need to set it, and
  cookies issued before a restart or update remain valid. Session *records*
  live in Pomerium's in-memory store, though, so after a restart you are
  sent through sign-in again (usually instant with the hosted authenticate
  service; a full login with your own identity provider).
- The generated Pomerium config lives at `/data/pomerium.yaml` inside the
  container (mode 0600, contains secrets). It is regenerated from the
  options on every start; option changes take effect on restart.

## Troubleshooting

- **400 Bad Request from Home Assistant** — the `http:` trusted-proxy
  settings from step 3 are missing, or Home Assistant wasn't restarted.
- **Login loop or "route not found"** — the hostname you're visiting doesn't
  exactly match a route's `from`.
- **Certificate errors that persist** — check the log for ACME errors; the
  domain must resolve to your public IP and ports 443/80 must reach the
  add-on. Behind CGNAT, autocert's TLS-ALPN/HTTP-01 can't work — use DNS
  challenges via the Let's Encrypt add-on and `autocert: false` instead.
- **Everything is denied** — expected for routes without access fields; add
  `allowed_users`, `allowed_domains`, or `allow_any_authenticated_user`.
- Set `log_level: debug` for more detail.
