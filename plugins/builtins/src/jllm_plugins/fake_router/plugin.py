"""Route requests to the first explicitly bound fake provider."""


class Plugin:
    async def setup(self, ctx):
        self.providers = ctx.dependencies["fake.provider"]
        return {"fake.routed": self}

    def echo(self, text: str) -> str:
        return self.providers[0].echo(text)

    async def start(self):
        pass

    async def stop(self):
        pass
