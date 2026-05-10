# Hardcoded base: HA Supervisor's build_from allowlist rejects mcr.microsoft.com,
# so a build.yaml with BUILD_FROM=mcr.microsoft.com/playwright:... silently falls
# back to ghcr.io/home-assistant/base:latest (Alpine) and apt-get breaks. The
# Microsoft image is multi-arch so Docker resolves amd64/arm64 from the manifest.
FROM mcr.microsoft.com/playwright:v1.57.0-noble

# Labels
LABEL maintainer="con-ant"
LABEL io.hass.version="0.1.0"
LABEL io.hass.type="addon"
LABEL io.hass.arch="amd64,aarch64"
LABEL org.opencontainers.image.title="Playwright Browser for Home Assistant"
LABEL org.opencontainers.image.description="Headless Chromium with CDP endpoint for browser automation"
LABEL org.opencontainers.image.source="https://github.com/con-ant/hass-addons"
LABEL org.opencontainers.image.licenses="MIT"

# jq is already available in the Playwright image (Ubuntu-based)
# Just ensure it's installed
RUN apt-get update && \
    apt-get install -y --no-install-recommends jq curl nginx && \
    rm -rf /var/lib/apt/lists/*

# Create data directory
RUN mkdir -p /data

# Copy run script
COPY run.sh /run.sh
RUN chmod +x /run.sh

# Expose CDP port
EXPOSE 9222

# Health check - verify CDP endpoint responds
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -sf http://localhost:9222/json/version || exit 1

CMD ["/run.sh"]
