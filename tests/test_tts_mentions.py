# ruff: noqa: E501
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from jllm_plugins.reader.core import ReaderCog
from jllm_plugins.voice_settings.speeds import VoiceSpeeds
from jllm_plugins.voice_settings.volumes import VoiceVolumes


class TTSMentionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot_user = SimpleNamespace(id=99, display_name='jllm-voice')
        self.user = SimpleNamespace(id=30, display_name='こしあん')
        self.guild = SimpleNamespace(
            id=1, get_member=lambda uid: None,
            get_role=lambda uid: SimpleNamespace(name='参加者'),
            get_channel_or_thread=lambda uid: SimpleNamespace(name='雑談'),
        )
        self.player = SimpleNamespace(
            voice_client=SimpleNamespace(is_connected=lambda: True),
            text_channel_id=123, skip_users=set(), allow_nonverbal_emojis=False,
            speaker_id=1,
            enqueue=Mock(), enqueue_sound=Mock(), skip=Mock(),
        )
        self.cog = ReaderCog.__new__(ReaderCog)
        self.cog.bot = SimpleNamespace(user=self.bot_user)
        self.cog.players = {1: self.player}
        self.cog._handle_html_registration = AsyncMock(return_value=False)
        self.cog._handle_media_registration = AsyncMock(return_value=False)
        self.cog._send_media = AsyncMock()
        self.cog._react_skip = AsyncMock()
        self.cog.dictionary = SimpleNamespace(
            split=lambda text: [SimpleNamespace(kind='text', value=text)],
        )
        self.cog.readings = SimpleNamespace(apply=lambda text: text)
        self.cog.user_voices = {}
        self.cog.forced_voices = {}
        self.cog.voice_volumes = VoiceVolumes()
        self.cog.voice_speeds = VoiceSpeeds()
        self.cog._voice_id_names = {}
        self.cog.tts = SimpleNamespace(list_voices=AsyncMock(return_value=[]))

    async def send(self, text, mentions=(), reply=False, clean=None):
        message = SimpleNamespace(
            id=21,
            guild=self.guild, author=SimpleNamespace(id=2, bot=False),
            channel=SimpleNamespace(id=123), content=text,
            clean_content=clean or text, mentions=list(mentions), role_mentions=[],
            reference=SimpleNamespace(message_id=10) if reply else None,
        )
        with patch.object(self.cog.config, 'allowed_channel_ids', {123}):
            await self.cog.on_message(message)

    async def test_bot_mention_skips_entire_message_without_interrupting(self):
        for text in ('<@99> 要約して', '前の文章 <@!99> 続き', '<@30> <@99> 音声 s'):
            await self.send(text, [self.bot_user, self.user])
        self.player.enqueue.assert_not_called()
        self.player.enqueue_sound.assert_not_called()
        self.player.skip.assert_not_called()
        self.cog._react_skip.assert_not_awaited()

    async def test_user_mentions_use_display_name_and_honorific(self):
        await self.send('<@30> こんにちは', [self.user], clean='@こしあん こんにちは')
        self.player.enqueue.assert_called_once_with('こしあんさん、 こんにちは', None, 1.2, '21', '1:2', volume=1.0, effects=())

    async def test_nickname_mentions_and_multiple_users(self):
        second = SimpleNamespace(id=40, display_name='Ryu')
        await self.send('<@!30>と<@40> ありがとう', [self.user, second])
        self.player.enqueue.assert_called_once_with('こしあんさん、とRyuさん、 ありがとう', None, 1.2, '21', '1:2', volume=1.0, effects=())

    async def test_reply_ping_does_not_add_user_or_bot_name(self):
        for target in (self.user, self.bot_user):
            await self.send('続きの文章', [target], reply=True)
        self.assertEqual(self.player.enqueue.call_count, 2)
        self.player.enqueue.assert_called_with('続きの文章', None, 1.2, '21', '1:2', volume=1.0, effects=())

    async def test_explicit_mention_in_reply_still_converts(self):
        await self.send('<@30> 続き', [self.user], reply=True)
        self.player.enqueue.assert_called_once_with('こしあんさん、 続き', None, 1.2, '21', '1:2', volume=1.0, effects=())

    async def test_roles_channels_and_plain_at_name_preserved(self):
        await self.send('<@30> <@&50> <#60> @こしあん', [self.user])
        self.player.enqueue.assert_called_once_with('こしあんさん、 @参加者 #雑談 @こしあん', None, 1.2, '21', '1:2', volume=1.0, effects=())

    async def test_missing_user_has_readable_fallback_without_id(self):
        await self.send('<@70> 確認してください')
        self.player.enqueue.assert_called_once_with('不明なユーザーさん、 確認してください', None, 1.2, '21', '1:2', volume=1.0, effects=())
