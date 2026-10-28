from .conversation import ConversationService


class Plugin:
    async def setup(self, ctx):
        self.service = ConversationService(
            ctx.dependencies["llm.chat"],
            fetcher=ctx.dependencies.get("web.safe-fetch"),
        )
        return {"feature.conversation": self.service}

    async def start(self):
        pass

    async def stop(self):
        pass
