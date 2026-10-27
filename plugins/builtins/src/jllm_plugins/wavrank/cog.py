import asyncio
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from .service import WavRankConfig, compute, render

log = logging.getLogger("jllm-voice.wavrank")
COOLDOWN_SECONDS = 60
MAX_CONCURRENT = 1


class WavRankCog(commands.Cog):
    def __init__(self, bot: commands.Bot, config: WavRankConfig):
        self.bot = bot
        self.config = config
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        self.last_used: dict[int, float] = {}
        self._closing = False

    def cog_unload(self):
        self._closing = True

    @app_commands.command(
        name="wavrank",
        description="wav 呼び出し語句のランキング画像を作る（自分／指定した人）",
    )
    @app_commands.guild_only()
    @app_commands.describe(user="他の人のランキングを見たいときだけ指定（省略すると自分）")
    async def wavrank(self, interaction: discord.Interaction, user: discord.Member | None = None):
        target = user or interaction.user
        now = time.time()
        previous = self.last_used.get(interaction.user.id, 0.0)
        if now - previous < COOLDOWN_SECONDS:
            wait = int(COOLDOWN_SECONDS - (now - previous)) + 1
            await interaction.response.send_message(
                f"連続では作れません。{wait} 秒ほど待ってからどうぞ。",
                ephemeral=True,
            )
            return
        self.last_used[interaction.user.id] = now
        await interaction.response.defer(thinking=True)
        try:
            async with self.semaphore:
                stats = await asyncio.to_thread(compute, target.id, config=self.config)
                if self._closing:
                    return
                if not stats["rows"]:
                    await interaction.followup.send(
                        f"{target.display_name} さんの wav 呼び出しはまだ記録にありません。",
                        ephemeral=True,
                    )
                    return
                png = await asyncio.to_thread(render, stats, config=self.config)
                if self._closing:
                    return
        except FileNotFoundError:
            if not self._closing:
                await interaction.followup.send(
                    "書き出しデータが見つかりません（`data/exports/` を確認してください）。",
                    ephemeral=True,
                )
            return
        except Exception:
            log.exception("ランキング画像の作成に失敗")
            if not self._closing:
                await interaction.followup.send("画像の作成に失敗しました。", ephemeral=True)
            return

        top = "、".join(f"{key} {count:,}回" for key, count in stats["rows"][:3])
        embed = discord.Embed(
            title=f"{stats['name']} さんの wav 呼び出しランキング",
            description=(
                f"発火 {stats['fired']:,} メッセージ / 投稿 {stats['msgs']:,} 件 / "
                f"{stats['first']} 〜 {stats['last']}\n上位: {top}"
            ),
            colour=0x57B7FF,
        )
        embed.set_footer(text="その時点の最新データで集計（bot と同じマッチング仕様）")
        embed.set_image(url="attachment://wavrank.png")
        if not self._closing:
            await interaction.followup.send(
                embed=embed,
                file=discord.File(png, filename="wavrank.png"),
            )
