import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

from jllm_plugins.llm import llm_backend
from webauthn.helpers import bytes_to_base64url

from jllm import cli
from jllm.kernel import Kernel, ProfileError, discover, load_profile, resolve

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles" / "production.example.yaml"


class ProductionProfileTests(unittest.TestCase):
    def setUp(self):
        self.profile = load_profile(PROFILE)
        self.manifests = discover()

    def test_includes_combined_features_with_one_discord_and_tts_router(self):
        expected = set()
        for name in ("reader", "translation", "dashboard", "wavrank"):
            expected.update(load_profile(ROOT / "profiles" / f"{name}.yaml").plugins)
        expected.remove("tts.direct")
        expected.add("tts.router")
        self.assertTrue(expected <= self.profile.plugins.keys())
        plan = resolve(self.profile, self.manifests)
        self.assertTrue(all(node.state == "PENDING" for node in plan.nodes.values()))
        self.assertEqual(sum(n.manifest.id == "adapter.discord" for n in plan.nodes.values()), 1)
        self.assertEqual(plan.nodes["tts.router/main"].bindings["tts.provider"],
                         ["provider.tts.voicevox/example"])
        self.assertEqual(plan.nodes["feature.reader/main"].bindings["tts.synthesis"],
                         ["tts.router/main"])
        self.assertEqual(plan.nodes["web.dashboard/main"].bindings["tts.synthesis"],
                         ["tts.router/main"])
        self.assertEqual(plan.nodes["llm.router/main"].bindings["llm.provider"],
                         ["provider.llm.sarashina/example"])

    def test_fails_closed_and_uses_only_local_example_endpoints(self):
        plugins = self.profile.plugins
        self.assertTrue(plugins["adapter.discord"]["critical"])
        self.assertEqual(plugins["adapter.discord"]["config"], {"token_env": "DISCORD_TOKEN"})
        dashboard = plugins["web.dashboard"]
        self.assertTrue(dashboard["critical"])
        self.assertTrue(dashboard["config"]["require_existing_passkey"])
        self.assertEqual(dashboard["config"]["host"], "127.0.0.1")
        self.assertEqual(dashboard["config"]["public_url"], "http://127.0.0.1:8765")
        for name in ("feature.reader", "feature.translation"):
            self.assertEqual(plugins[name]["config"]["allowed_channel_ids"], [0])
        self.assertEqual(plugins["feature.translation"]["config"]["talk_mention_gate"], "session")
        self.assertEqual(plugins["feature.wavrank"]["config"]["channel_id"], "0")
        self.assertFalse(plugins["tts.router"]["config"]["failover"])
        for plugin, option in (("tts.router", "require_existing_state"),
                               ("feature.reader", "require_existing_toggles")):
            self.assertTrue(plugins[plugin]["critical"])
            self.assertTrue(plugins[plugin]["config"][option])

        def check_urls(value):
            if isinstance(value, dict):
                for child in value.values():
                    check_urls(child)
            elif isinstance(value, list):
                for child in value:
                    check_urls(child)
            elif isinstance(value, str) and value.startswith(("http://", "https://")):
                self.assertEqual(urlsplit(value).hostname, "127.0.0.1")

        check_urls(plugins)

    def test_bad_binding_and_missing_critical_dependency_are_rejected(self):
        bad = copy.deepcopy(self.profile)
        bad.bindings["tts.router"]["tts.provider"] = ["provider.tts.voicevox/missing"]
        with self.assertRaises(ProfileError):
            resolve(bad, self.manifests)
        bad = copy.deepcopy(self.profile)
        bad.plugins["dictionary.reading"]["enabled"] = False
        with self.assertRaises(ProfileError):
            resolve(bad, self.manifests)

    def test_doctor_and_validate_without_starting_plugins(self):
        with (
            contextlib.redirect_stdout(io.StringIO()) as out,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(cli.main(["doctor", str(PROFILE)]), 0)
            self.assertNotIn("BLOCKED", out.getvalue())
            self.assertEqual(cli.main(["config", "validate", str(PROFILE)]), 0)


class ProductionStartupTests(unittest.IsolatedAsyncioTestCase):
    def prepare_state(self, root):
        data = root / "data"
        data.mkdir()
        (data / "tts_state.json").write_text(json.dumps({
            "preferred": "provider.tts.voicevox/example",
            "active": "provider.tts.voicevox/example",
            "failover": False,
        }), encoding="utf-8")
        (data / "toggles.json").write_text(
            json.dumps({"img": False, "wav": False}), encoding="utf-8"
        )

    async def test_all_plugins_start_and_close_without_external_connections(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.chdir(temp):
            root = Path(temp)
            profile = load_profile(PROFILE)
            profile.plugins["storage.local"]["config"]["data_dir"] = str(root / "data")
            profile.plugins["feature.wavrank"]["config"]["data_dir"] = str(root / "data")
            profile.plugins["web.dashboard"]["config"]["port"] = 0
            self.prepare_state(root)
            auth = root / "data" / ".dashboard-auth.passkeys.json"
            auth.write_text(json.dumps({
                "user_id": bytes_to_base64url(bytes(32)),
                "credentials": [{"id": bytes_to_base64url(b"test-id"),
                                 "public_key": bytes_to_base64url(b"test-key"),
                                 "sign_count": 0, "name": "test"}],
            }), encoding="utf-8")
            plan = resolve(profile, discover())
            kernel = Kernel(plan)
            try:
                with (
                    patch.dict(
                        "os.environ", {"DISCORD_TOKEN": "fake-token", "HOME": temp}, clear=True
                    ),
                    patch(
                        "socket.socket.connect", side_effect=AssertionError("network forbidden")
                    ) as connect,
                    patch("discord.Client.start", new_callable=AsyncMock) as discord_start,
                    patch("jllm_plugins.tts_router.plugin.TTSRouter.probe",
                          new_callable=AsyncMock, return_value=True) as probe,
                ):
                    try:
                        await kernel.start()
                        failures = {n.id: (n.state, n.reason) for n in plan.nodes.values()
                                    if n.state != "RUNNING"}
                        self.assertEqual(failures, {})
                        self.assertEqual(len(plan.nodes), 31)
                        gateway = plan.nodes["adapter.discord/main"].services["discord.gateway"]
                        bot = gateway.bot
                        self.assertEqual(set(bot.cogs), {
                            "ReaderCog", "TranslationCog", "MiqCog", "UmigameCog", "WavRankCog",
                        })
                        dashboard = plan.nodes["web.dashboard/main"].services["web.dashboard"]
                        self.assertTrue(dashboard.require_existing_passkey)
                        self.assertEqual(dashboard._passkey_path, auth)
                        self.assertTrue(dashboard._passkeys["credentials"])
                        self.assertIsNotNone(dashboard._runner)
                        discord_start.assert_not_awaited()
                        connect.assert_not_called()
                        probe.assert_awaited()
                    finally:
                        await kernel.stop()
                    self.assertEqual({n.state for n in plan.nodes.values()}, {"STOPPED"})
                    self.assertTrue(all(n.scope._closed for n in plan.nodes.values()))
                    self.assertEqual(dict(bot.cogs), {})
            finally:
                llm_backend._reset_config()

    async def test_missing_passkey_aborts_critical_dashboard_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.chdir(temp):
            self.prepare_state(Path(temp))
            profile = load_profile(PROFILE)
            profile.plugins["storage.local"]["config"]["data_dir"] = str(Path(temp) / "data")
            profile.plugins["feature.wavrank"]["config"]["data_dir"] = str(Path(temp) / "data")
            profile.plugins["web.dashboard"]["config"]["port"] = 0
            plan = resolve(profile, discover())
            kernel = Kernel(plan)
            try:
                with (
                    patch.dict(
                        "os.environ", {"DISCORD_TOKEN": "fake-token", "HOME": temp}, clear=True
                    ),
                    patch(
                        "socket.socket.connect", side_effect=AssertionError("network forbidden")
                    ) as connect,
                    patch("discord.Client.start", new_callable=AsyncMock) as discord_start,
                    patch("jllm_plugins.tts_router.plugin.TTSRouter.probe",
                          new_callable=AsyncMock, return_value=True),
                ):
                    try:
                        with self.assertRaisesRegex(
                            RuntimeError, "critical plugin failed: web.dashboard/main"
                        ):
                            await kernel.start()
                        dashboard = plan.nodes["web.dashboard/main"]
                        self.assertEqual(dashboard.state, "FAILED")
                        self.assertNotIn("web.dashboard", dashboard.services)
                        self.assertTrue(
                            all(n.scope._closed for n in plan.nodes.values() if n.scope)
                        )
                        self.assertNotIn("RUNNING", {n.state for n in plan.nodes.values()})
                        discord_start.assert_not_awaited()
                        connect.assert_not_called()
                    finally:
                        await kernel.stop()
            finally:
                llm_backend._reset_config()

    async def test_invalid_or_missing_state_aborts_before_discord_activation(self):
        cases = (
            ("tts_state.json", None, "tts.router/main"),
            ("tts_state.json", {"preferred": "unknown"}, "tts.router/main"),
            ("toggles.json", None, "feature.reader/main"),
            ("toggles.json", {"img": False, "wav": "yes"}, "feature.reader/main"),
        )
        for filename, value, failed in cases:
            with self.subTest(filename=filename, value=value):
                with tempfile.TemporaryDirectory() as temp, contextlib.chdir(temp):
                    root = Path(temp)
                    self.prepare_state(root)
                    path = root / "data" / filename
                    if value is None:
                        path.unlink()
                    else:
                        path.write_text(json.dumps(value), encoding="utf-8")
                    profile = load_profile(PROFILE)
                    profile.plugins["storage.local"]["config"]["data_dir"] = str(root / "data")
                    profile.plugins["feature.wavrank"]["config"]["data_dir"] = str(root / "data")
                    profile.plugins["web.dashboard"]["config"]["port"] = 0
                    plan = resolve(profile, discover())
                    try:
                        with (
                            patch.dict("os.environ", {"DISCORD_TOKEN": "fake-token", "HOME": temp},
                                       clear=True),
                            patch(
                                "socket.socket.connect",
                                side_effect=AssertionError("network forbidden"),
                            ) as connect,
                            patch("discord.Client.start", new_callable=AsyncMock) as discord_start,
                            patch("jllm.cli._activate", new_callable=AsyncMock) as activate,
                            patch("jllm_plugins.tts_router.plugin.TTSRouter.probe",
                                  new_callable=AsyncMock, return_value=True),
                            contextlib.redirect_stderr(io.StringIO()),
                        ):
                            self.assertEqual(await cli._run(plan), 1)
                            self.assertEqual(plan.nodes[failed].state, "FAILED")
                            self.assertTrue(
                                all(n.scope._closed for n in plan.nodes.values() if n.scope)
                            )
                            self.assertNotIn("RUNNING", {n.state for n in plan.nodes.values()})
                            activate.assert_not_awaited()
                            discord_start.assert_not_awaited()
                            connect.assert_not_called()
                    finally:
                        llm_backend._reset_config()


if __name__ == "__main__":
    unittest.main()
