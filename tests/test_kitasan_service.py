import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import jsonschema
from jllm_plugins.conversation.kitasan import KitasanService, is_kitasan_trigger
from jllm_plugins.conversation.kitasan_plugin import Plugin

from jllm.kernel import discover


class KitasanServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SimpleNamespace(complete=AsyncMock(return_value="要約1\n要約2\n要約3\n要約4"))
        self.service = KitasanService(self.client, bot_id=999)

    async def test_plugin_config_applies_words_and_history_limits(self):
        calls = []

        async def history(limit, before=None):
            calls.append((limit, before))
            for index in range(3):
                yield SimpleNamespace(
                    id=index, content="あ" * 90,
                    author=SimpleNamespace(id=index, bot=False, display_name="user"),
                )

        context = SimpleNamespace(
            dependencies={"llm.chat": self.client, "policy.restrictions": None},
            config={"words": ["  SUMMARY  "], "messages": 3, "item_chars": 80, "max_chars": 100},
        )
        service = (await Plugin().setup(context))["feature.kitasan"]
        self.assertTrue(service.matches_trigger(" summary "))
        self.assertFalse(service.matches_trigger("今北産業"))
        message = SimpleNamespace(channel=SimpleNamespace(history=history), guild=None)
        await service.summarize_message(message)
        self.assertEqual(calls, [(3, message)])
        prompt = self.client.complete.await_args.args[0][1]["content"]
        self.assertEqual(prompt.count("user: "), 1)
        self.assertEqual(prompt.count("あ"), 79)
        self.assertIn("…", prompt)

    async def test_disabled_and_empty_words_never_match(self):
        context = SimpleNamespace(
            dependencies={"llm.chat": self.client, "policy.restrictions": None},
            config={"enabled": False, "words": ["今北産業"]},
        )
        service = (await Plugin().setup(context))["feature.kitasan"]
        self.assertFalse(service.matches_trigger("今北産業"))
        context.config = {"words": []}
        service = (await Plugin().setup(context))["feature.kitasan"]
        self.assertFalse(service.matches_trigger("今北産業"))
        context.config = {}
        service = (await Plugin().setup(context))["feature.kitasan"]
        self.assertTrue(service.matches_trigger("今北産業"))

    def test_schema_accepts_valid_config_and_rejects_invalid_limits(self):
        schema = discover()["feature.kitasan"].config_schema
        valid = {"enabled": False, "words": ["custom"], "messages": 1,
                 "item_chars": 20, "max_chars": 100}
        jsonschema.validate(valid, schema)
        for name, value in (("enabled", "false"), ("words", "custom"),
                            ("messages", 0), ("item_chars", 19), ("max_chars", 99)):
            with self.subTest(name=name), self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate({**valid, name: value}, schema)

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
