"""Host-side control plane for Web Terminal Box lifecycle."""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from webterminal.boxlite import (
    DEFAULT_REST_URL,
    cloud_ready,
    create_runtime,
    delete_box,
    ensure_stopped,
    is_managed_name,
    load_cloud_credentials,
    next_host_port,
    normalize_status,
    parse_resources,
    parse_runtime,
    published_host_port,
    prepare_runtime_env,
    redact_url,
    service_url,
)
from webterminal.provision import (
    create_managed_box,
    provision_web_terminal,
    resolve_box,
    resume_web_terminal,
)


CONTROL_HTML = pathlib.Path(__file__).resolve().parent / "control.html"
DEFAULT_CONTROL_HOST = "127.0.0.1"
DEFAULT_CONTROL_PORT = 3100
STATE_VERSION = 1


def default_state_path():
    override = os.environ.get("WEB_TERMINAL_STATE_PATH", "").strip()
    if override:
        return pathlib.Path(override).expanduser()
    return pathlib.Path.home() / ".web-terminal" / "control-state.json"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class BoxRef:
    def __init__(self, box_id):
        self.id = box_id


class BoxRunner:
    def __init__(self):
        self._runtimes = {}
        self._lock = threading.Lock()

    def runtime(self, mode, *, build=False):
        prepare_runtime_env(mode, build=build)
        with self._lock:
            cached = self._runtimes.get(mode)
            if cached is not None:
                return cached
            runtime = create_runtime(mode)
            self._runtimes[mode] = runtime
            return runtime

    async def create(self, mode, *, name=None, host_port=None, resources=None, log=None):
        runtime = self.runtime(mode, build=True)
        return await create_managed_box(
            runtime,
            mode,
            name=name,
            host_port=host_port,
            resources=resources,
            log=log,
        )

    async def provision(self, mode, box, *, host_port=None, log=None):
        runtime = self.runtime(mode, build=True)
        return await provision_web_terminal(
            runtime, box, mode, host_port=host_port, log=log
        )

    async def list_info(self, mode):
        runtime = self.runtime(mode, build=False)
        return await runtime.list_info()

    async def get_info(self, mode, box_id):
        runtime = self.runtime(mode, build=False)
        return await runtime.get_info(box_id)

    async def resolve(self, mode, box_id):
        runtime = self.runtime(mode, build=False)
        return await resolve_box(runtime, box_id)

    async def stop(self, mode, box):
        runtime = self.runtime(mode, build=False)
        await ensure_stopped(runtime, box, mode)

    async def resume(self, mode, box, *, host_port=None, log=None):
        runtime = self.runtime(mode, build=True)
        return await resume_web_terminal(
            runtime, box, mode, host_port=host_port, log=log
        )

    async def delete(self, mode, box_id):
        runtime = self.runtime(mode, build=False)
        await delete_box(runtime, box_id, mode)

    def access_url(self, mode, box_id, host_port=None):
        return service_url(mode, BoxRef(box_id), authenticated=True, host_port=host_port)


class ControlService:
    def __init__(self, state_path=None, runner=None):
        self.state_path = pathlib.Path(state_path or default_state_path())
        self.runner = runner or BoxRunner()
        self._state_lock = threading.Lock()
        self._jobs_lock = threading.Lock()
        self._jobs = {}
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro, timeout=None):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout)

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def load_state(self):
        with self._state_lock:
            if not self.state_path.is_file():
                return {"version": STATE_VERSION, "boxes": []}
            try:
                raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {"version": STATE_VERSION, "boxes": []}
            boxes = raw.get("boxes") if isinstance(raw, dict) else None
            if not isinstance(boxes, list):
                boxes = []
            return {"version": STATE_VERSION, "boxes": boxes}

    def _write_state(self, state):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.state_path)

    def upsert_record(self, record):
        with self._state_lock:
            if self.state_path.is_file():
                raw = json.loads(self.state_path.read_text(encoding="utf-8"))
                boxes = raw.get("boxes") if isinstance(raw, dict) else []
            else:
                boxes = []
            updated = []
            found = False
            for item in boxes:
                if item.get("id") == record["id"]:
                    merged = dict(item)
                    merged.update(record)
                    updated.append(merged)
                    found = True
                else:
                    updated.append(item)
            if not found:
                updated.append(dict(record))
            self._write_state({"version": STATE_VERSION, "boxes": updated})
            return dict(record)

    def remove_record(self, box_id):
        with self._state_lock:
            state = (
                json.loads(self.state_path.read_text(encoding="utf-8"))
                if self.state_path.is_file()
                else {"boxes": []}
            )
            boxes = [item for item in state.get("boxes", []) if item.get("id") != box_id]
            self._write_state({"version": STATE_VERSION, "boxes": boxes})

    def record_for(self, box_id):
        for item in self.load_state()["boxes"]:
            if item.get("id") == box_id:
                return item
        return None

    def inventory_for(self, mode):
        return [item for item in self.load_state()["boxes"] if item.get("runtime") == mode]

    def allocate_host_port(self, extra_used=None):
        used = [
            item.get("host_port")
            for item in self.load_state()["boxes"]
            if item.get("runtime") == "embedded"
        ]
        if extra_used:
            used.extend(extra_used)
        return next_host_port(used)

    async def used_embedded_ports(self):
        used = [
            item.get("host_port")
            for item in self.inventory_for("embedded")
            if item.get("host_port")
        ]
        try:
            for info in await self.runner.list_info("embedded"):
                port = published_host_port(info)
                if port:
                    used.append(port)
        except Exception:
            pass
        return used

    def status(self):
        return {
            "cloud": {
                "ready": cloud_ready(),
                "rest_url": os.environ.get("BOXLITE_REST_URL") or DEFAULT_REST_URL,
            },
            "embedded": {"ready": True},
            "control": {
                "state_path": str(self.state_path),
            },
        }

    def job_snapshot(self, job, *, include_logs=False):
        payload = {
            "id": job["id"],
            "action": job["action"],
            "status": job["status"],
            "runtime": job.get("runtime"),
            "box_id": job.get("box_id"),
            "name": job.get("name"),
            "error": job.get("error"),
            "url": job.get("url"),
            "created_at": job.get("created_at"),
            "updated_at": job.get("updated_at"),
        }
        if include_logs:
            payload["logs"] = list(job.get("logs") or [])
        return payload

    def get_job(self, job_id, *, include_logs=False):
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            return self.job_snapshot(job, include_logs=include_logs)

    def _append_log(self, job, chunk):
        text = str(chunk)
        if not text:
            return
        with self._jobs_lock:
            job["logs"].append(redact_url(text))
            job["updated_at"] = utc_now()

    def start_job(self, action, runtime, handler):
        job = {
            "id": uuid.uuid4().hex[:12],
            "action": action,
            "status": "running",
            "runtime": runtime,
            "box_id": None,
            "name": None,
            "error": None,
            "url": None,
            "logs": [],
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        with self._jobs_lock:
            self._jobs[job["id"]] = job

        async def runner():
            try:
                await handler(job)
                with self._jobs_lock:
                    if job["status"] == "running":
                        job["status"] = "done"
                        job["updated_at"] = utc_now()
            except Exception as exc:
                with self._jobs_lock:
                    job["status"] = "error"
                    job["error"] = str(exc)
                    job["updated_at"] = utc_now()
                    job["logs"].append(redact_url(f"ERROR {exc}\n"))
                traceback.print_exc()

        self.submit(runner())
        return self.job_snapshot(job)

    def resolve_runtime(self, box_id, requested=None):
        record = self.record_for(box_id)
        if requested and record and record.get("runtime") != requested:
            raise RuntimeError(
                f"Box {box_id} is registered as {record['runtime']}, not {requested}"
            )
        if requested:
            return requested
        if record:
            return record["runtime"]
        raise RuntimeError("runtime is required for boxes not tracked by this console")

    def require_runtime(self, mode):
        if mode == "cloud" and not cloud_ready():
            raise RuntimeError("BOXLITE_API_KEY is not set")

    async def list_boxes(self, mode):
        self.require_runtime(mode)
        inventory = {item["id"]: item for item in self.inventory_for(mode) if item.get("id")}
        live = {}
        try:
            for info in await self.runner.list_info(mode):
                live[info.id] = {
                    "id": info.id,
                    "name": info.name or "",
                    "status": normalize_status(info),
                    "image": getattr(info, "image", "") or "",
                    "cpus": getattr(info, "cpus", None),
                    "memory_mib": getattr(info, "memory_mib", None),
                    "host_port": published_host_port(info),
                    "created_at": _as_iso(getattr(info, "created_at", None)),
                }
        except Exception as exc:
            if not inventory:
                raise RuntimeError(f"Failed to list {mode} boxes: {exc}") from exc

        box_ids = set(inventory) | {
            box_id
            for box_id, item in live.items()
            if is_managed_name(item.get("name"))
        }
        boxes = []
        for box_id in sorted(box_ids):
            record = inventory.get(box_id, {})
            live_item = live.get(box_id)
            status = (live_item or {}).get("status") or "unknown"
            if live_item is None and box_id in inventory:
                status = "missing"
            host_port = record.get("host_port") or (live_item or {}).get("host_port")
            url = None
            if status not in {"missing", "unknown"}:
                if mode == "embedded" and not host_port:
                    url = None
                else:
                    try:
                        url = self.runner.access_url(mode, box_id, host_port)
                    except Exception:
                        if mode == "embedded" and host_port:
                            url = service_url(mode, BoxRef(box_id), host_port=host_port)
            boxes.append(
                {
                    "id": box_id,
                    "name": (live_item or {}).get("name") or record.get("name") or box_id,
                    "runtime": mode,
                    "status": status,
                    "image": (live_item or {}).get("image") or record.get("image") or "",
                    "host_port": host_port,
                    "cpus": (live_item or {}).get("cpus") or record.get("cpus"),
                    "memory_mib": (live_item or {}).get("memory_mib") or record.get("memory_mib"),
                    "disk_size_gb": record.get("disk_size_gb"),
                    "created_at": (live_item or {}).get("created_at")
                    or record.get("created_at"),
                    "url": url,
                    "tracked": box_id in inventory,
                }
            )
        return boxes

    def create_box(self, mode, name=None, resources=None):
        self.require_runtime(mode)
        spec = parse_resources(resources)

        async def handler(job):
            job["name"] = name
            host_port = None
            if mode == "embedded":
                host_port = next_host_port(await self.used_embedded_ports())
            box, box_name = await self.runner.create(
                mode,
                name=name,
                host_port=host_port,
                resources=spec,
                log=lambda chunk: self._append_log(job, chunk),
            )
            with self._jobs_lock:
                job["box_id"] = box.id
                job["name"] = box_name
            self.upsert_record(
                {
                    "id": box.id,
                    "name": box_name,
                    "runtime": mode,
                    "host_port": host_port,
                    "image": os.environ.get("BOXLITE_IMAGE", "python"),
                    "cpus": spec["cpus"],
                    "memory_mib": spec["memory_mib"],
                    "disk_size_gb": spec["disk_size_gb"],
                    "created_at": utc_now(),
                }
            )
            url = await self.runner.provision(
                mode,
                box,
                host_port=host_port,
                log=lambda chunk: self._append_log(job, chunk),
            )
            with self._jobs_lock:
                job["url"] = url

        return self.start_job("create", mode, handler)

    def stop_box(self, box_id, runtime=None):
        mode = self.resolve_runtime(box_id, runtime)
        self.require_runtime(mode)

        async def handler(job):
            job["box_id"] = box_id
            box, name = await self.runner.resolve(mode, box_id)
            job["name"] = name
            self._append_log(job, f"stopping {box_id}\n")
            await self.runner.stop(mode, box)
            self._append_log(job, "stopped\n")

        return self.start_job("stop", mode, handler)

    def resume_box(self, box_id, runtime=None):
        mode = self.resolve_runtime(box_id, runtime)
        self.require_runtime(mode)
        record = self.record_for(box_id) or {}
        host_port = record.get("host_port")

        async def handler(job):
            job["box_id"] = box_id
            box, name = await self.runner.resolve(mode, box_id)
            job["name"] = name
            url = await self.runner.resume(
                mode,
                box,
                host_port=host_port,
                log=lambda chunk: self._append_log(job, chunk),
            )
            with self._jobs_lock:
                job["url"] = url

        return self.start_job("resume", mode, handler)

    def delete_box(self, box_id, runtime=None):
        mode = self.resolve_runtime(box_id, runtime)
        self.require_runtime(mode)

        async def handler(job):
            job["box_id"] = box_id
            self._append_log(job, f"deleting {box_id}\n")
            try:
                await self.runner.delete(mode, box_id)
            except Exception as exc:
                if self.record_for(box_id) is None:
                    raise
                self._append_log(job, f"runtime delete failed: {exc}\n")
            self.remove_record(box_id)
            self._append_log(job, "deleted\n")

        return self.start_job("delete", mode, handler)


def _as_iso(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _json_body(handler):
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def make_handler(service):
    class ControlHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            message = fmt % args if args else fmt
            print(f"{self.address_string()} {redact_url(message)}")

        def _send(self, status, body, content_type="application/json; charset=utf-8"):
            payload = body if isinstance(body, (bytes, bytearray)) else body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _send_json(self, status, payload):
            self._send(status, json.dumps(payload), "application/json; charset=utf-8")

        def _error(self, status, message):
            self._send_json(status, {"error": message})

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            try:
                if path in {"/", "/index.html"}:
                    html = CONTROL_HTML.read_text(encoding="utf-8")
                    self._send(200, html, "text/html; charset=utf-8")
                    return
                if path == "/healthz":
                    self._send(200, "ok\n", "text/plain; charset=utf-8")
                    return
                if path == "/api/status":
                    self._send_json(200, service.status())
                    return
                if path == "/api/boxes":
                    mode = parse_runtime((query.get("runtime") or ["cloud"])[0])
                    boxes = service.run(service.list_boxes(mode), timeout=60)
                    self._send_json(200, {"runtime": mode, "boxes": boxes})
                    return
                if path.startswith("/api/jobs/") and path.endswith("/log"):
                    job_id = path[len("/api/jobs/") : -len("/log")]
                    self._stream_job(job_id)
                    return
                if path.startswith("/api/jobs/"):
                    job = service.get_job(path[len("/api/jobs/") :], include_logs=True)
                    if job is None:
                        self._error(404, "job not found")
                        return
                    self._send_json(200, job)
                    return
                self._error(404, "not found")
            except Exception as exc:
                self._error(400, str(exc))

        def do_POST(self):
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                body = _json_body(self)
            except json.JSONDecodeError:
                self._error(400, "invalid JSON")
                return
            try:
                if path == "/api/boxes":
                    mode = parse_runtime(body.get("runtime"))
                    name = (body.get("name") or "").strip() or None
                    resources = body.get("resources") or body
                    job = service.create_box(mode, name, resources)
                    self._send_json(202, {"job": job})
                    return
                if path.startswith("/api/boxes/") and path.endswith("/stop"):
                    box_id = path[len("/api/boxes/") : -len("/stop")]
                    runtime = parse_runtime(body.get("runtime")) if body.get("runtime") else None
                    job = service.stop_box(box_id, runtime)
                    self._send_json(202, {"job": job})
                    return
                if path.startswith("/api/boxes/") and path.endswith("/resume"):
                    box_id = path[len("/api/boxes/") : -len("/resume")]
                    runtime = parse_runtime(body.get("runtime")) if body.get("runtime") else None
                    job = service.resume_box(box_id, runtime)
                    self._send_json(202, {"job": job})
                    return
                self._error(404, "not found")
            except Exception as exc:
                self._error(400, str(exc))

        def do_DELETE(self):
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            try:
                body = {}
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._error(400, "invalid JSON")
                return
            try:
                if path.startswith("/api/boxes/") and len(path) > len("/api/boxes/"):
                    box_id = path[len("/api/boxes/") :]
                    raw_runtime = body.get("runtime") or (query.get("runtime") or [None])[0]
                    runtime = parse_runtime(raw_runtime) if raw_runtime else None
                    job = service.delete_box(box_id, runtime)
                    self._send_json(202, {"job": job})
                    return
                self._error(404, "not found")
            except Exception as exc:
                self._error(400, str(exc))

        def _stream_job(self, job_id):
            if service.get_job(job_id) is None:
                self._error(404, "job not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            cursor = 0
            while True:
                job = service.get_job(job_id, include_logs=True)
                if job is None:
                    self.wfile.write(b"event: error\ndata: {\"error\":\"job not found\"}\n\n")
                    self.wfile.flush()
                    return
                logs = job.get("logs") or []
                if cursor < len(logs):
                    for line in logs[cursor:]:
                        payload = json.dumps({"line": line})
                        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    cursor = len(logs)
                    self.wfile.flush()
                if job["status"] in {"done", "error"}:
                    payload = json.dumps(service.get_job(job_id))
                    self.wfile.write(f"event: end\ndata: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    return
                time.sleep(0.25)

    return ControlHandler


def control_bind():
    host = os.environ.get("WEB_TERMINAL_CONTROL_HOST", DEFAULT_CONTROL_HOST).strip() or DEFAULT_CONTROL_HOST
    raw_port = os.environ.get("WEB_TERMINAL_CONTROL_PORT", str(DEFAULT_CONTROL_PORT))
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise RuntimeError(f"Invalid WEB_TERMINAL_CONTROL_PORT: {raw_port}") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError(f"Invalid WEB_TERMINAL_CONTROL_PORT: {raw_port}")
    return host, port


def main():
    load_cloud_credentials()
    host, port = control_bind()
    service = ControlService()
    server = ThreadingHTTPServer((host, port), make_handler(service))
    print(f"CONTROL_URL=http://{host}:{port}", flush=True)
    print(f"CLOUD={'ready' if cloud_ready() else 'missing'}", flush=True)
    print("LOCAL=ready", flush=True)
    print(f"STATE={service.state_path}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("control plane stopped")
        server.shutdown()


if __name__ == "__main__":
    main()
