from .translation import build_translation_client


class Plugin:
    async def setup(self, ctx):
        config = dict(ctx.config)
        self.client = build_translation_client("sarashina", config)
        return {"llm.provider": self.client}

    async def start(self):
        pass

    async def stop(self):
        pass
