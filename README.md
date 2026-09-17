# web-terminal

A browser-based terminal service and BoxLite deployment toolkit.

Agents: read [`AGENTS.md`](./AGENTS.md) before deploying. Run
`bash bootstrap_host.sh` first so Python, Go, and the BoxLite SDK are
installed automatically.

## Build the Deployment Binary

```bash
bash build_web_terminal_binary.sh
```

The Linux AMD64 binary is written to `build/web-terminal-linux-amd64`. The
`build/` directory contains local build artifacts and is ignored by Git. The
deployment and update scripts invoke the build script automatically when the
binary is missing. Local embedded deployment on Apple Silicon builds
`build/web-terminal-linux-arm64` instead.

If deployment is interrupted, set `BOXLITE_BOX_ID` and run the deployment
script again to continue provisioning the existing Box instead of creating a
new one.

After a cloud deployment succeeds, the script prints an authenticated preview
URL with `BOXLITE_BOX_AUTH_KEY` in its query string. Treat the complete URL as
a secret: do not commit it, publish it, or include it in ordinary logs.

New Boxes are created with automatic stopping disabled and remain running until
they are stopped explicitly.

## Deploy

Cloud REST on `dev.boxlite.ai` is the default:

```bash
export BOXLITE_API_KEY=...
python3 deploy_web_terminal.py
```

Local embedded BoxLite uses the in-process runtime (`Boxlite.default()`),
forwards guest port `3001` to `127.0.0.1`, and does not require a cloud API
key:

```bash
export BOXLITE_RUNTIME=embedded
python3 deploy_web_terminal.py
```

Embedded requirements are Python 3.10+ with `pip install boxlite`, plus Apple
Silicon or Linux with KVM. Override the host bind port with
`BOXLITE_HOST_PORT`. The local URL is loopback-only; there is no authenticated
preview proxy in this mode.

Update an existing Box with the same runtime:

```bash
BOXLITE_RUNTIME=embedded BOXLITE_BOX_ID=... python3 update_web_terminal.py
```

## Network Exposure

The custom Web Terminal listens on port `3001`. It is the only terminal port
that may be published by this project. Port `22222` must remain internal and
must not be exposed through public ingress, preview URLs, container port
publishing, or service manifests.

## Container Image Delivery

The Dockerfile, image build/test/push scripts, GHCR publishing guide, and
agent.market manifest are grouped under [`image/`](./image/README.md).

> **Validation status:** The container image build, runtime, registry push, and
> BoxLite image deployment workflows have not been validated end to end. Treat
> the image documentation and scripts as provisional until that validation is
> completed.
