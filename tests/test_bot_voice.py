"""Bot 自身の返答を読む声（/botvoice）のテスト。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jllm_plugins.reader.core import ReaderCog
from jllm_plugins.voice_settings.settings import JsonMap

from tests.test_voice_lengths import BOT_VOICE, interaction, make_cog, sent


def with_bot_voice(cog, path, name=None):
    cog.bot_voice = JsonMap(Path(path))
    if name is not None:
        cog.bot_voice.set("bot", name)
    return cog


class BotVoiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "bot_voice.json"

    def test_unset_is_empty(self):
        cog = with_bot_voice(make_cog(), self.path)
        self.assertEqual(cog.bot_voice_name(), "")
        self.assertEqual(cog.bot_speaker(cog.players[1]), 1)

    def test_stored_name_is_used(self):
        cog = with_bot_voice(make_cog(), self.path, BOT_VOICE)
        self.assertEqual(cog.bot_voice_name(), BOT_VOICE)
        self.assertEqual(cog.bot_speaker(cog.players[1]), BOT_VOICE)

    async def test_own_response_is_read_with_the_bot_voice(self):
        cog = with_bot_voice(make_cog(), self.path, BOT_VOICE)
        with patch("jllm_plugins.llm.llm_backend.active_read_mode", return_value="tts"):
            await cog.speak_own_response(sent(), "応答です")
        args = cog.players[1].enqueue.call_args.args
        self.assertEqual(args[1], BOT_VOICE)

    async def test_own_response_falls_back_to_the_server_default(self):
        cog = with_bot_voice(make_cog(), self.path)
        with patch("jllm_plugins.llm.llm_backend.active_read_mode", return_value="tts"):
            await cog.speak_own_response(sent(), "応答です")
        self.assertEqual(cog.players[1].enqueue.call_args.args[1], 1)

    async def test_own_response_uses_the_bot_voice_length(self):
        from jllm_plugins.voice_settings.lengths import VoiceLengths

        lengths = VoiceLengths()
        lengths.set(BOT_VOICE, 3)
        cog = with_bot_voice(make_cog(lengths), self.path, BOT_VOICE)
        with patch("jllm_plugins.llm.llm_backend.active_read_mode", return_value="tts"):
            await cog.speak_own_response(sent(), "あいうえおかきくけこ")
        self.assertEqual(cog.players[1].enqueue.call_args.args[0], "あいう、以下略")

    async def test_readlength_without_speaker_targets_the_bot_voice(self):
        cog = with_bot_voice(make_cog(), self.path, BOT_VOICE)
        cog.config.owner_id = 42
        caller = interaction(42)
        await ReaderCog.readlength.callback(cog, caller, length=30)
        self.assertEqual(cog.voice_lengths.get(BOT_VOICE), 30)


class BotVoiceCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cog = with_bot_voice(make_cog(), Path(self.temp.name) / "bot_voice.json")
        self.cog.config.owner_id = 42

    async def test_non_owner_is_denied(self):
        caller = interaction(99)
        await ReaderCog.botvoice.callback(self.cog, caller, speaker=BOT_VOICE)
        caller.response.send_message.assert_awaited_once()
        self.assertEqual(self.cog.bot_voice_name(), "")

    async def test_sets_shows_and_clears(self):
        owner = interaction(42)
        await ReaderCog.botvoice.callback(self.cog, owner, speaker=BOT_VOICE)
        self.assertEqual(self.cog.bot_voice_name(), BOT_VOICE)
        self.assertIn(BOT_VOICE, owner.response.send_message.call_args.args[0])
        owner.response.send_message.reset_mock()
        await ReaderCog.botvoice.callback(self.cog, owner)
        self.assertIn(BOT_VOICE, owner.response.send_message.call_args.args[0])
        owner.response.send_message.reset_mock()
        await ReaderCog.botvoice.callback(self.cog, owner, clear=True)
        self.assertEqual(self.cog.bot_voice_name(), "")
        self.assertIn("戻しました", owner.response.send_message.call_args.args[0])

    async def test_unknown_voice_is_rejected(self):
        owner = interaction(42)
        await ReaderCog.botvoice.callback(self.cog, owner, speaker="存在しない声")
        self.assertEqual(self.cog.bot_voice_name(), "")
        self.assertIn("見つかりません", owner.response.send_message.call_args.args[0])
