# web-terminal

A browser terminal (HTTP + WebSocket PTY on port `3001`) and a host-side
BoxLite toolkit for creating local or cloud sandboxes.

Agents: read [`AGENTS.md`](./AGENTS.md) before deploying. From the repo root:

```bash
bash scripts/bootstrap_host.sh
```

That installs Python 3.10+, Go 1.26+, and the `boxlite` / `websockets`
packages. Use the `PYTHON=` path it prints for later commands.

## Layout

| Path | Role |
|---|---|
| `go/` | Guest Go service and module (`/`, `/ws`, `/healthz`) |
| `go/terminal/test/` | Guest Go unit tests |
| `hangar/` | Hangar control plane and BoxLite provision/update |
| `hangar/test/` | Hangar unit tests |
| `scripts/` | Host bootstrap, Linux binary build, e2e check |
| `docs/` | Architecture notes |
| `image/` | Provisional OCI image path (not required for script deploy) |

Build artifacts land in `build/` and are gitignored.

## Hangar

Host-only console for create / stop / resume / delete. It binds
`127.0.0.1:3100` and must not be published. BoxLite SDK calls never run
inside a terminal session.

```bash
python3 -m hangar.control
```

Open `http://127.0.0.1:3100`. Choose **LOCAL** or **CLOUD**, then provision a
box. Cloud access links include `BOXLITE_BOX_AUTH_KEY`; treat the complete URL
as a secret. Local links are loopback URLs on an allocated host port.

Inventory is stored in `~/.web-terminal/control-state.json`. Override the bind
address with `WEB_TERMINAL_CONTROL_HOST` / `WEB_TERMINAL_CONTROL_PORT`.

## Deploy

Cloud REST on `app.boxlite.ai` is the default:

```bash
export BOXLITE_API_KEY=...
python3 -m hangar.deploy
```

Local embedded BoxLite uses `Boxlite.default()`, forwards guest port `3001`
to `127.0.0.1`, and does not need a cloud API key:

```bash
export BOXLITE_RUNTIME=embedded
python3 -m hangar.deploy
```

Embedded needs Python 3.10+ with `boxlite`, plus Apple Silicon or Linux with
KVM. Override the host bind port with `BOXLITE_HOST_PORT`.

The deploy path builds `build/web-terminal-linux-<arch>` if missing, installs
tmux, Codex, Claude Code, and Cursor CLI in the guest, and starts
`/usr/local/bin/web-terminal`. If a deploy is interrupted, set
`BOXLITE_BOX_ID` and rerun the same command.

Update an existing box with the same runtime:

```bash
BOXLITE_RUNTIME=embedded BOXLITE_BOX_ID=... python3 -m hangar.update
```

After cloud deploy, the script prints an authenticated preview URL. Do not
commit it, publish it, or put it in ordinary logs. New boxes stay running
until they are stopped.

Verify a running terminal:

```bash
python3 scripts/e2e_web_terminal.py --base-url "$URL"
```

## Network

Only port `3001` may be published. Port `22222` must stay internal: no public
ingress, preview URLs, container publish, or service manifests.

## Image path

Dockerfile, image build/test/push scripts, GHCR notes, and the agent.market
manifest live under [`image/`](./image/README.md). That workflow is
provisional and has not been validated end to end.

## License

MIT. See [`LICENSE`](./LICENSE). Security reporting is in [`SECURITY.md`](./SECURITY.md).
