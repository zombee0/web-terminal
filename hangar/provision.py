"""Provision and restart Web Terminal inside a BoxLite sandbox."""

import asyncio
import json
import os
import pathlib
import re
import secrets
import shlex
import urllib.request
from datetime import datetime, timezone

from hangar.boxlite import (
    app_path,
    box_display_name,
    create_box_options,
    create_cloud_box,
    ensure_started,
    guest_goarch,
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
  git \
  locales \
  passwd \
  procps \
  tar \
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

curl --connect-timeout 10 --max-time 60 -fsSL https://chatgpt.com/codex/install.sh -o /tmp/install-codex.sh
timeout 180s su -s /bin/bash boxlite -c \
  'HOME=/home/boxlite PATH=/home/boxlite/.local/bin:/usr/local/bin:/usr/bin:/bin CODEX_NON_INTERACTIVE=1 sh /tmp/install-codex.sh'
rm -f /tmp/install-codex.sh

command -v tmux >/dev/null
su -s /bin/bash boxlite -c \
  'HOME=/home/boxlite PATH=/home/boxlite/.local/bin:/usr/local/bin:/usr/bin:/bin codex --version'
rm -rf /var/lib/apt/lists/*
"""

INSTALL_AGENT_CLIS_COMMAND = r"""
set -eu
if [ "$(id -u)" -ne 0 ]; then
  echo "Installing agent CLIs requires root inside the Box." >&2
  exit 1
fi
if ! grep -q '^boxlite:' /etc/passwd 2>/dev/null; then
  useradd --create-home --shell /bin/bash boxlite
fi
install -d -o boxlite -g boxlite \
  /home/boxlite/.local/bin \
  /home/boxlite/.local/share \
  /home/boxlite/.local/share/cursor-agent/versions \
  /home/boxlite/.npm-global/bin
chown -R boxlite:boxlite /home/boxlite/.local
BOXLITE_ENV='HOME=/home/boxlite PATH=/home/boxlite/.local/bin:/home/boxlite/.npm-global/bin:/usr/local/bin:/usr/bin:/bin'

if ! su -s /bin/bash boxlite -c \
  'HOME=/home/boxlite PATH=/home/boxlite/.local/bin:/home/boxlite/.npm-global/bin:/usr/local/bin:/usr/bin:/bin sh -c "curl -fsSL https://claude.ai/install.sh | bash"'; then
  echo "WARNING: Claude Code installation failed; continuing without claude" >&2
fi
su -s /bin/bash boxlite -c \
  'HOME=/home/boxlite PATH=/home/boxlite/.local/bin:/home/boxlite/.npm-global/bin:/usr/local/bin:/usr/bin:/bin sh -c "curl --connect-timeout 10 --max-time 180 -fsS https://cursor.com/install | bash"'

if su -s /bin/bash boxlite -c "$BOXLITE_ENV claude --version"; then :; else echo "WARNING: claude is unavailable" >&2; fi
su -s /bin/bash boxlite -c "$BOXLITE_ENV agent --version"
"""

CHECK_AGENT_CLIS_COMMAND = r"""
set -eu
BOXLITE_ENV='HOME=/home/boxlite PATH=/home/boxlite/.local/bin:/home/boxlite/.npm-global/bin:/usr/local/bin:/usr/bin:/bin'
su -s /bin/bash boxlite -c "$BOXLITE_ENV command -v claude >/dev/null"
su -s /bin/bash boxlite -c "$BOXLITE_ENV command -v agent >/dev/null"
su -s /bin/bash boxlite -c "$BOXLITE_ENV claude --version"
su -s /bin/bash boxlite -c "$BOXLITE_ENV agent --version"
"""

# This is the container's root PID 1 in protected deployments. Keeping the
# listening socket here makes service-child failure fail closed; an unprivileged
# terminal cannot replace the authenticated listener while it restarts.
PROTECTED_INIT = r"""
import os,pathlib,signal,socket,subprocess,time
listener=socket.socket()
listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
listener.bind(('0.0.0.0',3001))
listener.listen(128)
pathlib.Path('/run/web-terminal-supervised').touch(mode=0o600)
child=None
stopping=False
def stop(signum,frame):
    global stopping
    stopping=True
    if child is not None: child.terminate()
signal.signal(signal.SIGTERM,stop)
signal.signal(signal.SIGINT,stop)
while not stopping:
    if not os.path.isfile('/etc/web-terminal/gateway.json') or not os.path.isfile('/usr/local/bin/web-terminal'):
        time.sleep(1)
        continue
    child=subprocess.Popen(['/usr/local/bin/web-terminal'],cwd='/home/boxlite',
        env={'PATH':'/usr/local/bin:/usr/bin:/bin','WEB_TERMINAL_GATEWAY_CONFIG':'/etc/web-terminal/gateway.json',
             'WEB_TERMINAL_LISTEN_FD':str(listener.fileno())},pass_fds=(listener.fileno(),),
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    child.wait()
    if not stopping: time.sleep(1)
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


async def create_managed_box(runtime, mode, *, name=None, host_port=None, resources=None, log=None, inbound=True):
    box_name = new_box_name(name or os.environ.get("BOXLITE_BOX_NAME"))
    spec = parse_resources(resources)
    _emit(
        log,
        f"creating box name={box_name} cpus={spec['cpus']} memory_mib={spec['memory_mib']} disk_size_gb={spec['disk_size_gb']}",
    )
    if mode == "cloud":
        box_id, box_name = await asyncio.to_thread(create_cloud_box, box_name, spec, inbound=inbound)
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


async def install_agent_clis(box, mode, *, log=None):
    try:
        await run_checked(
            box,
            "sh",
            args=["-lc", CHECK_AGENT_CLIS_COMMAND],
            user="root",
            timeout_secs=30,
            log=log,
        )
        _emit(log, "Claude Code and Cursor CLI already installed")
        return
    except RuntimeError:
        pass
    _emit(log, "installing Claude Code and Cursor CLI inside Box")
    await run_checked(
        box,
        "sh",
        args=["-lc", INSTALL_AGENT_CLIS_COMMAND],
        user="root",
        timeout_secs=600,
        log=log,
    )


def protected_start_command():
    return r"""
set -eu
test "$(id -u)" -eq 0
test -f /etc/web-terminal/gateway.json
test -x /usr/bin/setpriv
pkill -u root -f '^/usr/local/bin/web-terminal$' || true
if [ -f /run/web-terminal-supervised ]; then exit 0; fi
install -m 0600 /dev/null /var/log/web-terminal.log
cd /home/boxlite
env -i PATH=/usr/local/bin:/usr/bin:/bin WEB_TERMINAL_GATEWAY_CONFIG=/etc/web-terminal/gateway.json \
  setsid -f /usr/local/bin/web-terminal >/var/log/web-terminal.log 2>&1 </dev/null
"""


async def install_web_terminal(box, mode, *, log=None, gateway_config=None):
    if gateway_config is not None:
        await box.copy_in(str(app_path(mode)), "/root/.web-terminal-upload")
        config = shlex.quote(json.dumps(gateway_config, separators=(",", ":")))
        command = (
            "set -eu\ninstall -d -m 0755 -o root -g root /etc/web-terminal\n"
            "install -m 0755 -o root -g root /root/.web-terminal-upload /usr/local/bin/web-terminal\n"
            "rm -f /root/.web-terminal-upload\n"
            f"printf '%s' {config} > /etc/web-terminal/gateway.json\n"
            "chown root:root /etc/web-terminal/gateway.json\nchmod 0600 /etc/web-terminal/gateway.json\n"
            + protected_start_command()
        )
        await run_checked(box, "sh", args=["-lc", command], user="root", timeout_secs=60, log=log)
        return
    await box.copy_in(str(app_path(mode)), "/home/boxlite/.web-terminal-upload")
    await run_checked(
        box,
        "sh",
        args=["-lc", INSTALL_APP_COMMAND],
        user="root",
        timeout_secs=60,
        log=log,
    )


async def provision_web_terminal(runtime, box, mode, *, host_port=None, log=None, gateway_config=None, publish_url=True):
    _emit(log, "starting box")
    await ensure_started(runtime, box, mode)
    _emit(log, "bootstrapping guest packages")
    apt_mirror = os.environ.get("WEB_TERMINAL_APT_MIRROR", "").strip()
    if not apt_mirror and mode == "embedded":
        apt_mirror = "mirrors.tuna.tsinghua.edu.cn"
    bootstrap_command = BOOTSTRAP_COMMAND
    if apt_mirror:
        bootstrap_command = (
            f"APT_MIRROR={shlex.quote(apt_mirror)}\n"
            "find /etc/apt -type f \\\n+  \\( -name '*.list' -o -name '*.sources' \\) \\\n+  -exec sed -i \"s|deb.debian.org|$APT_MIRROR|g\" {} +\n"
            + BOOTSTRAP_COMMAND
        )
    if apt_mirror:
        bootstrap_command = (
            f"APT_MIRROR={shlex.quote(apt_mirror)}\n"
            "find /etc/apt -type f -exec sed -i \"s|deb.debian.org|$APT_MIRROR|g\" {} +\n"
            + BOOTSTRAP_COMMAND
        )
    await run_checked(
        box,
        "sh",
        args=["-lc", bootstrap_command],
        user="root",
        timeout_secs=900,
        log=log,
    )
    await install_agent_clis(box, mode, log=log)
    _emit(log, "installing web-terminal")
    await install_web_terminal(box, mode, log=log, gateway_config=gateway_config)
    _emit(log, "waiting for guest /healthz")
    await wait_for_guest_health(box, attempts=60, timeout_secs=90, log=log)
    url = service_url(mode, box, authenticated=True, host_port=host_port) if publish_url else None
    if mode == "embedded" and url:
        await wait_for_host_health(url.rstrip("/") + "/healthz")
    _emit(log, "provision complete")
    return url


async def resume_web_terminal(
    runtime,
    box,
    mode,
    *,
    host_port=None,
    log=None,
    reinstall=False,
    authenticated=True,
    install_clis=True,
    gateway_config=None,
    publish_url=True,
):
    _emit(log, "resuming box")
    await ensure_started(runtime, box, mode)
    if install_clis:
        await install_agent_clis(box, mode, log=log)
    if reinstall or gateway_config is not None:
        _emit(log, "reinstalling web-terminal")
        await install_web_terminal(box, mode, log=log, gateway_config=gateway_config)
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
                authenticated=authenticated,
                install_clis=False,
                gateway_config=gateway_config,
                publish_url=publish_url,
            )
    await wait_for_guest_health(box, attempts=30, timeout_secs=60, log=log)
    url = service_url(mode, box, authenticated=authenticated, host_port=host_port) if publish_url else None
    if mode == "embedded" and url:
        await wait_for_host_health(url.rstrip("/") + "/healthz")
    _emit(log, "resume complete")
    return url
