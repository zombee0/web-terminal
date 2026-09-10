import asyncio
import os

from boxlite import Boxlite, BoxliteRestOptions


BOX_ID = "xzt7sMEDdWBQ"
APP = os.path.join(os.path.dirname(__file__), "web-terminal-linux-amd64")


async def main():
    runtime = Boxlite.rest(BoxliteRestOptions.from_env())
    box = await runtime.get(BOX_ID)
    await box.copy_in(APP, "/app/web-terminal")
    result = await box.exec(
        "sh",
        args=[
            "-lc",
            "pkill -f '^/app/web-terminal$' || true; "
            "chmod +x /app/web-terminal; "
            "nohup /app/web-terminal >/tmp/web-terminal.log 2>&1 &",
        ],
    )
    await result.wait()
    await asyncio.sleep(1)
    print("Web Terminal updated")


if not os.environ.get("BOXLITE_REST_URL"):
    raise RuntimeError("BOXLITE_REST_URL is not set")

asyncio.run(main())
