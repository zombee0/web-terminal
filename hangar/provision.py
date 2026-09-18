"""Provision and restart Web Terminal inside a BoxLite sandbox."""

import os
import pathlib
import re
import secrets
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

curl -fsSL https://chatgpt.com/codex/install.sh -o /tmp/install-codex.sh
su -s /bin/bash boxlite -c \
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
  /home/boxlite/.local/share/cursor-agent/versions \
  /home/boxlite/.npm-global/bin
BOXLITE_ENV='HOME=/home/boxlite PATH=/home/boxlite/.local/bin:/home/boxlite/.npm-global/bin:/usr/local/bin:/usr/bin:/bin'

CLAUDE_SRC=/home/boxlite/.web-terminal-claude
if [ -d "$CLAUDE_SRC" ]; then
  CLAUDE_SRC=$(find "$CLAUDE_SRC" -type f | head -n 1)
fi
if [ ! -f "$CLAUDE_SRC" ]; then
  echo "Claude binary was not uploaded" >&2
  exit 1
fi
install -m 0755 -o boxlite -g boxlite "$CLAUDE_SRC" /home/boxlite/.local/bin/claude
rm -rf /home/boxlite/.web-terminal-claude

CURSOR_TAR=/home/boxlite/.web-terminal-cursor-agent.tar.gz
CURSOR_VER_FILE=/home/boxlite/.web-terminal-cursor-version
if [ -d "$CURSOR_TAR" ]; then
  CURSOR_TAR=$(find "$CURSOR_TAR" -type f | head -n 1)
fi
if [ -d "$CURSOR_VER_FILE" ]; then
  CURSOR_VER_FILE=$(find "$CURSOR_VER_FILE" -type f | head -n 1)
fi
if [ ! -f "$CURSOR_TAR" ] || [ ! -s "$CURSOR_VER_FILE" ]; then
  echo "Cursor archive was not uploaded" >&2
  exit 1
fi
CURSOR_VERSION=$(cat "$CURSOR_VER_FILE")
CURSOR_DEST=/home/boxlite/.local/share/cursor-agent/versions/$CURSOR_VERSION
rm -rf "$CURSOR_DEST"
mkdir -p "$CURSOR_DEST"
tar --strip-components=1 -xzf "$CURSOR_TAR" -C "$CURSOR_DEST"
chown -R boxlite:boxlite /home/boxlite/.local/share/cursor-agent
ln -sfn "$CURSOR_DEST/cursor-agent" /home/boxlite/.local/bin/agent
ln -sfn "$CURSOR_DEST/cursor-agent" /home/boxlite/.local/bin/cursor-agent
chown -h boxlite:boxlite /home/boxlite/.local/bin/agent /home/boxlite/.local/bin/cursor-agent
rm -f /home/boxlite/.web-terminal-cursor-agent.tar.gz /home/boxlite/.web-terminal-cursor-version

su -s /bin/bash boxlite -c "$BOXLITE_ENV claude --version"
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

CLI_CACHE_DIR = pathlib.Path.home() / ".web-terminal" / "cli-cache"
CLAUDE_RELEASES_URL = "https://downloads.claude.ai/claude-code-releases"
CURSOR_INSTALL_URL = "https://cursor.com/install"
_HTTP_HEADERS = {"User-Agent": "web-terminal-provision"}


def _cli_linux_arch(mode):
    arch = guest_goarch(mode)
    if arch == "amd64":
        return "x64"
    if arch == "arm64":
        return "arm64"
    raise RuntimeError(f"Unsupported guest architecture for agent CLIs: {arch}")


def _http_download(url, dest=None, timeout=120):
    request = urllib.request.Request(url, headers=_HTTP_HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read()
    if dest is None:
        return data
    dest = pathlib.Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return dest


def fetch_agent_cli_artifacts(mode, *, log=None):
    arch = _cli_linux_arch(mode)
    cache = CLI_CACHE_DIR / arch
    cache.mkdir(parents=True, exist_ok=True)

    _emit(log, f"downloading Claude Code for linux-{arch}")
    version = _http_download(f"{CLAUDE_RELEASES_URL}/latest").decode().strip()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+.*", version):
        raise RuntimeError("Claude release metadata was not a version string")
    claude_bin = cache / f"claude-{version}-linux-{arch}"
    if not claude_bin.is_file():
        _http_download(
            f"{CLAUDE_RELEASES_URL}/{version}/linux-{arch}/claude",
            claude_bin,
            timeout=180,
        )
        claude_bin.chmod(0o755)

    _emit(log, "downloading Cursor CLI")
    installer = _http_download(CURSOR_INSTALL_URL).decode()
    match = re.search(r"lab/([^/]+)/", installer)
    if match is None:
        raise RuntimeError("Cursor installer did not include a package version")
    cursor_version = match.group(1)
    cursor_tarball = cache / f"cursor-agent-{cursor_version}-linux-{arch}.tar.gz"
    if not cursor_tarball.is_file():
        _http_download(
            f"https://downloads.cursor.com/lab/{cursor_version}/linux/{arch}/agent-cli-package.tar.gz",
            cursor_tarball,
            timeout=180,
        )
    cursor_version_file = cache / f"cursor-agent-{cursor_version}-linux-{arch}.version"
    cursor_version_file.write_text(cursor_version + "\n", encoding="utf-8")
    return claude_bin, cursor_tarball, cursor_version_file

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
    _emit(log, "installing Claude Code and Cursor CLI")
    claude_bin, cursor_tarball, cursor_version = fetch_agent_cli_artifacts(mode, log=log)
    await box.copy_in(str(claude_bin), "/home/boxlite/.web-terminal-claude")
    await box.copy_in(str(cursor_tarball), "/home/boxlite/.web-terminal-cursor-agent.tar.gz")
    await box.copy_in(str(cursor_version), "/home/boxlite/.web-terminal-cursor-version")
    await run_checked(
        box,
        "sh",
        args=["-lc", INSTALL_AGENT_CLIS_COMMAND],
        user="root",
        timeout_secs=120,
        log=log,
    )


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
    await install_agent_clis(box, mode, log=log)
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
    runtime,
    box,
    mode,
    *,
    host_port=None,
    log=None,
    reinstall=False,
    authenticated=True,
    install_clis=True,
):
    _emit(log, "resuming box")
    await ensure_started(runtime, box, mode)
    if install_clis:
        await install_agent_clis(box, mode, log=log)
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
                authenticated=authenticated,
                install_clis=False,
            )
    await wait_for_guest_health(box, attempts=30, timeout_secs=60, log=log)
    url = service_url(mode, box, authenticated=authenticated, host_port=host_port)
    if mode == "embedded":
        await wait_for_host_health(url.rstrip("/") + "/healthz")
    _emit(log, "resume complete")
    return url
