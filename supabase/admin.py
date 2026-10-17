"""Read-only Box inventory and owner-controlled application membership."""

import json
import uuid
from datetime import datetime

from aiohttp import web
import asyncpg
from supabase.store import Conflict, NotFound


def response(data):
    def encode(value):
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, uuid.UUID):
            return str(value)
        raise TypeError()
    return web.json_response(data, dumps=lambda value: json.dumps(value, default=encode))


class AdminStore:
    def __init__(self, pool):
        self.pool = pool

    async def role(self, user_id):
        return await self.pool.fetchval('SELECT application_role($1)', user_id)

    async def allowed(self, user_id):
        return await self.role(user_id) in {'admin', 'owner'}

    async def lookup(self, email):
        if not isinstance(email, str) or not email.strip() or len(email) > 254:
            raise ValueError()
        rows = await self.pool.fetch("""SELECT p.id AS user_id,verified_google_email(p.id) AS email,p.display_name,
            coalesce(r.role,'user') AS role FROM profiles p LEFT JOIN app_roles r ON r.user_id=p.id
            WHERE lower(verified_google_email(p.id))=$1 LIMIT 2""", email.strip().lower())
        if len(rows) != 1:
            raise NotFound()
        return dict(rows[0])

    async def members(self):
        rows = await self.pool.fetch("""SELECT r.user_id,r.role,r.updated_at,p.display_name,
            coalesce(verified_google_email(p.id),p.email) AS email,
            verified_google_email(p.id) IS NOT NULL AS verified
            FROM app_roles r JOIN profiles p ON p.id=r.user_id ORDER BY r.role DESC,r.updated_at,r.user_id""")
        return {'items': [dict(row) for row in rows]}

    async def history(self, page):
        page = int(page)
        if not 1 <= page <= 1000000:
            raise ValueError()
        async with self.pool.acquire() as conn, conn.transaction(isolation='repeatable_read', readonly=True):
            total = await conn.fetchval('SELECT count(*) FROM role_events')
            rows = await conn.fetch("""SELECT e.*,a.email AS actor_email,t.email AS target_email
                FROM role_events e LEFT JOIN profiles a ON a.id=e.actor_id LEFT JOIN profiles t ON t.id=e.target_id
                ORDER BY e.id DESC LIMIT 25 OFFSET $1""", (page-1)*25)
        return {'items': [dict(row) for row in rows], 'page': page, 'page_size': 25, 'total': total}

    async def change(self, session_id, target, role, expected):
        if role not in ('user', 'admin', 'owner') or expected not in ('user', 'admin', 'owner'):
            raise ValueError()
        try:
            return await self.pool.fetchval('SELECT change_application_role($1,$2,$3,$4)', session_id, target, role, expected)
        except asyncpg.InsufficientPrivilegeError:
            raise web.HTTPForbidden(text='Site owner access required') from None
        except asyncpg.NoDataFoundError:
            raise NotFound() from None
        except asyncpg.InvalidParameterValueError:
            raise ValueError() from None
        except asyncpg.RaiseError as error:
            raise Conflict(str(error)) from None

    async def overview(self):
        async with self.pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
            boxes = await conn.fetch("SELECT status,count(*) AS count FROM boxes GROUP BY status")
            allocation = await conn.fetchrow("""SELECT coalesce(sum(cpus),0) AS cpus,
                coalesce(sum(memory_mib),0) AS memory_mib,coalesce(sum(disk_size_gb),0) AS disk_size_gb
                FROM boxes WHERE deleted_at IS NULL""")
        return {"boxes": {r['status']: r['count'] for r in boxes}, "allocated": dict(allocation)}

    async def listing(self, section, query):
        # Only these explicit columns can reach the browser. No provider URLs,
        # token tables, raw audit metadata, or BoxLite identifiers are exposed.
        definitions = {
            "boxes": ("""b.id,b.name,b.status,b.cpus,b.memory_mib,b.disk_size_gb,
                b.created_at,b.updated_at,b.deleted_at,b.failure_code,p.email AS owner_email,p.display_name AS owner_name""",
                "boxes b JOIN profiles p ON p.id=b.owner_id",
                "concat_ws(' ',b.id::text,b.name,p.email,p.display_name)", "b.status", "b.created_at DESC,b.id DESC",
                {"creating", "starting", "running", "stopping", "stopped", "deleting", "error", "deleted"}),
        }
        if section not in definitions:
            raise web.HTTPNotFound()
        columns, tables, searchable, status_column, order, statuses = definitions[section]
        search = query.get("search", "").strip()
        status = query.get("status", "")
        page = int(query.get("page", "1"))
        if len(search) > 120 or (status and status not in statuses) or not 1 <= page <= 1000000:
            raise ValueError()
        # Treat SQL LIKE wildcard characters as literal search text.
        pattern = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" if search else ""
        where = f"WHERE ($1::text='' OR {searchable} ILIKE $1) AND ($2::text='' OR {status_column}=$2)"
        size = 25
        async with self.pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
            count = await conn.fetchval(f"SELECT count(*) FROM {tables} {where}", pattern, status)
            rows = await conn.fetch(f"SELECT {columns} FROM {tables} {where} ORDER BY {order} LIMIT $3 OFFSET $4",
                                    pattern, status, size, (page-1)*size)
        return {"items": [dict(row) for row in rows], "total": count, "page": page, "page_size": size}
