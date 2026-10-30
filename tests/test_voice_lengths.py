"""話者ごとの読み上げ文字数（``/readlength``）のテスト。"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from jllm_plugins.reader.core import ReaderCog
from jllm_plugins.storage.plugin import LocalStorage
from jllm_plugins.voice_settings.lengths import VoiceLengths, check_length
from jllm_plugins.voice_settings.settings import VoiceSettings
from jllm_plugins.voice_settings.speeds import VoiceSpeeds
from jllm_plugins.voice_settings.volumes import VoiceVolumes
from jllm_tts import Voice

BOT_VOICE = "clone こしあん"


class VoiceLengthsStoreTests(unittest.TestCase):
    def test_unset_is_none_and_invalid_values_rejected(self):
        store = VoiceLengths()
        self.assertIsNone(store.get("nobody"))
        for bad in (0, 1901, -5, 12.5, "200", True, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    store.set("voice", bad)
        self.assertIsNone(store.get("voice"))

    def test_boundaries_and_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/voice_lengths.json"
            store = VoiceLengths(path)
            self.assertEqual(store.set("a", 1), 1)
            self.assertEqual(store.set("b", 1900), 1900)
            self.assertEqual(store.set("c", 120), 120)
            reloaded = VoiceLengths(path)
            self.assertEqual(reloaded.get("a"), 1)
            self.assertEqual(reloaded.get("b"), 1900)
            self.assertEqual(reloaded.get("c"), 120)
            self.assertTrue(reloaded.remove("c"))
            self.assertFalse(reloaded.remove("c"))
            self.assertIsNone(VoiceLengths(path).get("c"))

    def test_corrupt_and_invalid_entries_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "voice_lengths.json"
            path.write_text("{broken", encoding="utf-8")
            self.assertIsNone(VoiceLengths(path).get("a"))
            path.write_text(
                json.dumps({"ok": 50, "zero": 0, "text": "120", "bool": True}),
                encoding="utf-8",
            )
            store = VoiceLengths(path)
            self.assertEqual(store.get("ok"), 50)
            self.assertIsNone(store.get("zero"))
            self.assertIsNone(store.get("text"))
            self.assertIsNone(store.get("bool"))
            self.assertEqual(check_length(1900), 1900)


class VoiceSettingsLengthsTests(unittest.TestCase):
    def test_default_path_and_custom_path(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalStorage(directory)
            settings = VoiceSettings(storage, {})
            self.assertEqual(settings.lengths.path.name, "voice_lengths.json")
            settings.lengths.set(BOT_VOICE, 77)
            self.assertEqual(VoiceLengths(settings.lengths.path).get(BOT_VOICE), 77)
            custom = VoiceSettings(storage, {"lengths_path": "custom_lengths.json"})
            self.assertEqual(custom.lengths.path.name, "custom_lengths.json")


def make_cog(lengths=None, speaker_id=1):
    cog = ReaderCog.__new__(ReaderCog)
    cog.bot = SimpleNamespace(user=SimpleNamespace(id=999))
    cog.players = {}
    player = SimpleNamespace(
        voice_client=SimpleNamespace(
            is_connected=lambda: True, channel=SimpleNamespace(id=999)
        ),
        text_channel_id=123,
        skip_users=set(),
        allow_nonverbal_emojis=False,
        speaker_id=speaker_id,
        enqueue=Mock(),
        enqueue_sound=Mock(),
    )
    cog.players[1] = player
    cog.dictionary = SimpleNamespace(
        split=lambda value: [SimpleNamespace(kind="text", value=value)],
        volume_of_file=lambda name: 1.0,
    )
    cog.readings = SimpleNamespace(apply=lambda value: value)
    cog.user_voices = {}
    cog.forced_voices = {}
    cog.voice_speeds = VoiceSpeeds()
    cog.voice_volumes = VoiceVolumes()
    cog.voice_lengths = lengths if lengths is not None else VoiceLengths()
    cog._voice_id_names = {}
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
    return cog


def message(content="こんにちは", author_id=2):
    return SimpleNamespace(
        id=11,
        content=content,
        clean_content=content,
        guild=SimpleNamespace(id=1),
        channel=SimpleNamespace(id=123),
        author=SimpleNamespace(id=author_id, bot=False, display_name="user", name="user"),
        mentions=[],
        role_mentions=[],
        add_reaction=AsyncMock(),
    )


def sent(ident=77, channel_id=123):
    return SimpleNamespace(
        id=ident,
        guild=SimpleNamespace(id=1),
        channel=SimpleNamespace(id=channel_id),
    )


async def send(cog, text, author_id=2):
    with patch.object(cog.config, "allowed_channel_ids", {123}):
        await cog.on_message(message(text, author_id))


class ReaderVoiceLengthTests(unittest.IsolatedAsyncioTestCase):
    async def test_unset_falls_back_to_the_config_default(self):
        cog = make_cog()
        self.assertEqual(await cog.tts_length_for(1), 200)
        cog.config.max_message_length = 120
        self.assertEqual(await cog.tts_length_for(1), 120)

    async def test_name_and_id_lookup(self):
        lengths = VoiceLengths()
        lengths.set(BOT_VOICE, 42)
        cog = make_cog(lengths)
        self.assertEqual(await cog.tts_length_for(BOT_VOICE), 42)
        self.assertEqual(await cog.tts_length_for(1), 42)
        self.assertEqual(await cog.tts_length_for(99), 200)

    async def test_backend_failure_falls_back_to_the_default(self):
        cog = make_cog()
        cog.tts = SimpleNamespace(
            list_voices=AsyncMock(side_effect=RuntimeError("tts down"))
        )
        self.assertEqual(await cog.tts_length_for(1), 200)

    async def test_posts_ignore_voice_lengths(self):
        """文字数の設定は Bot の返答だけに効く（ユーザー投稿は変わらない）。"""

        lengths = VoiceLengths()
        lengths.set(BOT_VOICE, 3)
        cog = make_cog(lengths)
        with patch("jllm_plugins.llm.llm_backend.active_read_mode", return_value="tts"):
            await send(cog, "あいうえおかきくけこ")
        cog.players[1].enqueue.assert_called_once()
        self.assertEqual(cog.players[1].enqueue.call_args.args[0], "あいうえおかきくけこ")

    async def test_forced_voice_does_not_change_the_post_length(self):
        lengths = VoiceLengths()
        lengths.set(BOT_VOICE, 3)
        lengths.set("Clone Second", 6)
        cog = make_cog(lengths)
        cog.user_voices = {"1:2": "Clone Second"}
        with patch("jllm_plugins.llm.llm_backend.active_read_mode", return_value="tts"):
            await send(cog, "あいうえおかきくけこ")
        self.assertEqual(
            cog.players[1].enqueue.call_args.args[0], "あいうえおかきくけこ"
        )

    async def test_unset_post_uses_the_config_default(self):
        cog = make_cog()
        cog.config.max_message_length = 5
        with patch("jllm_plugins.llm.llm_backend.active_read_mode", return_value="tts"):
            await send(cog, "あいうえおかきくけこ")
        self.assertEqual(cog.players[1].enqueue.call_args.args[0], "あいうえお、以下略")

    async def test_bot_response_uses_the_bot_voice_length(self):
        lengths = VoiceLengths()
        lengths.set(BOT_VOICE, 3)
        cog = make_cog(lengths)
        with patch("jllm_plugins.llm.llm_backend.active_read_mode", return_value="tts"):
            await cog.speak_own_response(sent(), "あいうえおかきくけこ")
        cog.players[1].enqueue.assert_called_once()
        self.assertEqual(cog.players[1].enqueue.call_args.args[0], "あいう、以下略")

    async def test_unset_bot_response_uses_the_config_default(self):
        cog = make_cog()
        cog.config.max_message_length = 4
        with patch("jllm_plugins.llm.llm_backend.active_read_mode", return_value="tts"):
            await cog.speak_own_response(sent(), "あいうえおかきくけこ")
        self.assertEqual(cog.players[1].enqueue.call_args.args[0], "あいうえ、以下略")


def interaction(uid=42, guild_id=1):
    return SimpleNamespace(
        user=SimpleNamespace(id=uid),
        guild=SimpleNamespace(id=guild_id),
        response=SimpleNamespace(send_message=AsyncMock()),
    )


class ReadLengthCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = make_cog()
        self.cog.config.owner_id = 42

    async def test_non_owner_and_unset_owner_are_denied(self):
        caller = interaction(99)
        await ReaderCog.readlength.callback(self.cog, caller, length=10)
        caller.response.send_message.assert_awaited_once()
        self.assertIsNone(self.cog.voice_lengths.get(BOT_VOICE))
        caller.response.send_message.reset_mock()
        self.cog.config.owner_id = 0
        owner = interaction(42)
        await ReaderCog.readlength.callback(self.cog, owner, length=10)
        owner.response.send_message.assert_awaited_once()
        self.assertIsNone(self.cog.voice_lengths.get(BOT_VOICE))

    async def test_sets_shows_and_clears(self):
        caller = interaction(42)
        await ReaderCog.readlength.callback(self.cog, caller, length=50)
        self.assertEqual(self.cog.voice_lengths.get(BOT_VOICE), 50)
        self.assertIn("先頭 50 文字", caller.response.send_message.call_args.args[0])
        caller.response.send_message.reset_mock()
        await ReaderCog.readlength.callback(self.cog, caller, length=None)
        self.assertIn("先頭 50 文字", caller.response.send_message.call_args.args[0])
        caller.response.send_message.reset_mock()
        await ReaderCog.readlength.callback(self.cog, caller, clear=True)
        self.assertIsNone(self.cog.voice_lengths.get(BOT_VOICE))
        self.assertIn("先頭 200 文字", caller.response.send_message.call_args.args[0])

    async def test_rejects_out_of_range_lengths(self):
        for bad in (0, 1901, -1):
            with self.subTest(bad=bad):
                caller = interaction(42)
                await ReaderCog.readlength.callback(self.cog, caller, length=bad)
                caller.response.send_message.assert_awaited_once()
                self.assertIsNone(self.cog.voice_lengths.get(BOT_VOICE))
                self.assertIn("1〜1900", caller.response.send_message.call_args.args[0])

    async def test_targets_the_bot_voice(self):
        caller = interaction(42)
        await ReaderCog.readlength.callback(self.cog, caller, length=30)
        self.assertEqual(self.cog.voice_lengths.get(BOT_VOICE), 30)
        self.assertIn(
            "ユーザー投稿の文字数は変わりません",
            caller.response.send_message.call_args.args[0],
        )

    async def test_autocomplete_lists_speaker_names(self):
        choices = await self.cog._speaker_name_choices(interaction(42), "こし")
        self.assertEqual([choice.value for choice in choices], [BOT_VOICE])


if __name__ == "__main__":
    unittest.main()
