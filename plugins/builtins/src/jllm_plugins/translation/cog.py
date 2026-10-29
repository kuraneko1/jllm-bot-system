import asyncio
import logging
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import discord
from discord import app_commands
from discord.ext import commands

from jllm_plugins.conversation.conversation import (
    SYSTEM,
    ConversationCancelled,
    ConversationService,
    conversation_pages,
    referenced_message,
)
from jllm_plugins.conversation.kitasan import KitasanService
from jllm_plugins.conversation.sessions import SessionStore
from jllm_plugins.conversation.talk_log import TalkLog
from jllm_plugins.llm import llm_backend
from jllm_plugins.llm.translation import (
    SwitchableClient,
    TranslationSettings,
    translation_pages,
    translation_text,
)
from jllm_plugins.llm.translation_control import (
    TranslationControlError,
    build_translation_controller,
)
from jllm_plugins.miq.trigger import is_miq_image, is_miq_trigger
from jllm_plugins.restrictions.service import UserRestrictions


@dataclass
class TranslationCogConfig:
    allowed_channel_ids: frozenset[int] = frozenset()
    owner_id: int = 0
    session_dir: str = "data/sessions"
    talk_call_suffix: str = ""
    talk_log_path: str = "data/talk_log.jsonl"
    talk_mention_gate: str = "open"
    talk_model: str = ""
    talk_open_channels: frozenset[int] = frozenset()
    talk_session_seconds: float = 1800.0
    talk_stats_path: str = "data/talk_stats.json"
    talk_system_prompt_file: str = ""
    translation_settings_path: str = "data/translation.json"
    tts_switch_user_ids: frozenset[int] = frozenset()

logger = logging.getLogger(__name__)
_DEFAULT = object()


def can_manage(user, owner_id=0):
    return user.id == owner_id or getattr(
        getattr(user, "guild_permissions", None), "manage_guild", False
    )


def user_labels(users):
    ids = sorted(users)
    return ", ".join(f"<@{uid}>" for uid in ids[:20]) + (
        f" ほか{len(ids) - 20}人" if len(ids) > 20 else ""
    )


class ParticipantSelect(discord.ui.Select):
    def __init__(self, panel):
        self.panel = panel
        super().__init__(placeholder="① VC参加者を名前で選択（複数可）", min_values=0, row=0)
        self.refresh()

    def refresh(self):
        page = self.panel.participants[self.panel.page * 25 : (self.panel.page + 1) * 25]
        active = self.panel.cog.settings.users(self.panel.guild.id, self.panel.channel_id)
        options = []
        for member in page:
            voice = getattr(member, "voice", None)
            in_vc = bool(voice and voice.channel)
            if in_vc:
                state = "VC参加中"
            elif member.id in self.panel.text_ids:
                state = "テキスト投稿"
            else:
                state = "VC未参加"
            options.append(
                discord.SelectOption(
                    label=member.display_name[:100],
                    value=str(member.id),
                    description=(
                        ("翻訳オン · " if member.id in active else "翻訳オフ · ")
                        + (member.voice.channel.name if in_vc else state)
                    )[:100],
                    emoji="🟢" if member.id in active else "⚪",
                    default=member.id in self.panel.users,
                )
            )
        self.options = options
        self.disabled = not page
        if not self.options:
            self.options = [discord.SelectOption(label="選択できる参加者がいません", value="none")]
        self.max_values = len(self.options)

    async def callback(self, interaction):
        page_ids = {
            m.id for m in self.panel.participants[self.panel.page * 25 : (self.panel.page + 1) * 25]
        }
        self.panel.users.difference_update(page_ids)
        self.panel.users.update(int(value) for value in self.values if value != "none")
        self.panel.refresh()
        await interaction.response.edit_message(
            content=None, embed=self.panel.embed(), view=self.panel
        )


class TranslationView(discord.ui.View):
    def __init__(self, cog, interaction, recent=None):
        super().__init__(timeout=300)
        self.cog = cog
        self.owner_id = interaction.user.id
        self.guild, self.channel_id = interaction.guild, interaction.channel_id
        self.users = cog.settings.users(self.guild.id, self.channel_id)
        self.pending_enabled = bool(self.users)
        self.pending_talk = cog.settings.mode(self.guild.id, self.channel_id) == "talk"
        self.pending_original = cog.settings.show_original(self.guild.id, self.channel_id)
        self.finished = False
        self.applying = False
        self.message = None
        self.page = 0
        active = cog.settings.users(self.guild.id, self.channel_id)
        members = {}
        vc_ids = set()
        for channel in [*self.guild.voice_channels, *self.guild.stage_channels]:
            for m in channel.members:
                if not m.bot:
                    members[m.id] = m
                    vc_ids.add(m.id)
        for uid in active:
            member = self.guild.get_member(uid)
            if member and not member.bot:
                members[uid] = member
        self.text_ids = set()
        for m in recent or []:
            if m.bot:
                continue
            members.setdefault(m.id, m)
            if m.id not in vc_ids:
                self.text_ids.add(m.id)
        self.participants = sorted(
            members.values(), key=lambda m: (m.display_name.casefold(), m.id)
        )
        self.selector = ParticipantSelect(self)
        self.add_item(self.selector)
        self.refresh()

    def refresh(self):
        self.selector.refresh()
        if self.pending_talk:
            self.selector.disabled = True
        self.previous.disabled = self.page == 0
        self.next_page.disabled = (self.page + 1) * 25 >= len(self.participants)
        self.apply_button.disabled = self.pending_enabled and not self.users
        for option in self.enabled_select.options:
            option.default = option.value == (
                "talk" if self.pending_talk else "on" if self.pending_enabled else "off"
            )
        for option in self.original_select.options:
            option.default = (option.value == "show") == self.pending_original
        if self.applying:
            for child in self.children:
                child.disabled = True
        else:
            self.enabled_select.disabled = False
            self.original_select.disabled = False
            self.cancel.disabled = False

    def embed(self, error=None):
        embed = discord.Embed(
            title="🌐 このチャンネルの翻訳・会話設定",
            description=(
                "設定を保存しました。変更するときは /translate を開き直してください。"
                if self.finished
                else (
                "モード・翻訳対象・原文表示を選んで **✅ 設定する** で確定します。"
                "対象はVC参加者と直近の投稿者から選べます。"
            )
            ),
            colour=discord.Colour.blurple(),
        )
        embed.add_field(name="翻訳対象", value=user_labels(self.users) or "未選択", inline=False)
        text_only = sorted(uid for uid in self.users if uid in self.text_ids)
        if text_only and (self.pending_enabled or self.pending_talk):
            embed.add_field(
                name="テキスト対象（VC外でも翻訳）", value=user_labels(text_only), inline=False
            )
        embed.add_field(
            name="モード",
            value="💬 会話・要約（メンション／Botへのリプ）"
            if self.pending_talk
            else "✅ 翻訳（日本語へ翻訳）"
            if self.pending_enabled
            else "🔇 停止",
            inline=True,
        )
        embed.add_field(
            name="原文表示",
            value="表示する（コピー用コードブロック）"
            if self.pending_original
            else "表示しない（日本語訳のみ）",
            inline=True,
        )
        embed.add_field(
            name="適用済みの設定",
            value=self.cog.status(
                SimpleNamespace(guild_id=self.guild.id, channel_id=self.channel_id)
            ),
            inline=False,
        )
        if self.applying:
            embed.add_field(name="設定中", value=self.cog.applying_note, inline=False)
        if error:
            embed.add_field(name="設定できませんでした", value=error[:1024], inline=False)
        pages = max(1, (len(self.participants) + 24) // 25)
        embed.set_footer(
            text=(
                f"参加者一覧 {self.page + 1}/{pages}ページ · "
                "選択はまだ未確定です。✅ 設定する で保存。"
            )
            if not self.finished
            else "設定完了 · 次の変更は /translate"
        )
        return embed

    async def interaction_check(self, interaction):
        if (
            interaction.user.id != self.owner_id
            or not can_manage(interaction.user, self.cog.config.owner_id)
        ):
            await interaction.response.send_message(
                "この設定パネルは開いた管理者だけが操作できます。", ephemeral=True
            )
            return False
        if self.finished or self.applying:
            await interaction.response.send_message(
                "このパネルは処理中または確定済みです。変更は /translate で開き直してください。",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.select(
        placeholder="モード：停止／翻訳／会話・要約",
        row=1,
        options=[
            discord.SelectOption(
                label="✅ 翻訳オン", value="on", description="選択した人の投稿を日本語へ翻訳する"
            ),
            discord.SelectOption(
                label="🔇 停止",
                value="off",
                description="翻訳・会話を停止。対象が全部なくなればバックエンドも停止",
            ),
            discord.SelectOption(
                label="💬 会話・要約",
                value="talk",
                description="メンション／Botへのリプで会話・長文要約",
            ),
        ],
    )
    async def enabled_select(self, interaction, select):
        self.pending_enabled = select.values[0] == "on"
        self.pending_talk = select.values[0] == "talk"
        self.refresh()
        await interaction.response.edit_message(content=None, embed=self.embed(), view=self)

    @discord.ui.select(
        placeholder="原文を表示する／表示しない",
        row=2,
        options=[
            discord.SelectOption(
                label="原文を表示する",
                value="show",
                description="日本語訳とコピー用の原文を公開する",
            ),
            discord.SelectOption(
                label="原文を表示しない", value="hide", description="日本語訳だけを公開する"
            ),
        ],
    )
    async def original_select(self, interaction, select):
        self.pending_original = select.values[0] == "show"
        self.refresh()
        await interaction.response.edit_message(content=None, embed=self.embed(), view=self)

    @discord.ui.button(label="前のページ", row=3)
    async def previous(self, interaction, button):
        self.page = max(0, self.page - 1)
        self.refresh()
        await interaction.response.edit_message(content=None, embed=self.embed(), view=self)

    @discord.ui.button(label="次のページ", row=3)
    async def next_page(self, interaction, button):
        self.page = min(max(0, (len(self.participants) - 1) // 25), self.page + 1)
        self.refresh()
        await interaction.response.edit_message(content=None, embed=self.embed(), view=self)

    @discord.ui.button(label="✅ 設定する", style=discord.ButtonStyle.primary, row=4)
    async def apply_button(self, interaction, button):
        members = []
        unresolved = []
        known = {m.id: m for m in self.participants}
        for uid in self.users:
            member = self.guild.get_member(uid) or known.get(uid)
            if member is None and hasattr(self.guild, "fetch_member"):
                try:
                    member = await self.guild.fetch_member(uid)
                except discord.HTTPException, discord.NotFound, discord.Forbidden:
                    member = None
            if member is None:
                unresolved.append(uid)
            members.append(member)
        if unresolved:
            await interaction.response.send_message(
                (
                    "サーバーに見つからないユーザーが含まれています。"
                    "/translate を開き直して選び直してください。"
                ),
                ephemeral=True,
            )
            return
        error = self.cog.validate(interaction, self.pending_enabled, members, self.text_ids)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        self.applying = True
        self.refresh()
        await interaction.response.defer()
        await interaction.edit_original_response(content=None, embed=self.embed(), view=self)
        by_id = {m.id: m for m in members if m is not None}
        text_picked = set()
        if self.pending_enabled and not self.pending_talk:
            for uid in self.users:
                member = by_id.get(uid)
                voice = getattr(member, "voice", None) if member else None
                if not (voice and voice.channel) and uid in self.text_ids:
                    text_picked.add(uid)
        try:
            await self.cog.configure(
                self.guild.id,
                self.channel_id,
                self.users if self.pending_enabled else set(),
                self.pending_original,
                talk=self.pending_talk,
                text=text_picked,
            )
        except Exception as exc:
            logger.exception("Translation panel configuration failed")
            self.applying = False
            self.refresh()
            await interaction.edit_original_response(
                content=None, embed=self.embed(self.cog.operation_error(exc)), view=self
            )
            return
        self.applying = False
        self.finished = True
        self.stop()
        await interaction.edit_original_response(content=None, embed=self.embed(), view=None)

    @discord.ui.button(label="キャンセル", row=4)
    async def cancel(self, interaction, button):
        self.finished = True
        self.stop()
        await interaction.response.edit_message(
            content="翻訳設定の変更をキャンセルしました。", embed=None, view=None
        )

    async def on_timeout(self):
        if self.finished or self.applying:
            return
        self.finished = True
        if self.message is not None:
            try:
                await self.message.edit(
                    content="翻訳設定パネルは期限切れです。/translate で開き直してください。",
                    embed=None,
                    view=None,
                )
            except discord.HTTPException:
                logger.warning("Could not expire translation panel")


class TranslationCog(commands.Cog):
    def __init__(
        self,
        bot,
        settings=None,
        client=None,
        controller=None,
        talk_log=None,
        sessions=None,
        restrictions=None,
        conversation=None,
        kitasan=_DEFAULT,
        config=None,
        miq=_DEFAULT,
    ):
        self.bot = bot
        self.restrictions = restrictions or UserRestrictions()
        self.config = config or TranslationCogConfig()
        self.settings = settings or TranslationSettings(self.config.translation_settings_path)
        self.client = client or SwitchableClient()
        self.controller = controller or build_translation_controller(
            getattr(self.client, "backend", None),
        )
        self.control_lock = asyncio.Lock()
        self.backend_ready = False
        self.last_error = ""
        self.queue = asyncio.Queue(maxsize=32)
        self.worker = None
        self.warnings = {}
        self.voice_epochs = {}
        self.conversation = conversation or ConversationService(self.client)
        # 「今北産業」だけの投稿は、直前の投稿を3行に要約して返す（会話・翻訳とは別経路）。
        # クライアントは会話・翻訳と同じものを使い回す。
        self.kitasan = (
            KitasanService(
                self.client,
                bot_id=getattr(getattr(bot, "user", None), "id", None),
                restrictions=restrictions,
            )
            if kitasan is _DEFAULT else kitasan
        )
        self.miq = miq
        self.talk_pending = set()
        # いま話している相手（チャンネルID → (ユーザーID, 最終やり取りの時刻)）。
        # 会話を続けられるのはこの人だけ。別の人はメンションでは割り込めず、
        # 語尾の短縮コマンド（self.config.talk_call_suffix）で明示的に呼び出したときだけ交代する。
        self.talk_owner: dict[int, tuple[int, float]] = {}
        # 誰が・何回・何を呼び出したか（横槍で無視した分も kind 付きで残す）。
        self.talk_log = (
            talk_log
            if talk_log is not None
            else TalkLog(self.config.talk_log_path, self.config.talk_stats_path)
        )
        # セッション（1会話=1 SQLite）。None なら従来どおり Discord の返信から文脈を作る。
        self.sessions = sessions if sessions is not None else SessionStore(self.config.session_dir)

    async def cog_load(self):
        try:
            await self.controller.set_enabled(self.settings.any_enabled())
            self.backend_ready = self.settings.any_enabled()
        except Exception as exc:
            self.last_error = self.operation_error(exc)
            logger.exception("Translation server startup reconciliation failed")
        self.worker = asyncio.create_task(self.process_queue())

    async def cog_unload(self):
        if self.worker:
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass
        self.talk_log.flush()
        await self.client.close()

    @property
    def manages_process(self):
        """ローカルの翻訳サーバーを起動・停止するバックエンドか。"""
        return bool(getattr(self.controller, "manages_process", True))

    @property
    def applying_note(self):
        return (
            "翻訳サーバーの起動・停止を確認しています…"
            if self.manages_process
            else "翻訳の設定を保存しています…"
        )

    @staticmethod
    def operation_error(exc):
        return (
            str(exc)
            if isinstance(exc, TranslationControlError)
            else "翻訳の設定変更に失敗しました。設定の保存先・サーバーの状態を確認してください。"
        )

    async def set_enabled(self, guild_id, channel_id, enabled, users=None):
        async with self.control_lock:
            if enabled:
                previously_enabled = self.settings.any_enabled()
                try:
                    await self.controller.set_enabled(True)
                    self.backend_ready = True
                    self.settings.change(guild_id, channel_id, True, users)
                except Exception as exc:
                    self.last_error = self.operation_error(exc)
                    if isinstance(exc, TranslationControlError):
                        self.backend_ready = False
                    if not previously_enabled:
                        self.backend_ready = False
                        # If persistence failed after a successful load, release it.
                        try:
                            await self.controller.set_enabled(False)
                        except Exception:
                            logger.exception("Could not clean up failed translation start")
                    raise
            else:
                self.settings.change(guild_id, channel_id, False, users)
                self.talk_owner.pop(channel_id, None)
                if not self.settings.any_enabled():
                    self.backend_ready = False
                    try:
                        await self.controller.set_enabled(False)
                    except Exception as exc:
                        self.last_error = self.operation_error(exc)
                        raise
            self.last_error = ""

    def validate(self, interaction, enabled, users, text_ids=()):
        if (
            interaction.channel_id not in self.config.allowed_channel_ids
            and interaction.channel_id not in self.config.talk_open_channels
        ):
            return "翻訳はBotの許可チャンネルで設定してください。"
        if enabled:
            if not users:
                return "翻訳元の参加者を選択してください。"
            text_ids = set(text_ids)
            for u in users:
                if u is None:
                    return (
                        "サーバーに見つからないユーザーが含まれています。"
                        "/translate を開き直して選び直してください。"
                    )
                if getattr(u, "bot", False):
                    return "Botは翻訳対象にできません。"
                voice = getattr(u, "voice", None)
                if not (voice and voice.channel) and u.id not in text_ids:
                    return (
                        "翻訳オンにする対象は、このサーバーのVC参加者か"
                        "直近の投稿者から選んでください。"
                    )
        return None

    async def configure(self, guild_id, channel_id, users, show_original, talk=False, text=()):
        async with self.control_lock:
            previously_enabled = self.settings.any_enabled()
            try:
                if users or talk:
                    await self.controller.set_enabled(True)
                    self.backend_ready = True
                self.settings.configure(
                    guild_id, channel_id, users, show_original, talk=talk, text_users=text
                )
                if not talk:
                    self.talk_owner.pop(channel_id, None)
                if not self.settings.any_enabled():
                    self.backend_ready = False
                    await self.controller.set_enabled(False)
                self.last_error = ""
            except Exception as exc:
                self.last_error = self.operation_error(exc)
                if isinstance(exc, TranslationControlError):
                    self.backend_ready = False
                if (users or talk) and not previously_enabled:
                    self.backend_ready = False
                    try:
                        await self.controller.set_enabled(False)
                    except Exception:
                        logger.exception("Could not clean up failed panel configuration")
                raise

    def status(self, interaction):
        users = sorted(self.settings.users(interaction.guild_id, interaction.channel_id))
        mode = self.settings.mode(interaction.guild_id, interaction.channel_id)
        if mode == "talk":
            text = (
                "**会話・要約：オン**" if self.backend_ready else "**会話・要約：起動失敗・停止中**"
            )
            text += (
                "\nメンション、Botへのリプ、語尾の「{}」で応答します。VC参加は不要です。".format(
                    self.config.talk_call_suffix or "（未設定）"
                )
            )
            stats = self.talk_log.summary(interaction.guild_id, interaction.channel_id)
            if stats:
                text += "\n" + stats
            partners = sorted(self.talk_partners(interaction.channel_id))
            if partners:
                text += "\nいまの相手：" + " / ".join(f"<@{uid}>" for uid in partners[:3])
            try:
                sessions = (
                    self.sessions.for_channel_summary(
                        interaction.guild_id,
                        interaction.channel_id,
                    )
                    if self.sessions
                    else ""
                )
            except Exception:
                logger.exception("会話セッションの一覧を読めません")
                sessions = ""

            if sessions:
                text += "\n" + sessions
            if self.last_error:
                text += "\n⚠ " + self.last_error
            return text
        state = (
            "オン" if users and self.backend_ready else ("起動失敗・停止中" if users else "オフ")
        )
        text = f"**日本語翻訳：{state}**"
        if users:
            text += "\n対象：" + user_labels(users)
            text += "\n対象者がVCにいる間、日本語訳をこのチャンネル全員に公開します。"
            text_ids = sorted(
                self.settings.text_users(interaction.guild_id, interaction.channel_id)
            )
            if text_ids:
                text += "\nテキスト対象（VC外でも翻訳）：" + user_labels(text_ids)
        show = self.settings.show_original(interaction.guild_id, interaction.channel_id)
        text += "\n原文表示：" + (
            "オン（コピー用コードブロック）" if show else "オフ（日本語訳のみ）"
        )
        if not users:
            released = (
                "モデル停止・メモリ解放済み。"
                if self.manages_process
                else "停止しています（クラウドのため解放するものはありません）。"
            )
            failed = (
                "停止・メモリ解放を確認できていません。"
                if self.manages_process
                else "停止を確認できていません。"
            )
            text += "\n" + (
                "ほかのチャンネルにも翻訳対象が設定されています。"
                if self.settings.any_enabled()
                else (failed if self.last_error else released)
            )
        if self.last_error:
            text += "\n⚠ " + self.last_error
        return text

    @app_commands.command(
        name="translate", description="翻訳・会話設定パネル（停止／翻訳／会話・要約）"
    )
    @app_commands.guild_only()
    async def translate(self, interaction: discord.Interaction):
        if not can_manage(interaction.user, self.config.owner_id):
            await interaction.response.send_message(
                "翻訳の設定はオーナーまたはサーバー管理権限を持つ人が変更できます。", ephemeral=True
            )
            return
        error = self.validate(interaction, False, [])
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        recent = []
        try:
            async for msg in interaction.channel.history(limit=20):
                author = getattr(msg, "author", None)
                if (
                    author is None
                    or getattr(author, "bot", False)
                    or getattr(msg, "webhook_id", None)
                ):
                    continue
                recent.append(author)
        except discord.HTTPException, AttributeError:
            logger.warning("Could not fetch recent authors for translation panel")
        view = TranslationView(self, interaction, recent)
        await interaction.response.defer(ephemeral=True)
        view.message = await interaction.followup.send(
            embed=view.embed(), view=view, ephemeral=True, wait=True
        )

    # ------------------------------------------------------------------ モデル切替

    llm_group = app_commands.Group(
        name="llm",
        description="翻訳・会話のモデル（バックエンド）を表示・切り替え",
    )

    @staticmethod
    def _llm_backend_lines(snapshot):
        lines = []

        for name in llm_backend.BACKENDS:
            info = llm_backend.INFO[name]
            marks = []

            if name == snapshot["backend"]:
                marks.append("使用中")

            if name == snapshot["default"]:
                marks.append("既定")

            lines.append(
                f"・**{info.display}**（`{name}`）\n　{info.detail}"
                + (f"［{'・'.join(marks)}］" if marks else "")
            )

        return "\n".join(lines)

    def _llm_can_switch(self, user) -> bool:
        if user.id in self.config.tts_switch_user_ids:
            return True

        return can_manage(user, self.config.owner_id)

    async def _llm_guard(self, interaction) -> bool:
        if self._llm_can_switch(interaction.user):
            return True

        await interaction.response.send_message(
            "モデルの切り替えはオーナーまたはサーバー管理権限を持つ人だけです。",
            ephemeral=True,
        )

        return False

    async def _llm_switch_to(self, interaction, backend, read_mode=None):
        """保存して、走っているクライアントと制御を差し替える（defer 済み前提）。"""

        snapshot = llm_backend.snapshot()

        if backend == snapshot["backend"] and (
            read_mode is None or read_mode == snapshot["read_mode"]
        ):
            await interaction.followup.send(
                f"すでに **{snapshot['display']}**（`{snapshot['backend']}`）です。"
                f"\n読み上げ: {llm_backend.read_mode_label(snapshot['read_mode'])}",
                ephemeral=True,
            )
            return

        try:
            snapshot = llm_backend.set_selection(backend, read_mode)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        logger.info(
            "LLM backend switched to %s (read_mode=%s) by %s",
            snapshot["backend"],
            snapshot["read_mode"],
            interaction.user.id,
        )

        notes = []
        switched = False

        if hasattr(self.client, "switch"):
            await self.client.switch(snapshot["backend"])
            switched = True

        previous = self.controller
        self.controller = build_translation_controller(snapshot["backend"])

        # ローカルの llama.cpp を離れるときは、翻訳対象が残っていても止めて VRAM を返す。
        if previous is not self.controller and getattr(previous, "manages_process", False):
            try:
                await previous.set_enabled(False)
            except Exception as exc:
                logger.exception("Could not stop the previous translation backend")
                notes.append(
                    f"前のローカル翻訳を停止できませんでした（{self.operation_error(exc)}）"
                )

        try:
            await self.controller.set_enabled(self.settings.any_enabled())
            if self.settings.any_enabled():
                self.backend_ready = True
        except Exception as exc:
            logger.exception("Could not reconcile the new translation backend")
            notes.append(
                f"新しいバックエンドの起動を確認できませんでした（{self.operation_error(exc)}）"
            )

        health = None

        if switched:
            health = await self.client.health()

        lines = [
            f"**{snapshot['display']} に切り替えました**（`{snapshot['backend']}`）",
            f"読み上げ: {llm_backend.read_mode_label(snapshot['read_mode'])}",
        ]

        if snapshot["read_mode"] == "wav":
            lines.append("辞書に登録した wav だけ鳴らし、TTS は使いません（入室案内もしません）。")

        if health is True:
            lines.append("疎通: OK")
        elif health is False:
            lines.append("⚠ 疎通を確認できません（サーバの起動・接続を確認してください）。")

        lines.extend(notes)
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @llm_group.command(
        name="status",
        description="いま使っているモデルと読み上げモードを表示します",
    )
    async def llm_status(self, interaction: discord.Interaction):
        if not await self._llm_guard(interaction):
            return

        await interaction.response.defer(ephemeral=True)
        snapshot = llm_backend.snapshot()
        source_label = (
            "保存した選択" if snapshot["source"] == "file" else "既定値（TRANSLATION_BACKEND）"
        )
        lines = [
            f"**いまのモデル: {snapshot['display']}**（`{snapshot['backend']}`）",
            f"読み上げ: {llm_backend.read_mode_label(snapshot['read_mode'])}",
            f"由来: {source_label}",
            "",
            self._llm_backend_lines(snapshot),
        ]

        health = None

        if hasattr(self.client, "health"):
            health = await self.client.health()

        if health is True:
            lines.append(f"\n疎通: OK（{self.client.display}）")
        elif health is False:
            lines.append(f"\n⚠ 疎通: NG（{self.client.display}）")

        overrides = snapshot.get("channels") or {}

        if overrides:
            rows = []

            for channel_id, name in list(overrides.items())[:10]:
                rows.append(f"　<#{channel_id}> → `{name}`")

            lines.append("\n**チャンネル別の設定**\n" + "\n".join(rows))

            if len(overrides) > 10:
                lines.append(f"　ほか {len(overrides) - 10} 件")

        lines.append(
            "\n切替: `/llm switch`（全体）　チャンネル別: `/llm channel`　読み上げ: `/llm read`"
        )
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @llm_group.command(
        name="switch",
        description="翻訳・会話のモデルを切り替えます（この選択は保存されます）",
    )
    @app_commands.describe(
        backend="使うバックエンド",
        read_mode="読み上げモード（省略時はバックエンドの既定）",
    )
    @app_commands.choices(
        backend=[
            app_commands.Choice(name="sarashina（クラウド・文脈128k）", value="sarashina"),
            app_commands.Choice(name="local（GPU機の llama.cpp・文脈4096）", value="local"),
            app_commands.Choice(name="atmos（z170 の gemma4-atmos-sft）", value="atmos"),
        ],
        read_mode=[
            app_commands.Choice(name="TTSで読み上げ", value="tts"),
            app_commands.Choice(name="辞書wavのみ（TTSしない）", value="wav"),
        ],
    )
    async def llm_switch(
        self,
        interaction: discord.Interaction,
        backend: app_commands.Choice[str],
        read_mode: app_commands.Choice[str] | None = None,
    ):
        if not await self._llm_guard(interaction):
            return

        await interaction.response.defer(ephemeral=True)
        await self._llm_switch_to(
            interaction,
            backend.value,
            read_mode.value if read_mode is not None else None,
        )

    @llm_group.command(
        name="channel",
        description="このチャンネルだけ別のモデルにします（全体設定より優先）",
    )
    @app_commands.describe(
        backend="このチャンネルで使うバックエンド",
        channel="対象のチャンネル／スレッド（省略時はこのチャンネル）",
        clear="このチャンネルの設定を消して全体設定に戻す",
    )
    @app_commands.choices(
        backend=[
            app_commands.Choice(name="sarashina（クラウド・文脈128k）", value="sarashina"),
            app_commands.Choice(name="local（GPU機の llama.cpp・文脈4096）", value="local"),
            app_commands.Choice(name="atmos（z170 の gemma4-atmos-sft）", value="atmos"),
        ]
    )
    async def llm_channel(
        self,
        interaction: discord.Interaction,
        backend: app_commands.Choice[str] | None = None,
        channel: discord.TextChannel | discord.Thread | None = None,
        clear: bool = False,
    ):
        if not await self._llm_guard(interaction):
            return

        target = channel or interaction.channel

        if target is None:
            await interaction.response.send_message(
                "チャンネルを特定できませんでした。`channel` を指定してください。",
                ephemeral=True,
            )
            return

        if clear:
            llm_backend.clear_channel(target.id)
            note = f"**{target.mention} は全体設定に戻しました**"
        elif backend is None:
            await interaction.response.send_message(
                "`backend` を選ぶか、`clear` を付けてください。",
                ephemeral=True,
            )
            return
        else:
            try:
                snapshot = llm_backend.set_channel(target.id, backend.value)
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            note = (
                f"**{target.mention} は {snapshot['display']}**"
                f"（`{snapshot['backend']}`）にしました"
            )

        lines = [
            note,
            f"全体の設定: `{llm_backend.snapshot()['global_backend']}`",
            "読み上げモードはチャンネル別ではなく全体の設定です（`/llm read`）。",
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @llm_group.command(
        name="read",
        description="読み上げモードだけを変えます（モデルはそのまま）",
    )
    @app_commands.describe(mode="TTSで読み上げる / 辞書wavだけ鳴らす")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="TTSで読み上げ", value="tts"),
            app_commands.Choice(name="辞書wavのみ（TTSしない）", value="wav"),
        ]
    )
    async def llm_read(
        self,
        interaction: discord.Interaction,
        mode: app_commands.Choice[str],
    ):
        if not await self._llm_guard(interaction):
            return

        await interaction.response.defer(ephemeral=True)
        snapshot = llm_backend.snapshot()
        await self._llm_switch_to(interaction, snapshot["backend"], mode.value)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot or before.channel is None or after.channel is not None:
            return
        await self.remove_departed_user(member.guild.id, member.id)

    async def remove_departed_user(self, guild_id, user_id):
        # Invalidate in-flight results immediately, even while a start/stop holds
        # the control lock and the member quickly reconnects.
        key = (guild_id, user_id)
        self.voice_epochs[key] = self.voice_epochs.get(key, 0) + 1
        async with self.control_lock:
            if not self.settings.remove_user(guild_id, user_id):
                return
            if not self.settings.any_enabled():
                self.backend_ready = False
                try:
                    await self.controller.set_enabled(False)
                    self.last_error = ""
                except Exception as exc:
                    self.last_error = self.operation_error(exc)
                    logger.exception("Could not release translation model after VC departure")

    @commands.Cog.listener()
    async def on_ready(self):
        # Discord voice-state caches are available here, unlike setup_hook.
        # Remove stale targets whose departure occurred while the bot was down.
        for guild in self.bot.guilds:
            if guild.unavailable:
                continue
            voice_ids = {
                m.id
                for channel in [*guild.voice_channels, *guild.stage_channels]
                for m in channel.members
            }
            targets = set().union(
                *(
                    users
                    for key, users in self.settings.targets.items()
                    if key.startswith(f"{guild.id}:")
                )
            )
            for user_id in targets - voice_ids:
                await self.remove_departed_user(guild.id, user_id)

    @commands.Cog.listener()
    async def on_message(self, message):
        if not message.guild or message.author.bot or message.webhook_id:
            return
        # まるごと無視にされている人の投稿には返事をしない
        if self.restrictions.ignored(message.guild.id, message.author.id):
            return
        # 「miq」だけの投稿は引用画像の合図なので、会話としても読み上げとしても扱わない
        if (
            is_miq_trigger(message.content) if self.miq is _DEFAULT
            else self.miq is not None and self.miq.matches_trigger(message.content)
        ):
            return
        if (
            message.channel.id not in self.config.allowed_channel_ids
            and message.channel.id not in self.config.talk_open_channels
        ):
            return
        # 「今北産業」だけの投稿は、直前の投稿を3行に要約して返す（会話・翻訳より先に判定）
        if self.kitasan is not None and self.kitasan.matches_trigger(message.content):
            await self.kitasan.handle(message)
            return
        if self.settings.mode(message.guild.id, message.channel.id) == "talk":
            await self.enqueue_talk(message)
            return
        if not message.content.strip():
            return
        ticket = self.settings.ticket(message.guild.id, message.channel.id, message.author.id)
        if ticket is None:
            return
        voice = getattr(message.author, "voice", None)
        in_vc = bool(voice and voice.channel)
        if not in_vc and message.author.id not in self.settings.text_users(
            message.guild.id, message.channel.id
        ):
            return
        ticket = (*ticket, self.voice_epochs.get((message.guild.id, message.author.id), 0))
        text = translation_text(message.content)
        if not text:
            return
        if not self.backend_ready:
            await self.warn(
                message,
                self.last_error
                or "翻訳サーバーが停止しています。/translate で再度オンにしてください。",
            )
            return
        try:
            self.queue.put_nowait((message, ticket, text))
        except asyncio.QueueFull:
            await self.warn(message, "翻訳が混み合っているため、この投稿の翻訳をスキップしました。")

    def active(self, message, ticket):
        if ticket[2] == "talk":
            key = self.settings.key(message.guild.id, message.channel.id)
            return (
                self.backend_ready
                and self.settings.mode(message.guild.id, message.channel.id) == "talk"
                and ticket[:2] == (key, self.settings.revisions.get(key, 0))
            )
        member = message.guild.get_member(message.author.id)
        in_vc = bool(member and member.voice and member.voice.channel)
        if in_vc:
            return (
                self.backend_ready
                and self.settings.valid(ticket[:2], message.author.id)
                and ticket[2] == self.voice_epochs.get((message.guild.id, message.author.id), 0)
            )
        return (
            self.backend_ready
            and self.settings.valid(ticket[:2], message.author.id)
            and ticket[2] == self.voice_epochs.get((message.guild.id, message.author.id), 0)
            and message.author.id in self.settings.text_users(message.guild.id, message.channel.id)
        )

    def talk_model(self) -> str:
        return self.config.talk_model

    def talk_system_prompt(self) -> str:
        """セッションへ保存するシステムプロンプト（ファイル優先）。"""
        if not self.config.talk_system_prompt_file:
            return SYSTEM

        try:
            text = Path(self.config.talk_system_prompt_file).read_text(encoding="utf-8").strip()
        except OSError:
            logger.warning(
                "システムプロンプトのファイルを読めません: %s", self.config.talk_system_prompt_file
            )
            return SYSTEM

        return text or SYSTEM

    def resolve_session(self, message, reply_to):
        """この投稿が属するセッションを決める（無効時は None）。"""
        if self.sessions is None:
            return None

        author = message.author

        try:
            session_id, action = self.sessions.resolve(
                message.guild.id,
                message.channel.id,
                author.id,
                getattr(author, "display_name", "") or getattr(author, "name", ""),
                reply_to,
            )
        except Exception:
            logger.exception("会話セッションを解決できません")
            return None

        if action == "new":
            # 新規セッションは今の既定を焼き込む。フォークは親の設定を引き継ぐ。
            self.sessions.update_settings(
                session_id,
                model=self.talk_model(),
                system_prompt=self.talk_system_prompt(),
            )

        return session_id

    def talk_partner(self, channel_id):
        """いま話している相手。無操作が続いて会話が終わっていれば None。"""
        remembered = self.talk_owner.get(channel_id)

        if not remembered:
            return None

        user_id, seen = remembered

        if (
            self.config.talk_session_seconds
            and time.monotonic() - seen > self.config.talk_session_seconds
        ):
            self.talk_owner.pop(channel_id, None)
            return None

        return user_id

    def remember_talk_partner(self, channel_id, user_id):
        self.talk_owner[channel_id] = (user_id, time.monotonic())

    def talk_partners(self, channel_id) -> set:
        """いま会話中の相手。セッションがあればそちらが正（再起動でも残る）。"""
        if self.sessions is not None:
            try:
                return set(
                    self.sessions.active_partners(channel_id, self.config.talk_session_seconds)
                )
            except Exception:
                logger.exception("会話セッションの相手を読めません")

        partner = self.talk_partner(channel_id)
        return {partner} if partner else set()

    async def talk_invocation(self, message):
        """会話モードの呼び出しかどうかと、モデルへ渡す本文を返す。

        呼び出し方は3つ。メンション、語尾の短縮コマンド（既定 `jbot`）、
        Botへの返信。会話が続いている間は**相手が決まっていて**、

        - 相手本人: メンション・語尾・返信のどれでも続けられる
        - ほかの人: メンションでは割り込めない（返信も同じ）。
          語尾のコマンドで明示的に呼び出したときだけ相手が交代する
        - 会話が終わっていれば（無操作 self.config.talk_session_seconds）、
          ほかの人の普通のメンションでも新しく始められる
        """
        bot_id = self.bot.user.id
        mentioned = any(user.id == bot_id for user in getattr(message, "mentions", []))
        text = message.clean_content
        tail = text.rstrip()

        # 返信先が miq の生成した画像なら、内容によらず返事をしない
        parent = await referenced_message(message)

        if parent is not None and is_miq_image(parent):
            logger.info(
                "miq画像への返信には応答しない: channel=%s user=%s",
                message.channel.id,
                message.author.id,
            )
            return None, text, "ignored-miq-image"

        suffix = self.config.talk_call_suffix
        called = bool(suffix) and tail.casefold().endswith(suffix.casefold())

        if called:
            # 語尾の合図は本文から取り除いてからモデルへ渡す。
            # 明示的な呼び出しなので、会話中でも相手を交代できる。
            return "call", tail[: -len(suffix)].strip(), "suffix"

        if mentioned:
            import re

            partners = self.talk_partners(message.channel.id)

            # 既定（open）は誰のメンションにも応答する。相手ごとに別セッションに
            # なるので、進行中の会話を奪うことはない。
            # session にすると、会話を持っている人だけが呼べる（他は黙って無視）。
            if (
                self.config.talk_mention_gate == "session"
                and partners
                and message.author.id not in partners
                and message.channel.id not in self.config.talk_open_channels
            ):
                logger.info(
                    "会話中のため他ユーザーのメンションを見送り: channel=%s user=%s",
                    message.channel.id,
                    message.author.id,
                )
                return None, text, "ignored-mention"

            return "call", re.sub(rf"<@!?{bot_id}>", "", message.content).strip(), "mention"

        if not parent or parent.author.id != bot_id:
            return None, text, None

        # 返信は誰でも受ける。話の続きはセッション側が決める（別の人ならフォーク）。
        return "reply", text, "reply"

    async def enqueue_talk(self, message):
        invocation, text, kind = await self.talk_invocation(message)

        if kind is None:
            return

        author = message.author
        self.talk_log.record(
            message.guild.id,
            message.channel.id,
            author.id,
            getattr(author, "display_name", "") or getattr(author, "name", ""),
            text,
            kind=kind,
        )

        if invocation is None:
            return

        if invocation in ("call", "reply"):
            # 呼び出した本人を覚える。返信のときは会話を切らさない。
            self.remember_talk_partner(message.channel.id, author.id)

        if not self.backend_ready:
            await self.warn(
                message,
                self.last_error
                or (
                    "会話サーバーが停止しています。"
                    "/translate の設定パネルで再度オンにしてください。"
                ),
            )
            return
        pending_key = (message.channel.id, message.author.id)
        if pending_key in self.talk_pending:
            await self.warn(
                message, "前の依頼に回答中です。回答が終わってから次の依頼を送ってください。"
            )
            return
        if not text and not any(
            a.filename.lower().endswith((".txt", ".md", ".log"))
            for a in getattr(message, "attachments", [])
        ):
            return
        key = self.settings.key(message.guild.id, message.channel.id)
        reply_to = getattr(getattr(message, "reference", None), "message_id", None)
        session_id = self.resolve_session(message, reply_to)
        ticket = (key, self.settings.revisions.get(key, 0), "talk", session_id)
        try:
            self.queue.put_nowait((message, ticket, text))
            self.talk_pending.add(pending_key)
        except asyncio.QueueFull:
            await self.warn(
                message, "会話・要約が混み合っています。少し待ってから依頼してください。"
            )

    async def _save_session_turns(self, message, session_id, text, answer, sent_ids, sent_at=None):
        """呼び出しと回答をセッションへ残し、Botの投稿を返信先として登録する。"""
        author = message.author

        try:
            await asyncio.to_thread(
                self.sessions.append_user,
                session_id,
                author.id,
                getattr(author, "display_name", "") or getattr(author, "name", ""),
                text,
                getattr(message, "id", None),
                created_at=getattr(message, "created_at", None),
                avatar=getattr(getattr(author, "avatar", None), "key", None),
            )
            turn = await asyncio.to_thread(
                self.sessions.append_assistant,
                session_id,
                answer,
                sent_ids[0] if sent_ids else None,
                created_at=sent_at,
            )

            for message_id in sent_ids:
                await asyncio.to_thread(
                    self.sessions.register_message,
                    message_id,
                    session_id,
                    turn,
                    True,
                    created_at=sent_at,
                )
        except Exception:
            logger.exception("会話セッションの保存に失敗: %s", session_id)

    async def warn(self, message, text):
        now = time.monotonic()
        if now - self.warnings.get(message.channel.id, -100) < 30:
            return
        self.warnings[message.channel.id] = now
        try:
            await message.reply(
                text, mention_author=False, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException:
            logger.warning("Could not post translation warning in channel %s", message.channel.id)

    async def process_queue(self):
        while True:
            message, ticket, text = await self.queue.get()
            # このメッセージを処理している間だけ、チャンネル別のモデル設定を効かせる
            channel_token = llm_backend.set_channel_context(
                getattr(getattr(message, "channel", None), "id", None)
            )
            try:
                if not self.active(message, ticket):
                    continue
                if ticket[2] == "talk":
                    session_id = ticket[3] if len(ticket) > 3 else None
                    session_settings, history = {}, None

                    if session_id and self.sessions is not None:
                        session_settings = await asyncio.to_thread(
                            self.sessions.settings, session_id
                        )
                        history = await asyncio.to_thread(self.sessions.history, session_id)

                    active = partial(self.active, message, ticket)
                    async with message.channel.typing():
                        answer = await self.conversation.respond(
                            message, text, self.bot.user.id, active,
                            history=history,
                            system_prompt=session_settings.get("system_prompt") or None,
                            model=session_settings.get("model") or None,
                        )

                    sent_ids, sent_at = [], None
                    for page in conversation_pages(answer):
                        if not active():
                            break
                        sent = await message.reply(
                            page,
                            mention_author=False,
                            allowed_mentions=discord.AllowedMentions.none(),
                            suppress_embeds=True,
                        )
                        sent_id = getattr(sent, "id", None)
                        if sent_id is not None:
                            sent_ids.append(sent_id)
                            sent_at = sent_at or getattr(sent, "created_at", None)

                    if session_id and self.sessions is not None:
                        await self._save_session_turns(
                            message, session_id, text, answer, sent_ids, sent_at
                        )
                    continue


                    if session_id and self.sessions is not None:
                        await self._save_session_turns(
                            message, session_id, text, answer, sent_ids, sent_at
                        )
                    continue
                translated = await self.client.translate(text)
                for page in translation_pages(
                    message.author.display_name,
                    message.clean_content,
                    translated,
                    self.settings.show_original(message.guild.id, message.channel.id),
                ):
                    if not self.active(message, ticket):
                        break
                    await message.reply(
                        page,
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none(),
                        suppress_embeds=True,
                    )
            except asyncio.CancelledError:
                raise
            except ConversationCancelled:
                pass
            except Exception as exc:
                logger.exception("Translation failed for message %s", message.id)
                if self.active(message, ticket):
                    if ticket[2] == "talk":
                        await self.warn(
                            message,
                            str(exc)
                            if isinstance(exc, ValueError)
                            else (
                                "会話・要約に失敗しました。"
                                "時間・入力サイズ・メモリの上限を確認して再試行してください。"
                            ),
                        )
                    else:
                        await self.warn(
                            message, "この投稿の翻訳に失敗しました。読み上げは継続します。"
                        )
            finally:
                llm_backend.reset_channel_context(channel_token)
                if ticket[2] == "talk":
                    self.talk_pending.discard((message.channel.id, message.author.id))
                self.queue.task_done()
