# Pomerium's official image is distroless (no shell, no jq), so it can't
# consume /data/options.json directly. Instead, copy the multi-arch static
# binary out of the pinned official image into the standard HA Alpine base.
FROM pomerium/pomerium:v0.33.1 AS pomerium

ARG BUILD_FROM
FROM ${BUILD_FROM}

LABEL maintainer="con-ant"

# jq: read /data/options.json; yq: pretty-print generated config to YAML;
# curl: healthcheck. bash/ca-certificates ship with the base but are cheap
# to assert.
RUN apk add --no-cache bash curl jq yq ca-certificates && \
    update-ca-certificates

COPY --from=pomerium /bin/pomerium /usr/local/bin/pomerium

COPY run.sh /run.sh
RUN chmod +x /run.sh

EXPOSE 443 80

# /ping is served on the HTTPS listener regardless of Host; -k because the
# cert may be self-signed until autocert finishes issuing.
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -kfs https://127.0.0.1:443/ping || exit 1

CMD ["/run.sh"]
