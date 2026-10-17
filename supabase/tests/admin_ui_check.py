import asyncio
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]
NOW = '2026-10-17T09:00:00+00:00'
ROW = {'id': 'dce21551-dfff-4992-99b0-d06fd735533a', 'box_id': '33f05bbc-d093-44e5-a6ea-dbc1e5f42933',
       'name': 'research-terminal', 'box_name': 'research-terminal', 'owner_email': 'eric@example.com',
       'owner_name': 'Eric Chen', 'email': 'eric@example.com', 'display_name': 'Eric Chen',
       'status': 'running', 'box_status': 'running', 'cpus': 2, 'memory_mib': 4096, 'disk_size_gb': 10,
       'created_at': NOW, 'updated_at': NOW, 'last_login_at': NOW, 'occurred_at': NOW,
       'operation': 'create', 'attempts': 1, 'event': 'operation_completed', 'to_state': 'running',
       'action': 'login', 'source': 'application'}


async def main():
    status = 200
    owner_role = 'admin'
    member = {'user_id': ROW['id'], 'email': 'colleague@example.com', 'display_name': '<img src=x onerror=alert(1)>', 'role': 'user', 'updated_at': NOW, 'verified': True}
    history = []
    queries = []
    async def serve(route):
        parsed = urlsplit(route.request.url)
        if parsed.path in ('/admin/', '/admin.js'):
            file = ROOT / 'supabase' / ('admin.html' if parsed.path == '/admin/' else 'admin.js')
            await route.fulfill(body=file.read_text(), content_type='text/html' if parsed.path == '/admin/' else 'application/javascript')
        elif parsed.path == '/api/admin/overview':
            await route.fulfill(status=status, json={'users': 42, 'logins_24h': 18,
                'viewer': {'email': 'eric@example.com', 'role': owner_role}, 'boxes': {'running': 12, 'stopped': 8, 'deleted': 6, 'error': 1},
                'jobs': {'queued': 2, 'running': 1, 'failed': 1}, 'expired_job_leases': 0,
                'allocated': {'cpus': 42, 'memory_mib': 86016, 'disk_size_gb': 210}})
        elif parsed.path == '/api/session':
            await route.fulfill(json={'user': {'role': owner_role}, 'csrf': 'test-csrf'})
        elif parsed.path == '/api/admin/members/lookup':
            assert route.request.headers['x-csrf-token'] == 'test-csrf'
            await route.fulfill(json=member)
        elif parsed.path == '/api/admin/members':
            await route.fulfill(json={'items': [member] if member['role'] != 'user' else []})
        elif parsed.path == '/api/admin/role-events':
            await route.fulfill(json={'items': history, 'total': len(history), 'page': 1, 'page_size': 25})
        elif parsed.path.startswith('/api/admin/members/'):
            assert route.request.method == 'PUT'
            assert route.request.headers['x-csrf-token'] == 'test-csrf'
            data = route.request.post_data_json
            assert data['expected_role'] == member['role']
            history.insert(0, {'actor_email': 'eric@example.com', 'target_email': member['email'], 'occurred_at': NOW,
                              'previous_role': member['role'], 'new_role': data['role'], 'source': 'dashboard'})
            member['role'] = data['role']
            await route.fulfill(json={'role': member['role']})
        elif parsed.path.startswith('/api/admin/'):
            query = parse_qs(parsed.query)
            queries.append((parsed.path, query))
            item = dict(ROW)
            if query.get('search'):
                item['name'] = '<img src=x onerror="window.injected=true">'
            await route.fulfill(status=status, json={'items': [item], 'total': 26, 'page': int(query.get('page', ['1'])[0]), 'page_size': 25})
        else:
            await route.fulfill(status=404)
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel='chrome', headless=True)
        page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.route('**/*', serve)
        await page.goto('https://ui.example.test/admin/')
        await page.wait_for_function('document.getElementById("boxes").textContent === "21"')
        await page.wait_for_function('document.getElementById("page-label").textContent.includes("26 records")')
        assert await page.locator('#boxes').inner_text() == '21'
        assert await page.locator('#stopped').inner_text() == '8'
        assert await page.locator('a[href*="/terminal/"]').count() == 0
        assert await page.locator('[data-section]').count() == 0
        assert await page.locator('tbody button').count() == 0
        assert await page.locator('#membership').is_hidden()
        await page.screenshot(path=str(ROOT/'build/ui-preview/admin-desktop.png'), full_page=True)
        await page.locator('#next').click()
        await page.wait_for_function('document.getElementById("page-label").textContent.includes("Page 2")')
        assert await page.locator('#next').is_disabled()
        await page.locator('#status').select_option('stopped')
        await page.wait_for_function('document.getElementById("page-label").textContent.includes("Page 1")')
        assert queries[-1][1]['status'] == ['stopped']
        await page.locator('#search').fill('literal')
        await page.wait_for_function('document.querySelector("tbody").textContent.includes("<img")')
        assert await page.locator('tbody img').count() == 0
        assert not await page.evaluate('Boolean(window.injected)')
        await page.locator('#search').fill('')
        await page.wait_for_function('!document.querySelector("tbody").textContent.includes("<img")')
        for width in (390, 320):
            await page.set_viewport_size({'width': width, 'height': 900})
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            await page.screenshot(path=str(ROOT/f'build/ui-preview/admin-mobile-{width}.png'), full_page=True)
        owner_role = 'owner'
        await page.set_viewport_size({'width': 1440, 'height': 1100})
        await page.reload()
        await page.locator('#membership').wait_for()
        await page.locator('#member-email').fill(member['email'])
        await page.get_by_role('button', name='FIND USER').click()
        await page.locator('#candidate select').wait_for()
        assert await page.locator('#candidate img').count() == 0
        page.on('dialog', lambda dialog: dialog.accept())
        await page.locator('#candidate select').select_option('admin')
        await page.locator('#candidate button').click()
        await page.wait_for_function('document.querySelector("#member-records tbody").textContent.includes("colleague@example.com")')
        assert len(history) == 1 and member['role'] == 'admin'
        await page.screenshot(path=str(ROOT/'build/ui-preview/owner-desktop.png'), full_page=True)
        await page.locator('#member-records select').select_option('user')
        await page.locator('#member-records button').click()
        await page.wait_for_function('document.querySelector("#member-records tbody").children.length === 0')
        assert member['role'] == 'user' and len(history) == 2
        for width in (390, 320):
            await page.set_viewport_size({'width': width, 'height': 900})
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        owner_role = 'admin'
        await page.locator('#refresh').click()
        await page.wait_for_function('document.getElementById("membership").hidden')
        assert await page.locator('#role-history tbody tr').count() == 0
        status = 403
        await page.locator('#refresh').click()
        await page.wait_for_function('document.getElementById("boxes").textContent === "—"')
        assert 'research-terminal' not in await page.locator('body').inner_text()
        assert 'eric@example.com' not in await page.locator('body').inner_text()
        assert await page.locator('#next').is_disabled()
        assert await page.locator('#reauth').is_hidden()
        status = 401
        await page.reload()
        await page.locator('#reauth').wait_for()
        assert await page.locator('#boxes').inner_text() == '—'
        assert not errors, errors
        await browser.close()
    print('PASS: Box overview, owner-only grant/revoke UI, audit history, CSRF headers, XSS text, responsive layout, and revocation clearing')


asyncio.run(main())
