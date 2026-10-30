from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

import discord

from jllm_plugins.bookmarks import LIST_EXCERPT_LENGTH, excerpt, format_added_at, jump_url
from jllm_plugins.restrictions.service import DESCRIPTIONS, FEATURES, LABELS

if TYPE_CHECKING:
    from .core import ReaderCog

logger = logging.getLogger(__name__)
MYTALK_MAX_SPEAKER_OPTIONS = 22
MYTALK_PANEL_TIMEOUT = 300
MYTALK_MUTE_VALUE = "mute"
#: /mytalk の一覧から隠す話者。既定は空で、profile の mytalk_hidden_voices で指定する。
MYTALK_HIDDEN_VOICES: frozenset[str] = frozenset()


class MyTalkView(discord.ui.View):
    """/mytalk のパネル。声（話者）と「読み上げない」を選んで ✅設定する で確定すると、
    その場でコンポーネントが消える使い捨て UI（ephemeral）。変更は /mytalk 開き直し。"""

    def __init__(
        self,
        cog: ReaderCog,
        *,
        guild_id: int,
        user_id: int,
        speakers: list[tuple[int, str]],
    ):
        super().__init__(timeout=MYTALK_PANEL_TIMEOUT)
        self.cog = cog
        self.guild_id = guild_id
        self.user_id = user_id
        self.speakers = speakers

        key = cog._user_key(guild_id, user_id)
        current_voice = cog.user_voices.get(key)

        player = cog.players.get(guild_id)
        muted = player is not None and user_id in player.skip_users

        visible = [
            (speaker_id, name)
            for speaker_id, name in speakers
            if name not in cog.config.mytalk_hidden_voices
        ]

        options = [
            discord.SelectOption(
                label="🔇 読み上げない",
                value=MYTALK_MUTE_VALUE,
                description="あなたの投稿だけ読み上げを止める（VC退出で解除）",
            ),
            discord.SelectOption(
                label="サーバー既定",
                value="default",
                description="いまサーバーで使われている既定の声に戻す",
            ),
        ]
        marked = muted
        options[0].default = muted
        options[1].default = not muted and current_voice is None

        has_overflow = len(visible) > MYTALK_MAX_SPEAKER_OPTIONS
        limit = MYTALK_MAX_SPEAKER_OPTIONS - (1 if has_overflow else 0)

        displayed = visible[:limit]
        # 現在の話者が一覧の後半にあっても、選択状態を表示できるようにする。
        current_option = next((speaker for speaker in visible if speaker[1] == current_voice), None)
        if current_option is not None and current_option not in displayed:
            displayed[-1] = current_option

        for speaker_id, name in displayed:
            option = discord.SelectOption(
                label=f"{speaker_id}: {name}"[:100],
                value=str(speaker_id),
            )
            if not marked and name == current_voice:
                option.default = True
                marked = True
            options.append(option)

        if has_overflow:
            options.append(
                discord.SelectOption(
                    label="…ほか（一覧の表示上限）",
                    value="overflow",
                )
            )

        self.speaker_select = discord.ui.Select(
            placeholder="声（話者）を選ぶ",
            options=options,
            custom_id="mytalk:speaker",
        )
        self.speaker_select.callback = self.on_select
        self.add_item(self.speaker_select)

        self.apply_button = discord.ui.Button(
            label="✅ 設定する",
            style=discord.ButtonStyle.primary,
            custom_id="mytalk:apply",
        )
        self.apply_button.callback = self.on_apply
        self.add_item(self.apply_button)

        # 選択中の未確定値。None のあいだは「設定する」を押せない
        self.pending: str | None = None
        self.finished = False
        self.message: discord.Message | None = None

    async def _guard(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id == self.user_id:
            return True

        await interaction.response.send_message(
            "このパネルは呼び出した本人だけ操作できます。",
            ephemeral=True,
        )
        return False

    def _option_label(self, value: str) -> str:
        for option in self.speaker_select.options:
            if option.value == value:
                return option.label

        return value

    async def on_select(
        self,
        interaction: discord.Interaction,
    ):
        if not await self._guard(interaction):
            return

        self.pending = self.speaker_select.values[0]
        for option in self.speaker_select.options:
            option.default = option.value == self.pending
        await interaction.response.edit_message(
            embed=self._embed(pending=self.pending),
            view=self,
        )

    async def on_apply(
        self,
        interaction: discord.Interaction,
    ):
        if not await self._guard(interaction):
            return

        if self.finished:
            await interaction.response.send_message(
                "このパネルは確定済みです。/mytalk で開き直してください。",
                ephemeral=True,
            )
            return

        if self.pending is None:
            await interaction.response.send_message(
                "ドロップダウンで声を選ぶと「設定する」が効きます。",
                ephemeral=True,
            )
            return

        value = self.pending
        key = self.cog._user_key(self.guild_id, self.user_id)
        player = self.cog.players.get(self.guild_id)

        if value == MYTALK_MUTE_VALUE:
            if player is None:
                await interaction.response.send_message(
                    "先に /join で読み上げを開始してください。",
                    ephemeral=True,
                )
                return

            player.skip_users.add(self.user_id)
            result = "🔇 あなたの投稿は読み上げません（ボイスチャンネルを出ると自動解除）"

        elif value == "overflow":
            await interaction.response.send_message(
                "このパネルの一覧には表示上限があります。表示されている話者から選んでください。",
                ephemeral=True,
            )
            return

        else:
            if value == "default":
                if self.cog.user_voices.pop(key, None) is not None:
                    self.cog._save_user_voices()
                result = "声はサーバー既定に戻しました"
            else:
                selected = next(
                    (name for speaker_id, name in self.speakers if str(speaker_id) == value),
                    None,
                )

                if selected is None:
                    await interaction.response.send_message(
                        "その話者は存在しません。/mytalk を開き直してください。",
                        ephemeral=True,
                    )
                    return

                self.cog.user_voices[key] = selected
                self.cog._save_user_voices()
                result = f"声は {self._option_label(value)} に設定しました"

            # 声を選ぶ＝読み上げてもらう意思表示なのでミュートは解除する
            if player is not None:
                player.skip_users.discard(self.user_id)

        self.finished = True

        # 使い捨て: 確定したらコンポーネントを消す。変更は /mytalk 開き直し
        await interaction.response.edit_message(
            content=result,
            embed=None,
            view=None,
        )

    async def on_timeout(self):
        if self.finished:
            return

        self.finished = True

        for child in self.children:
            child.disabled = True

        if self.message is not None:
            try:
                await self.message.edit(
                    content="設定しないままパネルが期限切れになりました。"
                    "必要なら /mytalk で開き直してください。",
                    embed=None,
                    view=self,
                )

            except discord.HTTPException:
                logger.exception("mytalkパネルの期限切れ表示に失敗")

    def _embed(
        self,
        pending: str | None = None,
    ) -> discord.Embed:
        embed = self.cog._mytalk_embed(
            self.guild_id,
            self.user_id,
            self.speakers,
        )

        if pending is not None:
            embed.add_field(
                name="選択中（未確定）",
                value=(
                    f"{self._option_label(pending)}\n"
                    "下の **✅ 設定する** で確定（確定後このパネルは閉じます）"
                ),
                inline=False,
            )

        return embed


class ForceVoiceView(discord.ui.View):
    """/forcevoice のパネル。オーナーだけが操作でき、メンバーの声を
    1人ずつ強制変更・解除する使い捨て UI（ephemeral）。確定で閉じる。"""

    def __init__(
        self,
        cog: ReaderCog,
        *,
        guild_id: int,
        owner_id: int,
        speakers: list[tuple[int, str]],
    ):
        super().__init__(timeout=MYTALK_PANEL_TIMEOUT)
        self.cog = cog
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.speakers = speakers
        self.pending_user: int | None = None
        self.pending_voice: str | None = None
        self.finished = False
        self.message: discord.Message | None = None

        self.user_select = discord.ui.UserSelect(
            placeholder="強制するメンバーを選ぶ",
            min_values=1,
            max_values=1,
            custom_id="forcevoice:user",
        )
        self.user_select.callback = self.on_select_user
        self.add_item(self.user_select)

        options = [
            discord.SelectOption(
                label="🔓 強制を解除",
                value="release",
                description="個人設定・サーバー既定に戻す",
            ),
        ]
        for speaker_id, name in speakers:
            options.append(
                discord.SelectOption(
                    label=f"{speaker_id}: {name}"[:100],
                    value=str(speaker_id),
                )
            )
        self.speaker_select = discord.ui.Select(
            placeholder="強制する声を選ぶ",
            options=options,
            custom_id="forcevoice:speaker",
        )
        self.speaker_select.callback = self.on_select_voice
        self.add_item(self.speaker_select)

        self.apply_button = discord.ui.Button(
            label="✅ 強制設定",
            style=discord.ButtonStyle.primary,
            custom_id="forcevoice:apply",
        )
        self.apply_button.callback = self.on_apply
        self.add_item(self.apply_button)

    async def _guard(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id == self.owner_id:
            return True

        await interaction.response.send_message(
            "このパネルはオーナーだけ操作できます。",
            ephemeral=True,
        )
        return False

    def _option_label(self, value: str) -> str:
        if value == "release":
            return "🔓 強制を解除"
        for option in self.speaker_select.options:
            if option.value == value:
                return option.label
        return value

    def _member_label(self, user_id: int) -> str:
        return f"<@{user_id}>"

    def embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🔒 強制ボイス設定（オーナーのみ）",
            description=(
                "メンバーの声を1人ずつ強制変更します（VC外も可）。"
                "強制は本人の `/mytalk` 設定より優先されます。"
            ),
            colour=discord.Colour.red(),
        )
        prefix = f"{self.guild_id}:"
        lines = [
            f"{self._member_label(int(key.split(':', 1)[1]))}: {name}"
            for key, name in sorted(self.cog.forced_voices.items())
            if key.startswith(prefix)
        ]
        embed.add_field(
            name="現在の強制設定",
            value="\n".join(lines) if lines else "なし",
            inline=False,
        )
        if self.pending_user is not None:
            voice = (
                self._option_label(self.pending_voice)
                if self.pending_voice is not None
                else "（声を未選択）"
            )
            embed.add_field(
                name="選択中（未確定）",
                value=(
                    f"{self._member_label(self.pending_user)} → {voice}\n"
                    "下の **✅ 強制設定** で確定（確定後このパネルは閉じます）"
                ),
                inline=False,
            )
        return embed

    async def on_select_user(
        self,
        interaction: discord.Interaction,
    ):
        if not await self._guard(interaction):
            return

        values = self.user_select.values
        self.pending_user = values[0].id if values else None
        await interaction.response.edit_message(
            embed=self.embed(),
            view=self,
        )

    async def on_select_voice(
        self,
        interaction: discord.Interaction,
    ):
        if not await self._guard(interaction):
            return

        values = self.speaker_select.values
        self.pending_voice = values[0] if values else None
        await interaction.response.edit_message(
            embed=self.embed(),
            view=self,
        )

    async def on_apply(
        self,
        interaction: discord.Interaction,
    ):
        if not await self._guard(interaction):
            return

        if self.finished:
            await interaction.response.send_message(
                "このパネルは確定済みです。/forcevoice で開き直してください。",
                ephemeral=True,
            )
            return

        if self.pending_user is None or self.pending_voice is None:
            await interaction.response.send_message(
                "メンバーと声の両方を選ぶと「強制設定」が効きます。",
                ephemeral=True,
            )
            return

        key = self.cog._user_key(self.guild_id, self.pending_user)
        if self.pending_voice == "release":
            if self.cog.forced_voices.pop(key, None) is not None:
                self.cog._save_forced_voices()
            result = (
                f"{self._member_label(self.pending_user)} の強制を解除しました"
                "（個人設定に戻ります）"
            )
        else:
            selected = next(
                (
                    name
                    for speaker_id, name in self.speakers
                    if str(speaker_id) == self.pending_voice
                ),
                None,
            )
            if selected is None:
                await interaction.response.send_message(
                    "その話者は存在しません。/forcevoice を開き直してください。",
                    ephemeral=True,
                )
                return
            self.cog.forced_voices[key] = selected
            self.cog._save_forced_voices()
            result = (
                f"{self._member_label(self.pending_user)} の声を "
                f"{self._option_label(self.pending_voice)} に強制しました"
            )

        self.finished = True
        await interaction.response.edit_message(
            content=result,
            embed=None,
            view=None,
        )

    async def on_timeout(self):
        if self.finished:
            return

        self.finished = True

        for child in self.children:
            child.disabled = True

        if self.message is not None:
            try:
                await self.message.edit(
                    content="設定しないままパネルが期限切れになりました。"
                    "必要なら /forcevoice で開き直してください。",
                    embed=None,
                    view=self,
                )

            except discord.HTTPException:
                logger.exception("forcevoiceパネルの期限切れ表示に失敗")


class BookmarkView(discord.ui.View):
    """ブックマーク一覧のページ送り。"""

    PAGE_SIZE = 8

    def __init__(self, cog: ReaderCog, user_id: int, items: list[dict]):
        super().__init__(timeout=180)
        self.cog = cog
        self.user_id = user_id
        self.items = items
        self.page = 0
        self._sync()

    @property
    def pages(self) -> int:
        return max(1, math.ceil(len(self.items) / self.PAGE_SIZE))

    def _sync(self):
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= self.pages - 1
        self.counter.label = f"{self.page + 1}/{self.pages}"

    def embed(self) -> discord.Embed:
        start = self.page * self.PAGE_SIZE
        page_items = self.items[start : start + self.PAGE_SIZE]

        lines = []

        for index, item in enumerate(page_items, start=start + 1):
            label = item.get("excerpt") or item.get("author") or "（本文なし）"
            title = excerpt(label, LIST_EXCERPT_LENGTH) or "（本文なし）"
            lines.append(
                "**{}. [{}]({})**\n　{} ・ {}".format(
                    index,
                    title,
                    jump_url(item),
                    "#" + str(item.get("channel") or "不明"),
                    format_added_at(item.get("added_at")) or "",
                )
            )

        return discord.Embed(
            title=f"ブックマーク（{len(self.items)}件）",
            description="\n".join(lines),
            colour=discord.Colour.blurple(),
        )

    async def _show(self, interaction: discord.Interaction):
        self._sync()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="◀ 前へ", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "自分の一覧だけを操作できます。",
                ephemeral=True,
            )
            return

        self.page = max(0, self.page - 1)
        await self._show(interaction)

    @discord.ui.button(label="次へ ▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "自分の一覧だけを操作できます。",
                ephemeral=True,
            )
            return

        self.page = min(self.pages - 1, self.page + 1)
        await self._show(interaction)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.primary, disabled=True)
    async def counter(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()


class RestrictView(discord.ui.View):
    """/restrict のパネル。オーナーだけが操作でき、選んだメンバーの
    wav再生・GIF再生・まるごと無視を切り替える（ephemeral、その場で更新）。"""

    def __init__(
        self,
        cog: ReaderCog,
        *,
        guild_id: int,
        owner_id: int,
    ):
        super().__init__(timeout=MYTALK_PANEL_TIMEOUT)
        self.cog = cog
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.selected_user: int | None = None
        self.finished = False
        self.message: discord.Message | None = None

        self.user_select = discord.ui.UserSelect(
            placeholder="制限するメンバーを選ぶ",
            min_values=1,
            max_values=1,
            custom_id="restrict:user",
        )
        self.user_select.callback = self.on_select_user
        self.add_item(self.user_select)

        self.buttons: dict[str, discord.ui.Button] = {}

        for feature in FEATURES:
            button = discord.ui.Button(
                label=LABELS[feature],
                style=discord.ButtonStyle.secondary,
                custom_id=f"restrict:{feature}",
            )
            button.callback = self._make_toggle(feature)
            self.buttons[feature] = button
            self.add_item(button)

        self.clear_button = discord.ui.Button(
            label="すべて解除",
            style=discord.ButtonStyle.success,
            custom_id="restrict:clear",
        )
        self.clear_button.callback = self.on_clear
        self.add_item(self.clear_button)

        self._refresh()

    # ------------------------------------------------------------------ 表示

    def _flags(self) -> frozenset:
        if self.selected_user is None:
            return frozenset()

        return self.cog._restriction_store().flags(self.guild_id, self.selected_user)

    def _refresh(self):
        """選んでいる人の状態をボタンに反映する。"""

        flags = self._flags()

        for feature, button in self.buttons.items():
            blocked = feature in flags
            button.label = f"{LABELS[feature]}: {'剥奪中' if blocked else '許可'}"
            button.style = discord.ButtonStyle.danger if blocked else discord.ButtonStyle.secondary
            button.disabled = self.selected_user is None

        self.clear_button.disabled = self.selected_user is None or not flags

    def embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🚫 ユーザー制限（オーナーのみ）",
            description=(
                "メンバーを選んで、その人だけ wav再生・GIF再生を止められます。"
                "**まるごと無視**にすると、その人の投稿は読み上げも会話もメディアも"
                "引用画像もすべて無視されます。"
            ),
            colour=discord.Colour.dark_red(),
        )
        embed.add_field(
            name="制限の内容",
            value="\n".join(
                f"・**{LABELS[feature]}**: {DESCRIPTIONS[feature]}" for feature in FEATURES
            ),
            inline=False,
        )

        if self.selected_user is None:
            embed.add_field(name="選択中", value="（メンバーを選んでください）", inline=False)
        else:
            flags = self._flags()
            state = "、".join(LABELS[feature] for feature in FEATURES if feature in flags)
            embed.add_field(
                name="選択中",
                value=(
                    f"{self._member_label(self.selected_user)}: "
                    f"{state or '制限なし'}\n"
                    "下のボタンを押すと、その場で切り替わります"
                ),
                inline=False,
            )

        current = self.cog._restriction_store().guild_entries(self.guild_id)
        lines = [
            f"{self._member_label(user_id)}: " + "、".join(LABELS[feature] for feature in flags)
            for user_id, flags in current.items()
        ]
        embed.add_field(
            name="いま制限中のユーザー",
            value="\n".join(lines) if lines else "なし",
            inline=False,
        )

        return embed

    def _member_label(self, user_id: int) -> str:
        return f"<@{user_id}>"

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True

        await interaction.response.send_message(
            "このパネルはオーナーだけ操作できます。",
            ephemeral=True,
        )
        return False

    # ------------------------------------------------------------------ 操作

    async def on_select_user(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return

        values = self.user_select.values
        self.selected_user = values[0].id if values else None
        self._refresh()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    def _make_toggle(self, feature: str):
        async def toggle(interaction: discord.Interaction):
            if not await self._guard(interaction):
                return

            if self.selected_user is None:
                await interaction.response.send_message(
                    "先にメンバーを選んでください。",
                    ephemeral=True,
                )
                return

            blocked = feature in self._flags()
            self.cog._restriction_store().set_flag(
                self.guild_id,
                self.selected_user,
                feature,
                not blocked,
            )
            logger.info(
                "ユーザー制限 %s: %s の %s を %s",
                self.guild_id,
                self.selected_user,
                feature,
                "解除" if blocked else "設定",
            )
            self._refresh()
            await interaction.response.edit_message(embed=self.embed(), view=self)

        return toggle

    async def on_clear(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return

        if self.selected_user is None:
            await interaction.response.send_message(
                "先にメンバーを選んでください。",
                ephemeral=True,
            )
            return

        self.cog._restriction_store().clear(self.guild_id, self.selected_user)
        logger.info("ユーザー制限 %s: %s をすべて解除", self.guild_id, self.selected_user)
        self._refresh()
        await interaction.response.edit_message(embed=self.embed(), view=self)
