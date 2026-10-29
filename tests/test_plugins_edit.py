import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jllm import cli
from jllm.kernel import Manifest


def manifest(name, provides=(), requires=()):
    return Manifest.from_dict({
        "id": name,
        "version": "1.0.0",
        "api": ">=1.0,<2.0",
        "entry": name + ":Plugin",
        "provides": [{"capability": item, "version": 1} for item in provides],
        "requires": [{"capability": item, "version": 1} for item in requires],
        "config_schema": {},
    })


class PluginEditTests(unittest.TestCase):
    def setUp(self):
        self.discovered = {
            "provider": manifest("provider", provides=["echo"]),
            "consumer": manifest("consumer", requires=["echo"]),
            "lonely": manifest("lonely"),
        }

    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with (
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
            patch("jllm.cli.discover", return_value=self.discovered),
        ):
            code = cli.main(args)
        return code, out.getvalue(), err.getvalue()

    def profile(self, directory, body=None):
        path = Path(directory) / "profile.yaml"
        path.write_text(
            body
            or (
                "plugins:\n"
                "  provider:\n"
                "    enabled: true\n"
                "  consumer:\n"
                "    enabled: true\n"
                "bindings:\n"
                "  consumer:\n"
                "    echo: provider/main\n"
            ),
            encoding="utf-8",
        )
        return path

    def test_add_writes_the_block_and_keeps_the_rest(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.profile(directory)
            code, out, _ = self.run_cli(
                ["plugins", "add", "lonely", "--profile", str(path)]
            )
            text = path.read_text(encoding="utf-8")

        self.assertEqual(code, 0)
        self.assertIn("  lonely:\n    enabled: true", text)
        self.assertIn("  provider:\n    enabled: true", text)
        self.assertIn("enabled: true", out)

    def test_add_is_idempotent_and_reports_already_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.profile(directory)
            code, _, err = self.run_cli(
                ["plugins", "add", "provider", "--profile", str(path)]
            )

        self.assertEqual(code, 1)
        self.assertIn("already enabled", err)

    def test_invalid_config_leaves_the_profile_untouched(self):
        self.discovered["typed"] = Manifest.from_dict({
            "id": "typed",
            "version": "1.0.0",
            "api": ">=1.0,<2.0",
            "entry": "typed:Plugin",
            "provides": [],
            "requires": [],
            "config_schema": {
                "type": "object",
                "properties": {"count": {"type": "integer"}},
            },
        })
        with tempfile.TemporaryDirectory() as directory:
            path = self.profile(directory)
            before = path.read_text(encoding="utf-8")
            code, _, err = self.run_cli(
                [
                    "plugins", "add", "typed",
                    "--profile", str(path),
                    "--config", "count=abc",
                ]
            )
            after = path.read_text(encoding="utf-8")

        self.assertEqual(code, 1)
        self.assertEqual(before, after)
        self.assertIn("invalid config", err)

    def test_remove_refuses_while_something_uses_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.profile(directory)
            code, _, err = self.run_cli(
                ["plugins", "remove", "provider", "--profile", str(path)]
            )
            self.assertIn("  provider:", path.read_text(encoding="utf-8"))

            forced, _, _ = self.run_cli(
                ["plugins", "remove", "provider", "--profile", str(path), "--force"]
            )
            text = path.read_text(encoding="utf-8")

        self.assertEqual(code, 1)
        self.assertIn("is used by", err)
        self.assertEqual(forced, 0)
        self.assertNotIn("  provider:", text)
        self.assertIn("  consumer:", text)


if __name__ == "__main__":
    unittest.main()
