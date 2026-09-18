"""Replace Web Terminal in an existing Box without provisioning a new Box."""

import asyncio
import os

from webterminal.boxlite import create_runtime, prepare_runtime_env, runtime_mode
from webterminal.provision import resolve_box, resume_web_terminal


async def main():
    mode = runtime_mode()
    if not os.environ.get("BOXLITE_BOX_ID"):
        raise RuntimeError("BOXLITE_BOX_ID is not set")
    prepare_runtime_env(mode)
    box_id = os.environ["BOXLITE_BOX_ID"]
    runtime = create_runtime(mode)
    box, _ = await resolve_box(runtime, box_id)
    url = await resume_web_terminal(runtime, box, mode, reinstall=True, authenticated=False)
    print(f"RUNTIME={mode}")
    print(f"BOX_ID={box_id}")
    print(f"URL={url}")


if __name__ == "__main__":
    asyncio.run(main())
