from .backend import OpenAITTSBackend


class Plugin:
    async def setup(self, ctx):
        self.backend = OpenAITTSBackend(
            ctx.config.get("url", "http://127.0.0.1:8090"),
            default_voice=ctx.config.get("voice", ""),
            voices=ctx.config.get("voices", ""),
            voices_path=ctx.config.get("voices_path", "/v1/audio/voices"),
            health_path=ctx.config.get("health_path", "/health"),
            model_name=ctx.config.get("model", ""),
            speed_mode=ctx.config.get("speed_mode", "atempo"),
            default_speed=ctx.config.get("default_speed", 1.0),
            ignore_speaker_speed=ctx.config.get("ignore_speaker_speed", False),
            seed=ctx.config.get("seed"),
            silence_retries=ctx.config.get("silence_retries", 1),
            silence_rms_dbfs=ctx.config.get("silence_rms_dbfs", -45.0),
            timeout=ctx.config.get("timeout", 120.0),
            streaming=ctx.config.get("streaming", True),
        )
        ctx.scope.add(self.backend.close)
        return {"tts.provider": self.backend}

    async def start(self):
        await self.backend.setup()

    async def stop(self):
        pass
