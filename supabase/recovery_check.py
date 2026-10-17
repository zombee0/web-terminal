"""Live management restart acceptance, retaining the existing Box and URL."""

import asyncio
import time

import aiohttp

from hangar.boxlite import cloud_api, ensure_stopped
from supabase.config import STATE, load_deployment_env, load_private
from supabase.preflight import manager


async def main():
    load_deployment_env()
    runtime, box, state = await manager()
    await ensure_stopped(runtime, box, "cloud")
    print("Management Box stopped; waiting for scheduled recovery", flush=True)
    started = time.monotonic()
    async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=8)) as client:
        for attempt in range(50):
            try:
                async with client.get(state["entry_url"]+"/healthz") as response:
                    if response.status == 200 and (await response.text()).strip() == "ok":
                        async with client.get(state["entry_url"]+"/api/boxes") as denied:
                            if denied.status != 401:
                                raise RuntimeError("Authentication did not survive restart")
                        print("Management recovery passed in", round(time.monotonic()-started), "seconds; same Box and HTTPS entry", flush=True)
                        return
            except (aiohttp.ClientError, TimeoutError):
                pass
            if attempt % 5 == 0:
                print("Waiting for management recovery", flush=True)
            await asyncio.sleep(3)
    raise RuntimeError("Scheduled management recovery failed")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        print("Recovery acceptance failed:", type(error).__name__, flush=True)
        raise SystemExit(1)
