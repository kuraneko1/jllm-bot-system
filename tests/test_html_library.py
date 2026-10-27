import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from jllm_plugins.html_library.store import HtmlConfig, HtmlStore
from jllm_plugins.storage.plugin import LocalStorage

from tests.test_reader_core import ReaderCoreTests, message


class HtmlStoreTests(unittest.TestCase):
    def test_publish_dashboard_and_wrapper(self):
        with __import__("tempfile").TemporaryDirectory() as temp:
            store = HtmlStore(LocalStorage(temp), HtmlConfig())
            request_id = store.register(
                b"<html><head><title>Example</title></head><body>Body</body></html>",
                "example.html",
                SimpleNamespace(id=42, display_name="tester"),
            )
            result = store.publish(request_id)
            self.assertIn("HTMLを登録しました", result)
            self.assertEqual(store.catalog[0]["title"], "Example")
            self.assertTrue((store.html_dir / "users" / "42" / request_id / "index.html").is_file())
            self.assertIn("HTML Dashboard", (store.html_dir / "index.html").read_text())
            wrapper = (store.html_dir / "users" / "42" / request_id / "index.html").read_text()
            self.assertIn("html-viewer-nav", wrapper)
            self.assertIn("Example", wrapper)

    def test_reject_and_expire_remove_pending(self):
        with __import__("tempfile").TemporaryDirectory() as temp:
            store = HtmlStore(LocalStorage(temp), HtmlConfig())
            request_id = store.register(b"data", "x.html", SimpleNamespace(id=1, display_name="x"))
            path = store.pending[request_id]["path"]
            store.reject(request_id)
            self.assertFalse(Path(path).exists())
            request_id = store.register(b"data", "x.html", SimpleNamespace(id=1, display_name="x"))
            path = store.pending[request_id]["path"]
            store.expire(request_id)
            self.assertFalse(Path(path).exists())


class ReaderHtmlTests(ReaderCoreTests):
    def setUp(self):
        super().setUp()
        self.store = HtmlStore(LocalStorage(self.root), HtmlConfig(require_approval=False))
        self.cog.html_store = self.store
        self.cog.html_dir = self.store.html_dir
        self.cog.pending_html = self.store.pending
        self.cog.html_catalog = self.store.catalog

    async def test_html_registration_publishes(self):
        attachment = SimpleNamespace(
            content_type="text/html",
            filename="example.html",
            size=100,
            read=AsyncMock(return_value=b"<title>Example</title><p>body</p>"),
        )
        call = message("<@999> html")
        call.mentions = [self.cog.bot.user]
        call.attachments = [attachment]
        call.reply = AsyncMock()
        self.assertTrue(await self.cog._handle_html_registration(call))
        self.assertEqual(len(self.store.catalog), 1)
        call.reply.assert_awaited_once()
        self.assertIn("HTMLを登録しました", call.reply.await_args.args[0])

    async def test_html_approval_view_publishes(self):
        self.store.config = HtmlConfig(require_approval=True, approver_id=99)
        attachment = SimpleNamespace(
            content_type="text/html",
            filename="example.html",
            size=100,
            read=AsyncMock(return_value=b"<title>Example</title>"),
        )
        call = message("<@999> html")
        call.mentions = [self.cog.bot.user]
        call.attachments = [attachment]
        call.reply = AsyncMock()
        self.assertTrue(await self.cog._handle_html_registration(call))
        request_id = next(iter(self.store.pending))
        view = __import__(
            "jllm_plugins.reader.media", fromlist=["HtmlApprovalView"]
        ).HtmlApprovalView(self.cog, request_id)
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=99),
            response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()),
        )
        await view.approve.callback(interaction)
        self.assertEqual(len(self.store.catalog), 1)


if __name__ == "__main__":
    unittest.main()
