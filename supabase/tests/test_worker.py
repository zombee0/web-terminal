import unittest
import uuid
from unittest.mock import AsyncMock, patch

from hangar.boxlite import CloudAPIError
from supabase.worker import Worker


class WorkerResourcesTest(unittest.IsolatedAsyncioTestCase):
    async def test_provider_receives_the_reserved_resources_and_enabled_inbound(self):
        runtime = AsyncMock()
        with patch('supabase.worker.create_runtime', return_value=runtime):
            worker = Worker(AsyncMock(), AsyncMock())
        row = {'id': uuid.uuid4(), 'provider_id': None, 'provider_name': 'fixture',
               'cpus': 1, 'memory_mib': 2048, 'disk_size_gb': 8}
        with patch('supabase.worker.cloud_api', side_effect=[CloudAPIError(404), {'id': 'fixture-provider'}]) as api:
            await worker.resolve(row, create=True)
        payload = api.call_args_list[1].args[2]
        self.assertEqual((payload['cpus'], payload['memory_mib'], payload['disk_size_gb']), (1,2048,8))
        self.assertEqual(payload['network']['inbound']['mode'], 'enabled')
        self.assertEqual(payload['auto_delete'], 0)
        runtime.get.assert_awaited_once_with('fixture-provider')
