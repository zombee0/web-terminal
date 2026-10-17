"""Durable lifecycle worker. Every provider identifier remains server-side."""

import asyncio
import contextlib
import time
import traceback
import uuid

from hangar.boxlite import CloudAPIError, cloud_api, cloud_create_payload, create_runtime, delete_box, ensure_stopped, normalize_status
from hangar.provision import PROTECTED_INIT, provision_web_terminal, resume_web_terminal
from supabase.store import box_resources, Store


class Worker:
    def __init__(self, pool, crypto):
        self.pool, self.crypto = pool, crypto
        self.store, self.id = Store(pool), uuid.uuid4()
        self.runtime = create_runtime("cloud")
        self.last_heartbeat = 0.0

    async def resolve(self, row, *, create=False):
        if row["provider_id"]:
            return await self.runtime.get(row["provider_id"])
        # Recover a creation whose provider response or DB write was interrupted.
        try:
            found = await asyncio.to_thread(cloud_api, "GET", "/api/v1/boxes/"+row["provider_name"])
            provider_id = found.get("box_id") or found.get("id")
            if not provider_id:
                raise RuntimeError("Provider lookup returned no ID")
        except CloudAPIError as error:
            if error.status != 404:
                raise
            provider_id = None
        if provider_id is None and create:
            payload = cloud_create_payload(row["provider_name"], box_resources(row), inbound=True)
            payload.update(entrypoint=["/usr/local/bin/python3", "-u", "-c", PROTECTED_INIT], cmd=[], auto_delete=0)
            created = await asyncio.to_thread(cloud_api, "POST", "/api/v1/boxes", payload, timeout=90)
            provider_id = created.get("box_id") or created.get("id")
            if not provider_id:
                raise RuntimeError("Provider creation returned no ID")
        if provider_id is None:
            return None
        await self.pool.execute("UPDATE boxes SET provider_id=$2,updated_at=now() WHERE id=$1", row["id"], provider_id)
        return await self.runtime.get(provider_id)

    async def execute(self, job):
        row = await self.pool.fetchrow("SELECT * FROM boxes WHERE id=$1", job["box_id"])
        operation = job["operation"]
        box = await self.resolve(row, create=operation == "create")
        if operation == "delete":
            if box is not None:
                await delete_box(self.runtime, box.id, "cloud")
                for _ in range(90):
                    if await self.runtime.get(box.id) is None:
                        break
                    await asyncio.sleep(1)
                else:
                    raise RuntimeError("Deletion is not yet confirmed")
            await self.store.finish(job, self.id, "deleted")
            return
        if box is None:
            raise RuntimeError("Provider Box is missing")
        if operation == "stop":
            await ensure_stopped(self.runtime, box, "cloud")
            await self.store.finish(job, self.id, "stopped")
            return
        options = {"gateway_config": self.crypto.guest_config(row["id"]), "publish_url": False, "log": lambda _: None}
        if row["provisioned"]:
            await resume_web_terminal(self.runtime, box, "cloud", install_clis=False, **options)
        else:
            await provision_web_terminal(self.runtime, box, "cloud", **options)
        await self.store.finish(job, self.id, "running", provisioned=True)

    async def heartbeat(self, job, work):
        while not work.done():
            await asyncio.sleep(25)
            try:
                held = await self.store.renew(job["id"], self.id)
            except Exception:
                held = False
            if not held:
                work.cancel()
                return

    async def process(self, job):
        # A database session lock prevents two live workers from operating on
        # the same Box, even when a lease expires during a slow provider call.
        async with self.pool.acquire() as conn:
            key = int.from_bytes(job["box_id"].bytes[:8], "big", signed=True)
            if not await conn.fetchval("SELECT pg_try_advisory_lock($1)", key):
                return
            work = asyncio.create_task(self.execute(job))
            heartbeat = asyncio.create_task(self.heartbeat(job, work))
            try:
                await work
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
            except Exception as error:
                frames = traceback.extract_tb(error.__traceback__)
                print("worker: operation failed", type(error).__name__, frames[-1].name if frames else "", flush=True)
                await self.store.fail(job, self.id, "provider_operation_failed")
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
                await conn.execute("SELECT pg_advisory_unlock($1)", key)

    async def maintain(self):
        await self.pool.execute("SELECT ingest_auth_audit()")
        await self.pool.execute("DELETE FROM oauth_flows WHERE expires_at<now()")
        await self.pool.execute("DELETE FROM sessions WHERE expires_at<now()-interval '7 days'")
        rows = await self.pool.fetch("""SELECT b.* FROM boxes b WHERE deleted_at IS NULL
            AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.box_id=b.id AND j.status IN ('queued','running'))""")
        if not rows:
            return
        for row in rows:
            if not row["provider_id"]:
                continue
            info = await self.runtime.get_info(row["provider_id"])
            observed = normalize_status(info, "missing")
            if observed not in {"running", "stopped", "missing"} or observed == row["status"]:
                continue
            # Missing inventory is recorded as an error, never as deletion;
            # quota is released only by a successful explicit delete job.
            status = "error" if observed == "missing" or not row["provisioned"] else observed
            async with self.pool.acquire() as conn, conn.transaction():
                changed = await conn.fetchval("""UPDATE boxes b SET status=$2,failure_code=$3,updated_at=now()
                    WHERE id=$1 AND status=$4 AND deleted_at IS NULL AND NOT EXISTS
                    (SELECT 1 FROM jobs j WHERE j.box_id=b.id AND j.status IN ('queued','running')) RETURNING true""",
                    row["id"], status, "provider_missing" if observed == "missing" else None, row["status"])
                if changed:
                    await conn.execute("INSERT INTO lifecycle_events(box_id,event,from_state,to_state) VALUES($1,'provider_state_observed',$2,$3)", row["id"], row["status"], status)

    async def run(self):
        next_maintenance = 0.0
        while True:
            try:
                self.last_heartbeat = time.monotonic()
                if time.monotonic() >= next_maintenance:
                    await self.maintain()
                    next_maintenance = time.monotonic()+60
                job = await self.store.claim(self.id)
                if job:
                    await self.process(job)
                else:
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never log SDK exceptions: they can contain upstream URLs.
                print("worker: retrying after dependency failure", flush=True)
                await asyncio.sleep(5)
