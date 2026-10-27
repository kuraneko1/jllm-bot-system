import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jllm.kernel import Kernel, discover, load_profile, resolve

PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "tts-voicevox.yaml"
OPENAI_PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "tts-openai.yaml"
SPEECHCPP_PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "tts-speechcpp.yaml"
IRODORI_PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "tts-irodori.yaml"


class TTSIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_voicevox_is_bound_to_synthesis_without_network(self):
        plan = resolve(load_profile(PROFILE), discover())
        kernel = Kernel(plan)
        try:
            await kernel.start()
            provider = plan.nodes["provider.tts.voicevox/local"]
            direct = plan.nodes["tts.direct/main"]
            self.assertIs(direct.services["tts.synthesis"], provider.services["tts.provider"])
            self.assertEqual(provider.services["tts.provider"].base_url, "http://127.0.0.1:50021")
        finally:
            await kernel.stop()
        self.assertTrue(provider.services["tts.provider"].session.closed)

    async def test_openai_config_is_bound_without_network(self):
        plan = resolve(load_profile(OPENAI_PROFILE), discover())
        kernel = Kernel(plan)
        try:
            await kernel.start()
            provider = plan.nodes["provider.tts.openai/drama3"]
            backend = provider.services["tts.provider"]
            self.assertIs(plan.nodes["tts.direct/main"].services["tts.synthesis"], backend)
            self.assertEqual(backend.default_voice, "test")
            self.assertEqual(backend.model_name, "drama3-small")
            self.assertEqual(backend.seed, 1234)
            self.assertTrue(backend.ignore_speaker_speed)
        finally:
            await kernel.stop()
        self.assertTrue(backend.session.closed)

    async def test_speechcpp_config_is_bound_without_network(self):
        plan = resolve(load_profile(SPEECHCPP_PROFILE), discover())
        kernel = Kernel(plan)
        try:
            await kernel.start()
            provider = plan.nodes["provider.tts.speechcpp/local"]
            backend = provider.services["tts.provider"]
            self.assertIs(plan.nodes["tts.direct/main"].services["tts.synthesis"], backend)
            self.assertEqual(backend.base_url, "http://127.0.0.1:8089")
            self.assertEqual(backend.default_voice, "none")
            self.assertEqual(backend.model_name, "")
        finally:
            await kernel.stop()
        self.assertTrue(backend.session.closed)

    async def test_irodori_config_and_secret_env_are_bound(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = load_profile(IRODORI_PROFILE)
            profile.plugins["provider.tts.irodori"]["instances"]["home"]["data_dir"] = temp
            plan = resolve(profile, discover())
            kernel = Kernel(plan)
            with patch.dict("os.environ", {"IRODORI_API_KEY": "secret"}, clear=True):
                try:
                    await kernel.start()
                    provider = plan.nodes["provider.tts.irodori/home"]
                    backend = provider.services["tts.provider"]
                    self.assertIs(plan.nodes["tts.direct/main"].services["tts.synthesis"], backend)
                    self.assertEqual(backend.default_voice, "none")
                    self.assertEqual(backend.api_key, "secret")
                    self.assertEqual(
                        backend.disabled_voices_path, Path(temp) / "disabled_voices.json"
                    )
                    self.assertEqual(backend.chunk_min_chars, 80)
                    self.assertEqual(backend.first_chunk_min_chars, 12)
                finally:
                    await kernel.stop()
            self.assertTrue(backend.session.closed)


if __name__ == "__main__":
    unittest.main()
