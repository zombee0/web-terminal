# web-terminal

A browser-based terminal service and BoxLite deployment toolkit.

## Build the Deployment Binary

```bash
bash build_web_terminal_binary.sh
```

The Linux AMD64 binary is written to `build/web-terminal-linux-amd64`. The
`build/` directory contains local build artifacts and is ignored by Git. The
deployment and update scripts invoke the build script automatically when the
binary is missing.

If deployment is interrupted, set `BOXLITE_BOX_ID` and run the deployment
script again to continue provisioning the existing Box instead of creating a
new one.

After deployment succeeds, the script prints an authenticated preview URL with
`BOXLITE_BOX_AUTH_KEY` in its query string. Treat the complete URL as a secret:
do not commit it, publish it, or include it in ordinary logs.

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
