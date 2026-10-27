import importlib.util
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "repository_check",
    Path(__file__).resolve().parents[1] / "tools" / "check-repository.py",
)
repository_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(repository_check)


class RepositoryHygieneTests(unittest.TestCase):
    def test_runtime_data_and_secrets_are_rejected(self):
        names = (
            ".env",
            ".env.production",
            "data/user_voices.json",
            "nested/data/user_voices.json",
            "voice.wav",
            "session.sqlite-wal",
            "session.sqlite3",
            "archive.jsonl",
            "model.gguf",
        )
        for name in names:
            with self.subTest(name=name):
                self.assertIsNotNone(repository_check.path_problem(name))

    def test_source_documents_and_examples_are_allowed(self):
        names = (
            ".env.example",
            "README.md",
            "src/jllm/api.py",
            "docs/example.png",
            "plugins/builtins/src/jllm_plugins/fake_provider/plugin.json",
            "plugins/builtins/src/jllm_plugins/media/store.py",
        )
        for name in names:
            with self.subTest(name=name):
                self.assertIsNone(repository_check.path_problem(name))
