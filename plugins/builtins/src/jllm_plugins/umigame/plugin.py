from .cog import UmigameCog, UmigameConfig


class Plugin:
    async def setup(self, ctx):
        config = UmigameConfig(
            d1_api_url=ctx.config.get("d1_api_url", "http://127.0.0.1:8083"),
            d1_timeout_seconds=ctx.config.get("d1_timeout_seconds", 20.0),
            idle_minutes=ctx.config.get("idle_minutes", 30),
        )
        self.cog = UmigameCog(
            ctx.dependencies["discord.gateway"].bot,
            config=config,
            restrictions=ctx.dependencies["policy.restrictions"],
        )
        remove = await ctx.dependencies["discord.gateway"].add_cog(self.cog)
        ctx.scope.add(remove)
        return {"feature.umigame": self.cog}

    async def start(self):
        pass

    async def stop(self):
        await self.cog.cog_unload()
