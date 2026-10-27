import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from jllm_plugins.media.store import MediaConfig, MediaStore
from jllm_plugins.storage.plugin import LocalStorage

from tests.test_reader_core import ReaderCoreTests, message


class MediaStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = MediaStore(LocalStorage(self.temp.name), MediaConfig())

    def tearDown(self):
        self.temp.cleanup()

    def test_approve_reject_and_safe_filenames(self):
        self.store.media["unsafe"] = "../outside.gif"
        self.store.media["safe"] = "safe.gif"
        self.store.data_path.write_text('{"safe": "safe.gif", "unsafe": "../outside.gif"}')
        self.store._load()
        self.assertNotIn("unsafe", self.store.media)

        request_id = self.store.register("mar", b"GIF89a-data", 42, 5, 10)
        self.store._compress_gif = Mock(
            side_effect=lambda source, target: target.write_bytes(b"GIF89a-compressed")
        )
        result = __import__("asyncio").run(self.store.approve(request_id))
        self.assertIn("登録しました", result)
        self.assertEqual(self.store.media["mar"], "mar-" + request_id + ".gif")
        self.assertFalse((self.store.pending_dir / (request_id + ".gif")).exists())
        self.assertEqual(self.store.media_key("MAR"), "mar")
        self.assertFalse(self.store.pending)

    def test_reject_and_expire_remove_pending_files(self):
        for action in (self.store.reject, self.store.expire):
            request_id = self.store.register("x", b"GIF89a", 1, 2, 3)
            path = self.store.pending[request_id]["path"]
            __import__("asyncio").run(action(request_id))
            self.assertFalse(Path(path).exists())


class ReaderMediaTests(ReaderCoreTests):
    def setUp(self):
        super().setUp()
        self.store = MediaStore(LocalStorage(self.root), MediaConfig(approver_id=42))
        self.cog.media_store = self.store
        self.cog.media = self.store.media
        self.cog.pending_media = self.store.pending
        self.cog.media_dir = self.store.media_dir
        self.cog.pending_media_dir = self.store.pending_dir
        self.store._compress_gif = Mock(
            side_effect=lambda source, target: target.write_bytes(b"GIF89a-compressed")
        )

    async def test_registration_by_approver_is_immediate(self):
        attachment = SimpleNamespace(
            content_type="image/gif",
            filename="sample.gif",
            size=11,
            read=AsyncMock(return_value=b"GIF89a-data"),
        )
        bot_user = self.cog.bot.user
        call = message("<@999> mar")
        call.mentions = [bot_user]
        call.attachments = [attachment]
        call.reply = AsyncMock()
        self.assertTrue(await self.cog._handle_media_registration(call))
        self.assertIn("mar", self.store.media)
        call.reply.assert_awaited_once()

    async def test_media_reply_uses_exact_trigger(self):
        target = self.store.media_dir / "a.gif"
        target.write_bytes(b"GIF89a")
        self.store.media["mar"] = "a.gif"
        call = message("mar")
        call.channel.send = AsyncMock()
        await self.cog._send_media(call)
        call.channel.send.assert_awaited_once()
        sent = call.channel.send.await_args.kwargs["file"]
        self.assertEqual(sent.filename, "a.gif")
        sent.close()

        call = message("tomar")
        call.channel.send = AsyncMock()
        await self.cog._send_media(call)
        call.channel.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
