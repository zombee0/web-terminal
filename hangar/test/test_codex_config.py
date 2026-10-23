import pathlib
import subprocess
import sys
import tempfile
import tomllib
import unittest

from hangar.provision import CODEX_INLINE_SCRIPT, codex_inline_command


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
        command = codex_inline_command()
        self.assertIn("runuser -u boxlite -- python3 -c", command)
        self.assertIn("/home/boxlite/.codex/config.toml || echo", command)


if __name__ == "__main__":
    unittest.main()
