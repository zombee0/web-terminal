"""One-time owner initialization using the operator's database connection."""

import argparse
import asyncio

from supabase.config import load_deployment_env, required
from supabase.database import create_pool


async def bootstrap_owner(pool, email):
    if not isinstance(email, str) or not email.strip() or len(email) > 254:
        raise ValueError("Supply the verified Google email of an existing user")
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(793105732)")
        users = await conn.fetch("SELECT id FROM profiles WHERE lower(verified_google_email(id))=$1", email.strip().lower())
        if len(users) != 1:
            raise ValueError("Exactly one existing, verified Google user must match")
        uid = users[0]['id']
        owners = await conn.fetch("SELECT user_id FROM app_roles WHERE role='owner'")
        if owners:
            if any(row['user_id'] == uid for row in owners):
                return False
            raise ValueError("An owner already exists; manage roles in the dashboard")
        if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM app_roles)"):
            raise ValueError("Existing roles require operator recovery, not initialization")
        await conn.execute("INSERT INTO app_roles(user_id,role) VALUES($1,'owner')", uid)
        await conn.execute("""INSERT INTO role_events(actor_id,target_id,previous_role,new_role,source)
            VALUES($1,$1,'user','owner','bootstrap')""", uid)
        return True


async def main(email):
    load_deployment_env()
    pool = await create_pool(required('SUPABASE_DB_URL'), dns_fallback=True)
    try:
        changed = await bootstrap_owner(pool, email)
        print('Initial site owner created' if changed else 'Site owner already initialized')
    finally:
        await pool.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--email', required=True)
    args = parser.parse_args()
    try:
        asyncio.run(main(args.email))
    except Exception as error:
        print('Owner initialization failed:', type(error).__name__)
        raise SystemExit(1)
