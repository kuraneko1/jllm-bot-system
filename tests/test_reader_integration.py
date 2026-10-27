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

    def event(self, handler):
        self.events.append(handler)
        return handler


class ReaderIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_reader_profile_starts_without_gateway_connection(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = load_profile(PROFILE)
            profile.plugins["storage.local"]["config"]["data_dir"] = temp
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
                    bot.add_cog.assert_awaited_once()
                    bot.start.assert_not_called()
                finally:
                    await kernel.stop()
            self.assertTrue(all(node.state == "STOPPED" for node in plan.nodes.values()))
            bot.close.assert_awaited()


if __name__ == "__main__":
    unittest.main()
