import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from jllm_plugins.llm import llm_backend
from jllm_plugins.llm.translation import SwitchableClient, TranslationSettings
from jllm_plugins.translation.cog import TranslationCog


class AdminSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(llm_backend._reset_config)
        llm_backend.configure_state(Path(self.tmp.name) / "backend.json", "sarashina")
        self.client = SwitchableClient(factory=lambda name: SimpleNamespace(close=AsyncMock()))
        self.cog = TranslationCog(
            None, settings=TranslationSettings(Path(self.tmp.name) / "settings.json"),
            client=self.client,
            controller=SimpleNamespace(set_enabled=AsyncMock(), manages_process=False),
            talk_log=SimpleNamespace(flush=lambda: None), sessions=object(), kitasan=None,
        )

    async def test_switch_preserves_read_mode(self):
        llm_backend.set_selection("sarashina", "tts")
        result = await self.cog.admin_select("atmos")
        self.assertEqual(result["backend"], "atmos")
        self.assertEqual(result["read_mode"], "tts")
        self.assertEqual(self.client.backend, "atmos")
        self.assertFalse(self.cog.manages_process)

    async def test_idle_selects_local_without_process_operation(self):
        result = await self.cog.admin_select("local")
        self.assertEqual(result["global_backend"], "local")
        self.assertTrue(self.cog.manages_process)
        with self.assertRaises(RuntimeError):
            await self.cog.admin_select("openai")
        self.assertEqual(self.client.backend, "local")

    async def test_local_and_local_overrides_are_rejected(self):
        self.cog.settings.change(1, 2, True, [3])
        with self.assertRaises(RuntimeError):
            await self.cog.admin_select("local")
        llm_backend.set_channel(123, "local")
        with self.assertRaises(RuntimeError):
            await self.cog.admin_select("openai")
        self.assertEqual(self.client.backend, "sarashina")
        self.assertEqual(llm_backend.snapshot()["global_backend"], "sarashina")

    async def test_cancelled_switch_finishes_before_releasing_lock(self):
        entered, release = asyncio.Event(), asyncio.Event()
        old = self.client._clients["sarashina"]

        async def close():
            entered.set()
            await release.wait()

        old.close = close
        task = asyncio.create_task(self.cog.admin_select("atmos"))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertTrue(self.cog.control_lock.locked())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.client.backend, "atmos")
        self.assertEqual(llm_backend.snapshot()["global_backend"], "atmos")
        self.assertFalse(self.cog.manages_process)

    async def test_shared_admin_lock_blocks_selection(self):
        self.client._admin_lock = asyncio.Lock()
        async with self.client._admin_lock:
            task = asyncio.create_task(self.cog.admin_select("openai"))
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertEqual(self.client.backend, "sarashina")
        await task
        self.assertEqual(self.client.backend, "openai")

    async def test_invalid_selection_does_not_change_state(self):
        with self.assertRaises(ValueError):
            await self.cog.admin_select("unknown")
        self.assertEqual(self.client.backend, "sarashina")
