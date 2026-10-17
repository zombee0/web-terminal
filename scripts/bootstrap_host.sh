#!/usr/bin/env bash
# Install host-side tools needed to deploy Web Terminal. Safe to rerun.
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/go/bin:${HOME}/go/bin:${PATH}"

need_cmd() {
  command -v "$1" >/dev/null 2>&1
}

os_name="$(uname -s)"
arch="$(uname -m)"

if [[ "$os_name" == Darwin && "$arch" != arm64 && "$arch" != aarch64 ]]; then
  echo "Embedded BoxLite on macOS requires Apple Silicon." >&2
  exit 1
fi

pick_python() {
  local candidate
  for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if need_cmd "$candidate" && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

if ! python_bin="$(pick_python)"; then
  if [[ "$os_name" == Darwin ]] && need_cmd brew; then
    echo "Installing Python 3.12 via Homebrew."
    brew install python@3.12
    export PATH="/opt/homebrew/opt/python@3.12/bin:/opt/homebrew/bin:${PATH}"
    python_bin="$(pick_python)"
  fi
fi

if [[ -z "${python_bin:-}" ]]; then
  echo "Python 3.10+ is required. Install it and rerun $0." >&2
  exit 1
fi

if ! need_cmd go; then
  if [[ "$os_name" == Darwin ]] && need_cmd brew; then
    echo "Installing Go via Homebrew."
    brew install go
    export PATH="/opt/homebrew/bin:${PATH}"
  elif [[ "$os_name" == Linux ]]; then
    echo "Go is required. Install the Go toolchain (1.26+) and rerun $0." >&2
    exit 1
  else
    echo "Go is required but no installer is available on this platform." >&2
    exit 1
  fi
fi

if ! need_cmd go; then
  echo "Go was not found after install." >&2
  exit 1
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
venv_dir="${WEB_TERMINAL_VENV:-${repo_root}/.venv}"
if [[ ! -x "${venv_dir}/bin/python" ]]; then
  "$python_bin" -m venv "$venv_dir"
fi
python_bin="${venv_dir}/bin/python"
echo "Installing isolated host dependencies."
"$python_bin" -m pip install --disable-pip-version-check -r "${repo_root}/requirements.txt"

"$python_bin" -c 'import boxlite; print("boxlite", boxlite.__version__)'
go version

echo "PYTHON=${python_bin}"
echo "GO=$(command -v go)"
echo "HOST_BOOTSTRAP_OK"
