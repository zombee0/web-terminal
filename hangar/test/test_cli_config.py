import pathlib
import subprocess
import sys
import tempfile
import tomllib
import unittest

import json
import os
import re

from hangar.provision import AGY_INLINE_SCRIPT, AGY_SIGNIN_MARKER, CODEX_INLINE_SCRIPT, INSTALL_AGY_COMMAND, inline_cli_command


class CodexInlineConfigTest(unittest.TestCase):
    def configure(self, text=None):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / ".codex" / "config.toml"
            if text is not None:
                path.parent.mkdir()
                path.write_text(text)
            result = subprocess.run([sys.executable, "-c", CODEX_INLINE_SCRIPT, str(path)],
                                    capture_output=True, text=True, check=True)
            return path.read_text() if path.exists() else None, result.stderr

    def test_creates_config(self):
        text, _ = self.configure()
        self.assertEqual(tomllib.loads(text), {"tui": {"alternate_screen": "never"}})

    def test_extends_existing_tui_table(self):
        original = 'model = "gpt-5"\n\n[tui]\nscreen_reader_detection_done = true\n\n[projects."/home/boxlite"]\ntrust_level = "trusted"\n'
        text, _ = self.configure(original)
        config = tomllib.loads(text)
        self.assertEqual(config["tui"], {"screen_reader_detection_done": True, "alternate_screen": "never"})
        self.assertEqual(config["model"], "gpt-5")
        self.assertEqual(config["projects"]["/home/boxlite"]["trust_level"], "trusted")
        self.assertEqual(self.configure(text)[0], text, "rerunning must not change the file")

    def test_appends_tui_table(self):
        text, _ = self.configure('model = "gpt-5"')
        self.assertEqual(tomllib.loads(text), {"model": "gpt-5", "tui": {"alternate_screen": "never"}})

    def test_keeps_user_choice(self):
        for original in ('[tui]\nalternate_screen = "always"\n', 'tui.alternate_screen = "auto"\n'):
            self.assertEqual(self.configure(original)[0], original)

    def test_leaves_unextendable_config(self):
        original = 'tui = { screen_reader_detection_done = true }\n'
        text, stderr = self.configure(original)
        self.assertEqual(text, original)
        self.assertIn("unchanged", stderr)

    def test_guest_command_runs_as_terminal_user_and_never_fails_provisioning(self):
        command = inline_cli_command()
        self.assertIn("runuser -u boxlite -- python3 -c", command)
        self.assertIn("/home/boxlite/.codex/config.toml || echo", command)
        self.assertIn("if [ -x /home/boxlite/.local/bin/agy ]", command)
        self.assertIn("/home/boxlite/.gemini/antigravity-cli/settings.json || echo", command)


class AgyTest(unittest.TestCase):
    def configure(self, text=None):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "antigravity-cli" / "settings.json"
            if text is not None:
                path.parent.mkdir()
                path.write_text(text)
            result = subprocess.run([sys.executable, "-c", AGY_INLINE_SCRIPT, str(path)],
                                    capture_output=True, text=True, check=True)
            return path.read_text() if path.exists() else None, result.stderr

    def test_inline_settings(self):
        self.assertEqual(json.loads(self.configure()[0]), {"altScreenMode": "never"})
        text, _ = self.configure('{"modelProvider": "gemini"}')
        self.assertEqual(json.loads(text), {"modelProvider": "gemini", "altScreenMode": "never"})
        self.assertEqual(self.configure('{"altScreenMode": "always"}')[0], '{"altScreenMode": "always"}')
        text, stderr = self.configure("not json")
        self.assertEqual(text, "not json")
        self.assertIn("unchanged", stderr)

    def test_signin_runs_once_in_the_first_terminal(self):
        snippet = re.search(r"<<'BASHRC'\n(.*?)\nBASHRC\n", INSTALL_AGY_COMMAND, re.S).group(1)
        self.assertEqual(AGY_SIGNIN_MARKER, "/home/boxlite/.config/web-terminal/agy-signin")
        with tempfile.TemporaryDirectory() as home:
            marker = pathlib.Path(home, ".config/web-terminal/agy-signin")
            marker.parent.mkdir(parents=True)
            marker.touch()
            bin_dir = pathlib.Path(home, "bin")
            bin_dir.mkdir()
            agy = bin_dir / "agy"
            agy.write_text("#!/bin/sh\necho started >> \"$HOME/agy-runs\"\n")
            agy.chmod(0o755)
            env = dict(os.environ, HOME=home, PATH=f"{bin_dir}:{os.environ['PATH']}")
            first = subprocess.run(["bash", "-c", snippet], env=env, capture_output=True, text=True, check=True)
            subprocess.run(["bash", "-c", snippet], env=env, capture_output=True, text=True, check=True)
            self.assertIn("tap the Google link", first.stdout)
            self.assertEqual(pathlib.Path(home, "agy-runs").read_text(), "started\n")
            self.assertFalse(marker.exists())

    def test_install_never_fails_provisioning(self):
        self.assertIn("continuing without agy", INSTALL_AGY_COMMAND)
        self.assertIn("exit 0", INSTALL_AGY_COMMAND)
        self.assertIn("https://antigravity.google/cli/install.sh", INSTALL_AGY_COMMAND)


if __name__ == "__main__":
    unittest.main()
