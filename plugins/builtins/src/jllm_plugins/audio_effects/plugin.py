import logging
from pathlib import Path

from . import build_filter, describe_effects

logger = logging.getLogger(__name__)


class AudioEffects:
    def __init__(self, ir_path: str):
        self.ir_path = ir_path
        if not Path(ir_path).is_file():
            # 残響（ec）は IR が無いと掛からない。起動時に気づけるようにする。
            logger.warning("音声効果のIRが見つかりません（エコーは無効）: %s", ir_path)

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
