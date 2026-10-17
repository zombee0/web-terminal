"""Live role checks with disposable identities/sessions; Google OAuth is separate.

No provider Box is created. Only fixture-owned rows are changed and removed.
"""

import asyncio
import hashlib
import json
import secrets
import shlex
import uuid

import aiohttp

from hangar.boxlite import run_checked
from supabase.config import ROOT, STATE, load_deployment_env, load_private, required, save_private
from supabase.crypto import Crypto, digest
from supabase.database import create_pool
from supabase.preflight import manager


async def main():
    load_deployment_env()
    hashes = {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
              for name in ['supabase/admin.py', 'supabase/app.py', 'supabase/config.py', 'supabase/admin.html',
                           'supabase/admin.js', 'supabase/store.py', 'supabase/worker.py', 'supabase/hosted.js', 'hangar/control.html']}
    runtime_box, box, _ = await manager()
    probe = """import hashlib,json,stat
from pathlib import Path
path = Path('/etc/web-terminal/manager.json')
assert 'admin_emails' not in json.loads(path.read_text())
assert stat.S_IMODE(path.stat().st_mode) == 0o600 and path.stat().st_uid == 0
for name, expected in HASHES.items():
    assert hashlib.sha256(Path(name).read_bytes()).hexdigest() == expected
print('PASS: deployed role code matches local source; configuration contains no administrator list')
"""
    probe = 'HASHES='+repr(hashes)+'\n'+probe
    await run_checked(box, 'sh', args=['-lc', 'cd /opt/web-terminal/current && /opt/web-terminal/venv/bin/python -c '+shlex.quote(probe)], user='root', log=print)
    state, keys = load_private(STATE/'manager.json'), load_private(STATE/'secrets.json')
    crypto = Crypto(keys['encryption_key'], keys['signing_key'])
    pool = await create_pool(required('SUPABASE_DB_URL'), dns_fallback=True)
    fixture_path = STATE/'admin-acceptance.json'
    fixture = load_private(fixture_path)
    if not fixture:
        fixture = {'users': [str(uuid.uuid4()), str(uuid.uuid4())], 'boxes': [str(uuid.uuid4()), str(uuid.uuid4())],
                   'cookies': [secrets.token_urlsafe(32), secrets.token_urlsafe(32)]}
        save_private(fixture_path, fixture)
    users, boxes = [[uuid.UUID(value) for value in fixture[key]] for key in ['users', 'boxes']]
    emails = ['wt-admin-check-'+uid.hex+'@example.invalid' for uid in users]
    baseline = [tuple(row) for row in await pool.fetch('SELECT user_id,role FROM app_roles WHERE NOT(user_id=ANY($1::uuid[])) ORDER BY user_id', users)]
    try:
        for uid, bid, cookie, email in zip(users, boxes, fixture['cookies'], emails):
            async with pool.acquire() as conn, conn.transaction():
                await conn.execute("""INSERT INTO auth.users(id,aud,role,email,email_confirmed_at,created_at,updated_at)
                    VALUES($1,'authenticated','authenticated',$2,now(),now(),now()) ON CONFLICT DO NOTHING""", uid, email)
                await conn.execute("""INSERT INTO auth.identities(id,user_id,provider_id,provider,identity_data,created_at,updated_at)
                    VALUES($1,$2,$3,'google',$4::jsonb,now(),now()) ON CONFLICT DO NOTHING""", uuid.uuid4(), uid, 'fixture-'+uid.hex,
                    json.dumps({'sub': 'fixture-'+uid.hex, 'email': email, 'email_verified': True}))
                await conn.execute('INSERT INTO profiles(id,email) VALUES($1,$2) ON CONFLICT DO NOTHING', uid, email)
                sid = uuid.uuid4()
                await conn.execute("INSERT INTO auth.sessions(id,user_id,created_at,updated_at,not_after) VALUES($1,$2,now(),now(),now()+interval '1 hour')", sid, uid)
                await conn.execute("""INSERT INTO sessions(id,user_id,auth_session_id,access_token,refresh_token,access_expires_at,expires_at)
                    VALUES($1,$2,$3,$4,$4,now()+interval '1 hour',now()+interval '1 hour')
                    ON CONFLICT(id) DO UPDATE SET auth_session_id=excluded.auth_session_id,access_expires_at=excluded.access_expires_at,
                    expires_at=excluded.expires_at,revoked_at=NULL""", digest(cookie), uid, sid, crypto.encrypt('admin-check-fixture'))
                # No provider ID or queued job; the worker cannot provision these fixtures.
                await conn.execute("""INSERT INTO boxes(id,owner_id,name,provider_name,status,desired_state)
                    VALUES($1,$2,'Admin check fixture',$3,'stopped','stopped') ON CONFLICT DO NOTHING""", bid, uid, 'admin-check-'+bid.hex)
        await pool.execute('DELETE FROM app_roles WHERE user_id=ANY($1::uuid[])', users)
        base = state['entry_url']
        async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=30)) as client:
            async def request(path, index=None, *, method='GET', data=None, csrf=True):
                headers = {}
                if index is not None:
                    cookie = fixture['cookies'][index]
                    headers = {'Cookie': '__Host-wt-session='+cookie, 'Origin': base}
                    if csrf: headers['X-CSRF-Token'] = crypto.csrf(cookie)
                async with client.request(method, base+path, headers=headers, json=data, allow_redirects=False) as result:
                    body = await result.read()
                    for secret in [b'provider_id', b'provider_name', b'access_token', b'refresh_token', b'BOXLITE_BOX_AUTH_KEY']:
                        assert secret not in body, 'Private field exposure'
                    return result.status, json.loads(body) if result.content_type == 'application/json' else body
            for path in ['/api/admin/overview', '/api/admin/boxes', '/api/admin/members', '/api/admin/role-events', '/admin.js']:
                assert (await request(path))[0] == 401
            assert (await request('/admin/'))[0] == 302
            for index in [0, 1]:
                status, data = await request('/api/boxes', index)
                assert status == 200 and [row['id'] for row in data['boxes']] == [str(boxes[index])]
                assert (await request('/api/admin/overview', index))[0] == 403
            await pool.execute('INSERT INTO admin_users(user_id) VALUES($1) ON CONFLICT DO NOTHING', users[0])
            assert not (await request('/api/session', 0))[1]['user']['is_admin']
            print('PASS: anonymous/ordinary-user denial; legacy grants ignored; personal Fleet isolation', flush=True)
            await pool.execute("INSERT INTO app_roles(user_id,role) VALUES($1,'owner')", users[0])
            assert (await request('/admin/', 0))[0] == 200
            assert (await request('/api/session', 0))[1]['user']['role'] == 'owner'
            lookup = await request('/api/admin/members/lookup', 0, method='POST', data={'email': emails[1]})
            assert lookup[0] == 200 and lookup[1]['user_id'] == str(users[1])
            path = '/api/admin/members/'+str(users[1])
            grant = {'role': 'admin', 'expected_role': 'user'}
            assert (await request(path, 0, method='PUT', data=grant, csrf=False))[0] == 403
            assert (await request(path, 0, method='PUT', data=grant))[0] == 200
            assert (await request('/api/admin/overview', 1))[0] == 200
            assert (await request('/api/admin/boxes', 1))[0] == 200
            for route in ['/api/admin/members', '/api/admin/role-events']:
                assert (await request(route, 1))[0] == 403
            assert (await request(path, 1, method='PUT', data={'role': 'owner', 'expected_role': 'admin'}))[0] == 403
            assert (await request(path, 0, method='PUT', data=grant))[0] == 409
            # Test both application roles against the other user's terminal and controls.
            for actor in [0, 1]:
                foreign = boxes[1-actor]
                for method, route in [('GET', f'/terminal/{foreign}/'), ('GET', f'/terminal/{foreign}/ws'),
                                      ('POST', f'/api/boxes/{foreign}/stop'), ('POST', f'/api/boxes/{foreign}/resume'),
                                      ('DELETE', f'/api/boxes/{foreign}')]:
                    assert (await request(route, actor, method=method))[0] == 404
            assert (await request(path, 0, method='PUT', data={'role': 'user', 'expected_role': 'admin'}))[0] == 200
            assert (await request('/api/admin/overview', 1))[0] == 403
            events = (await request('/api/admin/role-events', 0))[1]['items']
            changes = [row for row in events if row['target_id'] == str(users[1])]
            assert [(r['previous_role'], r['new_role']) for r in changes[:2]] == [('admin','user'), ('user','admin')]
            print('PASS: live grant/revoke, CSRF, stale update, role history, and owner/admin foreign-Box denial', flush=True)
            await pool.execute('UPDATE auth.users SET email_confirmed_at=NULL WHERE id=$1', users[0])
            assert (await request('/api/admin/overview', 0))[0] == 403
            await pool.execute('UPDATE sessions SET revoked_at=now() WHERE user_id=$1', users[0])
            assert (await request('/api/admin/overview', 0))[0] == 401
            # Test live API quota rejection without enqueuing a provider operation:
            # the existing fixture consumes 2 CPU / 4 GiB.
            assert (await request('/api/boxes', 1, method='POST', data={'name': 'Over quota', 'cpus': 1, 'memory_mib': 512}))[0] == 409
            assert (await request('/api/boxes', 1, method='POST', data={'name': 'Oversized disk', 'disk_size_gb': 11}))[0] == 400
        runtime = await create_pool(keys['database_url'], dns_fallback=True)
        try:
            for table in ['admin_users', 'app_roles', 'role_events']:
                assert await runtime.fetchval('SELECT has_table_privilege(current_user,$1,\'SELECT\')', table)
                for privilege in ['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE']:
                    assert not await runtime.fetchval('SELECT has_table_privilege(current_user,$1,$2)', table, privilege)
            for role in ['anon', 'authenticated']:
                assert not await runtime.fetchval("SELECT has_function_privilege($1,'web_terminal.change_application_role(text,uuid,text,text)','EXECUTE')", role)
            print('PASS: runtime cannot write roles/audit directly; browser DB roles cannot execute role changes', flush=True)
            # Use a rollback-only transaction for two small allocations. No queued
            # jobs or committed fixture Boxes can reach the cloud worker.
            async with runtime.acquire() as conn:
                transaction = conn.transaction()
                await transaction.start()
                try:
                    await conn.execute('UPDATE boxes SET cpus=1,memory_mib=2048 WHERE id=$1', boxes[1])
                    await conn.execute("""INSERT INTO boxes(id,owner_id,name,provider_name,status,desired_state,cpus,memory_mib,disk_size_gb)
                        VALUES($1,$2,'Quota fixture',$3,'stopped','stopped',1,2048,10)""", uuid.uuid4(), users[1], 'quota-fixture-'+uuid.uuid4().hex)
                    totals = await conn.fetchrow('SELECT sum(cpus) AS cpus,sum(memory_mib) AS memory,sum(disk_size_gb) AS disk FROM boxes WHERE owner_id=$1 AND deleted_at IS NULL', users[1])
                    assert tuple(totals.values()) == (2,4096,20)
                finally:
                    await transaction.rollback()
            print('PASS: deployed API rejects over-quota creation; runtime accepts two 1 CPU / 2 GiB / 10 GB Boxes', flush=True)
        finally:
            await runtime.close()
    finally:
        try:
            async with pool.acquire() as conn, conn.transaction():
                await conn.execute('DELETE FROM role_events WHERE actor_id=ANY($1::uuid[]) OR target_id=ANY($1::uuid[])', users)
                for uid in users:
                    for table, column in [('app_roles','user_id'), ('admin_users','user_id'), ('boxes','owner_id'),
                                          ('sessions','user_id'), ('login_events','user_id'), ('profiles','id'),
                                          ('auth.sessions','user_id'), ('auth.identities','user_id'), ('auth.users','id')]:
                        await conn.execute(f'DELETE FROM {table} WHERE {column}=$1', uid)
                current = [tuple(row) for row in await conn.fetch('SELECT user_id,role FROM app_roles ORDER BY user_id')]
            fixture_path.unlink(missing_ok=True)
            assert baseline == current, 'Non-fixture roles changed during acceptance'
            print('Temporary fixtures removed; existing user roles preserved', flush=True)
        finally:
            await pool.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as error:
        import traceback
        frames = traceback.extract_tb(error.__traceback__)
        print('Admin acceptance failed:', type(error).__name__, frames[-1].name if frames else '', frames[-1].lineno if frames else '')
        raise SystemExit(1)
