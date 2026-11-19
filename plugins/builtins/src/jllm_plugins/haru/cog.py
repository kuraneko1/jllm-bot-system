# ruff: noqa: E501
"""Haru リアルタイム音声会話用 Discord スラッシュコマンドとセッション管理。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable

import discord
from discord import app_commands
from discord.ext import commands

from jllm_plugins import ducking
from jllm_plugins.playback.player import install_send_stats

from .bridge import HaruConversationSession
from .client import HaruApiClient, HaruApiError, HaruConfig
from .personas import HaruPersonaStore, build_system_prompt

logger = logging.getLogger(__name__)


class HaruCog(commands.Cog):
    """スラッシュコマンドで話せるひとを選び、VC でリアルタイム音声会話する。"""

    haru_group = app_commands.Group(
        name="haru",
        description="Haru リアルタイム音声会話",
        guild_only=True,
    )

    def __init__(
        self,
        bot: commands.Bot,
        *,
        store: HaruPersonaStore,
        client: HaruApiClient,
        config: HaruConfig | None = None,
        reader=None,
        restrictions=None,
    ):
        self.bot = bot
        self.store = store
        self.client = client
        self.config = config or HaruConfig()
        self.reader = reader
        self.restrictions = restrictions
        self.sessions: dict[int, HaruConversationSession] = {}
        self._guild_locks: dict[int, asyncio.Lock] = {}

    def _lock_for(self, guild_id: int) -> asyncio.Lock:
        lock = self._guild_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._guild_locks[guild_id] = lock
        return lock

    def _is_ignored(self, guild_id: int, user_id: int) -> bool:
        store = self.restrictions or getattr(self.bot, "restrictions", None)
        if store is None:
            return False
        ignored = getattr(store, "ignored", None)
        if callable(ignored):
            return bool(ignored(guild_id, user_id))
        return False

    def _reader_player(self, guild_id: int):
        if self.reader is None:
            return None
        players = getattr(self.reader, "players", None)
        if not isinstance(players, dict):
            return None
        return players.get(guild_id)

    async def _persona_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        needle = (current or "").strip().lower()
        choices: list[app_commands.Choice[str]] = []
        for item in self.store.list_personas():
            name = item["name"]
            pid = item["id"]
            if needle and needle not in name.lower() and needle not in pid.lower():
                continue
            label = name[:100]
            choices.append(app_commands.Choice(name=label, value=pid[:100]))
            if len(choices) >= 25:
                break
        return choices

    async def _ensure_voice_client(self, guild: discord.Guild, channel: discord.VoiceChannel):
        if not ducking.VOICE_RECV_AVAILABLE or ducking.voice_recv is None:
            raise HaruApiError("音声受信モジュール（voice_recv）が利用できません。")

        recv_cls = ducking.voice_recv.VoiceRecvClient
        voice_client = guild.voice_client
        player = self._reader_player(guild.id)

        if voice_client is not None and voice_client.is_connected():
            if isinstance(voice_client, recv_cls):
                if voice_client.channel != channel:
                    await voice_client.move_to(channel)
                return voice_client
            with contextlib.suppress(Exception):
                await voice_client.disconnect(force=True)
        elif voice_client is not None:
            with contextlib.suppress(Exception):
                await voice_client.disconnect(force=True)

        new_client = await channel.connect(cls=recv_cls)
        if player is not None:
            player.voice_client = new_client
            player.channel_id = channel.id
            player.send_stats = install_send_stats(new_client)
            with contextlib.suppress(Exception):
                await player.enable_listening()
        return new_client

    async def _send_transcript(self, text_channel_id: int, text: str) -> None:
        channel = self.bot.get_channel(text_channel_id)
        if channel is None:
            return
        send = getattr(channel, "send", None)
        if not callable(send):
            return
        session = next(
            (s for s in self.sessions.values() if s.text_channel_id == text_channel_id), None
        )
        speaker = session.persona.get("name", "Haru") if session else "Haru"
        await send(f"**{speaker}**: {text[:1900]}")

    async def _on_session_ended(self, guild_id: int, reason: str | None) -> None:
        session = self.sessions.get(guild_id)
        if session is None:
            return
        await self.stop_guild(guild_id, keep_voice_if_reader=True)
        if reason:
            channel = self.bot.get_channel(session.text_channel_id)
            send = getattr(channel, "send", None)
            if callable(send):
                with contextlib.suppress(Exception):
                    await send(f"Haru 会話が終了しました（{reason}）。")

    def _resolve_speaker_name(self, guild: discord.Guild | None, user_id: int | None) -> str:
        if guild is None or user_id is None:
            return ""
        member = guild.get_member(user_id)
        if member is None:
            return ""
        return str(member.display_name)

    async def start_session(
        self,
        *,
        guild: discord.Guild,
        voice_channel: discord.VoiceChannel,
        text_channel_id: int,
        persona: dict,
        focus_user_id: int | None,
        show_transcript: bool = True,
    ) -> HaruConversationSession:
        async with self._lock_for(guild.id):
            if not await self.client.ready():
                raise HaruApiError(
                    "Haru サーバが起動していないか、モデルの準備中です。"
                    "少し待ってからもう一度お試しください。"
                )

            vpath = self.store.voice_path(persona)
            audio = await asyncio.to_thread(vpath.read_bytes) if vpath is not None else None
            speaker_name = self._resolve_speaker_name(guild, focus_user_id)
            system_prompt = build_system_prompt(persona, speaker_name=speaker_name)
            guidance = float(persona.get("guidance_scale") or self.config.guidance_scale)

            session_id = await self.client.create_session(
                persona_id=persona["id"],
                system_prompt=system_prompt,
                audio=audio,
                guidance_scale=guidance,
            )

            await self._stop_guild_locked(guild.id, disconnect=False)
            voice_client = await self._ensure_voice_client(guild, voice_channel)
            ws = await self.client.connect_chat(session_id)

            session = HaruConversationSession(
                guild_id=guild.id,
                voice_channel_id=voice_channel.id,
                text_channel_id=text_channel_id,
                persona=persona,
                voice_client=voice_client,
                ws=ws,
                focus_user_id=focus_user_id,
                output_volume=self.config.output_volume,
                show_transcript=show_transcript,
                on_transcript=self._send_transcript,
                on_ended=self._on_session_ended,
            )
            self.sessions[guild.id] = session
            session.start()
            return session

    async def _stop_guild_locked(self, guild_id: int, *, disconnect: bool) -> bool:
        session = self.sessions.pop(guild_id, None)
        if session is None:
            return False
        await session.close()
        if disconnect and self._reader_player(guild_id) is None:
            vc = session.voice_client
            if vc is not None and getattr(vc, "is_connected", lambda: False)():
                with contextlib.suppress(Exception):
                    await vc.disconnect(force=False)
        return True

    async def stop_guild(self, guild_id: int, *, keep_voice_if_reader: bool = True) -> bool:
        async with self._lock_for(guild_id):
            return await self._stop_guild_locked(guild_id, disconnect=keep_voice_if_reader)

    def set_guild_focus(self, guild_id: int, user_id: int | None) -> int | None:
        saved = self.store.set_focus(guild_id, user_id)
        session = self.sessions.get(guild_id)
        if session is not None:
            session.set_focus(saved)
        return saved

    async def switch_active_persona(self, guild_id: int, persona_id: str) -> HaruConversationSession:
        session = self.sessions.get(guild_id)
        if session is None:
            raise HaruApiError("このサーバーでは Haru 会話が開始されていません。")
        persona = self.store.get(persona_id)
        if persona is None:
            raise HaruApiError("指定された話者が見つかりません。")
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            raise HaruApiError("サーバーが見つかりません。")
        channel = guild.get_channel(session.voice_channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            raise HaruApiError("ボイスチャンネルが見つかりません。")
        focus_uid = self.store.get_focus(guild_id)
        return await self.start_session(
            guild=guild,
            voice_channel=channel,
            text_channel_id=session.text_channel_id,
            persona=persona,
            focus_user_id=focus_uid,
            show_transcript=session.show_transcript,
        )

    async def status_snapshot(self) -> dict:
        server_ready = await self.client.ready()
        guilds: list[dict] = []
        seen_guilds: set[int] = set()

        def member_list(members: Iterable[discord.Member]) -> list[dict]:
            out = []
            for m in members:
                if getattr(m, "bot", False):
                    continue
                out.append({"id": str(m.id), "name": str(m.display_name)})
            return sorted(out, key=lambda x: x["name"].lower())

        for guild_id, session in list(self.sessions.items()):
            seen_guilds.add(guild_id)
            guild = self.bot.get_guild(guild_id)
            vc = guild.get_channel(session.voice_channel_id) if guild is not None else None
            members = member_list(getattr(vc, "members", ())) if vc is not None else []
            focus = session.focus_user_id
            focus_name = next((m["name"] for m in members if m["id"] == str(focus)), "") if focus else ""
            if focus and not focus_name and guild is not None:
                m = guild.get_member(focus)
                if m is not None:
                    focus_name = str(m.display_name)
            guilds.append(
                {
                    "guild_id": str(guild_id),
                    "guild_name": str(getattr(guild, "name", guild_id)),
                    "active": True,
                    "channel_id": str(session.voice_channel_id),
                    "channel_name": str(getattr(vc, "name", session.voice_channel_id)),
                    "persona_id": session.persona.get("id", ""),
                    "persona_name": session.persona.get("name", ""),
                    "focus_user_id": str(focus) if focus is not None else None,
                    "focus_user_name": focus_name,
                    "members": members,
                }
            )

        for guild in getattr(self.bot, "guilds", ()):
            if guild.id in seen_guilds:
                continue
            vc = getattr(getattr(guild, "voice_client", None), "channel", None)
            focus = self.store.get_focus(guild.id)
            members = member_list(getattr(vc, "members", ())) if vc is not None else []
            if vc is None and focus is None:
                continue
            guilds.append(
                {
                    "guild_id": str(guild.id),
                    "guild_name": str(guild.name),
                    "active": False,
                    "channel_id": str(vc.id) if vc is not None else None,
                    "channel_name": str(vc.name) if vc is not None else "",
                    "persona_id": "",
                    "persona_name": "",
                    "focus_user_id": str(focus) if focus is not None else None,
                    "focus_user_name": self._resolve_speaker_name(guild, focus),
                    "members": members,
                }
            )

        return {
            "server_ready": server_ready,
            "personas": self.store.list_personas(),
            "guilds": guilds,
        }

    # ------------------------------------------------------------ Slash Commands

    @haru_group.command(
        name="join",
        description="話せるひとを選んでボイスチャンネルでリアルタイム会話を始めます",
    )
    @app_commands.describe(
        persona="話す相手（WebUI の「Haru会話」タブで登録・編集）",
        focus="声を聞く相手（省略すると設定中の対象または全員）",
        transcript="Haru の返答テキストをチャットにも流す（既定: True）",
    )
    @app_commands.autocomplete(persona=_persona_autocomplete)
    async def haru_join(
        self,
        interaction: discord.Interaction,
        persona: str | None = None,
        focus: discord.Member | None = None,
        transcript: bool = True,
    ):
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
            return
        if self._is_ignored(guild.id, interaction.user.id):
            await interaction.response.send_message("この操作は制限されています。", ephemeral=True)
            return
        voice_state = interaction.user.voice
        if voice_state is None or not isinstance(voice_state.channel, discord.VoiceChannel):
            await interaction.response.send_message(
                "先にボイスチャンネルに参加してください。", ephemeral=True
            )
            return

        personas = self.store.list_personas()
        if not personas:
            await interaction.response.send_message(
                "話せるひとがまだ登録されていません。WebUI の管理者画面「Haru会話」タブから登録してください。",
                ephemeral=True,
            )
            return

        target = self.store.get(persona) if persona else personas[0]
        if target is None:
            await interaction.response.send_message(
                "指定された相手が見つかりませんでした。候補から選び直してください。",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        focus_uid = (
            self.store.set_focus(guild.id, focus.id)
            if focus is not None
            else self.store.get_focus(guild.id)
        )
        try:
            await self.start_session(
                guild=guild,
                voice_channel=voice_state.channel,
                text_channel_id=interaction.channel_id,
                persona=target,
                focus_user_id=focus_uid,
                show_transcript=transcript,
            )
        except HaruApiError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except (discord.DiscordException, OSError):
            logger.exception("Haru VC 接続に失敗")
            await interaction.followup.send(
                "ボイスチャンネルへの接続に失敗しました。Bot の権限を確認してください。",
                ephemeral=True,
            )
            return

        focus_label = (
            f"<@{focus_uid}> のみ"
            if focus_uid is not None
            else "VC 全員（ミックス）"
        )
        await interaction.followup.send(
            f"{voice_state.channel.mention} で **{target['name']}** と会話を始めました。\n"
            f"聞き取り対象: {focus_label}",
            ephemeral=True,
        )

    @haru_group.command(
        name="focus",
        description="Haru が声を聞く相手（特定の1人 / 省略で全員）を切り替えます",
    )
    @app_commands.describe(member="フォーカスする相手（指定しない場合は VC 全員の声を聞きます）")
    async def haru_focus(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
            return
        if self._is_ignored(guild.id, interaction.user.id):
            await interaction.response.send_message("この操作は制限されています。", ephemeral=True)
            return
        if member is not None and getattr(member, "bot", False):
            await interaction.response.send_message("Bot はフォーカス対象にできません。", ephemeral=True)
            return

        saved = self.set_guild_focus(guild.id, member.id if member is not None else None)
        label = f"{member.mention} のみ" if saved is not None and member is not None else "VC 全員（ミックス）"
        active_note = "（会話中に即時反映しました）" if guild.id in self.sessions else "（次回会話開始時から適用）"
        await interaction.response.send_message(
            f"フォーカス話者を **{label}** に変更しました。{active_note}",
            ephemeral=True,
        )

    @haru_group.command(name="stop", description="Haru のリアルタイム音声会話を終了します")
    async def haru_stop(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        stopped = await self.stop_guild(guild.id, keep_voice_if_reader=True)
        if not stopped:
            await interaction.followup.send("現在 Haru 会話は行われていません。", ephemeral=True)
            return
        await interaction.followup.send("Haru 会話を終了しました。", ephemeral=True)

    @haru_group.command(name="status", description="Haru 会話の現在の状態を表示します")
    async def haru_status(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        ready = await self.client.ready()
        session = self.sessions.get(guild.id)
        focus_uid = session.focus_user_id if session is not None else self.store.get_focus(guild.id)
        focus_label = f"<@{focus_uid}>" if focus_uid is not None else "VC 全員（ミックス）"
        personas = self.store.list_personas()
        lines = [
            f"Haru サーバ: {'オンライン' if ready else 'オフライン / 準備中'}",
            f"登録済み話者: {len(personas)} 人",
            f"聞き取りフォーカス: {focus_label}",
        ]
        if session is not None:
            lines.insert(0, f"会話中: **{session.persona.get('name', 'Haru')}**（<#{session.voice_channel_id}>）")
        else:
            lines.insert(0, "会話中: なし")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    async def close_all(self) -> None:
        for guild_id in list(self.sessions):
            with contextlib.suppress(Exception):
                await self.stop_guild(guild_id, keep_voice_if_reader=True)
        await self.client.close()
