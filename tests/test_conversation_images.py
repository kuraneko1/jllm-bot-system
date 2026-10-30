# ruff: noqa: E501
"""画像対応（gemma4 系だけ）のテスト。

偽 Discord メッセージと偽 HTTP サーバ（aiohttp）で、実際に送られる JSON を
そのまま検証する。z170 の実機（gemma4-atmos-sft）は使わない。

確かめること:

- atmos では画像が OpenAI の content parts として届く
- sarashina（と local / openai）では画像を送らず、短く断る
- 枚数（4枚）と合計サイズ（8MiB）の上限、長辺 1568px への縮小
- prompt_tokens が base64 を数えない（文字数はテキストだけ、画像は固定値）
- SYSTEM の「画像は読めない」文言が、画像対応時だけ差し替わる
"""

import asyncio
import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web
from jllm_plugins.conversation.conversation import (
    MAX_IMAGE_BYTES,
    SYSTEM,
    ConversationService,
    attached_images,
    has_source_attachment,
    with_image_support,
)
from jllm_plugins.conversation.sessions import SessionStore
from jllm_plugins.conversation.talk_log import TalkLog
from jllm_plugins.llm import prompts
from jllm_plugins.llm.translation import (
    AtmosClient,
    SarashinaClient,
    SwitchableClient,
    TranslationSettings,
)
from jllm_plugins.translation.cog import TranslationCog
from PIL import Image


def png_bytes(width=32, height=32):
    """テスト用の PNG（Pillow で作る）。"""

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


def decode_data_url(url):
    """data URL から MIME と本文を取り出す。"""

    header, data = url.split(",", 1)
    return header.split(";")[0].removeprefix("data:"), base64.b64decode(data)


class FakeAttachment:
    def __init__(self, data, filename="image.png", size=None):
        self.filename = filename
        self.size = len(data) if size is None else size
        self.data = data

    async def read(self):
        return self.data


def fake_message(attachments=(), text=""):
    """respond() が触る属性だけを持つ偽メッセージ。"""

    return SimpleNamespace(
        id=1,
        attachments=list(attachments),
        clean_content=text,
        reference=None,
        channel=None,
    )


class RecordingServer:
    """OpenAI 互換の /v1/chat/completions だけを返す偽サーバ。"""

    answer = "受け取りました。"

    def __init__(self):
        self.payloads = []

    async def start(self):
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self.completion)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        host, port = self.runner.addresses[0]
        self.base_url = f"http://{host}:{port}"
        return self

    async def completion(self, request):
        self.payloads.append(await request.json())
        return web.json_response(
            {"choices": [{"message": {"content": self.answer}, "finish_reason": "stop"}]}
        )

    async def stop(self):
        await self.runner.cleanup()


class CapturingClient:
    """system ロールを送れるバックエンドの代役（画像対応）。"""

    context_window = 8192
    prompt_style = prompts.SYSTEM
    supports_images = True

    def __init__(self):
        self.messages = []

    async def prompt_tokens(self, messages):
        total = 0

        for message in messages:
            chars, images = prompts.content_size(message.get("content"))
            total += chars * 3 // 4 + images * prompts.IMAGE_TOKENS + 8

        return total

    async def complete(self, messages, max_tokens=768, temperature=0.5, model=None):
        self.messages.append(messages)
        return "ok"


class ImageConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = await RecordingServer().start()
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()
        await self.server.stop()

    def atmos(self):
        client = AtmosClient(
            self.server.base_url, "gemma4-atmos-sft", context=8192, max_tokens=1024
        )
        self.clients.append(client)
        return client

    def sarashina(self):
        client = SarashinaClient(
            self.server.base_url, "", "sarashina3-mini", requests_per_minute=0, api_key=""
        )
        self.clients.append(client)
        return client

    async def test_atmos_sends_image_content_parts(self):
        """atmos では text と image_url の parts がそのまま送られる。"""

        service = ConversationService(self.atmos())
        message = fake_message([FakeAttachment(png_bytes())], "この画像は何？")
        answer = await service.respond(message, "この画像は何？", 99, lambda: True, history=[])

        self.assertEqual(answer, "受け取りました。")
        payload = self.server.payloads[-1]
        self.assertEqual([m["role"] for m in payload["messages"]], ["user"])
        content = payload["messages"][0]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]["type"], "text")
        self.assertIn("この画像は何？", content[0]["text"])
        self.assertEqual(content[1]["type"], "image_url")
        mime, data = decode_data_url(content[1]["image_url"]["url"])
        self.assertEqual(mime, "image/png")
        self.assertTrue(data.startswith(b"\x89PNG"))

    async def test_atmos_image_only_post_gets_a_default_instruction(self):
        service = ConversationService(self.atmos())
        message = fake_message([FakeAttachment(png_bytes())], "")
        await service.respond(message, "", 99, lambda: True, history=[])

        content = self.server.payloads[-1]["messages"][0]["content"]
        self.assertIn("画像", content[0]["text"])
        self.assertEqual(content[1]["type"], "image_url")

    async def test_sarashina_never_receives_images(self):
        """sarashina では画像 parts を送らず、回答の末尾で断る。"""

        service = ConversationService(self.sarashina())
        message = fake_message([FakeAttachment(png_bytes())], "この画像は何？")
        answer = await service.respond(message, "この画像は何？", 99, lambda: True, history=[])

        self.assertIn("画像を読めません", answer)
        payload = self.server.payloads[-1]
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertIsInstance(payload["messages"][-1]["content"], str)
        self.assertNotIn("image_url", json.dumps(payload, ensure_ascii=False))
        self.assertIn("この画像は何？", payload["messages"][-1]["content"])

    async def test_sarashina_image_only_post_is_not_sent_to_the_model(self):
        service = ConversationService(self.sarashina())
        message = fake_message([FakeAttachment(png_bytes())], "")
        answer = await service.respond(message, "", 99, lambda: True, history=[])

        self.assertIn("画像を読めません", answer)
        self.assertEqual(self.server.payloads, [])

    async def test_switchable_client_judges_by_the_leased_backend(self):
        """lease した実クライアントで判定する（チャンネル別でも取り違えない）。"""

        atmos = self.atmos()
        router = SwitchableClient(backend="atmos", factory=lambda name: atmos)
        self.clients.append(router)
        service = ConversationService(router)
        message = fake_message([FakeAttachment(png_bytes())], "見て")
        await service.respond(message, "見て", 99, lambda: True, history=[])

        content = self.server.payloads[-1]["messages"][0]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(content[1]["type"], "image_url")

    async def test_image_count_and_total_size_limits(self):
        five = fake_message(
            [FakeAttachment(png_bytes(), f"image-{index}.png") for index in range(5)]
        )
        with self.assertRaisesRegex(ValueError, "4枚"):
            await attached_images(five)

        oversized = fake_message(
            [FakeAttachment(png_bytes(), "big.png", size=MAX_IMAGE_BYTES + 1)]
        )
        with self.assertRaisesRegex(ValueError, "8MiB"):
            await attached_images(oversized)

    async def test_large_image_is_downscaled_to_jpeg(self):
        parts = await attached_images(
            fake_message([FakeAttachment(png_bytes(2400, 1200), "big.png")])
        )
        mime, data = decode_data_url(parts[0]["image_url"]["url"])
        self.assertEqual(mime, "image/jpeg")

        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.size, (1568, 784))

    async def test_gif_is_converted_to_the_first_frame(self):
        buffer = io.BytesIO()
        frames = [Image.new("RGB", (40, 40), color) for color in ((255, 0, 0), (0, 255, 0))]
        frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:])
        parts = await attached_images(
            fake_message([FakeAttachment(buffer.getvalue(), "anim.gif")])
        )
        mime, data = decode_data_url(parts[0]["image_url"]["url"])
        self.assertEqual(mime, "image/jpeg")

        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.format, "JPEG")

    async def test_prompt_tokens_counts_images_fixed_and_ignores_base64(self):
        client = self.atmos()
        huge = "A" * (1024 * 1024)
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "説明して"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + huge}},
                ],
            }
        ]

        tokens = await client.prompt_tokens(messages)
        self.assertLess(tokens, 2000)
        self.assertGreaterEqual(tokens, prompts.IMAGE_TOKENS)
        self.assertEqual(await self.sarashina().prompt_tokens(messages), tokens)

        plain = await client.prompt_tokens([{"role": "user", "content": "あ" * 10}])
        self.assertEqual(plain, 10 * 3 // 4 + 8)

    async def test_system_prompt_depends_on_image_support(self):
        self.assertIn("画像、音声", SYSTEM)
        readable = with_image_support(SYSTEM)
        self.assertNotIn("画像、音声", readable)
        self.assertIn("画像に写っていない", readable)
        self.assertEqual(with_image_support("独自プロンプト"), "独自プロンプト")

    async def test_system_backend_with_images_gets_readable_prompt(self):
        client = CapturingClient()
        service = ConversationService(client)
        message = fake_message([FakeAttachment(png_bytes())], "何か")
        await service.respond(message, "何か", 99, lambda: True, history=[])

        messages = client.messages[-1]
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("画像に写っていない", messages[0]["content"])
        self.assertNotIn("画像、音声", messages[0]["content"])
        self.assertIsInstance(messages[1]["content"], list)

    def test_only_atmos_advertises_image_support(self):
        from jllm_plugins.llm.translation import OpenAIClient, TranslationClient

        self.assertTrue(AtmosClient.supports_images)
        self.assertFalse(SarashinaClient.supports_images)
        self.assertFalse(OpenAIClient.supports_images)
        self.assertFalse(TranslationClient.supports_images)

    def test_attachment_gate_accepts_text_and_images_only(self):
        self.assertTrue(has_source_attachment(fake_message([FakeAttachment(b"a", "note.txt")])))
        self.assertTrue(has_source_attachment(fake_message([FakeAttachment(png_bytes(), "p.jpg")])))
        self.assertFalse(has_source_attachment(fake_message([FakeAttachment(b"a", "data.zip")])))


class NullContext:
    async def __aenter__(self):
        pass

    async def __aexit__(self, *args):
        pass


class TalkGateTests(unittest.IsolatedAsyncioTestCase):
    """cog の門番: 本文が空でも画像添付だけで会話へ入る。"""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = TranslationSettings(Path(self.temp.name) / "settings.json")
        self.client = SimpleNamespace(
            context_window=4096,
            model="",
            prompt_tokens=AsyncMock(return_value=0),
            complete=AsyncMock(return_value="回答"),
            close=AsyncMock(),
        )
        self.controller = SimpleNamespace(set_enabled=AsyncMock())
        self.bot = SimpleNamespace(user=SimpleNamespace(id=99))
        self.cog = TranslationCog(
            self.bot,
            self.settings,
            self.client,
            self.controller,
            TalkLog(None, None),
            SessionStore(Path(self.temp.name) / "sessions"),
        )
        self.member = SimpleNamespace(id=2, display_name="User", bot=False, voice=None)
        self.guild = SimpleNamespace(
            id=1,
            voice_channels=[],
            stage_channels=[],
            get_member=lambda user_id: self.member,
        )
        self.channel = SimpleNamespace(
            id=10, history=self.history, fetch_message=AsyncMock(), typing=lambda: NullContext()
        )
        patcher = patch.object(self.cog.config, "allowed_channel_ids", {10})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.settings.configure(1, 10, set(), False, talk=True)
        await self.cog.cog_load()

    async def asyncTearDown(self):
        await self.cog.cog_unload()

    async def history(self, **kwargs):
        """履歴は使わない（セッション履歴を渡すので reply_tree は走らない）。"""

        for message in ():
            yield message

    def message(self, text):
        return SimpleNamespace(
            id=100,
            content=text,
            clean_content=text,
            author=self.member,
            guild=self.guild,
            channel=self.channel,
            reference=None,
            mentions=[self.bot.user] if "<@99>" in text else [],
            webhook_id=None,
            attachments=[],
            reply=AsyncMock(return_value=SimpleNamespace(id=101)),
        )

    async def test_image_attachment_alone_reaches_the_conversation(self):
        message = self.message("<@99>")
        message.attachments = [
            SimpleNamespace(filename="photo.png", size=64, read=AsyncMock(return_value=png_bytes()))
        ]
        await self.cog.on_message(message)
        await asyncio.wait_for(self.cog.queue.join(), 1)

        message.reply.assert_awaited_once()
        self.assertIn("画像を読めません", message.reply.call_args.args[0])

    async def test_unreadable_attachment_alone_stays_ignored(self):
        message = self.message("<@99>")
        message.attachments = [SimpleNamespace(filename="data.zip", size=64, read=AsyncMock())]
        await self.cog.on_message(message)
        await asyncio.sleep(0.05)

        message.reply.assert_not_awaited()
