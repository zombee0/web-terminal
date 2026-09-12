# Web Terminal and Marketplace Responsibilities

> **Unverified workflow:** The complete image build, registry, marketplace, and
> BoxLite runtime path has not been validated end to end. This document defines
> an intended boundary, not evidence of a working production integration.

The intended delivery flow is: builder creates an OCI image, publishes it to a
public registry, and the platform imports, validates, and releases an immutable
version. Use [`ghcr-publish.md`](./ghcr-publish.md), [`Dockerfile`](./Dockerfile),
and [`agent.market.yaml`](./agent.market.yaml) as the local package definition.

## Builder Responsibilities

The builder provides:

1. An OCI image containing Codex, tmux, Bash, and the Web Terminal service.
2. A service manifest describing the image, command, resources, network policy,
   authenticated preview access, and port `3001`.
3. Marketplace-facing capabilities, limitations, and support information.
4. Acceptance steps for browser access, terminal input, resize behavior,
   installed tools, non-root execution, and shutdown behavior.

The image must not contain Codex login state, registry tokens, API keys, SSH
private keys, customer files, or terminal history. Runtime users authorize
model access through a controlled session. Registry credentials must never be
passed into the runtime Box.

## Platform Responsibilities

The platform should:

- Pull the image without builder credentials and lock it to a digest
- Verify architecture, manifest validity, resource policy, and quotas
- Scan and test the immutable version in a clean Box
- Manage Box creation, startup, permissions, network policy, TTL, logs, stop,
  and deletion
- Authenticate both HTTP and WebSocket traffic at the preview proxy
- Run explicit readiness checks instead of assuming Docker health-check support
- Publish only versions backed by platform-controlled test evidence
- Start user sessions from the validated version without reusing builder Boxes
  or credentials

The platform must start the service exactly once. It must verify whether the
deployed BoxLite runtime honors the image entrypoint or requires an explicit
command instead of assuming either behavior.

Web Terminal is a long-running interactive service, not a task that exits and
writes a result file. The manifest therefore does not declare a task/event/result
file protocol.

## Development Scripts

`deploy_web_terminal.py` creates and provisions a development Box.
`update_web_terminal.py` updates an existing Box selected explicitly through
`BOXLITE_BOX_ID`. These scripts support rapid source iteration; they are not a
production marketplace deployment API and do not validate image execution.

The deployment script requests one CPU, 1024 MiB of memory, a 10 GiB disk, and
unrestricted outbound networking. It installs Codex, tmux, a UTF-8 locale, and
fonts with CJK coverage. The update script does not create a Box or reinstall
those dependencies.

## Port Policy

Only port `3001` may be published for this service. Port `22222` must remain
internal and must not appear in public ingress, preview endpoints, Docker port
publishing, or marketplace service definitions.

## Known Limitations

- Real image build and BoxLite image startup have not been validated.
- Non-root execution must be confirmed in the actual BoxLite runtime.
- The manifest egress allowlist may not cover interactive login, Git, or every
  Codex workflow; additions require explicit review.
- Browser CDN access and Box egress are separate network paths.
- Removing a network interface may also break service preview connectivity.
- A registry digest provides immutability, not permanent availability.
- Authentication, WebSocket proxying, resource enforcement, image scanning,
  and lifecycle cleanup require independent platform validation.
