import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from jllm.kernel import Kernel, discover, load_profile, resolve

PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "reader.yaml"


class FakeBot:
    def __init__(self):
        self.add_cog = AsyncMock()
        self.remove_cog = AsyncMock()
        self.close = AsyncMock()
        self.start = AsyncMock()
        self.events = []
        self.is_ready = lambda: True

    def event(self, handler):
        self.events.append(handler)
        return handler


class ReaderIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_reader_profile_starts_without_gateway_connection(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = load_profile(PROFILE)
            profile.plugins["storage.local"]["config"]["data_dir"] = temp
            toggles = Path(temp) / "custom" / "toggles.json"
            toggles.parent.mkdir()
            toggles.write_text('{"img": false, "wav": false}', encoding="utf-8")
            profile.plugins["feature.reader"]["config"]["toggles_path"] = str(toggles)
            profile.plugins["feature.reader"]["config"]["require_existing_toggles"] = True
            profile.plugins["feature.reader"]["config"]["mytalk_hidden_voices"] = ["custom"]
            profile.plugins["feature.miq"].setdefault("config", {})["words"] = ["qmiq"]
            plan = resolve(profile, discover())
            kernel = Kernel(plan)
            bot = FakeBot()
            with (
                patch.dict("os.environ", {"DISCORD_TOKEN": "secret"}, clear=True),
                patch("jllm_plugins.discord.plugin.commands.Bot", return_value=bot),
            ):
                try:
                    await kernel.start()
                    self.assertTrue(all(node.state == "RUNNING" for node in plan.nodes.values()))
                    self.assertEqual(bot.add_cog.await_count, 3)
                    reader = plan.nodes["feature.reader/main"].services["feature.reader"]
                    self.assertEqual(reader.config.toggles_path, str(toggles))
                    self.assertTrue(reader.config.require_existing_toggles)
                    self.assertEqual(reader.config.mytalk_hidden_voices, {"custom"})
                    self.assertIs(
                        reader.miq, plan.nodes["feature.miq/main"].services["feature.miq"]
                    )
                    self.assertTrue(reader.miq.matches_trigger("qmiq"))
                    self.assertFalse(reader.miq.matches_trigger("miq"))
                    self.assertIsNone(reader.kitasan)
                    self.assertEqual(reader.toggles, {"img": False, "wav": False})
                    reader.toggles["wav"] = True
                    reader._save_toggles()
                    self.assertIn('"wav": true', toggles.read_text(encoding="utf-8"))
                    bot.start.assert_not_called()
                finally:
                    await kernel.stop()
            self.assertTrue(all(node.state == "STOPPED" for node in plan.nodes.values()))
            bot.close.assert_awaited()


if __name__ == "__main__":
    unittest.main()
