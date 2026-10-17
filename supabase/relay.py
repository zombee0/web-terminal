"""Deploy the private API relay; credentials never enter its source bundle."""

import asyncio
import json
import secrets

import aiohttp

from supabase.config import ROOT, STATE, load_deployment_env, load_private, project_ref, required, save_private


async def deploy():
    state = load_private(STATE / "secrets.json")
    if not state.get("relay_token"):
        state["relay_token"] = secrets.token_urlsafe(48)
        save_private(STATE / "secrets.json", state)
    base = "https://api.supabase.com/v1/projects/" + project_ref()
    headers = {"Authorization": "Bearer " + required("SUPABASE_ACCESS_TOKEN")}
    async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=120)) as client:
        manager = load_private(STATE/"manager.json", {})
        configured = [
            {"name": "WT_RELAY_TOKEN", "value": state["relay_token"]},
            {"name": "WT_BOXLITE_API_KEY", "value": required("BOXLITE_API_KEY")},
        ]
        if manager.get("entry_url"):
            configured += [{"name": "WT_MANAGER_BOX_ID", "value": manager["box_id"]},
                           {"name": "WT_MANAGER_URL", "value": manager["entry_url"]}]
        async with client.post(base+"/secrets", headers=headers, json=configured) as response:
            if response.status not in (200, 201):
                raise RuntimeError(f"Relay secret configuration HTTP {response.status}")
        form = aiohttp.FormData()
        form.add_field("metadata", json.dumps({"entrypoint_path": "index.ts", "name": "boxlite-relay", "verify_jwt": False}))
        form.add_field("file", (ROOT/"supabase/functions/boxlite-relay/index.ts").read_bytes(), filename="index.ts", content_type="application/typescript")
        async with client.post(base+"/functions/deploy?slug=boxlite-relay", headers=headers, data=form) as response:
            if response.status not in (200, 201):
                raise RuntimeError(f"Relay deployment HTTP {response.status}")
        url = required("SUPABASE_URL") + "/functions/v1/boxlite-relay"
        async with client.get(url+"/v1/boxes", headers={"Authorization": "Bearer "+state["relay_token"]}) as response:
            if response.status != 200:
                raise RuntimeError(f"Relay provider verification HTTP {response.status}")
        async with client.get(url+"/v1/boxes") as response:
            if response.status != 401:
                raise RuntimeError("Relay allowed an unauthenticated request")
    print("Private relay deployed: provider access and anonymous denial verified", flush=True)


if __name__ == "__main__":
    load_deployment_env()
    try:
        asyncio.run(deploy())
    except Exception as error:
        print("Relay deployment failed:", type(error).__name__, flush=True)
        raise SystemExit(1)
