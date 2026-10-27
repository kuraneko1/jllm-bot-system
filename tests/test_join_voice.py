"""入室案内の音声キャッシュと、miq のサーバー表示名。"""

import asyncio
import tempfile
import unittest
from pathlib import Path

from jllm_plugins.voice_settings.join_voice import JoinVoiceCache


class JoinVoiceCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / "join_voice"
        self.cache = JoinVoiceCache(self.directory, voice="clone こしあん", limit=10)
        self.calls = []

    async def synthesize(self, text):
        self.calls.append(text)

        return f"wav:{text}".encode()

    async def test_first_time_creates_and_second_time_reuses(self):
        first = await self.cache.get_or_create(123, "こしあんさんが入室しました。", self.synthesize)
        second = await self.cache.get_or_create(
            123, "こしあんさんが入室しました。", self.synthesize
        )

        self.assertEqual(first, second)
        self.assertTrue(await asyncio.to_thread(Path(first).is_file))
        self.assertEqual(self.calls, ["こしあんさんが入室しました。"])  # 2回目は生成しない
        self.assertEqual(
            await asyncio.to_thread(Path(first).read_bytes),
            "wav:こしあんさんが入室しました。".encode(),
        )

    async def test_name_change_makes_a_new_file(self):
        first = await self.cache.get_or_create(123, "こしあんさんが入室しました。", self.synthesize)
        renamed = await self.cache.get_or_create(123, "あんこさんが入室しました。", self.synthesize)

        self.assertNotEqual(first, renamed)
        self.assertEqual(len(self.calls), 2)

    async def test_another_member_gets_their_own_file(self):
        first = await self.cache.get_or_create(123, "同じ文", self.synthesize)
        other = await self.cache.get_or_create(456, "同じ文", self.synthesize)

        self.assertNotEqual(first, other)
        self.assertTrue(Path(first).name.startswith("123-"))
        self.assertTrue(Path(other).name.startswith("456-"))

    async def test_voice_change_makes_a_new_file(self):
        other_voice = JoinVoiceCache(self.directory, voice="別の話者", limit=10)
        first = await self.cache.get_or_create(123, "同じ文", self.synthesize)
        second = await other_voice.get_or_create(123, "同じ文", self.synthesize)

        self.assertNotEqual(first, second)

    async def test_failure_returns_none_and_leaves_nothing(self):
        async def broken(text):
            raise RuntimeError("TTSが落ちている")

        self.assertIsNone(await self.cache.get_or_create(123, "文", broken))
        self.assertEqual(list(self.directory.glob("*.wav")), [])

    async def test_empty_audio_is_not_saved(self):
        async def empty(text):
            return b""

        self.assertIsNone(await self.cache.get_or_create(123, "文", empty))
        self.assertEqual(list(self.directory.glob("*.wav")), [])

    async def test_old_files_are_pruned(self):
        for index in range(14):
            await self.cache.get_or_create(index, f"文{index}", self.synthesize)

        files = list(self.directory.glob("*.wav"))
        self.assertEqual(len(files), 10)

    def test_get_does_not_create_anything(self):
        self.assertIsNone(self.cache.get(123, "まだ無い"))
        self.assertFalse(self.directory.exists())


if __name__ == "__main__":
    unittest.main()
