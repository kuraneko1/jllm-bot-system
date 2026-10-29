import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from jllm_plugins.reader.core import ReaderCog
from jllm_plugins.tts_router.router import TTSRouter
from jllm_plugins.vram_control.service import VramControlError
from jllm_tts import TTSBackendError

from tests.test_reader_core import ReaderCoreTests


class TTSCommandTests(ReaderCoreTests):
    def setUp(self):
        super().setUp()
        self.cog.config.owner_id = 99
        self.cog.config.tts_switch_user_ids = frozenset({42})
        self.cog.tts = Mock(spec=TTSRouter)
        self.cog.tts.disabled_voices = set()
        self.cog.tts.speakers = AsyncMock(
            return_value=[{"styles": [{"id": "clone こしあん", "name": "clone こしあん"}]}]
        )
        self.cog.tts.synthesize = AsyncMock(return_value=b"wav")
        self.tts = self.cog.tts
        self.cog.tts.describe.return_value = "router"
        self.cog.tts.check_all = AsyncMock(
            return_value={
                "backends": [
                    {
                        "id": "home",
                        "display": "自宅",
                        "kind": "irodori",
                        "display_name": "自宅",
                        "active": True,
                        "preferred": True,
                        "healthy": True,
                        "error": "",
                    }
                ]
            }
        )
        self.cog.tts.set_active = AsyncMock(
            return_value={
                "active": "home",
                "preferred": "home",
                "healthy": True,
            }
        )

    def interaction(self, user_id=99):
        return SimpleNamespace(
            user=SimpleNamespace(id=user_id),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def test_status_check_and_switch(self):
        call = self.interaction(42)
        await self.cog.tts_status.callback(self.cog, call)
        await self.cog.tts_check.callback(self.cog, call)
        await self.cog.tts_switch.callback(self.cog, call, "home")
        self.assertEqual(self.cog.tts.check_all.await_count, 2)
        self.cog.tts.set_active.assert_awaited_once_with("home")
        self.assertEqual(call.followup.send.await_count, 3)

    async def test_switch_rejects_unauthorized_and_unknown(self):
        call = self.interaction(1)
        await self.cog.tts_switch.callback(self.cog, call, "home")
        self.cog.tts.set_active.assert_not_called()
        call = self.interaction(99)
        self.cog.tts.set_active.side_effect = TTSBackendError("そのバックエンドはありません")
        await self.cog.tts_switch.callback(self.cog, call, "unknown")
        self.assertIn("切り替えできません", call.followup.send.await_args.args[0])

    async def test_vram_free_and_restore(self):
        self.cog.vram = SimpleNamespace(
            configured=True,
            status=AsyncMock(
                return_value={"service": "drama3", "vram": {"used_mib": 10, "total_mib": 100}}
            ),
            free=AsyncMock(
                return_value={"service": "drama3", "vram": {"used_mib": 10, "total_mib": 100}}
            ),
            restore=AsyncMock(
                return_value={"service": "drama3", "vram": {"used_mib": 20, "total_mib": 100}}
            ),
        )
        call = self.interaction()
        await self.cog._tts_vram(call, "free")
        await self.cog._tts_vram(call, "restore")
        self.cog.vram.free.assert_awaited_once()
        self.cog.vram.restore.assert_awaited_once()
        self.assertEqual(ReaderCog.tts_target("vram-free"), ("vram", "free"))

    async def test_vram_error_is_reported(self):
        self.cog.vram = SimpleNamespace(
            configured=True,
            free=AsyncMock(side_effect=VramControlError("failed")),
        )
        call = self.interaction()
        await self.cog._tts_vram(call, "free")
        self.assertIn("GPUの操作に失敗しました", call.followup.send.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
