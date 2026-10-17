"""Create or reuse the authorized management Box and verify its network path."""

import asyncio
import json
import re
import shlex
import traceback
import urllib.parse
import urllib.request

from hangar.boxlite import cloud_api, cloud_create_payload, cloud_tunnel_url, create_runtime, ensure_started, run_checked
from supabase.config import ROOT, STATE, load_deployment_env, load_private, project_ref, required, save_private


async def manager():
    runtime = create_runtime("cloud")
    state_path = STATE / "manager.json"
    state = load_private(state_path, {})
    name = "web-terminal-manager-" + project_ref()
    infos = await runtime.list_info()
    if not state.get("box_id"):
        matching = [info for info in infos if info.name == name]
        if len(matching) > 1:
            raise RuntimeError("Multiple management Boxes found; refusing duplicate creation")
        if matching:
            state = {"box_id": matching[0].id, "name": name}
        else:
            payload = cloud_create_payload(name, {"cpus": 4, "memory_mib": 8192, "disk_size_gb": 10}, inbound=True)
            payload["auto_delete"] = 0
            result = await asyncio.to_thread(cloud_api, "POST", "/api/v1/boxes", payload, timeout=90)
            box_id = result.get("id") or result.get("box_id")
            if not box_id:
                raise RuntimeError("Management creation returned no ID")
            state = {"box_id": box_id, "name": name}
        save_private(state_path, state)
    box = await runtime.get(state["box_id"])
    if box is None:
        raise RuntimeError("Recorded management Box is missing; refusing duplicate creation")
    await ensure_started(runtime, box, "cloud")
    return runtime, box, state


async def main():
    load_deployment_env()
    runtime, box, state = await manager()
    print("Management Box ready; recovery ID stored privately", flush=True)
    if not state.get("deployed"):
        await run_checked(box, "mkdir", args=["-p", "/opt/web-terminal"], user="root", log=lambda _: None)
        print("Management exec verified", flush=True)
        await box.copy_in(str(ROOT / "supabase" / "maintenance.py"), "/opt/web-terminal/maintenance.py")
        print("Management file copy verified", flush=True)
        start = (
            "import socket,subprocess; s=socket.socket(); "
            "busy=s.connect_ex(('127.0.0.1',3001))==0; s.close(); "
            "None if busy else subprocess.Popen(['python3','/opt/web-terminal/maintenance.py'],"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)"
        )
        await run_checked(box, "sh", args=["-lc", "python3 -c " + shlex.quote(start)], user="root", log=lambda _: None)
    probe = """
import json,socket,urllib.request,urllib.error
for name,url in TARGETS.items():
    try:
        with urllib.request.urlopen(url,timeout=15) as r: print(name, 'HTTP',r.status)
    except urllib.error.HTTPError as e: print(name,'HTTP',e.code)
    except Exception as e: print(name,'FAILED',type(e).__name__)
try:
    host,port=DB
    with socket.create_connection((host,port),timeout=15): print('database_tcp','OK')
except Exception as e: print('database_tcp','FAILED',type(e).__name__)
"""
    db = urllib.parse.urlsplit(required("SUPABASE_DB_URL"))
    targets = {"boxlite_api": "https://app.boxlite.ai/api/v1/boxes", "supabase_auth": required("SUPABASE_URL").rstrip("/")+"/auth/v1/health", "guest_health": "http://127.0.0.1:3001/healthz"}
    probe = "TARGETS="+repr(targets)+"\nDB="+repr((db.hostname, db.port or 5432))+"\n"+probe
    await run_checked(box, "sh", args=["-lc", "python3 -c " + shlex.quote(probe)], user="root", timeout_secs=75, log=print)
    url = await asyncio.to_thread(cloud_tunnel_url, box.id)
    if not url:
        raise RuntimeError("Management tunnel returned no HTTPS entry")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.query or parsed.fragment or parsed.username:
        raise RuntimeError("Management tunnel URI cannot be used as a stable public HTTPS entry")
    state["entry_url"] = url.rstrip("/")
    save_private(STATE / "manager.json", state)
    health_url = state["entry_url"] + "/healthz"
    def check():
        with urllib.request.urlopen(health_url, timeout=20) as response:
            return response.status == 200 and response.read(32).strip() == b"ok"
    if not await asyncio.to_thread(check):
        raise RuntimeError("Management HTTPS health check failed")
    print("Management HTTPS ingress verified; address stored privately", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        # SDK and HTTP exception strings may contain authenticated upstream URIs.
        print("Preflight failed:", type(error).__name__, flush=True)
        frames = traceback.extract_tb(error.__traceback__)
        if frames:
            print("Failure location:", frames[-1].name, "line", frames[-1].lineno, flush=True)
        status = re.search(r"(?:HTTP|status(?: code)?)[ :=]*(\d{3})", str(error), re.I)
        if status:
            print("Provider HTTP status:", status[1], flush=True)
        exit_code = re.search(r"exit code (\d+)", str(error))
        if exit_code:
            print("Guest exit code:", exit_code[1], flush=True)
        for category in ("SyntaxError", "NameError", "PermissionError", "FileNotFoundError", "ModuleNotFoundError"):
            if category in str(error):
                print("Guest error category:", category, flush=True)
        raise SystemExit(1)
