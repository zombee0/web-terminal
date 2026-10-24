import asyncio
import json
import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from supabase.app import Gateway, application
from supabase.auth import Auth, AuthenticationError
from supabase.crypto import Crypto, new_keys, unb64
from supabase.store import public_box


class CryptoTest(unittest.TestCase):
    def test_grant_bound_to_box_method_path_and_encryption(self):
        keys = new_keys()
        crypto = Crypto(**keys)
        box = uuid.uuid4()
        payload, signature = crypto.grant(box).split(".")
        key = Ed25519PublicKey.from_public_bytes(unb64(crypto.guest_config(box)["public_key"]))
        key.verify(unb64(signature), unb64(payload))
        claims = json.loads(unb64(payload))
        self.assertEqual((claims["box"], claims["method"], claims["path"]), (str(box), "GET", "/ws"))
        with self.assertRaises(InvalidSignature):
            key.verify(unb64(signature), unb64(payload)+b" ")
        encrypted = crypto.encrypt("provider-token")
        self.assertNotIn("provider-token", encrypted)
        self.assertEqual(crypto.decrypt(encrypted), "provider-token")
        self.assertNotEqual(crypto.csrf("cookie-a"), crypto.csrf("cookie-b"))

    def test_api_redacts_provider_fields(self):
        row = dict(id=uuid.uuid4(), name="Example", status="running", created_at=datetime.now(timezone.utc),
                   failure_code=None, provider_id="PRIVATE-ID", provider_name="PRIVATE-NAME", url="SECRET-URL",
                   cpus=1, memory_mib=2048, disk_size_gb=10, agy_trial=True)
        exposed = json.dumps(public_box(row))
        self.assertIs(public_box(row)["gemini"], True)
        for value in ("PRIVATE-ID", "PRIVATE-NAME", "SECRET-URL"):
            self.assertNotIn(value, exposed)
        self.assertIsNone(public_box(row)["url"])


class GatewayTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gateway = Gateway(AsyncMock(), Crypto(**new_keys()), AsyncMock(), {
            "public_url": "https://terminal.test", "supabase_url": "https://project.supabase.co", "publishable_key": "test"})
        self.gateway.auth.session = AsyncMock(side_effect=AuthenticationError)
        self.client = TestClient(TestServer(application(self.gateway)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_anonymous_cannot_read_or_mutate_resources(self):
        box, job = uuid.uuid4(), uuid.uuid4()
        for method, path in [("get", "/api/boxes"), ("post", "/api/boxes"), ("get", "/api/jobs"),
                             ("get", f"/api/jobs/{job}/log"), ("delete", f"/api/boxes/{box}"),
                             ("post", f"/api/boxes/{box}/resume"), ("post", f"/api/boxes/{box}/stop")]:
            response = await getattr(self.client, method)(path)
            self.assertEqual(response.status, 401, path)
        response = await self.client.get(f"/terminal/{box}/", allow_redirects=False)
        self.assertEqual(response.status, 302)
        self.assertTrue(response.headers["Location"].startswith("/auth/login?"))
        response = await self.client.get(f"/terminal/{box}/ws", headers={"Origin": "https://terminal.test"})
        self.assertEqual(response.status, 401)

    async def test_admin_routes_deny_anonymous_and_non_admin_before_queries(self):
        self.gateway.admin_store.overview = AsyncMock()
        self.gateway.admin_store.listing = AsyncMock()
        for path in ['/admin.js', '/api/admin/overview', '/api/admin/boxes']:
            response = await self.client.get(path)
            self.assertEqual(response.status, 401, path)
        response = await self.client.get('/admin/', allow_redirects=False)
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers['Location'], '/auth/login?next=/admin/')
        self.gateway.auth.session = AsyncMock(return_value={'user_id': uuid.uuid4()})
        self.gateway.admin_store.allowed = AsyncMock(return_value=False)
        for path in ['/admin/', '/admin.js', '/api/admin/overview', '/api/admin/boxes']:
            response = await self.client.get(path)
            self.assertEqual(response.status, 403, path)
        self.gateway.admin_store.overview.assert_not_awaited()
        self.gateway.admin_store.listing.assert_not_awaited()

    async def test_csrf_and_origin_both_required(self):
        self.gateway.auth.session = AsyncMock(return_value={"csrf": "correct", "user_id": uuid.uuid4()})
        for headers in ({}, {"Origin": "https://evil.test", "X-CSRF-Token": "correct"}, {"Origin": "https://terminal.test"}):
            response = await self.client.post("/api/boxes", headers=headers, json={})
            self.assertEqual(response.status, 403)

    async def test_exception_text_never_reaches_client(self):
        self.gateway.auth.session = AsyncMock(side_effect=RuntimeError("SECRET-UPSTREAM-URL"))
        response = await self.client.get("/api/boxes")
        self.assertEqual(response.status, 503)
        self.assertNotIn("SECRET", await response.text())

    async def test_hosted_page_disables_local_without_upstream_config(self):
        response = await self.client.get("/")
        html = await response.text()
        self.assertIn('id="btn-local" disabled', html)
        self.assertIn('src="/hosted.js"', html)
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertNotIn("publishable_key", html)


class AuthTest(unittest.IsolatedAsyncioTestCase):
    async def test_pkce_callback_requires_browser_flow_cookie(self):
        auth = Auth(AsyncMock(), Crypto(**new_keys()), AsyncMock(), "https://project.supabase.co", "key", "https://terminal.test")
        request = make_mocked_request("GET", "/auth/callback?code=test")
        with self.assertRaises(AuthenticationError):
            await auth.callback(request)

    async def test_external_next_is_not_a_redirect_target(self):
        pool = AsyncMock()
        auth = Auth(pool, Crypto(**new_keys()), AsyncMock(), "https://project.supabase.co", "key", "https://terminal.test")
        response = await auth.start(make_mocked_request("GET", "/auth/login?next=https://evil.test"))
        self.assertEqual(pool.execute.call_args.args[3], "/")
        self.assertTrue(response.cookies["__Host-wt-flow"]["secure"])
        self.assertTrue(response.cookies["__Host-wt-flow"]["httponly"])
        self.assertIn("code_challenge_method=s256", response.headers["Location"])

    async def test_admin_login_destination_is_allowlisted(self):
        pool = AsyncMock()
        auth = Auth(pool, Crypto(**new_keys()), AsyncMock(), 'https://project.supabase.co', 'key', 'https://terminal.test')
        for path, expected in [('/admin/', '/admin/'), ('/admin', '/admin/'), ('//evil.test/admin/', '/'), ('/admin/../evil', '/')]:
            await auth.start(make_mocked_request('GET', '/auth/login?next='+path))
            self.assertEqual(pool.execute.call_args.args[3], expected)
