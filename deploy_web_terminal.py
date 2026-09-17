"""Create and provision a Web Terminal Box on cloud REST or local embedded BoxLite."""

import asyncio
import os
import secrets
from datetime import UTC, datetime

from web_terminal_boxlite import (
    app_path,
    box_display_name,
    create_box_options,
    create_runtime,
    ensure_started,
    prepare_runtime_env,
    run_checked,
    runtime_mode,
    service_url,
    wait_for_guest_health,
    wait_for_host_health,
)


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
  tmux \
  util-linux

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
install -m 0755 -o root -g root /home/boxlite/.web-terminal-upload /usr/local/bin/web-terminal
rm -f /home/boxlite/.web-terminal-upload
su -s /bin/bash boxlite -c \
  'cd /home/boxlite && HOME=/home/boxlite LANG=zh_CN.UTF-8 LC_ALL=zh_CN.UTF-8 PATH=/home/boxlite/.local/bin:/home/boxlite/.npm-global/bin:/usr/local/bin:/usr/bin:/bin setsid -f /usr/local/bin/web-terminal >/tmp/web-terminal.log 2>&1 </dev/null'
"""


def new_box_name():
    configured = os.environ.get("BOXLITE_BOX_NAME")
    if configured:
        return configured
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"web-terminal-{timestamp}-{secrets.token_hex(3)}"


async def main():
    mode = runtime_mode()
    prepare_runtime_env(mode)
    runtime = create_runtime(mode)
    box_id = os.environ.get("BOXLITE_BOX_ID")
    if box_id:
        box = await runtime.get(box_id)
        if box is None:
            raise RuntimeError(f"Box not found: {box_id}")
        info = await runtime.get_info(box_id)
        box_name = box_display_name(info, box_id)
        print(f"Resuming deployment in existing Box: {box_id}", flush=True)
    else:
        box_name = new_box_name()
        box = await runtime.create(create_box_options(mode), name=box_name)
    print(f"RUNTIME={mode}", flush=True)
    print(f"BOX_ID={box.id}", flush=True)

    await ensure_started(runtime, box, mode)
    await run_checked(
        box,
        "sh",
        args=["-lc", BOOTSTRAP_COMMAND],
        user="root",
        timeout_secs=900,
    )
    await box.copy_in(str(app_path(mode)), "/home/boxlite/.web-terminal-upload")
    await run_checked(
        box,
        "sh",
        args=["-lc", INSTALL_APP_COMMAND],
        user="root",
        timeout_secs=60,
    )
    await wait_for_guest_health(box, attempts=60, timeout_secs=90)
    url = service_url(mode, box, authenticated=True)
    if mode == "embedded":
        await wait_for_host_health(url.rstrip("/") + "/healthz")
    print(f"BOX_NAME={box_name}")
    print(f"URL={url}")
    print(f"To update this Box: BOXLITE_RUNTIME={mode} BOXLITE_BOX_ID={box.id} python3 update_web_terminal.py")


if __name__ == "__main__":
    asyncio.run(main())
