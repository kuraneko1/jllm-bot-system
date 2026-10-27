import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from jllm_plugins.discord.plugin import DiscordGateway, Plugin


class FakeTree:
    def __init__(self):
        self.copy_global_to = Mock()
        self.sync = AsyncMock(return_value=[])
        self.clear_commands = Mock()

    def reset_mock(self):
        self.copy_global_to.reset_mock()
        self.sync.reset_mock()
        self.clear_commands.reset_mock()


class FakeBot:
    def __init__(self):
        self.tree = FakeTree()
        self.guilds = []
        self.user = "test-bot"
        self.add_cog = AsyncMock()
        self.remove_cog = AsyncMock()
        self.start = AsyncMock()
        self.close = AsyncMock()
        self.events = []

    def event(self, handler):
        self.events.append(handler)
        return handler


class DiscordGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_cog_registration_is_disposable(self):
        bot = FakeBot()
        gateway = DiscordGateway(bot, "secret")
        cog = SimpleNamespace(qualified_name="TestCog")
        remove = await gateway.add_cog(cog)
        bot.add_cog.assert_awaited_once_with(cog)
        await remove()
        bot.remove_cog.assert_awaited_once_with("TestCog")

    async def test_first_and_reconnect_command_sync(self):
        bot = FakeBot()
        bot.guilds = [SimpleNamespace(name="one"), SimpleNamespace(name="two")]
        gateway = DiscordGateway(bot, "secret")
        await gateway.on_ready()
        self.assertEqual(bot.tree.copy_global_to.call_count, 2)
        self.assertEqual(bot.tree.sync.await_count, 3)
        bot.tree.clear_commands.assert_called_once_with(guild=None)
        self.assertTrue(gateway._commands_ready)

        bot.tree.reset_mock()
        await gateway.on_ready()
        bot.tree.copy_global_to.assert_not_called()
        bot.tree.clear_commands.assert_not_called()
        self.assertEqual(bot.tree.sync.await_count, 2)

    async def test_sync_failure_does_not_block_reconnect(self):
        bot = FakeBot()
        bot.guilds = [SimpleNamespace(name="one")]
        response = SimpleNamespace(status=500, reason="error", headers={})
        bot.tree.sync.side_effect = discord.HTTPException(response, "boom")
        gateway = DiscordGateway(bot, "secret")
        with self.assertLogs("jllm_plugins.discord.plugin", level="ERROR"):
            await gateway.on_ready()
        self.assertTrue(gateway._commands_ready)
        with self.assertLogs("jllm_plugins.discord.plugin", level="ERROR"):
            await gateway.on_ready()
        self.assertEqual(bot.tree.sync.await_count, 3)

    async def test_activate_and_close_use_bot_without_exposing_token(self):
        bot = FakeBot()
        gateway = DiscordGateway(bot, "secret")
        await gateway.activate()
        bot.start.assert_awaited_once_with("secret")
        await gateway.close()
        bot.close.assert_awaited_once()


class DiscordPluginTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_token_is_rejected_without_value(self):
        plugin = Plugin()
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "DISCORD_TOKEN"):
                await plugin.setup(SimpleNamespace(config={}))

    async def test_setup_registers_ready_and_close(self):
        bot = FakeBot()
        scope = SimpleNamespace(add=Mock())
        ctx = SimpleNamespace(config={"token_env": "TEST_TOKEN"}, scope=scope)
        with (
            patch.dict("os.environ", {"TEST_TOKEN": "secret"}, clear=True),
            patch("jllm_plugins.discord.plugin.commands.Bot", return_value=bot),
            patch("jllm_plugins.discord.plugin.discord.Intents.default") as intents,
        ):
            intents.return_value = SimpleNamespace(message_content=False)
            services = await Plugin().setup(ctx)
        self.assertTrue(intents.return_value.message_content)
        self.assertEqual(len(bot.events), 1)
        self.assertIs(services["discord.gateway"].bot, bot)
        scope.add.assert_called_once_with(services["discord.gateway"].close)


if __name__ == "__main__":
    unittest.main()
