"""Repeatable hosted deployment; recovery IDs and URLs stay in private state."""

import asyncio
import io
import json
import os
import pathlib
import shlex
import subprocess
import tarfile
import time
import urllib.request

import aiohttp

from hangar.boxlite import run_checked
from supabase.config import ROOT, STATE, load_deployment_env, load_private, project_ref, required, save_private
from supabase.preflight import manager


def build_bundle():
    env = dict(os.environ, GOOS="linux", GOARCH="amd64")
    subprocess.run(["bash", "scripts/build_web_terminal_binary.sh"], cwd=ROOT, env=env, check=True)
    page = subprocess.check_output(["go", "-C", "go", "run", ".", "--print-page"], cwd=ROOT)
    (ROOT/"build/terminal.html").write_bytes(page)
    target = ROOT/"build/supabase-release.tar.gz"
    paths = [ROOT/"requirements.txt", ROOT/"build/web-terminal-linux-amd64", ROOT/"hangar/control.html"]
    for directory in ("hangar", "supabase"):
        paths.extend((ROOT/directory).glob("*.py"))
    paths += [ROOT/"supabase/requirements.txt", ROOT/"supabase/hosted.js", ROOT/"supabase/admin.html",
              ROOT/"supabase/admin.js", ROOT/"supabase/certs/supabase-ca-2021.crt"]
    with tarfile.open(target, "w:gz") as archive:
        for path in paths:
            archive.add(path, arcname=str(path.relative_to(ROOT)), recursive=False)
        archive.add(ROOT/"build/terminal.html", arcname="supabase/terminal.html")
    return target


async def configure_auth(entry):
    payload = {"site_url": entry, "uri_allow_list": entry+"/auth/callback",
               "external_google_enabled": True, "external_github_enabled": False,
               "external_email_enabled": False, "external_phone_enabled": False,
               "external_anonymous_users_enabled": False,
               "external_google_client_id": required("GOOGLE_OAUTH_CLIENT_ID"),
               "external_google_secret": required("GOOGLE_OAUTH_CLIENT_SECRET")}
    async with aiohttp.ClientSession(trust_env=True) as client:
        async with client.patch("https://api.supabase.com/v1/projects/"+project_ref()+"/config/auth",
                                headers={"Authorization": "Bearer "+required("SUPABASE_ACCESS_TOKEN")}, json=payload) as response:
            if response.status != 200:
                raise RuntimeError(f"Auth configuration HTTP {response.status}")
    print("Google Auth callback configured for the management entry", flush=True)


async def deploy():
    load_deployment_env()
    bundle = await asyncio.to_thread(build_bundle)
    runtime, box, state = await manager()
    if not state.get("entry_url"):
        raise RuntimeError("Run supabase.preflight first")
    keys = load_private(STATE/"secrets.json")
    config = {key: keys[key] for key in ("database_url", "encryption_key", "signing_key")}
    config.update({"public_url": state["entry_url"], "supabase_url": required("SUPABASE_URL"),
                   "publishable_key": required("SUPABASE_PUBLISHABLE_KEY"),
                   "boxlite_url": required("SUPABASE_URL")+"/functions/v1/boxlite-relay/api",
                   "boxlite_key": keys["relay_token"]})
    private = STATE/"manager-config.json"
    save_private(private, config)
    print("Uploading management release", flush=True)
    await box.copy_in(str(bundle), "/root/web-terminal-release.tar.gz")
    await box.copy_in(str(private), "/root/web-terminal-manager.json")
    command = r'''
set -eu
umask 077
mkdir -p /opt/web-terminal/current /etc/web-terminal
chmod 700 /etc/web-terminal
install -m 0600 /root/web-terminal-manager.json /etc/web-terminal/manager.json
rm -f /root/web-terminal-manager.json
tar -xzf /root/web-terminal-release.tar.gz -C /opt/web-terminal/current
rm -f /root/web-terminal-release.tar.gz
rm -f /opt/web-terminal/current/supabase/admin_access.py
python3 -m venv /opt/web-terminal/venv
/opt/web-terminal/venv/bin/pip install --disable-pip-version-check -q -r /opt/web-terminal/current/supabase/requirements.txt
'''
    await run_checked(box, "sh", args=["-lc", command], user="root", timeout_secs=900, log=lambda _: None)
    print("Management dependencies installed", flush=True)
    await configure_auth(state["entry_url"])
    start = r'''
set -eu
if [ -f /opt/web-terminal/supervisor.pid ]; then
  kill "$(cat /opt/web-terminal/supervisor.pid)" 2>/dev/null || true
  sleep 3
fi
pkill -f '^python3 /opt/web-terminal/maintenance.py$' || true
pkill -f '^/opt/web-terminal/venv/bin/python -m supabase.app$' || true
install -m 0600 /dev/null /var/log/web-terminal-manager.log
nohup /opt/web-terminal/venv/bin/python /opt/web-terminal/current/supabase/supervise.py >/var/log/web-terminal-manager.log 2>&1 </dev/null &
echo $! >/opt/web-terminal/supervisor.pid
'''
    await run_checked(box, "sh", args=["-lc", start], user="root", timeout_secs=30, log=lambda _: None)
    async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=15)) as client:
        for attempt in range(30):
            try:
                async with client.get(state["entry_url"]+"/healthz") as response:
                    if response.status == 200 and (await response.text()).strip() == "ok":
                        break
            except Exception:
                pass
            await asyncio.sleep(2)
        else:
            raise RuntimeError("Management health verification failed")
        async with client.get(state["entry_url"]+"/api/session") as response:
            if response.status != 200 or (await response.json()).get("user") is not None:
                raise RuntimeError("Anonymous session verification failed")
        async with client.get(state["entry_url"]+"/api/boxes") as response:
            if response.status != 401:
                raise RuntimeError("Anonymous inventory was not denied")
    state["deployed"] = True
    save_private(STATE/"manager.json", state)
    print("Management deployed: HTTPS health and anonymous access checks passed; entry stored privately", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(deploy())
    except Exception as error:
        # No HTTP exception rendering: it could disclose the private entry.
        import traceback
        frames = traceback.extract_tb(error.__traceback__)
        print("Deployment failed:", type(error).__name__, frames[-1].name if frames else "", flush=True)
        raise SystemExit(1)
