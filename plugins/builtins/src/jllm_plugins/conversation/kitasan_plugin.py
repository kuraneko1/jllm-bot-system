from .kitasan import KitasanService


class Plugin:
    async def setup(self, ctx):
        self.service = KitasanService(
            ctx.dependencies["llm.chat"],
            restrictions=ctx.dependencies["policy.restrictions"],
            limit=ctx.config.get("messages", 100),
        )
        return {"feature.kitasan": self.service}

    async def start(self):
        pass

    async def stop(self):
        pass
