import unittest
from pathlib import Path

from jllm.kernel import Kernel, discover, load_profile, resolve

PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "tts-voicevox.yaml"


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


if __name__ == "__main__":
    unittest.main()
