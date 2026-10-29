import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer
from jllm_plugins.dashboard.dashboard import DashboardConfig, ReadingDashboard
from jllm_plugins.dictionaries.dictionary import SoundDictionary
from jllm_plugins.dictionaries.readings import ReadingDictionary
from jllm_plugins.reader.core import ReaderCog, ReaderConfig
from jllm_plugins.restrictions.service import UserRestrictions
from jllm_plugins.storage.plugin import LocalStorage
from jllm_plugins.voice_settings.settings import VoiceSettings

from tests.passkey_auth import ensure_admin_session
from tests.test_voice_admin import FakeVoiceTTS


class VoiceSettingsCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = LocalStorage(self.root)
        self.tts = FakeVoiceTTS()
        self.readings = ReadingDictionary(str(self.root / "readings.json"))
        self.dictionary = SoundDictionary(str(self.root / "sounds.json"), str(self.root / "sounds"))

    def make_reader(self):
        settings = VoiceSettings(self.storage, {})
        reader = ReaderCog(
            SimpleNamespace(user=SimpleNamespace(id=999)), self.tts, None, settings,
            self.dictionary, self.readings, UserRestrictions(),
            ReaderConfig(toggles_path=str(self.root / "toggles.json"),
                         vc_announcement_cooldown_seconds=100),
        )
        return reader, settings

    def test_legacy_voice_maps_and_announcement_history(self):
        for name in ("user_voices.json", "forced_voices.json"):
            (self.root / name).write_text(
                json.dumps({"1:1": 123, "1:2": None, "1:3": True}), encoding="utf-8"
            )
        history = {"ok": 9950, "cutoff": 9900, "old": 9899, "text": "9999",
                   "bool": True, "nan": float("nan"), "infinity": float("inf")}
        (self.root / "vc_announcements.json").write_text(json.dumps(history), encoding="utf-8")
        with patch("jllm_plugins.reader.core.time.time", return_value=10000):
            reader, settings = self.make_reader()
        self.assertEqual(reader.user_voices, {"1:1": "123", "1:2": "None", "1:3": "True"})
        self.assertEqual(reader.forced_voices, reader.user_voices)
        self.assertEqual(reader.voice_entries, {"ok": 9950.0, "cutoff": 9900.0})
        self.assertIs(reader.voice_entries, settings.voice_entries.entries)
        reader._save_voice_history()
        self.assertEqual(json.loads((self.root / "vc_announcements.json").read_text()),
                         {"ok": 9950.0, "cutoff": 9900.0})

    async def test_real_reader_dashboard_voice_settings_survive_restart(self):
        reader, settings = self.make_reader()
        self.assertIs(reader.voice_speeds, settings.speeds)
        self.assertIs(reader.voice_volumes, settings.volumes)
        dashboard = ReadingDashboard(
            self.readings, self.tts, reader=reader, password_file=str(self.root / "auth.json"),
            config=DashboardConfig(webchat_dir=str(self.root / "webchat")),
        )
        client = TestClient(TestServer(dashboard.app))
        await client.start_server()
        try:
            await ensure_admin_session(client, dashboard)
            for field, value in (("speed", 1.5), ("volume", 2.0)):
                response = await client.put(f"/api/voice-library/{field}",
                                            json={"name": "Clone Ryu", field: value})
                self.assertEqual(response.status, 200, await response.text())
            self.assertEqual(await reader.tts_speed_for("Clone Ryu"), 1.5)
            self.assertEqual(await reader.tts_gain_for("Clone Ryu"), 2.0)
            restarted, restarted_settings = self.make_reader()
            dashboard.reader = restarted
            self.assertIs(restarted.voice_speeds, restarted_settings.speeds)
            self.assertIs(restarted.voice_volumes, restarted_settings.volumes)
            response = await client.get("/api/voice-library")
            self.assertEqual(response.status, 200)
            library = await response.json()
            voice = next(item for item in library["voices"] if item["name"] == "Clone Ryu")
            self.assertEqual((voice["speed"], voice["volume"]), (1.5, 2.0))
        finally:
            await client.close()
