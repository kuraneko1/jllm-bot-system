"""翻訳・会話のバックエンド切替（``/llm``）と atmos クライアントの契約。

- 選んだバックエンドと読み上げモードは ``data/llm_backend.json`` に残る
- ``atmos``（z170 の gemma4-atmos-sft）は llama.cpp の OpenAI 互換 API
- 読み上げモードはバックエンドと独立で、切り替えても変わらない
"""

import asyncio
import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from jllm_plugins.llm import llm_backend
from jllm_plugins.llm.translation import (
    AtmosClient,
    SarashinaClient,
    SwitchableClient,
    build_translation_client,
)
from jllm_plugins.llm.translation_control import (
    NoTranslationController,
    TranslationController,
    build_translation_controller,
)

OK = {
    "choices": [
        {"message": {"content": "こんにちは", "role": "assistant"}, "finish_reason": "stop"}
    ]
}


class BackendStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "llm_backend.json"
        patcher = patch.dict("os.environ", {"LLM_BACKEND_PATH": str(self.path)})
        patcher.start()
        self.addCleanup(patcher.stop)
        llm_backend._reset_config()
        self.addCleanup(llm_backend._forget)

    def test_default_comes_from_the_environment_value(self):
        with patch.dict("os.environ", {"TRANSLATION_BACKEND": "sarashina"}):
            snapshot = llm_backend.snapshot()

        self.assertEqual(snapshot["backend"], "sarashina")
        self.assertEqual(snapshot["read_mode"], "tts")
        self.assertEqual(snapshot["source"], "env")
        self.assertFalse(self.path.exists())

    def test_selection_is_saved_and_survives_a_reload(self):
        with patch.dict("os.environ", {"TRANSLATION_BACKEND": "sarashina"}):
            llm_backend.set_selection("atmos")
            llm_backend._reset_config()
            snapshot = llm_backend.snapshot()

        self.assertEqual(snapshot["backend"], "atmos")
        self.assertEqual(snapshot["source"], "file")
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["backend"], "atmos")
        self.assertEqual(saved["read_mode"], "tts")
        self.assertEqual(saved["version"], 1)

    def test_backend_switch_keeps_the_reading_mode(self):
        llm_backend.set_selection("atmos")
        self.assertEqual(llm_backend.active_read_mode(), "tts")

        llm_backend.set_selection(read_mode="wav")
        llm_backend.set_selection("sarashina")
        self.assertEqual(llm_backend.snapshot()["backend"], "sarashina")
        self.assertEqual(llm_backend.active_read_mode(), "wav")

    def test_every_read_mode_is_accepted(self):
        for mode in ("tts", "wav", "none"):
            with self.subTest(mode=mode):
                llm_backend.set_selection(read_mode=mode)
                self.assertEqual(llm_backend.active_read_mode(), mode)

    def test_read_mode_labels_cover_three_modes(self):
        self.assertEqual(llm_backend.read_mode_label("tts"), "TTSで読み上げ")
        self.assertEqual(llm_backend.read_mode_label("wav"), "辞書wavのみ（TTSなし）")
        self.assertEqual(llm_backend.read_mode_label("none"), "読み上げなし")

    def test_read_mode_can_be_overridden_without_touching_the_backend(self):
        llm_backend.set_selection("atmos", "tts")
        snapshot = llm_backend.snapshot()
        self.assertEqual(snapshot["backend"], "atmos")
        self.assertEqual(snapshot["read_mode"], "tts")

        llm_backend.set_selection(read_mode="wav")
        snapshot = llm_backend.snapshot()
        self.assertEqual(snapshot["backend"], "atmos")
        self.assertEqual(snapshot["read_mode"], "wav")

    def test_unknown_values_are_rejected(self):
        with self.assertRaises(ValueError):
            llm_backend.set_selection("gpt-9")

        with self.assertRaises(ValueError):
            llm_backend.set_selection("sarashina", "video")

    def test_broken_file_falls_back_to_the_environment(self):
        self.path.write_text("{not json", encoding="utf-8")
        llm_backend._reset_config()

        with patch.dict("os.environ", {"TRANSLATION_BACKEND": "local"}):
            snapshot = llm_backend.snapshot()

        self.assertEqual(snapshot["backend"], "local")
        self.assertEqual(snapshot["source"], "env")

    def test_unknown_stored_backend_is_ignored(self):
        self.path.write_text('{"backend": "removed", "read_mode": "wav"}', encoding="utf-8")
        llm_backend._reset_config()

        with patch.dict("os.environ", {"TRANSLATION_BACKEND": "sarashina"}):
            snapshot = llm_backend.snapshot()

        self.assertEqual(snapshot["backend"], "sarashina")
        self.assertEqual(snapshot["read_mode"], "tts")


class BackendFactoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        # configure_state() は _reset_config() が消すので、リセット之后再設定する。
        # 旧 repo の config.LLM_BACKEND_PATH patch と同じく既定（data/）へ触らせない。
        llm_backend._reset_config()
        self.addCleanup(llm_backend._reset_config)
        llm_backend.configure_state(Path(self.directory.name) / "llm_backend.json")
        self.addCleanup(llm_backend._forget)

    def test_atmos_builds_the_z170_client_without_a_process(self):
        client = build_translation_client(
            "atmos",
            {
                "api_url": "http://127.0.0.1:8123",
                "model": "gemma4-atmos-sft",
                "context": 8192,
                "max_tokens": 1024,
            },
        )

        self.assertIsInstance(client, AtmosClient)
        self.assertEqual(client.model, "gemma4-atmos-sft")
        self.assertEqual(client.context_window, 8192)
        self.assertIsInstance(build_translation_controller("atmos"), NoTranslationController)

    def test_only_local_manages_a_process(self):
        self.assertIsInstance(build_translation_controller("local"), TranslationController)
        self.assertIsInstance(build_translation_controller("sarashina"), NoTranslationController)

    def test_selection_is_what_the_factory_uses(self):
        llm_backend.set_selection("atmos")

        self.assertIsInstance(
            build_translation_client(config={"api_url": "http://127.0.0.1:8123"}), AtmosClient
        )


class AtmosClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.responses = []
        self.healthy = True
        self.deltas = ["こん", "にちは"]

        async def handler(request):
            self.requests.append(await request.json())
            if self.responses:
                status, payload = self.responses.pop(0)
                if isinstance(payload, web.StreamResponse):
                    return payload
                return web.json_response(payload, status=status)
            return web.json_response(OK)

        async def health(request):
            if not self.healthy:
                return web.json_response({"status": "loading"}, status=503)

            return web.json_response({"status": "ok"})

        async def stream_handler(request):
            self.requests.append(await request.json())
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)

            for delta in self.deltas:
                payload = {"choices": [{"delta": {"content": delta}}]}
                await response.write(f"data: {json.dumps(payload)}\n\n".encode())

            await response.write(b"data: [DONE]\n\n")
            await response.write_eof()
            return response

        app = web.Application()
        app.router.add_post("/v1/chat/completions", handler)
        app.router.add_post("/sse/v1/chat/completions", stream_handler)
        app.router.add_get("/health", health)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]

        self.client = AtmosClient(
            f"http://127.0.0.1:{port}",
            "gemma4-atmos-sft",
            timeout=5,
            max_tokens=1024,
        )
        self.client.retry_delay = 0.01

    async def asyncTearDown(self):
        await self.client.close()
        await self.runner.cleanup()

    async def test_chat_payload_matches_the_operating_notes(self):
        self.assertEqual(
            await self.client.complete([{"role": "user", "content": "やあ"}]),
            "こんにちは",
        )
        body = self.requests[0]
        self.assertEqual(body["model"], "gemma4-atmos-sft")
        self.assertEqual(body["messages"], [{"role": "user", "content": "やあ"}])
        # llama-server はサーバ既定が無制限なので必ず切る（max_tokens を送る）。
        self.assertEqual(body["max_tokens"], 768)
        self.assertNotIn("max_completion_tokens", body)
        self.assertEqual(body["repeat_last_n"], 512)
        self.assertEqual(body["repeat_penalty"], 1.15)
        self.assertEqual(body["top_p"], 0.95)
        self.assertFalse(body["stream"])
        self.assertNotIn("stop", body)

    async def test_output_is_capped_at_the_configured_limit(self):
        await self.client.complete([{"role": "user", "content": "hi"}], max_tokens=9999)
        self.assertEqual(self.requests[0]["max_tokens"], 1024)

    async def test_translate_never_sends_a_system_message(self):
        self.assertEqual(await self.client.translate("Hello"), "こんにちは")
        body = self.requests[0]
        self.assertEqual([message["role"] for message in body["messages"]], ["user"])
        self.assertTrue(body["messages"][0]["content"].endswith("Hello"))

    async def test_length_and_empty_are_rejected(self):
        self.responses.append(
            (200, {"choices": [{"message": {"content": "途中"}, "finish_reason": "length"}]})
        )
        with self.assertRaises(ValueError):
            await self.client.translate("Hello")

        self.responses.append(
            (200, {"choices": [{"message": {"content": "  "}, "finish_reason": "stop"}]})
        )
        with self.assertRaises(ValueError):
            await self.client.translate("Hello")

    async def test_complete_rejects_over_context(self):
        with self.assertRaises(ValueError):
            await self.client.complete([{"role": "user", "content": "あ" * (8192 * 4)}])

    async def test_server_error_is_retried(self):
        self.responses.append((503, {"error": "loading"}))
        self.assertEqual(
            await self.client.complete([{"role": "user", "content": "hi"}]),
            "こんにちは",
        )
        self.assertEqual(len(self.requests), 2)

    async def test_stream_yields_content_deltas(self):
        root = self.client.base_url
        self.client.base_url = root + "/sse"
        chunks = []

        async for delta in self.client.stream([{"role": "user", "content": "やあ"}]):
            chunks.append(delta)

        self.assertEqual(chunks, self.deltas)
        body = self.requests[0]
        self.assertTrue(body["stream"])
        self.assertEqual(body["max_tokens"], 768)

    async def test_health(self):
        self.assertTrue(await self.client.health())
        self.healthy = False
        self.assertFalse(await self.client.health())


class SwitchableClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_inflight_request_and_lease_survive_switch(self):
        started, release = asyncio.Event(), asyncio.Event()

        class FakeClient:
            def __init__(self, name):
                self.name, self.closed = name, False

            async def complete(self, *args, **kwargs):
                started.set()
                await release.wait()
                self.assert_open()
                return self.name

            def assert_open(self):
                if self.closed:
                    raise AssertionError('client closed during request')

            async def close(self):
                self.closed = True

        client = SwitchableClient(backend='sarashina', factory=FakeClient)
        old = client.inner
        with patch.object(llm_backend, 'stored', return_value={'channels': {}}):
            async with client.lease() as leased:
                request = asyncio.create_task(client.complete([]))
                await started.wait()
                await client.switch('atmos')
                self.assertFalse(old.closed)
                self.assertIs(leased, old)
                release.set()
                self.assertEqual(await request, 'sarashina')
                self.assertFalse(old.closed)
            self.assertTrue(old.closed)
        await client.close()

    async def test_stream_remains_open_until_generator_finishes(self):
        class FakeClient:
            def __init__(self, name):
                self.name, self.closed = name, False

            async def stream(self, *args, **kwargs):
                yield 'first'
                if self.closed:
                    raise AssertionError('stream closed during switch')
                yield 'second'

            async def close(self):
                self.closed = True

        client = SwitchableClient(backend='sarashina', factory=FakeClient)
        old = client.inner
        with patch.object(llm_backend, 'stored', return_value={'channels': {}}):
            stream = client.stream([])
            self.assertEqual(await anext(stream), 'first')
            await client.switch('atmos')
            self.assertFalse(old.closed)
            self.assertEqual(await anext(stream), 'second')
            await stream.aclose()
            self.assertTrue(old.closed)
        await client.close()

    async def test_switch_swaps_the_inner_client(self):
        built = []

        class FakeClient:
            def __init__(self, name):
                self.name = name
                self.context_window = 100
                self.closed = False

            async def translate(self, text):
                return f"{self.name}:{text}"

            async def complete(self, messages, max_tokens=768, temperature=0.5, model=None):
                return self.name

            async def prompt_tokens(self, messages):
                return 1

            async def close(self):
                self.closed = True

            async def health(self):
                return True

        def factory(name):
            built.append(name)
            return FakeClient(name)

        with patch("jllm_plugins.llm.llm_backend.active_backend", return_value="sarashina"):
            client = SwitchableClient(factory=factory)

        self.assertEqual(client.backend, "sarashina")
        self.assertEqual(await client.translate("x"), "sarashina:x")
        self.assertEqual(client.context_window, 100)

        old = client.inner
        await client.switch("atmos")
        self.assertEqual(client.backend, "atmos")
        self.assertEqual(await client.translate("x"), "atmos:x")
        self.assertTrue(old.closed)
        self.assertEqual(built, ["sarashina", "atmos"])


class FactoryMatchesSelectionTests(unittest.TestCase):
    def test_sarashina_selection_still_builds_the_cloud_client(self):
        with patch("jllm_plugins.llm.llm_backend.active_backend", return_value="sarashina"):
            client = build_translation_client(
                config={"key_file": "/tmp/key", "model": "sarashina3-mini"}
            )

        self.assertIsInstance(client, SarashinaClient)


if __name__ == "__main__":
    unittest.main()
