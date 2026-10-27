import tempfile
import unittest
from pathlib import Path

from jllm.kernel import Kernel, discover, load_profile, resolve

PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "speech-services.yaml"


class SpeechServicesTests(unittest.IsolatedAsyncioTestCase):
    async def test_dictionary_and_effect_plugins_start(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = load_profile(PROFILE)
            profile.plugins["storage.local"]["config"]["data_dir"] = temp
            profile.plugins["speech.effects"]["config"]["ir_path"] = "custom-room.wav"
            plan = resolve(profile, discover())
            kernel = Kernel(plan)
            try:
                await kernel.start()
                self.assertIn("dictionary.sound", plan.nodes["dictionary.sound/main"].services)
                self.assertIn("dictionary.reading", plan.nodes["dictionary.reading/main"].services)
                effects = plan.nodes["speech.effects/main"].services["speech.effects"]
                self.assertIn("custom-room.wav", effects.build_filter([("ec", 1)]))
                self.assertIn("リバーブ", effects.describe_effects([("rb", 2)]))
            finally:
                await kernel.stop()
            self.assertTrue(all(node.state == "STOPPED" for node in plan.nodes.values()))


if __name__ == "__main__":
    unittest.main()
