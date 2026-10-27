import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jllm_plugins.fake_consumer.plugin import Plugin as Consumer
from jllm_plugins.fake_provider.plugin import Plugin as Provider
from jllm_plugins.fake_router.plugin import Plugin as Router


class FakePluginTests(unittest.IsolatedAsyncioTestCase):
    async def test_ordered_binding_can_swap_provider(self):
        providers = []
        for prefix in ("first: ", "second: "):
            provider = Provider()
            services = await provider.setup(SimpleNamespace(config={"prefix": prefix}))
            self.assertIs(services["fake.provider"], provider)
            await provider.start()
            providers.append(provider)
        for order, expected in ((providers, "first: hello"), (providers[::-1], "second: hello")):
            router = Router()
            services = await router.setup(SimpleNamespace(dependencies={"fake.provider": order}))
            await router.start()
            consumer = Consumer()
            await consumer.setup(
                SimpleNamespace(
                    dependencies={"fake.routed": services["fake.routed"]},
                    config={"message": "hello"},
                )
            )
            await consumer.start()
            self.assertEqual(consumer.result, expected)
            await consumer.stop()
            await router.stop()
        for provider in reversed(providers):
            await provider.stop()
