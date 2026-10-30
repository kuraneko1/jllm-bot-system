# ruff: noqa: E501
"""「辞書wavのみ」モード（``/llm switch atmos`` など）の読み上げ。

このモードでは wav 登録した語句だけを鳴らし、それ以外は TTS で読まない。
入室案内も合成しない。
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from jllm_plugins.reader.core import ReaderCog
from jllm_plugins.voice_settings.speeds import VoiceSpeeds
from jllm_plugins.voice_settings.volumes import VoiceVolumes


class WavOnlyReadingTests(unittest.IsolatedAsyncioTestCase):
    def build(self):
        cog = ReaderCog.__new__(ReaderCog)
        cog._handle_html_registration = AsyncMock(return_value=False)
        cog._handle_media_registration = AsyncMock(return_value=False)
        cog._send_media = AsyncMock()
        cog._react_skip = AsyncMock()
        cog.toggles = {"wav": True}
        player = SimpleNamespace(
            voice_client=SimpleNamespace(is_connected=lambda: True),
            text_channel_id=123, skip_users=set(), allow_nonverbal_emojis=False,
            speaker_id=1,
            enqueue=Mock(), enqueue_sound=Mock(), skip=Mock(return_value=True),
            skip_current_post=Mock(return_value=True),
        )
        cog.players = {1: player}
        cog.dictionary = SimpleNamespace(
            split=lambda value: [
                SimpleNamespace(kind="sound", value="sounds/a.wav", text="わん"),
                SimpleNamespace(kind="text", value=value),
            ],
            volume_of_file=lambda name: 1.0,
        )
        cog.readings = SimpleNamespace(apply=lambda value: value)
        cog.user_voices = {}
        cog.forced_voices = {}
        cog.voice_volumes = VoiceVolumes()
        cog.voice_speeds = VoiceSpeeds()
        cog._voice_id_names = {}
        cog.tts = SimpleNamespace(list_voices=AsyncMock(return_value=[]))
        return cog, player

    async def send(self, cog, text="こんにちは"):
        message = SimpleNamespace(
            id=11,
            guild=SimpleNamespace(id=1), author=SimpleNamespace(id=2, bot=False),
            channel=SimpleNamespace(id=123), content=text, clean_content=text,
        )
        with patch.object(cog.config, "allowed_channel_ids", {123}):
            await cog.on_message(message)

    async def test_wav_only_mode_plays_sounds_but_never_speaks(self):
        cog, player = self.build()

        with patch("jllm_plugins.llm.llm_backend.active_read_mode", return_value="wav"):
            await self.send(cog)

        player.enqueue_sound.assert_called_once()
        player.enqueue.assert_not_called()

    async def test_tts_mode_reads_the_rest_aloud(self):
        cog, player = self.build()

        with patch("jllm_plugins.llm.llm_backend.active_read_mode", return_value="tts"):
            await self.send(cog)

        player.enqueue_sound.assert_called_once()
        player.enqueue.assert_called_once()

    async def test_wav_only_mode_also_skips_the_wav_blocked_fallback(self):
        """wav再生を剥奪された人でも、このモードでは TTS で読まない。"""

        cog, player = self.build()
        cog._restricted = lambda message, feature: feature == "wav"

        with patch("jllm_plugins.llm.llm_backend.active_read_mode", return_value="wav"):
            await self.send(cog)

        player.enqueue_sound.assert_not_called()
        player.enqueue.assert_not_called()


class NoneModeReadingTests(unittest.IsolatedAsyncioTestCase):
    """``none`` では辞書 wav も TTS も鳴らさない（キューにも積まない）。"""

    def build(self):
        return WavOnlyReadingTests.build(self)

    async def send(self, cog, text="こんにちは"):
        return await WavOnlyReadingTests.send(self, cog, text)

    async def test_none_mode_plays_neither_wav_nor_tts(self):
        cog, player = self.build()

        with patch("jllm_plugins.llm.llm_backend.active_read_mode", return_value="none"):
            await self.send(cog)

        player.enqueue_sound.assert_not_called()
        player.enqueue.assert_not_called()

    async def test_reader_config_can_force_none(self):
        cog, player = self.build()
        cog.config.read_mode = "none"

        with patch("jllm_plugins.llm.llm_backend.active_read_mode", return_value="tts"):
            await self.send(cog)

        player.enqueue_sound.assert_not_called()
        player.enqueue.assert_not_called()


class AnnouncementTests(unittest.IsolatedAsyncioTestCase):
    async def test_wav_only_mode_does_not_announce_joins(self):
        cog = ReaderCog.__new__(ReaderCog)
        cog._user_key = lambda guild_id, user_id: f"{guild_id}:{user_id}"
        cog.voice_entries = {}
        cog._save_voice_entries = Mock()
        player = SimpleNamespace(
            closed=False,
            voice_client=SimpleNamespace(
                is_connected=lambda: True,
                channel=SimpleNamespace(id=5),
            ),
            enqueue_sound=Mock(),
            enqueue=Mock(),
        )
        cog.players = {1: player}
        member = SimpleNamespace(
            id=2, bot=False, display_name="だれか", name="dareka",
            guild=SimpleNamespace(id=1),
            voice=SimpleNamespace(channel=SimpleNamespace(id=5)),
        )
        before = SimpleNamespace(channel=None)
        after = SimpleNamespace(channel=SimpleNamespace(id=5))

        with patch.object(cog.config, "vc_announcement_enabled", True), \
             patch("jllm_plugins.llm.llm_backend.active_read_mode", return_value="wav"):
            await cog.on_voice_state_update(member, before, after)

        player.enqueue_sound.assert_not_called()
        player.enqueue.assert_not_called()

    async def test_none_mode_does_not_announce_joins(self):
        cog = ReaderCog.__new__(ReaderCog)
        cog._user_key = lambda guild_id, user_id: f"{guild_id}:{user_id}"
        cog.voice_entries = {}
        cog._save_voice_entries = Mock()
        player = SimpleNamespace(
            closed=False,
            voice_client=SimpleNamespace(
                is_connected=lambda: True,
                channel=SimpleNamespace(id=5),
            ),
            enqueue_sound=Mock(),
            enqueue=Mock(),
        )
        cog.players = {1: player}
        member = SimpleNamespace(
            id=2, bot=False, display_name="だれか", name="dareka",
            guild=SimpleNamespace(id=1),
            voice=SimpleNamespace(channel=SimpleNamespace(id=5)),
        )
        before = SimpleNamespace(channel=None)
        after = SimpleNamespace(channel=SimpleNamespace(id=5))

        with patch.object(cog.config, "vc_announcement_enabled", True), \
             patch("jllm_plugins.llm.llm_backend.active_read_mode", return_value="none"):
            await cog.on_voice_state_update(member, before, after)

        player.enqueue_sound.assert_not_called()
        player.enqueue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
