"""読み上げモード ``full``（Bot の返答も辞書wav＋TTS）のテスト。"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from jllm_plugins.llm import llm_backend
from jllm_plugins.reader.core import ReaderCog
from jllm_plugins.voice_settings.lengths import VoiceLengths
from jllm_plugins.voice_settings.speeds import VoiceSpeeds
from jllm_plugins.voice_settings.volumes import VoiceVolumes
from jllm_tts import Voice

SOUND = "/sounds/mar.wav"
BOT_VOICE = "clone こしあん"


class FakeDictionary:
    """``mar`` を登録 wav、それ以外を本文として分割する辞書。"""

    def split(self, value):
        pieces = value.split("mar")
        segments = []
        for index, piece in enumerate(pieces):
            if piece:
                segments.append(SimpleNamespace(kind="text", value=piece))
            if index < len(pieces) - 1:
                segments.append(SimpleNamespace(kind="sound", value=SOUND, text="mar"))
        return segments

    def volume_of_file(self, name):
        return 1.0


def make_cog():
    cog = ReaderCog.__new__(ReaderCog)
    cog.bot = SimpleNamespace(user=SimpleNamespace(id=999))
    player = SimpleNamespace(
        voice_client=SimpleNamespace(
            is_connected=lambda: True, channel=SimpleNamespace(id=999)
        ),
        text_channel_id=123,
        skip_users=set(),
        allow_nonverbal_emojis=False,
        speaker_id=1,
        closed=False,
        enqueue=Mock(return_value=True),
        enqueue_sound=Mock(return_value=True),
        skip=Mock(return_value=True),
        skip_current_post=Mock(return_value=True),
    )
    cog.players = {1: player}
    cog.dictionary = FakeDictionary()
    cog.readings = SimpleNamespace(apply=lambda value: value)
    cog.user_voices = {}
    cog.forced_voices = {}
    cog.voice_speeds = VoiceSpeeds()
    cog.voice_volumes = VoiceVolumes()
    cog.voice_lengths = VoiceLengths()
    cog._voice_id_names = {}
    cog.voice_entries = {}
    cog._save_voice_history = Mock()
    cog.tts = SimpleNamespace(
        list_voices=AsyncMock(return_value=[Voice(id=1, name=BOT_VOICE)]),
        speakers=AsyncMock(
            return_value=[{"styles": [{"id": 1, "name": BOT_VOICE}]}]
        ),
    )
    cog._handle_html_registration = AsyncMock(return_value=False)
    cog._handle_media_registration = AsyncMock(return_value=False)
    cog._send_media = AsyncMock()
    cog._react_skip = AsyncMock()
    return cog, player


def sent(ident=77, channel_id=123):
    return SimpleNamespace(
        id=ident,
        guild=SimpleNamespace(id=1),
        channel=SimpleNamespace(id=channel_id),
    )


def message(content="こんにちは", author_id=2, channel_id=123):
    return SimpleNamespace(
        id=11,
        content=content,
        clean_content=content,
        guild=SimpleNamespace(id=1),
        channel=SimpleNamespace(id=channel_id),
        author=SimpleNamespace(id=author_id, bot=False, display_name="user", name="user"),
        mentions=[],
        role_mentions=[],
        add_reaction=AsyncMock(),
    )


def mode(mode_name):
    return patch(
        "jllm_plugins.llm.llm_backend.active_read_mode", return_value=mode_name
    )


class FullModeOwnResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_splits_the_dictionary_and_plays_the_sound(self):
        cog, player = make_cog()
        with mode("full"):
            await cog.speak_own_response(sent(), "marと本文")
        player.enqueue_sound.assert_called_once()
        self.assertEqual(player.enqueue_sound.call_args.args[0], SOUND)
        player.enqueue.assert_called_once()
        self.assertEqual(player.enqueue.call_args.args[0], "と本文")

    async def test_tts_mode_reads_the_response_without_the_dictionary(self):
        cog, player = make_cog()
        with mode("tts"):
            await cog.speak_own_response(sent(), "marと本文")
        player.enqueue_sound.assert_not_called()
        player.enqueue.assert_called_once()
        self.assertEqual(player.enqueue.call_args.args[0], "marと本文")

    async def test_full_without_the_wav_toggle_speaks_everything(self):
        cog, player = make_cog()
        cog.toggles["wav"] = False
        with mode("full"):
            await cog.speak_own_response(sent(), "marと本文")
        player.enqueue_sound.assert_not_called()
        spoken = [call.args[0] for call in player.enqueue.call_args_list]
        self.assertEqual(spoken, ["mar", "と本文"])

    async def test_full_label_and_mode_are_registered(self):
        self.assertEqual(
            llm_backend.read_mode_label("full"),
            "Botの返答を辞書wav＋TTSで読む",
        )
        self.assertIn("full", llm_backend.READ_MODES)
        self.assertEqual(llm_backend.read_mode_label("tts"), "Botの返答をTTSで読む")
        self.assertEqual(
            llm_backend.read_mode_label("wav"), "Botの返答を辞書wavだけで読む"
        )
        self.assertEqual(llm_backend.read_mode_label("none"), "Botの返答を読まない")

    async def test_reader_config_does_not_override_the_bot_mode(self):
        """Botの返答は /llm read の設定だけで決まる（reader 設定は効かない）。"""

        cog, player = make_cog()
        cog.config.read_mode = "full"
        with mode("wav"):
            await cog.speak_own_response(sent(), "marと本文")
        player.enqueue_sound.assert_called_once()
        player.enqueue.assert_not_called()

    async def test_none_mode_stays_quiet(self):
        cog, player = make_cog()
        with mode("none"):
            await cog.speak_own_response(sent(), "marと本文")
        player.enqueue_sound.assert_not_called()
        player.enqueue.assert_not_called()


class FullModeUserPostTests(unittest.IsolatedAsyncioTestCase):
    async def send(self, cog, text="marと本文"):
        with patch.object(cog.config, "allowed_channel_ids", {123}):
            await cog.on_message(message(text))

    async def test_full_and_tts_read_user_posts_the_same_way(self):
        for mode_name in ("tts", "full"):
            with self.subTest(mode=mode_name):
                cog, player = make_cog()
                with mode(mode_name):
                    await self.send(cog)
                player.enqueue_sound.assert_called_once()
                player.enqueue.assert_called_once()
                self.assertEqual(player.enqueue.call_args.args[0], "と本文")


class FullModeAnnouncementTests(unittest.IsolatedAsyncioTestCase):
    async def announce(self, mode_name):
        cog, player = make_cog()
        cog._join_cache = lambda: SimpleNamespace(
            voice=BOT_VOICE, get=lambda member_id, text: "/cached.wav"
        )
        member = SimpleNamespace(
            id=5,
            bot=False,
            display_name="だれか",
            name="dareka",
            guild=SimpleNamespace(id=1),
            voice=SimpleNamespace(channel=SimpleNamespace(id=999)),
        )
        before = SimpleNamespace(channel=None)
        after = SimpleNamespace(channel=SimpleNamespace(id=999))
        with mode(mode_name):
            await cog.on_voice_state_update(member, before, after)
        return player

    async def test_full_announces_joins_like_tts(self):
        player = await self.announce("full")
        player.enqueue_sound.assert_called_once()
        self.assertEqual(player.enqueue_sound.call_args.args[0], "/cached.wav")

    async def test_wav_mode_keeps_join_announcements_quiet(self):
        player = await self.announce("wav")
        player.enqueue_sound.assert_not_called()


if __name__ == "__main__":
    unittest.main()
