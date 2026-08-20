# BUILD_FROM must be declared before the first FROM to be usable in a later
# FROM line (stage-scoped ARGs are not visible to FROM instructions).
# Supervisor always passes it from build.yaml; the default only makes a bare
# `docker build pomerium/` work for local testing.
ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-debian:bookworm

# Pomerium's official image is distroless (no package manager, no jq), so it
# can't consume /data/options.json directly. Instead, copy the multi-arch
# binary out of the pinned official image into the HA Debian base. Debian,
# not Alpine: the embedded Envoy that pomerium execs at runtime needs glibc
# (see build.yaml).
FROM pomerium/pomerium:v0.33.1 AS pomerium

FROM ${BUILD_FROM}

LABEL maintainer="con-ant"

# bash, curl (healthcheck) and jq (read /data/options.json) already ship in
# the HA Debian base; installing them is a cheap assertion. ca-certificates
# is needed for ACME and IdP calls.
RUN apt-get update && \
    apt-get install -y --no-install-recommends bash curl jq ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY --from=pomerium /bin/pomerium /usr/local/bin/pomerium

# Same default the upstream image sets, so a file-mode config with
# `autocert: true` and no `autocert_dir` keeps its ACME account and
# certificates in persistent /data instead of the container's writable layer
# (which is discarded on every restart). A config-file value still overrides.
ENV AUTOCERT_DIR=/data/autocert

COPY run.sh /run.sh
RUN chmod +x /run.sh

EXPOSE 443 80

# Options mode always listens on :443, where /ping is served regardless of
# Host (-k: the cert may be self-signed until autocert finishes issuing).
# File mode lets the user pick any address/scheme, so the probe cannot know
# where to look and is skipped rather than reporting a healthy proxy as
# unhealthy (which Supervisor's watchdog would restart in a loop).
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD [ "$(jq -r '.config_mode // "options"' /data/options.json 2>/dev/null)" = "file" ] \
        || curl -kfs https://127.0.0.1:443/ping || exit 1

CMD ["/run.sh"]
