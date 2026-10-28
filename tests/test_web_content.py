import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
from jllm_plugins.conversation.web_content import (
    PublicResolver,
    WebContentError,
    WebContentFetcher,
    extract_html,
    extract_urls,
    validate_url,
)
from yarl import URL

ARTICLE = (
    "<html><head><title>試験記事</title></head><body><nav>ナビゲーション広告</nav><main><h1>記事の題名</h1><p>"
    + "担当は佐藤さん、予算は10万円。" * 10
    + "</p><ul><li>期限は金曜日</li><li>品質確認を行う</li></ul>"
    + "<script>secret script text</script><footer>広告</footer></main></body></html>"
)


class ExtractionTests(unittest.TestCase):
    def test_extraction_preserves_headings_lists_and_excludes_chrome(self):
        title, text = extract_html(ARTICLE)
        self.assertEqual(title, "試験記事")
        self.assertIn("# 記事の題名", text)
        self.assertIn("- 期限は金曜日", text)
        self.assertNotIn("ナビゲーション", text)
        self.assertNotIn("secret script", text)
        self.assertNotIn("広告", text)
        _, semantic_text = extract_html(
            ARTICLE.replace("<main>", '<div role="main">')
            .replace("</main>", "</div>")
            .replace("<nav>", '<div class="related">')
            .replace("</nav>", "</div>")
        )
        self.assertIn("佐藤", semantic_text)
        self.assertNotIn("ナビゲーション", semantic_text)

    def test_url_extraction_deduplicates_and_handles_markdown_and_parentheses(self):
        text = "[記事](https://example.com/a_(b)) <https://example.com/x?q=1> https://example.com/x?q=1。"
        self.assertEqual(
            extract_urls(text), ["https://example.com/a_(b)", "https://example.com/x?q=1"]
        )

    def test_internal_urls_credentials_schemes_and_ports_rejected(self):
        for value in (
            "http://127.0.0.1",
            "http://192.168.0.22",
            "http://100.70.56.109",
            "http://169.254.169.254",
            "http://[::1]",
            "http://[::ffff:127.0.0.1]",
            "ftp://example.com",
            "https://user:password@example.com",
            "https://example.com:8092",
        ):
            with self.subTest(value=value), self.assertRaises(WebContentError):
                validate_url(value)
        self.assertEqual(
            str(validate_url("https://example.com/path#anchor")), "https://example.com/path"
        )

    def test_shift_jis_html_and_empty_script_only_page(self):
        html = ARTICLE.replace("<head>", '<head><meta charset="shift_jis">').encode("shift_jis")
        title, text = extract_html(html)
        self.assertEqual(title, "試験記事")
        self.assertIn("佐藤", text)
        with self.assertRaises(WebContentError):
            extract_html("<html><body><script>large content in JS</script></body></html>")


class FetchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.private_hits = 0

        async def article(request):
            return web.Response(text=ARTICLE, content_type="text/html")

        async def binary(request):
            return web.Response(body=b"PDF", content_type="application/pdf")

        async def blocked(request):
            return web.Response(status=403)

        async def redirect(request):
            raise web.HTTPFound("http://192.168.0.22:8088/v1/models")

        async def large(request):
            return web.Response(text="x" * 3000, content_type="text/plain")

        app = web.Application()
        for route, handler in [
            ("/article", article),
            ("/binary", binary),
            ("/blocked", blocked),
            ("/redirect", redirect),
            ("/large", large),
        ]:
            app.router.add_get(route, handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        await self.runner.cleanup()

    def route_fixture(self, url):
        # Only the test public host is redirected to a local HTTP fixture;
        # redirects returned by that fixture use the real production validator.
        if url.startswith("https://fixture.example/"):
            return URL(url.replace("https://fixture.example", f"http://127.0.0.1:{self.port}"))
        return validate_url(url)

    async def test_real_download_and_extraction_contract(self):
        with patch(
            "jllm_plugins.conversation.web_content.validate_url",
            side_effect=self.route_fixture,
        ):
            page = await WebContentFetcher().fetch("https://fixture.example/article")
        self.assertEqual(page["title"], "試験記事")
        self.assertIn("10万円", page["content"])
        self.assertFalse(page["truncated"])

    async def test_redirect_to_internal_host_rejected_before_connection(self):
        with (
            patch(
                "jllm_plugins.conversation.web_content.validate_url",
                side_effect=self.route_fixture,
            ),
            self.assertRaises(WebContentError),
        ):
            await WebContentFetcher().fetch("https://fixture.example/redirect")

    async def test_status_binary_and_download_limits(self):
        with patch(
            "jllm_plugins.conversation.web_content.validate_url",
            side_effect=self.route_fixture,
        ):
            for path in ("binary", "blocked"):
                with self.subTest(path=path), self.assertRaises(WebContentError):
                    await WebContentFetcher().fetch("https://fixture.example/" + path)
            with (
                patch("jllm_plugins.conversation.web_content.MAX_BYTES", 1000),
                self.assertRaises(WebContentError),
            ):
                await WebContentFetcher().fetch("https://fixture.example/large")

    async def test_dns_private_or_mixed_answers_rejected(self):
        resolver = PublicResolver()
        try:
            with patch.object(
                resolver.resolver,
                "resolve",
                new=AsyncMock(return_value=[{"host": "8.8.8.8"}, {"host": "127.0.0.1"}]),
            ):
                with self.assertRaises(WebContentError):
                    await resolver.resolve("example.test")
        finally:
            await resolver.close()
