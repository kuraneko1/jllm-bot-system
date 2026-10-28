# ruff: noqa: E501
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from jllm_plugins.reader.core import ReaderCog, media_trigger_match


class MediaUrlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cog = ReaderCog.__new__(ReaderCog)
        self.cog.media_dir = Path(self.directory.name)
        self.cog.media = {'V100': 'v100.gif', 'hello': 'hello.gif'}
        for filename in self.cog.media.values():
            (self.cog.media_dir / filename).write_bytes(b'GIF89a')

    def message(self, content):
        return SimpleNamespace(content=content, channel=SimpleNamespace(send=AsyncMock()))

    async def test_url_path_query_and_fragment_never_trigger(self):
        for content in (
            'https://example.com/V100.gif',
            'https://example.com/?q=hello#v100',
            '<https://example.com/V100>',
            '[リンク](https://example.com/V100)',
            'HTTPS://example.com/V100',
            'https://html.example/users/123/v100/',
        ):
            with self.subTest(content=content):
                message = self.message(content)
                await self.cog._send_media(message)
                message.channel.send.assert_not_awaited()

    async def test_caption_still_triggers(self):
        message = self.message('Hello https://example.com/v100')
        await self.cog._send_media(message)
        message.channel.send.assert_awaited_once()
        file = message.channel.send.call_args.kwargs['file']
        self.assertEqual(file.filename, 'hello.gif')
        file.close()

    async def test_global_word_inside_url_does_not_trigger(self):
        message = self.message('https://example.com/V100')
        with patch.object(self.cog.config, 'global_reaction_words', {'v100'}):
            await self.cog._send_media(message, global_only=True)
        message.channel.send.assert_not_awaited()

    async def test_global_caption_retains_global_filter(self):
        message = self.message('Hello V100 https://example.com/hello')
        with patch.object(self.cog.config, 'global_reaction_words', {'v100'}):
            await self.cog._send_media(message, global_only=True)
        message.channel.send.assert_awaited_once()
        file = message.channel.send.call_args.kwargs['file']
        self.assertEqual(file.filename, 'v100.gif')
        file.close()

    async def test_substring_inside_sentence_does_not_trigger(self):
        self.cog.media = {'mar': 'mar.gif', 'awa': 'awa.gif'}
        for filename in ('mar.gif', 'awa.gif'):
            (self.cog.media_dir / filename).write_bytes(b'GIF89a')
        for content in (
            'tomarRow',
            'かわいいmarだね',
            'MARmalade',
            'mar!',
            'awa、すごい',
            'mar。',
        ):
            with self.subTest(content=content):
                message = self.message(content)
                await self.cog._send_media(message)
                message.channel.send.assert_not_awaited()

    async def test_standalone_or_whitespace_delimited_triggers(self):
        self.cog.media = {'mar': 'mar.gif', 'awa': 'awa.gif'}
        for filename in ('mar.gif', 'awa.gif'):
            (self.cog.media_dir / filename).write_bytes(b'GIF89a')
        for content, expected in (
            ('mar', 'mar.gif'),
            ('MAR', 'mar.gif'),
            ('すごい mar だね', 'mar.gif'),
            ('　awa　', 'awa.gif'),
            ('mar  mar', 'mar.gif'),
        ):
            with self.subTest(content=content):
                message = self.message(content)
                await self.cog._send_media(message)
                self.assertTrue(message.channel.send.await_count >= 1)
                file = message.channel.send.call_args.kwargs['file']
                self.assertEqual(file.filename, expected)
                file.close()

    async def test_newline_separated_triggers_each_fire(self):
        self.cog.media = {'mar': 'mar.gif', 'awa': 'awa.gif'}
        for filename in ('mar.gif', 'awa.gif'):
            (self.cog.media_dir / filename).write_bytes(b'GIF89a')
        sent = []

        async def record(**kwargs):
            sent.append(kwargs['file'].filename)
            kwargs['file'].close()

        message = SimpleNamespace(
            content='mar\nawa', channel=SimpleNamespace(send=record)
        )
        await self.cog._send_media(message)
        self.assertEqual(sent, ['mar.gif', 'awa.gif'])

    def test_matcher_handles_multiword_and_empty_triggers(self):
        self.assertTrue(media_trigger_match('good morning all', 'good morning'))
        self.assertTrue(media_trigger_match('GOOD MORNING', 'good morning'))
        self.assertFalse(media_trigger_match('goodmorning', 'good morning'))
        self.assertFalse(media_trigger_match('anything', ''))
        self.assertFalse(media_trigger_match('anything', '   '))
