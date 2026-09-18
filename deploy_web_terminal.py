"""Create and provision a Web Terminal Box on cloud REST or local embedded BoxLite."""

import asyncio
import os

from web_terminal_boxlite import (
    create_runtime,
    prepare_runtime_env,
    runtime_mode,
)
from web_terminal_provision import (
    create_managed_box,
    provision_web_terminal,
    resolve_box,
)


async def main():
    mode = runtime_mode()
    prepare_runtime_env(mode)
    runtime = create_runtime(mode)
    box_id = os.environ.get("BOXLITE_BOX_ID")
    if box_id:
        box, box_name = await resolve_box(runtime, box_id)
        print(f"Resuming deployment in existing Box: {box_id}", flush=True)
    else:
        box, box_name = await create_managed_box(runtime, mode)
    print(f"RUNTIME={mode}", flush=True)
    print(f"BOX_ID={box.id}", flush=True)

    url = await provision_web_terminal(runtime, box, mode)
    print(f"BOX_NAME={box_name}")
    print(f"URL={url}")
    print(
        f"To update this Box: BOXLITE_RUNTIME={mode} BOXLITE_BOX_ID={box.id} python3 update_web_terminal.py"
    )


if __name__ == "__main__":
    asyncio.run(main())
