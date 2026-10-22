"""Exercise the shipped page with real xterm in isolated mobile/desktop browsers.

Run with make test-terminal-ui. Requires Playwright (as do the hosted UI tests),
Chrome and Playwright WebKit. CDN assets are cached under build/terminal-ui.
The websocket/clipboard fixtures never connect to a Box or read real clipboards.
"""

import asyncio
import re
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "build" / "terminal-ui"
HTML = (OUT / "terminal.html").read_text()
FIXTURE = """() => {
  window.sent = [];
  window.tmux = {available: true, active: false, position: 0, history: 300};
  window.clipboardText = '';
  window.clipboardDenied = false;
  Object.defineProperty(navigator, 'clipboard', {value: {
    readText: async () => {
      if (window.clipboardDenied) throw new DOMException('Denied', 'NotAllowedError');
      return window.clipboardText;
    },
    writeText: async text => {
      if (window.clipboardDenied) throw new DOMException('Denied', 'NotAllowedError');
      window.clipboardText = text;
    }
  }, configurable: true});
  window.WebSocket = class {
    static OPEN = 1;
    constructor(url) {
      this.url = url;
      this.readyState = 1;
      window.socket = this;
      setTimeout(() => {
        this.onopen?.();
        this.onmessage?.({data: JSON.stringify({type: 'browse-ready', tmux: !window.disableTmux})});
      }, 0);
    }
    send(data) {
      if (data instanceof ArrayBuffer) { window.sent.push(new TextDecoder().decode(data)); return; }
      window.sent.push(data);
      if (typeof data !== 'string') return;
      const request = JSON.parse(data);
      if (request.type !== 'tmux-browse') return;
      // A tmux client paired with this browser, as the guest wrapper reports it.
      const t = window.tmux;
      if (request.action === 'enter' && t.available) t.active = true;
      if (request.action === 'leave') { t.active = false; t.position = 0; }
      if (request.action === 'scroll' && t.active) t.position = Math.max(0, Math.min(t.history, t.position - request.lines));
      const reply = {type: 'tmux-browse', id: request.id, available: t.available,
        active: t.available && t.active, position: t.position, history: t.history};
      setTimeout(() => this.onmessage({data: JSON.stringify(reply)}), 5);
    }
  };
}"""


def assets():
    result = {}
    for url in re.findall(r'(?:src|href)="(https://cdn.jsdelivr.net/[^\"]+)"', HTML):
        path = OUT / url.split("/npm/")[1].replace("/", "_")
        if not path.exists():
            with urllib.request.urlopen(url, timeout=30) as response:
                path.write_bytes(response.read())
        data = path.read_text()
        if url.endswith("/lib/xterm.js"):
            data += "\nwindow.Terminal = class extends Terminal { constructor(options) { super(options); window.testTerm = this; } };"
        result[url] = data
    return result


class Touch:
    def __init__(self, page, cdp):
        self.page, self.cdp = page, cdp

    async def event(self, kind, x=0, y=0):
        if self.cdp:
            await self.cdp.send("Input.dispatchTouchEvent", {
                "type": kind,
                "touchPoints": [] if kind in ("touchEnd", "touchCancel") else [{"x": x, "y": y, "id": 1}],
            })
            await self.page.evaluate("() => new Promise(requestAnimationFrame)")
        else:
            # WebKit has no CDP touch input. Exercise its TouchEvent/DOM path;
            # native iOS callouts and keyboard behavior still need a real phone.
            await self.page.evaluate("""({kind, x, y}) => {
              if (kind === 'touchStart') window.touchTarget = document.elementFromPoint(x, y);
              const target = window.touchTarget;
              const touch = {identifier: 1, target, clientX: x, clientY: y, pageX: x, pageY: y};
              const ending = kind === 'touchEnd' || kind === 'touchCancel';
              const event = new Event(kind.toLowerCase(), {bubbles: true, cancelable: true});
              Object.assign(event, {touches: ending ? [] : [touch],
                targetTouches: ending ? [] : [touch], changedTouches: [touch]});
              target.dispatchEvent(event);
            }""", {"kind": kind, "x": x, "y": y})

    async def hold(self, x, y):
        await self.event("touchStart", x, y)
        await self.page.wait_for_timeout(520)
        await self.event("touchEnd", x, y)

    async def swipe(self, x, y, end_y):
        await self.event("touchStart", x, y)
        for step in range(1, 9):
            await self.event("touchMove", x, y + (end_y - y) * step / 8)
        await self.event("touchEnd", x, end_y)


async def write(page, text, reset=True):
    await page.evaluate("""async ({text, reset}) => {
      if (reset) testTerm.reset();
      await new Promise(resolve => testTerm.write(text, resolve));
      await new Promise(requestAnimationFrame);
    }""", {"text": text, "reset": reset})


async def point(page, col, row):
    return await page.evaluate("""({col, row}) => {
      const r = document.querySelector('.xterm-screen').getBoundingClientRect();
      return [r.left + (col + .5) * r.width / testTerm.cols, r.top + (row + .5) * r.height / testTerm.rows];
    }""", {"col": col, "row": row})


async def browse_mode(page, touch=None):
    """Browse output drives tmux copy mode in place: no separate reader."""
    await page.evaluate("window.tmux = {available: true, active: false, position: 0, history: 300}; window.sent = []")
    await page.get_by_role("button", name="Browse output", exact=True).click()
    await page.wait_for_function("document.querySelector('#browse-output').textContent === 'Back to terminal'")
    assert await page.locator("dialog[open]").count() == 0
    assert await page.locator("#browse-status").text_content() == "Swipe to read history"

    async def scroll(older):
        x, y = await point(page, 10, 6)
        if touch:
            # A finger moving down reveals older rows, as in a native list.
            await touch.swipe(x, y, y + 200) if older else await touch.swipe(x, y + 200, y)
        else:
            await page.mouse.move(x, y)
            await page.mouse.wheel(0, -400 if older else 400)

    await scroll(True)
    await page.wait_for_function("tmux.position > 0")
    older = await page.evaluate("tmux.position")
    await scroll(False)
    await page.wait_for_function("older => tmux.position < older", arg=older)
    # At the oldest row, further scrolling keeps the same mode at the top.
    await page.evaluate("tmux.position = tmux.history - 1")
    await scroll(True)
    await page.wait_for_function("tmux.position === tmux.history")
    await page.wait_for_function("document.querySelector('#browse-status').textContent === 'Top of history'")
    await scroll(True)
    await page.wait_for_timeout(150)
    assert await page.evaluate("tmux.active && tmux.position === tmux.history")
    assert await page.locator("dialog[open]").count() == 0
    # Keys and swipes never reach the shell while browsing.
    await page.keyboard.type("x")
    assert await page.evaluate("sent.every(x => x.startsWith('{'))"), await page.evaluate("sent")
    await page.get_by_role("button", name="Back to terminal", exact=True).click()
    await page.wait_for_function("document.querySelector('#browse-output').textContent === 'Browse output'")
    assert not await page.evaluate("tmux.active")
    assert await page.locator("#browse-status").is_hidden()
    # A plain shell has no tmux client to browse.
    await page.evaluate("tmux.available = false")
    await page.get_by_role("button", name="Browse output", exact=True).click()
    await page.wait_for_function("document.querySelector('#notice').textContent.includes('Start tmux')")
    assert await page.locator("#browse-output").text_content() == "Browse output"
    await page.evaluate("tmux.available = true")


async def mobile(browser, name, sources, chromium=False):
    context = await browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, device_scale_factor=2)
    await context.add_init_script(f"({FIXTURE})()")
    page = await context.new_page()
    page.set_default_timeout(10000)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    async def serve(route):
        if route.request.url in sources:
            await route.fulfill(body=sources[route.request.url], content_type="text/css" if route.request.url.endswith(".css") else "application/javascript")
        elif route.request.url == "https://terminal.example.test/terminal/box-a/?access=fixture":
            await route.fulfill(body=HTML, content_type="text/html")
        else:
            await route.abort()

    await page.route("**/*", serve)
    await page.goto("https://terminal.example.test/terminal/box-a/?access=fixture")
    await page.wait_for_function("window.testTerm && testTerm.rows > 5 && window.sent.length > 0")
    assert await page.evaluate("socket.url") == "wss://terminal.example.test/terminal/box-a/ws?access=fixture"
    assert await page.evaluate("socket.binaryType") == "arraybuffer"
    assert not await page.evaluate("document.activeElement === testTerm.textarea"), "Mobile must not autofocus"
    touch = Touch(page, await context.new_cdp_session(page) if chromium else None)
    await browse_mode(page, touch)
    menu = page.locator("#selection-menu")

    # Select a word, then extend the end handle across a CJK/emoji line.
    await write(page, "alpha beta gamma\r\n中文🙂 delta\r\n")
    await touch.hold(*await point(page, 7, 0))
    assert await menu.is_visible()
    assert await page.evaluate("testTerm.unicode.activeVersion") == "11"
    assert await page.evaluate("testTerm.getSelection()") == "beta"
    assert not await page.evaluate("document.activeElement === testTerm.textarea")
    handle = await page.locator("#selection-end").bounding_box()
    start_x, start_y = handle["x"] + handle["width"] / 2, handle["y"] + 18
    cell_width, cell_height = await page.evaluate("""() => {
      const r = document.querySelector('.xterm-screen').getBoundingClientRect();
      return [r.width / testTerm.cols, r.height / testTerm.rows];
    }""")
    await touch.event("touchStart", start_x, start_y)
    target_x, target_y = start_x - 4 * cell_width, start_y + cell_height
    await touch.event("touchMove", target_x, target_y)
    await touch.event("touchEnd", target_x, target_y)
    selected = await page.evaluate("testTerm.getSelection()")
    assert selected.replace("\r\n", "\n") == "beta gamma\n中文🙂", repr(selected)
    await page.screenshot(path=str(OUT / f"{name}-selection.png"))
    await menu.get_by_role("button", name="Copy", exact=True).click()
    assert await page.evaluate("window.clipboardText") == selected
    assert await menu.is_hidden()

    # The start handle can independently expand the selection to the left.
    await touch.hold(*await point(page, 7, 0))
    handle = await page.locator("#selection-start").bounding_box()
    start_x, start_y = handle["x"] + handle["width"] / 2, handle["y"] + 26
    await touch.event("touchStart", start_x, start_y)
    await touch.event("touchMove", start_x - 6 * cell_width, start_y)
    await touch.event("touchEnd", start_x - 6 * cell_width, start_y)
    assert await page.evaluate("testTerm.getSelection()") == "alpha beta"
    await menu.get_by_role("button", name="Select all", exact=True).click()
    assert "中文🙂 delta" in await page.evaluate("testTerm.getSelection()")
    await menu.get_by_role("button", name="Dismiss selection", exact=True).click()

    # Chinese wide cells, wrapped words and a drag above the viewport boundary.
    await touch.hold(*await point(page, 1, 1))
    assert await page.evaluate("testTerm.getSelection()") == "中文🙂"
    await menu.get_by_role("button", name="Dismiss selection", exact=True).click()
    cols = await page.evaluate("testTerm.cols")
    word = "x" * (cols + 5)
    await write(page, word + " tail")
    await touch.hold(*await point(page, 2, 1))
    assert await page.evaluate("testTerm.getSelection()") == word
    await menu.get_by_role("button", name="Dismiss selection", exact=True).click()
    # An end exactly on a wrap boundary must drag from the preceding row's edge.
    await write(page, "x" * cols + " tail")
    await touch.hold(*await point(page, 2, 0))
    handle = await page.locator("#selection-end").bounding_box()
    x, y = handle["x"] + handle["width"] / 2, handle["y"] + 18
    await touch.event("touchStart", x, y)
    await touch.event("touchMove", x - cell_width, y)
    await touch.event("touchEnd", x - cell_width, y)
    assert await page.evaluate("testTerm.getSelection()") == "x" * (cols - 1)
    await menu.get_by_role("button", name="Dismiss selection", exact=True).click()

    # A successful clipboard paste uses xterm's bracketed paste, without extra Enter.
    await write(page, "prompt> \x1b[?2004h")
    await page.evaluate("window.clipboardText = 'echo 中文\\nsecond line'; window.sent = []")
    await touch.hold(*await point(page, 20, 2))
    await menu.get_by_role("button", name="Paste", exact=True).click()
    assert await page.evaluate("sent.filter(x => !x.startsWith('{')).join('')") == "\x1b[200~echo 中文\rsecond line\x1b[201~"

    # Permission denial exposes a real native textarea, sending only on confirmation.
    await page.evaluate("window.clipboardDenied = true; window.sent = []")
    await touch.hold(*await point(page, 20, 2))
    await menu.get_by_role("button", name="Paste", exact=True).click()
    field = page.get_by_role("textbox", name="Clipboard text")
    await field.fill("git status")
    assert await page.evaluate("sent.filter(x => !x.startsWith('{')).length") == 0
    # Model a keyboard shrinking/panning only the visual viewport (iOS-style),
    # leaving the layout viewport unchanged. This is not a native keyboard test.
    await page.evaluate("""() => {
      Object.defineProperties(visualViewport, {
        height: {value: 360, configurable: true}, offsetTop: {value: 180, configurable: true}
      });
      visualViewport.dispatchEvent(new Event('resize'));
    }""")
    await page.wait_for_function("document.querySelector('#app').getBoundingClientRect().height === 360")
    dialog_bounds = await page.locator("#clipboard-dialog").bounding_box()
    assert dialog_bounds["y"] >= 180 and dialog_bounds["y"] + dialog_bounds["height"] <= 540, dialog_bounds
    await page.screenshot(path=str(OUT / f"{name}-keyboard-viewport.png"))
    await page.evaluate("""() => {
      delete visualViewport.height; delete visualViewport.offsetTop;
      visualViewport.dispatchEvent(new Event('resize'));
    }""")
    await page.wait_for_function("document.querySelector('#app').getBoundingClientRect().height === visualViewport.height")
    await page.screenshot(path=str(OUT / f"{name}-paste.png"))
    await page.get_by_role("button", name="Paste into terminal", exact=True).click()
    assert await page.evaluate("sent.filter(x => !x.startsWith('{')).join('')") == "\x1b[200~git status\x1b[201~"
    await page.wait_for_function("document.querySelector('#clipboard-text').value === ''")
    # Cancel must not write, and disconnect must preserve the user's draft.
    await touch.hold(*await point(page, 20, 2))
    await menu.get_by_role("button", name="Paste", exact=True).click()
    await field.fill("do not send")
    await page.evaluate("window.sent = []")
    await page.get_by_role("button", name="Cancel", exact=True).click()
    assert await page.evaluate("sent.filter(x => !x.startsWith('{')).length") == 0
    await touch.hold(*await point(page, 20, 2))
    await menu.get_by_role("button", name="Paste", exact=True).click()
    await field.fill("keep draft")
    await page.evaluate("socket.readyState = 3")
    await page.get_by_role("button", name="Paste into terminal", exact=True).click()
    assert await field.input_value() == "keep draft"
    assert await page.locator("#clipboard-dialog").is_visible()
    await page.get_by_role("button", name="Cancel", exact=True).click()
    await page.evaluate("socket.readyState = 1")

    # Copy denial uses a readonly selectable field, never interpolated HTML.
    await write(page, "<b>literal</b>")
    await touch.hold(*await point(page, 4, 0))
    await menu.get_by_role("button", name="Copy", exact=True).click()
    assert await field.input_value() == "<b>literal</b>"
    assert await field.get_attribute("readonly") is not None
    await page.get_by_role("button", name="Done", exact=True).click()

    # Swipes scroll both ways without opening the menu, keyboard or sending keys.
    await write(page, "\r\n".join(f"line {i}" for i in range(200)))
    await page.evaluate("testTerm.blur(); window.sent = []")
    bottom = await page.evaluate("testTerm.buffer.active.viewportY")
    await touch.swipe(150, 250, 450)
    earlier = await page.evaluate("testTerm.buffer.active.viewportY")
    assert earlier < bottom
    await touch.swipe(150, 450, 300)
    assert await page.evaluate("testTerm.buffer.active.viewportY") > earlier
    assert await menu.is_hidden()
    assert not await page.evaluate("document.activeElement === testTerm.textarea")
    assert await page.evaluate("sent.filter(x => !x.startsWith('{')).length") == 0
    # Long press timer cancellation and a normal tap focusing the keyboard.
    await touch.event("touchStart", 160, 200)
    await touch.event("touchCancel")
    await page.wait_for_timeout(500)
    assert await menu.is_hidden()
    await touch.event("touchStart", 160, 200)
    await touch.event("touchEnd", 160, 200)
    assert await page.evaluate("document.activeElement === testTerm.textarea")

    # Applications using SGR mouse reporting (tmux) receive wheel reports.
    await write(page, "\x1b[?1049h\x1b[?1000h\x1b[?1006happ")
    await page.evaluate("window.sent = []")
    await touch.swipe(150, 400, 250)
    inputs = await page.evaluate("sent.filter(x => !x.startsWith('{'))")
    assert inputs and all(re.fullmatch(r"\x1b\[<65;\d+;\d+M", text) for text in inputs), inputs
    await page.evaluate("window.sent = []")
    await touch.hold(*await point(page, 1, 0))
    assert await page.evaluate("testTerm.getSelection()") == "app"
    assert await page.evaluate("sent.filter(x => !x.startsWith('{')).length") == 0
    await menu.get_by_role("button", name="Dismiss selection", exact=True).click()
    # Older mouse protocols use xterm's binary channel instead of UTF-8 onData.
    await write(page, "\x1b[?1006l", reset=False)
    await page.evaluate("window.sent = []")
    await touch.swipe(150, 400, 250)
    reports = await page.evaluate("sent.map(data => Array.from(data))")
    assert reports and all(report[:4] == [27, 91, 77, 97] and len(report) == 6 for report in reports), reports
    # Alternate screen without mouse mode still uses xterm's cursor-key behavior.
    await write(page, "\x1b[?1000l\x1b[?1006l", reset=False)
    await page.evaluate("window.sent = []")
    await touch.swipe(150, 300, 400)
    inputs = await page.evaluate("sent.filter(x => !x.startsWith('{')).join('')")
    assert inputs and re.fullmatch(r"(?:\x1b\[A)+", inputs), repr(inputs)

    # Real binary websocket frames and viewport changes (keyboard/orientation size).
    await page.evaluate("socket.onmessage({data: new TextEncoder().encode('binary 中文').buffer})")
    await page.wait_for_function("testTerm.buffer.active.getLine(0).translateToString().includes('binary 中文')")
    for width, height in ((320, 568), (390, 360), (844, 390)):
        await page.set_viewport_size({"width": width, "height": height})
        await page.wait_for_function("Math.abs(document.querySelector('#app').getBoundingClientRect().height - visualViewport.height) < 2")
        await write(page, "alpha beta gamma")
        await page.get_by_role("button", name="Browse output", exact=True).click()
        await page.wait_for_function("document.querySelector('#browse-output').textContent === 'Back to terminal'")
        # Browsing keeps the terminal in place; only the toolbar changes.
        for selector in ("#browse-output", "#browse-status"):
            box = await page.locator(selector).bounding_box()
            assert box["x"] >= 0 and box["x"] + box["width"] <= width + 1, (selector, width, box)
        term_box = await page.locator("#term").bounding_box()
        assert term_box["height"] >= height * .6 and await page.locator("dialog[open]").count() == 0
        await page.screenshot(path=str(OUT / f"{name}-browse-{width}x{height}.png"))
        await page.get_by_role("button", name="Back to terminal", exact=True).click()
        await page.wait_for_function("document.querySelector('#browse-output').textContent === 'Browse output'")
        await touch.hold(*await point(page, 8, 0))
        box = await menu.bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= width + 1, (width, box)
        assert box["y"] >= 0 and box["y"] + box["height"] <= height + 1, (height, box)
        await page.screenshot(path=str(OUT / f"{name}-{width}x{height}.png"))
        await menu.get_by_role("button", name="Dismiss selection", exact=True).click()
    assert not errors, errors
    await context.close()
    print(f"PASS {name}: tmux browse mode, selection/CJK/wrap, copy/paste/fallback, cancel/disconnect, scroll/mouse/alt, binary/proxy, viewport", flush=True)


async def desktop(browser, sources):
    context = await browser.new_context(viewport={"width": 1280, "height": 800})
    await context.add_init_script(f"({FIXTURE})()")
    page = await context.new_page()
    page.set_default_timeout(10000)

    async def serve(route):
        if route.request.url in sources:
            # Ensure the optional Unicode addon can fail without breaking the UI.
            if "unicode11" in route.request.url:
                await route.abort()
            else:
                await route.fulfill(body=sources[route.request.url], content_type="text/css" if route.request.url.endswith(".css") else "application/javascript")
        else:
            await route.fulfill(body=HTML, content_type="text/html")

    await page.route("**/*", serve)
    await page.goto("http://terminal.example.test/")
    await page.wait_for_function("window.sent.length > 0")
    assert await page.evaluate("socket.url") == "ws://terminal.example.test/ws"
    assert await page.evaluate("document.activeElement === testTerm.textarea")
    await write(page, "alpha beta gamma")
    x1, y1 = await point(page, 6, 0)
    x2, _ = await point(page, 10, 0)
    await page.mouse.move(x1 - 4, y1)
    await page.mouse.down()
    await page.mouse.move(x2 - 4, y1, steps=5)
    await page.mouse.up()
    assert await page.evaluate("testTerm.getSelection()") == "beta"
    assert await page.locator("#selection-menu").is_hidden()
    await page.locator(".xterm-helper-textarea").focus()
    await page.evaluate("window.sent = []")
    await page.keyboard.type("pwd")
    await page.keyboard.press("Enter")
    assert await page.evaluate("sent.filter(x => !x.startsWith('{')).join('')") == "pwd\r"
    await browse_mode(page)
    await context.add_init_script('window.disableTmux = true')
    await page.reload()
    await page.wait_for_function('window.testTerm && window.sent.length > 0')
    await page.evaluate('window.sent = []')
    await page.get_by_role('button', name='Browse output', exact=True).click()
    await page.wait_for_function("document.querySelector('#notice').textContent.includes('Start tmux')")
    assert await page.evaluate('sent.every(x => JSON.parse(x).type === "resize")')
    await context.close()
    print("PASS desktop: tmux browse mode via wheel, mouse selection, typing/Enter, root websocket, optional addon failure", flush=True)


async def main():
    sources = assets()
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        try:
            await mobile(browser, "chromium", sources, chromium=True)
            await desktop(browser, sources)
        finally:
            await browser.close()
        browser = await p.webkit.launch(headless=True)
        try:
            await mobile(browser, "webkit", sources)
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
