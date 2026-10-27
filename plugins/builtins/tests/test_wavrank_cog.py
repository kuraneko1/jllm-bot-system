import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from jllm_plugins.wavrank.cog import WavRankCog
from jllm_plugins.wavrank.service import WavRankConfig


def interaction(user_id=42):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, display_name="caller"),
        response=SimpleNamespace(
            send_message=AsyncMock(),
            defer=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )


class WavRankCogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cog = WavRankCog(SimpleNamespace(), WavRankConfig(data_dir=Path(self.temp.name)))

    def tearDown(self):
        self.temp.cleanup()

    async def test_empty_result_is_ephemeral_and_consumes_cooldown(self):
        first = interaction()
        second = interaction()
        with (
            patch("jllm_plugins.wavrank.cog.time.time", side_effect=[100.0, 120.0]),
            patch("jllm_plugins.wavrank.cog.compute", return_value={"rows": []}),
        ):
            await self.cog.wavrank.callback(self.cog, first)
            await self.cog.wavrank.callback(self.cog, second)
        first.followup.send.assert_awaited_once()
        self.assertTrue(first.followup.send.await_args.kwargs["ephemeral"])
        second.response.send_message.assert_awaited_once()
        self.assertIn("41 秒", second.response.send_message.await_args.args[0])
        self.assertEqual(self.cog.last_used[42], 100.0)

    async def test_missing_archive_and_generic_error_have_distinct_messages(self):
        for error, expected in (
            (FileNotFoundError("missing"), "書き出しデータが見つかりません"),
            (RuntimeError("boom"), "画像の作成に失敗しました"),
        ):
            with self.subTest(error=type(error).__name__):
                cog = WavRankCog(SimpleNamespace(), WavRankConfig())
                call = interaction()
                with patch("jllm_plugins.wavrank.cog.compute", side_effect=error):
                    await cog.wavrank.callback(cog, call)
                call.followup.send.assert_awaited_once()
                self.assertIn(expected, call.followup.send.await_args.args[0])
                self.assertTrue(call.followup.send.await_args.kwargs["ephemeral"])

    async def test_success_is_public_embed_with_attachment(self):
        call = interaction()
        target = SimpleNamespace(id=42, display_name="caller")
        call.user = target
        stats = {
            "rows": [("mar", 2)],
            "name": "caller",
            "fired": 1,
            "msgs": 1,
            "first": "2026-01-01",
            "last": "2026-01-02",
        }
        output = Path(self.temp.name, "rank.png")
        await asyncio.to_thread(output.write_bytes, b"fake png")
        with (
            patch("jllm_plugins.wavrank.cog.compute", return_value=stats),
            patch("jllm_plugins.wavrank.cog.render", return_value=str(output)),
        ):
            await self.cog.wavrank.callback(self.cog, call)
        kwargs = call.followup.send.await_args.kwargs
        self.assertNotIn("ephemeral", kwargs)
        self.assertEqual(kwargs["embed"].title, "caller さんの wav 呼び出しランキング")
        self.assertEqual(kwargs["file"].filename, "wavrank.png")
        kwargs["file"].close()

    async def test_close_during_compute_suppresses_response(self):
        call = interaction()
        with patch(
            "jllm_plugins.wavrank.cog.compute",
            side_effect=lambda *args, **kwargs: self.cog.cog_unload() or {"rows": []},
        ):
            await self.cog.wavrank.callback(self.cog, call)
        call.followup.send.assert_not_called()

    async def test_compute_and_render_are_limited_to_one(self):
        active = 0
        maximum = 0
        lock = threading.Lock()

        def fake_compute(*args, **kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            return {"rows": []}

        with patch("jllm_plugins.wavrank.cog.compute", side_effect=fake_compute):
            await asyncio.gather(
                self.cog.wavrank.callback(self.cog, interaction(1)),
                self.cog.wavrank.callback(self.cog, interaction(2)),
            )
        self.assertEqual(maximum, 1)

    async def test_cancelling_thread_does_not_send_followup(self):
        started = threading.Event()
        release = threading.Event()
        call = interaction()

        def fake_compute(*args, **kwargs):
            started.set()
            release.wait(1)
            return {"rows": []}

        with patch("jllm_plugins.wavrank.cog.compute", side_effect=fake_compute):
            task = asyncio.create_task(self.cog.wavrank.callback(self.cog, call))
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        call.followup.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
