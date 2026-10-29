import contextlib
import io
import os
import tempfile
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

    def test_explicit_environment_files_follow_legacy_priority(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"SHARED": "process", "PROCESS_ONLY": "present"}, clear=True
        ):
            env_file = Path(directory) / ".env"
            policy_file = Path(directory) / "bot-policy.env"
            env_file.write_text(
                "SHARED=env\nENV_ONLY=from_env\nJLLM_PROFILE=env.yaml\n"
                "TOKEN=synthetic-secret\nEXPANDED=${SHARED}\n", encoding="utf-8"
            )
            policy_file.write_text(
                "SHARED=policy\nPOLICY_EXPANDED=${ENV_ONLY}\n", encoding="utf-8"
            )
            with (
                patch("jllm.cli.discover", return_value={}),
                patch("jllm.cli.load_profile") as load,
                patch("jllm.cli.resolve", return_value=SimpleNamespace(nodes={})),
            ):
                code, out, err = self.run_cli(
                    ["doctor", "--env-file", str(env_file), "--policy-file", str(policy_file)]
                )
            self.assertEqual(code, 0)
            load.assert_called_once_with(Path("env.yaml"))
            self.assertEqual(os.environ["SHARED"], "policy")
            self.assertEqual(os.environ["EXPANDED"], "process")
            self.assertEqual(os.environ["POLICY_EXPANDED"], "from_env")
            self.assertEqual(os.environ["PROCESS_ONLY"], "present")
            self.assertNotIn("synthetic-secret", out + err)

    def test_no_automatic_dotenv_loading(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / ".env"
            path.write_text("JLLM_PROFILE=unexpected.yaml\n", encoding="utf-8")
            with contextlib.chdir(directory), patch("jllm.cli.load_profile") as load:
                code, _, _ = self.run_cli(["doctor"])
            self.assertEqual(code, 1)
            load.assert_not_called()

    def test_bad_or_missing_file_is_safe_and_atomic(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            env_file = Path(directory) / ".env"
            policy_file = Path(directory) / "policy"
            env_file.write_text("TOKEN=synthetic-secret\n", encoding="utf-8")
            for text in (None, "BAD=\"synthetic-secret\n"):
                if text is not None:
                    policy_file.write_text(text, encoding="utf-8")
                with self.subTest(text=text), patch("jllm.cli.load_profile") as load:
                    code, out, err = self.run_cli(
                        ["--env-file", str(env_file), "doctor", "--policy-file", str(policy_file)]
                    )
                self.assertEqual(code, 1)
                self.assertIn("cannot read or parse environment file", err)
                self.assertNotIn("synthetic-secret", out + err)
                self.assertNotIn("TOKEN", os.environ)
                load.assert_not_called()

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
