# Security

This project is a browser terminal plus host-side BoxLite tooling. Treat
preview URLs, API keys, and box auth tokens as secrets.

## Do not commit

- `BOXLITE_API_KEY`
- `BOXLITE_BOX_AUTH_KEY` or any full cloud preview URL
- ChatGPT, Claude, Cursor, or other model credentials
- SSH private keys, registry tokens, or `.env` files
- `~/.web-terminal/control-state.json` (local Hangar inventory)

The guest image and provisioned box must not contain those values. Users
authenticate agent CLIs inside the box after it starts.

## Exposure rules

- Publish only guest port `3001`. Never expose `22222`.
- Hangar (`python3 -m webterminal.control`) binds `127.0.0.1:3100` by default. Do
  not publish the control plane.
- The Go service has no end-user auth. Put an authenticated proxy in front
  of HTTP and WebSocket on cloud deployments.

## Reporting

Open a private GitHub security advisory on this repository. Do not file a
public issue that includes keys, preview URLs, or reproduction data that
would grant access to a running box.
