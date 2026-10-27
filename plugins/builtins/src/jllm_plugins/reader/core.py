import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from jllm_tts import TTSBackendError

from jllm_plugins.bookmarks import (
    emoji_display,
    emoji_matches,
    excerpt,
    jump_url,
    parse_emoji_spec,
)
from jllm_plugins.dictionaries.dictionary import transcode_to_wav
from jllm_plugins.ducking import VOICE_RECV_AVAILABLE, voice_recv
from jllm_plugins.miq.trigger import is_miq_trigger
from jllm_plugins.playback.player import SpeechPlayer
from jllm_plugins.speech_text import _apply_trailing_speed, _parse_pieces, parse_commands

from .media import MediaApprovalView, SoundApprovalView
from .panels import BookmarkView, ForceVoiceView, MyTalkView, RestrictView

logger = logging.getLogger(__name__)
URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
CUSTOM_EMOJI_PATTERN = re.compile(r"<a?:[A-Za-z0-9_]+:\d+>")
COLON_TOKEN_PATTERN = re.compile(r":[^:\s]+:")
SKIP_WORDS = {"s", "ｓ"}
SKIP_PREFIXES = (";", ":")
SKIP_REACTION = "⏩"
V100_EMOJI_NAMES = ("Teslav100", "v100_gif", "v100_spin")
EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),
    (0x2600, 0x27BF),
    (0x2300, 0x23FF),
    (0x2B00, 0x2BFF),
    (0xFE00, 0xFE0F),
    (0x200D, 0x200D),
    (0x20E3, 0x20E3),
)
NONVERBAL_EMOJIS = frozenset({"👅", "💋"})


def tts_message_text(message: discord.Message) -> str:
    if not re.search(r"<@!?\d+>", message.content):
        return message.clean_content
    users = {user.id: user for user in message.mentions}
    roles = {role.id: role for role in getattr(message, "role_mentions", [])}

    def replace(match):
        kind, identifier = match.group(1), int(match.group(2))
        if kind in ("@", "@!"):
            user = message.guild.get_member(identifier) or users.get(identifier)
            return f"{(user.display_name if user else '不明なユーザー')}さん、"
        if kind == "@&":
            role = message.guild.get_role(identifier) or roles.get(identifier)
            return "@" + (role.name if role else "deleted-role")
        channel = message.guild.get_channel_or_thread(identifier)
        return "#" + (channel.name if channel else "deleted-channel")

    return discord.utils.escape_mentions(re.sub(r"<(@[!&]?|#)(\d+)>", replace, message.content))


def prepare_text(content: str, max_length: int = 200) -> str:
    content = URL_PATTERN.sub("", content)
    content = CUSTOM_EMOJI_PATTERN.sub("", content)
    content = COLON_TOKEN_PATTERN.sub("", content)
    content = " ".join(content.split())
    if len(content) > max_length:
        content = content[:max_length] + "、以下略"
    return content


def _is_emoji(char: str) -> bool:
    code = ord(char)
    if code in (0x2122, 0x2139, 0x24C2, 0x3030, 0x303D, 0x3297, 0x3299):
        return True
    if 0x2934 <= code <= 0x2935:
        return True
    return any(start <= code <= end for start, end in EMOJI_RANGES)


def filter_emojis(content: str, allow_nonverbal: bool = False) -> str:
    if not allow_nonverbal:
        for emoji in NONVERBAL_EMOJIS:
            content = content.replace(emoji, "")
    return content


def has_readable_text(content: str) -> bool:
    text = re.sub(r"[#*0-9]\ufe0f?\u20e3", "", content)
    return any(char.isalnum() and not _is_emoji(char) for char in text)


def media_trigger_match(content: str, trigger: str) -> bool:
    folded = trigger.casefold().strip()
    if not folded:
        return False
    return re.search(r"(?:^|\s)" + re.escape(folded) + r"(?:\s|$)", content.casefold()) is not None


def is_kitasan_trigger(content: str) -> bool:
    return content.strip().casefold() in {"今北産業", "今北", "きたさん"}


def duck_voice_client_class():
    if not VOICE_RECV_AVAILABLE:
        return None
    return voice_recv.VoiceRecvClient


async def connect_voice(channel, cls=None):
    if cls is not None:
        try:
            return await channel.connect(cls=cls)
        except Exception:
            logger.warning("発話を受け取れる接続に失敗しました。通常の接続にします", exc_info=True)
    return await channel.connect()


@dataclass(frozen=True)
class ReaderConfig:
    allowed_channel_ids: frozenset[int] = frozenset()
    global_reaction_words: tuple[str, ...] = ()
    default_speed: float = 1.2
    max_message_length: int = 200
    vc_announcement_enabled: bool = True
    vc_announcement_cooldown_seconds: int = 3600
    wav_enabled: bool = True
    read_mode: str = "tts"
    duck_enabled: bool = False
    owner_id: int = 0
    default_speaker: int = 1
    bookmark_reaction: str = ""
    bookmark_notify: bool = True

    def channel_allowed(self, channel_id: int) -> bool:
        return not self.allowed_channel_ids or channel_id in self.allowed_channel_ids


class ReaderCog(commands.Cog):
    def __init__(
        self,
        bot,
        tts,
        player_factory,
        settings,
        dictionary,
        readings,
        restrictions,
        config,
        bookmarks=None,
        media_store=None,
    ):
        self.bot = bot
        self.tts = tts
        self.player_factory = player_factory
        self.settings = settings
        self.dictionary = dictionary
        self.readings = readings
        self.restrictions = restrictions
        self.config = config
        self.bookmarks = bookmarks
        self.media_store = media_store
        self.media = media_store.media if media_store is not None else {}
        self.pending_media = media_store.pending if media_store is not None else {}
        self.pending_sound = {}
        self.pending_sound_dir = self.dictionary.sounds_dir / "pending"
        self.pending_sound_dir.mkdir(parents=True, exist_ok=True)
        self.media_dir = media_store.media_dir if media_store is not None else Path("media")
        self.pending_media_dir = (
            media_store.pending_dir if media_store is not None else Path("media/pending")
        )
        self.bookmark_rules = parse_emoji_spec(config.bookmark_reaction)
        self.players: dict[int, SpeechPlayer] = {}
        self.voice_entries = settings.voice_entries.entries
        self.toggles = {"wav": config.wav_enabled}

    @property
    def user_voices(self):
        return self.settings.user_voices.entries

    def _save_user_voices(self):
        self.settings.user_voices.save()

    @property
    def forced_voices(self):
        return self.settings.forced_voices.entries

    def _save_forced_voices(self):
        self.settings.forced_voices.save()

    @staticmethod
    def _user_key(guild_id: int, user_id: int) -> str:
        return f"{guild_id}:{user_id}"

    def _restricted(self, message, feature: str) -> bool:
        return self.restrictions.blocked(message.guild.id, message.author.id, feature)

    async def _flatten_speakers(self):
        try:
            speakers = await self.tts.speakers()
        except TTSBackendError:
            return []
        disabled = set(getattr(self.tts, "disabled_voices", None) or set())
        return [
            (style["id"], style["name"])
            for speaker in speakers
            for style in speaker["styles"]
            if style["name"] not in disabled
        ]

    def effective_voice_for(self, guild_id: int, user_id: int):
        key = self._user_key(guild_id, user_id)
        return self.settings.forced_voices.get(key) or self.settings.user_voices.get(key)

    async def tts_speed_for(self, speaker):
        return self.settings.speeds.get(str(speaker))

    async def tts_gain_for(self, speaker):
        return self.settings.volumes.get(str(speaker))

    def _mytalk_embed(self, guild_id: int, user_id: int, speakers):
        key = self._user_key(guild_id, user_id)
        player = self.players.get(guild_id)
        voice_name = self.user_voices.get(key)
        default_id = player.speaker_id if player is not None else self.config.default_speaker
        default_name = next(
            (name for speaker_id, name in speakers if speaker_id == default_id),
            f"ID {default_id}",
        )
        embed = discord.Embed(
            title="🎙️ あなたの読み上げ設定",
            colour=discord.Colour.blurple(),
        )
        embed.add_field(
            name="声（話者）",
            value=voice_name or f"サーバー既定（{default_name}）",
            inline=False,
        )
        if player is None:
            playback_value = "読み上げが開始されていません（先に /join）"
        elif user_id in player.skip_users:
            playback_value = (
                "🔇 あなたの投稿は読み上げられていません\n"
                "ボイスチャンネルを出ると自動で解除されます"
            )
        else:
            playback_value = "✅ 読み上げられています"
        embed.add_field(name="あなたの投稿", value=playback_value, inline=False)
        embed.set_footer(
            text="声を選んで ✅設定する で確定。このパネルは一回限り（次は /mytalk を開き直し）"
        )
        return embed

    @app_commands.command(
        name="mytalk",
        description="自分の読み上げ設定（声 / 自分の投稿を読み上げない）をパネルで変更",
    )
    @app_commands.guild_only()
    async def mytalk(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            return
        await interaction.response.defer(ephemeral=True)
        speakers = await self._flatten_speakers()
        view = MyTalkView(self, guild_id=guild.id, user_id=interaction.user.id, speakers=speakers)
        message = await interaction.followup.send(
            embed=self._mytalk_embed(guild.id, interaction.user.id, speakers),
            view=view,
            ephemeral=True,
        )
        view.message = message

    @app_commands.command(
        name="forcevoice", description="メンバーの声を強制変更します（オーナーのみ）"
    )
    @app_commands.guild_only()
    async def forcevoice(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            return
        if not self.config.owner_id or interaction.user.id != self.config.owner_id:
            await interaction.response.send_message(
                "このコマンドはオーナーだけが使えます。", ephemeral=True
            )
            return
        members = [
            member
            for channel in (*guild.voice_channels, *getattr(guild, "stage_channels", []))
            for member in channel.members
            if not member.bot
        ]
        known = {member.id for member in members}
        channel = getattr(interaction, "channel", None)
        if channel is not None:
            try:
                async for message in channel.history(limit=20):
                    author = getattr(message, "author", None)
                    if (
                        author is None
                        or getattr(author, "bot", False)
                        or getattr(message, "webhook_id", None)
                        or author.id in known
                    ):
                        continue
                    members.append(author)
                    known.add(author.id)
            except discord.HTTPException, AttributeError:
                logger.warning("forcevoiceの直近投稿者を取得できません")
        if not members:
            await interaction.response.send_message(
                "VC参加者も直近の投稿者もいません。", ephemeral=True
            )
            return
        speakers = await self._flatten_speakers()
        if not speakers:
            await interaction.response.send_message(
                "TTSの話者一覧を取得できません。接続設定を確認してください。", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        view = ForceVoiceView(
            self, guild_id=guild.id, owner_id=interaction.user.id, speakers=speakers
        )
        message = await interaction.followup.send(
            embed=view.embed(),
            view=view,
            ephemeral=True,
            wait=True,
        )
        view.message = message

    @app_commands.command(
        name="resettalk", description="このサーバーの全員の話者設定をリセットします"
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def resettalk(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            return
        member = interaction.user
        if not (isinstance(member, discord.Member) and member.guild_permissions.manage_guild):
            await interaction.response.send_message("サーバー管理権限が必要です。", ephemeral=True)
            return
        prefix = f"{guild.id}:"
        removed = [key for key in self.user_voices if key.startswith(prefix)]
        for key in removed:
            del self.user_voices[key]
        if removed:
            self._save_user_voices()
        player = self.players.get(guild.id)
        if player is not None:
            player.speaker_id = self.config.default_speaker
        await interaction.response.send_message(
            f"全員の話者設定をリセットしました（{len(removed)}件）。",
            ephemeral=True,
        )

    @app_commands.command(
        name="restrict",
        description="特定のユーザーのwav/GIF再生や、まるごと無視を設定します（オーナーのみ）",
    )
    @app_commands.guild_only()
    async def restrict(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            return
        if not self.config.owner_id or interaction.user.id != self.config.owner_id:
            await interaction.response.send_message(
                "このコマンドはオーナーだけが使えます。", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        view = RestrictView(self, guild_id=guild.id, owner_id=interaction.user.id)
        message = await interaction.followup.send(
            embed=view.embed(),
            view=view,
            ephemeral=True,
            wait=True,
        )
        view.message = message

    @app_commands.command(name="join", description="ボイスチャンネルに参加して読み上げを開始します")
    @app_commands.guild_only()
    async def join(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            return
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "サーバー内で実行してください。", ephemeral=True
            )
            return
        if member.voice is None or member.voice.channel is None:
            await interaction.response.send_message(
                "先にボイスチャンネルに参加してください。", ephemeral=True
            )
            return
        channel = member.voice.channel
        await interaction.response.defer(ephemeral=True)
        player = self.players.get(guild.id)
        try:
            if player is not None:
                voice_client = player.voice_client
                if voice_client.is_connected():
                    if voice_client.channel != channel:
                        await voice_client.move_to(channel)
                    player.text_channel_id = interaction.channel_id
                    await interaction.followup.send(
                        "読み上げチャンネルを設定しました。", ephemeral=True
                    )
                    return
                self.players.pop(guild.id, None)
                await player.close()
            voice_client = guild.voice_client
            recv_cls = duck_voice_client_class() if self.config.duck_enabled else None
            if voice_client is not None:
                if voice_client.is_connected():
                    if recv_cls is not None and not isinstance(voice_client, recv_cls):
                        await voice_client.disconnect(force=True)
                        voice_client = await connect_voice(channel, recv_cls)
                    else:
                        await voice_client.move_to(channel)
                else:
                    await voice_client.disconnect(force=True)
                    voice_client = await connect_voice(channel, recv_cls)
            else:
                voice_client = await connect_voice(channel, recv_cls)
            player = self.player_factory.create(voice_client, self.tts, interaction.channel_id)
            await player.enable_listening()
            self.players[guild.id] = player
            await interaction.followup.send(
                f"{channel.mention} に参加しました。\nこのテキストチャンネルの投稿を読み上げます。",
                ephemeral=True,
            )
        except discord.DiscordException, OSError:
            logger.exception("ボイスチャンネルへの参加に失敗しました。")
            await interaction.followup.send(
                "参加できませんでした。Botの接続・発言権限を確認してください。", ephemeral=True
            )

    @app_commands.command(name="leave", description="読み上げを停止して退出します")
    @app_commands.guild_only()
    async def leave(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            return
        player = self.players.pop(guild.id, None)
        if player is None:
            await interaction.response.send_message(
                "現在、読み上げを行っていません。", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        await player.close()
        await interaction.followup.send("読み上げを停止して退出しました。", ephemeral=True)

    @app_commands.command(
        name="emoji", description="👅💋 の非言語音絵文字を有効/無効にします（デフォルト無効）"
    )
    @app_commands.guild_only()
    @app_commands.describe(enabled="Trueで👅💋を有効化、Falseで除去（デフォルト）")
    async def emoji(self, interaction: discord.Interaction, enabled: bool):
        guild = interaction.guild
        if guild is None:
            return
        player = self.players.get(guild.id)
        if player is None:
            await interaction.response.send_message(
                "先に /join で読み上げを開始してください。", ephemeral=True
            )
            return
        player.allow_nonverbal_emojis = enabled
        message = (
            "👅💋 を有効にしました（演出に使われます）。"
            if enabled
            else "👅💋 を無効にしました（読み上げから除去）。"
        )
        await interaction.response.send_message(message, ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot or self._restricted(message, "ignore"):
            return
        if is_miq_trigger(message.content) or is_kitasan_trigger(message.content):
            return
        channel_allowed = self.config.channel_allowed(message.channel.id)
        content = message.content.casefold()
        if not channel_allowed:
            if not any(
                media_trigger_match(content, word) for word in self.config.global_reaction_words
            ):
                return
            await self._send_media(message, global_only=True)
            if "v100" in self.config.global_reaction_words and "v100" in content:
                await self._react_v100(message)
            return
        if await self._handle_media_registration(message):
            return
        await self._send_media(message)
        if "v100" in message.content.lower():
            await self._react_v100(message)
        player = self.players.get(message.guild.id)
        if (
            player is None
            or message.channel.id != player.text_channel_id
            or not player.voice_client.is_connected()
        ):
            return
        if message.content.startswith(SKIP_PREFIXES):
            return
        bot_user = getattr(self.bot, "user", None)
        if bot_user and re.search(rf"<@!?{bot_user.id}>", message.content):
            return
        if message.content.strip().lower() in SKIP_WORDS:
            if player.skip_current_post():
                await self._react_skip(message)
            return
        read_text = tts_message_text(message)
        command = parse_commands(read_text)
        post_id = str(message.id)
        author_key = self._user_key(message.guild.id, message.author.id)
        trailing_speed = None
        stop_requested = False
        if command is not None:
            base, trailing_speed, stop_requested = command
            read_text = base
            if not has_readable_text(prepare_text(base, self.config.max_message_length)):
                return
            pieces = _parse_pieces(base, self.config.default_speed)
            pieces = _apply_trailing_speed(pieces, trailing_speed)
            sounds = [
                segment.value
                for chunk, _, _ in pieces
                for segment in self.dictionary.split(chunk)
                if segment.kind == "sound"
            ]
            if sounds and self.toggles["wav"] and not self._restricted(message, "wav"):
                if stop_requested:
                    player.skip()
                    await self._react_skip(message)
                for chunk, chunk_speed, chunk_effects in pieces:
                    for segment in self.dictionary.split(chunk):
                        if segment.kind == "sound":
                            player.enqueue_sound(
                                segment.value,
                                chunk_speed,
                                self.dictionary.volume_of_file(Path(segment.value).name),
                                post_id,
                                author_key,
                                effects=chunk_effects,
                            )
                return
        if message.author.id in player.skip_users:
            return
        text = prepare_text(read_text, self.config.max_message_length)
        text = filter_emojis(text, player.allow_nonverbal_emojis)
        if not has_readable_text(text):
            return
        text = self.readings.apply(text)
        if not text.strip():
            return
        speaker = self.effective_voice_for(message.guild.id, message.author.id)
        speaker_key = speaker if speaker is not None else player.speaker_id
        voice_speed = await self.tts_speed_for(speaker_key)
        base_speed = voice_speed if voice_speed is not None else self.config.default_speed
        pieces = _apply_trailing_speed(_parse_pieces(text, base_speed), trailing_speed)
        if not pieces:
            pieces = [(text, base_speed, ())]
        if stop_requested:
            player.skip()
            await self._react_skip(message)
        gain = await self.tts_gain_for(speaker_key)
        wav_blocked = self._restricted(message, "wav")
        speak = self.config.read_mode != "wav"
        for chunk, chunk_speed, chunk_effects in pieces:
            for segment in self.dictionary.split(chunk):
                if segment.kind == "sound":
                    if self.toggles["wav"] and not wav_blocked:
                        player.enqueue_sound(
                            segment.value,
                            chunk_speed,
                            self.dictionary.volume_of_file(Path(segment.value).name),
                            post_id,
                            author_key,
                            effects=chunk_effects,
                        )
                    elif speak and has_readable_text(segment.text):
                        player.enqueue(
                            segment.text,
                            speaker,
                            chunk_speed,
                            post_id,
                            author_key,
                            volume=gain,
                            effects=chunk_effects,
                        )
                elif speak and has_readable_text(segment.value):
                    player.enqueue(
                        segment.value,
                        speaker,
                        chunk_speed,
                        post_id,
                        author_key,
                        volume=gain,
                        effects=chunk_effects,
                    )

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot:
            return
        player = self.players.get(member.guild.id)
        if player is None or player.voice_client.channel is None:
            return
        before_id = before.channel.id if before.channel else None
        after_id = after.channel.id if after.channel else None
        if before_id == after_id:
            return
        if before_id == player.voice_client.channel.id and member.id in player.skip_users:
            player.skip_users.discard(member.id)
        if (
            not self.config.vc_announcement_enabled
            or after_id != player.voice_client.channel.id
            or player.closed
            or not player.voice_client.is_connected()
            or self.config.read_mode == "wav"
        ):
            return
        now = time.time()
        key = self._user_key(member.guild.id, member.id)
        previous = self.voice_entries.get(key)
        self.voice_entries = {
            entry: timestamp
            for entry, timestamp in self.voice_entries.items()
            if timestamp >= now - self.config.vc_announcement_cooldown_seconds
        }
        self.voice_entries[key] = now
        self.settings.voice_entries.entries = self.voice_entries
        self.settings.voice_entries.save()
        if previous is not None and now - previous <= self.config.vc_announcement_cooldown_seconds:
            return
        name = prepare_text(member.display_name, self.config.max_message_length)
        if not has_readable_text(name):
            name = prepare_text(member.name, self.config.max_message_length)
        name = self.readings.apply(name)
        text = f"{name}さんが入室しました。"
        cached = self.settings.join_cache.get(member.id, text)
        speed = await self.tts_speed_for(self.settings.join_cache.voice)
        volume = await self.tts_gain_for(self.settings.join_cache.voice)
        if cached is not None:
            player.enqueue_sound(cached, speed=speed, volume=volume)
            return
        speakers = await self._flatten_speakers()
        if not any(name == self.settings.join_cache.voice for _, name in speakers):
            logger.warning("入室案内話者が見つかりません: %s", self.settings.join_cache.voice)
            return
        if self.players.get(member.guild.id) is not player or player.closed:
            return
        created = await self.settings.join_cache.get_or_create(
            member.id,
            text,
            lambda value: self.tts.synthesize(
                text=value, speaker_id=self.settings.join_cache.voice, speed=1.0
            ),
        )
        if created is not None:
            player.enqueue_sound(created, speed=speed, volume=volume)
        else:
            player.enqueue(text, self.settings.join_cache.voice, speed=speed, volume=volume)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if self._is_bookmark_reaction(payload):
            await self._add_bookmark(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        if self._is_bookmark_reaction(payload):
            self.bookmarks.remove(payload.user_id, payload.message_id)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload):
        if self.bookmark_rules:
            self.bookmarks.remove_message(payload.message_id)

    def _is_bookmark_reaction(self, payload) -> bool:
        if (
            not self.bookmark_rules
            or not self.config.owner_id
            or payload.user_id != self.config.owner_id
        ):
            return False
        if payload.guild_id is None:
            return False
        return emoji_matches(payload.emoji, self.bookmark_rules)

    async def _add_bookmark(self, payload):
        channel = self.bot.get_channel(payload.channel_id) or await self._fetch_channel(
            payload.channel_id
        )
        if channel is None:
            return
        message = await self._fetch_message(channel, payload.message_id)
        excerpt_text = ""
        if message is not None:
            excerpt_text = excerpt(message.content)
            if not excerpt_text:
                if message.attachments:
                    excerpt_text = "（添付ファイル）"
                elif message.embeds:
                    excerpt_text = "（埋め込み）"
                else:
                    excerpt_text = "（本文なし）"
        author = ""
        if message is not None and message.author is not None:
            author = getattr(message.author, "display_name", None) or message.author.name
        item = {
            "message_id": str(payload.message_id),
            "channel_id": str(payload.channel_id),
            "guild_id": str(payload.guild_id),
            "channel": getattr(channel, "name", "") or "",
            "author": author,
            "excerpt": excerpt_text,
            "added_at": datetime.now(UTC).isoformat(),
        }
        count = self.bookmarks.add(payload.user_id, self._bookmark_user_name(payload), item)
        if not self.config.bookmark_notify:
            return
        user = self.bot.get_user(payload.user_id) or await self._fetch_user(payload.user_id)
        if user is None:
            return
        try:
            await user.send(f"{payload.emoji} 保存しました（{count}件）\n{jump_url(item)}")
        except discord.HTTPException:
            logger.info("ブックマークのDM通知を送れませんでした: %s", payload.user_id)

    def _bookmark_user_name(self, payload) -> str:
        member = getattr(payload, "member", None)
        if member is not None:
            return getattr(member, "display_name", None) or member.name
        user = self.bot.get_user(payload.user_id)
        return getattr(user, "display_name", "") if user else ""

    async def _fetch_channel(self, channel_id: int):
        try:
            return await self.bot.fetch_channel(channel_id)
        except discord.HTTPException, discord.NotFound:
            logger.info("ブックマーク対象のチャンネルを取得できません: %s", channel_id)
            return None

    async def _fetch_message(self, channel, message_id: int):
        try:
            return await channel.fetch_message(message_id)
        except discord.HTTPException, discord.NotFound:
            logger.info("ブックマーク対象の本文を取得できません: %s", message_id)
            return None

    async def _fetch_user(self, user_id: int):
        try:
            return await self.bot.fetch_user(user_id)
        except discord.HTTPException:
            return None

    @app_commands.command(
        name="bookmark", description="リアクションで保存した投稿の一覧を表示します（オーナーのみ）"
    )
    @app_commands.guild_only()
    async def bookmark(self, interaction: discord.Interaction):
        if not self.config.owner_id or interaction.user.id != self.config.owner_id:
            await interaction.response.send_message(
                "ブックマークを開けるのはBotオーナーだけです。", ephemeral=True
            )
            return
        if not self.bookmark_rules:
            await interaction.response.send_message(
                "ブックマークの絵文字が設定されていません（`BOOKMARK_REACTION`）。",
                ephemeral=True,
            )
            return
        items = self.bookmarks.items(interaction.user.id)
        if not items:
            reaction = emoji_display(self.config.bookmark_reaction) or "設定した絵文字"
            await interaction.response.send_message(
                f"ブックマークはまだありません。投稿に {reaction} を押すと保存されます。",
                ephemeral=True,
            )
            return
        view = BookmarkView(self, interaction.user.id, items)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    dict_group = app_commands.Group(name="dict", description="特定語句を登録wavに差し替える辞書")

    @dict_group.command(name="add", description="語句に音声ファイルを登録します")
    @app_commands.describe(phrase="読み上げ文中の語句", file="差し替える音声ファイル（wav/mp3等）")
    async def dict_add(
        self,
        interaction: discord.Interaction,
        phrase: str,
        file: discord.Attachment,
    ):
        await interaction.response.defer(ephemeral=True)
        phrase = " ".join(phrase.split())
        if not phrase or len(phrase) > 80:
            await interaction.followup.send(
                "登録語句は1〜80文字で指定してください。",
                ephemeral=True,
            )
            return
        if not self.config.owner_id:
            await interaction.followup.send(
                "登録管理者が未設定のため、音声を登録できません。",
                ephemeral=True,
            )
            return
        request_id = uuid.uuid4().hex
        target = self.pending_sound_dir / f"{request_id}.wav"
        suffix = Path(file.filename or "").suffix.lower() or ".wav"
        try:
            data = await file.read()
            converted = await asyncio.to_thread(transcode_to_wav, data, suffix, target)
        except Exception:
            logger.exception("音声登録ファイルの読み込み・変換に失敗")
            converted = False
        if not converted:
            target.unlink(missing_ok=True)
            await interaction.followup.send(
                "音声の読み込み・変換に失敗しました。もう一度添付してください。",
                ephemeral=True,
            )
            return
        self.pending_sound[request_id] = {
            "path": str(target),
            "phrase": phrase,
            "requester_id": interaction.user.id,
            "author": interaction.user.display_name,
        }
        if interaction.user.id == self.config.owner_id:
            result = await self._approve_sound(request_id)
            await interaction.followup.send(result, ephemeral=True)
            return
        view = SoundApprovalView(self, request_id)
        try:
            from contextlib import closing

            with closing(discord.File(target, filename="preview.wav")) as preview:
                view.message = await interaction.channel.send(
                    content=(
                        f"<@{self.config.owner_id}> さん、音声登録申請です。\n"
                        f"申請者: <@{interaction.user.id}>\n"
                        f"語句: {discord.utils.escape_markdown(phrase)}\n"
                        f"申請ID: `{request_id}`\n"
                        "添付音声を確認して、下のボタンで許可または拒否してください。"
                    ),
                    file=preview,
                    view=view,
                    allowed_mentions=discord.AllowedMentions(
                        users=[discord.Object(id=self.config.owner_id)],
                        everyone=False,
                        roles=False,
                        replied_user=False,
                    ),
                )
        except Exception:
            logger.exception("音声登録申請の送信に失敗")
            await self._expire_sound(request_id)
            view.stop()
            await interaction.followup.send(
                "申請を送信できませんでした。時間を置いて再申請してください。",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            "管理者へ音声登録を申請しました。承認後に登録されます（申請は10分で期限切れ）。",
            ephemeral=True,
        )

    def _rebuild_sound_dashboard(self):
        # Web ダッシュボードの公開物は後続の web.host 側で生成する。
        return None

    async def _approve_sound(self, request_id: str) -> str:
        request = self.pending_sound.pop(request_id, None)
        if request is None:
            return "この音声登録申請は期限切れです。"
        source = Path(request["path"])
        if not await asyncio.to_thread(source.is_file):
            return "登録用音声が見つかりません。再申請してください。"
        target = self.dictionary.sounds_dir / f"sound-{request_id}.wav"
        try:
            await asyncio.to_thread(source.replace, target)
            self.dictionary.add(request["phrase"], target.name, author=request["author"])
        except Exception:
            logger.exception("承認済み音声の登録に失敗")
            await asyncio.to_thread(source.unlink, missing_ok=True)
            return "音声の登録に失敗しました。再申請してください。"
        self._rebuild_sound_dashboard()
        phrase = discord.utils.escape_markdown(request["phrase"])
        return f"「{phrase}」の音声を登録しました。"

    async def _reject_sound(self, request_id: str) -> str:
        await self._expire_sound(request_id)
        return "音声登録申請を拒否しました。"

    async def _expire_sound(self, request_id: str):
        request = self.pending_sound.pop(request_id, None)
        if request is not None:
            await asyncio.to_thread(Path(request["path"]).unlink, missing_ok=True)

    reading_group = app_commands.Group(name="reading", description="文字列の読み（カナ）置換辞書")

    @reading_group.command(name="add", description="文字列の読みを登録します")
    @app_commands.describe(
        trigger="置換する文字列（例: DGXspark）",
        reading="読み（例: でぃーじーえっくす）",
    )
    async def reading_add(self, interaction: discord.Interaction, trigger: str, reading: str):
        await interaction.response.defer(ephemeral=True)
        self.readings.add(trigger, reading, author=interaction.user.display_name)
        await interaction.followup.send(
            f"「{trigger}」→「{reading}」を登録しました。",
            ephemeral=True,
        )

    @reading_group.command(name="remove", description="読み辞書の登録を削除します")
    async def reading_remove(self, interaction: discord.Interaction, trigger: str):
        await interaction.response.defer(ephemeral=True)
        message = (
            f"「{trigger}」を削除しました。"
            if self.readings.remove(trigger)
            else "その文字列は登録されていません。"
        )
        await interaction.followup.send(message, ephemeral=True)

    def _is_approver(self, user_id: int) -> bool:
        approver = self.media_store.config.approver_id if self.media_store is not None else 0
        return not approver or user_id == approver

    def _media_key(self, phrase: str) -> str | None:
        return self.media_store.media_key(phrase) if self.media_store is not None else None

    async def _approve_media(self, request_id: str) -> str:
        return await self.media_store.approve(request_id)

    async def _reject_media(self, request_id: str) -> str:
        return await self.media_store.reject(request_id)

    async def _expire_media(self, request_id: str):
        return await self.media_store.expire(request_id)

    async def _handle_media_registration(self, message: discord.Message) -> bool:
        if (
            self.media_store is None
            or self.bot.user is None
            or self.bot.user not in message.mentions
        ):
            return False
        attachments = [
            attachment
            for attachment in message.attachments
            if attachment.content_type == "image/gif"
            or (attachment.filename or "").lower().endswith(".gif")
        ]
        if len(attachments) != 1:
            return False
        phrase = re.sub(rf"<@!?{self.bot.user.id}>", "", message.content)
        phrase = " ".join(phrase.split())
        if not phrase:
            await message.reply(
                "登録する語句をメンションの後ろに書いてください。", mention_author=False
            )
            return True
        if len(phrase) > 80:
            await message.reply("登録語句が長すぎます（80文字以内）。", mention_author=False)
            return True
        attachment = attachments[0]
        if attachment.size > self.media_store.config.max_bytes:
            await message.reply("GIFが大きすぎます（10MB以内）。", mention_author=False)
            return True
        data = await attachment.read()
        if not data.startswith((b"GIF87a", b"GIF89a")):
            await message.reply("GIFファイルとして読み込めませんでした。", mention_author=False)
            return True
        request_id = await __import__("asyncio").to_thread(
            self.media_store.register,
            phrase,
            data,
            message.author.id,
            message.channel.id,
            message.id,
        )
        existing = self._media_key(phrase)
        if self._is_approver(message.author.id):
            result = await self._approve_media(request_id)
            await message.reply(result, mention_author=False)
            return True
        view = MediaApprovalView(self, request_id)
        suffix = "（既存登録を上書きします）" if existing else ""
        approver = self.media_store.config.approver_id
        response = await message.reply(
            (
                f"<@{approver}> さん、メディア登録申請です。\n"
                f"語句: `{phrase}` {suffix}\n"
                f"申請ID: `{request_id}`\n"
                "下のボタンで許可または拒否してください。"
            ),
            mention_author=False,
            allowed_mentions=discord.AllowedMentions(
                users=True,
                everyone=False,
                roles=False,
                replied_user=False,
            ),
            view=view,
        )
        view.message = response
        return True

    async def _send_media(self, message: discord.Message, *, global_only: bool = False):
        if self.media_store is None or self._restricted(message, "gif"):
            return
        content = URL_PATTERN.sub("", message.content).casefold()
        for trigger, filename in self.media.items():
            if global_only and trigger.casefold() not in self.config.global_reaction_words:
                continue
            if not media_trigger_match(content, trigger):
                continue
            path = self.media_dir / filename
            if not path.is_file():
                logger.error("メディアファイルがありません: %s", path)
                continue
            try:
                await message.channel.send(file=discord.File(path, filename=path.name))
            except discord.DiscordException, OSError:
                logger.exception("メディア送信に失敗: %s", path)

    @staticmethod
    async def _react_skip(message):
        try:
            await message.add_reaction(SKIP_REACTION)
        except discord.HTTPException:
            logger.exception("スキップのリアクションに失敗")

    @staticmethod
    async def _react_v100(message):
        for emoji_name in V100_EMOJI_NAMES:
            emoji = discord.utils.get(message.guild.emojis, name=emoji_name)
            if emoji is not None:
                try:
                    await message.add_reaction(emoji)
                except discord.HTTPException:
                    logger.exception("V100リアクションに失敗: %s", emoji_name)

    async def cog_unload(self):
        await asyncio.gather(
            *(player.close() for player in self.players.values()), return_exceptions=True
        )
        self.players.clear()


class Plugin:
    async def setup(self, ctx):
        config = ReaderConfig(
            allowed_channel_ids=frozenset(ctx.config.get("allowed_channel_ids", [])),
            global_reaction_words=tuple(ctx.config.get("global_reaction_words", [])),
            default_speed=ctx.config.get("default_speed", 1.2),
            max_message_length=ctx.config.get("max_message_length", 200),
            vc_announcement_enabled=ctx.config.get("vc_announcement_enabled", True),
            vc_announcement_cooldown_seconds=ctx.config.get(
                "vc_announcement_cooldown_seconds", 3600
            ),
            wav_enabled=ctx.config.get("wav_enabled", True),
            read_mode=ctx.config.get("read_mode", "tts"),
            duck_enabled=ctx.config.get("duck_enabled", False),
            owner_id=ctx.config.get("owner_id", 0),
            default_speaker=ctx.config.get("default_speaker", 1),
            bookmark_reaction=ctx.config.get("bookmark_reaction", ""),
            bookmark_notify=ctx.config.get("bookmark_notify", True),
        )
        self.cog = ReaderCog(
            ctx.dependencies["discord.gateway"].bot,
            ctx.dependencies["tts.synthesis"],
            ctx.dependencies["speech.playback"],
            ctx.dependencies["voice.settings"],
            ctx.dependencies["dictionary.sound"],
            ctx.dependencies["dictionary.reading"],
            ctx.dependencies["policy.restrictions"],
            config,
            bookmarks=ctx.dependencies["feature.bookmarks"],
            media_store=ctx.dependencies["dictionary.media"],
        )
        remove = await ctx.dependencies["discord.gateway"].add_cog(self.cog)
        ctx.scope.add(remove)
        return {}

    async def start(self):
        pass

    async def stop(self):
        await self.cog.cog_unload()
