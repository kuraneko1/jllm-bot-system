"""Consume the routed fake capability without importing its provider."""


class Plugin:
    async def setup(self, ctx):
        self.echo = ctx.dependencies["fake.routed"]
        self.message = ctx.config.get("message", "hello")
        return {}

    async def start(self):
        self.result = self.echo.echo(self.message)

    async def stop(self):
        pass
