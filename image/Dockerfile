# python:slim is a Docker Official Image, also used by BoxLite CodeBox.
# Build a complete environment, independently of any existing development Box.
# Pin BASE_IMAGE and GO_IMAGE to digests for a release rebuild.
ARG BASE_IMAGE=python:slim
ARG GO_IMAGE=golang:1.26.5-bookworm
FROM --platform=$BUILDPLATFORM ${GO_IMAGE} AS builder
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY cmd/web-terminal ./cmd/web-terminal
RUN go test ./...
ARG TARGETOS
ARG TARGETARCH
RUN CGO_ENABLED=0 GOOS=${TARGETOS} GOARCH=${TARGETARCH} \
    go build -trimpath -ldflags="-s -w" -o /out/web-terminal ./cmd/web-terminal

FROM ${BASE_IMAGE}

ARG BOXLITE_USER=boxlite
ARG CODEX_INSTALL_URL=https://chatgpt.com/codex/install.sh
ARG CODEX_EXPECTED_VERSION
ARG CLAUDE_INSTALL_URL=https://claude.ai/install.sh
ARG CURSOR_INSTALL_URL=https://cursor.com/install

USER root

RUN set -eux; \
    test -f /etc/debian_version; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      bash \
      ca-certificates \
      curl \
      fontconfig \
      fonts-noto-cjk \
      fonts-noto-color-emoji \
      git \
      locales \
      passwd \
      tar \
      tmux; \
    if ! id -u "${BOXLITE_USER}" >/dev/null 2>&1; then \
      useradd --create-home --shell /bin/bash "${BOXLITE_USER}"; \
    fi; \
    mkdir -p "/home/${BOXLITE_USER}/.local/bin"; \
    chown -R "${BOXLITE_USER}:${BOXLITE_USER}" "/home/${BOXLITE_USER}"; \
    rm -rf /var/lib/apt/lists/*

ENV HOME=/home/${BOXLITE_USER} \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    LC_CTYPE=C.UTF-8 \
    TERM=xterm-256color \
    PATH=/home/${BOXLITE_USER}/.local/bin:/home/${BOXLITE_USER}/.npm-global/bin:/usr/local/bin:/usr/bin:/bin

# Install Codex using the official standalone Linux installer. No
# ChatGPT/API credential is included in the image; authentication happens at
# runtime inside the Box.
USER ${BOXLITE_USER}
RUN curl -fsSL "${CODEX_INSTALL_URL}" -o /tmp/install-codex.sh \
    && CODEX_NON_INTERACTIVE=1 sh /tmp/install-codex.sh \
    && rm /tmp/install-codex.sh \
    && command -v codex \
    && codex --version \
    && if [ -n "${CODEX_EXPECTED_VERSION}" ]; then \
         test "$(codex --version)" = "${CODEX_EXPECTED_VERSION}"; fi

# Official Claude Code and Cursor CLI installers. No API credentials are
# included; authentication happens at runtime inside the Box.
RUN curl -fsSL "${CLAUDE_INSTALL_URL}" -o /tmp/install-claude.sh \
    && CI=1 bash /tmp/install-claude.sh stable \
    && rm /tmp/install-claude.sh \
    && command -v claude \
    && claude --version

RUN curl -fsSL "${CURSOR_INSTALL_URL}" -o /tmp/install-cursor.sh \
    && bash /tmp/install-cursor.sh \
    && rm /tmp/install-cursor.sh \
    && command -v agent \
    && agent --version

USER root
COPY --from=builder --chmod=0755 /out/web-terminal /usr/local/bin/web-terminal
RUN mkdir -p /usr/share/web-terminal \
    && dpkg-query -W > /usr/share/web-terminal/packages.txt
USER ${BOXLITE_USER}
WORKDIR /home/${BOXLITE_USER}
RUN codex --version > /home/${BOXLITE_USER}/codex-build-version.txt \
    && tmux -V && locale charmap | grep -qx UTF-8

EXPOSE 3001
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD curl -fsS http://127.0.0.1:3001/healthz >/dev/null || exit 1

# Keep the service in the foreground so BoxLite owns its lifecycle.
ENTRYPOINT ["/usr/local/bin/web-terminal"]
