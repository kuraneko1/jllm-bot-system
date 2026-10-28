from .service import build_vram_controller


class Plugin:
    async def setup(self, ctx):
        self.controller = build_vram_controller()
        return {"ops.vram": self.controller}

    async def start(self):
        pass

    async def stop(self):
        pass
