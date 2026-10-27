import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from jllm_plugins.dictionaries.dictionary import SoundDictionary
from jllm_plugins.dictionaries.readings import ReadingDictionary
from jllm_plugins.reader.core import ReaderCog, ReaderConfig
from jllm_plugins.restrictions.service import UserRestrictions
from jllm_plugins.voice_settings.join_voice import JoinVoiceCache
from jllm_plugins.voice_settings.settings import JsonMap
from jllm_plugins.voice_settings.speeds import VoiceSpeeds
from jllm_plugins.voice_settings.volumes import VoiceVolumes


class FakePlayer:
    def __init__(self, voice_client=None):
        self.voice_client = voice_client or SimpleNamespace(
            channel=SimpleNamespace(id=999),
            is_connected=lambda: True,
        )
        self.text_channel_id = 123
        self.speaker_id = 1
        self.speed = 1.2
        self.volume = 0.2
        self.allow_nonverbal_emojis = False
        self.skip_users = set()
        self.closed = False
        self.enqueue = Mock(return_value=True)
        self.enqueue_sound = Mock(return_value=True)
        self.skip = Mock(return_value=True)
        self.skip_current_post = Mock(return_value=True)
        self.enable_listening = AsyncMock()
        self.close = AsyncMock(side_effect=lambda: setattr(self, "closed", True))


def message(content="こんにちは", author_id=42, channel_id=123):
    return SimpleNamespace(
        id=10,
        content=content,
        clean_content=content,
        guild=SimpleNamespace(
            id=1,
            get_member=lambda _: None,
            get_role=lambda _: None,
            get_channel_or_thread=lambda _: None,
            emojis=[],
        ),
        channel=SimpleNamespace(id=channel_id),
        author=SimpleNamespace(id=author_id, bot=False, display_name="user", name="user"),
        mentions=[],
        role_mentions=[],
        add_reaction=AsyncMock(),
    )


class ReaderCoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.player = FakePlayer()
        self.settings = SimpleNamespace(
            forced_voices=JsonMap(self.root / "forced.json"),
            user_voices=JsonMap(self.root / "users.json"),
            speeds=VoiceSpeeds(self.root / "speeds.json"),
            volumes=VoiceVolumes(self.root / "volumes.json"),
            voice_entries=JsonMap(self.root / "history.json"),
            join_cache=JoinVoiceCache(self.root / "join", voice="clone こしあん"),
        )
        (self.root / "sounds.json").write_text('{"mar": "a.wav"}')
        (self.root / "sounds").mkdir()
        self.dictionary = SoundDictionary(str(self.root / "sounds.json"), str(self.root / "sounds"))
        self.readings = ReadingDictionary(str(self.root / "readings.json"))
        self.restrictions = UserRestrictions()
        self.tts = SimpleNamespace(
            speakers=AsyncMock(
                return_value=[{"styles": [{"id": "clone こしあん", "name": "clone こしあん"}]}]
            ),
            synthesize=AsyncMock(return_value=b"wav"),
        )
        self.factory = SimpleNamespace(create=Mock(return_value=self.player))
        self.cog = ReaderCog(
            SimpleNamespace(user=SimpleNamespace(id=999)),
            self.tts,
            self.factory,
            self.settings,
            self.dictionary,
            self.readings,
            self.restrictions,
            ReaderConfig(),
        )
        self.cog.players[1] = self.player

    def tearDown(self):
        self.temp.cleanup()

    async def test_normal_text_is_enqueued_with_user_voice(self):
        self.settings.user_voices.set("1:42", "clone")
        self.settings.speeds.set("clone", 1.5)
        self.settings.volumes.set("clone", 0.4)
        await self.cog.on_message(message())
        self.player.enqueue.assert_called_once()
        args, kwargs = self.player.enqueue.call_args
        self.assertEqual(args[:3], ("こんにちは", "clone", 1.5))
        self.assertEqual(kwargs["volume"], 0.4)

    async def test_skip_prefix_miq_and_ignored_user_do_not_enqueue(self):
        await self.cog.on_message(message(";secret"))
        await self.cog.on_message(message("miq"))
        self.restrictions.set_flag(1, 42, "ignore", True)
        await self.cog.on_message(message("別の本文"))
        self.player.enqueue.assert_not_called()

    async def test_wav_only_mode_does_not_synthesize_text(self):
        self.cog.config = ReaderConfig(read_mode="wav")
        await self.cog.on_message(message())
        self.player.enqueue.assert_not_called()

    async def test_sound_command_uses_dictionary_and_speed(self):
        await self.cog.on_message(message("mar f2"))
        self.player.enqueue_sound.assert_called_once()
        args, kwargs = self.player.enqueue_sound.call_args
        self.assertEqual(args[0], str(self.root / "sounds" / "a.wav"))
        self.assertEqual(args[1], 2.0)
        self.player.enqueue.assert_not_called()

    async def test_join_and_leave(self):
        voice_client = self.player.voice_client

        class FakeMember:
            def __init__(self):
                self.id = 42
                self.voice = SimpleNamespace(
                    channel=SimpleNamespace(
                        id=999,
                        mention="#voice",
                        connect=AsyncMock(return_value=voice_client),
                    )
                )

        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1, voice_client=None),
            user=FakeMember(),
            channel_id=123,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with patch("jllm_plugins.reader.core.discord.Member", FakeMember):
            self.cog.players.pop(1)
            await self.cog.join.callback(self.cog, interaction)
        self.factory.create.assert_called_once()
        self.player.enable_listening.assert_awaited_once()
        interaction.followup.send.assert_awaited_once()
        await self.cog.leave.callback(self.cog, interaction)
        self.player.close.assert_awaited()

    async def test_voice_state_announcement_uses_cache_and_history(self):
        member = SimpleNamespace(
            id=42,
            bot=False,
            display_name="user",
            name="user",
            guild=SimpleNamespace(id=1),
        )
        before = SimpleNamespace(channel=None)
        after = SimpleNamespace(channel=self.player.voice_client.channel)
        await self.cog.on_voice_state_update(member, before, after)
        self.player.enqueue_sound.assert_called_once()
        self.assertIn("1:42", self.settings.voice_entries.entries)
        self.tts.synthesize.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
