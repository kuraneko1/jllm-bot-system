"""``feature.haru`` プラグインエントリポイント。"""

from __future__ import annotations

from .client import HaruApiClient, HaruConfig
from .cog import HaruCog
from .personas import HaruPersonaStore


class Plugin:
    async def setup(self, ctx):
        storage = ctx.dependencies["storage.local"]
        config = HaruConfig(
            url=ctx.config.get("url", "http://127.0.0.1:8998"),
            personas_path=ctx.config.get("personas_path", "haru_personas.json"),
            voices_dir=ctx.config.get("voices_dir", "haru_voices"),
            focus_path=ctx.config.get("focus_path", "haru_focus.json"),
            guidance_scale=float(ctx.config.get("guidance_scale", 1.5)),
            output_volume=float(ctx.config.get("output_volume", 1.0)),
            connect_timeout_seconds=float(ctx.config.get("connect_timeout_seconds", 20.0)),
        )
        store = HaruPersonaStore(
            storage.legacy_path(config.personas_path),
            storage.legacy_path(config.voices_dir),
            storage.legacy_path(config.focus_path),
        )
        client = HaruApiClient(config.url, timeout=config.connect_timeout_seconds)
        self.cog = HaruCog(
            ctx.dependencies["discord.gateway"].bot,
            store=store,
            client=client,
            config=config,
            reader=ctx.dependencies.get("feature.reader"),
            restrictions=ctx.dependencies.get("policy.restrictions"),
        )
        remove = await ctx.dependencies["discord.gateway"].add_cog(self.cog)
        ctx.scope.add(self.cog.close_all)
        ctx.scope.add(remove)
        return {"feature.haru": self.cog}

    async def start(self):
        pass

    async def stop(self):
        await self.cog.close_all()
