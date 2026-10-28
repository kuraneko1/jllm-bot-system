from .translation import build_translation_client


class Plugin:
    async def setup(self, ctx):
        self.client = build_translation_client(
            "local",
            {
                "api_url": ctx.config.get("api_url", "http://127.0.0.1:8081"),
                "key_file": ctx.config.get("key_file", ""),
            },
        )
        return {"llm.provider": self.client}

    async def start(self):
        pass

    async def stop(self):
        pass
