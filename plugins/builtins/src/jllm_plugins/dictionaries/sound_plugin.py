from .dictionary import AudioProcessingConfig, SoundDictionary


class Plugin:
    async def setup(self, ctx):
        storage = ctx.dependencies["storage.local"]
        self.dictionary = SoundDictionary(
            str(storage.legacy_path(ctx.config.get("data_file", "sounds.json"))),
            str(storage.legacy_path(ctx.config.get("sounds_dir", "sounds"))),
            audio_config=AudioProcessingConfig(**ctx.config.get("audio_processing", {})),
        )
        return {"dictionary.sound": self.dictionary}

    async def start(self):
        pass

    async def stop(self):
        pass
