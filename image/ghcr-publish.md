# Build, Test, and Publish Web Terminal to Public GHCR

> **Unverified workflow:** The real image build, container smoke test, registry
> push, anonymous pull, and BoxLite image deployment paths have not been
> validated end to end. The commands below describe the intended workflow and
> must be verified in a clean environment before release.

Run these steps on a developer workstation or in CI, not inside a runtime Box.
An existing local image can start at step 2.

## 1. Build the Image

Prerequisites are Docker Engine with buildx, Bash, and curl. The target platform
is `linux/amd64`; other development architectures need working cross-platform
build and runtime emulation. Go does not need to be installed on the host.

```bash
export LOCAL_IMAGE=web-terminal:0.1.0
export IMAGE_VERSION=0.1.0
export BASE_IMAGE=python:slim
# Optional source repository metadata for the GHCR package.
export SOURCE_REPOSITORY=https://github.com/your-org/your-repo
bash image/build_web_terminal_image.sh
```

The multi-stage build compiles the source and installs Bash, Codex, Claude
Code, Cursor CLI, tmux, a UTF-8 locale, and fonts in the final image. The image
runs as `boxlite` and its entrypoint starts the foreground service on port
`3001`.

The separate deployment scripts use `build/web-terminal-linux-amd64`. Running
`scripts/build_web_terminal_binary.sh` creates that file without modifying the
image build. The `build/` directory is ignored by Git.

### Reproducibility Boundary

Rolling base-image tags and the Codex installer do not provide byte-for-byte
reproducibility. A release record should retain:

- The resolved `BASE_IMAGE` and `GO_IMAGE` digests
- The source revision, target architecture, and build log
- `/home/boxlite/codex-build-version.txt`
- `/usr/share/web-terminal/packages.txt`
- The final registry manifest digest

`CODEX_EXPECTED_VERSION` can assert the complete expected `codex --version`
output, but it does not pin the installer artifact. Strict reproducibility also
requires verified, immutable Codex and operating-system package sources.

## 2. Test the Local Image

```bash
LOCAL_IMAGE=web-terminal:0.1.0 bash image/test_web_terminal_image.sh
```

The helper is intended to check the target architecture, non-root identity,
Codex, Claude Code, Cursor CLI, tmux, UTF-8, `/healthz`, and the home page. It binds a randomly selected
host port on `127.0.0.1`, stops only its own container, does not call model APIs,
and does not validate BoxLite integration.

Manual startup:

```bash
docker run --rm -p 127.0.0.1:3001:3001 "$LOCAL_IMAGE"
```

Health check from another terminal:

```bash
curl -fsS http://127.0.0.1:3001/healthz
```

Open `http://127.0.0.1:3001` and verify terminal input, multilingual text,
resize handling, `id`, `codex --version`, `claude --version`, `agent --version`, and `tmux -V`. Each connection starts
a new shell. Use `tmux new -A -s work` when persistent shell state is needed.
The browser must be able to load xterm.js from jsDelivr.

Override the entrypoint for image diagnostics:

```bash
docker run --rm --entrypoint /bin/bash "$LOCAL_IMAGE" -ec \
  'id; command -v codex; command -v claude; command -v agent; tmux -V'
```

The image contract and external service manifest declare port `3001`.

## 3. Prepare GHCR Permission

1. Create a short-lived GitHub classic PAT.
2. Grant only `write:packages` unless the repository relationship requires more.
3. Confirm the token owner can write the target namespace and package.
4. Authorize organization SSO when required.
5. For GitHub Actions, prefer `GITHUB_TOKEN` with `contents: read` and `packages: write`.

See the [GitHub Container registry documentation](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

## 4. Log In and Push

Replace the owner and username placeholders with real values. The owner must be
the lowercase GitHub user or organization name.

```bash
export GHCR_OWNER=your-org
export GHCR_USERNAME=your-github-user
export GHCR_IMAGE="ghcr.io/${GHCR_OWNER}/web-terminal"
export IMAGE_VERSION=0.1.0
export LOCAL_IMAGE=web-terminal:0.1.0

read -rsp "GHCR PAT: " GHCR_PAT
echo
printf '%s' "$GHCR_PAT" | docker login ghcr.io \
  --username "$GHCR_USERNAME" --password-stdin
unset GHCR_PAT

bash image/push_web_terminal_image.sh
```

The push helper is intended to rerun the local smoke test, tag the image, push
it, and display registry manifest information. It does not rebuild the image or
change package visibility. Never put a PAT in source files, image build
arguments, image environment variables, manifests, or marketplace forms.

Before making an image public, scan all layers for credentials, customer data,
terminal history, private files, vulnerabilities, and secrets.

## 5. Make the Package Public

The first command-line push normally creates a private package. A package
administrator can change visibility under:

```text
GitHub account or organization -> Packages -> web-terminal
-> Package settings -> Change visibility -> Public
```

A public source repository does not automatically make its package public.
Deleted files can remain recoverable from earlier image layers.

## 6. Verify Anonymous Pull and Record the Digest

Use an empty temporary Docker configuration so existing credentials cannot hide
an access-control problem:

```bash
anonymous_config="$(mktemp -d)"
docker --config "$anonymous_config" pull \
  --platform linux/amd64 "${GHCR_IMAGE}:${IMAGE_VERSION}"
docker buildx imagetools inspect "${GHCR_IMAGE}:${IMAGE_VERSION}"
```

Record the top-level `sha256` manifest digest, then verify a digest pull:

```bash
export IMAGE_DIGEST=sha256:REPLACE_WITH_REAL_64_HEX_DIGEST
docker --config "$anonymous_config" pull \
  --platform linux/amd64 "${GHCR_IMAGE}@${IMAGE_DIGEST}"
```

If anonymous pull fails, inspect package visibility, the image reference,
network policy, and retention. Do not provide a registry PAT to the runtime
platform as a workaround.

## 7. Platform Handoff

Replace the digest placeholder in [`agent.market.yaml`](./agent.market.yaml).
The handoff should include:

- The public OCI image reference and `linux/amd64` platform
- The service manifest for port `3001`, including WebSocket and readiness rules
- Product capabilities, limitations, and support information
- Clean-environment acceptance steps

The platform must independently pull by digest, scan the image, launch a clean
Box, verify HTTP and WebSocket behavior, enforce authenticated preview access,
and test lifecycle cleanup. A Docker entrypoint does not prove that a BoxLite
SDK starts it automatically, and a Docker smoke test does not validate the
BoxLite image path.
