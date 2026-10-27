class Plugin:
    async def setup(self, ctx):
        return {"tts.synthesis": ctx.dependencies["tts.provider"]}

    async def start(self):
        pass

    async def stop(self):
        pass
