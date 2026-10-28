"""GPU機のVRAM開放（制限付きSSHの呼び出し側）。"""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from jllm_plugins.vram_control.service import (
    NoVramController,
    VramControlError,
    VramController,
    build_vram_controller,
    vram_summary,
)


class FakeSshTests(unittest.IsolatedAsyncioTestCase):
    """``ssh`` を偽物に差し替えて、応答の解釈だけを見る。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bin = Path(self.tmp.name)
        fake = self.bin / "ssh"
        fake.write_text('#!/bin/sh\nprintf "%s" "$FAKE_VRAM_OUTPUT"\n', encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.bin}:{self.old_path}"
        self.addCleanup(os.environ.__setitem__, "PATH", self.old_path)
        self.controller = VramController("ryu@example", "/tmp/key", "/tmp/known_hosts")

    def respond(self, payload):
        os.environ["FAKE_VRAM_OUTPUT"] = (
            payload if isinstance(payload, str) else json.dumps(payload)
        )

    async def test_free_reads_the_vram_report(self):
        self.respond(
            {
                "ok": True,
                "action": "free",
                "stopped": ["irodori-tts.service"],
                "vram": {"used_mib": 900, "free_mib": 11000, "total_mib": 12282},
                "services": {"irodori-tts.service": False},
            }
        )

        result = await self.controller.free()

        self.assertEqual(result["action"], "free")
        self.assertEqual(vram_summary(result), "VRAM 使用 900MiB / 空き 11000MiB（全12282MiB）")

    async def test_restore_reports_failure_from_the_host(self):
        self.respond({"ok": False, "error": "読み上げサーバーを起動できませんでした。", "vram": {}})

        with self.assertRaises(VramControlError) as caught:
            await self.controller.restore()

        self.assertIn("起動できませんでした", str(caught.exception))

    async def test_broken_output_is_reported(self):
        self.respond("not json at all")

        with self.assertRaises(VramControlError):
            await self.controller.status()

    async def test_unknown_action_is_rejected_before_connecting(self):
        with self.assertRaises(VramControlError):
            await self.controller.run("rm-rf")


class BuildControllerTests(unittest.IsolatedAsyncioTestCase):
    """``VRAM_CONTROL_*`` の有無で実装が変わる（環境変数に依存しないよう直接差し替える）。"""

    async def test_without_settings_it_does_nothing(self):
        controller = build_vram_controller({})

        self.assertFalse(controller.configured)
        self.assertIsInstance(controller, NoVramController)

        with self.assertRaises(VramControlError):
            await controller.free()

    async def test_with_settings_it_builds_the_ssh_controller(self):
        controller = build_vram_controller({
            "VRAM_CONTROL_HOST": "ryu@192.168.0.22",
            "VRAM_CONTROL_KEY": "/home/ryu/.ssh/id_ed25519_vram",
            "VRAM_CONTROL_KNOWN_HOSTS": "/home/ryu/.ssh/vram_known_hosts",
        })

        self.assertTrue(controller.configured)
        self.assertEqual(controller.host, "ryu@192.168.0.22")
        self.assertEqual(controller.key, "/home/ryu/.ssh/id_ed25519_vram")


class VramSummaryTests(unittest.TestCase):
    def test_empty_report_is_blank(self):
        self.assertEqual(vram_summary({}), "")
        self.assertEqual(vram_summary({"vram": {}}), "")

    def test_missing_numbers_do_not_crash(self):
        self.assertEqual(
            vram_summary({"vram": {"used_mib": 10}}), "VRAM 使用 10MiB / 空き ?MiB（全?MiB）"
        )


class TtsTargetTests(unittest.TestCase):
    """``/tts switch`` の引数の振り分け。"""

    def test_vram_actions_are_selected_by_argument(self):
        from jllm_plugins.reader.core import ReaderCog

        self.assertEqual(ReaderCog.tts_target("vram-free"), ("vram", "free"))
        self.assertEqual(ReaderCog.tts_target(" vram-restore "), ("vram", "restore"))

    def test_backend_ids_stay_backends(self):
        from jllm_plugins.reader.core import ReaderCog

        self.assertEqual(ReaderCog.tts_target("home"), ("backend", "home"))
        self.assertEqual(ReaderCog.tts_target(""), ("backend", ""))


if __name__ == "__main__":
    unittest.main()
