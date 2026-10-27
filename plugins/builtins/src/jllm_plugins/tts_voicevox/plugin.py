from .backend import VoicevoxBackend


class Plugin:
    async def setup(self, ctx):
        self.backend = VoicevoxBackend(
            ctx.config.get("url", "http://127.0.0.1:50021"),
            timeout=ctx.config.get("timeout", 90.0),
            default_speaker=ctx.config.get("default_speaker"),
        )
        ctx.scope.add(self.backend.close)
        return {"tts.provider": self.backend}

    async def start(self):
        await self.backend.setup()

    async def stop(self):
        pass
