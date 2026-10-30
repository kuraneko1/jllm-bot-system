import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from jllm_plugins.conversation.kitasan import KitasanService
from jllm_plugins.dictionaries.dictionary import SoundDictionary
from jllm_plugins.dictionaries.readings import ReadingDictionary
from jllm_plugins.reader.core import Plugin as ReaderPlugin
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


def message(content="こんにちは", author_id=42, channel_id=123, bot=False, reference=None):
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
        author=SimpleNamespace(id=author_id, bot=bot, display_name="user", name="user"),
        mentions=[],
        role_mentions=[],
        reference=reference,
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
            ReaderConfig(
                allowed_channel_ids=frozenset({123}),
                toggles_path=str(self.root / "toggles.json"),
            ),
        )
        self.cog.players[1] = self.player

    def tearDown(self):
        self.temp.cleanup()

    def test_toggles_restore_on_restart_from_configured_path(self):
        self.cog.config.toggles_path = str(self.root / "nested" / "toggles.json")
        self.cog.toggles = {"img": False, "wav": False}
        self.cog._save_toggles()
        path = Path(self.cog.config.toggles_path)
        self.assertEqual(json.loads(path.read_text()), {"img": False, "wav": False})
        restarted = ReaderCog(
            self.cog.bot, self.tts, self.factory, self.settings, self.dictionary,
            self.readings, self.restrictions, self.cog.config,
        )
        self.assertEqual(restarted.toggles, {"img": False, "wav": False})

    @patch("jllm_plugins.reader.core.logger.exception")
    def test_toggles_missing_corrupt_and_partial_follow_defaults(self, log_error):
        path = Path(self.cog.config.toggles_path)
        cases = [
            (None, {"img": True, "wav": True}),
            ("{broken", {"img": True, "wav": True}),
            ("[]", {"img": True, "wav": True}),
            ('{"wav": 0, "unknown": false}', {"img": True, "wav": False}),
            ('{"img": "false"}', {"img": True, "wav": True}),
        ]
        for contents, expected in cases:
            with self.subTest(contents=contents):
                if contents is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_text(contents, encoding="utf-8")
                self.assertEqual(self.cog._load_toggles(), expected)
        with patch.object(Path, "read_text", side_effect=OSError("unreadable")):
            self.assertEqual(self.cog._load_toggles(), {"img": True, "wav": True})
        self.assertEqual(log_error.call_count, 2)
        path.unlink()
        self.cog.config.wav_enabled = False
        self.assertEqual(self.cog._load_toggles(), {"img": True, "wav": False})

    def test_required_toggles_fail_closed_at_startup(self):
        self.cog.config.require_existing_toggles = True
        path = Path(self.cog.config.toggles_path)
        for content in (None, "{broken", "[]", '{"img": false}',
                        '{"img": "false", "wav": false}',
                        '{"img": false, "wav": 0}'):
            with self.subTest(content=content):
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_text(content, encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    ReaderCog(
                        self.cog.bot, self.tts, self.factory, self.settings, self.dictionary,
                        self.readings, self.restrictions, self.cog.config,
                    )
        path.write_text('{"img": false, "wav": true, "other": 1}', encoding="utf-8")
        with patch.object(Path, "read_text", side_effect=OSError("unreadable")):
            with self.assertRaises(RuntimeError):
                self.cog._load_toggles()
        self.assertEqual(self.cog._load_toggles(), {"img": False, "wav": True})

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

    async def test_plugin_receives_shared_optional_trigger_services(self):
        miq = SimpleNamespace(matches_trigger=lambda text: text == "custom-miq")
        kitasan = KitasanService(None, words=["custom-kitasan"])
        dependencies = {
            "discord.gateway": SimpleNamespace(bot=self.cog.bot, add_cog=AsyncMock()),
            "tts.synthesis": self.tts, "speech.playback": self.factory,
            "voice.settings": self.settings, "dictionary.sound": self.dictionary,
            "dictionary.reading": self.readings, "policy.restrictions": self.restrictions,
            "feature.bookmarks": None, "dictionary.media": None, "feature.html-library": None,
            "feature.miq": miq, "feature.kitasan": kitasan,
        }
        ctx = SimpleNamespace(dependencies=dependencies, config={
            "toggles_path": str(self.root / "toggles.json")}, scope=SimpleNamespace(add=Mock()))
        reader = (await ReaderPlugin().setup(ctx))["feature.reader"]
        self.assertIs(reader.miq, miq)
        self.assertIs(reader.kitasan, kitasan)
        dependencies.pop("feature.miq")
        dependencies.pop("feature.kitasan")
        reader = (await ReaderPlugin().setup(ctx))["feature.reader"]
        self.assertIsNone(reader.miq)
        self.assertIsNone(reader.kitasan)

    async def test_miq_uses_shared_custom_trigger_and_explicit_none(self):
        self.cog.miq = SimpleNamespace(matches_trigger=lambda text: text == "custom-miq")
        await self.cog.on_message(message("custom-miq"))
        self.player.enqueue.assert_not_called()
        await self.cog.on_message(message("miq"))
        self.player.enqueue.assert_called_once()
        self.player.enqueue.reset_mock()
        self.cog.miq = None
        await self.cog.on_message(message("miq"))
        self.player.enqueue.assert_called_once()

    async def test_kitasan_uses_shared_custom_and_enabled_settings(self):
        self.cog.kitasan = KitasanService(None, words=["custom"])
        await self.cog.on_message(message("custom"))
        self.player.enqueue.assert_not_called()
        await self.cog.on_message(message("今北"))
        self.player.enqueue.assert_called_once()
        self.player.enqueue.reset_mock()
        self.cog.kitasan.enabled = False
        await self.cog.on_message(message("custom"))
        self.player.enqueue.assert_called_once()
        self.player.enqueue.reset_mock()
        self.cog.kitasan = None
        await self.cog.on_message(message("今北"))
        self.player.enqueue.assert_called_once()

    async def test_wav_only_mode_does_not_synthesize_text(self):
        self.cog.config.read_mode = "wav"
        await self.cog.on_message(message())
        self.player.enqueue.assert_not_called()

    async def test_joined_channel_is_read_even_outside_the_allowlist(self):
        # /join したチャンネルが読み上げ先。許可リストは範囲外の判定にだけ効く。
        self.cog.config.allowed_channel_ids = frozenset({999})
        await self.cog.on_message(message("こんにちは", channel_id=123))
        self.player.enqueue.assert_called_once()
        self.player.enqueue.reset_mock()
        await self.cog.on_message(message("よその雑談", channel_id=456))
        self.player.enqueue.assert_not_called()

    async def test_none_mode_keeps_text_and_wav_quiet(self):
        self.cog.config.read_mode = "none"
        await self.cog.on_message(message())
        await self.cog.on_message(message("mar f2"))
        self.player.enqueue.assert_not_called()
        self.player.enqueue_sound.assert_not_called()

    async def test_trailing_call_suffix_is_not_read_aloud(self):
        await self.cog.on_message(message("こんにちは jbot"))
        self.player.enqueue.assert_called_once()
        self.assertEqual(self.player.enqueue.call_args.args[0], "こんにちは")

    async def test_a_bare_call_suffix_is_not_read_aloud(self):
        for text in ("jbot", "JBOT", " jbot "):
            with self.subTest(text=text):
                self.player.enqueue.reset_mock()
                await self.cog.on_message(message(text))
                self.player.enqueue.assert_not_called()

    async def test_call_suffix_can_be_disabled_or_changed(self):
        self.cog.config.talk_call_suffix = ""
        await self.cog.on_message(message("こんにちは jbot"))
        self.assertEqual(self.player.enqueue.call_args.args[0], "こんにちは jbot")

        self.player.enqueue.reset_mock()
        self.cog.config.talk_call_suffix = "ボット"
        await self.cog.on_message(message("こんにちは ボット"))
        self.assertEqual(self.player.enqueue.call_args.args[0], "こんにちは")

    def sent(self, channel_id=123, ident=77):
        return SimpleNamespace(
            id=ident,
            guild=SimpleNamespace(id=1),
            channel=SimpleNamespace(id=channel_id),
        )

    async def test_own_response_is_read_with_the_default_speaker(self):
        await self.cog.speak_own_response(self.sent(), "応答です")
        self.player.enqueue.assert_called_once()
        args = self.player.enqueue.call_args.args
        self.assertEqual(args[:3], ("応答です", 1, 1.2))
        self.assertEqual(args[3], "77")
        self.assertEqual(args[4], "1:999")

    async def test_own_response_outside_the_reading_channel_is_not_read(self):
        await self.cog.speak_own_response(self.sent(channel_id=999), "よその返信")
        self.player.enqueue.assert_not_called()

    async def test_own_response_is_not_read_in_wav_or_none_mode(self):
        for mode in ("wav", "none"):
            with self.subTest(mode=mode):
                self.cog.config.read_mode = mode
                await self.cog.speak_own_response(self.sent(), "応答")
                self.player.enqueue.assert_not_called()
        self.cog.config.read_mode = "tts"

    async def test_bot_posts_are_not_read_by_watching_messages(self):
        # 読み上げは会話の応答を投稿した側が明示的に呼ぶ。投稿を監視して拾わない。
        await self.cog.on_message(
            message("応答です", author_id=999, bot=True, reference=SimpleNamespace(message_id=1))
        )
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
