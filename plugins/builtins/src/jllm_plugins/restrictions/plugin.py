from .service import UserRestrictions


class Plugin:
    async def setup(self, ctx):
        storage = ctx.dependencies["storage.local"]
        path = storage.legacy_path(ctx.config.get("path", "user_restrictions.json"))
        self.restrictions = UserRestrictions(path)
        return {"policy.restrictions": self.restrictions}

    async def start(self):
        pass

    async def stop(self):
        pass
