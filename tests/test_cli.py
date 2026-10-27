import contextlib
import io
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jllm import cli
from jllm.kernel import ProfileError


class CliTests(unittest.TestCase):
    def run_cli(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    @patch("jllm.cli.resolve", return_value=SimpleNamespace(nodes={}))
    @patch("jllm.cli.load_profile")
    @patch("jllm.cli.discover", return_value={})
    def test_profile_priority(self, discover, load, resolve):
        cases = [
            (["doctor"], "env.yaml", "env.yaml"),
            (["doctor", "positional.yaml"], "env.yaml", "positional.yaml"),
            (["doctor", "--profile", "option.yaml"], "env.yaml", "option.yaml"),
            (["--profile", "option.yaml", "doctor"], "env.yaml", "option.yaml"),
            (["config", "--profile", "option.yaml", "validate"], "env.yaml", "option.yaml"),
            (["config", "validate", "--profile", "option.yaml"], "env.yaml", "option.yaml"),
        ]
        for args, env, expected in cases:
            with self.subTest(args=args, env=env), patch.dict(os.environ, {"JLLM_PROFILE": env}):
                code, _, _ = self.run_cli(args)
                self.assertEqual(code, 0)
                load.assert_called_with(Path(expected))

    @patch("jllm.cli.load_profile")
    def test_profile_is_required(self, load):
        for args in (["doctor"], ["config", "validate"]):
            for env in ({}, {"JLLM_PROFILE": ""}):
                with self.subTest(args=args, env=env), patch.dict(os.environ, env, clear=True):
                    code, _, err = self.run_cli(args)
                    self.assertNotEqual(code, 0)
                    self.assertIn("--profile", err)
                    self.assertIn("JLLM_PROFILE", err)
                    load.assert_not_called()

    @patch("jllm.cli.discover")
    def test_plugins_list_reports_discovered_ids_and_versions(self, discover):
        discover.return_value = {"provider.fake": SimpleNamespace(version="1.0.0")}
        with patch.dict(os.environ, {}, clear=True):
            code, out, _ = self.run_cli(["plugins", "list"])
        self.assertEqual(code, 0)
        self.assertIn("provider.fake", out)
        self.assertIn("1.0.0", out)

    @patch("jllm.cli.resolve")
    @patch("jllm.cli.load_profile")
    @patch("jllm.cli.discover", return_value={})
    def test_doctor_reports_blocked_reason(self, discover, load, resolve):
        resolve.return_value = SimpleNamespace(
            nodes={"consumer/main": SimpleNamespace(state="BLOCKED", reason="missing dependency")}
        )
        code, out, _ = self.run_cli(["doctor", "profile.yaml"])
        self.assertEqual(code, 1)
        self.assertIn("consumer/main", out)
        self.assertIn("BLOCKED", out)
        self.assertIn("missing dependency", out)

    @patch("jllm.cli.resolve", side_effect=ProfileError("invalid configuration"))
    @patch("jllm.cli.load_profile")
    @patch("jllm.cli.discover", return_value={})
    def test_config_validate_rejects_invalid_profile(self, discover, load, resolve):
        code, _, err = self.run_cli(["config", "validate", "profile.yaml"])
        self.assertNotEqual(code, 0)
        self.assertIn("invalid configuration", err)
