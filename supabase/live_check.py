"""Live hosted acceptance using temporary fixtures, never a login bypass route.

The fixture creates private DB sessions administratively to exercise the real
gateway and worker. Google consent is a separate, human acceptance step.
Run with make test-supabase-live. Interrupted runs reuse the recorded Box.
"""

import asyncio
import contextlib
import json
import secrets
import shlex
import time
import uuid

import aiohttp

from hangar.boxlite import cloud_api, cloud_tunnel_url, run_checked
from supabase.config import STATE, load_deployment_env, load_private, required, save_private
from supabase.crypto import Crypto, digest
from supabase.database import create_pool
from supabase.preflight import manager

phase = "setup"


def report(message):
    global phase
    phase = message
    print(message, flush=True)


def expect(condition, message):
    if not condition:
        raise RuntimeError(message)


async def main():
    load_deployment_env()
    state = load_private(STATE/"manager.json")
    keys = load_private(STATE/"secrets.json")
    crypto = Crypto(keys["encryption_key"], keys["signing_key"])
    pool = await create_pool(required("SUPABASE_DB_URL"), dns_fallback=True)
    fixture = load_private(STATE/"acceptance.json", {})
    if not fixture:
        fixture = {"users": [str(uuid.uuid4()), str(uuid.uuid4())], "cookies": [secrets.token_urlsafe(32), secrets.token_urlsafe(32)]}
        save_private(STATE/"acceptance.json", fixture)
    owners = [uuid.UUID(value) for value in fixture["users"]]
    for owner, cookie in zip(owners, fixture["cookies"]):
        async with pool.acquire() as conn, conn.transaction():
            # Isolated fixture identities cannot sign in through Google and no
            # password or provider identity is created for them.
            await conn.execute("""INSERT INTO auth.users(id,aud,role,email,created_at,updated_at)
                VALUES($1,'authenticated','authenticated',$2,now(),now()) ON CONFLICT(id) DO NOTHING""",
                owner, "wt-acceptance-"+owner.hex+"@example.invalid")
            await conn.execute("INSERT INTO profiles(id,email) VALUES($1,$2) ON CONFLICT(id) DO NOTHING", owner, "acceptance@example.invalid")
            sid = uuid.uuid4()
            await conn.execute("INSERT INTO auth.sessions(id,user_id,created_at,updated_at,not_after) VALUES($1,$2,now(),now(),now()+interval '2 hours')", sid, owner)
            await conn.execute("""INSERT INTO sessions(id,user_id,auth_session_id,access_token,refresh_token,access_expires_at,expires_at)
                VALUES($1,$2,$3,$4,$4,now()+interval '2 hours',now()+interval '2 hours')
                ON CONFLICT(id) DO UPDATE SET auth_session_id=excluded.auth_session_id,access_expires_at=excluded.access_expires_at,expires_at=excluded.expires_at,revoked_at=NULL""",
                digest(cookie), owner, sid, crypto.encrypt("acceptance-fixture"))

    base = state["entry_url"]
    def headers(index=0):
        cookie = fixture["cookies"][index]
        return {"Cookie": "__Host-wt-session="+cookie, "Origin": base, "X-CSRF-Token": crypto.csrf(cookie)}

    async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=30)) as client:
        async def request(method, path, index=0, data=None):
            async with client.request(method, base+path, headers=headers(index), json=data, allow_redirects=False) as response:
                body = await response.read()
                expect(b"BOXLITE_BOX_AUTH_KEY" not in body and b"provider_id" not in body, "Secret field exposure")
                return response.status, json.loads(body) if response.content_type == "application/json" else body

        async def wait_job(job_id, limit=1500):
            start = time.monotonic()
            last = 0
            while time.monotonic()-start < limit:
                job = await pool.fetchrow("SELECT status,error_code FROM jobs WHERE id=$1", uuid.UUID(job_id))
                if job["status"] == "succeeded":
                    return
                expect(job["status"] != "failed", "Lifecycle job failed")
                if time.monotonic()-last > 30:
                    print("Waiting for lifecycle operation:", job["status"], flush=True)
                    last = time.monotonic()
                await asyncio.sleep(3)
            raise RuntimeError("Lifecycle operation timed out")

        report("Live acceptance: checking session and atomic Box creation")
        status, body = await request("GET", "/api/session")
        expect(status == 200 and body.get("user"), "Fixture session rejected")
        existing = await pool.fetchrow("SELECT * FROM boxes WHERE owner_id=$1 AND deleted_at IS NULL", owners[0])
        if existing is None:
            results = await asyncio.gather(*(request("POST", "/api/boxes", data={"name": "Acceptance terminal", "cpus": 2, "memory_mib": 4096}) for _ in range(5)))
            expect(sum(status == 202 for status, _ in results) == 1, "Quota race allowed multiple Boxes")
            created = next(body for status, body in results if status == 202)
            fixture["job"] = created["job"]["id"]
            fixture["box"] = created["job"]["box_id"]
            save_private(STATE/"acceptance.json", fixture)
        else:
            fixture["box"] = str(existing["id"])
            job = await pool.fetchrow("SELECT * FROM jobs WHERE box_id=$1 ORDER BY created_at DESC LIMIT 1", existing["id"])
            fixture["job"] = str(job["id"])
            if job["status"] == "failed":
                status, body = await request("POST", f"/api/boxes/{fixture['box']}/resume", data={})
                expect(status == 202, "Could not retry fixture provision")
                fixture["job"] = body["job"]["id"]
            save_private(STATE/"acceptance.json", fixture)

        box_id = uuid.UUID(fixture["box"])
        for method, path in [("GET", f"/terminal/{box_id}/"), ("GET", f"/terminal/{box_id}/ws"),
                             ("GET", f"/api/jobs/{fixture['job']}/log"), ("DELETE", f"/api/boxes/{box_id}"),
                             ("POST", f"/api/boxes/{box_id}/stop")]:
            status, _ = await request(method, path, index=1)
            expect(status == 404, "Foreign owner access was allowed")
        report("Live acceptance: ownership denial passed; waiting for guest provision")
        await wait_job(fixture["job"])
        row = await pool.fetchrow("SELECT * FROM boxes WHERE id=$1", box_id)
        expect((row["cpus"], row["memory_mib"], row["disk_size_gb"]) == (2, 4096, 10), "Unexpected resources")
        provider = await asyncio.to_thread(cloud_api, "GET", "/api/box/"+row["provider_id"])
        expect((provider.get("cpu"), provider.get("memory"), provider.get("disk")) == (2, 4, 10), "Provider resource limits differ")
        upstream = await asyncio.to_thread(cloud_tunnel_url, row["provider_id"])
        async with client.get(upstream.rstrip("/")+"/") as response:
            expect(response.status == 403, "Direct terminal access was not denied")
        async with client.get(upstream.rstrip("/")+"/ws", headers={"Origin": upstream.rstrip("/")}) as response:
            expect(response.status == 403, "Direct WebSocket access was not denied")
        report("Live acceptance: direct URL denied; checking authenticated PTY")
        async with client.ws_connect(base+f"/terminal/{box_id}/ws", headers=headers(), heartbeat=20) as ws:
            command = "printf 'ACCEPTANCE_UID='; id -u; printf 'ACCEPTANCE_PRIV='; awk '/NoNewPrivs/{print $2}' /proc/self/status; for c in tmux codex claude agent; do command -v $c >/dev/null || echo MISSING_$c; done; test ! -r /etc/web-terminal/gateway.json && echo CONFIG_PROTECTED; env | cut -d= -f1 | grep -E 'KEY|TOKEN|SUPABASE' && echo SECRET_ENV; printf '\\101CCEPTANCE_DONE\\n'\n"
            await ws.send_str(command)
            output = ""
            for _ in range(80):
                message = await asyncio.wait_for(ws.receive(), 10)
                if message.type not in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                    break
                output += message.data.decode(errors="replace") if isinstance(message.data, bytes) else message.data
                if "\r\nACCEPTANCE_DONE" in output:
                    break
            expect("ACCEPTANCE_UID=1000" in output, "PTY did not run as UID 1000")
            expect("ACCEPTANCE_PRIV=1" in output, "PTY permits privilege escalation")
            expect("\r\nCONFIG_PROTECTED" in output, "Guest config is readable")
            expect(not any("\r\nMISSING_"+name in output for name in ("tmux", "codex", "claude", "agent")), "Guest CLI missing")
            expect("\r\nSECRET_ENV" not in output, "Secret environment inherited")
            await pool.execute("UPDATE sessions SET revoked_at=now() WHERE id=$1", digest(fixture["cookies"][0]))
            while not ws.closed:
                message = await asyncio.wait_for(ws.receive(), 20)
                if message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                    break
            expect(message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED), "Revoked terminal stayed open")
        await pool.execute("UPDATE sessions SET revoked_at=NULL WHERE id=$1", digest(fixture["cookies"][0]))
        report("Live acceptance: PTY and revocation passed; checking stop/resume/delete")
        for operation in ("stop", "resume", "delete"):
            path = f"/api/boxes/{box_id}"+("" if operation == "delete" else "/"+operation)
            status, body = await request("DELETE" if operation == "delete" else "POST", path, data={})
            expect(status == 202, "Lifecycle request rejected")
            await wait_job(body["job"]["id"], 300)
            report("Live acceptance: "+operation+" passed")
        status, _ = await request("POST", "/auth/logout", data={})
        expect(status == 200, "Logout failed")
        status, _ = await request("GET", "/api/boxes")
        expect(status == 401, "Logged-out cookie retained access")
        # Delete only this run's fixture records and identities, after provider
        # deletion is confirmed. Real users and their history remain untouched.
        async with pool.acquire() as conn, conn.transaction():
            for owner in owners:
                await conn.execute("DELETE FROM lifecycle_events WHERE box_id IN (SELECT id FROM boxes WHERE owner_id=$1)", owner)
                await conn.execute("DELETE FROM jobs WHERE owner_id=$1", owner)
                await conn.execute("DELETE FROM boxes WHERE owner_id=$1", owner)
                await conn.execute("DELETE FROM sessions WHERE user_id=$1", owner)
                await conn.execute("DELETE FROM login_events WHERE user_id=$1", owner)
                await conn.execute("DELETE FROM profiles WHERE id=$1", owner)
                await conn.execute("DELETE FROM auth.sessions WHERE user_id=$1", owner)
                await conn.execute("DELETE FROM auth.users WHERE id=$1", owner)
        (STATE/"acceptance.json").unlink()
        save_private(STATE/"acceptance-receipt.json", {"passed": True, "at": int(time.time()), "checks": ["quota_race", "provider_resources", "ownership", "direct_denial", "PTY_uid", "no_new_privs", "secret_isolation", "revocation", "stop", "resume", "delete"]})
        report("Live acceptance passed; temporary Boxes and identities removed")
    await pool.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        # Do not render network exceptions containing secret upstream URLs.
        print("Live acceptance failed:", phase, type(error).__name__, flush=True)
        raise SystemExit(1)
