import os
from pathlib import Path

from .backend import IrodoriBackend, _load_map


class Plugin:
    async def setup(self, ctx):
        data_dir = Path(ctx.config.get("data_dir", "data"))

        def data_path(key, default):
            value = ctx.config.get(key, "")
            return value or str(data_dir / default)

        self.backend = IrodoriBackend(
            ctx.config.get("url", "http://127.0.0.1:8088"),
            default_voice=ctx.config.get("voice", "none"),
            caption=ctx.config.get("caption", ""),
            api_key=os.environ.get(ctx.config.get("api_key_env", "IRODORI_API_KEY"), ""),
            model_name=ctx.config.get("model", "irodori-tts"),
            voice_order=ctx.config.get("voice_order", []),
            lora_map=_load_map(data_path("lora_map_file", "lora_map.json")),
            voices_cache=data_path("voices_cache", "voices_cache.json"),
            disabled_voices_path=data_path("disabled_voices_path", "disabled_voices.json"),
            streaming=ctx.config.get("streaming", True),
            chunk_min_chars=ctx.config.get("chunk_min_chars", 80),
            first_chunk_min_chars=ctx.config.get("first_chunk_min_chars", 12),
            timeout=ctx.config.get("timeout", 120.0),
        )
        ctx.scope.add(self.backend.close)
        return {"tts.provider": self.backend}

    async def start(self):
        await self.backend.setup()

    async def stop(self):
        pass
