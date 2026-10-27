import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from jllm.kernel import Kernel, discover, load_profile, resolve

PROFILE_PATH = Path(__file__).resolve().parents[1] / "profiles" / "wavrank.yaml"


class FakeBot:
    def __init__(self):
        self.events = []
        self.add_cog = AsyncMock()
        self.remove_cog = AsyncMock()
        self.close = AsyncMock()
        self.start = AsyncMock()

    def event(self, handler):
        self.events.append(handler)
        return handler


class Phase2IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_profile_starts_all_plugins_without_gateway_connection(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = load_profile(PROFILE_PATH)
            profile.plugins["storage.local"]["config"]["data_dir"] = temp
            profile.plugins["feature.wavrank"]["config"]["data_dir"] = temp
            plan = resolve(profile, discover())
            bot = FakeBot()
            kernel = Kernel(plan)
            with (
                patch.dict("os.environ", {"DISCORD_TOKEN": "secret"}, clear=True),
                patch("jllm_plugins.discord.plugin.commands.Bot", return_value=bot),
            ):
                try:
                    await kernel.start()
                    self.assertTrue(all(node.state == "RUNNING" for node in plan.nodes.values()))
                    self.assertIn("feature.wavrank/main", plan.nodes)
                    self.assertIn("policy.restrictions/main", plan.nodes)
                    bot.start.assert_not_called()
                    bot.add_cog.assert_awaited_once()
                finally:
                    await kernel.stop()
            self.assertTrue(all(node.state == "STOPPED" for node in plan.nodes.values()))
            bot.close.assert_awaited()


if __name__ == "__main__":
    unittest.main()
