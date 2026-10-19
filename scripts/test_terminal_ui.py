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
      setTimeout(() => this.onopen?.(), 0);
    }
    send(data) { window.sent.push(data); }
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


async def browse_output(page, touch=None):
    cols = await page.evaluate("testTerm.cols")
    wrapped = "x" * (cols - 1) + " 中文🙂 end"
    wide_wrap = "w" * (cols - 1) + "中"
    history = "\r\n".join([wrapped, wide_wrap, "<img src=x onerror=alert(1)>"] +
                           [f"saved output {i}" for i in range(200)] + ["pending input"])
    await write(page, history)
    await page.evaluate("testTerm.blur(); window.sent = []")
    cursor = await page.evaluate("[testTerm.buffer.active.cursorX, testTerm.buffer.active.cursorY]")
    await page.get_by_role("button", name="Browse output", exact=True).click()
    reader = page.locator("#output-dialog")
    scroll = page.locator("#output-scroll")
    text = await page.locator("#output-history").text_content()
    assert wrapped in text and wide_wrap in text, repr(text[:cols * 3])
    assert "saved output 0" in text and "pending input" in text
    assert "<img src=x onerror=alert(1)>" in text
    assert await scroll.locator("img").count() == 0
    await page.wait_for_function("document.querySelector('#output-scroll').scrollTop > 0")
    bottom = await scroll.evaluate("e => e.scrollTop")
    box = await scroll.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    async def scroll_by(delta):
        if touch:
            await page.evaluate("""() => {
              window.readerTouchPrevented = false;
              document.addEventListener('touchmove', e => {
                window.readerTouchPrevented = e.defaultPrevented;
              }, {once: true});
            }""")
            direction = 1 if delta > 0 else -1
            await touch.swipe(x, y + direction * 70, y - direction * 70)
            assert not await page.evaluate("readerTouchPrevented")
            if not touch.cdp:
                # Mobile WebKit cannot synthesize native touch scrolling or
                # wheel input. Check uncancelled events and DOM scrollability;
                # native iOS scrolling still requires device acceptance.
                await scroll.evaluate("(e, delta) => { e.scrollTop += delta; }", delta)
        else:
            await page.mouse.move(x, y)
            await page.mouse.wheel(0, delta)

    await scroll_by(-300)
    await page.wait_for_function("bottom => document.querySelector('#output-scroll').scrollTop < bottom", arg=bottom)
    await page.wait_for_timeout(200)
    earlier = await scroll.evaluate("e => e.scrollTop")
    await scroll_by(100)
    await page.wait_for_function("earlier => document.querySelector('#output-scroll').scrollTop > earlier", arg=earlier)
    assert await page.evaluate("[testTerm.buffer.active.cursorX, testTerm.buffer.active.cursorY]") == cursor
    assert await page.evaluate("sent.filter(x => typeof x !== 'string' || !x.startsWith('{')).length") == 0
    # Incoming output cannot replace a snapshot or move the reading position.
    await scroll.evaluate("e => { e.scrollTop = 120; }")
    await write(page, "\r\nnew background output", reset=False)
    assert await page.locator("#output-history").text_content() == text
    assert await scroll.evaluate("e => e.scrollTop") == 120
    await reader.get_by_role("button", name="Refresh", exact=True).click()
    assert "new background output" in await page.locator("#output-history").text_content()
    assert await scroll.evaluate("e => e.scrollTop") == 120
    await reader.get_by_role("button", name="Latest", exact=True).click()
    assert await scroll.evaluate("e => e.scrollHeight - e.scrollTop - e.clientHeight") < 2
    await reader.get_by_role("button", name="Back to terminal", exact=True).click()
    assert await reader.is_hidden()
    await page.wait_for_function("document.querySelector('#output-history').textContent === ''")
    assert await page.locator("#output-history").text_content() == ""
    assert not await page.evaluate("document.activeElement === testTerm.textarea")
    # Fullscreen applications retain only their current screen, not scrollback.
    await write(page, "\x1b[?1049h\x1b[?1000h\x1b[?1006h\x1b[Hfullscreen current", reset=False)
    await page.evaluate("window.sent = []")
    cursor = await page.evaluate("[testTerm.buffer.active.cursorX, testTerm.buffer.active.cursorY]")
    await page.get_by_role("button", name="Browse output", exact=True).click()
    assert "saved output 0" in await page.locator("#output-history").text_content()
    assert "fullscreen current" in await page.locator("#output-screen").text_content()
    assert "Choose" in await page.locator("#output-help").text_content()
    browser_name = page.context.browser.browser_type.name
    await page.screenshot(path=str(OUT / f"{browser_name}-output-{page.viewport_size['width']}.png"))
    await scroll.evaluate("e => { e.scrollTop = 0; }")
    await scroll_by(150)
    await page.wait_for_function("document.querySelector('#output-scroll').scrollTop > 0")
    await reader.get_by_role("button", name="Back to terminal", exact=True).click()
    assert await page.evaluate("[testTerm.buffer.active.cursorX, testTerm.buffer.active.cursorY]") == cursor
    assert await page.evaluate("sent.filter(x => typeof x !== 'string' || !x.startsWith('{')).length") == 0
    await page.wait_for_function("document.querySelector('#output-screen').textContent === ''")
    assert await page.locator("#output-screen").text_content() == ""


async def history_paging(page):
    await write(page, "\r\n".join(f"older output {i}" for i in range(1200)))
    await page.evaluate("window.sent = []")
    await page.get_by_role("button", name="Browse output", exact=True).click()
    reader = page.locator("#output-dialog")
    scroll = page.locator("#output-scroll")
    previous = reader.get_by_role("button", name="Previous page", exact=True)
    following = reader.get_by_role("button", name="Next page", exact=True)
    bottom = await scroll.evaluate("e => e.scrollTop")
    height = await scroll.evaluate("e => e.clientHeight")
    for _ in range(4):
        await previous.click()
    earlier = await scroll.evaluate("e => e.scrollTop")
    assert bottom - earlier > height * 3
    await following.click()
    assert await scroll.evaluate("e => e.scrollTop") > earlier
    await reader.get_by_role("button", name="Latest", exact=True).click()
    assert await scroll.evaluate("e => e.scrollTop") == bottom
    assert await page.evaluate("sent.filter(x => typeof x !== 'string' || !x.startsWith('{')).length") == 0

    source = reader.get_by_role("combobox", name="Output source")
    for app, toggle, latest in (("codex", "\x14", "\x1b[F"), ("claude", "\x0f", "\x1b[1;5F")):
        await page.evaluate("window.sent = []")
        await source.select_option(app)
        assert await page.evaluate("sent.join('')") == toggle
        # The application owns older messages. Deliver its redraw over the
        # real websocket input path; they never existed in normal scrollback.
        await page.evaluate("socket.onmessage({data: '\\x1b[?1049h\\x1b[2J\\x1b[Happ latest'})")
        await page.wait_for_function("document.querySelector('#output-history').textContent.includes('app latest')")
        for i in range(3):
            sent_count = await page.evaluate("sent.length")
            await previous.click()
            assert await page.evaluate("sent.length") == sent_count + 1
            assert await page.evaluate("sent[sent.length - 1]") == "\x1b[5~"
            await page.evaluate("""i => socket.onmessage({data: new TextEncoder().encode(
                '\\x1b[2J\\x1b[Happ older page ' + i + ' 中文').buffer})""", i)
            await page.wait_for_function("i => document.querySelector('#output-history').textContent.includes('app older page ' + i)", arg=i)
        # Read the rest of a large returned page before requesting another.
        rows = await page.evaluate("testTerm.rows")
        await write(page, "\x1b[2J\x1b[H" + "\r\n".join(f"app long page {i}" for i in range(rows)), reset=False)
        await page.wait_for_function("document.querySelector('#output-history').textContent.includes('app long page 0')")
        await scroll.evaluate("e => { e.scrollTop = e.scrollHeight; }")
        page_bottom = await scroll.evaluate("e => e.scrollTop")
        assert page_bottom > 0
        await page.evaluate("window.sent = []")
        await previous.click()
        assert await scroll.evaluate("e => e.scrollTop") < page_bottom
        assert await page.evaluate("sent.length") == 0
        await scroll.evaluate("e => { e.scrollTop = 0; }")
        await previous.click()
        assert await page.evaluate("sent.join('')") == "\x1b[5~"
        await write(page, "\x1b[2J\x1b[Happ earlier screen", reset=False)
        await page.wait_for_function("document.querySelector('#output-history').textContent.includes('app earlier screen')")
        await following.click()
        assert await page.evaluate("sent[sent.length - 1]") == "\x1b[6~"
        await reader.get_by_role("button", name="Latest", exact=True).click()
        assert await page.evaluate("sent[sent.length - 1]") == latest
        # Live app redraws must not destroy native text selection during copy.
        await page.evaluate("""() => {
          const range = document.createRange();
          range.selectNodeContents(document.querySelector('#output-history'));
          getSelection().removeAllRanges(); getSelection().addRange(range);
        }""")
        selected = await page.evaluate("getSelection().toString()")
        await write(page, "\x1b[2J\x1b[Hnew app output", reset=False)
        assert await page.evaluate("getSelection().toString()") == selected
        await page.evaluate("getSelection().removeAllRanges()")
        await reader.get_by_role("button", name="Refresh", exact=True).click()
        assert "new app output" in await page.locator("#output-history").text_content()
        await source.select_option("terminal")
        assert await page.evaluate("sent[sent.length - 1]") == toggle
        await page.evaluate("socket.onmessage({data: '\\x1b[?1049l'})")
        await page.wait_for_function("document.querySelector('#output-history').textContent.includes('older output 0')")

    # Closing exits only an explicitly selected app transcript, exactly once.
    await source.select_option("codex")
    await page.evaluate("window.sent = []")
    await reader.get_by_role("button", name="Back to terminal", exact=True).click()
    await page.wait_for_function("sent.length === 1")
    assert await page.evaluate("sent.join('')") == "\x14"
    assert await page.locator("#output-source").input_value() == "terminal"
    await page.get_by_role("button", name="Browse output", exact=True).click()
    await source.select_option("claude")
    await page.evaluate("window.sent = []")
    await page.keyboard.press("Escape")
    await page.wait_for_function("sent.length === 1")
    assert await page.evaluate("sent.join('')") == "\x0f"
    await page.get_by_role("button", name="Browse output", exact=True).click()
    await source.select_option("claude")
    await page.evaluate("window.sent = []; socket.readyState = 3")
    await previous.click()
    assert "Disconnected" in await page.locator("#output-help").text_content()
    await reader.get_by_role("button", name="Back to terminal", exact=True).click()
    await page.wait_for_function("document.querySelector('#output-source').value === 'terminal'")
    assert await page.evaluate("sent.length") == 0
    await page.get_by_role("button", name="Browse output", exact=True).click()
    await source.select_option("codex")
    assert await source.input_value() == "terminal"
    assert await page.evaluate("sent.length") == 0
    await page.keyboard.press("Escape")
    await page.wait_for_function("!document.querySelector('#output-dialog').open")
    await page.evaluate("socket.readyState = 1")


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
    await history_paging(page)
    await browse_output(page, touch)
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
        reader = page.locator("#output-dialog")
        reader_box = await reader.bounding_box()
        assert reader_box["x"] >= 0 and reader_box["x"] + reader_box["width"] <= width + 1
        assert reader_box["y"] >= 0 and reader_box["y"] + reader_box["height"] <= height + 1
        assert await page.locator("#output-scroll").evaluate("e => e.clientHeight") >= height * .6
        await page.screenshot(path=str(OUT / f"{name}-reader-{width}x{height}.png"))
        await reader.get_by_role("button", name="Back to terminal", exact=True).click()
        await touch.hold(*await point(page, 8, 0))
        box = await menu.bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= width + 1, (width, box)
        assert box["y"] >= 0 and box["y"] + box["height"] <= height + 1, (height, box)
        await page.screenshot(path=str(OUT / f"{name}-{width}x{height}.png"))
        await menu.get_by_role("button", name="Dismiss selection", exact=True).click()
    assert not errors, errors
    await context.close()
    print(f"PASS {name}: output reader, selection/CJK/wrap, copy/paste/fallback, cancel/disconnect, scroll/mouse/alt, binary/proxy, viewport", flush=True)


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
    await history_paging(page)
    await browse_output(page)
    await write(page, "selectable output line")
    await page.get_by_role("button", name="Browse output", exact=True).click()
    text_box = await page.locator("#output-history").bounding_box()
    await page.mouse.move(text_box["x"], text_box["y"] + 10)
    await page.mouse.down()
    await page.mouse.move(text_box["x"] + 200, text_box["y"] + 10, steps=5)
    await page.mouse.up()
    assert await page.evaluate("getSelection().toString()") == "selectable output line"
    await page.keyboard.press("Escape")
    assert await page.locator("#output-dialog").is_hidden()
    await page.wait_for_function("document.querySelector('#output-history').textContent === ''")
    await context.close()
    print("PASS desktop: native output scrolling/selection, mouse selection, typing/Enter, root websocket, optional addon failure", flush=True)


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
