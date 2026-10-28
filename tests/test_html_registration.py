# ruff: noqa: E501
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from jllm_plugins.html_library.store import HtmlStore
from jllm_plugins.reader.core import ReaderCog


class HtmlRegistrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cog = ReaderCog.__new__(ReaderCog)
        self.bot_user = object()
        self.cog.bot = SimpleNamespace(user=self.bot_user)
        self.cog.html_pending_dir = Path(self.directory.name)
        self.cog.html_dir = Path(self.directory.name) / 'published'
        self.cog._save_html_catalog = lambda: None
        self.cog._rebuild_html_dashboards = lambda: None
        self.cog.pending_html = {}
        self.cog.html_catalog = []
        self.cog._publish_html = AsyncMock(side_effect=self.publish_html)
        self.limit = 5 * 1024 * 1024
        self.cog.html_store = SimpleNamespace(
            config=SimpleNamespace(max_bytes=self.limit, approver_id=0, require_approval=False),
            register=self.register_html,
            publish=self.publish_html,
            viewer_wrapper_html=HtmlStore.viewer_wrapper_html,
            reject=lambda request_id: 'rejected',
            expire=lambda request_id: 'expired',
        )

    def register_html(self, data, filename, author):
        self.cog.pending_html['request-id'] = {
            'title': filename,
            'id': 'request-id',
            'author_id': str(author.id),
            'display_name': author.display_name,
            'original': data,
            'created_at': 0,
        }
        return 'request-id'

    def publish_html(self, request_id):
        request = self.cog.pending_html.pop(request_id, None)
        if request is None:
            return 'このHTML登録申請は期限切れです。'
        site_dir = self.cog.html_dir / 'users' / request['author_id'] / request['id']
        site_dir.mkdir(parents=True, exist_ok=True)
        (site_dir / 'content.html').write_bytes(request['original'])
        (site_dir / 'index.html').write_text(
            self.cog._viewer_wrapper_html(
                request['title'], request['author_id'], request['id'], request['original']
            ),
            encoding='utf-8',
        )
        self.cog.html_catalog.append({
            'site_id': request['id'],
            'user_id': request['author_id'],
            'title': request['title'],
            'created_at': request['created_at'],
        })
        self.cog._save_html_catalog()
        self.cog._rebuild_html_dashboards()
        return 'published URL'

    def message(self, size, data=None):
        attachment = SimpleNamespace(
            filename='example.html', content_type='text/html', size=size,
            read=AsyncMock(return_value=b'x' * size if data is None else data),
        )
        return SimpleNamespace(
            mentions=[self.bot_user], attachments=[attachment],
            author=SimpleNamespace(id=99, display_name='User'), reply=AsyncMock(),
        )

    async def test_exactly_at_limit_allowed(self):
        message = self.message(self.limit)
        self.assertTrue(await self.cog._handle_html_registration(message))
        self.cog._publish_html.assert_awaited_once()

    async def test_over_limit_downloads_and_pends_with_dm_approval(self):
        with patch.object(self.cog.html_store.config, 'approver_id', 42):
            approver = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=7)))
            self.cog.bot.fetch_user = AsyncMock(return_value=approver)
            message = self.message(self.limit + 1)
            await self.cog._handle_html_registration(message)
        message.attachments[0].read.assert_awaited()
        self.cog._publish_html.assert_not_awaited()
        self.cog.bot.fetch_user.assert_awaited_once_with(42)
        approver.send.assert_awaited_once()
        self.assertIsNotNone(approver.send.call_args.kwargs.get('view'))
        args = message.reply.call_args.args[0]
        self.assertIn('承認待ち', args)
        self.assertNotIn('<@42>', args)
        self.assertEqual(len(self.cog.pending_html), 1)

    async def test_actual_download_size_also_triggers_approval(self):
        with patch.object(self.cog.html_store.config, 'approver_id', 42):
            message = self.message(1, data=b'x' * (self.limit + 1))
            await self.cog._handle_html_registration(message)
        self.cog._publish_html.assert_not_awaited()
        self.assertEqual(len(self.cog.pending_html), 1)

    async def test_dm_failure_falls_back_to_channel_mention(self):
        with patch.object(self.cog.html_store.config, 'approver_id', 42):
            self.cog.bot.fetch_user = AsyncMock(side_effect=OSError('no route'))
            message = self.message(self.limit + 1)
            await self.cog._handle_html_registration(message)
        args = message.reply.call_args.args[0]
        self.assertIn('<@42>', args)
        self.assertIsNotNone(message.reply.call_args.kwargs.get('view'))
        self.assertEqual(len(self.cog.pending_html), 1)

    async def test_approver_own_oversize_publishes_directly(self):
        with patch.object(self.cog.html_store.config, 'approver_id', 99):
            message = self.message(self.limit + 1)
            await self.cog._handle_html_registration(message)
        self.cog._publish_html.assert_awaited_once()

    async def test_html_approval_can_be_disabled_independently(self):
        with patch.object(self.cog.html_store.config, 'approver_id', 42), patch.object(
            self.cog.html_store.config, 'require_approval', False
        ):
            await self.cog._handle_html_registration(self.message(100))
        self.cog._publish_html.assert_awaited_once()

    async def test_html_approval_remains_available(self):
        message = self.message(100)
        with patch.object(self.cog.html_store.config, 'approver_id', 42), patch.object(
            self.cog.html_store.config, 'require_approval', True
        ):
            await self.cog._handle_html_registration(message)
        self.cog._publish_html.assert_not_awaited()
        self.assertIsNotNone(message.reply.call_args.kwargs.get('view'))
        message.reply.call_args.kwargs['view'].stop()

    async def test_multiple_html_files_get_feedback(self):
        message = self.message(100)
        message.attachments.append(self.message(100).attachments[0])
        self.assertTrue(await self.cog._handle_html_registration(message))
        for attachment in message.attachments:
            attachment.read.assert_not_awaited()
        self.cog._publish_html.assert_not_awaited()
        self.assertIn('1ファイルずつ', message.reply.call_args.args[0])

    async def test_download_failure_gets_retry_feedback(self):
        message = self.message(100)
        message.attachments[0].read.side_effect = OSError('download failed')
        with self.assertLogs('jllm_plugins.reader.core', level='ERROR'):
            self.assertTrue(await self.cog._handle_html_registration(message))
        self.assertFalse(self.cog.pending_html)
        self.cog._publish_html.assert_not_awaited()
        self.assertIn('もう一度添付', message.reply.call_args.args[0])

    async def test_publish_keeps_original_and_builds_dashboard(self):
        self.cog.html_dir = Path(self.directory.name) / 'published'
        self.cog.html_catalog = []
        self.cog._save_html_catalog = lambda: None
        self.cog._rebuild_html_dashboards = lambda: None
        data = '<body><p>こんにちは</p></body>'.encode()
        message = self.message(len(data), data=data)
        with patch.object(
            self.cog, '_publish_html', ReaderCog._publish_html.__get__(self.cog)
        ):
            self.assertTrue(await self.cog._handle_html_registration(message))
        self.assertEqual(len(self.cog.html_catalog), 1)
        record = self.cog.html_catalog[0]
        site = self.cog.html_dir / 'users' / '99' / record['site_id']
        self.assertEqual((site / 'content.html').read_bytes(), data)
        rendered = (site / 'index.html').read_text()
        self.assertEqual(rendered.count('<style id="html-viewer-style">'), 1)
        self.assertIn('<nav id="html-viewer-nav">', rendered)
        self.assertIn('こんにちは', rendered)
        self.assertFalse(self.cog.pending_html)

    def test_wrapper_without_body_includes_style_only_once(self):
        rendered = self.cog._viewer_wrapper_html('Title', '99', 'abc', b'<p>Hi</p>')
        self.assertEqual(rendered.count('<style id="html-viewer-style">'), 1)
        self.assertIn('<nav id="html-viewer-nav">', rendered)
