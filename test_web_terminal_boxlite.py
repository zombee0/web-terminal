import importlib.util
import os
import unittest
from unittest import mock

import web_terminal_boxlite as wt


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
            with mock.patch("web_terminal_boxlite.platform.machine", return_value="arm64"):
                self.assertEqual(wt.guest_goarch("embedded"), "arm64")
            with mock.patch("web_terminal_boxlite.platform.machine", return_value="x86_64"):
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
        self.assertFalse(options.auto_remove)
        self.assertTrue(options.detach)


if __name__ == "__main__":
    unittest.main()
