import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import jsonschema
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
from jllm_plugins.dashboard.dashboard import DashboardConfig, ReadingDashboard
from jllm_plugins.dictionaries.dictionary import AudioProcessingConfig, transcode_to_wav
from jllm_plugins.dictionaries.readings import ReadingDictionary
from jllm_plugins.dictionaries.sound_plugin import Plugin
from jllm_plugins.reader.core import ReaderCog, ReaderConfig
from jllm_plugins.restrictions.service import UserRestrictions
from jllm_plugins.storage.plugin import LocalStorage
from jllm_plugins.voice_settings.settings import VoiceSettings

from tests.passkey_auth import ensure_admin_session
from tests.test_voice_admin import FakeVoiceTTS

SCHEMA_PATH = (Path(__file__).resolve().parents[1] / "plugins/builtins/src/"
               "jllm_plugins/dictionaries/sound_config.schema.json")


class AudioConfigIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_config_flows_into_both_registration_paths_and_ffmpeg(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            storage = LocalStorage(root)
            values = {"threshold_db": "-35dB", "keep_start_seconds": 0.2,
                      "keep_end_seconds": 0.3, "loudness_i": "-20",
                      "loudness_tp": "-2.5", "loudness_lra": "8"}
            schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
            jsonschema.validate({"audio_processing": values}, schema)
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate({"audio_processing": {"loudness_i": "-16,evil"}}, schema)
            ctx = SimpleNamespace(dependencies={"storage.local": storage},
                                  config={"audio_processing": values})
            dictionary = (await Plugin().setup(ctx))["dictionary.sound"]
            config = dictionary.audio_config
            self.assertEqual(config, AudioProcessingConfig(**values))
            settings = VoiceSettings(storage, {})
            readings = ReadingDictionary(str(root / "readings.json"))
            tts = FakeVoiceTTS()
            reader = ReaderCog(
                SimpleNamespace(user=SimpleNamespace(id=999)), tts, None, settings, dictionary,
                readings, UserRestrictions(), ReaderConfig(owner_id=42,
                    toggles_path=str(root / "toggles.json")),
            )
            reader._rebuild_sound_dashboard = lambda: None
            captured = []

            def convert(data, suffix, target, audio_config):
                captured.append(audio_config)
                target.write_bytes(b"converted")
                return True

            interaction = SimpleNamespace(
                user=SimpleNamespace(id=42, display_name="tester"),
                response=SimpleNamespace(defer=AsyncMock()),
                followup=SimpleNamespace(send=AsyncMock()),
            )
            attachment = SimpleNamespace(filename="test.wav", read=AsyncMock(return_value=b"audio"))
            with patch("jllm_plugins.reader.core.transcode_to_wav", side_effect=convert):
                await reader.dict_add.callback(reader, interaction, "first", attachment)
            self.assertEqual(captured, [config])
            dashboard = ReadingDashboard(
                readings, tts, reader=reader, password_file=str(root / "auth.json"),
                config=DashboardConfig(webchat_dir=str(root / "webchat")),
            )
            client = TestClient(TestServer(dashboard.app))
            await client.start_server()
            try:
                await ensure_admin_session(client, dashboard)
                form = FormData()
                form.add_field("phrase", "second")
                form.add_field("file", b"audio", filename="test.wav", content_type="audio/wav")
                with patch("jllm_plugins.dashboard.dashboard_dictionaries.transcode_to_wav",
                           side_effect=convert):
                    response = await client.post("/api/sounds/upload", data=form)
                self.assertEqual(response.status, 201, await response.text())
                self.assertEqual(captured, [config, config])
            finally:
                await client.close()

            commands = []
            def run(command, **kwargs):
                commands.append(command)
                Path(command[-1]).write_bytes(b"X" * 129)
                return SimpleNamespace(returncode=0)
            with patch("jllm_plugins.dictionaries.dictionary.subprocess.run", side_effect=run):
                self.assertTrue(transcode_to_wav(b"audio", ".wav", root / "out.wav", config))
            trim = commands[0][commands[0].index("-af") + 1]
            loudness = commands[1][commands[1].index("-af") + 1]
            self.assertIn("start_threshold=-35dB", trim)
            self.assertIn("start_silence=0.2", trim)
            self.assertIn("start_silence=0.3", trim)
            self.assertIn("I=-20:TP=-2.5:LRA=8", loudness)
