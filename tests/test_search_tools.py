import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import aiohttp
from jllm_plugins.conversation.conversation import ConversationService
from jllm_plugins.conversation.search_mode import SearchModeStore
from jllm_plugins.conversation.search_tools import (
    SearchToolError,
    SearchTools,
    format_results,
)
from jllm_plugins.conversation.sessions import SessionStore
from jllm_plugins.conversation.web_content import WebContentError
from jllm_plugins.llm.translation import parse_text_tool_calls, strip_tool_markup


class SearchModeTests(unittest.TestCase):
    def test_channel_override_and_global_default(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "search.json"
            store = SearchModeStore(path, default=False)
            self.assertFalse(store.enabled(10))

            store.set(10, True)
            self.assertTrue(store.enabled(10))
            self.assertFalse(store.enabled(11))

            # 別インスタンスでも保存が読める（mtime キャッシュ越し）。
            other = SearchModeStore(path)
            self.assertTrue(other.enabled(10))

            other.set(None, True)
            self.assertTrue(other.enabled(11))

            other.clear(10)
            self.assertTrue(other.enabled(10))  # 全体既定へ戻る

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], 1)
            self.assertTrue(payload["default"])

    def test_broken_file_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "search.json"
            path.write_text("{", encoding="utf-8")
            store = SearchModeStore(path, default=False)
            self.assertFalse(store.enabled(10))


class FormatTests(unittest.TestCase):
    def test_snippets_and_total_are_bounded(self):
        results = [
            {"title": "t" * 300, "url": f"https://example.com/{index}", "content": "c" * 1000}
            for index in range(10)
        ]
        text = format_results(results)
        self.assertLessEqual(len(text), 3000)
        self.assertIn("https://example.com/0", text)
        self.assertNotIn("c" * 301, text)

    def test_results_without_url_are_skipped(self):
        self.assertEqual(format_results([{"title": "x", "content": "y"}]), "")


class TextToolCallTests(unittest.TestCase):
    # サーバが tool_calls へ変換せず、本文にネイティブ形式で書いてきた場合。
    TEXT = (
        '<|SBML|tool_calls>\n'
        '<|SBML|invoke name="web_search">\n'
        '<|SBML|parameter name="query">Ollama 地元の LLM</|SBML|parameter>\n'
        '<|SBML|parameter name="max_results">5</|SBML|parameter>\n'
        "</|SBML|invoke>\n"
        "</|SBML|tool_calls>"
    )

    def test_text_call_becomes_tool_calls(self):
        calls, rest = parse_text_tool_calls(self.TEXT)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "web_search")
        self.assertEqual(
            json.loads(calls[0]["function"]["arguments"]),
            {"query": "Ollama 地元の LLM", "max_results": 5},
        )
        self.assertEqual(rest, "")

    def test_tags_are_stripped_from_plain_text(self):
        text = "調べます。\n" + self.TEXT + "\n以上です。"
        calls, rest = parse_text_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(rest, "調べます。\n\n以上です。")
        self.assertNotIn("SBML", strip_tool_markup(self.TEXT))

    def test_plain_text_is_untouched(self):
        self.assertEqual(parse_text_tool_calls("こんにちは")[1], "こんにちは")
        self.assertEqual(strip_tool_markup("こんにちは"), "こんにちは")


class FakeSearchTools:
    max_rounds = 3
    max_calls = 4
    max_page_calls = 2

    def __init__(self):
        self.calls = []

    async def run(self, name, arguments):
        self.calls.append((name, arguments))
        return "1. 東京の天気\n   https://example.com/weather"


class FakeToolClient:
    context_window = 128000
    supports_tools = True

    def __init__(self):
        self.calls = []

    async def prompt_tokens(self, messages):
        return 10

    async def complete_message(self, messages, **kwargs):
        self.calls.append(([dict(message) for message in messages], kwargs))

        if len(self.calls) == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "arguments": json.dumps({"query": "東京 天気"}),
                        },
                    }
                ],
                "finish_reason": "tool_calls",
            }

        return {"content": "東京は晴れです。", "tool_calls": [], "finish_reason": "stop"}

    async def complete(self, messages, **kwargs):
        return "ツールなしの回答"


class AlwaysOn:
    def enabled(self, channel_id=None):
        return True


class ToolLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_call_then_answer(self):
        client = FakeToolClient()
        search = FakeSearchTools()
        trace = []
        service = ConversationService(client, search=search, search_mode=AlwaysOn())

        answer = await service._complete(
            client,
            [{"role": "user", "content": "東京の天気は？"}],
            model=None,
            active=lambda: True,
            tools=True,
            trace=trace,
        )

        self.assertEqual(answer, "東京は晴れです。")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(search.calls[0][0], "web_search")
        self.assertEqual(trace[0]["tool"], "web_search")
        self.assertEqual(trace[0]["arguments"]["query"], "東京 天気")
        self.assertIn("https://example.com/weather", trace[0]["result"])
        # 2 回目の messages には assistant(tool_calls) と tool が積まれている。
        second = client.calls[1][0]
        self.assertEqual(second[-2]["role"], "assistant")
        self.assertEqual(second[-2]["tool_calls"][0]["id"], "call-1")
        self.assertEqual(second[-1]["role"], "tool")
        self.assertEqual(second[-1]["tool_call_id"], "call-1")
        self.assertIn("東京の天気", second[-1]["content"])
        self.assertEqual(second[0]["role"], "user")

    async def test_round_limit_forces_a_final_answer(self):
        class Endless(FakeToolClient):
            async def complete_message(self, messages, **kwargs):
                self.calls.append((list(messages), kwargs))
                return {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-x",
                            "type": "function",
                            "function": {"name": "web_search", "arguments": "{}"},
                        }
                    ],
                    "finish_reason": "tool_calls",
                }

        client = Endless()
        search = FakeSearchTools()
        service = ConversationService(client, search=search, search_mode=AlwaysOn())

        answer = await service._complete(
            client,
            [{"role": "user", "content": "q"}],
            model=None,
            active=lambda: True,
            tools=True,
            trace=[],
        )

        # 3 ラウンドで打ち切り。最終ラウンドは tools を外してまとめさせる。
        self.assertEqual(len(client.calls), 3)
        self.assertIsNone(client.calls[-1][1].get("tools"))
        self.assertEqual(len(search.calls), 2)
        self.assertIn("まとめられませんでした", answer)

    async def test_intermediate_markup_is_not_kept(self):
        class Noisy(FakeToolClient):
            async def complete_message(self, messages, **kwargs):
                self.calls.append(([dict(message) for message in messages], kwargs))

                if len(self.calls) == 1:
                    return {
                        "content": (
                            "調べます。"
                            '<｜SBML｜tool_calls> <｜SBML｜invoke name="web_search">'
                            ' <｜SBML｜parameter name="query">x</｜SBML｜parameter>'
                            " </｜SBML｜invoke> </｜SBML｜tool_calls>"
                        ),
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "web_search", "arguments": '{"query": "x"}'},
                            }
                        ],
                        "finish_reason": "tool_calls",
                    }

                return {"content": "答え", "tool_calls": [], "finish_reason": "stop"}

        client = Noisy()
        service = ConversationService(client, search=FakeSearchTools(), search_mode=AlwaysOn())
        trace = []

        answer = await service._complete(
            client,
            [{"role": "user", "content": "q"}],
            model=None,
            active=lambda: True,
            tools=True,
            trace=trace,
        )

        self.assertEqual(answer, "答え")
        second = client.calls[1][0]
        assistant = next(m for m in second if m.get("role") == "assistant")
        self.assertEqual(assistant["content"], "調べます。")
        self.assertNotIn("SBML", json.dumps(second, ensure_ascii=False))

    async def test_tools_off_keeps_the_single_call(self):
        client = FakeToolClient()
        service = ConversationService(client)

        answer = await service._complete(
            client,
            [{"role": "user", "content": "q"}],
            model=None,
            active=lambda: True,
            tools=False,
            trace=[],
        )

        self.assertEqual(answer, "ツールなしの回答")
        self.assertEqual(client.calls, [])


class SearchToolsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        pass

    async def test_web_search_is_cached(self):
        tools = SearchTools("http://example.invalid", cache_seconds=60)
        calls = []

        async def fake_json(url, params):
            calls.append((url, params))
            return {"results": [{"title": "A", "url": "https://a.example", "content": "body"}]}

        tools._json = fake_json
        first = await tools.web_search("東京", 3)
        second = await tools.web_search("東京", 3)
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)
        self.assertIn("https://a.example", first)
        await tools.close()

    async def test_search_failure_becomes_a_tool_message(self):
        tools = SearchTools("http://example.invalid")

        async def boom(url, params):
            raise aiohttp.ClientConnectionError("down")

        tools._json = boom
        with self.assertRaises(SearchToolError):
            await tools.web_search("q")
        self.assertIn(
            "ツールを実行できませんでした",
            await tools.run("web_search", '{"query": "q"}'),
        )
        await tools.close()

    async def test_read_page_rejects_local_urls(self):
        tools = SearchTools()

        for url in ("file:///etc/passwd", "http://127.0.0.1/x", "http://localhost/x", "http://intranet/x"):
            text = await tools.run("read_page", {"url": url})
            self.assertIn("ツールを実行できませんでした", text, msg=url)

        await tools.close()

    async def test_read_page_uses_the_fetch_endpoint_and_truncates(self):
        tools = SearchTools(fetch_max_chars=20)
        seen = []

        async def fake_jina(target):
            seen.append(target)
            return "x" * 100

        tools._jina = fake_jina
        text = await tools.read_page("https://example.com/a")
        self.assertEqual(len(text), 20)
        self.assertEqual(seen, ["https://example.com/a"])
        await tools.close()


class FakeFetcher:
    def __init__(self, fail=False):
        self.fail = fail
        self.urls = []

    async def fetch(self, url):
        self.urls.append(url)

        if self.fail:
            raise WebContentError("ページを取得できませんでした（HTTP 404）。")

        return {"url": url, "title": "T", "text": "本文", "truncated": False}


class WebDocumentsTests(unittest.IsolatedAsyncioTestCase):
    def message(self, parent):
        return SimpleNamespace(
            channel=SimpleNamespace(id=10),
            reference=SimpleNamespace(message_id=1, channel_id=10, resolved=parent),
        )

    async def test_bot_parent_urls_are_not_fetched(self):
        # Bot の回答に含まれる引用 URL を返信のたびに取りに行くと、
        # 死んだ URL で回答そのものが失敗する。
        fetcher = FakeFetcher()
        service = ConversationService(object(), fetcher=fetcher)
        parent = SimpleNamespace(
            id=1,
            author=SimpleNamespace(id=99),
            clean_content="参照 [x](https://example.com/dead)",
        )

        urls, documents, failures, explicit = await service.web_documents(
            self.message(parent), "これについて詳しく", lambda: True, 99
        )

        self.assertEqual(urls, [])
        self.assertEqual(fetcher.urls, [])
        self.assertEqual(documents, [])
        self.assertFalse(explicit)

    async def test_user_parent_urls_are_fetched(self):
        fetcher = FakeFetcher()
        service = ConversationService(object(), fetcher=fetcher)
        parent = SimpleNamespace(
            id=1,
            author=SimpleNamespace(id=2),
            clean_content="https://example.com/a",
        )

        urls, documents, failures, explicit = await service.web_documents(
            self.message(parent), "要約して", lambda: True, 99
        )

        self.assertEqual(urls, ["https://example.com/a"])
        self.assertEqual(fetcher.urls, ["https://example.com/a"])
        self.assertEqual(len(documents), 1)
        self.assertFalse(explicit)

    async def test_code_block_urls_are_ignored(self):
        # curl の例のようなコード中の URL は取得対象にしない。
        fetcher = FakeFetcher()
        service = ConversationService(object(), fetcher=fetcher)
        message = SimpleNamespace(channel=SimpleNamespace(id=10), reference=None)
        fence = chr(96) * 3
        request = (
            fence + 'bash\ncurl -G "https://example.com/a"\n' + fence
            + "\nこのコマンドの意味は？"
        )

        urls, documents, failures, explicit = await service.web_documents(
            message, request, lambda: True, 99
        )

        self.assertEqual(urls, [])
        self.assertEqual(fetcher.urls, [])

    async def test_private_urls_are_ignored(self):
        fetcher = FakeFetcher()
        service = ConversationService(object(), fetcher=fetcher)
        message = SimpleNamespace(channel=SimpleNamespace(id=10), reference=None)

        urls, documents, failures, explicit = await service.web_documents(
            message, "http://127.0.0.1:4131/extract/ を叩いて", lambda: True, 99
        )

        self.assertEqual(urls, [])
        self.assertEqual(fetcher.urls, [])
        self.assertFalse(explicit)

    async def test_explicit_url_failure_is_reported(self):
        fetcher = FakeFetcher(fail=True)
        service = ConversationService(object(), fetcher=fetcher)
        message = SimpleNamespace(channel=SimpleNamespace(id=10), reference=None)

        urls, documents, failures, explicit = await service.web_documents(
            message, "https://example.com/a を要約して", lambda: True, 99
        )

        self.assertTrue(explicit)
        self.assertEqual(documents, [])
        self.assertEqual(len(failures), 1)


class SessionTraceTests(unittest.TestCase):
    def test_trace_round_trips_through_history(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SessionStore(Path(temp))
            session_id, _ = store.resolve(1, 10, 2, "User", None)
            store.append_user(session_id, 2, "User", "東京の天気は？")
            store.append_assistant(
                session_id,
                "晴れです。",
                trace=[
                    {
                        "tool": "web_search",
                        "arguments": {"query": "東京 天気"},
                        "result": "1. 天気",
                    }
                ],
            )
            records = store.history(session_id)
            self.assertEqual(records[-1]["text"], "晴れです。")
            self.assertEqual(records[-1]["search"][0]["tool"], "web_search")

    def test_old_markup_in_history_is_removed(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SessionStore(Path(temp))
            session_id, _ = store.resolve(1, 10, 2, "User", None)
            store.append_user(session_id, 2, "User", "調べて")
            store.append_assistant(
                session_id,
                '<｜SBML｜tool_calls> <｜SBML｜invoke name="web_search">'
                ' <｜SBML｜parameter name="query">x</｜SBML｜parameter>'
                " </｜SBML｜invoke> </｜SBML｜tool_calls>",
            )
            records = store.history(session_id)
            self.assertEqual([record["text"] for record in records], ["調べて"])


if __name__ == "__main__":
    unittest.main()
