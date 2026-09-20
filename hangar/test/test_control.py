import json
import asyncio
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.request import Request, urlopen

from hangar import boxlite as wt
from hangar import control


class FakeState:
    def __init__(self, running=True, status="running"):
        self.running = running
        self.status = status


class FakeInfo:
    def __init__(self, box_id, name, running=True, status="running"):
        self.id = box_id
        self.name = name
        self.state = FakeState(running, status)
        self.image = "python"
        self.created_at = None


class FakeBox:
    def __init__(self, box_id, name):
        self.id = box_id
        self.name = name


class FakeRunner:
    def __init__(self):
        self.infos = [
            FakeInfo("boxcloud1", "web-terminal-demo", running=True, status="running")
        ]
        self.created = []
        self.stopped = []
        self.resumed = []
        self.deleted = []
        self.ready = True

    async def terminal_ready(self, url):
        return bool(url) and self.ready

    async def list_info(self, mode):
        return list(self.infos)

    async def get_info(self, mode, box_id):
        for info in self.infos:
            if info.id == box_id:
                return info
        return None

    async def create(self, mode, *, name=None, host_port=None, resources=None, log=None):
        box = FakeBox("boxnew1", name or "web-terminal-new")
        self.created.append((mode, box.name, host_port, resources))
        if log:
            log("created\n")
        self.infos.append(FakeInfo(box.id, box.name))
        return box, box.name

    async def provision(self, mode, box, *, host_port=None, log=None):
        if log:
            log("provisioned\n")
        return self.access_url(mode, box.id, host_port)

    async def resolve(self, mode, box_id):
        info = await self.get_info(mode, box_id)
        if info is None:
            raise RuntimeError(f"Box not found: {box_id}")
        return FakeBox(info.id, info.name), info.name

    async def stop(self, mode, box):
        self.stopped.append((mode, box.id))
        for info in self.infos:
            if info.id == box.id:
                info.state.running = False
                info.state.status = "stopped"

    async def resume(self, mode, box, *, host_port=None, log=None):
        self.resumed.append((mode, box.id, host_port))
        if log:
            log("resumed\n")
        for info in self.infos:
            if info.id == box.id:
                info.state.running = True
                info.state.status = "running"
        return self.access_url(mode, box.id, host_port)

    async def delete(self, mode, box_id):
        self.deleted.append((mode, box_id))
        self.infos = [info for info in self.infos if info.id != box_id]

    def access_url(self, mode, box_id, host_port=None):
        if mode == "cloud":
            return f"https://preview.example/{box_id}?BOXLITE_BOX_AUTH_KEY=secret-token"
        return f"http://127.0.0.1:{host_port or 3001}"


class ControlServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmpdir.name) / "state.json"
        self.runner = FakeRunner()
        self.cloud_patch = mock.patch("hangar.control.cloud_ready", return_value=True)
        self.cloud_patch.start()
        self.service = control.ControlService(self.state_path, runner=self.runner)

    def tearDown(self):
        self.cloud_patch.stop()
        self.tmpdir.cleanup()

    def test_inventory_roundtrip(self):
        self.service.upsert_record(
            {
                "id": "boxcloud1",
                "name": "web-terminal-demo",
                "runtime": "cloud",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        )
        record = self.service.record_for("boxcloud1")
        self.assertEqual(record["name"], "web-terminal-demo")
        self.service.remove_record("boxcloud1")
        self.assertIsNone(self.service.record_for("boxcloud1"))

    def test_list_includes_access_url(self):
        boxes = self.service.run(self.service.list_boxes("cloud"))
        self.assertEqual(len(boxes), 1)
        self.assertEqual(boxes[0]["id"], "boxcloud1")
        self.assertIn("BOXLITE_BOX_AUTH_KEY=secret-token", boxes[0]["url"])

    def test_running_requires_terminal_health(self):
        self.runner.ready = False
        boxes = self.service.run(self.service.list_boxes("cloud"))
        self.assertEqual(boxes[0]["status"], "preparing")
        self.assertIsNone(boxes[0]["url"])

        self.runner.ready = True
        boxes = self.service.run(self.service.list_boxes("cloud"))
        self.assertEqual(boxes[0]["status"], "running")
        self.assertIsNotNone(boxes[0]["url"])
        self.runner.infos[0].state = FakeState(False, "stopped")
        boxes = self.service.run(self.service.list_boxes("cloud"))
        self.assertEqual(boxes[0]["status"], "stopped")
        self.assertIsNone(boxes[0]["url"])

    def test_starting_box_is_busy_and_preparing(self):
        self.runner.infos[0].state = FakeState(False, "starting")
        boxes = self.service.run(self.service.list_boxes("cloud"))
        self.assertEqual(boxes[0]["status"], "preparing")
        self.assertTrue(boxes[0]["busy"])
        self.assertIsNone(boxes[0]["url"])

    def test_provisioning_stays_preparing_until_completed(self):
        entered = threading.Event()
        release = threading.Event()

        async def provision(*args, **kwargs):
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            return "http://127.0.0.1:3001"

        self.runner.provision = provision
        job = self.service.create_box("embedded")
        try:
            self.assertTrue(entered.wait(2))
            boxes = self.service.run(self.service.list_boxes("embedded"))
            box = next(box for box in boxes if box["id"] == "boxnew1")
            self.assertEqual(box["status"], "preparing")
            self.assertTrue(box["busy"])
            self.assertIsNone(box["url"])
        finally:
            release.set()
            self._wait_job(job["id"])
        boxes = self.service.run(self.service.list_boxes("embedded"))
        box = next(box for box in boxes if box["id"] == "boxnew1")
        self.assertEqual(box["status"], "running")
        self.assertFalse(box["busy"])

    def test_embedded_list_does_not_reuse_default_port(self):
        self.runner.infos = [
            FakeInfo("oldbox", "web-terminal-e2e"),
            FakeInfo("newbox", "web-terminal-new"),
        ]
        self.service.upsert_record(
            {
                "id": "newbox",
                "name": "web-terminal-new",
                "runtime": "embedded",
                "host_port": 3002,
            }
        )
        boxes = self.service.run(self.service.list_boxes("embedded"))
        by_id = {box["id"]: box for box in boxes}
        self.assertEqual(by_id["newbox"]["host_port"], 3002)
        self.assertIsNone(by_id["oldbox"]["host_port"])
        self.assertIsNone(by_id["oldbox"]["url"])

    def test_create_stop_resume_delete(self):
        job = self.service.create_box("embedded", "web-terminal-local")
        self._wait_job(job["id"])
        done = self.service.get_job(job["id"])
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["box_id"], "boxnew1")
        self.assertTrue(done["url"].startswith("http://127.0.0.1:"))
        self.assertEqual(self.service.record_for("boxnew1")["runtime"], "embedded")
        self.assertEqual(self.service.record_for("boxnew1")["cpus"], 1)

        stop = self.service.stop_box("boxnew1", "embedded")
        self._wait_job(stop["id"])
        self.assertEqual(self.service.get_job(stop["id"])["status"], "done")
        self.assertEqual(self.runner.stopped, [("embedded", "boxnew1")])

        resume = self.service.resume_box("boxnew1", "embedded")
        self._wait_job(resume["id"])
        self.assertEqual(self.service.get_job(resume["id"])["status"], "done")
        self.assertEqual(self.runner.resumed[0][:2], ("embedded", "boxnew1"))

        delete = self.service.delete_box("boxnew1", "embedded")
        self._wait_job(delete["id"])
        self.assertEqual(self.service.get_job(delete["id"])["status"], "done")
        self.assertIsNone(self.service.record_for("boxnew1"))

    def test_create_uses_resources(self):
        job = self.service.create_box(
            "embedded",
            "web-terminal-fat",
            {"cpus": 2, "memory_mib": 2048, "disk_size_gb": 20},
        )
        self._wait_job(job["id"])
        self.assertEqual(
            self.runner.created[0][3],
            {"cpus": 2, "memory_mib": 2048, "disk_size_gb": 20},
        )
        record = self.service.record_for("boxnew1")
        self.assertEqual(record["memory_mib"], 2048)
        self.assertEqual(record["disk_size_gb"], 20)

    def _wait_job(self, job_id, timeout=3):
        deadline = __import__("time").time() + timeout
        while __import__("time").time() < deadline:
            job = self.service.get_job(job_id)
            if job and job["status"] in {"done", "error"}:
                if job["status"] == "error":
                    self.fail(job["error"])
                return job
            __import__("time").sleep(0.05)
        self.fail(f"job {job_id} did not finish")


class ControlHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.runner = FakeRunner()
        self.cloud_patch = mock.patch("hangar.control.cloud_ready", return_value=True)
        self.cloud_patch.start()
        self.service = control.ControlService(
            Path(self.tmpdir.name) / "state.json", runner=self.runner
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), control.make_handler(self.service))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base = f"http://{host}:{port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.cloud_patch.stop()
        self.tmpdir.cleanup()

    def fetch(self, path, method="GET", payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        request = Request(self.base + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        with urlopen(request, timeout=5) as response:
            body = response.read()
            return response.status, body

    def test_health_and_page(self):
        status, body = self.fetch("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body.decode().strip(), "ok")
        status, body = self.fetch("/")
        html = body.decode()
        self.assertEqual(status, 200)
        self.assertIn("HANGAR", html)
        self.assertIn("SANDBOX CONTROL PLANE", html)
        self.assertIn("https://github.com/boxlite-ai/boxlite", html)
        self.assertIn("PROVISION", html)
        self.assertIn("STOP", html)
        self.assertIn("RESUME", html)
        self.assertIn("DELETE", html)
        self.assertIn("/api/boxes", html)
        self.assertIn('id="btn-local" class="active"', html)
        self.assertIn('runtime: "embedded"', html)
        self.assertLess(html.index('id="btn-local"'), html.index('id="btn-cloud"'))
        self.assertIn("formatAccess", html)
        self.assertIn("table-layout: fixed", html)
        self.assertNotIn("box-card", html)
        self.assertIn('id="res-cpus"', html)
        self.assertIn('id="res-memory"', html)
        self.assertIn('id="res-disk"', html)

    def test_status_and_list(self):
        status, body = self.fetch("/api/status")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIn("cloud", payload)
        self.assertTrue(payload["embedded"]["ready"])
        status, body = self.fetch("/api/boxes?runtime=cloud")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["boxes"][0]["id"], "boxcloud1")
        self.assertIn("BOXLITE_BOX_AUTH_KEY", payload["boxes"][0]["url"])

    def test_unknown_runtime_rejected(self):
        request = Request(self.base + "/api/boxes?runtime=docker")
        with self.assertRaises(Exception):
            urlopen(request, timeout=5)


class ParseRuntimeTest(unittest.TestCase):
    def test_aliases(self):
        self.assertEqual(wt.parse_runtime("local"), "embedded")
        self.assertEqual(wt.parse_runtime("REST"), "cloud")


class TerminalHealthTest(unittest.TestCase):
    def test_probe_preserves_auth_query(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = b"ok\n"
        with mock.patch("hangar.control.urllib.request.urlopen", return_value=response) as request:
            ready = asyncio.run(control.BoxRunner().terminal_ready(
                "https://preview.example/terminal?BOXLITE_BOX_AUTH_KEY=test"
            ))
        self.assertTrue(ready)
        self.assertEqual(request.call_args.args[0],
                         "https://preview.example/terminal/healthz?BOXLITE_BOX_AUTH_KEY=test")

    def test_unreachable_or_nonterminal_endpoint_is_not_ready(self):
        runner = control.BoxRunner()
        with mock.patch("hangar.control.urllib.request.urlopen", side_effect=OSError):
            self.assertFalse(asyncio.run(runner.terminal_ready("http://127.0.0.1:3001")))
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = b"login page"
        with mock.patch("hangar.control.urllib.request.urlopen", return_value=response):
            self.assertFalse(asyncio.run(runner.terminal_ready("http://127.0.0.1:3001")))


if __name__ == "__main__":
    unittest.main()
