import logging
import os

import discord
from discord.ext import commands

logger = logging.getLogger(__name__)


class DiscordGateway:
    def __init__(self, bot: commands.Bot, token: str):
        self.bot = bot
        self._token = token
        self._commands_ready = False

    async def add_cog(self, cog: commands.Cog):
        await self.bot.add_cog(cog)

        async def remove():
            await self.bot.remove_cog(cog.qualified_name)

        return remove

    async def on_ready(self):
        logger.info("Bot起動完了: %s", self.bot.user)
        if not self._commands_ready:
            for guild in self.bot.guilds:
                try:
                    self.bot.tree.copy_global_to(guild=guild)
                    synced = await self.bot.tree.sync(guild=guild)
                    logger.info("ギルドコマンド同期完了: %s (%d件)", guild.name, len(synced))
                except discord.HTTPException:
                    logger.exception("ギルドコマンド同期に失敗: %s", guild.name)
            try:
                self.bot.tree.clear_commands(guild=None)
                await self.bot.tree.sync()
                logger.info("グローバルコマンドを削除")
            except discord.HTTPException:
                logger.exception("グローバルコマンドの削除に失敗")
            self._commands_ready = True
            return
        for guild in self.bot.guilds:
            try:
                await self.bot.tree.sync(guild=guild)
            except discord.HTTPException:
                logger.exception("ギルドコマンド同期に失敗: %s", guild.name)

    async def activate(self):
        await self.bot.start(self._token)

    async def close(self):
        await self.bot.close()


class Plugin:
    async def setup(self, ctx):
        token_env = ctx.config.get("token_env", "DISCORD_TOKEN")
        token = os.environ.get(token_env, "")
        if not token:
            raise ValueError(f"Discord token is not set: {token_env}")
        intents = discord.Intents.default()
        intents.message_content = True
        self.gateway = DiscordGateway(commands.Bot(command_prefix="!", intents=intents), token)
        self.gateway.bot.event(self.gateway.on_ready)
        ctx.scope.add(self.gateway.close)
        return {"discord.gateway": self.gateway}

    async def start(self):
        pass

    async def activate(self):
        await self.gateway.activate()

    async def stop(self):
        pass
