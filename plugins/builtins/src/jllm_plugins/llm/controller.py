from .translation_control import build_translation_controller


class Plugin:
    async def setup(self, ctx):
        self.controller = build_translation_controller()
        return {"llm.controller": self.controller}

    async def start(self):
        pass

    async def stop(self):
        pass
