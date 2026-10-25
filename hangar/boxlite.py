"""Shared BoxLite helpers for cloud REST and local embedded sandboxes."""

import asyncio
import inspect
import json
import os
import pathlib
import platform
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_REST_URL = "https://app.boxlite.ai/api"
DEFAULT_IMAGE = "python"
DEFAULT_RESOURCES = {"cpus": 1, "memory_mib": 1024, "disk_size_gb": 10}
PORT = 3001
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PROJECT_DIR = REPO_ROOT
BUILD_SCRIPT = PROJECT_DIR / "scripts" / "build_web_terminal_binary.sh"
CLOUD_RUNTIMES = {"cloud", "rest", "remote"}
EMBEDDED_RUNTIMES = {"embedded", "local"}
MANAGED_NAME_PREFIX = "web-terminal-"
_SECRET_QUERY_RE = re.compile(r"(BOXLITE_BOX_AUTH_KEY=)[^&]*", re.IGNORECASE)


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


def parse_runtime(raw):
    value = (raw or "cloud").strip().lower()
    if value == "" or value in CLOUD_RUNTIMES:
        return "cloud"
    if value in EMBEDDED_RUNTIMES:
        return "embedded"
    raise RuntimeError(
        f"Unknown runtime {raw!r}; use cloud (default) or embedded"
    )


def _parse_exported_key(line):
    text = line.strip()
    if text.startswith("export "):
        text = text[7:].strip()
    if not text.startswith("BOXLITE_API_KEY="):
        return None
    value = text.split("=", 1)[1].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value or None


def load_cloud_credentials():
    if not os.environ.get("BOXLITE_REST_URL"):
        os.environ["BOXLITE_REST_URL"] = DEFAULT_REST_URL
    if os.environ.get("BOXLITE_API_KEY"):
        return True
    for rc_path in (pathlib.Path.home() / ".zshrc", pathlib.Path.home() / ".bashrc"):
        try:
            lines = rc_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if not re.match(r"^\s*(export\s+)?BOXLITE_API_KEY=", line):
                continue
            value = _parse_exported_key(line)
            if value:
                os.environ["BOXLITE_API_KEY"] = value
                return True
    return False


def cloud_ready():
    load_cloud_credentials()
    return bool(os.environ.get("BOXLITE_API_KEY"))


def prepare_runtime_env(mode, *, build=True):
    if mode == "cloud":
        if not load_cloud_credentials():
            raise RuntimeError("BOXLITE_API_KEY is not set")
    if build:
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


def parse_resources(raw=None):
    data = raw or {}
    try:
        cpus = int(data.get("cpus", DEFAULT_RESOURCES["cpus"]))
        memory_mib = int(data.get("memory_mib", DEFAULT_RESOURCES["memory_mib"]))
        disk_size_gb = int(data.get("disk_size_gb", DEFAULT_RESOURCES["disk_size_gb"]))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("cpus, memory_mib, and disk_size_gb must be integers") from exc
    if cpus < 1:
        raise RuntimeError("cpus must be >= 1")
    if memory_mib < 256:
        raise RuntimeError("memory_mib must be >= 256")
    if disk_size_gb < 1:
        raise RuntimeError("disk_size_gb must be >= 1")
    return {"cpus": cpus, "memory_mib": memory_mib, "disk_size_gb": disk_size_gb}


def create_box_options(mode, host_port=None, resources=None):
    spec = parse_resources(resources)
    _, _, BoxOptions, NetworkSpec, _ = _import_boxlite()
    kwargs = {
        "image": os.environ.get("BOXLITE_IMAGE", DEFAULT_IMAGE),
        "cpus": spec["cpus"],
        "memory_mib": spec["memory_mib"],
        "disk_size_gb": spec["disk_size_gb"],
        "working_dir": "/tmp",
        "network": NetworkSpec(mode="enabled", allow_net=[]),
        "entrypoint": ["/bin/sleep"],
        "cmd": ["infinity"],
        "user": "root",
        "detach": True,
    }
    if mode == "embedded":
        kwargs["auto_remove"] = False
        kwargs["ports"] = [
            {
                "host_port": host_bind_port() if host_port is None else int(host_port),
                "guest_port": PORT,
                "protocol": "tcp",
                "host_ip": "127.0.0.1",
            }
        ]
    elif _box_options_accepts("auto_stop"):
        kwargs["auto_stop"] = 0
    return BoxOptions(**kwargs)


def cloud_create_payload(name, resources=None, *, inbound=True):
    spec = parse_resources(resources)
    return {
        "name": name,
        "image": os.environ.get("BOXLITE_IMAGE", DEFAULT_IMAGE),
        "cpus": spec["cpus"],
        "memory_mib": spec["memory_mib"],
        "disk_size_gb": spec["disk_size_gb"],
        "working_dir": "/tmp",
        "entrypoint": ["/bin/sleep"],
        "cmd": ["infinity"],
        "user": "root",
        "detach": True,
        "auto_stop": 0,
        "network": {
            "outbound": {"mode": "enabled", "allow_net": []},
            **({"inbound": {"mode": "enabled"}} if inbound else {}),
        },
    }


def create_cloud_box(name, resources=None, *, inbound=True):
    data = cloud_api(
        "POST", "/api/v1/boxes", cloud_create_payload(name, resources, inbound=inbound), timeout=90
    )
    box_id = data.get("box_id") or data.get("id")
    if not box_id:
        raise RuntimeError("Cloud create returned no box id")
    return box_id, data.get("name") or name


class CloudAPIError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"BoxLite API request failed with HTTP {status}")


def cloud_api(method, path, payload=None, timeout=30):
    if not os.environ.get("BOXLITE_REST_URL"):
        os.environ["BOXLITE_REST_URL"] = DEFAULT_REST_URL
    base = os.environ["BOXLITE_REST_URL"].rstrip("/").removesuffix("/api")
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(base + path, data=data, method=method)
    request.add_header("Authorization", "Bearer " + os.environ["BOXLITE_API_KEY"])
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise RuntimeError(
                "BoxLite API rejected BOXLITE_API_KEY (HTTP 401); configure a valid key and restart the control platform"
            ) from exc
        raise CloudAPIError(exc.code) from None


def authenticated_preview_url(preview):
    parts = urllib.parse.urlsplit(preview["url"])
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query.append(("BOXLITE_BOX_AUTH_KEY", preview["token"]))
    return urllib.parse.urlunsplit(
        parts._replace(path=parts.path or "/", query=urllib.parse.urlencode(query))
    )


def cloud_tunnel_url(box_id, port=PORT):
    """Get the public URI from the tunnel API without opening its TCP stream."""
    if not 1 <= int(port) <= 65535:
        raise ValueError("Invalid tunnel port")
    result = cloud_api("POST", f"/api/v1/boxes/{box_id}/network/tunnel?port={int(port)}", {})
    uri = result.get("uri")
    if not isinstance(uri, str) or not uri:
        raise RuntimeError("Tunnel API returned no URI")
    return uri


def close_cloud_tunnel(box_id, port):
    """Close a guest port's tunnel; an already-closed tunnel counts as closed."""
    if not 1 <= int(port) <= 65535:
        raise ValueError("Invalid tunnel port")
    try:
        cloud_api("DELETE", f"/api/v1/boxes/{box_id}/network/tunnel?port={int(port)}")
    except CloudAPIError as error:
        if error.status != 404:
            raise


def published_host_port(info, guest_port=PORT):
    if info is None:
        return None
    network = getattr(info, "network", None)
    ports = getattr(network, "published_ports", None) if network is not None else None
    if ports is None:
        ports = getattr(info, "published_ports", None)
    if not ports:
        return None
    for item in ports:
        if isinstance(item, dict):
            mapped_guest = item.get("guest_port")
            mapped_host = item.get("host_port")
        else:
            mapped_guest = getattr(item, "guest_port", None)
            mapped_host = getattr(item, "host_port", None)
        if mapped_guest in (None, guest_port) and mapped_host:
            return int(mapped_host)
    return None


def service_url(mode, box, *, authenticated=True, host_port=None):
    if mode != "cloud":
        port = host_bind_port() if host_port is None else int(host_port)
        return f"http://127.0.0.1:{port}"
    try:
        return cloud_tunnel_url(box.id)
    except CloudAPIError as error:
        if error.status != 409:
            raise
    # Existing private Boxes retain their legacy authenticated preview links.
    preview = cloud_api("GET", f"/api/box/{box.id}/ports/{PORT}/preview-url")
    if authenticated:
        return authenticated_preview_url(preview)
    return preview["url"]


def redact_url(url):
    if not url:
        return url
    return _SECRET_QUERY_RE.sub(r"\1***", url)


def port_is_free(port, host="127.0.0.1"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, int(port)))
        except OSError:
            return False
    return True


def next_host_port(used_ports=None, start=None):
    taken = {int(port) for port in (used_ports or []) if port}
    port = host_bind_port() if start is None else int(start)
    while port <= 65535 and (port in taken or not port_is_free(port)):
        port += 1
    if port > 65535:
        raise RuntimeError("No free loopback port available for an embedded Box")
    return port


def normalize_status(info, fallback="unknown"):
    if info is None:
        return fallback
    state = getattr(info, "state", None)
    if state is not None and getattr(state, "running", False):
        return "running"
    status = getattr(state, "status", None) if state is not None else None
    if status in {"started"}:
        return "running"
    if status in {"configured", "starting", "restoring", "creating"}:
        return "starting"
    if status in {"stopping", "destroying", "archiving"}:
        return "stopping"
    return status or fallback


def is_managed_name(name):
    return bool(name) and str(name).startswith(MANAGED_NAME_PREFIX)


def box_display_name(info, fallback):
    if info is None:
        return fallback
    return info.name or fallback


class _LogWriter:
    def __init__(self, write):
        self._write = write

    def write(self, text):
        if text:
            self._write(text)

    def flush(self):
        return None


async def drain(stream, destination, captured):
    async for chunk in stream:
        text = chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else str(chunk)
        captured.append(text)
        if destination is not None:
            destination.write(text)
            destination.flush()


async def run_checked(box, command, *, args=None, user=None, timeout_secs=None, log=None):
    execution = await box.exec(
        command,
        args=args,
        user=user,
        timeout_secs=timeout_secs,
    )
    stdout = []
    stderr = []
    destination_out = _LogWriter(log) if log is not None else sys.stdout
    destination_err = _LogWriter(log) if log is not None else sys.stderr
    # Consume both streams before wait(), matching the SDK's SimpleBox contract.
    # Waiting concurrently can close REST streams before their output is read.
    await asyncio.gather(
        drain(execution.stdout(), destination_out, stdout),
        drain(execution.stderr(), destination_err, stderr),
    )
    result = await execution.wait()
    exit_code = result.exit_code
    if getattr(result, "error_message", None):
        if runtime_mode() != "cloud":
            raise RuntimeError("Execution transport failed")
        from hangar.cloud_exec import recover_execution

        identifier = execution.id() if callable(execution.id) else execution.id
        exit_code = await recover_execution(box.id, identifier, destination_out, destination_err, (timeout_secs or 90)+30)
    if exit_code != 0:
        detail = "".join(stderr)[-4000:].strip()
        raise RuntimeError(
            f"Command failed with exit code {exit_code}: {command}\n{detail}"
        )


async def ensure_started(runtime, box, mode):
    info = await runtime.get_info(box.id)
    if info is not None and info.state.running:
        return
    if mode == "cloud":
        await asyncio.to_thread(cloud_api, "POST", f"/api/v1/boxes/{box.id}/start", {})
    else:
        await box.start()
    for _ in range(60):
        info = await runtime.get_info(box.id)
        if info is not None and info.state.running:
            return
        await asyncio.sleep(1)
    raise RuntimeError(f"Box did not become running: {box.id}")


async def ensure_stopped(runtime, box, mode):
    info = await runtime.get_info(box.id)
    if info is None or not info.state.running:
        return
    if mode == "cloud":
        await asyncio.to_thread(cloud_api, "POST", f"/api/v1/boxes/{box.id}/stop", {})
    else:
        await box.stop()
    for _ in range(60):
        info = await runtime.get_info(box.id)
        if info is None or not info.state.running:
            return
        await asyncio.sleep(1)
    raise RuntimeError(f"Box did not stop: {box.id}")


async def delete_box(runtime, box_id, mode):
    if mode == "cloud":
        await asyncio.to_thread(cloud_api, "DELETE", f"/api/v1/boxes/{box_id}")
        return
    await runtime.remove(box_id, force=True)


async def wait_for_guest_health(box, *, attempts, timeout_secs, log=None):
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
        log=log,
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
    raise RuntimeError(f"Host could not reach {redact_url(url)}")
