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

### Phone interaction

- Inside tmux, tap **Browse output** to put the attached pane into tmux's own
  copy mode, in place; the same toolbar button then reads **Back to terminal**.
  Swipe down for older rows and up for newer ones; desktop wheel, arrow and
  Page keys do the same. This includes output above a full-screen program,
  because tmux keeps the shell's earlier rows as pane history. A program on
  the alternate screen redraws in place, so tmux retains none of its own
  output. Codex does this by default inside tmux; provisioning therefore sets
  `[tui] alternate_screen = "never"` in the guest's `~/.codex/config.toml`
  (on every install and Resume, unless the user already chose a value), and
  Codex output then accumulates in tmux history like ordinary shell output.
  At tmux's oldest retained row, browsing simply stays at the top
  (`history-limit` sets how far back that is). **Back to terminal**, Escape or
  `q` cancel copy mode; the program's screen, the shell draft and its caret are
  unchanged. Copy mode entered with the user's own tmux prefix also switches
  the toolbar. Outside tmux, swipes scroll xterm's own scrollback.
- A transparent `tmux` wrapper, first on the shell's PATH, runs the real tmux
  and pairs that client with this WebSocket over a private Unix socket. The
  service sends `browse-ready` first; browse requests are text frames and
  terminal input is binary, so JSON typed by the user stays shell input. The
  service only issues `copy-mode` and `send-keys -X` commands to the paired
  client's pane, never keystrokes into the application, and cancels copy mode
  it started when the browser disconnects. The pairing address is kept out of
  the tmux server environment. Nothing is recorded to disk. The wrapper lives
  in `/var/lib/web-terminal/bin` (root-owned) for the protected service, or
  `~/.local/state/web-terminal/bin` for standalone use.
- Tap the terminal to focus the software keyboard. Mobile pages do not focus
  the terminal automatically when the WebSocket connects.
- Hold for 450 ms to select a word and open Copy / Paste / Select all / Done.
  Drag either handle to change the selection, including across wrapped lines
  and CJK text. Drag near a viewport edge to extend into scrollback.
- Swipe vertically to scroll. Movement cancels the long press. In mouse mode
  or the alternate screen, swipes use xterm's wheel handling so applications
  can receive their expected mouse reports or cursor keys.
- Copy and Paste first use the browser clipboard API on the button press.
  When unavailable or denied, a native textarea allows the phone's own
  long-press Copy/Paste menu. Fallback paste sends only after confirmation;
  cancel sends nothing. Clipboard text is cleared from the field on close.
- Pasting uses xterm's bracketed-paste support and does not append Enter.
  Newlines already present in the clipboard retain normal terminal behavior.
- The terminal follows the visual viewport when the keyboard or orientation
  changes. Resizing clears a touch selection because reflow changes its cells.

The HTML, CSS and JavaScript live in `go/terminal/web/` and are embedded into
the binary as a single page, including for `--print-page`. No additional
asset routes are needed through the authenticated proxy.

Run `make test-terminal-ui` for Chromium touch input, WebKit DOM touch events,
and desktop regression checks. This uses the existing Python Playwright
environment, installed Chrome and a Playwright WebKit installation
(`.venv/bin/python -m playwright install webkit`). The first run downloads the
pinned CDN assets into `build/terminal-ui/`; screenshots are written there too.
WebSocket and clipboard fixtures are isolated from real Boxes and clipboards.
Real iPhone/Android checks are still needed for system clipboard permissions,
native selection callouts and the software keyboard itself.
Browse mode is checked against a mocked tmux pairing with Chromium touch,
WebKit DOM touch events and desktop wheel input, at several phone viewports.

Run `make test TEST_TARGETS="test-go test-terminal-native"` for real tmux
acceptance. `test-go` drives the production WebSocket/PTY loop against an
isolated tmux server: pairing, copy mode over an alternate-screen program,
the top boundary and cleanup on disconnect. `test-terminal-native` adds
Chromium and WebKit on a phone viewport: bidirectional swipes, exact colored
rows, long-press copy, vi and emacs mode keys, a custom prefix, mouse on/off,
client isolation, a full-screen program over earlier shell output, and a
preserved shell draft and caret. No existing tmux sessions are read or changed.

For a host-recorded test run, use
`make test TEST_TARGETS="test-go test-terminal-ui"`. The default `make test`
suite is unchanged. The [mobile design and acceptance issue](https://github.com/zombee0/web-terminal/issues/2)
tracks this work.

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

The host control console (`python3 -m hangar.control` + `hangar/control.html`) is the
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
