import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.test_reader_core import ReaderCoreTests


class ReaderDictionaryCommandTests(ReaderCoreTests):
    def setUp(self):
        super().setUp()
        self.cog.config.owner_id = 99

    def interaction(self, user_id=99):
        return SimpleNamespace(
            user=SimpleNamespace(id=user_id, display_name="tester"),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            channel=SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock()))),
        )

    def file(self):
        return SimpleNamespace(filename="sample.wav", read=AsyncMock(return_value=b"RIFFfake"))

    def transcode(self):
        def convert(data, suffix, target, config):
            Path(target).write_bytes(b"RIFFfake")
            return True

        return convert

    async def test_owner_dict_add_is_immediate(self):
        call = self.interaction()
        with patch("jllm_plugins.reader.core.transcode_to_wav", side_effect=self.transcode()):
            await self.cog.dict_add.callback(self.cog, call, "mar", self.file())
        self.assertEqual(len(self.dictionary.entries["mar"]), 1)
        self.assertTrue(self.dictionary.entries["mar"][0].startswith("sound-"))
        call.followup.send.assert_awaited_once()
        self.assertFalse(self.cog.pending_sound)

    async def test_nonowner_dict_add_creates_approval(self):
        call = self.interaction(42)
        with patch("jllm_plugins.reader.core.transcode_to_wav", side_effect=self.transcode()):
            await self.cog.dict_add.callback(self.cog, call, "mar", self.file())
        self.assertEqual(len(self.cog.pending_sound), 1)
        call.channel.send.assert_awaited_once()
        request_id = next(iter(self.cog.pending_sound))
        result = await self.cog._approve_sound(request_id)
        self.assertIn("登録しました", result)
        self.assertIn("mar", self.dictionary.entries)

    async def test_reading_add_and_remove(self):
        call = self.interaction()
        await self.cog.reading_add.callback(self.cog, call, "v100", "ぶいひゃく")
        self.assertEqual(self.readings.apply("v100"), "ぶいひゃく")
        await self.cog.reading_remove.callback(self.cog, call, "v100")
        self.assertEqual(self.readings.apply("v100"), "v100")


if __name__ == "__main__":
    unittest.main()
