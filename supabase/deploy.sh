#!/usr/bin/env bash
set -euo pipefail
task_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$task_root"
bash scripts/bootstrap_host.sh
task_python="${WEB_TERMINAL_VENV:-$task_root/.venv}/bin/python"
"$task_python" -m pip install --disable-pip-version-check -r supabase/requirements.txt
"$task_python" -m supabase.migrate
"$task_python" -m supabase.preflight
"$task_python" -m supabase.relay
"$task_python" -m supabase.deploy
"$task_python" -m supabase.watchdog
