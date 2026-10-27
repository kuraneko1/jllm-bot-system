from .backend import SpeechCppBackend


class Plugin:
    async def setup(self, ctx):
        self.backend = SpeechCppBackend(
            ctx.config.get("url", "http://127.0.0.1:8089"),
            voices_dir=ctx.config.get("voices_dir", ""),
            default_voice=ctx.config.get("voice", "none"),
            model_name=ctx.config.get("model", ""),
            timeout=ctx.config.get("timeout", 120.0),
        )
        ctx.scope.add(self.backend.close)
        return {"tts.provider": self.backend}

    async def start(self):
        await self.backend.setup()

    async def stop(self):
        pass
