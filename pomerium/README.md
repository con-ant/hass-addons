# Pomerium Add-on for Home Assistant

[Pomerium](https://www.pomerium.com/) is an identity-aware reverse proxy. This
add-on puts it in front of Home Assistant and your other add-ons so you can
reach them from the internet behind single sign-on — no VPN, and no exposing
each service's port individually.

## What you get

- One HTTPS entry point (port 443) for Home Assistant and any add-on,
  routed by domain name.
- Login enforced before any request reaches a backend. The quick start uses
  Pomerium's hosted authenticate service (sign in with Google, GitHub,
  Microsoft, or Apple) — no identity-provider registration needed. You can
  also bring your own OIDC provider.
- Access rules per route: specific email addresses, whole email domains, or
  any authenticated user. Routes with no rule deny everything — safe by
  default.
- Automatic HTTPS via Let's Encrypt (`autocert`), or reuse the certificates
  in `/ssl` from the Let's Encrypt add-on.
- Backends stay internal: Pomerium reaches Home Assistant at
  `http://homeassistant:8123` and other add-ons by their hostname over the
  internal network, so they don't need host ports at all.

## Quick start

1. Point a DNS record (e.g. `home.example.com`) at your public IP and forward
   ports 443 and 80 from your router to your Home Assistant host.
2. Install the add-on, set your route's `from` domain and your email in
   `allowed_users`, and start it.
3. Add the trusted-proxy settings to Home Assistant's `configuration.yaml`
   (see the Documentation tab) and restart Home Assistant.
4. Open `https://home.example.com` and sign in.

Full setup, route examples, custom identity providers, and the advanced
config-file mode are in the **Documentation** tab (DOCS.md).

## Support

Issues and feature requests: https://github.com/con-ant/hass-addons/issues
