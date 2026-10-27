from . import BookmarkStore


class Plugin:
    async def setup(self, ctx):
        storage = ctx.dependencies["storage.local"]
        self.store = BookmarkStore(
            storage.legacy_path(ctx.config.get("data_file", "bookmarks.json")),
            max_items=ctx.config.get("max_items", 500),
        )
        return {"feature.bookmarks": self.store}

    async def start(self):
        pass

    async def stop(self):
        pass
