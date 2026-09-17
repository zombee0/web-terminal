"""Shared BoxLite helpers for cloud REST and local embedded sandboxes."""

import asyncio
import inspect
import json
import os
import pathlib
import platform
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_REST_URL = "https://dev.boxlite.ai/api"
DEFAULT_IMAGE = "python"
PORT = 3001
PROJECT_DIR = pathlib.Path(__file__).resolve().parent
BUILD_SCRIPT = PROJECT_DIR / "build_web_terminal_binary.sh"
CLOUD_RUNTIMES = {"cloud", "rest", "remote"}
EMBEDDED_RUNTIMES = {"embedded", "local"}


def runtime_mode():
    raw = os.environ.get("BOXLITE_RUNTIME", "cloud").strip().lower()
    if raw == "" or raw in CLOUD_RUNTIMES:
        return "cloud"
    if raw in EMBEDDED_RUNTIMES:
        return "embedded"
    raise RuntimeError(
        f"Unknown BOXLITE_RUNTIME={raw!r}; use cloud (default) or embedded"
    )


def guest_goarch(mode):
    override = os.environ.get("WEB_TERMINAL_GOARCH", "").strip()
    if override:
        return override
    if mode == "cloud":
        return "amd64"
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "arm64"
    if machine in ("x86_64", "amd64"):
        return "amd64"
    raise RuntimeError(f"Unsupported host architecture for embedded BoxLite: {machine}")


def app_path(mode):
    return PROJECT_DIR / "build" / f"web-terminal-linux-{guest_goarch(mode)}"


def host_bind_port():
    raw = os.environ.get("BOXLITE_HOST_PORT", str(PORT))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Invalid BOXLITE_HOST_PORT: {raw}") from exc
    if not 1 <= value <= 65535:
        raise RuntimeError(f"Invalid BOXLITE_HOST_PORT: {raw}")
    return value


def ensure_app(mode):
    path = app_path(mode)
    if path.is_file():
        return path
    print(f"Web Terminal binary not found; building {path}", flush=True)
    env = os.environ.copy()
    env["GOOS"] = "linux"
    env["GOARCH"] = guest_goarch(mode)
    subprocess.run(["bash", str(BUILD_SCRIPT)], cwd=PROJECT_DIR, env=env, check=True)
    if not path.is_file():
        raise RuntimeError(f"Build completed without producing: {path}")
    return path


def prepare_runtime_env(mode):
    if mode == "cloud":
        if not os.environ.get("BOXLITE_REST_URL"):
            os.environ["BOXLITE_REST_URL"] = DEFAULT_REST_URL
        if not os.environ.get("BOXLITE_API_KEY"):
            raise RuntimeError("BOXLITE_API_KEY is not set")
    ensure_app(mode)


def _import_boxlite():
    from boxlite import BoxOptions, Boxlite, BoxliteRestOptions, NetworkSpec, Options

    return Boxlite, BoxliteRestOptions, BoxOptions, NetworkSpec, Options


def create_runtime(mode):
    Boxlite, BoxliteRestOptions, _, _, Options = _import_boxlite()
    if mode == "cloud":
        return Boxlite.rest(BoxliteRestOptions.from_env())
    home_dir = os.environ.get("BOXLITE_HOME_DIR")
    if home_dir:
        return Boxlite(Options(home_dir=home_dir))
    return Boxlite.default()


def _box_options_accepts(name):
    _, _, BoxOptions, _, _ = _import_boxlite()
    try:
        return name in inspect.signature(BoxOptions).parameters
    except (TypeError, ValueError):
        return False


def create_box_options(mode):
    _, _, BoxOptions, NetworkSpec, _ = _import_boxlite()
    kwargs = {
        "image": os.environ.get("BOXLITE_IMAGE", DEFAULT_IMAGE),
        "cpus": 1,
        "memory_mib": 1024,
        "disk_size_gb": 10,
        "working_dir": "/tmp",
        "network": NetworkSpec(mode="enabled", allow_net=[]),
        "entrypoint": ["/bin/sleep"],
        "cmd": ["infinity"],
        "user": "root",
        "auto_remove": False,
        "detach": True,
    }
    if mode == "embedded":
        kwargs["ports"] = [
            {
                "host_port": host_bind_port(),
                "guest_port": PORT,
                "protocol": "tcp",
                "host_ip": "127.0.0.1",
            }
        ]
    elif _box_options_accepts("auto_stop"):
        kwargs["auto_stop"] = 0
    return BoxOptions(**kwargs)


def cloud_api(method, path, payload=None):
    base = os.environ["BOXLITE_REST_URL"].rstrip("/").removesuffix("/api")
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(base + path, data=data, method=method)
    request.add_header("Authorization", "Bearer " + os.environ["BOXLITE_API_KEY"])
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode() or "{}")


def authenticated_preview_url(preview):
    parts = urllib.parse.urlsplit(preview["url"])
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query.append(("BOXLITE_BOX_AUTH_KEY", preview["token"]))
    return urllib.parse.urlunsplit(
        parts._replace(path=parts.path or "/", query=urllib.parse.urlencode(query))
    )


def service_url(mode, box, *, authenticated=True):
    if mode != "cloud":
        return f"http://127.0.0.1:{host_bind_port()}"
    preview = cloud_api("GET", f"/api/box/{box.id}/ports/{PORT}/preview-url")
    if authenticated:
        return authenticated_preview_url(preview)
    return preview["url"]


def box_display_name(info, fallback):
    if info is None:
        return fallback
    return info.name or fallback


async def drain(stream, destination, captured):
    async for chunk in stream:
        text = str(chunk)
        captured.append(text)
        destination.write(text)
        destination.flush()


async def run_checked(box, command, *, args=None, user=None, timeout_secs=None):
    execution = await box.exec(
        command,
        args=args,
        user=user,
        timeout_secs=timeout_secs,
    )
    stdout = []
    stderr = []
    result, _, _ = await asyncio.gather(
        execution.wait(),
        drain(execution.stdout(), sys.stdout, stdout),
        drain(execution.stderr(), sys.stderr, stderr),
    )
    if result.exit_code != 0:
        detail = "".join(stderr)[-4000:].strip()
        raise RuntimeError(
            f"Command failed with exit code {result.exit_code}: {command}\n{detail}"
        )


async def ensure_started(runtime, box, mode):
    info = await runtime.get_info(box.id)
    if info is not None and info.state.running:
        return
    if mode == "cloud":
        cloud_api("POST", f"/api/v1/boxes/{box.id}/start", {})
    else:
        await box.start()
    for _ in range(60):
        info = await runtime.get_info(box.id)
        if info is not None and info.state.running:
            return
        await asyncio.sleep(1)
    raise RuntimeError(f"Box did not become running: {box.id}")


async def wait_for_guest_health(box, *, attempts, timeout_secs):
    await run_checked(
        box,
        "sh",
        args=[
            "-lc",
            (
                f"for attempt in $(seq 1 {attempts}); do "
                f"curl -fsS http://127.0.0.1:{PORT}/healthz | grep -qx ok && exit 0; "
                "sleep 1; done; cat /tmp/web-terminal.log >&2; exit 1"
            ),
        ],
        user="root",
        timeout_secs=timeout_secs,
    )


async def wait_for_host_health(url, *, attempts=30):
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.read().decode().strip() == "ok":
                    return
        except (OSError, urllib.error.URLError, urllib.error.HTTPError):
            pass
        await asyncio.sleep(1)
    raise RuntimeError(f"Host could not reach {url}")
