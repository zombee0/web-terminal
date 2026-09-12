# Web Terminal Image Delivery

This directory groups the complete container-image delivery surface:

- `Dockerfile` and `Dockerfile.dockerignore`: image definition and build-context filters
- `build_web_terminal_image.sh`: local Linux AMD64 image build
- `test_web_terminal_image.sh`: local image smoke-test helper
- `push_web_terminal_image.sh`: test, tag, and GHCR push helper
- `agent.market.yaml`: agent.market service manifest
- `ghcr-publish.md`: build and publishing procedure
- `agent-market-integration.md`: builder/platform responsibility boundary

Run the documented commands from the repository root unless a command states
otherwise.

> **Unverified workflow:** The real image build, container runtime, registry
> push, and BoxLite image deployment paths have not been validated end to end.
> The scripts and commands in this directory are provisional and must not be
> represented as production-ready until clean-environment validation succeeds.

The image and manifest expose only port `3001`. Port `22222` must not be
published or added to any external ingress configuration.
