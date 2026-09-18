# Agent instructions: Web Terminal deploy

Read this file before deploying. Do the work yourself: install missing host
SDKs, choose local or cloud, deploy, and verify. Do not ask the user to run
install or deploy commands that you can run.

## What this repo is

A Go Web Terminal (HTTP + WebSocket PTY on port `3001`) plus BoxLite
provisioning scripts. The service runs **inside** a BoxLite sandbox. Host
scripts create that sandbox, copy in the Linux binary, and start the process.

Two backends:

| Backend | When to use | Runtime |
|---|---|---|
| `cloud` (default) | User says cloud / remote / `app.boxlite.ai` | `Boxlite.rest(...)` |
| `embedded` | User says local / embedded / laptop sandbox | `Boxlite.default()` |

Aliases: `BOXLITE_RUNTIME=rest|remote` → cloud; `local` → embedded.

## Non-negotiables

- Publish **only** port `3001`. Never expose `22222`.
- Never print, commit, or log `BOXLITE_API_KEY`, preview URLs, or
  `BOXLITE_BOX_AUTH_KEY`. Treat the full cloud URL as a secret.
- Do not put API keys, ChatGPT credentials, or SSH keys in images or guest
  files.
- Guest Web Terminal must keep binding `0.0.0.0:3001` (`:3001` in Go). Local
  port-forwarding fails if the process binds loopback only.
- BoxLite SDK calls stay on the host. Do not drive BoxLite from inside the
  terminal session.

## 1. Bootstrap the host (always, automatically)

From the repo root, run:

```bash
bash scripts/bootstrap_host.sh
```

The script installs whatever is missing:

- Python **3.10+** (BoxLite has no wheels for 3.9)
- Go **1.26+** (needed to cross-compile the guest binary)
- `pip` packages `boxlite` and `websockets`

Use the `PYTHON=` path it prints for every later `python` command. If
bootstrap cannot install Go on Linux, install a 1.26+ toolchain and rerun.

Embedded extra requirements (not installed by the script):

- macOS: Apple Silicon (Hypervisor.framework)
- Linux: KVM (`/dev/kvm` accessible)

`scripts/bootstrap_host.sh` is idempotent. Run it at the start of every deploy
session.

## 2. Choose backend and env

### Cloud (default)

Required:

- `BOXLITE_API_KEY` — load from the user environment or `~/.zshrc` /
  `~/.bashrc` if already configured. **Never echo the value.**

```bash
eval "$(grep -E '^[[:space:]]*(export[[:space:]]+)?BOXLITE_API_KEY=' ~/.zshrc ~/.bashrc 2>/dev/null | head -n 1)"
export BOXLITE_API_KEY
export BOXLITE_RUNTIME=cloud
# optional; script default is https://app.boxlite.ai/api
export BOXLITE_REST_URL="${BOXLITE_REST_URL:-https://app.boxlite.ai/api}"
```

If the key is missing, stop and ask the user for it. Do not invent one.

Optional: `BOXLITE_IMAGE` (default `python`), `BOXLITE_BOX_NAME`,
`BOXLITE_BOX_ID` (resume), `BOXLITE_REST_PREFIX`, `WEB_TERMINAL_GOARCH`
(cloud default `amd64`).

### Local embedded

No API key. Guest arch follows the host (`arm64` on Apple Silicon).

```bash
export BOXLITE_RUNTIME=embedded
# optional; default 3001, bound to 127.0.0.1
export BOXLITE_HOST_PORT="${BOXLITE_HOST_PORT:-3001}"
```

Optional: `BOXLITE_IMAGE`, `BOXLITE_BOX_NAME`, `BOXLITE_BOX_ID` (resume),
`BOXLITE_HOME_DIR`, `WEB_TERMINAL_GOARCH`.

## 3. Deploy

The deploy script builds `build/web-terminal-linux-<arch>` if missing, creates
or resumes a Box, apt-installs tmux/fonts/locales, installs Codex, Claude
Code, and Cursor CLI inside the guest, copies the binary, and starts
`/usr/local/bin/web-terminal`.

First-time provision often takes **10–15 minutes** (Debian packages + CJK
fonts + agent CLIs). Wait for it. Bootstrap timeout is 900 seconds.

```bash
# PYTHON is the interpreter printed by scripts/bootstrap_host.sh
"$PYTHON" -m webterminal.deploy
```

Success prints `RUNTIME=`, `BOX_ID=`, `BOX_NAME=`, and `URL=`. Keep `BOX_ID`
for updates.

Interrupted deploy: set `BOXLITE_BOX_ID` to the printed id and rerun the same
command. Do not create a second Box unless the user wants a new one.

Update binary only (no apt/CLI reinstall):

```bash
BOXLITE_BOX_ID=<id> "$PYTHON" -m webterminal.update
```

Use the same `BOXLITE_RUNTIME` as the original deploy.

Host control console (lifecycle UI, still on the host):

```bash
"$PYTHON" -m webterminal.control
```

Default URL is `http://127.0.0.1:3100`. Do not expose this process. Cloud
access links shown in the UI contain `BOXLITE_BOX_AUTH_KEY`; do not paste them
into git or unredacted logs.

## 4. Verify

Cloud: `URL` is an authenticated preview URL. Do not paste it into git or
unredacted logs. Check `/healthz` through that URL.

Local: `URL` is `http://127.0.0.1:<BOXLITE_HOST_PORT>`.

```bash
"$PYTHON" scripts/e2e_web_terminal.py --base-url "$URL"
```

Pass means `/healthz` → `ok`, home page serves Box Terminal, WebSocket PTY
runs as `uid=1000(boxlite)`, `tmux` reports a version, and `codex`, `claude`,
and `agent` (Cursor CLI) are on PATH.

If `websockets` is missing, rerun `bash scripts/bootstrap_host.sh`.

## 5. Layout

| Path | Role |
|---|---|
| `scripts/bootstrap_host.sh` | Host SDK/toolchain install |
| `webterminal/control.py` | Hangar control plane (create/stop/resume/delete) |
| `webterminal/control.html` | Geek-style control console UI |
| `webterminal/deploy.py` | Create/provision Box |
| `webterminal/update.py` | Replace guest binary |
| `webterminal/provision.py` | Guest bootstrap + app install/start |
| `webterminal/boxlite.py` | Cloud vs embedded helper |
| `scripts/build_web_terminal_binary.sh` | Cross-compile Linux guest binary |
| `scripts/e2e_web_terminal.py` | HTTP + WebSocket PTY check |
| `cmd/web-terminal/` | Guest service (`:3001`, `/`, `/ws`, `/healthz`) |
| `docs/` | Architecture and design notes |
| `tests/` | Host-side unit tests |
| `image/` | OCI image path (provisional; not required for script deploy) |

Guest binary names:

- Cloud: `build/web-terminal-linux-amd64`
- Embedded Apple Silicon: `build/web-terminal-linux-arm64`

`build/` is gitignored.

## 6. Troubleshooting

- `No module named 'boxlite'` or Python 3.9: rerun bootstrap; use its
  `PYTHON=`.
- `BOXLITE_API_KEY is not set`: cloud only; load the key, do not switch to
  embedded unless the user asked for local.
- Guest `/healthz` ok but host cannot connect (embedded): confirm
  `BOXLITE_HOST_PORT` is free and the process still binds `:3001`.
- `https://app.boxlite.ai` timeout from inside a cloud Box does not fail
  deploy; guest egress policy may block that host.
- Do not debug by publishing extra ports.
