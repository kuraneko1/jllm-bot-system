from .sessions import SessionStore


class Plugin:
    async def setup(self, ctx):
        self.store = SessionStore(
            ctx.dependencies["storage.local"].legacy_path(ctx.config.get("directory", "sessions"))
        )
        return {"store.sessions": self.store}

    async def start(self):
        pass

    async def stop(self):
        pass
