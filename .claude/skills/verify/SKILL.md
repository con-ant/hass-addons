---
name: verify
description: Build and drive the Home Assistant add-ons in this repo end-to-end with plain docker (no Supervisor) — currently covers pomerium/.
---

# Verify (hass-addons)

No Supervisor here; emulate it: build with the `BUILD_FROM` from the add-on's
`build.yaml`, mount a synthetic `/data/options.json` (mirror `config.yaml`
`options:`), plus `/ssl` and `/config` (= `addon_config`) as needed.

## pomerium/

```bash
docker build -t pomerium-ha-verify pomerium/   # BUILD_FROM defaults to the amd64 Debian base;
                                               # pass --build-arg BUILD_FROM=... to test another arch/base
S=$(mktemp -d); mkdir -p $S/data $S/ssl $S/cfg; chmod 777 $S/*
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj /CN=ha.myhome.test \
  -keyout $S/ssl/privkey.pem -out $S/ssl/fullchain.pem
```

- **One-shot run.sh (see exit code):**
  `docker run --rm -v $S/data:/data --entrypoint /run.sh pomerium-ha-verify`
  — with the shipped defaults (route `home.example.com`) expect `[ERROR] ... placeholder` and exit 1.
- **Options mode under s6:** options.json with `autocert:false`, `certfile/keyfile`
  = the pem names, a route `https://ha.myhome.test -> http://homeassistant:8123`
  with `allow_any_authenticated_user:true`.
  `docker run -d --name pomv -v $S/data:/data -v $S/ssl:/ssl:ro pomerium-ha-verify`
  then poll `docker inspect -f '{{.State.Health.Status}}' pomv` until `healthy` (~30s).
  Checks: `docker exec pomv curl -kfs -o /dev/null -w '%{http_code}' https://127.0.0.1:443/ping` → 200;
  `stat -c %a /data/pomerium.yaml /data/cookie_secret` → 600; `docker logs pomv | grep -c deprecated` → 0;
  `jq 'del(.cookie_secret)' /data/pomerium.yaml` to eyeball the generated config.
- **File mode:** options.json `config_mode:file`; put `pomerium.yaml` in `$S/cfg`
  and add `-v $S/cfg:/config`. Use a non-443 `address` to prove the HEALTHCHECK
  no-ops. Log shows whether `COOKIE_SECRET` was injected; confirm via
  `/proc/<pomerium pid>/environ` (no `pgrep` in the image — loop `/proc/*/comm`).

Gotchas: files in `/data` are written as root with umask 077 — `chmod 777` the
host dir and `rm -f` between runs. IdP misconfig (e.g. okta without
`idp_provider_url`) does **not** fail at startup; it only surfaces on login.
`docker rm -f pomv` when done.
