"""Recover an existing REST execution through a proxy-aware WebSocket client.

The SDK's native WebSocket path may not use host HTTP proxies. Reattach to
the same execution; never restart a command because its stream disconnected.
Protocol: BoxLite REST binary channel 1=stdout, 2=stderr, JSON exit frames.
"""

import asyncio
import json
import os
import time
import urllib.parse

from websockets.asyncio.client import connect


async def recover_execution(box_id, execution_id, stdout, stderr, timeout=120):
    from hangar.boxlite import cloud_api

    base = os.environ.get("BOXLITE_REST_URL", "https://app.boxlite.ai/api").rstrip("/")
    path = f"/v1/boxes/{urllib.parse.quote(str(box_id), safe='')}/executions/{urllib.parse.quote(str(execution_id), safe='')}/attach"
    url = urllib.parse.urlsplit(base + path)
    url = urllib.parse.urlunsplit(url._replace(scheme="wss" if url.scheme == "https" else "ws"))
    deadline = time.monotonic() + timeout
    try:
        async with connect(url, additional_headers={"Authorization": "Bearer " + os.environ["BOXLITE_API_KEY"]},
                           open_timeout=20, close_timeout=5, ping_interval=20, ping_timeout=30,
                           max_size=4*1024*1024) as websocket:
            while time.monotonic() < deadline:
                message = await asyncio.wait_for(websocket.recv(), max(1, deadline-time.monotonic()))
                if isinstance(message, bytes) and message:
                    sink = stdout if message[0] == 1 else stderr if message[0] == 2 else None
                    if sink is not None:
                        sink.write(message[1:].decode("utf-8", errors="replace"))
                        sink.flush()
                elif isinstance(message, str):
                    data = json.loads(message)
                    if data.get("type") == "exit" and isinstance(data.get("exit_code"), int):
                        return data["exit_code"]
    except Exception:
        # Stream failure is not execution failure. The status endpoint is authoritative.
        pass
    while time.monotonic() < deadline:
        state = await asyncio.to_thread(cloud_api, "GET", f"/api/v1/boxes/{box_id}/executions/{execution_id}")
        if state.get("status") in {"completed", "failed", "exited"}:
            code = state.get("exit_code")
            if isinstance(code, int):
                return code
            raise RuntimeError("Remote execution ended without an exit status")
        await asyncio.sleep(1)
    raise RuntimeError("Remote execution is still running; no duplicate was started")
