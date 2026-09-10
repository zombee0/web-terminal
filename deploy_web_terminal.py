import asyncio
import json
import os
import pathlib
import urllib.request

import boxlite
from boxlite import Boxlite, BoxliteRestOptions


BOX_ID = "xzt7sMEDdWBQ"
PORT = 3001
APP = pathlib.Path(__file__).parent / "web-terminal-linux-amd64"


def api(method, path):
    base = os.environ["BOXLITE_REST_URL"].rstrip("/").removesuffix("/api")
    request = urllib.request.Request(base + path, method=method)
    request.add_header("Authorization", "Bearer " + os.environ["BOXLITE_API_KEY"])
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode() or "{}")


async def main():
    for variable in ("BOXLITE_REST_URL", "BOXLITE_API_KEY"):
        if not os.environ.get(variable):
            raise RuntimeError(f"{variable} is not set")
    runtime = Boxlite.rest(BoxliteRestOptions.from_env())
    box = await runtime.get(BOX_ID)
    await box.start()
    await box.copy_in(str(APP), "/app/web-terminal")
    server = await box.exec(
        "sh",
        args=[
            "-lc",
            "pkill -f '^/app/web-terminal$' || true; "
            "chmod +x /app/web-terminal; "
            "nohup /app/web-terminal >/tmp/web-terminal.log 2>&1 &",
        ],
    )
    await server.wait()
    await asyncio.sleep(1)
    health = await box.exec("sh", args=["-lc", "curl -fsS http://127.0.0.1:3001/ | grep -q 'Box Terminal'"])
    await health.wait()
    url = api("GET", f"/api/box/{BOX_ID}/ports/{PORT}/preview-url")["url"]
    print(f"URL={url}")


asyncio.run(main())
