import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from jllm.kernel import Kernel, discover, load_profile, resolve

PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "tts-router.yaml"


class TTSRouterIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_four_providers_bind_in_profile_order(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = load_profile(PROFILE)
            profile.plugins["storage.local"]["config"]["data_dir"] = temp
            profile.plugins["provider.tts.irodori"]["instances"]["home"]["data_dir"] = temp
            plan = resolve(profile, discover())
            kernel = Kernel(plan)
            with patch(
                "jllm_plugins.tts_router.plugin.TTSRouter.probe",
                new=AsyncMock(return_value=True),
            ):
                try:
                    await kernel.start()
                    router = plan.nodes["tts.router/main"].services["tts.synthesis"]
                    self.assertEqual(router.active, "provider.tts.irodori/home")
                    self.assertEqual(router.preferred, "provider.tts.irodori/home")
                    self.assertEqual(
                        [spec.id for spec in router.specs],
                        [
                            "provider.tts.irodori/home",
                            "provider.tts.speechcpp/local",
                            "provider.tts.openai/drama3",
                            "provider.tts.voicevox/local",
                        ],
                    )
                    self.assertTrue(router.failover)
                    self.assertEqual(
                        plan.nodes["tts.router/main"].state,
                        "RUNNING",
                    )
                finally:
                    await kernel.stop()
            for backend in router.backends.values():
                self.assertTrue(backend.session.closed)
            self.assertTrue((Path(temp) / "tts_state.json").is_file())


if __name__ == "__main__":
    unittest.main()
