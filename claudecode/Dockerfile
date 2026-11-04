ARG BUILD_FROM

# Build a current ttyd web client: the one embedded in the ttyd 1.7.7 binary
# predates OSC 52 clipboard support, so startup serves this build via --index.
# Built with yarn so ttyd's lockfile is honored (npm resolves newer transitive
# deps and fails type-checking). Everything is pinned - ttyd commit, corepack,
# lockfile deps - so the output is reproducible. COREPACK_INTEGRITY_KEYS=0
# keeps the pinned corepack working across npm signing-key rotations;
# integrity comes from the yarn.lock checksums. Drop this stage and the
# --index flag once a ttyd release newer than 1.7.7 ships.
#
# KNOWN FLAKE, CI ONLY: under QEMU aarch64 emulation this stage has died with
# SIGILL (exit 132) on one run and passed on the next with identical input -
# Node's JIT and QEMU's TCG do not get along. It does not affect users: the
# Supervisor builds the add-on natively on their own hardware, with no
# emulation. Re-run the CI job when it happens.
#
# Do NOT "fix" this with FROM --platform=$BUILDPLATFORM. That variable only
# exists under BuildKit; the Supervisor's classic builder leaves it empty and
# the build dies on this very line with "failed to parse platform". Verified
# 2026-11-01.
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

FROM ${BUILD_FROM}

# Labels
LABEL maintainer="Robson Felix"
LABEL org.opencontainers.image.title="Claude Code for Home Assistant"
LABEL org.opencontainers.image.description="Claude Code CLI with web terminal and HA integration"
LABEL org.opencontainers.image.source="https://github.com/robsonfelix/robsonfelix-hass-addons"
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

# Install Claude Code.
# 2.1.113 replaced the JS entrypoint with a Bun-compiled native binary, whose
# JavaScriptCore requires AVX. Hosts that lack it - notably VMs exposing the
# generic kvm64 CPU model - cannot run 2.1.113+ (it deadlocks or dies with
# SIGILL). Fix that at the hypervisor (Proxmox CPU type "host"), not here.
# No version is hardcoded: take latest when it runs, otherwise the newest
# release that passes a smoke test, so the add-on still builds on such hosts.
COPY stdin-runner.sh /usr/local/bin/stdin-runner.sh
RUN chmod +x /usr/local/bin/stdin-runner.sh

# The package pulls a ~240MB platform-native binary via optional deps, so clean
# the npm cache and temp dirs in the SAME layer - otherwise ~100MB+ of cache
# gets baked into the image.
COPY install-claude.sh /usr/local/bin/install-claude.sh
RUN chmod +x /usr/local/bin/install-claude.sh \
    && /usr/local/bin/install-claude.sh \
    && npm cache clean --force \
    && rm -rf /root/.npm /tmp/*

# Install hass-mcp for Home Assistant integration and pymodbus for Modbus operations
RUN pip3 install --no-cache-dir --break-system-packages hass-mcp pymodbus pyserial

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
# (curl exit 6). Upstream publishes only amd64 and aarch64, so other arches skip
# the CLI with a warning instead of failing the build on a 404.
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

# Create necessary directories (NOT /root/.claude - will be symlinked at runtime)
RUN mkdir -p \
    /homeassistant \
    /root/.config \
    /root/.local/bin

# Add-on-managed CLAUDE.md (copied to the persistent dir at runtime)
COPY CLAUDE.addon.md /usr/share/claudecode/CLAUDE.addon.md

# Shell and tmux config.
# These were heredocs (RUN cat > file << 'EOF'), which require BuildKit and a
# `# syntax=docker/dockerfile:1.4` directive. Builders without heredoc support
# parsed the body as Dockerfile instructions ("unknown instruction: set").
# COPY works on every builder and needs no syntax image.
COPY rootfs/root/.tmux.conf /root/.tmux.conf
COPY rootfs/root/.bashrc /root/.bashrc
COPY rootfs/usr/local/bin/claude-auto-launch /usr/local/bin/claude-auto-launch
RUN chmod +x /usr/local/bin/claude-auto-launch

# Playwright MCP launcher: resolves the Playwright Browser hostname each time
# the MCP server starts (see the script for why this can't happen at boot).
COPY rootfs/usr/local/bin/playwright-mcp-launch /usr/local/bin/playwright-mcp-launch
RUN chmod +x /usr/local/bin/playwright-mcp-launch

# Helper to pull the OAuth login URL out of the tmux session and save it to a
# file, working around ttyd/xterm.js not supporting OSC 52 clipboard writes
# and its link detector only scanning one wrapped row at a time.
COPY rootfs/usr/local/bin/claude-login-url /usr/local/bin/claude-login-url
RUN chmod +x /usr/local/bin/claude-login-url

# ha-ws: sends commands to Home Assistant's WebSocket API through the Supervisor
# proxy (registry updates, lovelace/config/save, ... that the REST API lacks and
# that must not be done by editing .storage/ while HA runs). Stdlib only.
COPY rootfs/usr/local/bin/ha-ws /usr/local/bin/ha-ws
RUN chmod +x /usr/local/bin/ha-ws

# The startup script (the container's CMD). The add-on version is not otherwise
# visible inside the container; the script's first log line prints it. The
# Home Assistant builder passes BUILD_VERSION.
COPY rootfs/usr/local/bin/claudecode-start /usr/local/bin/claudecode-start
RUN chmod +x /usr/local/bin/claudecode-start
ARG BUILD_VERSION
RUN echo "${BUILD_VERSION:-dev}" > /etc/claudecode-addon-version

# Ensure .bashrc is sourced for login shells too
RUN echo 'source ~/.bashrc' > /root/.profile

# Set up PATH. The extra_npm_packages install dir is on the persistent HA
# volume so user-installed MCP servers survive add-on rebuilds.
ENV PATH="/root/.local/bin:/homeassistant/.claudecode/npm-global/bin:${PATH}"

# Set working directory
WORKDIR /homeassistant

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -sf http://localhost:7681/ || exit 1

# Startup: read the options, wire up the persistent config dir, configure
# Claude Code, then hand off to ttyd. Everything the container does at boot
# is in that one script (rootfs/usr/local/bin/claudecode-start).
ENTRYPOINT []
CMD ["/usr/local/bin/claudecode-start"]
