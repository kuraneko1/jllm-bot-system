from .kitasan import KitasanService


class Plugin:
    async def setup(self, ctx):
        self.service = KitasanService(
            ctx.dependencies["llm.chat"],
            restrictions=ctx.dependencies["policy.restrictions"],
            limit=ctx.config.get("messages", 100),
            enabled=ctx.config.get("enabled", True),
            words=ctx.config.get("words"),
            item_chars=ctx.config.get("item_chars", 300),
            max_chars=ctx.config.get("max_chars", 4000),
        )
        return {"feature.kitasan": self.service}

    async def start(self):
        pass

    async def stop(self):
        pass
