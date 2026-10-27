from . import build_filter, describe_effects


class AudioEffects:
    def __init__(self, ir_path: str):
        self.ir_path = ir_path

    def build_filter(self, effects):
        return build_filter(effects, ir_path=self.ir_path)

    def describe_effects(self, effects):
        return describe_effects(effects)


class Plugin:
    async def setup(self, ctx):
        return {"speech.effects": AudioEffects(ctx.config.get("ir_path", "data/ir/room.wav"))}

    async def start(self):
        pass

    async def stop(self):
        pass
