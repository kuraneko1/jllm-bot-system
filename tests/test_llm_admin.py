import asyncio
import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from aiohttp import ClientError, web
from jllm_plugins.llm import llm_backend
from jllm_plugins.llm.router import Plugin


class AdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(llm_backend._reset_config)
        self.root = Path(self.temp.name)
        self.ctx = SimpleNamespace(config={"backend": "sarashina"},
                                   dependencies={"storage.local": SimpleNamespace(
                                       legacy_path=lambda name: self.root / name)}, scope=Mock())
        self.client = (await Plugin().setup(self.ctx))["llm.chat"]

    async def asyncTearDown(self):
        await self.client.close()

    async def test_update_persists_securely_and_restarts_without_selecting(self):
        status = await self.client.admin_update({
            "backend": "openai",
            "api_url": "https://api.example/v1",
            "model": "chat",
            "translate_model": "translate",
            "api_key": "secret-value",
        })
        self.assertEqual(status["backend"], "sarashina")
        self.assertTrue(status["backends"]["openai"]["key_configured"])
        self.assertNotIn("secret-value", repr(status))
        file = self.root / "llm_provider_config.json"
        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        saved = json.loads(file.read_text())
        self.assertEqual(saved["backends"]["openai"]["api_key"], "secret-value")
        restored = (await Plugin().setup(self.ctx))["llm.chat"]
        self.assertEqual(restored.admin_status(), status)
        await restored.admin_update({"backend": "openai", "model": "new"})
        self.assertTrue(restored.admin_status()["backends"]["openai"]["key_configured"])
        await restored.admin_update({"backend": "openai", "api_key": ""})
        self.assertFalse(restored.admin_status()["backends"]["openai"]["key_configured"])
        await restored.close()

    async def test_validation_and_save_failure_preserve_runtime(self):
        await self.client.admin_update({"backend": "sarashina", "api_url": "https://example.com",
                                        "api_key": "secret"})
        old = self.client.inner
        before = self.client.admin_status()
        for update in [
            {"backend": "sarashina", "api_url": "https://elsewhere.example"},
            {"backend": "openai", "api_url": "https://user:secret@example.com"},
            {"backend": "openai", "api_url": "https://example.com/secret"},
            {"backend": "openai", "api_url": "https://example.com/?key=secret"},
            {"backend": "openai", "api_url": "http://example.com", "api_key": "secret"},
            {"backend": "unknown", "api_key": "secret"},
        ]:
            with self.subTest(update=update), self.assertRaises(ValueError) as caught:
                await self.client.admin_update(update)
            self.assertNotIn('secret', str(caught.exception))
            self.assertEqual(self.client.admin_status(), before)
        with patch('jllm_plugins.llm.admin_config.os.replace', side_effect=OSError('secret')):
            with self.assertRaises(ValueError) as caught:
                await self.client.admin_update({"backend": "sarashina", "model": "other"})
        self.assertNotIn('secret', str(caught.exception))
        self.assertIs(old, self.client.inner)
        self.assertEqual(self.client.admin_status(), before)
        self.assertFalse(list(self.root.glob('.llm-config-*')))

    async def test_openai_chat_model_url_and_auth(self):
        seen = []
        async def handler(request):
            seen.append((request.path, request.headers.get('Authorization'), await request.json()))
            return web.json_response(
                {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
            )
        app = web.Application()
        app.router.add_post('/v1/chat/completions', handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        self.addAsyncCleanup(runner.cleanup)
        port = site._server.sockets[0].getsockname()[1]
        await self.client.admin_update({
            "backend": "openai",
            "api_url": f"http://127.0.0.1:{port}/v1",
            "api_key": "testkey",
            "model": "chat-model",
            "translate_model": "translation-model",
        })
        with llm_backend.channel_context(5):
            llm_backend.set_channel(5, 'openai')
            self.assertEqual(
                await self.client.complete([{"role": "user", "content": "hello"}]), 'ok'
            )
            self.assertEqual(await self.client.translate('hello'), 'ok')
        self.assertEqual([item[2]['model'] for item in seen], ['chat-model', 'translation-model'])
        self.assertTrue(
            all(
                item[0] == '/v1/chat/completions' and item[1] == 'Bearer testkey'
                for item in seen
            )
        )
        self.assertNotIn('repeat_penalty', seen[0][2])
        for _, _, body in seen:
            self.assertNotIn('max_tokens', body)
            self.assertNotIn('max_completion_tokens', body)

    async def test_sarashina_rate_state_survives_update_and_keys_are_separate(self):
        old = self.client.inner
        old.requests_per_minute = old.translate_rpm = 1
        old.window = 1
        old.interval_margin = 0.5
        await old._acquire('same-model', credential='Bearer same')
        await self.client.admin_update({'backend': 'sarashina', 'model': 'changed'})
        new = self.client.inner
        self.assertIs(old._rate_limiter, new._rate_limiter)
        # 新設定が無制限でも旧設定の直前の間隔を守る。
        with self.assertRaises(ValueError):
            await new._acquire('same-model', max_wait=0, credential='Bearer same')
        await new._acquire('same-model', max_wait=0, credential='Bearer different')
        await new._acquire('different-model', max_wait=0, credential='Bearer same')
        await new._acquire('same-model', max_wait=0, credential='')

    async def test_rate_limiter_concurrency_cancellation_and_rpm_change(self):
        client = self.client.inner
        client.window, client.interval_margin = 1, 0.1
        client.requests_per_minute = client.translate_rpm = 1
        await client._acquire('m', credential='key')
        waiting = asyncio.create_task(client._acquire('m', credential='key'))
        await asyncio.sleep(0.01)
        with self.assertRaises(ValueError):
            await client._acquire('m', max_wait=0.01, credential='key')
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        # キャンセルが後続に架空の予約を残さない。
        await asyncio.sleep(0.1)
        await client._acquire('m', max_wait=0.05, credential='key')
        # RPM が低くなる場合、新しい長い間隔を適用する。
        client.requests_per_minute = client.translate_rpm = 1
        client.interval_margin = 1
        with self.assertRaises(ValueError):
            await client._acquire('m', max_wait=0.01, credential='key')

    async def test_old_generation_cannot_weaken_new_rate(self):
        limiter = self.client.inner._rate_limiter
        await limiter.acquire('key', 'm', 0, 0, 1)
        await limiter.acquire('key', 'm', 0.04, 0.2, 2)
        await limiter.acquire('key', 'm', 0, 0.2, 1)
        with self.assertRaises(ValueError):
            await limiter.acquire('key', 'm', 0, 0, 1)
        await asyncio.sleep(0.05)
        await limiter.acquire('key', 'm', 0, 0, 2)
        await limiter.acquire('key', 'm', 0, 0, 2)

    async def test_update_waits_for_selection_lock(self):
        async with self.client._admin_lock:
            task = asyncio.create_task(
                self.client.admin_update({'backend': 'openai', 'model': 'locked'})
            )
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertNotEqual(self.client.admin_status()['backends']['openai']['model'], 'locked')
        await task
        self.assertEqual(self.client.admin_status()['backends']['openai']['model'], 'locked')

    async def test_redirect_does_not_forward_authorization(self):
        seen = []
        async def redirect(request):
            raise web.HTTPFound('/stolen')
        async def stolen(request):
            seen.append(request.headers.get('Authorization'))
            return web.json_response({})
        app = web.Application()
        app.router.add_post('/v1/chat/completions', redirect)
        app.router.add_route('*', '/stolen', stolen)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        self.addAsyncCleanup(runner.cleanup)
        port = site._server.sockets[0].getsockname()[1]
        await self.client.admin_update({'backend': 'openai',
            'api_url': f'http://127.0.0.1:{port}', 'api_key': 'testkey'})
        with llm_backend.channel_context(4):
            llm_backend.set_channel(4, 'openai')
            with self.assertRaises((KeyError, ClientError)):
                await self.client.complete([{'role': 'user', 'content': 'hi'}])
        self.assertEqual(seen, [])

    async def test_active_request_survives_reconfiguration(self):
        begun, finish = asyncio.Event(), asyncio.Event()
        class Fake:
            def __init__(self, name, config):
                self.name, self.closed = name, False
            async def complete(self, *args, **kwargs):
                begun.set()
                await finish.wait()
                self.assert_open()
                return 'ok'
            def assert_open(self):
                if self.closed:
                    raise AssertionError('closed while in use')
            async def close(self):
                self.closed = True
        old = self.client.inner
        with patch('jllm_plugins.llm.router.build_translation_client', side_effect=Fake):
            await self.client.admin_update({'backend': 'sarashina', 'model': 'first'})
            old = self.client.inner
            task = asyncio.create_task(self.client.complete([]))
            await begun.wait()
            await self.client.admin_update({'backend': 'sarashina', 'model': 'second'})
            self.assertFalse(old.closed)
            finish.set()
            self.assertEqual(await task, 'ok')
            self.assertTrue(old.closed)
