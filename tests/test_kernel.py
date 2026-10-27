import asyncio
import traceback
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from jllm.kernel import Kernel, Manifest, Profile, ProfileError, resolve


def manifest(name, provides=(), requires=(), optional=(), schema=None):
    def caps(items):
        return [
            dict(capability=c[0], version=c[1], **({"cardinality": c[2]} if len(c) > 2 else {}))
            for c in items
        ]

    return Manifest.from_dict(
        {
            "id": name,
            "version": "1.0.0",
            "api": ">=1.0,<2.0",
            "entry": name + ":Plugin",
            "provides": caps(provides),
            "requires": caps(requires),
            "optional": caps(optional),
            "config_schema": schema or {},
        }
    )


def plan(manifests, options=None, bindings=None):
    return resolve(
        Profile(options if options is not None else {m.id: {} for m in manifests}, bindings or {}),
        {m.id: m for m in manifests},
    )


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.a = manifest("a", [("echo", 1)])
        self.b = manifest("b", [("echo", 1)])
        self.consumer = manifest("consumer", requires=[("echo", 1)])

    def test_provider_zero_is_blocked_not_invalid(self):
        p = plan([self.consumer])
        self.assertEqual(p.nodes["consumer/main"].state, "BLOCKED")
        self.assertIn("echo", p.nodes["consumer/main"].reason)

    def test_one_ambiguous_unknown_and_version_mismatch_rejected(self):
        with self.assertRaises(ProfileError):
            plan([self.a, self.b, self.consumer])
        for target in ("unknown/main", "wrong/main"):
            wrong = manifest("wrong", [("echo", 2)])
            with self.subTest(target=target), self.assertRaises(ProfileError):
                plan([self.a, wrong, self.consumer], bindings={"consumer": {"echo": target}})

    def test_unbound_incompatible_version_is_rejected(self):
        wrong = manifest("wrong", [("echo", 2)])
        for consumer in (
            self.consumer,
            manifest("many", requires=[("echo", 1, "many")]),
            manifest("optional", optional=[("echo", 1)]),
        ):
            with self.subTest(consumer=consumer.id), self.assertRaises(ProfileError):
                plan([wrong, consumer])
        # Compatible providers are not invalidated by another major version.
        self.assertEqual(
            plan([wrong, self.a, self.consumer]).nodes["consumer/main"].state, "PENDING"
        )

    def test_explicit_one_binding_orders_provider_before_consumer(self):
        p = plan([self.consumer, self.a, self.b], bindings={"consumer": {"echo": "b/main"}})
        self.assertEqual(p.nodes["consumer/main"].bindings["echo"], ["b/main"])
        self.assertLess(p.order.index("b/main"), p.order.index("consumer/main"))

    def test_many_only_uses_explicit_binding_in_order(self):
        router = manifest("router", requires=[("echo", 1, "many")])
        p = plan([router, self.a, self.b], bindings={"router": {"echo": ["b/main", "a/main"]}})
        self.assertEqual(p.nodes["router/main"].bindings["echo"], ["b/main", "a/main"])
        with self.assertRaises(ProfileError):
            plan([router, self.a], bindings={"router": {"echo": ["a/main", "a/main"]}})

    def test_optional_missing_and_ambiguous(self):
        consumer = manifest("consumer", optional=[("echo", 1)])
        self.assertEqual(plan([consumer]).nodes["consumer/main"].state, "PENDING")
        with self.assertRaises(ProfileError):
            plan([consumer, self.a, self.b])

    def test_cycle_is_rejected(self):
        a = manifest("a", [("a", 1)], [("b", 1)])
        b = manifest("b", [("b", 1)], [("a", 1)])
        with self.assertRaises(ProfileError):
            plan([a, b])

    def test_critical_transitive_blocked_aborts_before_start(self):
        middle = manifest("middle", [("mid", 1)], [("missing", 1)])
        critical = manifest("critical", requires=[("mid", 1)])
        with self.assertRaises(ProfileError):
            plan([middle, critical], {"middle": {}, "critical": {"critical": True}})

    def test_config_validation_identifies_field_without_secret(self):
        m = manifest("a", schema={"type": "object", "properties": {"token": {"type": "integer"}}})
        with self.assertRaises(ProfileError) as caught:
            plan([m], {"a": {"config": {"token": "private-secret"}}})
        self.assertIn("token", str(caught.exception))
        self.assertNotIn("private-secret", str(caught.exception))


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_stop_waits_for_cleanup_and_concurrent_stop(self):
        calls = []
        entered = asyncio.Event()
        release = asyncio.Event()

        class Plugin:
            def __init__(self, name):
                self.name = name

            async def setup(self, ctx):
                async def cleanup():
                    await asyncio.sleep(0)
                    calls.append("cleanup " + self.name)

                ctx.scope.add(cleanup)
                return {}

            async def start(self):
                pass

            async def stop(self):
                calls.append("stop " + self.name)
                if self.name == "second":
                    entered.set()
                    await release.wait()
                calls.append("stopped " + self.name)

        p = plan([manifest("first"), manifest("second")])
        with patch(
            "jllm.kernel.importlib.import_module",
            side_effect=lambda name: SimpleNamespace(Plugin=lambda: Plugin(name)),
        ):
            k = Kernel(p)
            await k.start()
            first = asyncio.create_task(k.stop())
            await asyncio.wait_for(entered.wait(), 1)
            second = asyncio.create_task(k.stop())
            await asyncio.sleep(0)
            first.cancel()
            await asyncio.sleep(0)
            try:
                self.assertFalse(first.done())
                self.assertFalse(second.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(first, 1)
            await asyncio.wait_for(second, 1)
            await asyncio.wait_for(k.stop(), 1)

        self.assertEqual(
            calls,
            [
                "stop second",
                "stopped second",
                "cleanup second",
                "stop first",
                "stopped first",
                "cleanup first",
            ],
        )
        self.assertEqual([p.nodes[key].state for key in p.order], ["STOPPED", "STOPPED"])

    async def test_plugin_stop_cancellation_cleans_all_scopes_without_disclosing_secret(self):
        secret = "SECRET-plugin-cancellation"
        calls = []

        class Plugin:
            def __init__(self, name):
                self.name = name

            async def setup(self, ctx):
                ctx.scope.add(lambda: calls.append("cleanup " + self.name))
                return {}

            async def start(self):
                pass

            async def stop(self):
                calls.append("stop " + self.name)
                if self.name == "second":
                    raise asyncio.CancelledError(secret)

        p = plan([manifest("first"), manifest("second")])
        with (
            patch("jllm.kernel.logging.getLogger") as logger,
            patch(
                "jllm.kernel.importlib.import_module",
                side_effect=lambda name: SimpleNamespace(Plugin=lambda: Plugin(name)),
            ),
        ):
            k = Kernel(p)
            await k.start()
            with self.assertRaises(ExceptionGroup) as caught:
                await asyncio.wait_for(k.stop(), 1)
            with self.assertRaises(ExceptionGroup) as repeated:
                await asyncio.wait_for(k.stop(), 1)

        self.assertEqual(str(caught.exception), str(repeated.exception))
        self.assertTrue(all(isinstance(e, RuntimeError) for e in caught.exception.exceptions))
        self.assertEqual(calls, ["stop second", "cleanup second", "stop first", "cleanup first"])
        self.assertEqual([p.nodes[key].state for key in p.order], ["STOPPED", "STOPPED"])
        text = (
            "".join(traceback.format_exception(caught.exception))
            + "".join(traceback.format_exception(repeated.exception))
            + str(logger.mock_calls)
        )
        self.assertNotIn(secret, text)

    async def test_failure_rolls_back_scope_blocks_dependents_and_keeps_independent(self):
        calls = []

        class Plugin:
            def __init__(self, name):
                self.name = name

            async def setup(self, ctx):
                calls.append("setup " + self.name)
                ctx.scope.add(lambda: calls.append("cleanup " + self.name))
                if self.name == "bad":
                    raise ValueError("failed")
                return {}

            async def start(self):
                calls.append("start " + self.name)

            async def stop(self):
                calls.append("stop " + self.name)

        # A failed setup must clean its scope even if no service was registered.
        bad = manifest("bad", [("x", 1)])
        dependent = manifest("dependent", requires=[("x", 1)])
        independent = manifest("independent")
        p = plan([bad, dependent, independent])
        with patch(
            "jllm.kernel.importlib.import_module",
            side_effect=lambda name: SimpleNamespace(Plugin=lambda: Plugin(name)),
        ):
            k = Kernel(p)
            await k.start()
            self.assertEqual(p.nodes["bad/main"].state, "FAILED")
            self.assertEqual(p.nodes["dependent/main"].state, "BLOCKED")
            self.assertEqual(p.nodes["independent/main"].state, "RUNNING")
            self.assertIn("cleanup bad", calls)
            self.assertNotIn("setup dependent", calls)
            await k.stop()

    async def test_noncritical_start_failure_blocks_consumers_but_not_independent(self):
        calls = []

        class Plugin:
            def __init__(self, name):
                self.name = name

            async def setup(self, ctx):
                ctx.scope.add(lambda: calls.append("cleanup " + self.name))
                return {"x": object()} if self.name == "bad" else {}

            async def start(self):
                if self.name == "bad":
                    raise RuntimeError("failed")

            async def stop(self):
                pass

        p = plan(
            [
                manifest("bad", [("x", 1)]),
                manifest("consumer", requires=[("x", 1)]),
                manifest("independent"),
            ]
        )
        with patch(
            "jllm.kernel.importlib.import_module",
            side_effect=lambda name: SimpleNamespace(Plugin=lambda: Plugin(name)),
        ):
            k = Kernel(p)
            await k.start()
            self.assertEqual(p.nodes["bad/main"].state, "FAILED")
            self.assertEqual(p.nodes["consumer/main"].state, "BLOCKED")
            self.assertEqual(p.nodes["independent/main"].state, "RUNNING")
            self.assertIn("cleanup bad", calls)
            await k.stop()

    async def test_cleanup_and_stop_do_not_disclose_secrets(self):
        import traceback

        secret = "SECRET-rollback-stop-disposer"

        class Plugin:
            def __init__(self, name):
                self.name = name

            async def setup(self, ctx):
                def dispose():
                    raise ValueError(secret)

                ctx.scope.add(dispose)
                return {}

            async def start(self):
                if self.name == "bad":
                    raise ValueError(secret)

            async def stop(self):
                raise ValueError(secret)

        p = plan([manifest("ok"), manifest("bad")], {"ok": {}, "bad": {"critical": True}})
        with patch(
            "jllm.kernel.importlib.import_module",
            side_effect=lambda name: SimpleNamespace(Plugin=lambda: Plugin(name)),
        ):
            with self.assertLogs("jllm.kernel", level="ERROR") as logs:
                with self.assertRaises(ExceptionGroup) as caught:
                    await Kernel(p).start()
        text = "".join(traceback.format_exception(caught.exception)) + "".join(logs.output)
        self.assertNotIn(secret, text)
        self.assertIn("bad/main", text)
        self.assertIn("ok/main", text)

    async def test_critical_start_failure_unwinds_started_plugins_in_reverse(self):
        calls = []

        class Plugin:
            def __init__(self, name):
                self.name = name

            async def setup(self, ctx):
                ctx.scope.add(lambda: calls.append("cleanup " + self.name))
                return {}

            async def start(self):
                if self.name == "bad":
                    raise RuntimeError("bad")
                calls.append("start " + self.name)

            async def stop(self):
                calls.append("stop " + self.name)

        p = plan(
            [manifest("first"), manifest("second"), manifest("bad")],
            {"first": {}, "second": {}, "bad": {"critical": True}},
        )
        with patch(
            "jllm.kernel.importlib.import_module",
            side_effect=lambda name: SimpleNamespace(Plugin=lambda: Plugin(name)),
        ):
            with self.assertRaises(RuntimeError):
                await Kernel(p).start()
        self.assertEqual(
            calls,
            [
                "start first",
                "start second",
                "cleanup bad",
                "stop second",
                "cleanup second",
                "stop first",
                "cleanup first",
            ],
        )
