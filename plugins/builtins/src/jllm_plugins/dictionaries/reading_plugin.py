from .readings import ReadingDictionary


class Plugin:
    async def setup(self, ctx):
        storage = ctx.dependencies["storage.local"]
        self.dictionary = ReadingDictionary(
            str(storage.legacy_path(ctx.config.get("data_file", "readings.json")))
        )
        return {"dictionary.reading": self.dictionary}

    async def start(self):
        pass

    async def stop(self):
        pass
