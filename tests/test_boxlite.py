import importlib.util
import os
import unittest
from unittest import mock

from webterminal import boxlite as wt


class RuntimeModeTest(unittest.TestCase):
    def test_default_is_cloud(self):
        env = os.environ.copy()
        env.pop("BOXLITE_RUNTIME", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(wt.runtime_mode(), "cloud")

    def test_aliases(self):
        cases = {
            "cloud": "cloud",
            "REST": "cloud",
            "remote": "cloud",
            "embedded": "embedded",
            "local": "embedded",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                with mock.patch.dict(os.environ, {"BOXLITE_RUNTIME": raw}):
                    self.assertEqual(wt.runtime_mode(), expected)

    def test_unknown_runtime(self):
        with mock.patch.dict(os.environ, {"BOXLITE_RUNTIME": "docker"}):
            with self.assertRaisesRegex(RuntimeError, "Unknown BOXLITE_RUNTIME"):
                wt.runtime_mode()


class GuestArchTest(unittest.TestCase):
    def test_cloud_defaults_to_amd64(self):
        env = os.environ.copy()
        env.pop("WEB_TERMINAL_GOARCH", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(wt.guest_goarch("cloud"), "amd64")

    def test_embedded_follows_host_arch(self):
        env = os.environ.copy()
        env.pop("WEB_TERMINAL_GOARCH", None)
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch("webterminal.boxlite.platform.machine", return_value="arm64"):
                self.assertEqual(wt.guest_goarch("embedded"), "arm64")
            with mock.patch("webterminal.boxlite.platform.machine", return_value="x86_64"):
                self.assertEqual(wt.guest_goarch("embedded"), "amd64")

    def test_override(self):
        with mock.patch.dict(os.environ, {"WEB_TERMINAL_GOARCH": "amd64"}):
            self.assertEqual(wt.guest_goarch("embedded"), "amd64")


class HostPortTest(unittest.TestCase):
    def test_default_matches_service_port(self):
        env = os.environ.copy()
        env.pop("BOXLITE_HOST_PORT", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(wt.host_bind_port(), wt.PORT)

    def test_rejects_invalid_values(self):
        for raw in ("0", "65536", "abc"):
            with self.subTest(raw=raw):
                with mock.patch.dict(os.environ, {"BOXLITE_HOST_PORT": raw}):
                    with self.assertRaisesRegex(RuntimeError, "Invalid BOXLITE_HOST_PORT"):
                        wt.host_bind_port()


class ServiceUrlTest(unittest.TestCase):
    def test_embedded_uses_loopback(self):
        with mock.patch.dict(os.environ, {"BOXLITE_HOST_PORT": "4010"}):
            self.assertEqual(wt.service_url("embedded", object()), "http://127.0.0.1:4010")

    def test_embedded_host_port_override(self):
        with mock.patch.dict(os.environ, {"BOXLITE_HOST_PORT": "4010"}):
            self.assertEqual(
                wt.service_url("embedded", object(), host_port=4015),
                "http://127.0.0.1:4015",
            )


class SecretRedactionTest(unittest.TestCase):
    def test_redacts_box_auth_key(self):
        url = "https://preview.example/?BOXLITE_BOX_AUTH_KEY=super-secret"
        self.assertEqual(wt.redact_url(url), "https://preview.example/?BOXLITE_BOX_AUTH_KEY=***")

    def test_leaves_local_url_alone(self):
        self.assertEqual(wt.redact_url("http://127.0.0.1:3001"), "http://127.0.0.1:3001")


class PublishedPortTest(unittest.TestCase):
    def test_reads_network_published_ports(self):
        info = mock.Mock()
        info.network.published_ports = [mock.Mock(guest_port=3001, host_port=4015)]
        self.assertEqual(wt.published_host_port(info), 4015)

    def test_missing_network(self):
        info = mock.Mock(spec=["id", "name"])
        self.assertIsNone(wt.published_host_port(info))


class PortAllocationTest(unittest.TestCase):
    def test_skips_used_and_busy_ports(self):
        with mock.patch.dict(os.environ, {"BOXLITE_HOST_PORT": "3001"}):
            with mock.patch("webterminal.boxlite.port_is_free", side_effect=lambda port, host="127.0.0.1": port != 3002):
                self.assertEqual(wt.next_host_port([3001]), 3003)


class CredentialLoadTest(unittest.TestCase):
    def test_parses_export_line(self):
        self.assertEqual(wt._parse_exported_key('export BOXLITE_API_KEY="abc"'), "abc")
        self.assertEqual(wt._parse_exported_key("BOXLITE_API_KEY=xyz"), "xyz")


class StatusNormalizeTest(unittest.TestCase):
    def test_running_flag(self):
        info = mock.Mock()
        info.state.running = True
        info.state.status = "started"
        self.assertEqual(wt.normalize_status(info), "running")

    def test_stopped(self):
        info = mock.Mock()
        info.state.running = False
        info.state.status = "stopped"
        self.assertEqual(wt.normalize_status(info), "stopped")


@unittest.skipUnless(importlib.util.find_spec("boxlite"), "boxlite is not installed")
class BoxOptionsTest(unittest.TestCase):
    def test_embedded_options_include_loopback_port(self):
        with mock.patch.dict(
            os.environ,
            {"BOXLITE_HOST_PORT": "4010", "BOXLITE_IMAGE": "python:slim"},
        ):
            options = wt.create_box_options("embedded")
        self.assertIn("python:slim", repr(options))
        self.assertFalse(options.auto_remove)
        self.assertTrue(options.detach)

    def test_cloud_options_construct(self):
        options = wt.create_box_options("cloud")
        self.assertIn(options.auto_remove, (None, False))
        self.assertTrue(options.detach)


class ResourceSpecTest(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(wt.parse_resources(None), wt.DEFAULT_RESOURCES)

    def test_custom_values(self):
        self.assertEqual(
            wt.parse_resources({"cpus": 2, "memory_mib": 2048, "disk_size_gb": 20}),
            {"cpus": 2, "memory_mib": 2048, "disk_size_gb": 20},
        )

    def test_rejects_small_memory(self):
        with self.assertRaisesRegex(RuntimeError, "memory_mib"):
            wt.parse_resources({"memory_mib": 128})


class CloudCreatePayloadTest(unittest.TestCase):
    def test_omits_legacy_auto_remove(self):
        payload = wt.cloud_create_payload("web-terminal-demo")
        self.assertEqual(payload["name"], "web-terminal-demo")
        self.assertNotIn("auto_remove", payload)
        self.assertNotIn("ports", payload)
        self.assertEqual(payload["auto_stop"], 0)
        self.assertEqual(payload["network"]["outbound"]["mode"], "enabled")

    def test_uses_requested_resources(self):
        payload = wt.cloud_create_payload(
            "web-terminal-demo",
            {"cpus": 4, "memory_mib": 4096, "disk_size_gb": 30},
        )
        self.assertEqual(payload["cpus"], 4)
        self.assertEqual(payload["memory_mib"], 4096)
        self.assertEqual(payload["disk_size_gb"], 30)


class BoxOptionsKwargsTest(unittest.TestCase):
    def test_embedded_options_accept_explicit_host_port(self):
        box_options = mock.Mock()
        network_spec = mock.Mock(return_value="net")
        with mock.patch(
            "webterminal.boxlite._import_boxlite",
            return_value=(None, None, box_options, network_spec, None),
        ):
            wt.create_box_options("embedded", host_port=4020)
        kwargs = box_options.call_args.kwargs
        self.assertEqual(kwargs["ports"][0]["host_port"], 4020)
        self.assertEqual(kwargs["ports"][0]["guest_port"], wt.PORT)
        self.assertFalse(kwargs["auto_remove"])

    def test_cloud_options_omit_auto_remove(self):
        box_options = mock.Mock()
        network_spec = mock.Mock(return_value="net")
        with mock.patch(
            "webterminal.boxlite._import_boxlite",
            return_value=(None, None, box_options, network_spec, None),
        ):
            with mock.patch("webterminal.boxlite._box_options_accepts", return_value=True):
                wt.create_box_options("cloud")
        kwargs = box_options.call_args.kwargs
        self.assertNotIn("auto_remove", kwargs)
        self.assertNotIn("ports", kwargs)


if __name__ == "__main__":
    unittest.main()
