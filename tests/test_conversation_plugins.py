import tempfile
import unittest
from pathlib import Path

from jllm_plugins.llm import llm_backend

from jllm.kernel import Kernel, discover, load_profile, resolve

PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "conversation.yaml"


class ConversationPluginTests(unittest.IsolatedAsyncioTestCase):
    async def test_conversation_services_start(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = load_profile(PROFILE)
            profile.plugins["storage.local"]["config"]["data_dir"] = temp
            plan = resolve(profile, discover())
            kernel = Kernel(plan)
            try:
                await kernel.start()
                self.assertIn("store.sessions", plan.nodes["store.sessions/main"].services)
                self.assertIn(
                    "feature.conversation", plan.nodes["feature.conversation/main"].services
                )
                self.assertIn("feature.kitasan", plan.nodes["feature.kitasan/main"].services)
                self.assertIn("feature.talk-log", plan.nodes["feature.talk-log/main"].services)
                self.assertIn("llm.read-mode", plan.nodes["llm.router/main"].services)
            finally:
                await kernel.stop()
            llm_backend._reset_config()
            self.assertTrue(all(node.state == "STOPPED" for node in plan.nodes.values()))


if __name__ == "__main__":
    unittest.main()
