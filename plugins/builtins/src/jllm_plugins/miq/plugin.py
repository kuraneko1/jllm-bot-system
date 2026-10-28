from .cog import MiqCog, MiqConfig


class Plugin:
    async def setup(self, ctx):
        storage = ctx.dependencies["storage.local"]
        config = MiqConfig(
            emoji_cache_dir=str(storage.legacy_path("miq_emojis")),
            emoji_cache_limit=ctx.config.get("emoji_cache_limit", 500),
            layout=ctx.config.get("layout", "auto"),
            log_path=str(storage.legacy_path("miq.jsonl")),
            max_chars=ctx.config.get("max_chars", 2000),
            size=ctx.config.get("size", "640x360"),
            text_align=ctx.config.get("text_align", "right"),
            words=tuple(ctx.config.get("words", ["miq"])),
        )
        self.cog = MiqCog(
            ctx.dependencies["discord.gateway"].bot,
            config=config,
            restrictions=ctx.dependencies["policy.restrictions"],
        )
        remove = await ctx.dependencies["discord.gateway"].add_cog(self.cog)
        ctx.scope.add(remove)
        return {"feature.miq": self.cog}

    async def start(self):
        pass

    async def stop(self):
        await self.cog.cog_unload()
