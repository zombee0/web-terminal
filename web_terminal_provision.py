"""Provision and restart Web Terminal inside a BoxLite sandbox."""

import os
import secrets
from datetime import datetime, timezone

from web_terminal_boxlite import (
    app_path,
    box_display_name,
    create_box_options,
    create_cloud_box,
    ensure_started,
    parse_resources,
    run_checked,
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

START_APP_COMMAND = r"""
set -eu
if [ "$(id -u)" -ne 0 ]; then
  echo "Starting Web Terminal requires root inside the Box." >&2
  exit 1
fi
if [ ! -x /usr/local/bin/web-terminal ]; then
  echo "web-terminal is not installed" >&2
  exit 1
fi
pkill -f '^/usr/local/bin/web-terminal$' || true
su -s /bin/bash boxlite -c \
  'cd /home/boxlite && HOME=/home/boxlite LANG=zh_CN.UTF-8 LC_ALL=zh_CN.UTF-8 PATH=/home/boxlite/.local/bin:/home/boxlite/.npm-global/bin:/usr/local/bin:/usr/bin:/bin setsid -f /usr/local/bin/web-terminal >/tmp/web-terminal.log 2>&1 </dev/null'
"""


def new_box_name(configured=None):
    if configured:
        return configured
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"web-terminal-{timestamp}-{secrets.token_hex(3)}"


def _emit(log, message):
    if log is None:
        print(message, flush=True)
        return
    text = message if message.endswith("\n") else message + "\n"
    log(text)


async def create_managed_box(runtime, mode, *, name=None, host_port=None, resources=None, log=None):
    box_name = new_box_name(name or os.environ.get("BOXLITE_BOX_NAME"))
    spec = parse_resources(resources)
    _emit(
        log,
        f"creating box name={box_name} cpus={spec['cpus']} memory_mib={spec['memory_mib']} disk_size_gb={spec['disk_size_gb']}",
    )
    if mode == "cloud":
        box_id, box_name = create_cloud_box(box_name, spec)
        box = await runtime.get(box_id)
        if box is None:
            raise RuntimeError(f"Cloud box created but not readable: {box_id}")
    else:
        box = await runtime.create(
            create_box_options(mode, host_port=host_port, resources=spec),
            name=box_name,
        )
    _emit(log, f"created box id={box.id}")
    return box, box_name


async def resolve_box(runtime, box_id, fallback_name=None):
    box = await runtime.get(box_id)
    if box is None:
        raise RuntimeError(f"Box not found: {box_id}")
    info = await runtime.get_info(box_id)
    return box, box_display_name(info, fallback_name or box_id)


async def install_web_terminal(box, mode, *, log=None):
    await box.copy_in(str(app_path(mode)), "/home/boxlite/.web-terminal-upload")
    await run_checked(
        box,
        "sh",
        args=["-lc", INSTALL_APP_COMMAND],
        user="root",
        timeout_secs=60,
        log=log,
    )


async def provision_web_terminal(runtime, box, mode, *, host_port=None, log=None):
    _emit(log, "starting box")
    await ensure_started(runtime, box, mode)
    _emit(log, "bootstrapping guest packages")
    await run_checked(
        box,
        "sh",
        args=["-lc", BOOTSTRAP_COMMAND],
        user="root",
        timeout_secs=900,
        log=log,
    )
    _emit(log, "installing web-terminal")
    await install_web_terminal(box, mode, log=log)
    _emit(log, "waiting for guest /healthz")
    await wait_for_guest_health(box, attempts=60, timeout_secs=90, log=log)
    url = service_url(mode, box, authenticated=True, host_port=host_port)
    if mode == "embedded":
        await wait_for_host_health(url.rstrip("/") + "/healthz")
    _emit(log, "provision complete")
    return url


async def resume_web_terminal(
    runtime, box, mode, *, host_port=None, log=None, reinstall=False, authenticated=True
):
    _emit(log, "resuming box")
    await ensure_started(runtime, box, mode)
    if reinstall:
        _emit(log, "reinstalling web-terminal")
        await install_web_terminal(box, mode, log=log)
    else:
        _emit(log, "starting web-terminal")
        try:
            await run_checked(
                box,
                "sh",
                args=["-lc", START_APP_COMMAND],
                user="root",
                timeout_secs=60,
                log=log,
            )
        except RuntimeError:
            _emit(log, "web-terminal missing; reinstalling")
            return await resume_web_terminal(
                runtime,
                box,
                mode,
                host_port=host_port,
                log=log,
                reinstall=True,
            )
    await wait_for_guest_health(box, attempts=30, timeout_secs=60, log=log)
    url = service_url(mode, box, authenticated=authenticated, host_port=host_port)
    if mode == "embedded":
        await wait_for_host_health(url.rstrip("/") + "/healthz")
    _emit(log, "resume complete")
    return url
