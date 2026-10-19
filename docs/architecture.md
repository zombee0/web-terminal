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

- Tap **Browse output** for a full-height mobile reading view. **Terminal
  output** shows retained history; swipe or use **Previous** / **Next** to
  page through it. Native selection and paging in this source send no input.
- **Refresh** loads new output while preserving an earlier reading position;
  **Latest** jumps to the end. Terminal output stays fixed while reading.
  xterm retains 10,000 normal-buffer lines, but only a fullscreen program's
  current screen. It cannot reconstruct overwritten application text.
- While Codex or Claude Code is running, choose **Codex history** or **Claude
  history** in the reader. This opens the tool's own transcript using its
  default Ctrl+T or Ctrl+O binding. Earlier messages come from the application.
- In an app history source, **Previous** / **Next** first scroll the visible
  text, then send PageUp / PageDown at its boundary. **Latest** sends Ctrl+End
  for Codex and Claude fullscreen views, or End for Codex's classic pager.
  Codex's binding is chosen from the buffer mode before opening its history.
  Inline transcripts use retained terminal scrollback instead.
  Up/Down prompt-history keys are never
  sent by these controls.
- App pages follow returned output, pausing replacement during native text
  selection. **Back to terminal**, Escape, or switching back to **Terminal
  output** sends the selected app's transcript toggle once to leave it.
  Closing the reader clears copied text. Disconnected controls send nothing.
  These shortcuts require the matching running tool and its default bindings.
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
The output reader is checked with actual Chromium touch and desktop wheel
scrolling. Mobile WebKit cannot synthesize either native touch scrolling or
wheel input in Playwright; its tests verify uncancelled touch events and DOM
scrollability. Native iOS inertia and selection callouts remain device checks.
Paging checks include older application pages arriving as text/binary WebSocket
frames, source changes, selection preservation and disconnects. These fixtures
verify the browser protocol.

Run `make test TEST_TARGETS=test-terminal-apps` for native CLI acceptance through
a real PTY and the shipped page. It requires installed `codex` and `claude` CLIs
and uses disposable conversations/configuration with loopback service fixtures.
No model calls or user conversations are used. Codex 0.163.0 and Claude Code
2.1.303 pass in fullscreen and inline modes: all 120 history markers are visible
while paging, Latest returns to the last marker, and closing preserves the draft
and insertion point. These checks do not establish compatibility with the user's
guest versions, remapped shortcuts or terminal multiplexers.

For a host-recorded test run, use
`make test TEST_TARGETS="test-go test-terminal-ui"`. The default `make test`
suite is unchanged. The [mobile design and acceptance issue](https://github.com/zombee0/web-terminal/issues/2)
records the approach, alternatives and outstanding phone checks.

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
