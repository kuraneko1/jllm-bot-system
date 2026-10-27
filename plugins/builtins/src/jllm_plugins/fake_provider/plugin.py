"""Configurable fake provider used to exercise multiple instances."""


class Plugin:
    async def setup(self, ctx):
        self.prefix = ctx.config.get("prefix", "")
        return {"fake.provider": self}

    def echo(self, text: str) -> str:
        return self.prefix + text

    async def start(self):
        pass

    async def stop(self):
        pass
