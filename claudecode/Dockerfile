ARG BUILD_FROM

# Build a current ttyd web client. The client embedded in the ttyd 1.7.7
# binary predates OSC 52 clipboard support (xterm.js 5.4, no clipboard addon),
# so startup serves this one via --index instead. Built from a pinned ttyd
# main commit with yarn (via corepack, fetched from the npm registry) so
# yarn.lock is honored - a plain npm install resolves newer transitive deps
# and fails type-checking. Everything is pinned (ttyd commit, corepack, and
# the lockfile-resolved deps) so the output is reproducible and nothing
# drifts between builds. COREPACK_INTEGRITY_KEYS=0 keeps a pinned corepack
# working across npm signing-key rotations (which have broken pinned
# corepacks before); transport is TLS and dependency integrity comes from
# the yarn.lock checksums. Drop this stage and the --index flag once a ttyd
# release newer than 1.7.7 ships and the download below is bumped - but keep
# the shim injection below (or re-verify it is no longer needed).
FROM ${BUILD_FROM} AS ttyd-client
RUN apk add --no-cache curl nodejs npm
ARG TTYD_CLIENT_COMMIT=2922cb89f518bae4d0fcf4d757a7419638fc71fc
RUN curl -fsSL --retry 5 --retry-delay 2 --retry-all-errors \
        "https://github.com/tsl0922/ttyd/archive/${TTYD_CLIENT_COMMIT}.tar.gz" \
        | tar xz && mv "ttyd-${TTYD_CLIENT_COMMIT}" /ttyd
WORKDIR /ttyd/html
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    COREPACK_NPM_REGISTRY=https://registry.npmjs.org \
    COREPACK_INTEGRITY_KEYS=0
RUN npm install -g corepack@0.35.0 && corepack enable && \
    yarn install --immutable && yarn run inline
# The client bundle captures the WebAssembly global at module-init time (via
# xterm-addon-image / inwasm). Chrome removes that global entirely when the
# V8 JIT is blocked (enterprise policy DefaultJavaScriptJitSetting=2, or the
# "JavaScript optimisation" site setting), so on such browsers the script
# throws before rendering anything and the ingress panel is a blank white
# iframe. Splice in a placeholder so init survives; the terminal itself never
# touches wasm (only sixel images do, and enableSixel is never set here).
COPY ttyd-client/ /ttyd-client/
RUN node /ttyd-client/inject-wasm-shim.js /ttyd/html/dist/inline.html /ttyd-client/wasm-shim.html
# A gear in the terminal's top right corner opens the add-on's Configuration
# tab in Home Assistant (/config/app/<slug>/config); see settings-button.html.
RUN node /ttyd-client/inject-snippet.js /ttyd/html/dist/inline.html /ttyd-client/settings-button.html
RUN node /ttyd-client/inject-snippet.js /ttyd/html/dist/inline.html /ttyd-client/sessions-badge.html

FROM ${BUILD_FROM}

# Labels
LABEL maintainer="con-ant"
LABEL org.opencontainers.image.title="Claude Code for Home Assistant"
LABEL org.opencontainers.image.description="Claude Code CLI with web terminal and HA integration"
LABEL org.opencontainers.image.source="https://github.com/con-ant/hass-addons"
LABEL org.opencontainers.image.licenses="MIT"

# Environment setup
ENV \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TERM=xterm-256color

# Install system dependencies (ttyd installed separately as static binary)
RUN apk add --no-cache \
    bash \
    curl \
    git \
    github-cli \
    jq \
    nodejs \
    npm \
    openssh-client \
    libstdc++ \
    ncurses \
    vim \
    nano \
    tmux \
    tzdata \
    coreutils \
    findutils \
    grep \
    sed \
    gawk \
    ca-certificates \
    openssl \
    ripgrep \
    p7zip \
    poppler-utils \
    socat \
    docker-cli \
    && update-ca-certificates

# Install ttyd static binary (avoids libwebsockets issues); the web client
# it serves comes from the ttyd-client build stage above (see --index).
ARG BUILD_ARCH
RUN case "${BUILD_ARCH}" in \
        amd64) TTYD_ARCH="x86_64" ;; \
        aarch64) TTYD_ARCH="aarch64" ;; \
        armv7|armhf) TTYD_ARCH="armhf" ;; \
        i386) TTYD_ARCH="i686" ;; \
        *) TTYD_ARCH="x86_64" ;; \
    esac && \
    curl -fsSL --retry 5 --retry-delay 2 --retry-all-errors \
        "https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.${TTYD_ARCH}" -o /usr/bin/ttyd && \
    chmod +x /usr/bin/ttyd
COPY --from=ttyd-client /ttyd/html/dist/inline.html /usr/share/ttyd/index.html

# Install hass-mcp for Home Assistant integration and pymodbus for Modbus operations.
# hass-mcp is pinned: the job broker (docs/DESIGN-claude-jobs.md §4.5) allow-lists
# hass-mcp's one fixed /api/template query by importing it, and the design's
# tool/transport map was verified against 0.6.0. Bump deliberately.
RUN pip3 install --no-cache-dir --break-system-packages "hass-mcp==0.6.0" pymodbus pyserial

# Claude Jobs (docs/DESIGN-claude-jobs.md): the job validator/runner/endpoint are
# Python 3 stdlib plus these two. They must go through pip: the base image's
# Python 3.13 lives in /usr/local, so Alpine's py3-* apk packages (built for the
# distro python) would be invisible to it. jsonschema pulls rpds-py, which ships
# musllinux wheels for both arches this add-on builds.
RUN pip3 install --no-cache-dir --break-system-packages "pyyaml>=6,<7" "jsonschema>=4.18,<5"

# Install mbpoll (Modbus command line tool) from source
RUN apk add --no-cache --virtual .build-deps \
    build-base \
    cmake \
    libmodbus-dev \
    && git clone https://github.com/epsilonrt/mbpoll.git /tmp/mbpoll \
    && cd /tmp/mbpoll \
    && mkdir build && cd build \
    && cmake .. \
    && make \
    && make install \
    && cd / \
    && rm -rf /tmp/mbpoll \
    && apk del .build-deps \
    && apk add --no-cache libmodbus

# Install @playwright/mcp into /opt to avoid colliding with hass-mcp's
# /usr/local/bin/mcp (both packages would otherwise install a binary named
# `mcp` at the same path). Then symlink the actual playwright-mcp binary
# (different name, no collision) into /usr/local/bin so it lives in PATH and
# in a conventional location — Claude Code's `auto` permission classifier
# treats /opt as unusual.
RUN npm install -g --prefix=/opt/playwright-mcp @playwright/mcp && \
    ln -s /opt/playwright-mcp/bin/playwright-mcp /usr/local/bin/playwright-mcp && \
    npm cache clean --force && \
    rm -rf /root/.npm /tmp/*

# Install Home Assistant CLI (ha command)
# Pinned and retried: a transient DNS failure used to abort the whole build
# (curl exit 6), and "releases/latest" could silently change under us. Upstream
# publishes only amd64 and aarch64 (the only arches this add-on builds for);
# anything else skips the CLI with a warning instead of failing on a 404.
ARG HA_CLI_VERSION=5.2.0
RUN case "${BUILD_ARCH}" in \
        amd64|aarch64) \
            curl -fsSL --retry 5 --retry-delay 2 --retry-all-errors \
                "https://github.com/home-assistant/cli/releases/download/${HA_CLI_VERSION}/ha_${BUILD_ARCH}" \
                -o /usr/local/bin/ha && \
            chmod +x /usr/local/bin/ha ;; \
        *) \
            echo "[WARN] Home Assistant CLI is not published for ${BUILD_ARCH}; the 'ha' command will be unavailable" ;; \
    esac

# Install Claude Code.
# CLAUDE_CODE_VERSION is an npm dist-tag or an exact version. The default
# follows the stable channel (typically about a week behind latest, skipping
# releases with major regressions); `latest` follows every release as it
# ships; `2.1.308` forces that one. Override it per build with
# `args:` in build.yaml or `docker build --build-arg`.
# The ADD fetches the registry manifest for that tag on every build, so this
# layer re-runs when the channel moves and stays cached otherwise. Without it
# the layer would be reused forever and every rebuild would keep the Claude
# Code of the first build: Supervisor builds with the layer cache (--pull, no
# --no-cache). install-claude.sh installs the version the manifest names, so
# the install matches the cache key; it also handles hosts the release cannot
# run on (AVX, see the script). This step sits after the other installs so a
# new Claude Code release only rebuilds this layer and the ones below it.
# The package pulls a ~240MB platform-native binary via optional deps, so clean
# the npm cache and temp dirs in the SAME layer - otherwise ~100MB+ of cache
# gets baked into the image.
ARG CLAUDE_CODE_VERSION=stable
ADD https://registry.npmjs.org/@anthropic-ai/claude-code/${CLAUDE_CODE_VERSION} /tmp/claude-code-manifest.json
COPY install-claude.sh /usr/local/bin/install-claude.sh
RUN chmod +x /usr/local/bin/install-claude.sh \
    && CLAUDE_CODE_VERSION="${CLAUDE_CODE_VERSION}" /usr/local/bin/install-claude.sh \
    && npm cache clean --force \
    && rm -rf /root/.npm /tmp/*

# Create necessary directories (NOT /root/.claude - will be symlinked at runtime)
RUN mkdir -p \
    /homeassistant \
    /root/.config \
    /root/.local/bin

# Add-on-managed CLAUDE.md (copied to the persistent dir at runtime)
COPY CLAUDE.addon.md /usr/share/claudecode/CLAUDE.addon.md

# Shell config, tmux config, and helper scripts.
# These were heredocs (RUN cat > file << 'EOF'), which require BuildKit and a
# `# syntax=docker/dockerfile:1.4` directive. Builders without heredoc support
# parsed the body as Dockerfile instructions ("unknown instruction: set").
# COPY works on every builder and needs no syntax image.
COPY rootfs/ /
RUN chmod +x /usr/local/bin/playwright-mcp-launch /usr/local/bin/claude-auto-launch /usr/local/bin/claudecode-start \
    /usr/local/bin/claude-session-hook /usr/local/bin/claude-update-watcher \
    /usr/local/bin/claude-login-url /usr/local/bin/stdin-runner.sh \
    /usr/local/bin/claude-job /usr/local/bin/claude-job-notify /usr/local/bin/claude-job-endpoint /usr/local/bin/ha-ws \
    /usr/local/share/claudecode/git-hooks/pre-commit \
    /usr/local/lib/claude-job/ha_broker.py /usr/local/lib/claude-job/bin/ha \
    && python3 -m compileall -q /usr/local/lib/claude-job
# ^ bytecode is compiled at build time: /usr/local is read-only under the
#   AppArmor profile, so a runtime __pycache__ write would be denied (harmless,
#   but one audit line per job run).

# The add-on version is not otherwise visible inside the container; the job
# endpoint reports it on /health and GET /jobs. HA's builder passes BUILD_VERSION.
ARG BUILD_VERSION
RUN echo "${BUILD_VERSION:-dev}" > /etc/claudecode-addon-version

# Ensure .bashrc is sourced for login shells too
RUN echo 'source ~/.bashrc' > /root/.profile

# Set up PATH. The extra_npm_packages install dir is on the persistent HA
# volume so user-installed MCP servers survive add-on rebuilds.
ENV PATH="/root/.local/bin:/homeassistant/.claudecode/npm-global/bin:${PATH}"

# npm's cache on /data, so the Claude Code release the add-on updated itself
# to survives a restart (every restart recreates the container): at boot,
# claudecode-start reinstalls it from this cache, offline. The cache holds
# downloaded packages; the CLI still runs from /usr/local, and npx's scratch
# area in the cache is cleared at every start. Set after every build-time
# npm step, which must not write there.
ENV npm_config_cache=/data/npm-cache

# Set working directory
WORKDIR /homeassistant

# Health check. ttyd binds 7681 only at the END of startup, and a legitimate
# boot can spend many minutes before that (bringing back the last Claude Code
# update: up to 180s offline install, a 30s probe and a 300s rollback; then
# auto_update_claude: 300s npm install, plus a 300s rollback if the update
# cannot run; plus probes and MCP setup - and every restart recreates the
# container, redoing all of it). start-period must cover that worst case
# (about 1200s with everything failing): failures during it don't
# count, one success marks healthy immediately, so a fast boot loses nothing.
# With a short start-period the Supervisor Watchdog kills a slow boot at
# ~2min and loops forever, npm never finishing before the restart.
HEALTHCHECK --interval=30s --timeout=10s --start-period=1500s --retries=3 \
    CMD curl -sf http://localhost:7681/ || exit 1

# Startup logic lives in rootfs/usr/local/bin/claudecode-start: it reads the
# options, wires /root/.claude to the persistent /homeassistant/.claudecode,
# configures Claude Code, and runs ttyd. It never sets -e; a failing step
# warns and the terminal still comes up.
ENTRYPOINT []
CMD ["/usr/local/bin/claudecode-start"]
