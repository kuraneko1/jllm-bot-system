import discord


class MediaApprovalView(discord.ui.View):
    approval_kind = "media"
    registration_label = "メディア"

    def __init__(self, cog, request_id: str):
        super().__init__(timeout=600)
        self.cog = cog
        self.request_id = request_id
        self.message = None
        self.finished = False

    async def _authorized(self, interaction: discord.Interaction) -> bool:
        if self.cog._is_approver(interaction.user.id):
            return True
        await interaction.response.send_message(
            "この申請を確認できるのは登録管理者だけです。",
            ephemeral=True,
        )
        return False

    def _disable(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(
        label="✅ 許可 / 上書き", style=discord.ButtonStyle.success, custom_id="media_approve"
    )
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._authorized(interaction):
            return
        if self.finished:
            await interaction.response.send_message("この申請は処理済みです。", ephemeral=True)
            return
        self.finished = True
        result = await getattr(self.cog, f"_approve_{self.approval_kind}")(self.request_id)
        self._disable()
        await interaction.response.edit_message(content=result, view=self)

    async def on_timeout(self):
        if self.finished:
            return
        self.finished = True
        await getattr(self.cog, f"_expire_{self.approval_kind}")(self.request_id)
        self._disable()
        if self.message is not None:
            await self.message.edit(
                content=f"{self.registration_label}登録申請は期限切れです。",
                view=self,
            )

    @discord.ui.button(label="❌ 拒否", style=discord.ButtonStyle.danger, custom_id="media_reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._authorized(interaction):
            return
        if self.finished:
            await interaction.response.send_message("この申請は処理済みです。", ephemeral=True)
            return
        self.finished = True
        result = await getattr(self.cog, f"_reject_{self.approval_kind}")(self.request_id)
        self._disable()
        await interaction.response.edit_message(content=result, view=self)


class SoundApprovalView(MediaApprovalView):
    approval_kind = "sound"
    registration_label = "音声"

    def __init__(self, cog, request_id: str):
        super().__init__(cog, request_id)
        self.approve.label = "✅ 許可 / 登録"

    async def _authorized(self, interaction: discord.Interaction) -> bool:
        if self.cog.config.owner_id and interaction.user.id == self.cog.config.owner_id:
            return True
        await interaction.response.send_message(
            "この申請を確認できるのはBotオーナーだけです。",
            ephemeral=True,
        )
        return False
