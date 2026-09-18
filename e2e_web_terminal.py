#!/usr/bin/env python3
"""Exercise a running Web Terminal over HTTP and WebSocket."""

import argparse
import asyncio
import json
import re
import sys
import urllib.request

import websockets


async def wait_for_output(ws, predicate, timeout):
    buffer = bytearray()
    async with asyncio.timeout(timeout):
        while True:
            message = await ws.recv()
            if isinstance(message, str):
                buffer.extend(message.encode("utf-8", "replace"))
            else:
                buffer.extend(message)
            text = bytes(buffer).decode("utf-8", "replace")
            if predicate(text):
                return text
    raise TimeoutError("timed out waiting for terminal output")


async def exercise_terminal(ws_url):
    async with websockets.connect(ws_url, open_timeout=10, max_size=4 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "resize", "cols": 120, "rows": 40}))
        await asyncio.sleep(0.5)
        await ws.send("printf 'E2E_WEB_TERMINAL_OK\\n'\n")
        output = await wait_for_output(
            ws,
            lambda text: "E2E_WEB_TERMINAL_OK" in text,
            timeout=20,
        )
        await ws.send("id\n")
        output = await wait_for_output(
            ws,
            lambda text: "uid=" in text and "boxlite" in text,
            timeout=20,
        )
        await ws.send("tmux -V; command -v codex; command -v claude; command -v agent; id\n")
        output = await wait_for_output(
            ws,
            lambda text: bool(re.search(r"tmux \d", text))
            and "/codex" in text
            and "/claude" in text
            and "/agent" in text
            and "uid=" in text
            and "boxlite" in text,
            timeout=20,
        )
        return output


def fetch(url):
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.status, response.read()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:3001")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    status, body = fetch(base + "/healthz")
    if status != 200 or body.decode().strip() != "ok":
        raise SystemExit(f"healthz failed: status={status} body={body!r}")
    print("PASS healthz")

    status, body = fetch(base + "/")
    html = body.decode()
    if status != 200 or "Box Terminal" not in html or "/ws" not in html:
        raise SystemExit(f"home page failed: status={status}")
    print("PASS home page")

    ws_url = base.replace("http://", "ws://", 1).replace("https://", "wss://", 1) + "/ws"
    output = asyncio.run(exercise_terminal(ws_url))
    print("PASS websocket pty")
    print(output[-400:].replace("\x1b", ""))
    print("E2E_OK")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"E2E_FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
