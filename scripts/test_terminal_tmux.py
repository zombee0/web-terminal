"""Mobile paging against a real, isolated tmux server and shell PTY."""

import asyncio
import os
from pathlib import Path
import shlex
import shutil
import tempfile

from aiohttp import web
from playwright.async_api import async_playwright

from test_terminal_apps import Fixture, HISTORY, SCREEN, VISIBLE_HISTORY
from test_terminal_ui import HTML, OUT, assets


async def acceptance(browser, sources, mode):
    with tempfile.TemporaryDirectory(prefix="terminal-tmux-") as directory:
        cwd = Path(directory)
        socket = str(cwd / "socket")
        config = cwd / "tmux.conf"
        config.write_text(f"set -g history-limit 2000\nset -g mode-keys {mode}\n")
        seed = cwd / "seed.sh"
        seed.write_text("printf '%s\\n' " + shlex.quote(HISTORY) + "\n"
                        "export PS1='fixture> ' HISTFILE=/dev/null\n"
                        "exec /bin/bash --noprofile --norc -i\n")
        fixture = Fixture(cwd)
        fixture.command = [shutil.which("tmux"), "-S", socket, "-f", str(config),
                           "new-session", "-s", "fixture", "/bin/bash " + shlex.quote(str(seed))]

        async def tmux(*args):
            process = await asyncio.create_subprocess_exec(shutil.which("tmux"), "-S", socket, *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await process.communicate()
            if process.returncode:
                raise RuntimeError(stderr.decode())
            return stdout.decode().strip()

        server = web.Application()

        async def root(request):
            return web.Response(text=HTML, content_type="text/html")

        server.router.add_get("/", root)
        server.router.add_get("/ws", fixture.terminal)
        runner = web.AppRunner(server)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        context = await browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        page = await context.new_page()
        page.set_default_timeout(10000)

        async def serve_asset(route):
            await route.fulfill(body=sources[route.request.url],
                content_type="text/css" if route.request.url.endswith(".css") else "application/javascript")

        await page.route("https://cdn.jsdelivr.net/**", serve_asset)
        try:
            await page.goto(f"http://127.0.0.1:{port}")
            await page.wait_for_function("window.testTerm !== undefined")
            await page.wait_for_function(f"({SCREEN})().includes('fixture>')")
            assert "HISTORY-0000" not in await page.evaluate(SCREEN)
            assert int(await tmux("display-message", "-p", "-t", "fixture:0.0", "#{history_size}")) > 50
            await page.locator(".xterm-helper-textarea").focus()
            await page.keyboard.type("UNSENT-DRAFT")
            await page.wait_for_function(f"({SCREEN})().includes('UNSENT-DRAFT')")
            for _ in range(6):
                await page.keyboard.press("ArrowLeft")
            await page.get_by_role("button", name="Browse output", exact=True).click()
            await page.get_by_role("combobox", name="Output source").select_option("tmux")
            await page.wait_for_function("document.querySelector('#output-history').textContent.includes('HISTORY-')")
            scroll = page.locator("#output-scroll")
            previous = page.get_by_role("button", name="Previous page", exact=True)
            following = page.get_by_role("button", name="Next page", exact=True)
            seen = set()
            for _ in range(80):
                seen.update(await page.evaluate(VISIBLE_HISTORY))
                if 0 in seen:
                    break
                before = await page.locator("#output-history").text_content()
                position = await scroll.evaluate("e => e.scrollTop")
                await previous.click()
                await page.wait_for_function("""({before, position}) =>
                  document.querySelector('#output-history').textContent !== before ||
                  document.querySelector('#output-scroll').scrollTop !== position""",
                  arg=dict(before=before, position=position))
            assert seen == set(range(120)), sorted(seen)
            assert await tmux("display-message", "-p", "-t", "fixture:0.0", "#{pane_in_mode}") == "1"
            await page.screenshot(path=str(OUT / f"real-tmux-{mode}-earliest.png"))

            # Next must retrieve a newer tmux page, not merely move the DOM.
            await scroll.evaluate("e => { e.scrollTop = e.scrollHeight; }")
            before = await page.locator("#output-history").text_content()
            await following.click()
            await page.wait_for_function("before => document.querySelector('#output-history').textContent !== before", arg=before)
            assert 0 not in await page.evaluate(VISIBLE_HISTORY)
            await page.get_by_role("button", name="Latest", exact=True).click()
            await page.wait_for_function(f"({VISIBLE_HISTORY})().includes(119)")
            await page.get_by_role("button", name="Back to terminal", exact=True).click()
            await page.wait_for_function(f"({SCREEN})().includes('UNSENT-DRAFT')")
            # Wait for copy-mode to exit before continuing the shell draft.
            for _ in range(50):
                if await tmux("display-message", "-p", "-t", "fixture:0.0", "#{pane_in_mode}") == "0":
                    break
                await asyncio.sleep(.02)
            else:
                raise AssertionError("Back to terminal left tmux in copy mode")
            await page.locator(".xterm-helper-textarea").focus()
            await page.keyboard.type("-CONTINUED")
            await page.wait_for_function(f"({SCREEN})().includes('UNSENT-CONTINUED-DRAFT')")
            await page.screenshot(path=str(OUT / f"real-tmux-{mode}-returned.png"))
            print(f"PASS real tmux {mode}: {len(seen)} visible history lines, Previous/Next/Latest, preserved draft/caret", flush=True)
        except Exception:
            await page.screenshot(path=str(OUT / f"real-tmux-{mode}-failure.png"))
            raise
        finally:
            await context.close()
            await runner.cleanup()
            if Path(socket).exists():
                await tmux("kill-server")


async def main():
    binary = shutil.which("tmux")
    if not binary:
        raise RuntimeError("tmux must be installed for native history acceptance")
    process = await asyncio.create_subprocess_exec(binary, "-V", stdout=asyncio.subprocess.PIPE)
    stdout, _ = await process.communicate()
    print(stdout.decode().strip(), flush=True)
    sources = assets()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=True)
        try:
            for mode in os.environ.get("TERMINAL_TEST_TMUX_MODES", "emacs,vi").split(","):
                if mode not in ("emacs", "vi"):
                    raise ValueError("Unknown tmux mode-key style")
                await acceptance(browser, sources, mode)
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
