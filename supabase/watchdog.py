"""Install a once-per-minute recovery check with its token in Supabase Vault."""

import asyncio

from supabase.config import STATE, load_deployment_env, load_private, required
from supabase.database import create_pool


async def install():
    load_deployment_env()
    pool = await create_pool(required("SUPABASE_DB_URL"), dns_fallback=True)
    token = load_private(STATE/"secrets.json")["relay_token"]
    try:
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_cron")
            await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_net WITH SCHEMA extensions")
            # Supabase Vault is provided by the project, and its decrypted view
            # is never granted to the runtime role or the Data API roles.
            secret = await conn.fetchval("SELECT id FROM vault.secrets WHERE name='web_terminal_watchdog'")
            if secret:
                await conn.execute("SELECT vault.update_secret($1,$2)", secret, token)
            else:
                await conn.execute("SELECT vault.create_secret($1,'web_terminal_watchdog','Private management recovery token')", token)
            url = required("SUPABASE_URL")+"/functions/v1/boxlite-relay/watchdog"
            command = """SELECT net.http_post(
                url := '%s', body := '{}'::jsonb, timeout_milliseconds := 90000,
                headers := jsonb_build_object('Content-Type','application/json','Authorization','Bearer ' ||
                    (SELECT decrypted_secret FROM vault.decrypted_secrets WHERE name='web_terminal_watchdog'))
                );""" % url.replace("'", "''")
            await conn.fetchval("SELECT cron.schedule('web-terminal-watchdog','* * * * *',$1)", command)
        print("Management recovery scheduled every minute; token stored in Vault", flush=True)
    finally:
        await pool.close()


if __name__ == "__main__":
    try:
        asyncio.run(install())
    except Exception as error:
        print("Recovery schedule failed:", type(error).__name__, flush=True)
        raise SystemExit(1)
