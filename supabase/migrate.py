"""Apply reviewed migrations and create a restricted runtime database identity."""

import asyncio
import hashlib
import secrets
import urllib.parse

from supabase.config import ROOT, STATE, load_deployment_env, load_private, project_ref, required, save_private
from supabase.crypto import new_keys
from supabase.database import create_pool


async def migrate():
    load_deployment_env()
    path = STATE / "secrets.json"
    saved = load_private(path)
    if saved is None:
        saved = {**new_keys(), "database_password": secrets.token_urlsafe(48)}
        save_private(path, saved)
    pool = await create_pool(required("SUPABASE_DB_URL"), dns_fallback=True)
    try:
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(793105731)")
            await conn.execute("CREATE SCHEMA IF NOT EXISTS web_terminal")
            await conn.execute("""CREATE TABLE IF NOT EXISTS web_terminal.schema_migrations (
                name text PRIMARY KEY, checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())""")
            for migration in sorted((ROOT / "supabase" / "migrations").glob("*.sql")):
                sql = migration.read_text()
                checksum = hashlib.sha256(sql.encode()).hexdigest()
                previous = await conn.fetchval("SELECT checksum FROM web_terminal.schema_migrations WHERE name=$1", migration.name)
                if previous and previous != checksum:
                    raise RuntimeError("An applied migration changed; add a new migration")
                if previous:
                    continue
                await conn.execute(sql)
                await conn.execute("INSERT INTO web_terminal.schema_migrations(name,checksum) VALUES($1,$2)", migration.name, checksum)
                print("Applied migration:", migration.name, flush=True)
            role = "web_terminal_runtime"
            exists = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=$1)", role)
            password = saved["database_password"].replace("'", "''")
            if not exists:
                await conn.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{password}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS")
            await conn.execute(f"GRANT USAGE ON SCHEMA web_terminal TO {role}")
            tables = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname='web_terminal' AND tablename<>'schema_migrations'")
            for item in tables:
                table = item["tablename"]
                if table in {"admin_users", "app_roles", "role_events"}:
                    await conn.execute(f'REVOKE ALL ON web_terminal."{table}" FROM {role}')
                    await conn.execute(f'GRANT SELECT ON web_terminal."{table}" TO {role}')
                    if not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM pg_policies WHERE schemaname='web_terminal' AND tablename=$1 AND policyname='admin_read')", table):
                        await conn.execute(f'CREATE POLICY admin_read ON web_terminal."{table}" FOR SELECT TO {role} USING(true)')
                    continue
                await conn.execute(f'GRANT SELECT,INSERT,UPDATE,DELETE ON web_terminal."{table}" TO {role}')
                if not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM pg_policies WHERE schemaname='web_terminal' AND tablename=$1 AND policyname='runtime_access')", table):
                    await conn.execute(f'CREATE POLICY runtime_access ON web_terminal."{table}" TO {role} USING(true) WITH CHECK(true)')
            await conn.execute(f"GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA web_terminal TO {role}")
            await conn.execute(f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA web_terminal TO {role}")
            await conn.execute(f"ALTER ROLE {role} SET search_path=web_terminal,pg_catalog")
    finally:
        await pool.close()
    original = urllib.parse.urlsplit(required("SUPABASE_DB_URL"))
    username = "web_terminal_runtime." + project_ref()
    netloc = urllib.parse.quote(username, safe="")+":"+urllib.parse.quote(saved["database_password"], safe="")+"@"+original.hostname+":"+str(original.port or 5432)
    saved["database_url"] = urllib.parse.urlunsplit(original._replace(netloc=netloc))
    save_private(path, saved)
    runtime = await create_pool(saved["database_url"], dns_fallback=True)
    try:
        async with runtime.acquire() as conn:
            assert await conn.fetchval("SELECT current_user") == "web_terminal_runtime"
            assert await conn.fetchval("SELECT count(*) FROM boxes") is not None
            assert not await conn.fetchval("SELECT has_schema_privilege(current_user,'web_terminal','CREATE')")
            assert not await conn.fetchval("SELECT has_schema_privilege(current_user,'auth','USAGE')")
            assert not await conn.fetchval("SELECT auth_session_valid('00000000-0000-0000-0000-000000000000','00000000-0000-0000-0000-000000000000')")
        print("Restricted runtime database connection verified", flush=True)
    finally:
        await runtime.close()


if __name__ == "__main__":
    try:
        asyncio.run(migrate())
    except Exception as error:
        print("Migration failed:", type(error).__name__, flush=True)
        raise SystemExit(1)
