# Web Terminal Architecture

## Overview

The project provides a custom Go Web Terminal for environments that need a
controllable browser UI. The service runs an independent PTY shell inside the
Box and serves the browser application over HTTP and WebSocket on port `3001`.

Only port `3001` is part of the public service contract. Port `22222` is
reserved for internal platform use and must not be exposed through public
ingress, preview URLs, Docker publishing, or marketplace manifests.

## Runtime Contents

The runtime environment should include:

- Codex CLI
- Claude Code (`claude`)
- Cursor CLI (`agent` / `cursor-agent`)
- tmux
- The Go Web Terminal binary
- Bash
- A UTF-8 locale and fonts with CJK coverage
- Web Terminal startup configuration

The runtime image must not include:

- BoxLite API keys
- ChatGPT or OpenAI credentials
- Registry credentials
- SSH private keys
- Customer files or fixed user data

## Go Terminal Service

The implementation uses established libraries:

- `github.com/gorilla/websocket`
- `github.com/creack/pty/v2`

The service supports:

- Standard WebSocket upgrades
- Binary WebSocket frames for raw PTY output
- Ping/Pong keepalive handling
- Close handling and PTY cleanup
- Serialized WebSocket writes
- Terminal resize messages
- A UTF-8 locale
- `TERM=xterm-256color`
- A PATH that includes Codex, Claude Code, and Cursor CLI (`~/.local/bin`)

PTY output must use binary WebSocket frames. Sending arbitrary PTY bytes as
text frames can cause browsers to close the connection when output is not
valid UTF-8.

## Browser UI

The browser client uses xterm.js and supports configurable themes, fonts,
cursor behavior, scrollback, resizing, Unicode handling, connection messages,
and product-specific controls. CJK-capable fallback fonts are configured for
terminals that display East Asian text.

The current browser assets are loaded from jsDelivr, so the browser must be
able to reach that CDN.

## Authentication and Preview Access

The terminal process does not implement end-user authentication. A deployment
platform must place an authenticated reverse proxy in front of port `3001` and
must protect both HTTP and WebSocket traffic.

Preview URLs should be short lived and treated as sensitive. Authentication
tokens, API keys, and session credentials must not be embedded in images,
source code, URLs committed to Git, or ordinary logs.

## Box Orchestration Boundary

BoxLite API or SDK calls belong in a trusted deployment backend, not inside an
untrusted terminal session. The development scripts support two backends:

- Cloud REST (default): `Boxlite.rest(...)` against `app.boxlite.ai`
- Local embedded: `Boxlite.default()`, with guest port `3001` forwarded to
  `127.0.0.1`

Select the backend with `BOXLITE_RUNTIME=cloud` or `BOXLITE_RUNTIME=embedded`.
The backend is responsible for:

- Creating, starting, stopping, and deleting Boxes
- Applying CPU, memory, disk, and network policy
- Injecting only the required configuration
- Starting the service exactly once
- Performing readiness checks on `/healthz`
- Issuing authenticated preview access for port `3001` on cloud, or loopback
  port forwarding on the local embedded runtime
- Enforcing timeouts and cleanup

The host control console (`python3 -m webterminal.control` + `webterminal/control.html`) is the
interactive operator UI for that backend. It binds loopback by default, lets
the operator pick cloud or local, and shows each Box's access URL. Cloud URLs
include `BOXLITE_BOX_AUTH_KEY` and must be treated as secrets.

The deployment scripts in this repository are development tools. They are not
a replacement for a production orchestration API.

## Agent CLI Authentication

Interactive logins can be initiated inside the Box after provision:

```bash
codex login --device-auth
claude auth login
agent login
```

One-time codes must expire, must not appear in ordinary logs, and must never be
shown to another user. Any platform-managed API credentials must remain in the
trusted backend or a secret manager and must not be exposed to the terminal
user.

## Image Validation Status

The image-oriented build, local container runtime, registry publishing, and
BoxLite image deployment paths have not been validated end to end. The image
files describe the intended packaging contract, but they must be treated as
provisional until a clean-environment validation covers image build, startup,
health checks, HTTP/WebSocket access, PTY behavior, non-root execution,
authentication, network policy, and shutdown.
