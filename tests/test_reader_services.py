import tempfile
import unittest
from pathlib import Path

from jllm.kernel import Kernel, discover, load_profile, resolve

PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "reader-services.yaml"


class ReaderServicesTests(unittest.IsolatedAsyncioTestCase):
    async def test_reader_support_plugins_start(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = load_profile(PROFILE)
            profile.plugins["storage.local"]["config"]["data_dir"] = temp
            plan = resolve(profile, discover())
            kernel = Kernel(plan)
            try:
                await kernel.start()
                settings = plan.nodes["voice.settings/main"].services["voice.settings"]
                settings.user_voices.set("42", "clone")
                self.assertEqual(settings.user_voices.get("42"), "clone")
                self.assertEqual(settings.join_cache.voice, "clone こしあん")
                self.assertEqual(
                    plan.nodes["speech.text/main"]
                    .services["speech.text"]
                    .parse_speed_pieces("A f2", 1.2),
                    [("A", 2.0)],
                )
                self.assertIn("feature.bookmarks", plan.nodes["feature.bookmarks/main"].services)
            finally:
                await kernel.stop()
            self.assertTrue(all(node.state == "STOPPED" for node in plan.nodes.values()))


if __name__ == "__main__":
    unittest.main()
