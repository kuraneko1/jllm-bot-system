# ruff: noqa: E501
"""「今北産業」で直前の投稿を3行に要約する（services/kitasan.py / cogs/translation.py）。

観点:
- トリガー一致（語だけ／余白あり／他の語が混ざったら発動しない／設定で増やせる）
- 履歴の組み立て（古い順へ並べ直し・bot除外・空除外・切り詰め・件数上限・全体上限）
- 専用システムプロンプトが普段の会話・翻訳と別物であること
- LLM失敗時に例外を投げずエラー通知すること
- 通常の投稿では何も起きないこと
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

from jllm_plugins.conversation.conversation import SUMMARY_SYSTEM, WEBCHAT_SYSTEM
from jllm_plugins.conversation.conversation import SYSTEM as CONVERSATION_SYSTEM
from jllm_plugins.conversation.kitasan import (
    KITASAN_ERROR_MESSAGE,
    KITASAN_SYSTEM_PROMPT,
    KitasanError,
    KitasanService,
    build_history,
    clean_summary,
    collect_history,
    is_kitasan_trigger,
    summarize,
)
from jllm_plugins.conversation.sessions import SessionStore
from jllm_plugins.conversation.talk_log import TalkLog
from jllm_plugins.llm.translation import TRANSLATION_SYSTEM_PROMPT, TranslationSettings
from jllm_plugins.restrictions.service import UserRestrictions
from jllm_plugins.translation.cog import TranslationCog


def author(name, uid, bot=False):
    return SimpleNamespace(id=uid, bot=bot, display_name=name, name=name)


def post(mid, uid, name, content, bot=False, clean=None):
    return SimpleNamespace(
        id=mid, author=author(name, uid, bot=bot),
        content=content, clean_content=content if clean is None else clean,
    )


class FakeChannel:
    def __init__(self, messages, channel_id=10):
        self.messages = list(messages)
        self.id = channel_id
        self.calls = []

    async def history(self, limit=None, before=None):
        self.calls.append({"limit": limit, "before": before})

        for item in self.messages[:limit]:
            yield item


class FakeClient:
    """``complete`` だけを持つ（会話・翻訳と同じ呼び出し口）。"""

    def __init__(self, answer="一行目\n二行目\n三行目"):
        self.answer = answer
        self.calls = []

    async def complete(self, messages, max_tokens=768, temperature=0.5, model=None):
        self.calls.append({
            "messages": messages, "max_tokens": max_tokens,
            "temperature": temperature, "model": model,
        })

        if isinstance(self.answer, Exception):
            raise self.answer

        return self.answer


class TriggerTests(unittest.TestCase):
    def test_only_the_word_itself_fires(self):
        for content in ("今北産業", "  今北産業  ", "\n今北産業\n", "今北産業　"):
            with self.subTest(content=content):
                self.assertTrue(is_kitasan_trigger(content))

    def test_other_text_does_not_fire(self):
        for content in ("", "   ", "今北産業！", "今北産業 です", "今北産業 今北産業",
                        "今北", "いまきたさんぎょう", "今日の北産業"):
            with self.subTest(content=content):
                self.assertFalse(is_kitasan_trigger(content))

    def test_words_can_be_changed(self):
        self.assertTrue(is_kitasan_trigger("kitasan", ("kitasan", "今北産業")))
        self.assertTrue(is_kitasan_trigger("KITASAN", ("kitasan",)))
        self.assertFalse(is_kitasan_trigger("今北産業", ("kitasan",)))

    def test_disabled_never_fires(self):
        self.assertFalse(is_kitasan_trigger("今北産業", enabled=False))


class HistoryTests(unittest.TestCase):
    def test_orders_oldest_first(self):
        # Discord の history は新しい順。古い順へ並べ直す。
        messages = [post(3, 1, "うえ", "三番目"), post(2, 1, "なか", "二番目"), post(1, 1, "した", "一番目")]
        self.assertEqual(
            build_history(messages, limit=10),
            ["した: 一番目", "なか: 二番目", "うえ: 三番目"],
        )

    def test_excludes_bots_and_empty_messages(self):
        messages = [
            post(4, 1, "ひと", "   "),
            post(3, 9, "bot", "私はbotです", bot=True),
            post(2, 1, "ひと", "残る"),
        ]
        self.assertEqual(build_history(messages, bot_id=9, limit=10), ["ひと: 残る"])

    def test_excludes_bot_id_without_the_bot_flag(self):
        messages = [post(2, 9, "bot", "私の投稿"), post(1, 1, "ひと", "残る")]
        self.assertEqual(build_history(messages, bot_id=9, limit=10), ["ひと: 残る"])

    def test_excludes_ignored_users(self):
        messages = [post(2, 5, "むし", "無視される"), post(1, 1, "ひと", "残る")]
        self.assertEqual(build_history(messages, limit=10, skip_ids={5}), ["ひと: 残る"])

    def test_truncates_a_long_message(self):
        messages = [post(1, 1, "ひと", "あ" * 500)]
        line = build_history(messages, limit=10, item_chars=20, total_chars=1000)[0]
        self.assertEqual(len(line), len("ひと: ") + 20)
        self.assertTrue(line.endswith("…"))

    def test_collapses_newlines_into_one_line_per_post(self):
        messages = [post(1, 1, "ひと", "一行目\n\n二行目  三行目")]
        self.assertEqual(build_history(messages, limit=10), ["ひと: 一行目 二行目 三行目"])

    def test_limit_keeps_the_most_recent(self):
        messages = [post(index, 1, "ひと", f"本文{index}") for index in range(1, 11)]
        self.assertEqual(
            build_history(messages, limit=3),
            ["ひと: 本文8", "ひと: 本文9", "ひと: 本文10"],
        )

    def test_total_chars_drops_the_oldest_lines(self):
        messages = [post(1, 1, "ひと", "あ" * 10), post(2, 1, "ひと", "い" * 10), post(3, 1, "ひと", "う" * 10)]
        history = build_history(messages, limit=10, item_chars=50, total_chars=30)
        self.assertEqual(history, ["ひと: " + "い" * 10, "ひと: " + "う" * 10])
        self.assertLessEqual(sum(len(line) + 1 for line in history), 30)

    def test_zero_limit_returns_nothing(self):
        self.assertEqual(build_history([post(1, 1, "ひと", "本文")], limit=0), [])


class CollectHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetches_before_the_trigger_with_the_configured_limit(self):
        trigger = post(100, 1, "ひと", "今北産業")
        channel = FakeChannel([post(2, 1, "ひと", "前の投稿"), post(1, 1, "ひと", "もっと前")])
        history = await collect_history(channel, trigger, limit=2)
        self.assertEqual(history, ["ひと: もっと前", "ひと: 前の投稿"])
        self.assertEqual(channel.calls, [{"limit": 2, "before": trigger}])


class PromptTests(unittest.IsolatedAsyncioTestCase):
    def test_prompt_is_not_the_conversation_or_translation_prompt(self):
        for other in (CONVERSATION_SYSTEM, TRANSLATION_SYSTEM_PROMPT, SUMMARY_SYSTEM, WEBCHAT_SYSTEM):
            with self.subTest(other=other[:12]):
                self.assertNotEqual(KITASAN_SYSTEM_PROMPT, other)
                self.assertNotIn(KITASAN_SYSTEM_PROMPT, other)
        self.assertIn("3行", KITASAN_SYSTEM_PROMPT)

    async def test_summarize_sends_the_dedicated_system_prompt(self):
        client = FakeClient()
        result = await summarize(client, ["ひと: こんにちは", "ひと: 元気？"])
        self.assertEqual(result, "一行目\n二行目\n三行目")
        sent = client.calls[0]["messages"]
        self.assertEqual(sent[0], {"role": "system", "content": KITASAN_SYSTEM_PROMPT})
        self.assertEqual(sent[1]["role"], "user")
        self.assertIn("ひと: こんにちは", sent[1]["content"])
        self.assertIn("ひと: 元気？", sent[1]["content"])
        self.assertNotEqual(sent[0]["content"], CONVERSATION_SYSTEM)
        self.assertNotEqual(sent[0]["content"], TRANSLATION_SYSTEM_PROMPT)

    async def test_summarize_wraps_client_errors(self):
        with self.assertRaises(KitasanError):
            await summarize(FakeClient(RuntimeError("接続できません")), ["ひと: こんにちは"])

    async def test_summarize_rejects_an_empty_history(self):
        with self.assertRaises(KitasanError):
            await summarize(FakeClient(), [])


class CleanSummaryTests(unittest.TestCase):
    def test_keeps_three_lines_and_drops_fences(self):
        text = "```\n一つ目\n\n二つ目\n三つ目\n四つ目\n```"
        self.assertEqual(clean_summary(text), "一つ目\n二つ目\n三つ目")

    def test_empty_text_is_empty(self):
        self.assertEqual(clean_summary("\n \n```\n```"), "")


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def service(self, client, messages=None, restrictions=None):
        if messages is None:
            messages = [post(1, 1, "ひと", "こんにちは")]

        return KitasanService(
            client, bot_id=9, restrictions=restrictions,
            limit=10,
        ), FakeChannel(messages)

    async def test_handle_replies_with_the_summary(self):
        client = FakeClient()
        service, channel = self.service(client)
        trigger = post(100, 2, "ひと", "今北産業")
        trigger.channel = channel
        trigger.reply = AsyncMock()
        result = await service.handle(trigger)
        self.assertEqual(result, "一行目\n二行目\n三行目")
        trigger.reply.assert_awaited_once()
        self.assertEqual(trigger.reply.call_args.args[0], "一行目\n二行目\n三行目")
        self.assertFalse(trigger.reply.call_args.kwargs["mention_author"])
        self.assertEqual(
            client.calls[0]["messages"][0]["content"], KITASAN_SYSTEM_PROMPT,
        )

    async def test_handle_notifies_on_llm_failure_without_raising(self):
        client = FakeClient(RuntimeError("サーバー停止中"))
        service, channel = self.service(client)
        trigger = post(100, 2, "ひと", "今北産業")
        trigger.channel = channel
        trigger.reply = AsyncMock()
        with self.assertLogs("jllm_plugins.conversation.kitasan", level="WARNING") as logs:
            result = await service.handle(trigger)
        self.assertIsNone(result)
        trigger.reply.assert_awaited_once_with(
            KITASAN_ERROR_MESSAGE, mention_author=False, allowed_mentions=ANY,
        )
        self.assertTrue(any("今北産業" in line for line in logs.output))

    async def test_handle_notifies_when_there_is_no_history(self):
        client = FakeClient()
        service, channel = self.service(client, [])
        trigger = post(100, 2, "ひと", "今北産業")
        trigger.channel = channel
        trigger.reply = AsyncMock()
        with self.assertLogs("jllm_plugins.conversation.kitasan", level="WARNING"):
            self.assertIsNone(await service.handle(trigger))
        self.assertEqual(trigger.reply.call_args.args[0], KITASAN_ERROR_MESSAGE)
        self.assertEqual(client.calls, [])

    async def test_handle_notifies_when_the_reply_fails(self):
        client = FakeClient()
        service, channel = self.service(client)
        trigger = post(100, 2, "ひと", "今北産業")
        trigger.channel = channel
        trigger.reply = AsyncMock(side_effect=RuntimeError("送信できません"))
        with self.assertLogs("jllm_plugins.conversation.kitasan", level="ERROR"):
            self.assertEqual(await service.handle(trigger), "一行目\n二行目\n三行目")

    async def test_ignored_users_are_not_summarized(self):
        with tempfile.TemporaryDirectory() as directory:
            store = UserRestrictions(Path(directory) / "restrictions.json")
            store.entries["7:5"] = {"ignore"}
            client = FakeClient()
            service = KitasanService(client, bot_id=9, restrictions=store, limit=10)
            channel = FakeChannel([post(2, 5, "むし", "無視される"), post(1, 1, "ひと", "残る")])
            trigger = post(100, 2, "ひと", "今北産業")
            trigger.guild = SimpleNamespace(id=7)
            trigger.channel = channel
            trigger.reply = AsyncMock()
            await service.handle(trigger)
            sent = client.calls[0]["messages"][1]["content"]
            self.assertIn("ひと: 残る", sent)
            self.assertNotIn("無視される", sent)


class CogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = TranslationSettings(Path(self.directory.name) / "settings.json")
        self.client = FakeClient()
        self.client.translate = AsyncMock(return_value="こんにちは")
        self.client.close = AsyncMock()
        self.controller = SimpleNamespace(set_enabled=AsyncMock())
        self.cog = TranslationCog(
            None, self.settings, self.client, self.controller, TalkLog(None, None),
            SessionStore(Path(self.directory.name) / "sessions"),
        )
        self.whitelist = patch.object(self.cog.config, "allowed_channel_ids", {10})
        self.whitelist.start()
        self.addCleanup(self.whitelist.stop)
        self.member = SimpleNamespace(id=2, bot=False, display_name="Somebody")
        self.guild = SimpleNamespace(id=7, get_member=lambda uid: None)

    async def asyncTearDown(self):
        await self.cog.cog_unload()

    def trigger(self, content="今北産業", messages=None, reply=None):
        channel = FakeChannel(messages if messages is not None else [post(1, 1, "ひと", "こんにちは")])
        message = SimpleNamespace(
            id=100, guild=self.guild, channel=channel, author=self.member,
            content=content, clean_content=content, webhook_id=None,
            reply=reply or AsyncMock(),
        )

        return message

    async def test_a_bare_trigger_summarizes_and_replies(self):
        message = self.trigger()
        await self.cog.on_message(message)
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(
            self.client.calls[0]["messages"][0]["content"], KITASAN_SYSTEM_PROMPT,
        )
        message.reply.assert_awaited_once()
        self.assertEqual(message.reply.call_args.args[0], "一行目\n二行目\n三行目")

    async def test_a_normal_message_does_nothing(self):
        message = self.trigger(content="こんにちは")
        await self.cog.on_message(message)
        self.assertEqual(self.client.calls, [])
        message.reply.assert_not_awaited()

    async def test_a_trigger_with_other_words_does_nothing(self):
        message = self.trigger(content="今北産業 ってなに？")
        await self.cog.on_message(message)
        self.assertEqual(self.client.calls, [])
        message.reply.assert_not_awaited()

    async def test_ignored_users_do_not_trigger(self):
        store = UserRestrictions(Path(self.directory.name) / "restrictions.json")
        store.entries["7:2"] = {"ignore"}
        with patch.object(self.cog, "restrictions", store):
            message = self.trigger()
            await self.cog.on_message(message)
        self.assertEqual(self.client.calls, [])
        message.reply.assert_not_awaited()

    async def test_a_channel_outside_the_whitelist_does_nothing(self):
        message = self.trigger()
        message.channel.id = 999
        await self.cog.on_message(message)
        self.assertEqual(self.client.calls, [])
        message.reply.assert_not_awaited()

    async def test_failure_notifies_instead_of_raising(self):
        self.client.answer = RuntimeError("停止中")
        message = self.trigger()
        with self.assertLogs("jllm_plugins.conversation.kitasan", level="WARNING"):
            await self.cog.on_message(message)
        self.assertEqual(message.reply.call_args.args[0], KITASAN_ERROR_MESSAGE)


if __name__ == "__main__":
    unittest.main()
