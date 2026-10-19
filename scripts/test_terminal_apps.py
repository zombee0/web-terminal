"""Real CLI/PTY acceptance with synthetic history; no model or Box connection.

Run make test TEST_TARGETS=test-terminal-apps with codex and claude on PATH.
The native CLIs render and handle every key; only their upstream data is faked.
Codex uses its remote app-server protocol. Claude uses a local Messages API.
Versions are printed because app shortcuts/renderers can change independently.
"""

import asyncio
import contextlib
import fcntl
import json
import os
from pathlib import Path
import pty
import shutil
import signal
import struct
import tempfile
import termios

from aiohttp import web
from playwright.async_api import async_playwright

from test_terminal_ui import HTML, OUT, assets

THREAD_ID = "019cb123-4567-7000-8000-000000000001"
HISTORY = "\n".join(f"HISTORY-{i:04d} 中文 retained message" for i in range(120))
SCREEN = r"""() => Array.from({length: testTerm.buffer.active.length}, (_, i) =>
    testTerm.buffer.active.getLine(i).translateToString(true)).join('\n')"""
VISIBLE_HISTORY = r"""() => {
  const e = document.querySelector('#output-history');
  const viewport = document.querySelector('#output-scroll').getBoundingClientRect();
  return Array.from(e.textContent.matchAll(/HISTORY-(\d+)/g)).filter(match => {
    const range = document.createRange();
    range.setStart(e.firstChild, match.index);
    range.setEnd(e.firstChild, match.index + match[0].length);
    const rect = range.getBoundingClientRect();
    return rect.bottom > viewport.top && rect.top < viewport.bottom;
  }).map(match => Number(match[1]));
}"""


def codex_responses(cwd):
    thread = dict(id=THREAD_ID, sessionId=THREAD_ID, cliVersion="0.163.0", cwd=str(cwd),
                  ephemeral=True, modelProvider="fixture", preview="Fixture history",
                  projectId=None, createdAt=1, updatedAt=1, source="cli", status={"type": "idle"},
                  turns=[dict(id=f"turn-{i}", status="completed", items=[
                      dict(id=f"user-{i}", type="userMessage", content=[dict(type="text", text=f"Fixture turn {i}")]),
                      dict(id=f"assistant-{i}", type="agentMessage", text="\n".join(HISTORY.splitlines()[i * 15:(i + 1) * 15])),
                  ]) for i in range(8)])
    return {
        "initialize": dict(userAgent="history-fixture", codexHome=str(cwd), platformFamily="unix", platformOs="macos"),
        "config/read": dict(config={"projects": {str(cwd): {"trust_level": "trusted"}}}, origins={}, layers=[]),
        "configRequirements/read": dict(requirements=None),
        "model/list": dict(data=[dict(id="fixture", model="fixture", displayName="Fixture",
            description="Local history fixture", hidden=False, isDefault=True,
            defaultReasoningEffort="medium", supportedReasoningEfforts=[])]),
        "account/read": dict(account=None, requiresOpenaiAuth=False),
        "thread/resume": dict(thread=thread, model="fixture", modelProvider="fixture", cwd=str(cwd),
            approvalPolicy="never", approvalsReviewer="user", sandbox={"type": "readOnly"}),
        "thread/read": dict(thread=thread),
        "thread/turns/list": dict(data=thread["turns"], nextCursor=None, backwardsCursor=None),
        "thread/list": dict(data=[thread], nextCursor=None),
        "thread/goal/get": dict(goal=None),
        "thread/loaded/list": dict(data=[THREAD_ID]),
        "skills/list": dict(data=[]),
        "plugin/list": dict(marketplaces=[]),
        "app/list": dict(data=[]),
        "mcpServerStatus/list": dict(data=[]),
        "experimentalFeature/list": dict(data=[]),
        "collaborationMode/list": dict(data=[]),
        "hooks/list": dict(data=[]),
    }


class Fixture:
    def __init__(self, cwd):
        self.cwd = cwd
        self.responses = codex_responses(cwd)
        self.methods = set()
        self.command = []
        # Keep HOME's actual meaning; do not inherit credentials or custom flags.
        self.env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR") if key in os.environ}
        self.env.update(TERM="xterm-256color", LANG="en_US.UTF-8")
        self.process = None

    async def rpc(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            if message.type != web.WSMsgType.TEXT:
                continue
            packet = json.loads(message.data)
            method = packet.get("method")
            self.methods.add(method)
            if "id" not in packet:
                continue
            reply = {"id": packet["id"]}
            if method in self.responses:
                reply["result"] = self.responses[method]
            else:
                reply["error"] = dict(code=-32601, message="Not implemented by history fixture: " + str(method))
            await ws.send_json(reply)
        return ws

    async def messages(self, request):
        payload = await request.json()
        self.methods.add(request.path)
        if request.path.endswith("count_tokens"):
            return web.json_response({"input_tokens": 50})
        message = dict(id="msg_fixture", type="message", role="assistant", model=payload["model"],
                       content=[dict(type="text", text=HISTORY)], stop_reason="end_turn",
                       stop_sequence=None, usage=dict(input_tokens=50, output_tokens=1500))
        if not payload.get("stream"):
            return web.json_response(message)
        events = [dict(type="message_start", message={**message, "content": [], "stop_reason": None}),
                  dict(type="content_block_start", index=0, content_block=dict(type="text", text="")),
                  dict(type="content_block_delta", index=0, delta=dict(type="text_delta", text=HISTORY)),
                  dict(type="content_block_stop", index=0),
                  dict(type="message_delta", delta=dict(stop_reason="end_turn", stop_sequence=None), usage=dict(output_tokens=1500)),
                  dict(type="message_stop")]
        return web.Response(text="".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events),
                            content_type="text/event-stream")

    async def terminal(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        # The page sends its size before input; start the CLI at that size.
        size = json.loads((await ws.receive()).data)
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", size["rows"], size["cols"], 0, 0))
        self.process = await asyncio.create_subprocess_exec(*self.command, stdin=slave, stdout=slave, stderr=slave,
                                                          cwd=self.cwd, env=self.env, start_new_session=True)
        os.close(slave)
        os.set_blocking(master, False)
        loop = asyncio.get_running_loop()
        output = asyncio.Queue()

        def read():
            try:
                data = os.read(master, 65536)
            except BlockingIOError:
                return
            except OSError:
                data = b""
            if not data:
                loop.remove_reader(master)
            output.put_nowait(data)

        async def forward():
            while data := await output.get():
                await ws.send_bytes(data)

        loop.add_reader(master, read)
        sender = asyncio.create_task(forward())
        try:
            async for message in ws:
                if message.type not in (web.WSMsgType.TEXT, web.WSMsgType.BINARY):
                    continue
                data = message.data
                if isinstance(data, str) and data.startswith('{"type":"resize"'):
                    size = json.loads(data)
                    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", size["rows"], size["cols"], 0, 0))
                    os.kill(self.process.pid, signal.SIGWINCH)
                else:
                    os.write(master, data.encode() if isinstance(data, str) else data)
        finally:
            loop.remove_reader(master)
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, ConnectionResetError):
                await sender
            os.close(master)
            if self.process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(self.process.wait(), 5)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(self.process.pid, signal.SIGKILL)
                    await self.process.wait()
        return ws


async def acceptance(browser, sources, app, fullscreen=True):
    name = app + ("-fullscreen" if fullscreen else "-inline")
    with tempfile.TemporaryDirectory(prefix="terminal-app-history-") as directory:
        cwd = Path(directory)
        fixture = Fixture(cwd)
        server = web.Application()

        async def root(request):
            if request.headers.get("Upgrade", "").lower() == "websocket":
                return await fixture.rpc(request)
            return web.Response(text=HTML, content_type="text/html")

        server.router.add_get("/", root)
        server.router.add_get("/ws", fixture.terminal)
        server.router.add_post("/v1/messages", fixture.messages)
        server.router.add_post("/v1/messages/count_tokens", fixture.messages)
        runner = web.AppRunner(server)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        if app == "codex":
            fixture.command = [shutil.which("codex"), "resume", THREAD_ID, "--remote",
                               f"ws://127.0.0.1:{port}", "-C", directory]
            if not fullscreen:
                fixture.command.append("--no-alt-screen")
        else:
            config_dir = cwd / "claude-config"
            config_dir.mkdir()
            # Disposable first-run state for an empty fixture workspace. No
            # user's settings, credentials or saved conversations are copied.
            (config_dir / ".claude.json").write_text(json.dumps({
                "hasCompletedOnboarding": True,
                "customApiKeyResponses": {"approved": ["fixture-not-a-real-key"[-20:]], "rejected": []},
                "projects": {str(cwd.resolve()): {"hasTrustDialogAccepted": True}},
            }))
            (config_dir / "settings.json").write_text(json.dumps({"theme": "dark"}))
            fixture.env.update(CLAUDE_CONFIG_DIR=str(cwd / "claude-config"),
                ANTHROPIC_BASE_URL=base, ANTHROPIC_API_KEY="fixture-not-a-real-key",
                CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL="1",
                CLAUDE_CODE_NO_FLICKER="1" if fullscreen else "0",
                CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN="0" if fullscreen else "1")
            fixture.command = [shutil.which("claude"), "--bare", "--tools", "", "--strict-mcp-config",
                               "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "",
                               "--no-chrome", "--model", "claude-sonnet-4-5", "Show fixture history"]
        context = await browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        page = await context.new_page()
        page.set_default_timeout(15000)

        async def serve_asset(route):
            url = route.request.url
            await route.fulfill(body=sources[url], content_type="text/css" if url.endswith(".css") else "application/javascript")

        await page.route("https://cdn.jsdelivr.net/**", serve_asset)
        try:
            await page.goto(base)
            await page.wait_for_function("window.testTerm !== undefined")
            await page.wait_for_function(f"({SCREEN})().includes('HISTORY-0119')", timeout=30000)
            await page.locator(".xterm-helper-textarea").focus()
            await page.keyboard.type("UNSENT-DRAFT")
            await page.wait_for_function(f"({SCREEN})().includes('UNSENT-DRAFT')")
            for _ in range(6):
                await page.keyboard.press("ArrowLeft")
            await page.get_by_role("button", name="Browse output", exact=True).click()
            await page.get_by_role("combobox", name="Output source").select_option(app)
            await page.wait_for_function("document.querySelector('#output-history').textContent.includes('HISTORY-')")
            scroll = page.locator("#output-scroll")
            previous = page.get_by_role("button", name="Previous page", exact=True)

            # Each click may first read the current native page, then ask the app
            # for older history. The oldest line must eventually be reachable.
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
            await page.screenshot(path=str(OUT / f"real-{name}-earliest.png"))
            await page.get_by_role("button", name="Next page", exact=True).click()
            await page.get_by_role("button", name="Latest", exact=True).click()
            await page.wait_for_function(f"({VISIBLE_HISTORY})().includes(119)")
            await page.get_by_role("button", name="Back to terminal", exact=True).click()
            await page.wait_for_function(f"({SCREEN})().includes('UNSENT-DRAFT')")
            await page.locator(".xterm-helper-textarea").focus()
            await page.keyboard.type("-CONTINUED")
            await page.wait_for_function(f"({SCREEN})().includes('UNSENT-CONTINUED-DRAFT')")
            await page.screenshot(path=str(OUT / f"real-{name}-returned.png"))
            print(f"PASS real {name}: {len(seen)} history markers, earliest/latest, continued draft", flush=True)
        except Exception:
            print(f"FAIL real {name}; fixture requests: {sorted(str(m) for m in fixture.methods)}", flush=True)
            print("CLI exit:", fixture.process.returncode if fixture.process else None, flush=True)
            print(await page.evaluate(SCREEN), flush=True)
            await page.screenshot(path=str(OUT / f"real-{name}-failure.png"))
            raise
        finally:
            await context.close()
            await runner.cleanup()


async def main():
    for app in ("codex", "claude"):
        binary = shutil.which(app)
        if not binary:
            raise RuntimeError(f"{app} must be installed for real CLI acceptance")
        process = await asyncio.create_subprocess_exec(binary, "--version", stdout=asyncio.subprocess.PIPE)
        stdout, _ = await process.communicate()
        print(stdout.decode().strip(), flush=True)
    sources = assets()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=True)
        try:
            cases = os.environ.get("TERMINAL_TEST_APPS", "codex-fullscreen,codex-inline,claude-fullscreen,claude-inline").split(",")
            for case in cases:
                app, mode = case.split("-")
                if app not in ("codex", "claude") or mode not in ("fullscreen", "inline"):
                    raise ValueError("Unknown CLI acceptance case")
                await acceptance(browser, sources, app, fullscreen=mode == "fullscreen")
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
