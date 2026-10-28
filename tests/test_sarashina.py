# ruff: noqa: E501, B017
"""Sarashina バックエンド（クラウド）の契約。

llama.cpp と違い ``/apply-template`` ``/tokenize`` が無い・``max_tokens`` が
未対応フィールド（400）という違いがあるため、送信内容を固定する。
"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from jllm_plugins.llm import llm_backend
from jllm_plugins.llm.translation import SarashinaClient, build_translation_client
from jllm_plugins.llm.translation_control import (
    NoTranslationController,
    build_translation_controller,
)

OK = {"choices": [{"message": {"content": "こんにちは", "role": "assistant"}, "finish_reason": "stop"}]}


class SarashinaClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.responses = []

        async def handler(request):
            self.requests.append((dict(request.headers), await request.json()))
            if self.responses:
                status, payload = self.responses.pop(0)
                return web.json_response(payload, status=status)
            return web.json_response(OK)

        app = web.Application()
        app.router.add_post("/v1/chat/completions", handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        key = Path(self.directory.name) / "key"
        key.write_text("secret-key\n")

        self.client = SarashinaClient(f"http://127.0.0.1:{port}", str(key), "sarashina3-mini")
        self.client.retry_delay = 0.01
        # 本番の429待ちは8秒（Retry-Afterが無いため）。テストでは縮める。
        self.client.rate_limit_delay = 0.01
        self.client.rate_limit_max_delay = 0.05

    async def asyncTearDown(self):
        await self.client.close()
        await self.runner.cleanup()

    async def test_translate_sends_documented_fields_only(self):
        self.assertEqual(await self.client.translate("Hello"), "こんにちは")
        headers, body = self.requests[0]
        self.assertEqual(headers.get("Authorization"), "Bearer secret-key")
        self.assertEqual(body["model"], "sarashina3-mini")
        self.assertEqual(body["messages"][1]["content"], "Hello")
        self.assertFalse(body["stream"])
        # max_tokens は未対応フィールドで 400 になるため送らない。
        self.assertNotIn("max_tokens", body)
        self.assertEqual(body["max_completion_tokens"], 1536)

    async def test_length_and_empty_are_rejected(self):
        self.responses.append((200, {"choices": [{"message": {"content": "途中"}, "finish_reason": "length"}]}))
        with self.assertRaises(ValueError):
            await self.client.translate("Hello")
        self.responses.append((200, {"choices": [{"message": {"content": "  "}, "finish_reason": "stop"}]}))
        with self.assertRaises(ValueError):
            await self.client.translate("Hello")

    async def test_complete_caps_output_at_api_limit(self):
        self.assertEqual(await self.client.complete([{"role": "user", "content": "hi"}], max_tokens=9999), "こんにちは")
        self.assertEqual(self.requests[0][1]["max_completion_tokens"], 8000)

    async def test_retries_rate_limit_then_succeeds(self):
        self.responses.append((429, {"error": "rate limited"}))
        self.assertEqual(await self.client.translate("Hello"), "こんにちは")
        self.assertEqual(len(self.requests), 2)

    async def test_client_error_is_not_retried(self):
        self.responses.append((401, {"error": "unauthorized"}))
        with self.assertRaises(Exception):
            await self.client.translate("Hello")
        self.assertEqual(len(self.requests), 1)

    async def test_prompt_tokens_is_an_estimate(self):
        short = await self.client.prompt_tokens([{"role": "user", "content": "あ" * 100}])
        long = await self.client.prompt_tokens([{"role": "user", "content": "あ" * 1000}])
        self.assertGreater(long, short)
        self.assertEqual(self.client.context_window, 128000)

    async def test_complete_rejects_over_context(self):
        with self.assertRaises(ValueError):
            await self.client.complete([{"role": "user", "content": "あ" * (128000 * 4)}])


class BackendSelectionTests(unittest.TestCase):
    """/llm switch の保存（data/llm_backend.json）に影響されないこと。"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        # 稼働中の選択が残っていても、この既定値だけを見る。
        llm_backend._reset_config()
        llm_backend.configure_state(Path(self.directory.name) / "llm_backend.json")
        self.addCleanup(llm_backend._reset_config)

    def test_sarashina_backend_builds_cloud_client_and_no_controller(self):
        client = build_translation_client(config={"key_file": "/tmp/key", "model": "sarashina3-nano"})
        controller = build_translation_controller()
        self.assertIsInstance(client, SarashinaClient)
        self.assertEqual(client.model, "sarashina3-nano")
        self.assertEqual(client.context_window, 128000)
        self.assertIsInstance(controller, NoTranslationController)

    def test_local_backend_keeps_llama_cpp(self):
        with patch.dict("os.environ", {"TRANSLATION_BACKEND": "local"}):
            client = build_translation_client()
        self.assertEqual(client.context_window, 4096)
        self.assertTrue(hasattr(client, "prompt_tokens"))


class NoControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_controller_accepts_everything(self):
        self.assertTrue(await NoTranslationController().set_enabled(True))
        self.assertTrue(await NoTranslationController().set_enabled(False))


class RateLimitTests(unittest.IsolatedAsyncioTestCase):
    """RPM 上限に達しても投稿を捨てず、回復を待って処理する。"""

    async def asyncSetUp(self):
        self.calls = []
        self.plan = []  # (status, headers) を順に返す。尽きたら 200。
        self.clients = []

        async def handler(request):
            await request.json()
            self.calls.append(1)
            if self.plan:
                status, headers = self.plan.pop(0)
                return web.json_response({"error": "limited"}, status=status, headers=headers)
            return web.json_response(OK)

        app = web.Application()
        app.router.add_post("/v1/chat/completions", handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()
        await self.runner.cleanup()

    def client(self, **kwargs):
        retry_delay = kwargs.pop("retry_delay", None)
        client = SarashinaClient(self.url, "", "sarashina3-mini", **kwargs)
        if retry_delay is not None:
            client.retry_delay = retry_delay
        self.clients.append(client)
        return client

    async def test_pacing_spaces_requests(self):
        # window/rpm = 0.15秒間隔。初回は即時、以降は間隔を空ける。
        client = self.client(requests_per_minute=2, window=0.3, max_wait=5)
        started = time.monotonic()
        for _ in range(3):
            await client.translate("Hello")
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.25)
        self.assertEqual(client.waits, 2)
        self.assertEqual(len(self.calls), 3)

    async def test_gives_up_when_the_wait_is_longer_than_allowed(self):
        client = self.client(requests_per_minute=1, window=60, max_wait=0.05)
        await client.translate("Hello")
        with self.assertRaises(ValueError):
            await client.translate("Hello")
        self.assertEqual(len(self.calls), 1)

    async def test_conversation_gives_up_earlier_than_translation(self):
        client = self.client(requests_per_minute=1, window=60, max_wait=120)
        client.interactive_max_wait = 0.05
        await client.complete([{"role": "user", "content": "hi"}])
        with self.assertRaises(ValueError):
            await client.complete([{"role": "user", "content": "hi"}])

    async def test_retry_after_is_honored_then_succeeds(self):
        client = self.client(max_wait=5, retry_delay=0.001)
        self.plan.append((429, {"Retry-After": "0.2"}))
        started = time.monotonic()
        self.assertEqual(await client.translate("Hello"), "こんにちは")
        self.assertGreaterEqual(time.monotonic() - started, 0.15)
        self.assertEqual(len(self.calls), 2)

    async def test_repeated_429_stops_at_the_wait_budget(self):
        client = self.client(max_wait=0.3, retry_delay=0.001)
        self.plan.extend([(429, {"Retry-After": "0.5"})] * 3)
        with self.assertRaises(ValueError):
            await client.translate("Hello")
        self.assertEqual(len(self.calls), 1)

    async def test_server_error_is_retried(self):
        client = self.client(max_wait=5, retry_delay=0.001)
        self.plan.append((503, {}))
        self.assertEqual(await client.translate("Hello"), "こんにちは")
        self.assertEqual(len(self.calls), 2)


class ConversationThresholdTests(unittest.IsolatedAsyncioTestCase):
    async def test_sarashina_window_keeps_the_capped_source_intact(self):
        """128k あるので、48,000文字の上限までなら中間要約しない。"""
        from jllm_plugins.conversation.conversation import MAX_SOURCE_CHARS, ConversationService

        client = SarashinaClient("http://127.0.0.1:1", "", "sarashina3-mini")
        service = ConversationService(client)
        source = "あ" * MAX_SOURCE_CHARS
        # しきい値（文脈の58.6%＝約75,000トークン）に達しないため API を呼ばずに返る。
        self.assertEqual(await service.compress(source, lambda: True), source)

    async def test_local_window_thresholds_match_the_old_numbers(self):
        from jllm_plugins.conversation.conversation import (
            CHUNK_RATIO,
            COMPRESS_RATIO,
            FITS_RATIO,
            RETRY_RATIO,
        )

        window = 4096
        self.assertEqual(int(window * FITS_RATIO), 2400)
        self.assertEqual(int(window * CHUNK_RATIO), 2801)
        self.assertEqual(int(window * COMPRESS_RATIO), 2998)
        self.assertEqual(int(window * RETRY_RATIO), 3198)


if __name__ == "__main__":
    unittest.main()
