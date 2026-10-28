from .web_content import WebContentFetcher


class Plugin:
    async def setup(self, ctx):
        self.fetcher = WebContentFetcher()
        return {"web.safe-fetch": self.fetcher}

    async def start(self):
        pass

    async def stop(self):
        pass
