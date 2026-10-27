import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestServer
from jllm_plugins.tts_irodori.backend import IrodoriBackend
from jllm_tts import TTSBackendError


class DisabledVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "disabled_voices.json"
        self.entries = [
            {"id": "none", "no_ref": True},
            {"id": "Clone Ryu", "ref_wav": "voices/Clone Ryu.wav"},
            {"id": "Clone Summer", "ref_wav": "voices/Clone Summer.wav"},
        ]
        app = web.Application()
        app.router.add_get("/v1/audio/voices", self.listing)
        self.server = TestServer(app)
        await self.server.start_server()
        self.backend = IrodoriBackend(
            str(self.server.make_url("/")).rstrip("/"), disabled_voices_path=str(self.path)
        )

    async def listing(self, request):
        return web.json_response({"data": self.entries})

    async def asyncTearDown(self):
        await self.backend.close()
        await self.server.close()
        self.directory.cleanup()

    async def test_disable_persists_preserves_ids_and_blocks_name_and_id(self):
        await self.backend.list_voices()
        await self.backend.set_voice_enabled("Clone Ryu", False)
        voices = await self.backend.list_voices()
        self.assertEqual([(v.id, v.name) for v in voices], [(1, "none"), (3, "Clone Summer")])
        self.assertEqual(self.backend._resolve_voice("Clone Ryu"), "none")
        self.assertEqual(self.backend._resolve_voice(2), "none")
        self.assertEqual(self.backend._resolve_voice(3), "Clone Summer")
        self.assertEqual(json.loads(self.path.read_text()), ["Clone Ryu"])
        restored = IrodoriBackend(self.backend.base_url, disabled_voices_path=str(self.path))
        try:
            self.assertIn("Clone Ryu", restored.disabled_voices)
            await restored.set_voice_enabled("Clone Ryu", True)
            self.assertIn("Clone Ryu", [v.name for v in await restored.list_voices()])
        finally:
            await restored.close()

    async def test_builtin_not_disabled_and_failed_save_does_not_change_memory(self):
        with self.assertRaises(TTSBackendError):
            await self.backend.set_voice_enabled("none", False)
        with patch(
            "jllm_plugins.tts_irodori.backend.os.replace", side_effect=OSError("disk error")
        ):
            with self.assertRaises(TTSBackendError):
                await self.backend.set_voice_enabled("Clone Ryu", False)
        self.assertEqual(self.backend.disabled_voices, set())

    async def test_disabled_default_voice_falls_back_to_none(self):
        self.backend.default_voice = "Clone Ryu"
        await self.backend.set_voice_enabled("Clone Ryu", False)
        await self.backend.list_voices()
        self.assertEqual(self.backend._resolve_voice("Clone Ryu"), "none")
