import contextlib
import io
import os
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
    })


class GraphTests(unittest.TestCase):
    def run_cli(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(args)
        return code, stdout.getvalue()

    def profile(self, directory):
        path = Path(directory) / "profile.yaml"
        path.write_text(
            "plugins:\n  provider:\n    enabled: true\n  consumer:\n    enabled: true\n",
            encoding="utf-8",
        )
        return path

    def discovered(self):
        return {
            "provider": manifest("provider", provides=["echo"]),
            "consumer": manifest("consumer", requires=["echo"]),
        }

    def test_mermaid_lists_nodes_and_edges(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            profile = self.profile(directory)
            with patch("jllm.cli.discover", return_value=self.discovered()):
                code, out = self.run_cli(["graph", str(profile)])

        self.assertEqual(code, 0)
        self.assertIn("graph LR", out)
        self.assertIn('provider_main["provider"]', out)
        self.assertIn("provider_main -->|echo| consumer_main", out)

    def test_dot_quotes_the_same_edges(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            profile = self.profile(directory)
            with patch("jllm.cli.discover", return_value=self.discovered()):
                code, out = self.run_cli(["graph", "--format", "dot", str(profile)])

        self.assertEqual(code, 0)
        self.assertIn("digraph plugins {", out)
        self.assertIn('"provider/main" -> "consumer/main"', out)


if __name__ == "__main__":
    unittest.main()