from .service import build_tts_lifecycle


class Plugin:
    async def setup(self, ctx):
        self.lifecycle = build_tts_lifecycle()
        return {"tts.lifecycle": self.lifecycle}

    async def start(self):
        pass

    async def stop(self):
        pass
