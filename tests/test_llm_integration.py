import tempfile
import unittest
from pathlib import Path

from jllm_plugins.llm import llm_backend

from jllm.kernel import Kernel, discover, load_profile, resolve

PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "llm.yaml"


class LLMIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_router_switches_backend_and_read_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = load_profile(PROFILE)
            profile.plugins["storage.local"]["config"]["data_dir"] = temp
            plan = resolve(profile, discover())
            kernel = Kernel(plan)
            try:
                await kernel.start()
                client = plan.nodes["llm.router/main"].services["llm.chat"]
                state = plan.nodes["llm.router/main"].services["llm.read-mode"]
                self.assertEqual(client.backend, "sarashina")
                self.assertEqual(state.active_read_mode(), "tts")
                await client.switch("atmos")
                self.assertEqual(client.backend, "atmos")
                self.assertEqual(state.active_read_mode(), "wav")
                self.assertEqual(state.backend_for(), "atmos")
                state.set_channel(123, "sarashina")
                self.assertEqual(state.backend_for(123), "sarashina")
                state.clear_channel(123)
                self.assertEqual(state.backend_for(123), "atmos")
                await client.switch("sarashina")
                self.assertEqual(state.active_read_mode(), "tts")
            finally:
                await kernel.stop()
            llm_backend._reset_config()
            self.assertTrue(all(node.state == "STOPPED" for node in plan.nodes.values()))


if __name__ == "__main__":
    unittest.main()
