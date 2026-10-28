# ruff: noqa: E501
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from jllm_plugins.reader.core import ReaderCog, filter_emojis, has_readable_text
from jllm_plugins.voice_settings.speeds import VoiceSpeeds
from jllm_plugins.voice_settings.volumes import VoiceVolumes


class EmojiTests(unittest.IsolatedAsyncioTestCase):
    def test_unicode_emoji_preserved(self):
        text = 'こんにちは😀🍣👨‍👩‍👧‍👦🇯🇵1️⃣'
        self.assertEqual(filter_emojis(text), text)

    def test_nonverbal_toggle_preserved(self):
        self.assertEqual(filter_emojis('声👅💋😀'), '声😀')
        self.assertEqual(filter_emojis('声👅💋😀', True), '声👅💋😀')

    def test_emoji_only_is_not_readable(self):
        for text in ('😀', '🍣🎉', '1️⃣2️⃣', 'ℹ️', '👨‍👩‍👧‍👦🇯🇵', '😀！'):
            with self.subTest(text=text):
                self.assertFalse(has_readable_text(text))
        for text in ('こんにちは😀', '123', 'hello🍣'):
            self.assertTrue(has_readable_text(text))

    async def check_message(self, text):
        cog = ReaderCog.__new__(ReaderCog)
        cog._handle_html_registration = AsyncMock(return_value=False)
        cog._handle_media_registration = AsyncMock(return_value=False)
        cog._send_media = AsyncMock()
        player = SimpleNamespace(voice_client=SimpleNamespace(is_connected=lambda: True),
            text_channel_id=123, skip_users=set(), allow_nonverbal_emojis=False,
            speaker_id=1,
            enqueue=Mock())
        cog.players = {1: player}
        cog.dictionary = SimpleNamespace(split=lambda value: [SimpleNamespace(kind='text', value=value)])
        cog.readings = SimpleNamespace(apply=Mock(side_effect=lambda value: value))
        cog.user_voices = {}
        cog.forced_voices = {}
        cog.voice_volumes = VoiceVolumes()
        cog.voice_speeds = VoiceSpeeds()
        cog._voice_id_names = {}
        cog.tts = SimpleNamespace(list_voices=AsyncMock(return_value=[]))
        message = SimpleNamespace(id=7, guild=SimpleNamespace(id=1), author=SimpleNamespace(id=1,bot=False),
            channel=SimpleNamespace(id=123),content=text,clean_content=text)
        with patch.object(cog.config, 'allowed_channel_ids', {123}):
            await cog.on_message(message)
        return cog, player

    async def test_text_with_emoji_reaches_tts(self):
        _, player = await self.check_message('こんにちは🍣😀')
        player.enqueue.assert_called_once_with('こんにちは🍣😀', None, 1.2, '7', '1:1', volume=1.0, effects=())

    async def test_emoji_only_never_queued_or_expanded_by_reading_dictionary(self):
        cog, player = await self.check_message('1️⃣ℹ️😀')
        player.enqueue.assert_not_called()
        cog.readings.apply.assert_not_called()
