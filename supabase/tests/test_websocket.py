import asyncio
import contextlib
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from supabase.app import Gateway
from supabase.crypto import Crypto, new_keys


class WebSocketAdmissionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gateway = Gateway(AsyncMock(), Crypto(**new_keys()), AsyncMock(), {
            "public_url": "https://terminal.test", "supabase_url": "https://project.supabase.co",
            "publishable_key": "test"})
        self.box = uuid.uuid4()
        self.session = {"id": "test-session"}
        self.gateway.owned_running = AsyncMock(return_value=(self.session, {
            "id": self.box, "provider_id": "provider-test"}))
        self.request = make_mocked_request("GET", f"/terminal/{self.box}/ws",
                                           headers={"Origin": "https://terminal.test"})
        self.arrivals = asyncio.Queue()
        self.release = asyncio.Event()
        self.tasks = []
        self.gateway.client.ws_connect = self.upstream
        self.tunnel_patch = patch("supabase.app.cloud_tunnel_url", return_value="https://upstream.test")
        self.tunnel_patch.start()

    async def asyncTearDown(self):
        await self.cancel_requests()
        self.tunnel_patch.stop()

    @contextlib.asynccontextmanager
    async def upstream(self, *args, **kwargs):
        self.arrivals.put_nowait(True)
        await self.release.wait()
        raise ConnectionError("Upstream handshake failed")
        yield  # Context manager deliberately fails before admitting a socket.

    def connect(self):
        task = asyncio.create_task(self.gateway.websocket(self.request))
        self.tasks.append(task)
        return task

    async def cancel_requests(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

    async def fill_pending_slots(self):
        for _ in range(4):
            self.connect()
        for _ in range(4):
            await asyncio.wait_for(self.arrivals.get(), 5)

    async def test_pending_handshakes_count_toward_box_limit(self):
        await self.fill_pending_slots()
        fifth = self.connect()
        reached_upstream = asyncio.create_task(self.arrivals.get())
        try:
            done, _ = await asyncio.wait([fifth, reached_upstream], timeout=5,
                                         return_when=asyncio.FIRST_COMPLETED)
            self.assertIn(fifth, done, "A fifth handshake reached upstream instead of being rejected")
            with self.assertRaises(web.HTTPTooManyRequests):
                await fifth
            self.assertFalse(reached_upstream.done(), "Rejected handshake acquired an upstream connection")
        finally:
            reached_upstream.cancel()
            await asyncio.gather(reached_upstream, return_exceptions=True)

    async def test_cancellation_releases_all_pending_slots(self):
        await self.fill_pending_slots()
        await self.cancel_requests()
        await self.fill_pending_slots()

    async def test_failed_upstream_releases_slots(self):
        await self.fill_pending_slots()
        self.release.set()
        results = await asyncio.gather(*self.tasks, return_exceptions=True)
        self.assertTrue(all(isinstance(result, ConnectionError) for result in results))
        self.tasks.clear()
        self.release.clear()
        await self.fill_pending_slots()

    async def test_another_box_has_independent_capacity(self):
        await self.fill_pending_slots()
        self.gateway.owned_running.return_value = (self.session, {
            "id": uuid.uuid4(), "provider_id": "another-provider"})
        await self.fill_pending_slots()
