"""Real PostgreSQL concurrency and HTTP authorization in an isolated schema."""

import asyncio
import json
import os
import secrets
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from aiohttp.test_utils import TestClient, TestServer
from unittest.mock import AsyncMock

from supabase.app import Gateway, application
from supabase.admin import AdminStore
from supabase.bootstrap_owner import bootstrap_owner
from supabase.config import ROOT, load_deployment_env, required
from supabase.crypto import Crypto, digest, new_keys
from supabase.database import create_pool
from supabase.store import Conflict, Store


@unittest.skipUnless(os.environ.get("WT_TEST_DATABASE") == "1", "Use make test-supabase-db for isolated PostgreSQL integration")
class DatabaseTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        load_deployment_env()
        self.schema = "wt_test_"+uuid.uuid4().hex
        self.admin = await create_pool(required("SUPABASE_DB_URL"), dns_fallback=True)
        sql = (ROOT/"supabase/migrations/001_control_plane.sql").read_text().replace("web_terminal", self.schema)
        sql = sql.split("-- Narrow definer functions")[0].replace("REFERENCES auth.users(id)", "")
        await self.admin.execute(sql)
        await self.admin.execute((ROOT/"supabase/migrations/002_admin.sql").read_text().replace("web_terminal", self.schema))
        self.pool = await create_pool(required("SUPABASE_DB_URL"), schema=self.schema, dns_fallback=True)
        await self.pool.execute("CREATE TABLE test_auth_sessions(id uuid PRIMARY KEY,owner_id uuid)")
        await self.pool.execute(f"""CREATE FUNCTION auth_session_valid(sid uuid,uid uuid) RETURNS boolean LANGUAGE sql AS $$
            SELECT EXISTS(SELECT 1 FROM {self.schema}.test_auth_sessions WHERE id=sid AND owner_id=uid) $$""")
        await self.pool.execute("""CREATE TABLE test_auth_users(id uuid PRIMARY KEY,email text,email_confirmed_at timestamptz);
            CREATE TABLE test_auth_identities(user_id uuid,provider text,identity_data jsonb);""")
        sql = (ROOT/'supabase/migrations/003_config_admin.sql').read_text().replace('web_terminal', self.schema)
        sql = sql.replace('auth.users', self.schema+'.test_auth_users').replace('auth.identities', self.schema+'.test_auth_identities')
        await self.pool.execute(sql)
        await self.pool.execute((ROOT/'supabase/migrations/004_application_roles.sql').read_text().replace('web_terminal', self.schema))
        await self.pool.execute((ROOT/'supabase/migrations/005_user_resource_quota.sql').read_text().replace('web_terminal', self.schema))
        self.store = Store(self.pool)
        self.owners = [uuid.uuid4(), uuid.uuid4()]
        self.cookies = [secrets.token_urlsafe(32), secrets.token_urlsafe(32)]
        crypto = Crypto(**new_keys())
        for owner, cookie in zip(self.owners, self.cookies):
            email = str(owner)+"@example.invalid"
            await self.pool.execute("INSERT INTO test_auth_users VALUES($1,$2,now())", owner, email)
            await self.pool.execute("INSERT INTO test_auth_identities VALUES($1,'google',$2::jsonb)", owner,
                json.dumps({'email': email, 'email_verified': True}))
            await self.pool.execute("INSERT INTO profiles(id,email) VALUES($1,$2)", owner, str(owner)+"@example.invalid")
            sid = uuid.uuid4()
            await self.pool.execute("INSERT INTO test_auth_sessions VALUES($1,$2)", sid, owner)
            await self.pool.execute("""INSERT INTO sessions(id,user_id,auth_session_id,access_token,refresh_token,access_expires_at,expires_at)
                VALUES($1,$2,$3,$4,$4,now()+interval '1 hour',now()+interval '1 day')""", digest(cookie), owner, sid, crypto.encrypt("test"))
        self.gateway = Gateway(self.pool, crypto, AsyncMock(), {"public_url": "https://terminal.test", "supabase_url": "https://project.supabase.co", "publishable_key": "test"})
        self.client = TestClient(TestServer(application(self.gateway)))
        await self.client.start_server()

    async def asyncTearDown(self):
        async def close(pool):
            try:
                await asyncio.wait_for(pool.close(), 10)
            except TimeoutError:
                pool.terminate()
        try:
            await self.client.close()
        finally:
            try:
                await close(self.pool)
            finally:
                try:
                    await self.admin.execute('DROP SCHEMA "'+self.schema+'" CASCADE')
                finally:
                    await close(self.admin)

    def headers(self, index=0):
        cookie = self.cookies[index]
        return {"Cookie": "__Host-wt-session="+cookie, "Origin": "https://terminal.test", "X-CSRF-Token": self.gateway.crypto.csrf(cookie)}

    async def test_concurrent_creation_quota_and_deletion_release(self):
        results = await asyncio.gather(*(self.store.create(self.owners[0], "Terminal", str(uuid.uuid4())) for _ in range(12)), return_exceptions=True)
        jobs = [item for item in results if not isinstance(item, Exception)]
        self.assertEqual(len(jobs), 1)
        self.assertTrue(all(isinstance(item, Conflict) for item in results if isinstance(item, Exception)))
        job = jobs[0]
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM boxes"), 1)
        worker = uuid.uuid4()
        claimed = await self.store.claim(worker)
        await self.store.finish(claimed, worker, "stopped", provisioned=True)
        with self.assertRaises(Conflict):
            await self.store.create(self.owners[0], "Second", None)
        await self.store.operate(self.owners[0], job["box_id"], "delete", None)
        deleting = await self.store.claim(worker)
        await self.store.finish(deleting, worker, "deleted")
        await self.store.create(self.owners[0], "Replacement", None)
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM boxes WHERE deleted_at IS NULL"), 1)

    async def test_owner_checks_on_http_sse_ws_and_session_revocation(self):
        job = await self.store.create(self.owners[0], "Private", None)
        box = job["box_id"]
        response = await self.client.get("/api/boxes", headers=self.headers(1))
        self.assertEqual((await response.json())["boxes"], [])
        for method, path in [("get", f"/terminal/{box}/"), ("get", f"/terminal/{box}/ws"),
                             ("get", f"/api/jobs/{job['id']}/log"), ("delete", f"/api/boxes/{box}"),
                             ("post", f"/api/boxes/{box}/stop"), ("post", f"/api/boxes/{box}/resume")]:
            response = await getattr(self.client, method)(path, headers=self.headers(1), allow_redirects=False)
            self.assertEqual(response.status, 404, path)
        await self.pool.execute("DELETE FROM test_auth_sessions WHERE owner_id=$1", self.owners[0])
        response = await self.client.get("/api/boxes", headers=self.headers(0))
        self.assertEqual(response.status, 401)

    async def test_resource_validation_and_expired_lease_recovery(self):
        for resources in [{'cpus': 999}, {'memory_mib': 1}, {'disk_size_gb': 11}, {'cpus': True}, {'memory_mib': '2048'}]:
            response = await self.client.post('/api/boxes', json={'name': 'Invalid', **resources}, headers=self.headers())
            self.assertEqual(response.status, 400)
        response = await self.client.post("/api/boxes", json={"name": "Test", "cpus": 2, "memory_mib": 4096}, headers=self.headers())
        self.assertEqual(response.status, 202)
        box = await self.pool.fetchrow("SELECT * FROM boxes")
        self.assertEqual((box["cpus"], box["memory_mib"], box["disk_size_gb"]), (2, 4096, 10))
        first, second = uuid.uuid4(), uuid.uuid4()
        job = await self.store.claim(first)
        self.assertIsNone(await self.store.claim(second))
        await self.pool.execute("UPDATE jobs SET lease_until=now()-interval '1 second' WHERE id=$1", job["id"])
        recovered = await self.store.claim(second)
        self.assertEqual(recovered["id"], job["id"])
        with self.assertRaises(Conflict):
            await self.store.finish(job, first, "running")
        await self.store.finish(recovered, second, "running", provisioned=True)

    async def test_concurrent_retries_return_the_same_job(self):
        key = str(uuid.uuid4())
        created = await asyncio.gather(*(self.store.create(self.owners[0], "Retry", key) for _ in range(5)))
        self.assertEqual(len({row["id"] for row in created}), 1)
        worker = uuid.uuid4()
        job = await self.store.claim(worker)
        await self.store.finish(job, worker, "running", provisioned=True)
        key = str(uuid.uuid4())
        stopped = await asyncio.gather(*(self.store.operate(self.owners[0], job["box_id"], "stop", key) for _ in range(5)))
        self.assertEqual(len({row["id"] for row in stopped}), 1)

    async def test_admin_is_separate_from_personal_fleet_and_revocable(self):
        jobs = [await self.store.create(owner, f"Owner {index}", None) for index, owner in enumerate(self.owners)]
        paths = ["/admin/", "/admin.js", "/api/admin/overview", "/api/admin/boxes"]
        for path in paths:
            response = await self.client.get(path, headers=self.headers(), allow_redirects=False)
            self.assertEqual(response.status, 403, path)
        # The runtime never accepts a browser-provided role or owner override.
        response = await self.client.get("/api/boxes?owner_id="+str(self.owners[1])+"&is_admin=true", headers=self.headers())
        self.assertEqual([row['id'] for row in (await response.json())['boxes']], [str(jobs[0]['box_id'])])
        await self.pool.execute("INSERT INTO admin_users(user_id) VALUES($1)", self.owners[0])
        response = await self.client.get('/api/admin/overview', headers=self.headers())
        self.assertEqual(response.status, 403, 'Legacy database grants must not authorize admins')
        await self.pool.execute("INSERT INTO app_roles(user_id,role) VALUES($1,'admin')", self.owners[0])
        response = await self.client.get("/api/session", headers=self.headers())
        self.assertTrue((await response.json())['user']['is_admin'])
        for path in paths:
            response = await self.client.get(path, headers=self.headers(), allow_redirects=False)
            self.assertEqual(response.status, 200, path)
        response = await self.client.get("/api/admin/overview", headers=self.headers())
        summary = await response.json()
        self.assertEqual(set(summary), {'boxes', 'allocated', 'viewer'})
        self.assertEqual(summary['boxes'], {'creating': 2})
        self.assertEqual(summary['allocated'], {'cpus': 4, 'memory_mib': 8192, 'disk_size_gb': 20})
        response = await self.client.get("/api/admin/boxes", headers=self.headers())
        data = await response.json()
        self.assertEqual(data['total'], 2)
        self.assertEqual({item['id'] for item in data['items']}, {str(job['box_id']) for job in jobs})
        # Admin access does not bypass terminal ownership or the account quota.
        response = await self.client.get("/api/boxes", headers=self.headers())
        self.assertEqual(len((await response.json())['boxes']), 1)
        foreign = jobs[1]['box_id']
        for method, path in [('get', f'/terminal/{foreign}/'), ('get', f'/terminal/{foreign}/ws'),
                             ('post', f'/api/boxes/{foreign}/stop'), ('post', f'/api/boxes/{foreign}/resume'),
                             ('delete', f'/api/boxes/{foreign}'), ('get', f"/api/jobs/{jobs[1]['id']}/log")]:
            response = await getattr(self.client, method)(path, headers=self.headers(), allow_redirects=False)
            self.assertEqual(response.status, 404, path)
        for path in ['/api/admin/users', '/api/admin/jobs', '/api/admin/lifecycle', '/api/admin/logins']:
            response = await self.client.get(path, headers=self.headers())
            self.assertEqual(response.status, 404, path)
        response = await self.client.post('/api/boxes', headers=self.headers(), json={'name': 'Second'})
        self.assertEqual(response.status, 409)
        await self.pool.execute('DELETE FROM app_roles WHERE user_id=$1', self.owners[0])
        for path in paths:
            response = await self.client.get(path, headers=self.headers(), allow_redirects=False)
            self.assertEqual(response.status, 403, path)
        await self.pool.execute("INSERT INTO app_roles(user_id,role) VALUES($1,'admin')", self.owners[0])
        await self.pool.execute("UPDATE sessions SET revoked_at=now() WHERE user_id=$1", self.owners[0])
        response = await self.client.get('/api/admin/overview', headers=self.headers())
        self.assertEqual(response.status, 401)

    async def test_admin_pagination_search_status_and_explicit_fields(self):
        await self.pool.execute("INSERT INTO app_roles(user_id,role) VALUES($1,'admin')", self.owners[0])
        for index in range(28):
            await self.pool.execute("""INSERT INTO boxes(id,owner_id,name,provider_name,provider_id,status,deleted_at)
                VALUES($1,$2,$3,$4,$5,'deleted',now())""", uuid.uuid4(), self.owners[index % 2],
                f"Historical {index}", "PRIVATE-NAME-"+str(index), "PRIVATE-ID-"+str(index))
        job = await self.store.create(self.owners[0], "100%_literal", None)
        await self.pool.execute("INSERT INTO login_events(user_id,action,source,metadata) VALUES($1,'login','application',$2::jsonb)",
                                self.owners[0], json.dumps({'provider': 'google', 'secret': 'PRIVATE-TOKEN'}))
        await self.pool.execute("UPDATE lifecycle_events SET detail=$1::jsonb", json.dumps({'secret': 'PRIVATE-URL'}))
        response = await self.client.get('/api/admin/boxes', headers=self.headers())
        first = await response.json()
        self.assertEqual((first['total'], len(first['items'])), (29, 25))
        response = await self.client.get('/api/admin/boxes?page=2', headers=self.headers())
        second = await response.json()
        self.assertEqual(len(second['items']), 4)
        self.assertFalse({row['id'] for row in first['items']} & {row['id'] for row in second['items']})
        response = await self.client.get('/api/admin/boxes?status=creating&search=100%25_literal', headers=self.headers())
        result = await response.json()
        self.assertEqual([item['id'] for item in result['items']], [str(job['box_id'])])
        for section in ['boxes']:
            response = await self.client.get('/api/admin/'+section, headers=self.headers())
            body = await response.text()
            self.assertNotIn('PRIVATE-', body)
            for key in ['provider_id', 'provider_name', 'access_token', 'refresh_token', 'access_url', 'metadata', 'detail']:
                self.assertNotIn('"'+key+'"', body)
            response = await self.client.get('/api/admin/'+section, params={'search': "' OR 1=1 --"}, headers=self.headers())
            self.assertEqual((await response.json())['items'], [])
        for query in ['page=-1', 'page=1000001', 'page=abc', 'status=not_a_status', 'search='+'a'*121]:
            response = await self.client.get('/api/admin/boxes?'+query, headers=self.headers())
            self.assertEqual(response.status, 400, query)

    async def test_database_role_requires_current_verified_google_identity(self):
        email = str(self.owners[0])+'@example.invalid'
        await self.pool.execute("INSERT INTO app_roles(user_id,role) VALUES($1,'admin')", self.owners[0])
        response = await self.client.get('/api/admin/overview', headers=self.headers())
        self.assertEqual(response.status, 200)
        # A profile email or an identity from another provider is insufficient.
        await self.pool.execute('UPDATE profiles SET email=$1 WHERE id=$2', email, self.owners[1])
        response = await self.client.get('/api/admin/overview', headers=self.headers(1))
        self.assertEqual(response.status, 403)
        await self.pool.execute('UPDATE test_auth_users SET email_confirmed_at=NULL WHERE id=$1', self.owners[0])
        response = await self.client.get('/api/admin/overview', headers=self.headers())
        self.assertEqual(response.status, 403)
        await self.pool.execute('UPDATE test_auth_users SET email_confirmed_at=now() WHERE id=$1', self.owners[0])
        for provider, identity in [('email', {'email': email, 'email_verified': True}),
                                   ('google', {'email': email, 'email_verified': False}),
                                   ('google', {'email': 'other@example.invalid', 'email_verified': True})]:
            await self.pool.execute('UPDATE test_auth_identities SET provider=$2,identity_data=$3::jsonb WHERE user_id=$1',
                self.owners[0], provider, json.dumps(identity))
            response = await self.client.get('/api/admin/overview', headers=self.headers())
            self.assertEqual(response.status, 403)

    async def test_owner_bootstrap_is_verified_once_and_ignores_legacy_config(self):
        email = str(self.owners[0])+'@example.invalid'
        self.gateway.admin_store = AdminStore(self.pool)
        # An old deployed config must not grant roles in the new application.
        other = Gateway(self.pool, self.gateway.crypto, AsyncMock(), {
            'public_url': 'https://terminal.test', 'supabase_url': 'https://project.supabase.co',
            'publishable_key': 'test', 'admin_emails': [email]})
        self.assertFalse(await other.admin_store.allowed(self.owners[0]))
        with self.assertRaises(ValueError):
            await bootstrap_owner(self.pool, 'missing@example.invalid')
        self.assertTrue(await bootstrap_owner(self.pool, email.upper()))
        self.assertFalse(await bootstrap_owner(self.pool, email))
        with self.assertRaises(ValueError):
            await bootstrap_owner(self.pool, str(self.owners[1])+'@example.invalid')
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM role_events'), 1)
        # Role membership follows the user ID through a verified email change.
        await self.pool.execute("UPDATE test_auth_users SET email='changed@example.invalid' WHERE id=$1", self.owners[0])
        await self.pool.execute("""UPDATE test_auth_identities SET identity_data='{"email":"changed@example.invalid","email_verified":true}' WHERE user_id=$1""", self.owners[0])
        self.assertEqual(await other.admin_store.role(self.owners[0]), 'owner')

    async def test_owner_management_csrf_revocation_history_and_box_isolation(self):
        await bootstrap_owner(self.pool, str(self.owners[0])+'@example.invalid')
        path = '/api/admin/members/'+str(self.owners[1])
        change = {'role': 'admin', 'expected_role': 'user'}
        for headers in [{}, {'Cookie': self.headers()['Cookie']}, {**self.headers(), 'Origin': 'https://evil.test'}]:
            response = await self.client.put(path, headers=headers, json=change)
            self.assertIn(response.status, (401, 403))
        response = await self.client.put(path, headers=self.headers(1), json=change)
        self.assertEqual(response.status, 403)
        response = await self.client.post('/api/admin/members/lookup', headers=self.headers(), json={'email': str(self.owners[1])+'@example.invalid'})
        self.assertEqual((await response.json())['user_id'], str(self.owners[1]))
        response = await self.client.put(path, headers=self.headers(), json=change)
        self.assertEqual(response.status, 200)
        response = await self.client.get('/api/admin/overview', headers=self.headers(1))
        self.assertEqual(response.status, 200)
        for endpoint in ['/api/admin/members', '/api/admin/role-events']:
            response = await self.client.get(endpoint, headers=self.headers(1))
            self.assertEqual(response.status, 403)
        response = await self.client.put(path, headers=self.headers(1), json={'role': 'owner', 'expected_role': 'admin'})
        self.assertEqual(response.status, 403)
        response = await self.client.put(path, headers=self.headers(), json=change)
        self.assertEqual(response.status, 409, 'Stale submissions must not overwrite newer roles')
        # Even a site owner cannot inspect or mutate another user's resources.
        job = await self.store.create(self.owners[1], 'Foreign', None)
        box = job['box_id']
        for method, endpoint in [('get', f'/terminal/{box}/'), ('get', f'/terminal/{box}/ws'),
                                 ('get', f"/api/jobs/{job['id']}/log"), ('delete', f'/api/boxes/{box}'),
                                 ('post', f'/api/boxes/{box}/stop'), ('post', f'/api/boxes/{box}/resume')]:
            response = await getattr(self.client, method)(endpoint, headers=self.headers(), allow_redirects=False)
            self.assertEqual(response.status, 404)
        response = await self.client.put(path, headers=self.headers(), json={'role': 'user', 'expected_role': 'admin'})
        self.assertEqual(response.status, 200)
        response = await self.client.get('/api/admin/overview', headers=self.headers(1))
        self.assertEqual(response.status, 403, 'The same session must lose access on its next request')
        response = await self.client.get('/api/admin/role-events', headers=self.headers())
        history = await response.json()
        self.assertEqual(history['total'], 3)
        self.assertEqual([(r['previous_role'], r['new_role']) for r in history['items']], [('admin','user'), ('user','admin'), ('user','owner')])
        self.assertTrue(all(r['actor_id'] == str(self.owners[0]) for r in history['items']))

    async def test_last_owner_and_concurrent_self_demotion(self):
        await bootstrap_owner(self.pool, str(self.owners[0])+'@example.invalid')
        path = '/api/admin/members/'+str(self.owners[0])
        response = await self.client.put(path, headers=self.headers(), json={'role': 'admin', 'expected_role': 'owner'})
        self.assertEqual(response.status, 409)
        self.assertIn('last owner', (await response.json())['error'])
        await self.gateway.admin_store.change(digest(self.cookies[0]), self.owners[1], 'owner', 'user')
        # Two owners cannot each remove themselves based on the other's old role.
        results = await asyncio.gather(*(self.gateway.admin_store.change(digest(self.cookies[i]), uid, 'admin', 'owner')
                                        for i, uid in enumerate(self.owners)), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, Conflict) for result in results), 1)
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM app_roles WHERE role='owner'"), 1)
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM role_events'), 3)

    async def test_role_function_rechecks_session_and_verified_identity(self):
        from aiohttp import web
        await bootstrap_owner(self.pool, str(self.owners[0])+'@example.invalid')
        await self.pool.execute('UPDATE test_auth_users SET email_confirmed_at=NULL WHERE id=$1', self.owners[1])
        with self.assertRaises(ValueError):
            await self.gateway.admin_store.change(digest(self.cookies[0]), self.owners[1], 'admin', 'user')
        with self.assertRaises(web.HTTPForbidden):
            await self.gateway.admin_store.change('forged-session', self.owners[1], 'owner', 'user')
        await self.pool.execute('UPDATE sessions SET revoked_at=now() WHERE user_id=$1', self.owners[0])
        with self.assertRaises(web.HTTPForbidden):
            await self.gateway.admin_store.change(digest(self.cookies[0]), self.owners[1], 'owner', 'user')
        self.assertEqual(await self.pool.fetchval('SELECT count(*) FROM role_events'), 1)

    async def test_aggregate_quota_allows_two_boxes_and_serializes_concurrent_creation(self):
        resources = {'cpus': 1, 'memory_mib': 2048, 'disk_size_gb': 10}
        results = await asyncio.gather(*(self.store.create(self.owners[0], 'Small', str(uuid.uuid4()), resources)
                                        for _ in range(6)), return_exceptions=True)
        jobs = [result for result in results if not isinstance(result, Exception)]
        self.assertEqual(len(jobs), 2)
        self.assertTrue(all(isinstance(result, Conflict) for result in results if isinstance(result, Exception)))
        response = await self.client.get('/api/boxes', headers=self.headers())
        rows = (await response.json())['boxes']
        self.assertEqual(len(rows), 2)
        self.assertEqual([(row['cpus'],row['memory_mib'],row['disk_size_gb']) for row in rows], [(1,2048,10)]*2)
        self.assertEqual(await self.pool.fetchval('SELECT sum(disk_size_gb) FROM boxes'), 20)
        await self.pool.execute("UPDATE boxes SET status='stopped',desired_state='stopped'")
        with self.assertRaises(Conflict):
            await self.store.create(self.owners[0], 'Stopped still counts', None, resources)
        await self.pool.execute("UPDATE boxes SET status='deleted',desired_state='deleted',deleted_at=now() WHERE id=$1", jobs[0]['box_id'])
        replacement = await self.store.create(self.owners[0], 'Replacement', None, resources)
        self.assertIsNotNone(replacement)
        await self.store.create(self.owners[1], 'Independent account', None)

    async def test_memory_quota_and_direct_database_writes_cannot_bypass_limits(self):
        import asyncpg
        first = await self.store.create(self.owners[0], 'Memory', None, {'cpus': 1, 'memory_mib': 3072, 'disk_size_gb': 8})
        with self.assertRaises(Conflict):
            await self.store.create(self.owners[0], 'Too much memory', None, {'cpus': 1, 'memory_mib': 2048})
        second = await self.store.create(self.owners[0], 'Remaining', None, {'cpus': 1, 'memory_mib': 1024})
        with self.assertRaises(asyncpg.CheckViolationError):
            await self.pool.execute('UPDATE boxes SET memory_mib=2048 WHERE id=$1', second['box_id'])
        with self.assertRaises(asyncpg.CheckViolationError):
            await self.pool.execute("""INSERT INTO boxes(id,owner_id,name,provider_name,status,cpus,memory_mib)
                VALUES($1,$2,'Bypass','bypass','creating',1,512)""", uuid.uuid4(), self.owners[0])
        with self.assertRaises(asyncpg.CheckViolationError):
            await self.pool.execute('UPDATE boxes SET disk_size_gb=11 WHERE id=$1', first['box_id'])
