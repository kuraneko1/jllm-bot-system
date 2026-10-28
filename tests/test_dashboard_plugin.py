import tempfile
import unittest
from pathlib import Path

from jllm_plugins.llm import llm_backend

from jllm.kernel import Kernel, discover, load_profile, resolve

PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "dashboard.yaml"


class DashboardPluginTests(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_starts_and_stops_http_server(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = load_profile(PROFILE)
            profile.plugins["storage.local"]["config"]["data_dir"] = temp
            plan = resolve(profile, discover())
            kernel = Kernel(plan)
            try:
                await kernel.start()
                node = plan.nodes["web.dashboard/main"]
                self.assertEqual(node.state, "RUNNING")
                dashboard = node.services["web.dashboard"]
                self.assertIsNotNone(dashboard._runner)
                self.assertIsNotNone(dashboard.webchat)
            finally:
                await kernel.stop()
            llm_backend._reset_config()
            self.assertTrue(all(node.state == "STOPPED" for node in plan.nodes.values()))


if __name__ == "__main__":
    unittest.main()
