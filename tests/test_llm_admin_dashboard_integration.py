import asyncio
import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from jllm_plugins.dashboard.dashboard import DashboardConfig, ReadingDashboard
from jllm_plugins.dictionaries.readings import ReadingDictionary
from jllm_plugins.llm import llm_backend
from jllm_plugins.llm.router import Plugin, RoutedSwitchableClient
from jllm_plugins.translation.plugin import Plugin as TranslationPlugin

from tests.passkey_auth import ensure_admin_session


class LLMAdminDashboardIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(llm_backend._reset_config)
        self.root = Path(self.temp.name)
        self.ctx = SimpleNamespace(
            config={"backend": "openai"},
            dependencies={
                "storage.local": SimpleNamespace(legacy_path=lambda name: self.root / name)
            },
            scope=Mock(),
        )
        self.router = (await Plugin().setup(self.ctx))["llm.chat"]
        self.addAsyncCleanup(self.router.close)
        self.assertIsInstance(self.router, RoutedSwitchableClient)
        self.dashboard = ReadingDashboard(
            ReadingDictionary(str(self.root / "readings.json")), None,
            webchat_client=self.router, password_file=str(self.root / "auth.json"),
            config=DashboardConfig(webchat_dir=str(self.root / "webchat"),
                                   duck_curve_path=str(self.root / "duck.json")),
        )
        self.client = TestClient(TestServer(self.dashboard.app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.origin = str(self.client.make_url("/")).rstrip("/")
        self.headers = {"Origin": self.origin}
        self.seen = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

        async def completion(request):
            payload = await request.json()
            self.seen.append((request.path, request.headers.get("Authorization"), payload))
            if payload.get("stream"):
                response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
                await response.prepare(request)
                await response.write(b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n')
                self.started.set()
                await self.release.wait()
                await response.write(
                    b'data: {"choices":[{"delta":{"content":"second"}}]}\n\ndata: [DONE]\n\n'
                )
                await response.write_eof()
                return response
            return web.json_response(
                {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
            )

        app = web.Application()
        app.router.add_post("/v1/chat/completions", completion)
        self.upstream = TestServer(app)
        await self.upstream.start_server()
        self.addAsyncCleanup(self.upstream.close)
        self.addCleanup(self.release.set)
        self.url = str(self.upstream.make_url("/v1"))

    async def update(self, **values):
        response = await self.client.put(
            "/api/llm/settings", json={"backend": "openai", **values}, headers=self.headers,
        )
        text = await response.text()
        self.assertEqual(response.status, 200, text)
        return text

    async def test_authenticated_update_requests_and_restore(self):
        self.assertEqual((await self.client.get("/api/llm/settings")).status, 401)
        self.assertEqual((await self.client.put("/api/llm/settings", json={})).status, 401)
        self.assertEqual((await self.client.post("/api/llm/active", json={})).status, 401)
        await ensure_admin_session(self.client, self.dashboard)
        self.dashboard._passkey_origins = [(self.origin, "localhost")]
        secret = "integration-secret-key"
        previous = self.router.inner
        text = await self.update(
            api_url=self.url, model="chat-one",
            translate_model="translate-one", api_key=secret,
        )
        self.assertNotIn(secret, text)
        self.assertNotIn("api_key", text)
        self.assertIsNot(previous, self.router.inner)
        status_response = await self.client.get("/api/llm/settings")
        status_text = await status_response.text()
        self.assertEqual(status_response.status, 200)
        self.assertNotIn(secret, status_text)
        self.assertNotIn('"api_key"', status_text)
        status = json.loads(status_text)
        self.assertTrue(status["backends"]["openai"]["key_configured"])
        self.assertEqual(status["backends"]["openai"]["api_url"], self.url)
        messages = [{"role": "user", "content": "hello"}]
        self.assertEqual(await self.router.complete(messages), "ok")
        self.assertEqual(await self.router.translate("hello"), "ok")
        self.assertEqual([x[2]["model"] for x in self.seen], ["chat-one", "translate-one"])
        self.assertTrue(all(path == "/v1/chat/completions" and auth == f"Bearer {secret}"
                            for path, auth, _ in self.seen))
        path = self.root / "llm_provider_config.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(path.read_text())["backends"]["openai"]["api_key"], secret)
        restored = (await Plugin().setup(self.ctx))["llm.chat"]
        self.addAsyncCleanup(restored.close)
        self.assertEqual(restored.admin_status(), self.router.admin_status())
        self.assertEqual(await restored.complete(messages), "ok")
        self.assertEqual(self.seen[-1][0:2], ("/v1/chat/completions", f"Bearer {secret}"))
        self.assertEqual(self.seen[-1][2]["model"], "chat-one")

    async def test_select_backend_through_admin_api(self):
        plugin = TranslationPlugin()
        ctx = SimpleNamespace(config={}, scope=Mock(), dependencies={
            "storage.local": self.ctx.dependencies["storage.local"],
            "discord.gateway": SimpleNamespace(
                bot=None, add_cog=AsyncMock(return_value=lambda: None)
            ),
            "llm.chat": self.router, "feature.talk-log": SimpleNamespace(flush=lambda: None),
            "store.sessions": object(), "policy.restrictions": object(),
        })
        await plugin.setup(ctx)
        self.addAsyncCleanup(plugin.stop)
        await ensure_admin_session(self.client, self.dashboard)
        self.dashboard._passkey_origins = [(self.origin, "localhost")]
        response = await self.client.post(
            "/api/llm/active", json={"backend": "atmos"}, headers=self.headers
        )
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual((await response.json())["backend"], "atmos")
        self.assertEqual(self.router.backend, "atmos")
        self.assertEqual(llm_backend.stored()["backend"], "atmos")

    async def test_old_stream_completes_during_update(self):
        await ensure_admin_session(self.client, self.dashboard)
        self.dashboard._passkey_origins = [(self.origin, "localhost")]
        await self.update(api_url=self.url, model="old", api_key="old-key")
        old = self.router.inner
        chunks = []

        async def consume():
            async for chunk in self.router.stream([{"role": "user", "content": "hello"}]):
                chunks.append(chunk)

        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(self.started.wait(), 5)
            await self.update(model="new", api_key="new-key")
            self.assertIsNot(old, self.router.inner)
            self.assertFalse(old.session.closed)
            self.assertEqual(await self.router.complete([]), "ok")
            self.release.set()
            await asyncio.wait_for(task, 5)
        finally:
            self.release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(chunks, ["first", "second"])
        self.assertTrue(old.session.closed)
        self.assertEqual([(x[1], x[2]["model"]) for x in self.seen],
                         [("Bearer old-key", "old"), ("Bearer new-key", "new")])
        status = (await (await self.client.get("/api/llm/settings")).json())
        self.assertEqual(status["backends"]["openai"]["model"], "new")
        self.assertTrue(status["backends"]["openai"]["key_configured"])
