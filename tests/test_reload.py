import unittest
from types import SimpleNamespace
from unittest.mock import patch

from jllm.kernel import Kernel, Manifest, Profile, resolve


def manifest(name, provides=(), requires=(), optional=()):
    def caps(items):
        return [
            dict(capability=c[0], version=c[1], **({"cardinality": c[2]} if len(c) > 2 else {}))
            for c in items
        ]

    return Manifest.from_dict({
        "id": name, "version": "1.0.0", "api": ">=1.0,<2.0",
        "entry": name + ":Plugin",
        "provides": caps(provides), "requires": caps(requires), "optional": caps(optional),
        "config_schema": {},
    })


class ReloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []
        self.manifests = {
            "p": manifest("p", [("echo", 1)]),
            "c": manifest("c", requires=[("echo", 1)]),
            "o": manifest("o"),
            "q": manifest("q"),
        }

    def make_plan(self, options):
        return resolve(Profile(options), self.manifests)

    def factory(self, name):
        calls = self.calls

        class Plugin:
            def __init__(self):
                self.name = name

            async def setup(self, ctx):
                calls.append(f"setup {name} {ctx.config}")
                if ctx.config.get("fail"):
                    raise ValueError("boom")
                return {"echo": self} if name == "p" else {}

            async def start(self):
                calls.append(f"start {name}")

            async def stop(self):
                calls.append(f"stop {name}")

        return SimpleNamespace(Plugin=Plugin)

    async def start(self, options):
        with patch(
            "jllm.kernel.importlib.import_module",
            side_effect=lambda name: self.factory(name),
        ):
            kernel = Kernel(self.make_plan(options))
            await kernel.start()
        return kernel

    async def reload(self, kernel, options):
        with patch(
            "jllm.kernel.importlib.import_module",
            side_effect=lambda name: self.factory(name),
        ):
            return await kernel.reload(self.make_plan(options))

    async def test_unchanged_nodes_are_kept_and_only_the_change_restarts(self):
        base = {"p": {}, "c": {}, "o": {}}
        kernel = await self.start(base)
        old_p = kernel.plan.nodes["p/main"]
        self.calls.clear()

        summary = await self.reload(kernel, {"p": {}, "c": {}, "o": {"config": {"x": 1}}})

        self.assertEqual(summary["stopped"], ["o/main"])
        self.assertEqual(summary["started"], ["o/main"])
        self.assertIs(kernel.plan.nodes["p/main"], old_p)
        self.assertEqual(kernel.plan.nodes["c/main"].state, "RUNNING")
        self.assertNotIn("stop p", self.calls)
        self.assertNotIn("stop c", self.calls)

    async def test_removing_a_plugin_stops_only_it_and_adding_starts_it(self):
        kernel = await self.start({"p": {}, "c": {}, "o": {}})
        self.calls.clear()

        removed = await self.reload(kernel, {"p": {}, "c": {}})
        self.assertEqual(removed["stopped"], ["o/main"])
        self.assertNotIn("o/main", kernel.plan.nodes)
        self.assertIn("stop o", self.calls)

        self.calls.clear()
        added = await self.reload(kernel, {"p": {}, "c": {}, "q": {}})
        self.assertEqual(added["started"], ["q/main"])
        self.assertEqual(kernel.plan.nodes["q/main"].state, "RUNNING")

    async def test_config_change_restarts_the_provider_and_its_dependents(self):
        kernel = await self.start({"p": {"config": {"mode": "a"}}, "c": {}, "q": {}})
        old_q = kernel.plan.nodes["q/main"]
        self.calls.clear()

        summary = await self.reload(kernel, {"p": {"config": {"mode": "b"}}, "c": {}, "q": {}})

        self.assertEqual(sorted(summary["started"]), ["c/main", "p/main"])
        self.assertEqual(sorted(summary["stopped"]), ["c/main", "p/main"])
        self.assertIs(kernel.plan.nodes["q/main"], old_q)
        self.assertLess(self.calls.index("stop c"), self.calls.index("stop p"))

    async def test_failed_reload_restores_the_previous_plan(self):
        kernel = await self.start({"p": {"config": {"mode": "a"}}, "c": {}})
        with self.assertRaises(RuntimeError):
            await self.reload(
                kernel,
                {"p": {"config": {"mode": "b", "fail": True}, "critical": True}, "c": {}},
            )

        self.assertIs(kernel.plan.nodes["p/main"].manifest.id, "p")
        self.assertEqual(kernel.plan.nodes["p/main"].config, {"mode": "a"})
        states = {key: node.state for key, node in kernel.plan.nodes.items()}
        self.assertEqual(states, {"p/main": "RUNNING", "c/main": "RUNNING"})


if __name__ == "__main__":
    unittest.main()