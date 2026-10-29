import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from jllm import cli


class ScaffoldTests(unittest.TestCase):
    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(args)
        return code, out.getvalue(), err.getvalue()

    def workspace(self, root):
        (root / "plugins/builtins").mkdir(parents=True)
        (root / "plugins/builtins/pyproject.toml").write_text(
            '[project.entry-points."jllm.plugins"]\n"fake.provider" = "x:plugin.json"\n',
            encoding="utf-8",
        )
        (root / "tests").mkdir()

    def test_creates_manifest_schema_source_entry_and_test(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.workspace(root)
            code, out, _ = self.run_cli(
                [
                    "new", "plugin", "feature.example",
                    "--root", directory,
                    "--capability", "example.output",
                ]
            )

            self.assertEqual(code, 0)
            plugin = root / "plugins/builtins/src/jllm_plugins/feature_example"
            for name in ("__init__.py", "plugin.json", "config.schema.json", "plugin.py"):
                self.assertTrue((plugin / name).is_file(), name)
            self.assertIn("example.output", (plugin / "plugin.json").read_text(encoding="utf-8"))
            self.assertTrue((root / "tests/test_feature_example.py").is_file())
            pyproject = (root / "plugins/builtins/pyproject.toml").read_text(encoding="utf-8")
            expected = '"feature.example" = "jllm_plugins.feature_example:plugin.json"'
            self.assertIn(expected, pyproject)
            self.assertIn("enabled: true", out)

    def test_rejects_invalid_or_existing_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.workspace(root)
            for ident in ("Feature", "feature", "feature..x", "a/b"):
                with self.subTest(ident=ident):
                    code, _, err = self.run_cli(["new", "plugin", ident, "--root", directory])
                    self.assertEqual(code, 1)
                    self.assertIn("plugin id", err)

            first, _, _ = self.run_cli(["new", "plugin", "feature.dup", "--root", directory])
            second, _, err = self.run_cli(["new", "plugin", "feature.dup", "--root", directory])
            self.assertEqual((first, second), (0, 1))
            self.assertIn("already exists", err)


if __name__ == "__main__":
    unittest.main()
