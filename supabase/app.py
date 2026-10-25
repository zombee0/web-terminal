"""Authenticated control plane and terminal WebSocket gateway."""

import asyncio
import contextlib
import json
import logging
import os
import pathlib
import re
import time
import uuid
from urllib.parse import urlencode, urlsplit

import aiohttp
from aiohttp import web

from hangar.boxlite import close_cloud_tunnel, cloud_tunnel_url
from supabase.admin import AdminStore, response as admin_response
from supabase.auth import Auth, AuthenticationError, AuthProviderError
from supabase.config import ROOT, required
from supabase.crypto import Crypto
from supabase.database import create_pool
from supabase.store import (Conflict, NotFound, Store, public_box, public_job, public_port, validate_agy_trial,
                            validate_port, validate_resources)
from supabase.worker import Worker


def identifier(value):
    try:
        return uuid.UUID(value)
    except ValueError:
        raise web.HTTPNotFound() from None


@web.middleware
async def safe_errors(request, handler):
    try:
        response = await handler(request)
    except AuthenticationError:
        if request.path in {"/admin", "/admin/"} or (request.path.startswith("/terminal/") and not request.path.endswith("/ws")):
            response = web.HTTPFound("/auth/login?"+urlencode({"next": request.path}))
        else:
            response = web.json_response({"error": "Sign in with Google to continue"}, status=401)
    except NotFound:
        response = web.json_response({"error": "Resource not found"}, status=404)
    except Conflict as error:
        response = web.json_response({"error": str(error)}, status=409)
    except web.HTTPException as error:
        response = error
    except (ValueError, json.JSONDecodeError):
        response = web.json_response({"error": "Invalid request"}, status=400)
    except Exception:
        response = web.json_response({"error": "Service temporarily unavailable"}, status=503)
    if isinstance(response, web.HTTPException):
        raise response
    return response


async def security_headers(request, response):
    response.headers.update({
        "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
        "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; font-src 'self' data: https://fonts.gstatic.com; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    })


class Gateway:
    def __init__(self, pool, crypto, client, config):
        self.pool, self.crypto, self.client = pool, crypto, client
        self.store = Store(pool)
        self.admin_store = AdminStore(pool)
        self.public_url = config["public_url"].rstrip("/")
        self.auth = Auth(pool, crypto, client, config["supabase_url"], config["publishable_key"], self.public_url)
        self.sockets = set()
        self.connection_counts = {}
        self.login_times = []

    async def login(self, request):
        now = time.monotonic()
        self.login_times = [stamp for stamp in self.login_times if stamp > now-60]
        if len(self.login_times) >= 120:
            raise web.HTTPTooManyRequests()
        self.login_times.append(now)
        return await self.auth.start(request)

    async def session(self, request):
        try:
            session = await self.auth.session(request)
        except AuthenticationError:
            return web.json_response({"user": None})
        role = await self.admin_store.role(session['user_id'])
        return web.json_response({"user": {"email": session["email"], "name": session["display_name"],
            "role": role, "is_admin": role in {'admin', 'owner'}}, "csrf": session["csrf"]})

    async def administrator(self, request):
        session = await self.auth.session(request)
        if not await self.admin_store.allowed(session["user_id"]):
            raise web.HTTPForbidden(text="Administrator access required")
        return session

    async def admin_page(self, request):
        await self.administrator(request)
        return web.Response(text=(ROOT/"supabase/admin.html").read_text(), content_type="text/html")

    async def admin_script(self, request):
        await self.administrator(request)
        return web.Response(text=(ROOT/"supabase/admin.js").read_text(), content_type="application/javascript")

    async def admin_overview(self, request):
        session = await self.administrator(request)
        data = await self.admin_store.overview()
        data["viewer"] = {"name": session["display_name"], "email": session["email"],
                          "role": await self.admin_store.role(session['user_id'])}
        return admin_response(data)

    async def admin_list(self, request):
        await self.administrator(request)
        return admin_response(await self.admin_store.listing("boxes", request.query))

    async def site_owner(self, request, *, mutation=False):
        session = await (self.mutation(request) if mutation else self.auth.session(request))
        if await self.admin_store.role(session['user_id']) != 'owner':
            raise web.HTTPForbidden(text='Site owner access required')
        return session

    async def admin_members(self, request):
        await self.site_owner(request)
        return admin_response(await self.admin_store.members())

    async def admin_lookup(self, request):
        await self.site_owner(request, mutation=True)
        data = await request.json()
        if not isinstance(data, dict) or set(data) != {'email'}:
            raise ValueError()
        return admin_response(await self.admin_store.lookup(data['email']))

    async def admin_change_role(self, request):
        session = await self.site_owner(request, mutation=True)
        data = await request.json()
        if not isinstance(data, dict) or set(data) != {'role', 'expected_role'}:
            raise ValueError()
        role = await self.admin_store.change(session['id'], identifier(request.match_info['user']),
                                             data['role'], data['expected_role'])
        return web.json_response({'role': role})

    async def admin_role_history(self, request):
        await self.site_owner(request)
        return admin_response(await self.admin_store.history(request.query.get('page', '1')))

    async def mutation(self, request):
        session = await self.auth.session(request)
        self.auth.require_csrf(request, session)
        return session

    async def logout(self, request):
        session = await self.mutation(request)
        response = await self.auth.logout(request, session)
        await self.close_sockets(session_id=session["id"])
        return response

    async def status(self, request):
        return web.json_response({"cloud": {"ready": True}, "mode": "supabase"})

    async def boxes(self, request):
        session = await self.auth.session(request)
        return web.json_response({"boxes": [public_box(row) for row in await self.store.boxes(session["user_id"])]})

    def idempotency(self, request):
        key = request.headers.get("Idempotency-Key")
        if key and not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", key):
            raise ValueError()
        return key

    async def create(self, request):
        session = await self.mutation(request)
        data = await request.json()
        if not isinstance(data, dict) or data.get("runtime", "cloud") != "cloud":
            raise ValueError()
        name = str(data.get("name") or "My terminal").strip()
        if not name or len(name) > 80 or any(ord(char) < 32 for char in name):
            raise ValueError()
        job = await self.store.create(session["user_id"], name, self.idempotency(request), validate_resources(data),
                                      agy_trial=validate_agy_trial(data))
        return web.json_response({"job": public_job(job)}, status=202)

    async def operate(self, request):
        session = await self.mutation(request)
        box_id = identifier(request.match_info["box"])
        operation = "delete" if request.method == "DELETE" else request.match_info["operation"]
        job = await self.store.operate(session["user_id"], box_id, operation, self.idempotency(request))
        await self.close_sockets(box_id=box_id)
        return web.json_response({"job": public_job(job)}, status=202)

    async def jobs(self, request):
        session = await self.auth.session(request)
        return web.json_response({"jobs": [public_job(row) for row in await self.store.jobs(session["user_id"])]})

    async def events(self, request):
        session = await self.auth.session(request)
        job_id = identifier(request.match_info["job"])
        await self.store.job(session["user_id"], job_id)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "X-Accel-Buffering": "no"})
        await response.prepare(request)
        cursor = 0
        try:
            while True:
                session = await self.auth.session(request)
                rows = await self.store.events(session["user_id"], job_id, cursor)
                for row in rows:
                    cursor = row["id"]
                    await response.write(("data: "+json.dumps({"line": row["event"]+" "+(row["to_state"] or "")})+"\n\n").encode())
                job = await self.store.job(session["user_id"], job_id)
                if job["status"] in {"succeeded", "failed"}:
                    await response.write(("event: end\ndata: "+json.dumps(public_job(job))+"\n\n").encode())
                    break
                await response.write(b": keepalive\n\n")
                await asyncio.sleep(3)
        except (AuthenticationError, ConnectionError, asyncio.CancelledError):
            pass
        except Exception:
            pass
        return response

    async def ports(self, request):
        session = await self.auth.session(request)
        rows = await self.store.ports(session["user_id"], identifier(request.match_info["box"]))
        return web.json_response({"ports": [public_port(row) for row in rows]})

    async def expose_port(self, request):
        session = await self.mutation(request)
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError()
        port = validate_port(data.get("port"))
        row = await self.store.box(session["user_id"], identifier(request.match_info["box"]))
        if row["status"] != "running" or not row["provider_id"]:
            raise Conflict("Start this Box before exposing a port")
        # Anyone with this URI reaches the guest port: no sign-in, by the owner's choice.
        url = await asyncio.to_thread(cloud_tunnel_url, row["provider_id"], port)
        if urlsplit(url).scheme != "https":
            raise RuntimeError("Invalid tunnel URI")
        try:
            saved = await self.store.add_port(session["user_id"], row["id"], port, url)
        except (Conflict, NotFound):
            # Refused ports are always new, so their tunnel must not stay open unlisted.
            await asyncio.to_thread(close_cloud_tunnel, row["provider_id"], port)
            raise
        return web.json_response({"port": public_port(saved)}, status=201)

    async def close_port(self, request):
        session = await self.mutation(request)
        port = validate_port(int(request.match_info["port"]))
        row = await self.store.box(session["user_id"], identifier(request.match_info["box"]))
        if row["provider_id"]:
            await asyncio.to_thread(close_cloud_tunnel, row["provider_id"], port)
        await self.store.remove_port(session["user_id"], row["id"], port)
        return web.json_response({"closed": port})

    async def owned_running(self, request):
        session = await self.auth.session(request)
        row = await self.store.box(session["user_id"], identifier(request.match_info["box"]))
        if row["status"] != "running" or not row["provisioned"]:
            raise Conflict("Start this Box before entering its terminal")
        return session, row

    async def terminal(self, request):
        await self.owned_running(request)
        return web.Response(text=(ROOT/"supabase/terminal.html").read_text(), content_type="text/html")

    async def close_sockets(self, *, session_id=None, box_id=None):
        for websocket, session, box in list(self.sockets):
            if (session_id and session_id == session) or (box_id and box_id == box):
                await websocket.close(code=1008, message=b"Access ended")

    async def websocket(self, request):
        if request.headers.get("Origin") != self.public_url or request.query_string:
            raise web.HTTPForbidden()
        session, row = await self.owned_running(request)
        box_id = row["id"]
        count = self.connection_counts.get(box_id, 0)
        if count >= 4:
            raise web.HTTPTooManyRequests()
        # No await between checking and reserving: pending handshakes consume
        # capacity too. This gateway runs on one event loop in one process.
        self.connection_counts[box_id] = count + 1
        try:
            return await self.proxy_websocket(request, session, row)
        finally:
            remaining = self.connection_counts[box_id] - 1
            if remaining:
                self.connection_counts[box_id] = remaining
            else:
                del self.connection_counts[box_id]

    async def proxy_websocket(self, request, session, row):
        url = await asyncio.to_thread(cloud_tunnel_url, row["provider_id"])
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.fragment or parsed.query:
            raise RuntimeError("Invalid upstream URI")
        # No browser headers, cookies, URLs, or credentials cross this boundary.
        async with self.client.ws_connect(url.rstrip("/")+"/ws", headers={
            "X-Web-Terminal-Grant": self.crypto.grant(row["id"]), "Origin": url.rstrip("/")},
            max_msg_size=1024*1024, heartbeat=25, autoping=True) as upstream:
            # Recheck after the slow upstream handshake, before admitting a client.
            await self.owned_running(request)
            downstream = web.WebSocketResponse(max_msg_size=1024*1024, heartbeat=25)
            await downstream.prepare(request)
            entry = (downstream, session["id"], row["id"])
            self.sockets.add(entry)

            async def pump(source, destination):
                async for message in source:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        await destination.send_str(message.data)
                    elif message.type == aiohttp.WSMsgType.BINARY:
                        await destination.send_bytes(message.data)
                    elif message.type == aiohttp.WSMsgType.ERROR:
                        break

            async def revalidate():
                while True:
                    await asyncio.sleep(10)
                    await self.owned_running(request)

            tasks = [asyncio.create_task(pump(downstream, upstream)), asyncio.create_task(pump(upstream, downstream)), asyncio.create_task(revalidate())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                self.sockets.discard(entry)
                await downstream.close(code=1000)
            return downstream

    async def home(self, request):
        source = (ROOT/"hangar/control.html").read_text()
        source = source.replace('<script>', '<script src="/hosted.js"></script>\n<script>', 1)
        source = source.replace('id="btn-local" class="active"', 'id="btn-local" disabled')
        source = source.replace('id="btn-cloud"', 'id="btn-cloud" class="active"')
        source = source.replace('<h2>03  FLEET</h2>', '<h2>03  MY FLEET</h2>')
        source = re.sub(r"<footer>.*?</footer>", "<footer>GOOGLE SIGN-IN · 2 CPU / 4 GiB PER ACCOUNT · UP TO 10 GB DISK PER BOX</footer>", source)
        return web.Response(text=source, content_type="text/html")

    async def script(self, request):
        return web.Response(text=(ROOT/"supabase/hosted.js").read_text(), content_type="application/javascript")


def application(gateway):
    app = web.Application(middlewares=[safe_errors], client_max_size=8192)
    app.on_response_prepare.append(security_headers)
    app.add_routes([
        web.get("/", gateway.home), web.get("/hosted.js", gateway.script),
        web.get("/admin", gateway.admin_page), web.get("/admin/", gateway.admin_page), web.get("/admin.js", gateway.admin_script),
        web.get("/api/admin/overview", gateway.admin_overview),
        web.get("/api/admin/boxes", gateway.admin_list),
        web.get('/api/admin/members', gateway.admin_members),
        web.post('/api/admin/members/lookup', gateway.admin_lookup),
        web.put('/api/admin/members/{user}', gateway.admin_change_role),
        web.get('/api/admin/role-events', gateway.admin_role_history),
        web.get("/api/status", gateway.status), web.get("/api/session", gateway.session),
        web.get("/auth/login", gateway.login), web.get("/auth/callback", gateway.auth.callback),
        web.post("/auth/logout", gateway.logout), web.get("/api/boxes", gateway.boxes),
        web.post("/api/boxes", gateway.create), web.delete("/api/boxes/{box}", gateway.operate),
        web.post("/api/boxes/{box}/{operation:stop|resume}", gateway.operate),
        web.get("/api/boxes/{box}/ports", gateway.ports), web.post("/api/boxes/{box}/ports", gateway.expose_port),
        web.delete("/api/boxes/{box}/ports/{port:[0-9]+}", gateway.close_port),
        web.get("/api/jobs", gateway.jobs), web.get("/api/jobs/{job}/log", gateway.events),
        web.get("/terminal/{box}/", gateway.terminal), web.get("/terminal/{box}/ws", gateway.websocket),
    ])
    return app


async def create_app():
    config = json.loads(pathlib.Path(required("WEB_TERMINAL_CONFIG")).read_text())
    os.environ.update({"BOXLITE_REST_URL": config["boxlite_url"], "BOXLITE_API_KEY": config["boxlite_key"], "BOXLITE_RUNTIME": "cloud", "RUST_LOG": "off"})
    pool = await create_pool(config["database_url"])
    crypto = Crypto(config["encryption_key"], config["signing_key"])
    client = aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=30))
    gateway = Gateway(pool, crypto, client, config)
    app = application(gateway)
    worker = Worker(pool, crypto)

    async def health(request):
        await pool.fetchval("SELECT 1")
        if worker_task.done():
            raise web.HTTPServiceUnavailable()
        return web.Response(text="ok")

    app.router.add_get("/healthz", health)
    worker_task = asyncio.create_task(worker.run())

    async def cleanup(app):
        for websocket, _, _ in list(gateway.sockets):
            await websocket.close(code=1001)
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
        await client.close()
        await pool.close()

    app.on_cleanup.append(cleanup)
    return app


if __name__ == "__main__":
    # aiohttp exception/access logs can include OAuth codes; only safe categories
    # are emitted by application code, never request URLs or exception strings.
    logging.getLogger("aiohttp").disabled = True
    logging.getLogger("aiohttp.server").disabled = True
    web.run_app(create_app(), host="0.0.0.0", port=3001, access_log=None, print=None)
