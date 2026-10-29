import unittest
from pathlib import Path

from jllm.kernel import discover, load_profile, resolve

PROFILES = Path(__file__).resolve().parents[1] / "profiles"


class ExampleProfileTests(unittest.TestCase):
    def test_every_example_profile_resolves_without_blocked_nodes(self):
        manifests = discover()
        paths = sorted(PROFILES.glob("*.yaml"))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(profile=path.name):
                plan = resolve(load_profile(path), manifests)
                blocked = {
                    node.id: node.reason
                    for node in plan.nodes.values()
                    if node.state == "BLOCKED"
                }
                self.assertEqual(blocked, {})


if __name__ == "__main__":
    unittest.main()
