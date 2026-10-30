import io
import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from jllm_plugins.llm import llm_backend
from jllm_plugins.llm.router import Plugin

from jllm.kernel import Kernel, discover, load_profile, resolve

PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "llm.yaml"


class LLMIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_restores_legacy_global_and_channel_routing(self):
        self.addCleanup(llm_backend._reset_config)
        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "old-selection.json"
            original = json.dumps({"version": 1, "backend": " ATMOS ", "read_mode": "tts",
                                   "channels": {"123": "local", "456": "invalid"}})
            state_path.write_text(original, encoding="utf-8")
            ctx = SimpleNamespace(
                config={"backend": "sarashina", "state_file": "old-selection.json",
                        "atmos": {"model": "dummy-model"}},
                dependencies={"storage.local": SimpleNamespace(
                    legacy_path=lambda name: Path(temp) / name)},
                scope=Mock(),
            )
            factory = Mock(side_effect=lambda name, config: SimpleNamespace(
                name=name, config=config, close=AsyncMock()))
            with patch("jllm_plugins.llm.router.build_translation_client", factory):
                with llm_backend.channel_context(123):
                    services = await Plugin().setup(ctx)
                client = services["llm.chat"]
                state = services["llm.read-mode"]
                self.assertEqual(client.backend, "atmos")
                self.assertEqual(client.inner.name, "atmos")
                self.assertEqual(client.inner.config["model"], "dummy-model")
                self.assertEqual(state.active_read_mode(), "tts")
                self.assertEqual(state.backend_for(123), "local")
                with llm_backend.channel_context(123):
                    self.assertEqual(client.inner.name, "local")
                with llm_backend.channel_context(456):
                    self.assertEqual(client.inner.name, "atmos")
                self.assertEqual(state_path.read_text(encoding="utf-8"), original)
                # Cog が先に保存した明示モードを switch が上書きしない。
                state.set_selection("sarashina", "wav")
                await client.switch("sarashina")
                self.assertEqual(state.active_read_mode(), "wav")
                self.assertEqual(client.backend, "sarashina")
                with self.assertRaises(ValueError):
                    await client.switch("not-a-backend")
                self.assertEqual(client.backend, "sarashina")
                await client.close()

    async def test_missing_corrupt_or_unknown_state_uses_config_without_logging_contents(self):
        self.addCleanup(llm_backend._reset_config)
        for content in (None, "{SECRET-DO-NOT-LOG",
                        '{"backend":"SECRET-DO-NOT-LOG","read_mode":"wav"}'):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temp:
                state_path = Path(temp) / "llm_backend.json"
                if content is not None:
                    state_path.write_text(content, encoding="utf-8")
                ctx = SimpleNamespace(
                    config={"backend": "local"},
                    dependencies={"storage.local": SimpleNamespace(
                        legacy_path=lambda name: Path(temp) / name)},
                    scope=Mock(),
                )
                output = io.StringIO()
                handler = logging.StreamHandler(output)
                root = logging.getLogger()
                root.addHandler(handler)
                try:
                    with patch("jllm_plugins.llm.router.build_translation_client",
                               side_effect=lambda name, config: SimpleNamespace(
                                   name=name, close=AsyncMock())):
                        services = await Plugin().setup(ctx)
                        client = services["llm.chat"]
                        self.assertEqual(client.inner.name, "local")
                        self.assertEqual(services["llm.read-mode"].active_read_mode(), "tts")
                        await client.close()
                finally:
                    root.removeHandler(handler)
                self.assertNotIn("SECRET-DO-NOT-LOG", output.getvalue())
                self.assertEqual(state_path.exists(), content is not None)
                if content is not None:
                    self.assertEqual(state_path.read_text(encoding="utf-8"), content)

    async def test_router_switches_backend_and_read_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = load_profile(PROFILE)
            profile.plugins["storage.local"]["config"]["data_dir"] = temp
            plan = resolve(profile, discover())
            kernel = Kernel(plan)
            try:
                await kernel.start()
                client = plan.nodes["llm.router/main"].services["llm.chat"]
                state = plan.nodes["llm.router/main"].services["llm.read-mode"]
                self.assertEqual(client.backend, "sarashina")
                self.assertEqual(state.active_read_mode(), "tts")
                await client.switch("atmos")
                self.assertEqual(client.backend, "atmos")
                # バックエンドを変えても読み上げモードは変わらない。
                self.assertEqual(state.active_read_mode(), "tts")
                self.assertEqual(state.backend_for(), "atmos")
                state.set_channel(123, "sarashina")
                self.assertEqual(state.backend_for(123), "sarashina")
                state.clear_channel(123)
                self.assertEqual(state.backend_for(123), "atmos")
                await client.switch("sarashina")
                self.assertEqual(state.active_read_mode(), "tts")
            finally:
                await kernel.stop()
            llm_backend._reset_config()
            self.assertTrue(all(node.state == "STOPPED" for node in plan.nodes.values()))


if __name__ == "__main__":
    unittest.main()
