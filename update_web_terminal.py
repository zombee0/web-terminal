"""Replace Web Terminal in an existing Box without provisioning a new Box."""

import asyncio
import json
import os
import pathlib
import subprocess
import sys
import urllib.request

from boxlite import Boxlite, BoxliteRestOptions


DEFAULT_REST_URL = "https://dev.boxlite.ai/api"
PORT = 3001
APP = pathlib.Path(__file__).parent / "build" / "web-terminal-linux-amd64"
BUILD_SCRIPT = pathlib.Path(__file__).parent / "build_web_terminal_binary.sh"

UPDATE_COMMAND = r"""
set -eu
if [ "$(id -u)" -ne 0 ]; then
  echo "Updating Web Terminal requires root inside the Box." >&2
  exit 1
fi
pkill -f '^/usr/local/bin/web-terminal$' || true
install -m 0755 -o root -g root /home/boxlite/.web-terminal-upload /usr/local/bin/web-terminal
rm -f /home/boxlite/.web-terminal-upload
su -s /bin/bash boxlite -c \
  'cd /home/boxlite && HOME=/home/boxlite LANG=zh_CN.UTF-8 LC_ALL=zh_CN.UTF-8 PATH=/home/boxlite/.local/bin:/home/boxlite/.npm-global/bin:/usr/local/bin:/usr/bin:/bin setsid -f /usr/local/bin/web-terminal >/tmp/web-terminal.log 2>&1 </dev/null'
"""


def configure_environment():
    if not os.environ.get("BOXLITE_REST_URL"):
        os.environ["BOXLITE_REST_URL"] = DEFAULT_REST_URL
    if not os.environ.get("BOXLITE_API_KEY"):
        raise RuntimeError("BOXLITE_API_KEY is not set")
    if not os.environ.get("BOXLITE_BOX_ID"):
        raise RuntimeError("BOXLITE_BOX_ID is not set")
    ensure_app()


def ensure_app():
    if APP.is_file():
        return
    print(f"Web Terminal binary not found; building {APP}", flush=True)
    subprocess.run(["bash", str(BUILD_SCRIPT)], cwd=BUILD_SCRIPT.parent, check=True)
    if not APP.is_file():
        raise RuntimeError(f"Build completed without producing: {APP}")


def api(method, path, payload=None):
    base = os.environ["BOXLITE_REST_URL"].rstrip("/").removesuffix("/api")
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(base + path, data=data, method=method)
    request.add_header("Authorization", "Bearer " + os.environ["BOXLITE_API_KEY"])
    if data is not None:
        request.add_header("Content-Type", "application/json")
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


async def ensure_started(runtime, box):
    info = await runtime.get_info(box.id)
    if info is not None and info.state.running:
        return
    api("POST", f"/api/v1/boxes/{box.id}/start", {})
    for _ in range(60):
        info = await runtime.get_info(box.id)
        if info is not None and info.state.running:
            return
        await asyncio.sleep(1)
    raise RuntimeError(f"Box did not become running: {box.id}")


async def main():
    configure_environment()
    box_id = os.environ["BOXLITE_BOX_ID"]
    runtime = Boxlite.rest(BoxliteRestOptions.from_env())
    box = await runtime.get(box_id)
    if box is None:
        raise RuntimeError(f"Box not found: {box_id}")
    await ensure_started(runtime, box)
    await box.copy_in(str(APP), "/home/boxlite/.web-terminal-upload")
    await run_checked(
        box,
        "sh",
        args=["-lc", UPDATE_COMMAND],
        user="root",
        timeout_secs=60,
    )
    await run_checked(
        box,
        "sh",
        args=[
            "-lc",
            "for attempt in $(seq 1 30); do "
            "curl -fsS http://127.0.0.1:3001/healthz | grep -qx ok && exit 0; "
            "sleep 1; done; cat /tmp/web-terminal.log >&2; exit 1",
        ],
        user="root",
        timeout_secs=60,
    )
    url = api("GET", f"/api/box/{box_id}/ports/{PORT}/preview-url")["url"]
    print(f"BOX_ID={box_id}")
    print(f"URL={url}")


if __name__ == "__main__":
    asyncio.run(main())
