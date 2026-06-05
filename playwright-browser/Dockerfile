# Multi-stage build. The full mcr.microsoft.com/playwright image bundles
# Chromium + Firefox + WebKit and all their deps, which is why it ballooned to
# ~2.7GB. This add-on only needs headless Chromium with a CDP endpoint, so we
# download Chromium ONLY in a builder stage and copy just the browser into a
# slim runtime image.
#
# FROM is hardcoded (not a build.yaml BUILD_FROM arg): HA Supervisor's build_from
# allowlist only gates build.yaml, so a literal FROM in the Dockerfile is used
# as-is. node:*-slim is multi-arch, so Docker resolves amd64/arm64 from the
# manifest (covers amd64 + aarch64).

ARG PLAYWRIGHT_VERSION=1.57.0

# ---- builder: download Chromium only ----
FROM node:20-bookworm-slim AS builder
ARG PLAYWRIGHT_VERSION
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
# Install only the Chromium browser. Firefox/WebKit are never downloaded. Drop
# the headless-shell and ffmpeg builds (run.sh launches full Chromium with
# --headless), and clean the npm cache + temp dirs in the SAME layer.
RUN npm install -g playwright@${PLAYWRIGHT_VERSION} \
    && playwright install chromium \
    && rm -rf /ms-playwright/chromium_headless_shell-* /ms-playwright/ffmpeg-* \
    && npm cache clean --force \
    && rm -rf /root/.npm /tmp/*

# ---- final: slim runtime ----
FROM node:20-bookworm-slim
ARG PLAYWRIGHT_VERSION
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Labels
LABEL maintainer="con-ant"
LABEL io.hass.version="0.1.0"
LABEL io.hass.type="addon"
LABEL io.hass.arch="amd64,aarch64"
LABEL org.opencontainers.image.title="Playwright Browser for Home Assistant"
LABEL org.opencontainers.image.description="Headless Chromium with CDP endpoint for browser automation"
LABEL org.opencontainers.image.source="https://github.com/con-ant/hass-addons"
LABEL org.opencontainers.image.licenses="MIT"

# Install Chromium's runtime shared libraries (resolved via Playwright's own
# dependency list so nothing is missed), plus nginx/jq/curl for the CDP proxy
# and run script, and hostname for run.sh. Playwright is only needed to resolve
# the dep list, so it's removed in the same layer; apt lists and npm cache are
# cleaned in the same RUN too.
RUN apt-get update \
    && PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm install -g playwright@${PLAYWRIGHT_VERSION} \
    && playwright install-deps chromium \
    && apt-get install -y --no-install-recommends jq curl nginx hostname ca-certificates \
    && npm uninstall -g playwright \
    && npm cache clean --force \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /root/.npm /tmp/* \
    && mkdir -p /data

# Copy only the Chromium browser from the builder (Firefox/WebKit never installed)
COPY --from=builder /ms-playwright /ms-playwright

# Copy run script
COPY run.sh /run.sh
RUN chmod +x /run.sh

# Expose CDP port
EXPOSE 9222

# Health check - verify CDP endpoint responds
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -sf http://localhost:9222/json/version || exit 1

CMD ["/run.sh"]
