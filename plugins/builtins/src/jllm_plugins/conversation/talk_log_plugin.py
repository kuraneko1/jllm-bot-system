from .talk_log import TalkLog


class Plugin:
    async def setup(self, ctx):
        storage = ctx.dependencies["storage.local"]
        self.log = TalkLog(
            storage.legacy_path(ctx.config.get("log_path", "talk_log.jsonl")),
            storage.legacy_path(ctx.config.get("stats_path", "talk_stats.json")),
        )
        return {"feature.talk-log": self.log}

    async def start(self):
        pass

    async def stop(self):
        pass
