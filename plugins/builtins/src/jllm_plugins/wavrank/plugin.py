from pathlib import Path

from .cog import WavRankCog
from .service import WavRankConfig


class Plugin:
    async def setup(self, ctx):
        config = WavRankConfig(
            data_dir=Path(ctx.config.get("data_dir", "data")),
            channel_id=str(ctx.config.get("channel_id", "")),
            archive=Path(value) if (value := ctx.config.get("archive")) else None,
            sounds=Path(value) if (value := ctx.config.get("sounds")) else None,
            out_dir=Path(value) if (value := ctx.config.get("out_dir")) else None,
            font=ctx.config.get("font", ""),
        )
        self.cog = WavRankCog(ctx.dependencies["discord.gateway"].bot, config)
        remove = await ctx.dependencies["discord.gateway"].add_cog(self.cog)
        ctx.scope.add(remove)
        return {}

    async def start(self):
        pass

    async def stop(self):
        self.cog.cog_unload()
