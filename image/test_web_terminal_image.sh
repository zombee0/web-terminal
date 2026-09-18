#!/usr/bin/env bash
set -euo pipefail
: "${LOCAL_IMAGE:=web-terminal:0.1.0}"
docker image inspect "$LOCAL_IMAGE" >/dev/null
test "$(docker image inspect "$LOCAL_IMAGE" --format '{{.Os}}/{{.Architecture}}')" = linux/amd64
# Override ENTRYPOINT to execute checks rather than passing arguments to Go.
docker run --rm --entrypoint /bin/bash "$LOCAL_IMAGE" -ec \
  'test "$(id -u)" != 0; command -v codex; command -v claude; command -v agent; codex --version; claude --version; agent --version; tmux -V; test -x /usr/local/bin/web-terminal; test "$(locale charmap)" = UTF-8'
test_container="$(docker run -d --rm -p 127.0.0.1::3001 "$LOCAL_IMAGE")"
cleanup() { docker stop "$test_container" >/dev/null || true; }
trap cleanup EXIT
test_address="$(docker port "$test_container" 3001/tcp)"
for attempt in {1..60}; do
  if curl --max-time 2 -fsS "http://${test_address}/healthz" >/dev/null; then
    curl --max-time 5 -fsS "http://${test_address}/" | grep -q 'Box Terminal'
    echo "Docker smoke test passed (not a BoxLite verification)."
    exit 0
  fi
  sleep 1
done
docker logs "$test_container" >&2
echo "Service did not become ready within the test budget." >&2
exit 1
