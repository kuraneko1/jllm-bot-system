# ruff: noqa: E501
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from jllm_plugins.reader.core import ReaderCog


class MessageScopeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = ReaderCog.__new__(ReaderCog)
        self.cog.players = {}
        self.cog._handle_html_registration = AsyncMock(return_value=False)
        self.cog._handle_media_registration = AsyncMock(return_value=False)
        self.cog._send_media = AsyncMock()
        self.cog._react_v100 = AsyncMock()
        self.channels = patch.object(self.cog.config, "allowed_channel_ids", {123, 456})
        self.words = patch.object(self.cog.config, "global_reaction_words", {"v100", "tesla"})
        self.channels.start()
        self.words.start()
        self.addCleanup(self.channels.stop)
        self.addCleanup(self.words.stop)

    def message(self, channel, content):
        return SimpleNamespace(
            guild=SimpleNamespace(id=1), author=SimpleNamespace(bot=False),
            channel=SimpleNamespace(id=channel), content=content,
        )

    async def test_other_channel_ignored(self):
        await self.cog.on_message(self.message(999, "hello"))
        self.cog._handle_html_registration.assert_not_awaited()
        self.cog._handle_media_registration.assert_not_awaited()
        self.cog._send_media.assert_not_awaited()
        self.cog._react_v100.assert_not_awaited()

    async def test_each_whitelisted_channel_allowed(self):
        for channel in (123, 456):
            message = self.message(channel, "hello")
            await self.cog.on_message(message)
            self.cog._send_media.assert_awaited_with(message)
        self.assertEqual(self.cog._handle_html_registration.await_count, 2)

    async def test_global_word_does_not_enable_registration_or_speech(self):
        message = self.message(999, "This V100 is fast")
        self.cog.players = None  # 対象外チャンネルはプレイヤーにアクセスしない
        await self.cog.on_message(message)
        self.cog._send_media.assert_awaited_once_with(message, global_only=True)
        self.cog._react_v100.assert_awaited_once_with(message)
        self.cog._handle_html_registration.assert_not_awaited()
        self.cog._handle_media_registration.assert_not_awaited()

    async def test_global_media_filters_unrelated_triggers(self):
        with tempfile.TemporaryDirectory() as directory:
            self.cog.media_dir = Path(directory)
            self.cog.media = {"V100": "v100.gif", "hello": "hello.gif"}
            for filename in self.cog.media.values():
                (self.cog.media_dir / filename).write_bytes(b"GIF89a")
            message = self.message(999, "hello V100")
            message.channel.send = AsyncMock()
            await ReaderCog._send_media(self.cog, message, global_only=True)
            message.channel.send.assert_awaited_once()
            file = message.channel.send.call_args.kwargs["file"]
            self.assertEqual(file.filename, "v100.gif")
            file.close()

    async def test_empty_whitelist_still_allows_global_reactions_only(self):
        with patch.object(self.cog.config, "allowed_channel_ids", set()):
            message = self.message(123, "v100")
            await self.cog.on_message(message)
        self.cog._send_media.assert_awaited_once_with(message, global_only=True)
        self.cog._react_v100.assert_awaited_once_with(message)
        self.cog._handle_html_registration.assert_not_awaited()
        self.cog._handle_media_registration.assert_not_awaited()

    async def test_empty_lists_deny_every_message(self):
        with patch.object(self.cog.config, "allowed_channel_ids", set()), patch.object(
            self.cog.config, "global_reaction_words", set()
        ):
            await self.cog.on_message(self.message(123, "v100"))
        self.cog._send_media.assert_not_awaited()
        self.cog._react_v100.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
