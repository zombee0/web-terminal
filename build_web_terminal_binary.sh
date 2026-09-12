#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
output_dir="${project_dir}/build"
output="${output_dir}/web-terminal-linux-amd64"

command -v go >/dev/null
mkdir -p "$output_dir"

(
  cd "$project_dir"
  CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -trimpath -ldflags="-s -w" -o "$output" .
)

echo "Built ${output}"
