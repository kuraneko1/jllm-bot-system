import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from jllm_plugins.conversation.kitasan import KitasanService, is_kitasan_trigger


class KitasanServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SimpleNamespace(complete=AsyncMock(return_value="要約1\n要約2\n要約3\n要約4"))
        self.service = KitasanService(self.client, bot_id=999)

    async def test_trigger_and_summary_use_history(self):
        async def history(limit, before=None):
            for index in range(4):
                yield SimpleNamespace(
                    id=index,
                    content=f"本文{index}",
                    author=SimpleNamespace(id=index, bot=False, display_name=f"user{index}"),
                )

        channel = SimpleNamespace(history=history)
        message = SimpleNamespace(
            id=99,
            channel=channel,
            content="今北産業",
            guild=SimpleNamespace(id=1),
            author=SimpleNamespace(id=42, bot=False),
        )
        self.assertTrue(is_kitasan_trigger("今北産業"))
        self.assertFalse(is_kitasan_trigger("今北産業です"))
        summary = await self.service.summarize_message(message)
        self.assertEqual(summary, "要約1\n要約2\n要約3")
        self.client.complete.assert_awaited_once()

    async def test_handle_replies_without_raising(self):
        message = SimpleNamespace(
            id=99,
            channel=SimpleNamespace(history=self.history),
            content="今北産業",
            guild=SimpleNamespace(id=1),
            author=SimpleNamespace(id=42, bot=False),
            reply=AsyncMock(),
        )
        await self.service.handle(message)
        message.reply.assert_awaited_once()
        self.assertEqual(message.reply.await_args.args[0], "要約1\n要約2\n要約3")

    async def history(self, limit, before=None):
        for index in range(2):
            yield SimpleNamespace(
                id=index,
                content=f"本文{index}",
                author=SimpleNamespace(id=index, bot=False, display_name=f"user{index}"),
            )


if __name__ == "__main__":
    unittest.main()
