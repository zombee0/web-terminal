"""Transactional ownership, quotas, idempotency, and durable lifecycle jobs."""

import json
import uuid

import asyncpg

RESOURCES = {"cpus": 2, "memory_mib": 4096, "disk_size_gb": 10}


def validate_resources(data):
    result = {key: data.get(key, default) for key, default in RESOURCES.items()}
    minimums = {'cpus': 1, 'memory_mib': 512, 'disk_size_gb': 1}
    if any(type(value) is not int or not minimums[key] <= value <= RESOURCES[key]
           for key, value in result.items()):
        raise ValueError('Invalid Box resources')
    return result


def validate_agy_trial(data):
    value = data.get("gemini", False)
    if type(value) is not bool:
        raise ValueError('Invalid Gemini option')
    return value


TERMINAL_PORT = 3001
MAX_PUBLIC_PORTS = 10


def validate_port(value):
    if type(value) is not int or not 1 <= value <= 65535:
        raise ValueError('Invalid port')
    if value == TERMINAL_PORT:
        raise Conflict('Port 3001 is the terminal; it stays behind your sign-in')
    return value


def public_port(row):
    return {"port": row["port"], "url": row["url"], "created_at": row["created_at"].isoformat()}


def box_resources(row):
    return {key: row[key] for key in RESOURCES}


class Conflict(Exception):
    pass


class NotFound(Exception):
    pass


def public_box(row):
    if row is None:
        return None
    return {"id": str(row["id"]), "name": row["name"], "runtime": "cloud",
            "status": row["status"], "resources": box_resources(row), **box_resources(row),
            "gemini": row["agy_trial"],
            "created_at": row["created_at"].isoformat(),
            "access_url": f'/terminal/{row["id"]}/', "url": None,
            "error": row["failure_code"], "busy": row["status"] in {"creating", "starting", "stopping", "deleting"}}


def public_job(row):
    return {"id": str(row["id"]), "box_id": str(row["box_id"]), "action": row["operation"],
            "status": {"queued": "running", "succeeded": "done", "failed": "error"}.get(row["status"], row["status"]),
            "error": row["error_code"], "created_at": row["created_at"].isoformat()}


class Store:
    def __init__(self, pool):
        self.pool = pool

    async def boxes(self, owner):
        return await self.pool.fetch("SELECT * FROM boxes WHERE owner_id=$1 AND deleted_at IS NULL ORDER BY created_at", owner)

    async def box(self, owner, box_id):
        row = await self.pool.fetchrow("SELECT * FROM boxes WHERE id=$1 AND owner_id=$2 AND deleted_at IS NULL", box_id, owner)
        if row is None:
            raise NotFound()
        return row

    async def job(self, owner, job_id):
        row = await self.pool.fetchrow("SELECT * FROM jobs WHERE id=$1 AND owner_id=$2", job_id, owner)
        if row is None:
            raise NotFound()
        return row

    async def jobs(self, owner):
        return await self.pool.fetch("SELECT * FROM jobs WHERE owner_id=$1 ORDER BY created_at DESC LIMIT 50", owner)

    async def create(self, owner, name, key, resources=None, agy_trial=False):
        resources = validate_resources(resources or {})
        try:
            async with self.pool.acquire() as conn, conn.transaction():
                # Serialize a user's creation requests before checking idempotency
                # or inserting. The database trigger also guards direct writes.
                await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,793105733))', str(owner))
                if key:
                    previous = await conn.fetchrow("SELECT * FROM jobs WHERE owner_id=$1 AND idempotency_key=$2", owner, key)
                    if previous:
                        if previous["operation"] != "create":
                            raise Conflict("Idempotency key already used")
                        return previous
                box_id, job_id = uuid.uuid4(), uuid.uuid4()
                await conn.execute("""INSERT INTO boxes(id,owner_id,name,provider_name,status,cpus,memory_mib,disk_size_gb,agy_trial)
                    VALUES($1,$2,$3,$4,'creating',$5,$6,$7,$8)""",
                    box_id, owner, name, "web-terminal-user-" + box_id.hex,
                    resources['cpus'], resources['memory_mib'], resources['disk_size_gb'], bool(agy_trial))
                job = await conn.fetchrow("""INSERT INTO jobs(id,box_id,owner_id,operation,idempotency_key)
                    VALUES($1,$2,$3,'create',$4) RETURNING *""", job_id, box_id, owner, key)
                await conn.execute("""INSERT INTO lifecycle_events(box_id,actor_id,job_id,event,to_state)
                    VALUES($1,$2,$3,'creation_requested','creating')""", box_id, owner, job_id)
                return job
        except asyncpg.CheckViolationError as error:
            if error.constraint_name == 'user_resource_quota':
                raise Conflict('Account quota exceeded: total limit is 2 CPU / 4 GiB. Delete a Box to free its allocation.') from None
            raise
        except asyncpg.UniqueViolationError:
            if key:
                previous = await self.pool.fetchrow("SELECT * FROM jobs WHERE owner_id=$1 AND idempotency_key=$2", owner, key)
                if previous and previous["operation"] == "create":
                    return previous
            raise Conflict("A Box or operation already exists") from None

    async def operate(self, owner, box_id, operation, key):
        if operation not in {"stop", "resume", "delete"}:
            raise ValueError("Unknown operation")
        async with self.pool.acquire() as conn, conn.transaction():
            if key:
                prior = await conn.fetchrow("SELECT * FROM jobs WHERE owner_id=$1 AND idempotency_key=$2", owner, key)
                if prior:
                    if prior["box_id"] != box_id or prior["operation"] != operation:
                        raise Conflict("Idempotency key already used")
                    return prior
            box = await conn.fetchrow("SELECT * FROM boxes WHERE id=$1 AND owner_id=$2 AND deleted_at IS NULL FOR UPDATE", box_id, owner)
            if box is None:
                raise NotFound()
            if key:
                prior = await conn.fetchrow("SELECT * FROM jobs WHERE owner_id=$1 AND idempotency_key=$2", owner, key)
                if prior:
                    if prior["box_id"] != box_id or prior["operation"] != operation:
                        raise Conflict("Idempotency key already used")
                    return prior
            busy = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM jobs WHERE box_id=$1 AND status IN ('queued','running'))", box_id)
            if busy:
                raise Conflict("A lifecycle operation is already running")
            desired = {"stop": "stopped", "resume": "running", "delete": "deleted"}[operation]
            state = {"stop": "stopping", "resume": "starting", "delete": "deleting"}[operation]
            job = await conn.fetchrow("""INSERT INTO jobs(id,box_id,owner_id,operation,idempotency_key)
                VALUES($1,$2,$3,$4,$5) RETURNING *""", uuid.uuid4(), box_id, owner, operation, key)
            await conn.execute("UPDATE boxes SET status=$2,desired_state=$3,failure_code=NULL,updated_at=now() WHERE id=$1", box_id, state, desired)
            await conn.execute("""INSERT INTO lifecycle_events(box_id,actor_id,job_id,event,from_state,to_state)
                VALUES($1,$2,$3,$4,$5,$6)""", box_id, owner, job["id"], operation+"_requested", box["status"], state)
            return job

    async def ports(self, owner, box_id):
        await self.box(owner, box_id)
        return await self.pool.fetch("SELECT * FROM box_ports WHERE box_id=$1 ORDER BY port", box_id)

    async def add_port(self, owner, box_id, port, url):
        async with self.pool.acquire() as conn, conn.transaction():
            # Lock the Box so concurrent requests cannot pass the limit together.
            box = await conn.fetchrow("SELECT id FROM boxes WHERE id=$1 AND owner_id=$2 AND deleted_at IS NULL FOR UPDATE", box_id, owner)
            if box is None:
                raise NotFound()
            existing = await conn.fetchrow("SELECT * FROM box_ports WHERE box_id=$1 AND port=$2", box_id, port)
            if existing:
                return await conn.fetchrow("UPDATE box_ports SET url=$3 WHERE box_id=$1 AND port=$2 RETURNING *", box_id, port, url)
            if await conn.fetchval("SELECT count(*) FROM box_ports WHERE box_id=$1", box_id) >= MAX_PUBLIC_PORTS:
                raise Conflict(f'A Box can expose at most {MAX_PUBLIC_PORTS} ports. Remove one first.')
            return await conn.fetchrow("INSERT INTO box_ports(box_id,port,url) VALUES($1,$2,$3) RETURNING *", box_id, port, url)

    async def remove_port(self, owner, box_id, port):
        await self.box(owner, box_id)
        if await self.pool.fetchval("DELETE FROM box_ports WHERE box_id=$1 AND port=$2 RETURNING port", box_id, port) is None:
            raise NotFound()

    async def claim(self, worker):
        return await self.pool.fetchrow("""WITH candidate AS (
            SELECT id FROM jobs WHERE (status='queued' AND run_after<=now()) OR
            (status='running' AND lease_until<now()) ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1
        ) UPDATE jobs SET status='running',attempts=attempts+1,lease_owner=$1,
          lease_until=now()+interval '120 seconds',updated_at=now()
          WHERE id IN (SELECT id FROM candidate) RETURNING *""", worker)

    async def renew(self, job_id, worker):
        return await self.pool.fetchval("""UPDATE jobs SET lease_until=now()+interval '120 seconds',updated_at=now()
            WHERE id=$1 AND lease_owner=$2 AND status='running' RETURNING true""", job_id, worker)

    async def finish(self, job, worker, status, *, provisioned=False):
        async with self.pool.acquire() as conn, conn.transaction():
            held = await conn.fetchrow("SELECT * FROM jobs WHERE id=$1 AND lease_owner=$2 AND status='running' FOR UPDATE", job["id"], worker)
            if held is None:
                raise Conflict("Job lease was lost")
            previous = await conn.fetchval("SELECT status FROM boxes WHERE id=$1 FOR UPDATE", job["box_id"])
            await conn.execute("""UPDATE boxes SET status=$2,desired_state=$2,provisioned=provisioned OR $3,
                failure_code=NULL,updated_at=now(),deleted_at=CASE WHEN $2='deleted' THEN now() ELSE NULL END
                WHERE id=$1""", job["box_id"], status, provisioned)
            await conn.execute("UPDATE jobs SET status='succeeded',finished_at=now(),updated_at=now(),lease_until=NULL WHERE id=$1", job["id"])
            await conn.execute("""INSERT INTO lifecycle_events(box_id,actor_id,job_id,event,from_state,to_state)
                VALUES($1,$2,$3,'operation_completed',$4,$5)""", job["box_id"], job["owner_id"], job["id"], previous, status)

    async def fail(self, job, worker, category):
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow("SELECT * FROM jobs WHERE id=$1 AND lease_owner=$2 AND status='running' FOR UPDATE", job["id"], worker)
            if not row:
                return
            retry = row["attempts"] < 3
            await conn.execute("""UPDATE jobs SET status=$2,error_code=$3,lease_until=NULL,lease_owner=NULL,
                run_after=now()+interval '30 seconds',updated_at=now(),finished_at=CASE WHEN $2='failed' THEN now() ELSE NULL END
                WHERE id=$1""", job["id"], "queued" if retry else "failed", category)
            if not retry:
                await conn.execute("UPDATE boxes SET status='error',failure_code=$2,updated_at=now() WHERE id=$1 AND deleted_at IS NULL", job["box_id"], category)
            await conn.execute("""INSERT INTO lifecycle_events(box_id,actor_id,job_id,event,detail)
                VALUES($1,$2,$3,$4,$5::jsonb)""", job["box_id"], job["owner_id"], job["id"],
                "operation_retry" if retry else "operation_failed", json.dumps({"code": category}))

    async def events(self, owner, job_id, after=0):
        await self.job(owner, job_id)
        return await self.pool.fetch("""SELECT e.id,e.event,e.to_state,e.occurred_at FROM lifecycle_events e
            JOIN jobs j ON j.id=e.job_id WHERE j.id=$1 AND j.owner_id=$2 AND e.id>$3 ORDER BY e.id LIMIT 100""", job_id, owner, after)
