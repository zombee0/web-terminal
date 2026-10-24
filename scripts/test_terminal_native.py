"""Touch the real tmux copy-mode screen through the production WebSocket."""
import asyncio
import json
from pathlib import Path
import shlex
import shutil
import sys
import tempfile

from playwright.async_api import async_playwright
from test_terminal_ui import OUT, Touch, assets, point

SCREEN = "() => Array.from({length:testTerm.rows}, (_,i) => testTerm.buffer.active.getLine(testTerm.buffer.active.viewportY+i).translateToString(true)).join('\\n')"


async def check(browser, sources, base, mode):
    engine = browser.browser_type.name
    with tempfile.TemporaryDirectory(prefix="native-tmux-") as directory:
        socket = str(Path(directory) / "tmux.sock")
        config = Path(directory) / "tmux.conf"
        config.write_text(f"set -g history-limit 1000\nset -g status off\nset -g prefix C-a\nset -g mouse off\nset -g mode-keys {mode}\n")

        async def tmux(*args):
            process = await asyncio.create_subprocess_exec(shutil.which("tmux"), "-S", socket,
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await process.communicate()
            if process.returncode:
                raise RuntimeError(stderr.decode())
            return stdout.decode().rstrip("\n")

        context = await browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        await context.add_init_script("""Object.defineProperty(navigator,'clipboard',{value:{
            writeText:async text=>{window.copied=text},readText:async()=>''}})""")
        page = await context.new_page()
        page.set_default_timeout(12000)
        await context.route("https://cdn.jsdelivr.net/**", lambda route: route.fulfill(
            body=sources[route.request.url],
            content_type="text/css" if route.request.url.endswith(".css") else "application/javascript"))
        touch = Touch(page, await context.new_cdp_session(page) if engine == "chromium" else None)
        try:
            await page.goto(base)
            await page.wait_for_function(f"window.testTerm && ({SCREEN})().includes('fixture>')")
            command = f"tmux -S {shlex.quote(socket)} -f {shlex.quote(str(config))} new-session -s native 'bash --noprofile --norc -i'"
            await page.locator(".xterm-helper-textarea").focus()
            await page.keyboard.insert_text(command)
            await page.keyboard.press("Enter")
            for _ in range(100):
                if Path(socket).exists():
                    break
                await asyncio.sleep(.05)
            data = Path(directory) / "rows.txt"
            data.write_text("".join(f"\033[32mROW-{i:04d} 中文缩进\033[0m\n" for i in range(250)))
            await tmux("send-keys", "-t", "native", f"cat {shlex.quote(str(data))}", "Enter")
            await page.wait_for_function(f"({SCREEN})().includes('ROW-0249')")
            await page.locator(".xterm-helper-textarea").focus()
            await page.keyboard.type("UNSENT-DRAFT")
            for _ in range(6):
                await page.keyboard.press("ArrowLeft")
            before = await tmux("display-message", "-p", "-t", "native", "#{cursor_x}|#{cursor_y}")
            # A more recently attached browser must not become the scroll target.
            await tmux("new-session", "-d", "-s", "other", "bash --noprofile --norc -i")
            other = await context.new_page()
            await other.goto(base)
            await other.wait_for_function(f"window.testTerm && ({SCREEN})().includes('fixture>')")
            await other.locator(".xterm-helper-textarea").focus()
            await other.keyboard.insert_text(f"tmux -S {shlex.quote(socket)} attach -t other")
            await other.keyboard.press("Enter")
            await other.wait_for_function("testTerm.buffer.active.type === 'alternate'")
            await page.bring_to_front()
            await page.get_by_role("button", name="Browse output", exact=True).click()
            await page.wait_for_function("document.querySelector('#browse-output').textContent === 'Back to terminal'")
            assert await page.locator("dialog[open]").count() == 0
            assert await tmux("display-message", "-p", "-t", "native", "#{pane_mode}") == "copy-mode"
            assert await tmux("display-message", "-p", "-t", "other", "#{pane_in_mode}") == "0"
            assert await tmux("show-options", "-gv", "prefix") == "C-a"
            assert await tmux("show-options", "-gv", "mouse") == "off"

            async def position():
                return int(await tmux("display-message", "-p", "-t", "native", "#{scroll_position}") or 0)

            x, y = await point(page, 10, 12)
            await touch.swipe(x, y, y + 240)
            await page.wait_for_timeout(350)
            older = await position()
            assert older >= 8, f"swipe did not scroll history: {older}"
            await touch.swipe(x, y + 240, y + 100)
            await page.wait_for_timeout(350)
            assert await position() < older
            # capture-pane -M exposes the mode's backing grid, not its viewport.
            # Read the original rows at tmux's reported scroll offset instead.
            offset = await position()
            height = int(await tmux("display-message", "-p", "-t", "native", "#{pane_height}"))
            captured = await tmux("capture-pane", "-p", "-S", str(-offset),
                                  "-E", str(height - offset - 1), "-t", "native")
            (OUT / f"native-{mode}-capture.txt").write_text(captured)
            (OUT / f"native-{mode}-screen.txt").write_text(await page.evaluate(SCREEN))
            await page.wait_for_function(f"""expected => {{
                const actual = ({SCREEN})().split('\\n').map(row=>row.trimEnd());
                const rows = expected.split('\\n').map(row=>row.trimEnd());
                return actual.length === rows.length && actual[0].startsWith(rows[0]) &&
                    actual.slice(1).every((row,i)=>row===rows[i+1]);
            }}""", arg=captured)
            assert await page.evaluate("""() => {
                for(let y=1;y<testTerm.rows;y++) {
                    const l=testTerm.buffer.active.getLine(y);
                    if(l.translateToString(true).includes('ROW-')) return l.getCell(0).getFgColor()===2;
                } return false;
            }""")
            x, y = await point(page, 3, 3)
            await touch.hold(x, y)
            await page.get_by_role("button", name="Copy", exact=True).click()
            assert "ROW-" in await page.evaluate("window.copied")
            await page.screenshot(path=str(OUT / f"native-tmux-{engine}-{mode}.png"))
            # At tmux's oldest row, further swipes stay in copy mode at the top.
            history = int(await tmux('display-message', '-p', '-t', 'native', '#{history_size}'))
            for _ in range(20):
                if await position() == history:
                    break
                await page.keyboard.press('PageUp')
                await page.wait_for_timeout(100)
            assert await position() == history
            await touch.swipe(x, y, y + 160)
            await page.wait_for_timeout(350)
            assert await position() == history
            assert await tmux("display-message", "-p", "-t", "native", "#{pane_mode}") == "copy-mode"
            assert await page.locator('#browse-status').text_content() == 'Top of history'
            assert await page.locator('dialog[open]').count() == 0
            await page.get_by_role("button", name="Back to terminal", exact=True).click()
            await page.get_by_role("button", name="Browse output", exact=True).click()
            await page.wait_for_function("document.querySelector('#browse-output').textContent === 'Back to terminal'")
            # Escape exits only copy mode, then the draft/caret is still usable.
            await page.keyboard.press("Escape")
            await page.wait_for_function("document.querySelector('#browse-output').textContent === 'Browse output'")
            assert await tmux("display-message", "-p", "-t", "native", "#{cursor_x}|#{cursor_y}") == before
            await page.locator(".xterm-helper-textarea").focus()
            await page.keyboard.type("-CONTINUED")
            await page.wait_for_function(f"({SCREEN})().includes('UNSENT-CONTINUED-DRAFT')")
            # Mouse-on and mouse-off follow the same explicit copy-mode commands.
            await tmux("set-option", "-g", "mouse", "on")
            await page.get_by_role("button", name="Browse output", exact=True).click()
            await page.wait_for_function("document.querySelector('#browse-output').textContent === 'Back to terminal'")
            await touch.swipe(x, 200, 500)
            await page.wait_for_timeout(350)
            assert await position() > 0
            await page.get_by_role("button", name="Back to terminal", exact=True).click()
            await page.wait_for_function("document.querySelector('#browse-output').textContent === 'Browse output'")
            assert await tmux("display-message", "-p", "-t", "native", "#{pane_in_mode}") == "0"
            # The real, nondefault prefix also enters the same mobile controls.
            await page.locator(".xterm-helper-textarea").focus()
            await page.keyboard.press("Control+a")
            await page.keyboard.type("[")
            await page.wait_for_function("document.querySelector('#browse-output').textContent === 'Back to terminal'")
            await page.get_by_role("button", name="Back to terminal", exact=True).click()
            await page.wait_for_function("document.querySelector('#browse-output').textContent === 'Browse output'")
            await page.get_by_role("button", name="Browse output", exact=True).click()
            await page.wait_for_function("document.querySelector('#browse-output').textContent === 'Back to terminal'")
            await page.close()
            for _ in range(100):
                if await tmux("display-message", "-p", "-t", "native", "#{pane_in_mode}") == "0":
                    break
                await asyncio.sleep(.05)
            assert await tmux("display-message", "-p", "-t", "native", "#{pane_in_mode}") == "0"
            assert await tmux("display-message", "-p", "-t", "other", "#{pane_in_mode}") == "0"

            # A full-screen program (as Codex or Claude Code may use) covers the
            # shell's output. tmux still keeps that output as pane history, so
            # copy mode scrolls back to it, and leaving restores the program.
            page = other
            await page.bring_to_front()
            data.write_text("".join(f"ALT-{i:04d} 中文输出\n" for i in range(400)))
            await tmux("send-keys", "-t", "other",
                       f"cat {shlex.quote(str(data))}; printf '\\033[?1049h\\033[HFULL-SCREEN-APP'", "Enter")
            await page.wait_for_function(f"({SCREEN})().includes('FULL-SCREEN-APP')")
            alternate = await tmux("display-message", "-p", "-t", "other", "#{alternate_on}")
            assert alternate == "1", f"alternate-screen fixture: {alternate}"
            untouched = await tmux("capture-pane", "-p", "-t", "other")
            caret = await tmux("display-message", "-p", "-t", "other", "#{cursor_x}|#{cursor_y}")
            await page.get_by_role("button", name="Browse output", exact=True).click()
            await page.wait_for_function("document.querySelector('#browse-output').textContent === 'Back to terminal'")
            assert await tmux("display-message", "-p", "-t", "other", "#{pane_mode}") == "copy-mode"
            touch = Touch(page, await context.new_cdp_session(page) if engine == "chromium" else None)
            x, y = await point(page, 10, 6)
            for _ in range(3):
                await touch.swipe(x, y, y + 400)
                await page.wait_for_timeout(350)
            offset = int(await tmux('display-message', '-p', '-t', 'other', '#{scroll_position}'))
            assert offset > 20, f"swipe did not scroll above the full-screen app: {offset}"
            await page.wait_for_function(f"({SCREEN})().includes('ALT-03')")
            await touch.swipe(x, y + 400, y)
            await page.wait_for_timeout(350)
            assert int(await tmux('display-message', '-p', '-t', 'other', '#{scroll_position}')) < offset
            await page.screenshot(path=str(OUT / f"alternate-history-{engine}-{mode}.png"))
            await page.get_by_role('button', name='Back to terminal', exact=True).click()
            await page.wait_for_function("document.querySelector('#browse-output').textContent === 'Browse output'")
            assert await tmux("display-message", "-p", "-t", "other", "#{pane_in_mode}") == "0"
            assert await tmux("capture-pane", "-p", "-t", "other") == untouched
            assert await tmux("display-message", "-p", "-t", "other", "#{cursor_x}|#{cursor_y}") == caret
            print(f"PASS full-screen app {engine}/{mode}: copy mode reaches earlier shell output, bidirectional touch, preserved screen/caret", flush=True)
            print(f"PASS native tmux {engine}/{mode}: real copy mode, bidirectional touch, exact colored rows, long-press copy, top boundary, draft/caret, mouse on/off, client isolation, manual prefix, disconnect cleanup", flush=True)
        except Exception:
            if not page.is_closed():
                await page.screenshot(path=str(OUT / f"native-tmux-{mode}-failure.png"))
            raise
        finally:
            await context.close()
            if Path(socket).exists():
                await tmux("kill-server")


async def main():
    sources = assets()
    async with async_playwright() as playwright:
        for engine, modes in ((playwright.chromium, ("emacs", "vi")), (playwright.webkit, ("emacs",))):
            browser = await engine.launch(**({"channel": "chrome", "headless": True} if engine.name == "chromium" else {"headless": True}))
            try:
                for mode in modes:
                    await check(browser, sources, sys.argv[1], mode)
            finally:
                await browser.close()
    (OUT / "native-tmux-report.json").write_text(json.dumps({
        "real_tmux": True, "modes": ["emacs", "vi"], "mobile_viewport": [390, 844],
        "native_copy_mode": True, "exact_screen": True, "draft_and_caret_preserved": True,
        "bidirectional_touch": True, "long_press_copy": True,
        "engines": ["chromium", "webkit"], "client_isolation": True,
        "manual_prefix": True, "disconnect_cleanup": True,
        "alternate_screen_history": True, "single_browse_entry": True,
        "stays_in_copy_mode_at_top": True,
    }, indent=2) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
