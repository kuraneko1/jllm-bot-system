import unittest
from pathlib import Path

from jllm.kernel import Kernel, discover, load_profile, resolve


class MinimalProfileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.profile = Path(__file__).resolve().parents[1] / "profiles" / "minimal.yaml"

    async def test_installed_plugins_discovery_start_and_stop(self):
        discovered = discover()
        self.assertTrue(
            {"fake.provider", "fake.router", "fake.consumer"} <= discovered.keys(),
            "install the jllm-builtins workspace package before running tests",
        )
        plan = resolve(load_profile(self.profile), discovered)
        kernel = Kernel(plan)
        try:
            await kernel.start()
            self.assertTrue(all(node.state == "RUNNING" for node in plan.nodes.values()))
            self.assertEqual(plan.nodes["fake.consumer/main"].plugin.result, "first: hello")
            self.assertEqual(
                plan.nodes["fake.router/main"].plugin.dependency_ids,
                ["fake.provider/first", "fake.provider/second"],
            )
        finally:
            await kernel.stop()
        self.assertTrue(all(node.state == "STOPPED" for node in plan.nodes.values()))
