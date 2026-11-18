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

    def test_dashboard_lists_titles_owners_and_svg_icons(self):
        import re as _re
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            store = HtmlStore(LocalStorage(temp), HtmlConfig(require_approval=False))
            for key, title in (("a", "First page"), ("b", "Second page")):
                store.publish(
                    store.register(
                        f"<title>{title}</title>".encode(),
                        f"{key}.html",
                        SimpleNamespace(id=key, display_name=f"user-{key}"),
                    )
                )
            dashboard = (store.html_dir / "index.html").read_text()
        self.assertIn('<div class="chips" id="chips">', dashboard)
        self.assertIn('data-owner="user-a"', dashboard)
        self.assertIn("First page", dashboard)
        self.assertIn("user-b", dashboard)
        self.assertIn('class="badge"', dashboard)
        self.assertIn("<svg", dashboard)
        self.assertIn('class="thumb"', dashboard)
        self.assertIn('href="/users/a/', dashboard)
        self.assertIn('id="searchInput"', dashboard)
        self.assertIn('id="sortSelect"', dashboard)
        self.assertIn('id="themeBtn"', dashboard)
        self.assertIn("::-webkit-scrollbar", dashboard)
        self.assertIn('html[data-theme="light"]', dashboard)
        self.assertIsNone(_re.search("[\U0001F300-\U0001FAFF]", dashboard))

    def test_wrapper_nav_is_sticky_and_hidden_in_previews(self):
        wrapped = HtmlStore.viewer_wrapper_html(
            "T", "1", "s", b"<html><body><p>x</p></body></html>"
        )
        self.assertIn("window.self!==window.top", wrapped)
        self.assertIn("embed=1", wrapped)
        self.assertIn("html-viewer-nav", wrapped)
        self.assertIn('<iframe class="html-viewer-frame" src="content.html"', wrapped)
        self.assertNotIn("<p>x</p>", wrapped)

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
            response=SimpleNamespace(
                defer=AsyncMock(),
                edit_message=AsyncMock(),
                send_message=AsyncMock(),
            ),
            edit_original_response=AsyncMock(),
        )
        await view.approve.callback(interaction)
        self.assertEqual(len(self.store.catalog), 1)
        interaction.response.defer.assert_awaited_once()
        interaction.edit_original_response.assert_awaited_once()
        edited = interaction.edit_original_response.await_args.kwargs["content"]
        self.assertIn("HTMLを登録しました", edited)


class HtmlPreviewAndThumbnailTests(unittest.IsolatedAsyncioTestCase):
    async def test_link_preview_tags_for_published_site_and_dashboards(self):
        import re
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        from jllm_plugins.html_library.store import HtmlConfig, HtmlStore

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = SimpleNamespace(legacy_path=lambda p: root / p)
            cfg = HtmlConfig(
                require_approval=False,
                thumbnail=False,
                html_dir="html",
                pending_dir="pending",
                catalog_file="catalog.json",
                viewer_base_url="https://html.example.test",
                dashboard_url="https://html.example.test",
            )
            store = HtmlStore(storage, cfg)
            author = SimpleNamespace(id="u1", display_name="Alice")
            req_id = store.register(b"<title>Neon Wave</title><body>Hi</body>", "wave.html", author)
            store.publish(req_id)
            site_id = store.catalog[0]["site_id"]
            site_dir = root / "html" / "users" / "u1" / site_id
            (site_dir / "thumb.jpg").write_bytes(b"jpgdata")
            store.rebuild_dashboards()

            emoji_re = re.compile(r"[\U0001F300-\U0001FAFF]")
            site_html = (site_dir / "index.html").read_text(encoding="utf-8")
            self.assertIn('<meta property="og:title" content="Neon Wave">', site_html)
            self.assertIn('<meta property="og:description" content="登録者: Alice">', site_html)
            self.assertIn(
                f'<meta property="og:image" content="https://html.example.test/users/u1/{site_id}/thumb.jpg">',
                site_html,
            )
            self.assertIn(
                f'<meta property="og:url" content="https://html.example.test/users/u1/{site_id}/">',
                site_html,
            )
            self.assertIn('<meta name="twitter:card" content="summary_large_image">', site_html)
            self.assertIn('<meta name="author" content="Alice">', site_html)

            for dash_path, expected_title, expected_url in (
                (
                    root / "html" / "index.html",
                    "HTML Dashboard",
                    "https://html.example.test/",
                ),
                (
                    root / "html" / "users" / "u1" / "index.html",
                    "Alice のHTML",
                    "https://html.example.test/users/u1/",
                ),
            ):
                dash_html = dash_path.read_text(encoding="utf-8")
                self.assertIsNone(emoji_re.search(dash_html))
                self.assertIn(f'<meta property="og:title" content="{expected_title}">', dash_html)
                self.assertIn(f'<meta property="og:url" content="{expected_url}">', dash_html)
                self.assertIn("Alice", dash_html)
                self.assertIn("Neon Wave", dash_html)
                self.assertIn(
                    f'<meta property="og:image" content="https://html.example.test/users/u1/{site_id}/thumb.jpg">',
                    dash_html,
                )
                self.assertIn('<meta name="twitter:card" content="summary_large_image">', dash_html)

    async def test_thumb_server_and_wait_page_ready(self):
        import tempfile
        from pathlib import Path

        import aiohttp
        from jllm_plugins.html_library.store import _start_thumb_server, _wait_page_ready

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "content.html").write_text("<h1>OK</h1>", encoding="utf-8")
            (root.parent / "secret.txt").write_text("no", encoding="utf-8")

            runner, url = await _start_thumb_server(root, "content.html")
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        self.assertEqual(resp.status, 200)
                        self.assertEqual(await resp.text(), "<h1>OK</h1>")
                    base = url.rsplit("/", 1)[0]
                    async with session.get(f"{base}/../secret.txt") as resp:
                        self.assertIn(resp.status, (403, 404))
            finally:
                await runner.cleanup()

        calls = 0

        async def fake_command(method, params=None):
            nonlocal calls
            calls += 1
            blocking = calls == 1
            return {"result": {"value": {"ready": True, "blocking": blocking}}}

        await _wait_page_ready(fake_command, wait=0.2)
        self.assertGreaterEqual(calls, 2)

    async def test_publish_async_waits_for_thumbnail_before_returning(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock, patch

        from jllm_plugins.html_library.store import HtmlConfig, HtmlStore

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = SimpleNamespace(legacy_path=lambda p: root / p)
            cfg = HtmlConfig(
                require_approval=False,
                thumbnail=True,
                chrome="/usr/bin/chromium",
                html_dir="html",
                pending_dir="pending",
                catalog_file="catalog.json",
                viewer_base_url="https://html.example.test",
                dashboard_url="https://html.example.test",
            )
            store = HtmlStore(storage, cfg)
            author = SimpleNamespace(id="u2", display_name="Bob")
            req_id = store.register(
                b"<title>Sunset 3D</title><body>OK</body>",
                "sunset.html",
                author,
            )

            async def fake_capture(chrome, source, out):
                out.write_bytes(b"rendered-thumb")
                return True

            with patch(
                "jllm_plugins.html_library.store.capture_thumbnail",
                new=AsyncMock(side_effect=fake_capture),
            ) as mocked_capture:
                msg = await store.publish_async(req_id)

            mocked_capture.assert_awaited_once()
            site_id = store.catalog[0]["site_id"]
            self.assertIn(f"https://html.example.test/users/u2/{site_id}/", msg)
            self.assertTrue((root / "html" / "users" / "u2" / site_id / "thumb.jpg").is_file())
            dash = (root / "html" / "index.html").read_text(encoding="utf-8")
            self.assertIn(f"/users/u2/{site_id}/thumb.jpg", dash)

            req_fail = store.register(b"<title>Retry Page</title>", "retry.html", author)
            with (
                patch(
                    "jllm_plugins.html_library.store.capture_thumbnail",
                    new=AsyncMock(side_effect=TimeoutError),
                ),
                patch.object(store, "schedule_thumbnails", new=MagicMock()) as sched,
            ):
                msg_fail = await store.publish_async(req_fail, thumbnail_wait=0.1)

            self.assertIn("HTMLを登録しました", msg_fail)
            sched.assert_called_once()


if __name__ == "__main__":
    unittest.main()
