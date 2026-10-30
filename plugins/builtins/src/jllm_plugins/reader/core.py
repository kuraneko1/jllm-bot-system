import asyncio
import io
import json
import logging
import math
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

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
from jllm_plugins.conversation.kitasan import is_kitasan_trigger
from jllm_plugins.dictionaries.dictionary import transcode_to_wav
from jllm_plugins.ducking import VOICE_RECV_AVAILABLE, voice_recv
from jllm_plugins.imagegen.control import NoImageGenController
from jllm_plugins.imagegen.service import ImageGenError
from jllm_plugins.llm import llm_backend
from jllm_plugins.miq.trigger import is_miq_trigger
from jllm_plugins.playback.player import SpeechPlayer
from jllm_plugins.speech_text import _apply_trailing_speed, _parse_pieces, parse_commands
from jllm_plugins.tts_lifecycle.service import switch_message
from jllm_plugins.tts_router.router import TTSRouter
from jllm_plugins.voice_settings.join_voice import JoinVoiceCache
from jllm_plugins.vram_control.service import NoVramController, VramControlError, vram_summary

from .media import HtmlApprovalView, MediaApprovalView, SoundApprovalView
from .panels import MYTALK_HIDDEN_VOICES, BookmarkView, ForceVoiceView, MyTalkView, RestrictView

logger = logging.getLogger(__name__)
URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
CUSTOM_EMOJI_PATTERN = re.compile(r"<a?:[A-Za-z0-9_]+:\d+>")
COLON_TOKEN_PATTERN = re.compile(r":[^:\s]+:")
SKIP_WORDS = {"s", "ｓ"}
SKIP_PREFIXES = (";", ":")
SKIP_REACTION = "⏩"
KITASAN_DEFAULT = object()
MIQ_DEFAULT = object()
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


def strip_call_suffix(content: str, suffix: str = "jbot") -> str:
    """本文末尾の呼び出し語（既定 ``jbot``）を取り除く。

    大文字小文字は区別しない。英数字が直に続く語尾（例: ``foojbot``）は本文の
    一部とみなして残す。suffix が空なら何もしない。
    """

    suffix = (suffix or "").strip()
    if not suffix:
        return content
    text = content.rstrip()
    if not text.casefold().endswith(suffix.casefold()):
        return content
    head = text[: len(text) - len(suffix)]
    if head and head[-1].isascii() and head[-1].isalnum():
        return content
    return head.rstrip()


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


@dataclass
class ReaderConfig:
    allowed_channel_ids: frozenset[int] = frozenset()
    global_reaction_words: tuple[str, ...] = ()
    default_speed: float = 1.2
    max_message_length: int = 200
    vc_announcement_enabled: bool = True
    vc_announcement_cooldown_seconds: int = 3600
    wav_enabled: bool = True
    read_mode: str = "tts"
    talk_call_suffix: str = "jbot"
    duck_enabled: bool = False
    owner_id: int = 0
    default_speaker: int = 1
    mytalk_hidden_voices: frozenset[str] = MYTALK_HIDDEN_VOICES
    bookmark_reaction: str = ""
    bookmark_notify: bool = True
    tts_switch_user_ids: frozenset[int] = frozenset()
    toggles_path: str = "data/toggles.json"
    require_existing_toggles: bool = False
    dashboard_url: str = "http://127.0.0.1:8765"
    forced_voices_path: str = "data/forced_voices.json"
    user_voices_path: str = "data/user_voices.json"
    vc_announcement_voice: str = "clone こしあん"
    vc_announcement_cache_dir: str = "data/join_voice"
    vc_announcement_cache_limit: int = 128
    vc_announcement_history_path: str = "data/vc_announcements.json"

    def channel_allowed(self, channel_id: int) -> bool:
        return channel_id in self.allowed_channel_ids


class ReaderCog(commands.Cog):
    def __new__(cls, *args, **kwargs):
        self = super().__new__(cls)
        self.config = ReaderConfig()
        self.bot = None
        self.readings = SimpleNamespace(apply=lambda text: text)
        self.media_store = None
        self.media = {}
        self.media_dir = Path("media")
        self.html_dir = Path("html")
        self.toggles = {"img": True, "wav": True}
        return self

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
        html_store=None,
        vram=None,
        imagegen=None,
        imagegen_controller=None,
        kitasan=KITASAN_DEFAULT,
        miq=MIQ_DEFAULT,
    ):
        self.bot = bot
        self.tts = tts
        self.player_factory = player_factory
        self.settings = settings
        self.voice_speeds = settings.speeds
        self.voice_volumes = settings.volumes
        self.dictionary = dictionary
        self.readings = readings
        self.restrictions = restrictions
        self.config = config
        self.bookmarks = bookmarks
        self.media_store = media_store
        self.html_store = html_store
        self.vram = vram or NoVramController()
        self.imagegen = imagegen
        self.imagegen_controller = imagegen_controller or NoImageGenController()
        self.kitasan = kitasan
        self.miq = miq
        self.html_dir = html_store.html_dir if html_store is not None else Path("html")
        self.pending_html = html_store.pending if html_store is not None else {}
        self.html_catalog = html_store.catalog if html_store is not None else []
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
        self.voice_entries = self._load_voice_entries()
        self.toggles = self._load_toggles()

    @property
    def user_voices(self):
        if "settings" not in self.__dict__:
            return self.__dict__.setdefault("_user_voices_override", {})
        return self.settings.user_voices.entries

    @user_voices.setter
    def user_voices(self, value):
        if "settings" not in self.__dict__:
            self.__dict__["_user_voices_override"] = dict(value)
        else:
            self.settings.user_voices.entries.clear()
            self.settings.user_voices.entries.update(value)

    def _load_user_voices(self):
        if "settings" in self.__dict__:
            return self.settings.user_voices.entries
        try:
            data = json.loads(Path(self.config.user_voices_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.user_voices = (
            {str(key): str(value) for key, value in data.items()} if isinstance(data, dict) else {}
        )
        return self.user_voices

    def _save_user_voices(self):
        if "settings" in self.__dict__:
            self.settings.user_voices.save()
            return
        path = Path(self.config.user_voices_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.user_voices, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @property
    def forced_voices(self):
        if "settings" not in self.__dict__:
            return self.__dict__.setdefault("_forced_voices_override", {})
        return self.settings.forced_voices.entries

    @forced_voices.setter
    def forced_voices(self, value):
        if "settings" not in self.__dict__:
            self.__dict__["_forced_voices_override"] = dict(value)
        else:
            self.settings.forced_voices.entries.clear()
            self.settings.forced_voices.entries.update(value)

    def _load_forced_voices(self):
        if "settings" in self.__dict__:
            return self.settings.forced_voices.entries
        try:
            data = json.loads(Path(self.config.forced_voices_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.forced_voices = (
            {str(key): str(value) for key, value in data.items()} if isinstance(data, dict) else {}
        )
        return self.forced_voices

    def _save_forced_voices(self):
        if "settings" in self.__dict__:
            self.settings.forced_voices.save()
            return
        path = Path(self.config.forced_voices_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.forced_voices, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _join_cache(self):
        if "settings" in self.__dict__:
            return self.settings.join_cache
        cache = self.__dict__.get("_join_voice")
        if cache is None or cache.voice != self.config.vc_announcement_voice:
            cache = JoinVoiceCache(
                self.config.vc_announcement_cache_dir,
                voice=self.config.vc_announcement_voice,
                limit=self.config.vc_announcement_cache_limit,
            )
            self.__dict__["_join_voice"] = cache
        return cache

    def _load_voice_entries(self):
        if "settings" in self.__dict__:
            data = self.settings.voice_entries.entries
        else:
            try:
                data = json.loads(
                    Path(self.config.vc_announcement_history_path).read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                data = {}
        if not isinstance(data, dict):
            data = {}
        cutoff = time.time() - self.config.vc_announcement_cooldown_seconds
        self.voice_entries = {
            key: float(value)
            for key, value in data.items()
            if isinstance(key, str) and isinstance(value, (int, float))
            and not isinstance(value, bool) and math.isfinite(value) and value >= cutoff
        }
        if "settings" in self.__dict__:
            self.settings.voice_entries.entries = self.voice_entries
        return self.voice_entries

    def _read_mode(self):
        """読み上げモード（``tts`` / ``wav`` / ``none``）。

        設定で ``wav`` か ``none`` を明示していればそれを優先し、
        それ以外は LLM 側の全体設定（``/llm read``）に従う。
        """

        if self.config.read_mode in ("wav", "none"):
            return self.config.read_mode
        try:
            mode = llm_backend.active_read_mode()
        except Exception:
            return self.config.read_mode
        return mode if mode in ("tts", "wav", "none") else self.config.read_mode

    def _save_voice_history(self):
        if "settings" in self.__dict__:
            self.settings.voice_entries.entries = self.voice_entries
            self.settings.voice_entries.save()
            return
        path = Path(self.config.vc_announcement_history_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.voice_entries, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _user_key(guild_id: int, user_id: int) -> str:
        return f"{guild_id}:{user_id}"

    def _restriction_store(self):
        restrictions = getattr(self, "restrictions", None)
        if restrictions is not None:
            return restrictions
        return getattr(getattr(self, "bot", None), "restrictions", None)

    def _restricted(self, message, feature: str) -> bool:
        restrictions = self._restriction_store()
        if restrictions is None:
            return False
        return restrictions.blocked(message.guild.id, message.author.id, feature)

    async def _flatten_speakers(self):
        try:
            speakers = await self.tts.speakers()
        except TTSBackendError:
            return []
        disabled_value = getattr(self.tts, "disabled_voices", None)
        disabled = (
            set(disabled_value)
            if isinstance(disabled_value, (set, frozenset, list, tuple))
            else set()
        )
        return [
            (style["id"], style["name"])
            for speaker in speakers
            for style in speaker["styles"]
            if style["name"] not in disabled
        ]

    def effective_voice_for(self, guild_id: int, user_id: int):
        key = self._user_key(guild_id, user_id)
        return self.forced_voices.get(key) or self.user_voices.get(key)

    async def _voice_name(self, speaker):
        if speaker is None:
            return speaker
        names = self.__dict__.setdefault("_voice_id_names", {})
        if speaker not in names:
            try:
                voices = await self.tts.list_voices()
            except Exception:
                return speaker
            for voice in voices:
                names[voice.id] = voice.name
        return names.get(speaker, speaker)

    async def tts_speed_for(self, speaker):
        name = await self._voice_name(speaker)
        if "settings" in self.__dict__:
            return self.settings.speeds.get(str(name))
        return self.voice_speeds.get(str(name))

    async def tts_gain_for(self, speaker):
        name = await self._voice_name(speaker)
        if "settings" in self.__dict__:
            return self.settings.volumes.get(str(name))
        return self.voice_volumes.get(str(name))

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

    async def speak_own_response(self, sent, text: str) -> None:
        """会話の応答（Bot 自身の投稿本文）を TTS で読む。

        読むのは ``/join`` した読み上げ対象チャンネルだけで、読み上げモードが
        ``tts`` のときだけ。**会話の応答を投稿した側から明示的に呼ぶ**。
        翻訳結果やコマンド応答を二重に読まないよう、投稿を監視して拾うのではなく
        この入口だけを使う。
        """

        if self._read_mode() != "tts":
            return
        guild = getattr(sent, "guild", None)
        channel = getattr(sent, "channel", None)
        if guild is None or channel is None:
            return
        player = self.players.get(guild.id)
        if (
            player is None
            or channel.id != player.text_channel_id
            or not player.voice_client.is_connected()
        ):
            return
        # 応答は Discord の 1 ページ（最大 1900 字）単位で来るので、そのまま読む。
        text = prepare_text(text, 1900)
        text = filter_emojis(text, player.allow_nonverbal_emojis)
        if not has_readable_text(text):
            return
        text = self.readings.apply(text)
        if not text.strip():
            return
        bot_id = getattr(getattr(self.bot, "user", None), "id", 0)
        # 話者・効果・速度は既定のまま読む。
        player.enqueue(
            text,
            player.speaker_id,
            self.config.default_speed,
            str(getattr(sent, "id", "") or ""),
            self._user_key(guild.id, bot_id),
        )

    def _joined_channel(self, message) -> bool:
        """いま /join している読み上げ対象チャンネルかどうか。"""

        players = getattr(self, "players", None) or {}
        player = players.get(message.guild.id)
        if player is None:
            return False
        return (
            player.voice_client.is_connected()
            and message.channel.id == player.text_channel_id
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot or self._restricted(message, "ignore"):
            return
        kitasan = getattr(self, "kitasan", KITASAN_DEFAULT)
        is_kitasan = (
            is_kitasan_trigger(message.content) if kitasan is KITASAN_DEFAULT
            else kitasan is not None and kitasan.matches_trigger(message.content)
        )
        miq = getattr(self, "miq", MIQ_DEFAULT)
        is_miq = (
            is_miq_trigger(message.content) if miq is MIQ_DEFAULT
            else miq is not None and miq.matches_trigger(message.content)
        )
        if is_miq or is_kitasan:
            return
        # /join したチャンネルは、許可リストに無くても読み上げ対象にする。
        # 入室した場所がそのまま読み上げ先になる（範囲外のチャットは読まない）。
        channel_allowed = self.config.channel_allowed(message.channel.id) or (
            self._joined_channel(message)
        )
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
        if await self._handle_html_registration(message):
            return
        if await self._handle_media_registration(message):
            return
        if self.config.global_reaction_words or self.media_store is not None or self.media:
            await self._send_media(message)
        if "v100" in self.config.global_reaction_words and "v100" in message.content.lower():
            await self._react_v100(message)
        player = self.players.get(message.guild.id)
        if (
            player is None
            or message.channel.id != player.text_channel_id
            or not player.voice_client.is_connected()
        ):
            return
        mode = self._read_mode()
        if mode == "none":
            # 読み上げ無し。テキストも wav もキューに積まない。
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
        # 語尾の呼び出し語（既定 jbot）は読まない。
        read_text = strip_call_suffix(
            tts_message_text(message), self.config.talk_call_suffix
        )
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
        speak = mode == "tts"
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
            or self._read_mode() != "tts"
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
        self._save_voice_history()
        if previous is not None and now - previous <= self.config.vc_announcement_cooldown_seconds:
            return
        name = prepare_text(member.display_name, self.config.max_message_length)
        if not has_readable_text(name):
            name = prepare_text(member.name, self.config.max_message_length)
        name = self.readings.apply(name)
        text = f"{name}さんが入室しました。"
        join_cache = self._join_cache()
        cached = join_cache.get(member.id, text)
        speed = await self.tts_speed_for(join_cache.voice)
        volume = await self.tts_gain_for(join_cache.voice)
        if cached is not None:
            player.enqueue_sound(cached, speed=speed, volume=volume)
            return
        speakers = await self._flatten_speakers()
        if not any(name == join_cache.voice for _, name in speakers):
            logger.warning("入室案内話者が見つかりません: %s", join_cache.voice)
            return
        if self.players.get(member.guild.id) is not player or player.closed:
            return
        created = await join_cache.get_or_create(
            member.id,
            text,
            lambda value: self.tts.synthesize(
                text=value, speaker_id=join_cache.voice, speed=1.0
            ),
        )
        if created is not None:
            player.enqueue_sound(created, speed=speed, volume=volume)
        else:
            player.enqueue(text, join_cache.voice, speed=speed, volume=volume)

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

    @app_commands.command(name="help", description="読み上げの使い方を表示します")
    @app_commands.guild_only()
    async def help(self, interaction):
        await interaction.response.defer(ephemeral=True)
        url = self.config.dashboard_url
        lines = [
            "## 読み上げBotの使い方",
            f"[共通ボード：登録音声・辞書・GIFの閲覧／管理]({url}/)",
            "閲覧はログイン不要。管理操作には**パスキー**が必要です（初回はログイン画面で登録）。",
            "",
            "### 読み上げ",
            f"- {chr(96)}/join{chr(96)} — VCに参加して読み上げ開始",
            "- /leave — 読み上げ停止・VC退出",
            "- /mytalk — 自分の声・ミュートを選択し、**✅ 設定する**で確定",
            "- /forcevoice — メンバーの声を強制変更（オーナーのみ。本人設定より優先）",
            "- /emoji enabled: — 👅💋の読み上げを切り替え（既定は無効）",
            "- 投稿の先頭に ;・: — **その投稿だけ**読み上げない",
            "- s だけ送信 — 再生中の読み上げを停止",
            (
                "- f<数字> で倍速、rb・ec・ecl・ch・vi・lo・bs・pi・rv・lm・wd・ph・ex・kk・tc "
                "で音声効果"
            ),
            "- (この部分だけ)ec3 で括弧内だけに適用。並べると順に重ねて掛かります",
            f"- [実験場]({url}/?tab=playground) — 好きな文章に好きな効果を付けて試せます",
            "- /umigame — ウミガメのスープ（質問は末尾 ??、推測は末尾 !!）",
            "",
            "### 辞書・音声の登録",
            f"- [ボイスレコーダー]({url}/?tab=recorder) — 録音・結合・話者登録申請",
            "- /dict add phrase: file: — 音声を登録。管理者以外は承認申請",
            "- /reading add trigger: reading: — 読み方を登録",
            "- /reading remove trigger: — 登録した読み方を削除",
            "- GIF登録 — Botをメンションし、語句とGIFを添付",
            "",
            "### 画像生成・管理",
            "- /translate — 翻訳・会話設定",
            "- /llm status / /llm switch — 翻訳・会話のモデルを切替。atmos は辞書wavのみ",
            "- /llm read mode: — 読み上げモードだけ変更",
            "- 会話モード：Botへのメンション／リプで質問や要約を依頼",
            "- Web要約 — @Bot URL 要約して",
            "- /img prompt: — 画像を生成",
            "- /wavplay enabled: — 辞書音声再生のオン・オフ",
            "- /imggen enabled: — 画像生成サーバーの起動・停止",
            "- /resettalk — 全員の個別話者設定をリセット",
            "",
            "enabled: は True／False。/wavplay・/imggen は省略すると現在の設定を表示します。",
        ]
        await interaction.followup.send("\n".join(lines)[:1900], ephemeral=True)

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
            converted = await asyncio.to_thread(
                transcode_to_wav, data, suffix, target,
                getattr(self.dictionary, "audio_config", None),
            )
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
        # 旧静的ボードを残すと、辞書の削除済み音声まで公開されるため除去する。
        sounds_dir = self.html_dir / "sounds"
        try:
            (sounds_dir / "index.html").unlink(missing_ok=True)
            if sounds_dir.is_dir() and not any(sounds_dir.iterdir()):
                sounds_dir.rmdir()
            shutil.rmtree(self.html_dir / "audio", ignore_errors=True)
        except OSError:
            logger.exception("旧音声ボードの削除に失敗")

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

    tts_group = app_commands.Group(
        name="tts", description="読み上げエンジン（TTSバックエンド）の状態と切替"
    )
    VRAM_TARGETS = {"vram-free": "free", "vram-restore": "restore"}
    TOGGLE_LABELS = {"img": "画像生成", "wav": "辞書wav"}

    def _tts_router(self):
        return self.tts if isinstance(self.tts, TTSRouter) else None

    def _tts_lines(self, status: dict) -> str:
        lines = []
        for item in status["backends"]:
            marks = []
            if item["active"]:
                marks.append("使用中")
            if item["preferred"]:
                marks.append("優先")
            state = "疎通OK" if item["healthy"] else f"疎通NG（{item['error'] or '応答なし'}）"
            lines.append(
                f"・{item['display']}（{item['id']} / {item['kind']}）… {state}"
                + (f"［{'・'.join(marks)}］" if marks else "")
            )
        return "\n".join(lines)

    async def _tts_owner_only(self, interaction: discord.Interaction) -> bool:
        if self.config.owner_id and interaction.user.id == self.config.owner_id:
            return True
        if interaction.user.id in self.config.tts_switch_user_ids:
            return True
        await interaction.response.send_message("この操作は管理者のみです。", ephemeral=True)
        return False

    @tts_group.command(
        name="status",
        description="いま使っている読み上げエンジンと、各バックエンドの疎通を表示します",
    )
    async def tts_status(self, interaction: discord.Interaction):
        if not await self._tts_owner_only(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        router = self._tts_router()
        if router is None:
            await interaction.followup.send(
                (
                    f"単一のエンジンで動いています: {self.tts.describe()}\n"
                    "複数化するには tts-backends.json を置いてください。"
                ),
                ephemeral=True,
            )
            return
        status = await router.check_all()
        extra = ""
        if getattr(self.vram, "configured", False):
            try:
                extra = "\n\n" + vram_summary(await self.vram.status())
            except VramControlError:
                extra = "\n\nVRAMの状態を取得できませんでした。"
        await interaction.followup.send(
            f"いまのエンジン: {self.tts.describe()}\n\n{self._tts_lines(status)}{extra}",
            ephemeral=True,
        )

    @tts_group.command(
        name="check",
        description="各バックエンドに触って疎通を確かめ直します（優先先が戻れば自動で戻ります）",
    )
    async def tts_check(self, interaction: discord.Interaction):
        if not await self._tts_owner_only(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        router = self._tts_router()
        if router is None:
            await interaction.followup.send("単一のエンジンで動いています。", ephemeral=True)
            return
        status = await router.check_all()
        await interaction.followup.send(
            f"確認しました。いまのエンジン: {self.tts.describe()}\n\n{self._tts_lines(status)}",
            ephemeral=True,
        )

    @staticmethod
    def tts_target(target: str) -> tuple[str, str]:
        value = (target or "").strip()
        if value in ReaderCog.VRAM_TARGETS:
            return "vram", ReaderCog.VRAM_TARGETS[value]
        return "backend", value

    async def _tts_vram(self, interaction: discord.Interaction, action: str):
        if not getattr(self.vram, "configured", False):
            await interaction.followup.send(
                "VRAM開放の設定がありません（VRAM_CONTROL_*）。", ephemeral=True
            )
            return
        try:
            result = await self.vram.free() if action == "free" else await self.vram.restore()
        except VramControlError as exc:
            await interaction.followup.send(f"GPUの操作に失敗しました: {exc}", ephemeral=True)
            return
        logger.info("GPUのVRAM操作 %s: %s", action, vram_summary(result))
        summary = vram_summary(result)
        router = self._tts_router()
        if action == "free":
            note = (
                "GPUのサービスを止めてVRAMを空けました。\n"
                "読み上げは次の確認（最大30秒）で別のエンジンへ自動で退避します。"
                if router is not None
                else "GPUのサービスを止めてVRAMを空けました。読み上げは停止します。"
            )
        else:
            note = "読み上げサーバーを起動しました。次の確認で優先エンジンに戻ります。"
        await interaction.followup.send(f"{note}\n{summary}", ephemeral=True)

    async def _tts_choices(self, interaction: discord.Interaction, current: str):
        router = self._tts_router()
        lowered = current.strip().lower()
        choices = []
        if getattr(self.vram, "configured", False):
            choices.extend(
                [
                    app_commands.Choice(
                        name="VRAMを全開放（GPUのサービスを停止）", value="vram-free"
                    ),
                    app_commands.Choice(
                        name="GPUのサービスを戻す（読み上げを起動）", value="vram-restore"
                    ),
                ]
            )
        if router is not None:
            choices.extend(
                [
                    app_commands.Choice(
                        name=f"{spec.label()}（{spec.id} / {spec.kind}）"[:100], value=spec.id
                    )
                    for spec in router.specs
                ]
            )
        if lowered:
            choices = [
                choice
                for choice in choices
                if lowered in choice.value.lower() or lowered in choice.name.lower()
            ]
        return choices[:25]

    @tts_group.command(
        name="switch", description="読み上げエンジンの切替、またはGPUのVRAM開放・復帰を行います"
    )
    @app_commands.describe(
        backend="切り替え先のバックエンド、またはVRAMを全開放／GPUのサービスを戻す"
    )
    @app_commands.autocomplete(backend=_tts_choices)
    async def tts_switch(self, interaction: discord.Interaction, backend: str):
        if not await self._tts_owner_only(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        kind, value = self.tts_target(backend)
        if kind == "vram":
            await self._tts_vram(interaction, value)
            return
        router = self._tts_router()
        if router is None:
            await interaction.followup.send(
                "単一のエンジンで動いています。tts-backends.json を置くと切り替えられます。",
                ephemeral=True,
            )
            return
        try:
            result = await router.set_active(value)
        except TTSBackendError as exc:
            await interaction.followup.send(f"切り替えできません: {exc}", ephemeral=True)
            return
        note = switch_message(result, self.tts.describe())
        logger.info(
            "TTSバックエンドを %s に切り替え（%s / ライフサイクル %s）",
            result["active"],
            "疎通OK" if result["healthy"] else "疎通NG",
            "あり" if result.get("lifecycle") else "なし",
        )
        await interaction.followup.send(note, ephemeral=True)

    def _load_toggles(self):
        toggles = {"img": True, "wav": self.config.wav_enabled}
        path = Path(self.config.toggles_path)
        if not path.exists():
            if self.config.require_existing_toggles:
                raise RuntimeError("機能トグルの保存ファイルが見つかりません。")
            return toggles
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            if self.config.require_existing_toggles:
                raise RuntimeError("機能トグルの保存ファイルを読み込めません。") from exc
            logger.exception("toggles.json の読み込みに失敗")
            return toggles
        if self.config.require_existing_toggles and (
            not isinstance(data, dict)
            or any(not isinstance(data.get(key), bool) for key in toggles)
        ):
            raise RuntimeError("機能トグルの保存ファイルが不正です。")
        if isinstance(data, dict):
            for key in toggles:
                if key in data:
                    toggles[key] = bool(data[key])
        return toggles

    def _save_toggles(self):
        path = Path(self.config.toggles_path)
        try:
            if path.parent != Path("."):
                path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self.toggles, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            logger.exception("toggles.json の保存に失敗")

    async def _toggle_feature(self, interaction, feature, enabled):
        label = self.TOGGLE_LABELS[feature]
        if interaction.user.id != self.config.owner_id:
            await interaction.response.send_message(
                f"{label}のオン/オフはオーナーのみ変更できます。", ephemeral=True
            )
            return
        if enabled is None:
            state = "オン" if self.toggles[feature] else "オフ"
            await interaction.response.send_message(f"{label}: {state}", ephemeral=True)
            return
        if feature == "img":
            await interaction.response.defer(ephemeral=True)
            async with self.imagegen_controller.lock:
                self.toggles["img"] = False
                self._save_toggles()
                try:
                    await self.imagegen_controller.set_enabled(enabled)
                except ImageGenError as exc:
                    await interaction.followup.send(
                        f"画像生成はオフです。サーバの操作を完了できませんでした: {exc}",
                        ephemeral=True,
                    )
                    return
                self.toggles["img"] = enabled
                self._save_toggles()
            await interaction.followup.send(
                "画像生成サーバを起動しました。画像生成をオンにしました。"
                if enabled
                else (
                    "画像生成をオフにし、画像生成サーバを停止しました。"
                    "画像生成用のVRAMを解放しました。"
                ),
                ephemeral=True,
            )
            return
        self.toggles[feature] = enabled
        self._save_toggles()
        state = "オン" if enabled else "オフ"
        await interaction.response.send_message(f"{label}を{state}にしました。", ephemeral=True)

    @app_commands.command(
        name="img", description="画像を生成します（ポジティブプロンプトのみ入力）"
    )
    @app_commands.describe(prompt="生成したい画像の説明")
    @app_commands.guild_only()
    async def img(self, interaction, prompt: str):
        if not self.toggles["img"]:
            await interaction.response.send_message(
                "画像生成はオフになっています。", ephemeral=True
            )
            return
        await interaction.response.defer()
        if self.imagegen is None:
            await interaction.followup.send("画像生成サーバが設定されていません。")
            return
        try:
            data = await self.imagegen.generate(prompt)
        except ImageGenError as exc:
            await interaction.followup.send(f"画像生成に失敗しました: {exc}")
            return
        except Exception:
            logger.exception("画像生成でError")
            await interaction.followup.send("画像生成でエラーが発生しました。")
            return
        file = discord.File(io.BytesIO(data), filename="anima.png")
        await interaction.followup.send(content=chr(96) + prompt + chr(96), file=file)

    @app_commands.command(
        name="wavplay", description="辞書wav再生のオン/オフを切り替えます（オーナーのみ）"
    )
    @app_commands.describe(enabled="Trueで再生、Falseで停止（未指定時は現在の状態を表示）")
    @app_commands.guild_only()
    async def wavplay(self, interaction, enabled: bool | None = None):
        await self._toggle_feature(interaction, "wav", enabled)

    @app_commands.command(
        name="imggen", description="画像生成サーバを起動・停止します（オーナーのみ）"
    )
    @app_commands.describe(enabled="Trueでサーバ起動、Falseで停止してVRAM解放（未指定時は状態表示）")
    @app_commands.guild_only()
    async def imggen(self, interaction, enabled: bool | None = None):
        await self._toggle_feature(interaction, "img", enabled)

    def _is_approver(self, user_id: int) -> bool:
        approver = (
            self.media_store.config.approver_id
            if self.media_store is not None
            else self.html_store.config.approver_id
            if self.html_store is not None
            else 0
        )
        return not approver or user_id == approver

    def _media_key(self, phrase: str) -> str | None:
        return self.media_store.media_key(phrase) if self.media_store is not None else None

    async def _approve_media(self, request_id: str) -> str:
        return await self.media_store.approve(request_id)

    async def _reject_media(self, request_id: str) -> str:
        return await self.media_store.reject(request_id)

    async def _expire_media(self, request_id: str):
        return await self.media_store.expire(request_id)

    async def _notify_html_oversize(self, message, request_id, size, view):
        approver_id = self.html_store.config.approver_id or self.config.owner_id
        if not approver_id:
            return None
        request = self.pending_html.get(request_id)
        if request is None:
            return None
        try:
            approver = await self.bot.fetch_user(approver_id)
        except Exception:
            logger.exception("承認者の取得に失敗")
            return None
        source = "DM"
        if getattr(message, "guild", None) is not None:
            source = (
                f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}"
            )
        try:
            limit_mb = self.html_store.config.max_bytes // 1024 // 1024
            message = (
                f"HTML公開申請です（制限超過: {size / 1024 / 1024:.1f}MB／上限{limit_mb}MB）。\n"
            )
            return await approver.send(
                f"タイトル: {request['title']}\n"
                f"申請者: {request['display_name']}\n"
                f"申請ID: {request_id}\n"
                f"元メッセージ: {source}\n"
                "下のボタンで公開または拒否してください。",
                view=view,
            )
        except Exception:
            logger.exception("承認者へのDM送信に失敗")
            return None

    def _viewer_wrapper_html(self, title, user_id, site_id, data):
        return self.html_store.viewer_wrapper_html(title, user_id, site_id, data)

    async def _publish_html(self, request_id: str) -> str:
        return self.html_store.publish(request_id)

    async def _approve_html(self, request_id: str) -> str:
        return self.html_store.publish(request_id)

    async def _reject_html(self, request_id: str) -> str:
        return self.html_store.reject(request_id)

    async def _expire_html(self, request_id: str):
        return self.html_store.expire(request_id)

    async def _handle_html_registration(self, message: discord.Message) -> bool:
        if (
            self.html_store is None
            or self.bot.user is None
            or self.bot.user not in message.mentions
        ):
            return False
        attachments = [
            attachment
            for attachment in message.attachments
            if (attachment.filename or "").lower().endswith((".html", ".htm"))
            or attachment.content_type == "text/html"
        ]
        if not attachments:
            return False
        if len(attachments) != 1:
            await message.reply("HTMLは1ファイルずつ添付してください。", mention_author=False)
            return True
        attachment = attachments[0]
        try:
            data = await attachment.read()
        except discord.DiscordException, OSError:
            logger.exception("HTML添付ファイルのダウンロードに失敗")
            await message.reply(
                "HTMLを取得できませんでした。もう一度添付してください。", mention_author=False
            )
            return True
        config = self.html_store.config
        oversize = attachment.size > config.max_bytes or len(data) > config.max_bytes
        request_id = await __import__("asyncio").to_thread(
            self.html_store.register,
            data,
            attachment.filename or "HTML",
            message.author,
        )
        if oversize and not self._is_approver(message.author.id):
            view = HtmlApprovalView(self, request_id)
            sent = await self._notify_html_oversize(message, request_id, len(data), view)
            if sent is not None:
                view.message = sent
                await message.reply(
                    f"HTMLが大きすぎるため（{config.max_bytes // 1024 // 1024}MB以内）、"
                    "管理者の承認待ちです。承認後に公開されます。",
                    mention_author=False,
                )
                return True
            response = await message.reply(
                (
                    f"<@{config.approver_id}> さん、HTML公開申請です"
                    f"（制限超過: {len(data) / 1024 / 1024:.1f}MB）。\n"
                ),
                f"タイトル: {self.pending_html[request_id]['title']}\n"
                f"申請ID: {request_id}\n"
                "下のボタンで公開または拒否してください。",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions(
                    users=True, everyone=False, roles=False, replied_user=False
                ),
                view=view,
            )
            view.message = response
            return True
        if not config.require_approval or self._is_approver(message.author.id):
            result = await self._publish_html(request_id)
            await message.reply(result, mention_author=False)
            return True
        view = HtmlApprovalView(self, request_id)
        request = self.pending_html[request_id]
        response = await message.reply(
            f"<@{config.approver_id}> さん、HTML公開申請です。\n"
            f"タイトル: {request['title']}\n"
            f"申請ID: {request_id}\n"
            "下のボタンで公開または拒否してください。",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions(
                users=True, everyone=False, roles=False, replied_user=False
            ),
            view=view,
        )
        view.message = response
        return True

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
        if (
            getattr(self, "media_store", None) is None
            and not getattr(self, "media", None)
        ) or self._restricted(message, "gif"):
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
        close = getattr(self.imagegen, "close", None)
        if close is not None:
            await close()


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
            talk_call_suffix=ctx.config.get("talk_call_suffix", "jbot"),
            duck_enabled=ctx.config.get("duck_enabled", False),
            owner_id=ctx.config.get("owner_id", 0),
            default_speaker=ctx.config.get("default_speaker", 1),
            mytalk_hidden_voices=frozenset(
                ctx.config.get("mytalk_hidden_voices", [])
            ),
            bookmark_reaction=ctx.config.get("bookmark_reaction", ""),
            bookmark_notify=ctx.config.get("bookmark_notify", True),
            tts_switch_user_ids=frozenset(ctx.config.get("tts_switch_user_ids", [])),
            toggles_path=ctx.config.get("toggles_path", "data/toggles.json"),
            require_existing_toggles=ctx.config.get("require_existing_toggles", False),
            dashboard_url=ctx.config.get("dashboard_url", "http://127.0.0.1:8765"),
            forced_voices_path=ctx.config.get("forced_voices_path", "data/forced_voices.json"),
            user_voices_path=ctx.config.get("user_voices_path", "data/user_voices.json"),
            vc_announcement_voice=ctx.config.get("vc_announcement_voice", "clone こしあん"),
            vc_announcement_cache_dir=ctx.config.get(
                "vc_announcement_cache_dir", "data/join_voice"
            ),
            vc_announcement_cache_limit=ctx.config.get("vc_announcement_cache_limit", 128),
            vc_announcement_history_path=ctx.config.get(
                "vc_announcement_history_path", "data/vc_announcements.json"
            ),
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
            html_store=ctx.dependencies["feature.html-library"],
            vram=ctx.dependencies.get("ops.vram"),
            imagegen=ctx.dependencies.get("imagegen.provider"),
            imagegen_controller=ctx.dependencies.get("imagegen.controller"),
            kitasan=ctx.dependencies.get("feature.kitasan"),
            miq=ctx.dependencies.get("feature.miq"),
        )
        remove = await ctx.dependencies["discord.gateway"].add_cog(self.cog)
        ctx.scope.add(remove)
        return {"feature.reader": self.cog}

    async def start(self):
        pass

    async def stop(self):
        await self.cog.cog_unload()
