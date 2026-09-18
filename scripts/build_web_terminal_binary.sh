#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
output_dir="${project_dir}/build"
goos="${GOOS:-linux}"
goarch="${GOARCH:-amd64}"
output="${output_dir}/web-terminal-${goos}-${goarch}"

command -v go >/dev/null
mkdir -p "$output_dir"

(
  cd "$project_dir"
  CGO_ENABLED=0 GOOS="$goos" GOARCH="$goarch" \
    go build -trimpath -ldflags="-s -w" -o "$output" ./cmd/web-terminal
)

echo "Built ${output}"
