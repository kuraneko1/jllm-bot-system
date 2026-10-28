from .translation import build_translation_client


class Plugin:
    async def setup(self, ctx):
        self.client = build_translation_client("atmos", dict(ctx.config))
        return {"llm.provider": self.client}

    async def start(self):
        pass

    async def stop(self):
        pass
