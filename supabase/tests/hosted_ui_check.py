import asyncio
import json
import os
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright
from supabase.app import Gateway
from supabase.config import STATE, load_private

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'build' / 'ui-preview'


async def main():
    OUT.mkdir(exist_ok=True)
    hosted_html = (await Gateway.home(None, None)).text
    script = (ROOT / 'supabase/hosted.js').read_text()
    if os.environ.get('WT_UI_LIVE'):
        base = load_private(STATE / 'manager.json', {})['entry_url'].rstrip('/')
        with urllib.request.urlopen(base + '/hosted.js', timeout=30) as response:
            assert response.read().decode() == script, 'Deployed script does not match local source'
            assert response.headers.get('Cache-Control') == 'no-store'
        with urllib.request.urlopen(base + '/', timeout=30) as response:
            hosted_html = response.read().decode()
        with urllib.request.urlopen(base + '/healthz', timeout=30) as response:
            assert response.read().strip() == b'ok'
        print('PASS: deployed script matches source; HTTPS entry and health are available')
    session = {'user': {'name': 'Eric Chen', 'email': 'eric.chen@example.com'}, 'csrf': 'preview'}
    hosted = True
    logout_headers = []
    boxes = []
    created = []
    ports = []
    port_requests = []

    async def serve(route):
        path = urlsplit(route.request.url).path
        if urlsplit(route.request.url).hostname != 'ui.example.test':
            await route.abort()
        elif path == '/':
            html = hosted_html if hosted else (ROOT / 'hangar/control.html').read_text()
            await route.fulfill(body=html, content_type='text/html')
        elif path == '/hosted.js':
            await route.fulfill(body=script, content_type='application/javascript')
        elif path == '/api/session':
            await route.fulfill(json=session)
        elif path == '/api/status':
            await route.fulfill(json={'cloud': {'ready': True}})
        elif path == '/api/boxes' and route.request.method == 'POST':
            created.append(route.request.post_data_json)
            await route.fulfill(status=202, json={'job': {'id': 'job-1', 'box_id': 'box-1', 'action': 'create', 'status': 'running'}})
        elif path == '/api/boxes':
            await route.fulfill(json={'boxes': boxes})
        elif path.startswith('/api/boxes/') and '/ports' in path:
            port_requests.append((route.request.method, path, route.request.headers.get('x-csrf-token')))
            if route.request.method == 'POST':
                port = route.request.post_data_json['port']
                if port == 3001:
                    await route.fulfill(status=409, json={'error': 'Port 3001 is the terminal; it stays behind your sign-in'})
                    return
                ports.append({'port': port, 'url': f'https://p{port}.tunnel.example.test/', 'created_at': '2026-10-24T00:00:00+00:00'})
                await route.fulfill(status=201, json={'port': ports[-1]})
            elif route.request.method == 'DELETE':
                closed = int(path.rsplit('/', 1)[1])
                ports[:] = [item for item in ports if item['port'] != closed]
                await route.fulfill(json={'closed': closed})
            else:
                await route.fulfill(json={'ports': sorted(ports, key=lambda item: item['port'])})
        elif path == '/auth/logout':
            logout_headers.append(route.request.headers)
            session['user'] = None
            await route.fulfill(json={})
        else:
            await route.fulfill(status=404)

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel='chrome', headless=True)
        page = await browser.new_page(viewport={'width': 1440, 'height': 1000}, device_scale_factor=1)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.route('**/*', serve)
        await page.goto('https://ui.example.test/')
        await page.get_by_role('button', name='SIGN OUT', exact=True).wait_for()
        assert await page.locator('.runtime button').all_text_contents() == ['CLOUD', 'LOCAL']
        assert await page.locator('#btn-local').is_disabled()
        assert await page.locator('#btn-cloud').get_attribute('class') == 'active'
        assert await page.locator('header .sys').inner_text() and 'LOCAL DISABLED' in await page.locator('header .sys').inner_text()
        assert await page.locator('header #hosted-auth').count() == 1
        assert await page.locator('.hosted-account-name').inner_text() == 'Eric Chen'
        assert await page.locator('#btn-create').is_enabled()
        assert await page.locator('#res-cpus').input_value() == '1'
        assert await page.locator('#res-memory').input_value() == '2048'
        # Gemini is opt-in per Box and reaches the API as a boolean.
        gemini = page.get_by_role('checkbox', name='TRY GEMINI FREE TIER')
        assert not await gemini.is_checked()
        assert 'paste the code' in await page.locator('#hosted-gemini-note').inner_text()
        await page.locator('#btn-create').click()
        await gemini.check()
        await page.locator('#btn-create').click()
        for _ in range(50):
            if len(created) == 2:
                break
            await page.wait_for_timeout(100)
        assert [body.get('gemini') for body in created] == [False, True], created
        assert all(body['runtime'] == 'cloud' and body['cpus'] == 1 for body in created), created
        await gemini.uncheck()
        await page.screenshot(path=str(OUT/'create-gemini-option.png'))
        boxes.append({'id': '11111111-2222-4333-8444-555555555555', 'name': 'Small terminal', 'status': 'running',
                      'cpus': 1, 'memory_mib': 2048, 'disk_size_gb': 10, 'url': None, 'access_url': '/terminal/example/'})
        await page.locator('#btn-refresh').click()
        await page.locator('[data-act="open"]').wait_for()
        assert await page.locator('#btn-create').is_enabled()
        assert await page.locator('[data-act="open"]').is_enabled()
        assert await page.locator('[data-act="copy"]').is_enabled()
        await page.evaluate('''() => {
            window.openedUrl = null; window.copiedUrl = null;
            window.open = url => { window.openedUrl = url; };
            Object.defineProperty(navigator, 'clipboard', {value: {writeText: async url => { window.copiedUrl = url; }}, configurable: true});
        }''')
        await page.locator('[data-act="open"]').click()
        assert await page.evaluate('window.openedUrl') == '/terminal/example/'
        await page.locator('[data-act="copy"]').click()
        assert await page.evaluate('window.copiedUrl') == 'https://ui.example.test/terminal/example/'
        # Box details: expose public ports, list them, copy and close them.
        await page.get_by_role('button', name='DETAILS', exact=True).click()
        details = page.locator('#hosted-details')
        await details.wait_for()
        assert 'Small terminal' in await details.locator('dl').inner_text()
        await page.locator('.hosted-port-empty').wait_for()
        assert 'without signing in' in await details.inner_text()
        for port in ('8000', '5173'):
            await page.locator('#hosted-port-input').fill(port)
            await details.get_by_role('button', name='EXPOSE', exact=True).click()
            await page.wait_for_function(f'document.querySelector("#hosted-port-list").textContent.includes("p{port}.tunnel")')
        assert await page.locator('#hosted-port-list li b').all_text_contents() == ['5173', '8000']
        link = page.locator('#hosted-port-list a').first
        assert await link.get_attribute('href') == 'https://p5173.tunnel.example.test/'
        assert await link.get_attribute('target') == '_blank'
        await page.locator('#hosted-port-list button[data-action="copy"][data-port="8000"]').click()
        assert await page.evaluate('window.copiedUrl') == 'https://p8000.tunnel.example.test/'
        await page.locator('#hosted-port-input').fill('3001')
        await details.get_by_role('button', name='EXPOSE', exact=True).click()
        await page.wait_for_function('document.querySelector("#hosted-port-error").textContent.includes("3001")')
        await page.locator('#hosted-port-list button[data-action="close"][data-port="5173"]').click()
        await page.wait_for_function('document.querySelectorAll("#hosted-port-list li").length === 1')
        assert all(token == 'preview' for method, _, token in port_requests if method != 'GET'), port_requests
        assert ('DELETE', '/api/boxes/11111111-2222-4333-8444-555555555555/ports/5173', 'preview') in port_requests
        for width in (1440, 390, 320):
            await page.set_viewport_size({'width': width, 'height': 900})
            assert await details.evaluate('el => el.scrollWidth <= el.clientWidth && el.getBoundingClientRect().right <= innerWidth')
            await page.screenshot(path=str(OUT/f'box-details-{width}.png'))
        await page.set_viewport_size({'width': 1440, 'height': 1000})
        await details.get_by_role('button', name='CLOSE', exact=True).click()
        assert await details.is_hidden()
        for width in (1440, 1024, 768, 390, 320):
            await page.set_viewport_size({'width': width, 'height': 900})
            assert await page.locator('.row-actions button').count() == 6
            assert await page.locator('.row-actions').evaluate('el => [...el.children].every(b => b.getBoundingClientRect().left >= el.getBoundingClientRect().left - 1 && b.getBoundingClientRect().right <= el.getBoundingClientRect().right + 1 && b.scrollWidth <= b.clientWidth)')
            await page.screenshot(path=str(OUT/f'fleet-actions-{width}.png'), full_page=True)
        await page.set_viewport_size({'width': 1440, 'height': 1000})
        await page.locator('#res-memory').fill('3072')
        assert await page.locator('#btn-create').is_disabled()
        await page.locator('#res-memory').fill('2048')
        boxes.append({**boxes[0], 'id': '22222222-2222-4333-8444-555555555555', 'status': 'stopped'})
        await page.locator('#btn-refresh').click()
        await page.wait_for_function('document.getElementById("fleet-count").textContent === "2"')
        assert await page.locator('#btn-create').is_disabled()
        # A stopped Box lists its ports but cannot expose new ones.
        await page.locator('[data-hosted-details="22222222-2222-4333-8444-555555555555"]').click()
        await page.locator('#hosted-port-list li').first.wait_for()
        assert await page.locator('#hosted-port-input').is_disabled()
        await page.locator('#hosted-details').get_by_role('button', name='CLOSE', exact=True).click()
        boxes.clear()
        await page.locator('#btn-refresh').click()
        await page.wait_for_function('document.getElementById("fleet-count").textContent === "0"')
        assert await page.locator('#hosted-admin-link').is_hidden()
        session['user']['is_admin'] = True
        await page.evaluate('HANGAR_HOSTED.refresh()')
        assert await page.locator('#hosted-admin-link').is_visible()
        assert await page.locator('#hosted-admin-link').get_attribute('href') == '/admin/'
        session['user']['is_admin'] = False
        await page.evaluate('HANGAR_HOSTED.refresh()')
        assert await page.locator('#hosted-admin-link').is_hidden()
        await page.get_by_role('button', name='SIGN OUT', exact=True).focus()
        await page.evaluate('HANGAR_HOSTED.refresh()')
        assert await page.evaluate('document.activeElement.textContent') == 'SIGN OUT'
        await page.locator('body').click(position={'x': 1, 'y': 1})
        await page.screenshot(path=str(OUT / 'desktop.png'), full_page=True)
        session['user'] = {'name': 'A Very Long Account Display Name For Layout Review', 'email': 'a.long.google.email.address@example.com'}
        await page.evaluate('HANGAR_HOSTED.refresh()')
        for width in (1024, 768, 390, 320):
            await page.set_viewport_size({'width': width, 'height': 900})
            assert await page.locator('#hosted-auth').evaluate('(el) => el.scrollWidth <= el.clientWidth')
            bounds = await page.locator('#hosted-auth').bounding_box()
            assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= width, (width, bounds)
            await page.screenshot(path=str(OUT / f'account-{width}.png'), full_page=True)
        await page.get_by_role('button', name='SIGN OUT', exact=True).click()
        await page.get_by_role('button', name='SIGN IN WITH GOOGLE').wait_for()
        assert logout_headers[0]['x-csrf-token'] == 'preview'
        assert await page.locator('#btn-create').is_disabled()
        await page.screenshot(path=str(OUT / 'guest-mobile.png'), full_page=True)
        await page.set_viewport_size({'width': 1440, 'height': 1000})
        await page.screenshot(path=str(OUT / 'guest-desktop.png'), full_page=True)
        hosted = False
        await page.reload()
        await page.wait_for_function('document.getElementById("cloud-status").textContent === "READY"')
        assert await page.locator('.runtime button').all_text_contents() == ['LOCAL', 'CLOUD']
        assert await page.locator('#btn-local').is_enabled()
        assert await page.locator('#hosted-auth').count() == 0
        assert 'LOCAL READY' in await page.locator('header .sys').inner_text()
        assert not errors, errors
        await browser.close()
    print('PASS: hosted quota inputs, two-Box capacity, six unclipped actions, Box details with public ports, authenticated OPEN/COPY, login/logout, standalone layout')


asyncio.run(main())
