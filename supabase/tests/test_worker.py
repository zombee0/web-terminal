import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

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


class WorkerGeminiTest(unittest.IsolatedAsyncioTestCase):
    async def run_job(self, row):
        pool = AsyncMock()
        pool.fetchrow.return_value = row
        with patch('supabase.worker.create_runtime', return_value=AsyncMock()):
            worker = Worker(pool, MagicMock())
        worker.resolve = AsyncMock(return_value=object())
        worker.store = AsyncMock()
        with patch('supabase.worker.provision_web_terminal', new=AsyncMock()) as provision, \
                patch('supabase.worker.resume_web_terminal', new=AsyncMock()) as resume:
            await worker.execute({'box_id': row['id'], 'operation': 'create' if not row['provisioned'] else 'resume'})
        return provision, resume

    async def test_first_provision_installs_agy_only_when_chosen(self):
        for chosen in (True, False):
            provision, _ = await self.run_job({'id': uuid.uuid4(), 'provisioned': False, 'agy_trial': chosen})
            self.assertIs(provision.await_args.kwargs['install_agy'], chosen)

    async def test_resume_does_not_reinstall_clis(self):
        provision, resume = await self.run_job({'id': uuid.uuid4(), 'provisioned': True, 'agy_trial': True})
        provision.assert_not_awaited()
        self.assertIs(resume.await_args.kwargs['install_clis'], False)
