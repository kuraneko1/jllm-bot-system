import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from jllm_plugins.playback.player import SpeechPlayer

from jllm.kernel import Kernel, discover, load_profile, resolve

PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "speech-services.yaml"


class PlaybackPluginTests(unittest.IsolatedAsyncioTestCase):
    async def test_factory_creates_and_closes_player(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = load_profile(PROFILE)
            profile.plugins["storage.local"]["config"]["data_dir"] = temp
            plan = resolve(profile, discover())
            kernel = Kernel(plan)
            try:
                await kernel.start()
                factory = plan.nodes["speech.playback/main"].services["speech.playback"]
                voice = SimpleNamespace(
                    guild=None,
                    channel=None,
                    is_connected=lambda: False,
                    _get_voice_packet=None,
                )
                player = factory.create(voice, SimpleNamespace(), 123)
                self.assertIsInstance(player, SpeechPlayer)
                self.assertEqual(player.text_channel_id, 123)
                self.assertEqual(player.speed, 1.2)
                self.assertEqual(player.volume, 0.2)
            finally:
                await kernel.stop()
            self.assertTrue(player.closed)
            self.assertTrue(all(node.state == "STOPPED" for node in plan.nodes.values()))


if __name__ == "__main__":
    unittest.main()
