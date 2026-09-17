"""Replace Web Terminal in an existing Box without provisioning a new Box."""

import asyncio
import os

from web_terminal_boxlite import (
    app_path,
    create_runtime,
    ensure_started,
    prepare_runtime_env,
    run_checked,
    runtime_mode,
    service_url,
    wait_for_guest_health,
    wait_for_host_health,
)


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


async def main():
    mode = runtime_mode()
    if not os.environ.get("BOXLITE_BOX_ID"):
        raise RuntimeError("BOXLITE_BOX_ID is not set")
    prepare_runtime_env(mode)
    box_id = os.environ["BOXLITE_BOX_ID"]
    runtime = create_runtime(mode)
    box = await runtime.get(box_id)
    if box is None:
        raise RuntimeError(f"Box not found: {box_id}")
    await ensure_started(runtime, box, mode)
    await box.copy_in(str(app_path(mode)), "/home/boxlite/.web-terminal-upload")
    await run_checked(
        box,
        "sh",
        args=["-lc", UPDATE_COMMAND],
        user="root",
        timeout_secs=60,
    )
    await wait_for_guest_health(box, attempts=30, timeout_secs=60)
    url = service_url(mode, box, authenticated=False)
    if mode == "embedded":
        await wait_for_host_health(url.rstrip("/") + "/healthz")
    print(f"RUNTIME={mode}")
    print(f"BOX_ID={box_id}")
    print(f"URL={url}")


if __name__ == "__main__":
    asyncio.run(main())
