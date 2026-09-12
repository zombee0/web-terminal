"""Create and provision a new Web Terminal Box on dev.boxlite.ai."""

import asyncio
import json
import os
import pathlib
import secrets
import sys
import urllib.request
from datetime import UTC, datetime

from boxlite import Boxlite, BoxliteRestOptions, BoxOptions, NetworkSpec


DEFAULT_REST_URL = "https://dev.boxlite.ai/api"
DEFAULT_IMAGE = "python"
PORT = 3001
APP = pathlib.Path(__file__).parent / "web-terminal-linux-amd64"

BOOTSTRAP_COMMAND = r"""
set -eu
if [ "$(id -u)" -ne 0 ]; then
  echo "Provisioning requires root inside the Box." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  bash \
  ca-certificates \
  curl \
  fontconfig \
  fonts-noto-cjk \
  fonts-noto-color-emoji \
  locales \
  passwd \
  procps \
  tmux

if ! id -u boxlite >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash boxlite
fi
install -d -o boxlite -g boxlite /home/boxlite/.local/bin /home/boxlite/.npm-global/bin

if [ -f /etc/locale.gen ]; then
  sed -i 's/^# *zh_CN.UTF-8 UTF-8/zh_CN.UTF-8 UTF-8/' /etc/locale.gen
  grep -q '^zh_CN.UTF-8 UTF-8' /etc/locale.gen || echo 'zh_CN.UTF-8 UTF-8' >> /etc/locale.gen
  locale-gen zh_CN.UTF-8
fi

curl -fsSL https://chatgpt.com/codex/install.sh -o /tmp/install-codex.sh
su -s /bin/bash boxlite -c \
  'HOME=/home/boxlite PATH=/home/boxlite/.local/bin:/usr/local/bin:/usr/bin:/bin CODEX_NON_INTERACTIVE=1 sh /tmp/install-codex.sh'
rm -f /tmp/install-codex.sh

command -v tmux >/dev/null
su -s /bin/bash boxlite -c \
  'HOME=/home/boxlite PATH=/home/boxlite/.local/bin:/usr/local/bin:/usr/bin:/bin codex --version'
rm -rf /var/lib/apt/lists/*
"""

INSTALL_APP_COMMAND = r"""
set -eu
if [ "$(id -u)" -ne 0 ]; then
  echo "Installing Web Terminal requires root inside the Box." >&2
  exit 1
fi
pkill -f '^/usr/local/bin/web-terminal$' || true
install -m 0755 -o root -g root /tmp/web-terminal /usr/local/bin/web-terminal
su -s /bin/bash boxlite -c \
  'cd /home/boxlite && HOME=/home/boxlite LANG=zh_CN.UTF-8 LC_ALL=zh_CN.UTF-8 PATH=/home/boxlite/.local/bin:/home/boxlite/.npm-global/bin:/usr/local/bin:/usr/bin:/bin nohup /usr/local/bin/web-terminal >/tmp/web-terminal.log 2>&1 </dev/null &'
"""


def configure_environment():
    if not os.environ.get("BOXLITE_REST_URL"):
        os.environ["BOXLITE_REST_URL"] = DEFAULT_REST_URL
    if not os.environ.get("BOXLITE_API_KEY"):
        raise RuntimeError("BOXLITE_API_KEY is not set")
    if not APP.is_file():
        raise RuntimeError(f"Web Terminal binary not found: {APP}")


def api(method, path):
    base = os.environ["BOXLITE_REST_URL"].rstrip("/").removesuffix("/api")
    request = urllib.request.Request(base + path, method=method)
    request.add_header("Authorization", "Bearer " + os.environ["BOXLITE_API_KEY"])
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode() or "{}")


async def drain(stream, destination, captured):
    async for chunk in stream:
        text = str(chunk)
        captured.append(text)
        destination.write(text)
        destination.flush()


async def run_checked(box, command, *, args=None, user=None, timeout_secs=None):
    execution = await box.exec(
        command,
        args=args,
        user=user,
        timeout_secs=timeout_secs,
    )
    stdout = []
    stderr = []
    result, _, _ = await asyncio.gather(
        execution.wait(),
        drain(execution.stdout(), sys.stdout, stdout),
        drain(execution.stderr(), sys.stderr, stderr),
    )
    if result.exit_code != 0:
        detail = "".join(stderr)[-4000:].strip()
        raise RuntimeError(
            f"Command failed with exit code {result.exit_code}: {command}\n{detail}"
        )


def new_box_name():
    configured = os.environ.get("BOXLITE_BOX_NAME")
    if configured:
        return configured
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"web-terminal-{timestamp}-{secrets.token_hex(3)}"


async def main():
    configure_environment()
    runtime = Boxlite.rest(BoxliteRestOptions.from_env())
    box_name = new_box_name()
    box = await runtime.create(
        BoxOptions(
            image=os.environ.get("BOXLITE_IMAGE", DEFAULT_IMAGE),
            cpus=1,
            memory_mib=1024,
            disk_size_gb=50,
            working_dir="/tmp",
            network=NetworkSpec(mode="enabled", allow_net=[]),
            entrypoint=["/bin/sleep"],
            cmd=["infinity"],
            user="root",
            auto_remove=False,
            detach=True,
        ),
        name=box_name,
    )
    print(f"BOX_ID={box.id}", flush=True)

    await box.start()
    await run_checked(
        box,
        "sh",
        args=["-lc", BOOTSTRAP_COMMAND],
        user="root",
        timeout_secs=900,
    )
    await box.copy_in(str(APP), "/tmp/web-terminal")
    await run_checked(
        box,
        "sh",
        args=["-lc", INSTALL_APP_COMMAND],
        user="root",
        timeout_secs=60,
    )
    await run_checked(
        box,
        "sh",
        args=[
            "-lc",
            "for attempt in $(seq 1 60); do "
            "curl -fsS http://127.0.0.1:3001/healthz | grep -qx ok && exit 0; "
            "sleep 1; done; cat /tmp/web-terminal.log >&2; exit 1",
        ],
        user="root",
        timeout_secs=90,
    )

    url = api("GET", f"/api/box/{box.id}/ports/{PORT}/preview-url")["url"]
    print(f"BOX_NAME={box_name}")
    print(f"URL={url}")
    print(f"To update this Box: BOXLITE_BOX_ID={box.id} python3 update_web_terminal.py")


if __name__ == "__main__":
    asyncio.run(main())
