import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from jllm_plugins.conversation.conversation import plain_user_content
from jllm_plugins.llm import llm_backend
from jllm_plugins.llm.translation import SwitchableClient
from jllm_plugins.llm.translation_control import NoTranslationController
from jllm_plugins.translation.cog import TranslationCogConfig


class SwitchCommandTests(unittest.IsolatedAsyncioTestCase):
    """``/llm switch`` は保存・クライアント差し替え・読み上げモードまで一度に行う。"""

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        llm_backend._reset_config()
        llm_backend.configure_state(Path(self.directory.name) / "llm_backend.json")
        self.addCleanup(llm_backend._reset_config)

    def build_cog(self):
        from jllm_plugins.translation.cog import TranslationCog

        built = []

        class FakeClient:
            def __init__(self, name):
                self.name = name
                self.context_window = 100
                self.closed = False

            async def health(self):
                return True

            async def translate(self, text):
                return text

            async def complete(self, messages, max_tokens=768, temperature=0.5, model=None):
                return self.name

            async def prompt_tokens(self, messages):
                return 1

            async def close(self):
                self.closed = True

        def factory(name):
            built.append(name)
            return FakeClient(name)

        cog = TranslationCog.__new__(TranslationCog)
        cog.config = TranslationCogConfig(owner_id=1)
        cog.settings = SimpleNamespace(any_enabled=lambda: False)
        cog.backend_ready = False
        cog.last_error = ""
        cog.client = SwitchableClient("sarashina", factory=factory)
        cog.controller = NoTranslationController()
        self.built = built
        return cog

    @staticmethod
    def interaction():
        send = AsyncMock()
        return SimpleNamespace(
            user=SimpleNamespace(id=1),
            followup=SimpleNamespace(send=send),
        )

    async def test_switch_to_atmos_persists_and_switches_reading_mode(self):
        cog = self.build_cog()
        interaction = self.interaction()

        await cog._llm_switch_to(interaction, "atmos")

        text = interaction.followup.send.await_args.args[0]
        self.assertIn("辞書wavのみ", text)
        self.assertIn("疎通: OK", text)
        snapshot = llm_backend.snapshot()
        self.assertEqual(snapshot["backend"], "atmos")
        self.assertEqual(snapshot["read_mode"], "wav")
        self.assertEqual(cog.client.backend, "atmos")
        self.assertEqual(self.built, ["sarashina", "atmos"])

    async def test_switch_back_to_sarashina_restores_tts(self):
        llm_backend.set_selection("atmos")
        cog = self.build_cog()
        interaction = self.interaction()

        await cog._llm_switch_to(interaction, "sarashina")

        self.assertEqual(llm_backend.snapshot()["backend"], "sarashina")
        self.assertEqual(llm_backend.snapshot()["read_mode"], "tts")

    async def test_switching_to_the_same_backend_is_a_no_op(self):
        llm_backend.set_selection("atmos")
        cog = self.build_cog()
        interaction = self.interaction()

        await cog._llm_switch_to(interaction, "atmos")

        self.assertIn("すでに", interaction.followup.send.await_args.args[0])
        self.assertEqual(self.built, ["sarashina"])

    async def test_read_mode_alone_can_be_changed(self):
        cog = self.build_cog()
        interaction = self.interaction()

        await cog._llm_switch_to(interaction, "sarashina", "wav")

        self.assertEqual(llm_backend.snapshot()["backend"], "sarashina")
        self.assertEqual(llm_backend.snapshot()["read_mode"], "wav")


if __name__ == "__main__":
    unittest.main()


class PlainUserContentTest(unittest.TestCase):
    """atmos の user ターンに system プロンプトを畳まない（2026-10-23）。"""

    def test_system_prompt_is_not_prepended(self):

        body = plain_user_content(
            "あなたはDiscordの会話アシスタントです。最新の依頼に日本語で…",
            "Ryu: おはよう\nBot: おつかれ",
            "今日ひまだわ",
        )

        self.assertNotIn("Discordの会話アシスタント", body)
        self.assertTrue(body.endswith("今日ひまだわ"))
        self.assertIn("Ryu: おはよう", body)

    def test_without_source(self):
        from jllm_plugins.conversation.conversation import plain_user_content

        self.assertEqual(plain_user_content("人格", "", "話せ"), "話せ")

    def test_no_double_blank_line(self):
        from jllm_plugins.conversation.conversation import plain_user_content

        body = plain_user_content(None, "A: 1", "2")
        self.assertEqual(body, "A: 1\n2")

def stub_client(name):
    """テスト用の最小クライアント（``translate`` は ``<backend>:<text>``）。"""

    class FakeClient:
        def __init__(self):
            self.context_window = 100
            self.closed = False

        async def translate(self, text):
            return f"{name}:{text}"

        async def complete(self, messages, max_tokens=768, temperature=0.5, model=None):
            return name

        async def prompt_tokens(self, messages):
            return 1

        async def close(self):
            self.closed = True

        async def health(self):
            return True

    return FakeClient()


class ChannelOverrideTests(unittest.TestCase):
    """チャンネルごとに別のバックエンドを使う（``/llm channel``）。"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "llm_backend.json"
        llm_backend._reset_config()
        llm_backend.configure_state(self.path)

    def tearDown(self):
        llm_backend._reset_config()

    def test_channel_override_wins_over_global(self):
        llm_backend.set_selection("sarashina")
        llm_backend.set_channel(111, "atmos")

        self.assertEqual(llm_backend.backend_for(111), "atmos")
        self.assertEqual(llm_backend.backend_for(222), "sarashina")

    def test_override_is_saved_and_survives_reload(self):
        llm_backend.set_selection("sarashina")
        llm_backend.set_channel(111, "atmos")
        llm_backend._forget()  # ファイルから読み直す

        self.assertEqual(llm_backend.backend_for(111), "atmos")
        self.assertEqual(llm_backend.backend_for(999), "sarashina")

    def test_clear_returns_to_global(self):
        llm_backend.set_selection("sarashina")
        llm_backend.set_channel(111, "atmos")
        llm_backend.clear_channel(111)

        self.assertEqual(llm_backend.backend_for(111), "sarashina")
        self.assertEqual(llm_backend.snapshot()["channels"], {})

    def test_global_switch_keeps_channel_overrides(self):
        llm_backend.set_channel(111, "atmos")
        llm_backend.set_selection("local")
        llm_backend.set_selection("sarashina")

        self.assertEqual(llm_backend.backend_for(111), "atmos")
        self.assertEqual(llm_backend.backend_for(222), "sarashina")

    def test_unknown_backend_is_rejected(self):
        with self.assertRaises(ValueError):
            llm_backend.set_channel(111, "gpt5")

    def test_broken_channels_are_ignored(self):
        self.path.write_text(
            json.dumps({"version": 1, "backend": "sarashina", "read_mode": "tts",
                        "channels": {"not-a-number": "atmos", "123": "nope", "456": "atmos"}}),
            encoding="utf-8",
        )
        llm_backend._forget()

        self.assertEqual(llm_backend.stored()["channels"], {456: "atmos"})
        self.assertEqual(llm_backend.backend_for(456), "atmos")

    def test_snapshot_reports_effective_backend(self):
        llm_backend.set_selection("sarashina")
        llm_backend.set_channel(111, "atmos")

        snap = llm_backend.snapshot(111)
        self.assertEqual(snap["backend"], "atmos")
        self.assertEqual(snap["global_backend"], "sarashina")
        self.assertEqual(snap["source"], "channel")

    def test_read_mode_follows_channel_backend(self):
        llm_backend.set_selection("sarashina")
        llm_backend.set_channel(111, "atmos")

        # atmos の既定は「辞書wavのみ」
        self.assertEqual(llm_backend.snapshot(111)["read_mode"], "wav")
        self.assertEqual(llm_backend.snapshot(222)["read_mode"], "tts")


class ChannelContextClientTests(unittest.TestCase):
    """``SwitchableClient`` がチャンネル文脈でクライアントを選ぶ。"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "llm_backend.json"
        llm_backend._reset_config()
        llm_backend.configure_state(self.path)

    def tearDown(self):
        llm_backend._reset_config()

    def test_client_switches_per_channel_context(self):
        llm_backend.set_selection("sarashina")
        llm_backend.set_channel(111, "atmos")

        client = SwitchableClient(factory=stub_client)

        async def run():
            self.assertEqual(await client.translate("x"), "sarashina:x")

            token = llm_backend.set_channel_context(111)
            try:
                self.assertEqual(await client.translate("x"), "atmos:x")
            finally:
                llm_backend.reset_channel_context(token)

            # 文脈を抜けたら全体設定に戻る
            self.assertEqual(await client.translate("x"), "sarashina:x")

        asyncio.run(run())

    def test_channel_isolation_between_concurrent_tasks(self):
        llm_backend.set_selection("sarashina")
        llm_backend.set_channel(111, "atmos")

        client = SwitchableClient(factory=stub_client)

        async def ask(channel_id):
            token = llm_backend.set_channel_context(channel_id)
            try:
                await asyncio.sleep(0)
                return await client.translate("x")
            finally:
                llm_backend.reset_channel_context(token)

        async def run():
            return await asyncio.gather(ask(111), ask(222))

        self.assertEqual(asyncio.run(run()), ["atmos:x", "sarashina:x"])
