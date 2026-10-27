import asyncio
import traceback
import unittest

from jllm.api import ResourceScope


class ResourceScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_is_lifo_awaited_and_idempotent(self):
        scope = ResourceScope()
        calls = []
        scope.add(lambda: calls.append("first"))

        async def second():
            await asyncio.sleep(0)
            calls.append("second")

        scope.add(second)
        await scope.close()
        await scope.close()
        self.assertEqual(calls, ["second", "first"])
        with self.assertRaises(RuntimeError):
            scope.add(lambda: None)

    async def test_cleanup_continues_after_disposer_failure(self):
        scope = ResourceScope()
        calls = []
        scope.add(lambda: calls.append("earlier"))

        def fail():
            raise ValueError("secret cleanup error")

        scope.add(fail)
        with self.assertRaises(ExceptionGroup) as caught:
            await scope.close()
        self.assertTrue(all(isinstance(e, RuntimeError) for e in caught.exception.exceptions))
        self.assertNotIn("secret", "".join(traceback.format_exception(caught.exception)))
        self.assertEqual(calls, ["earlier"])

    async def test_cancelled_waiter_waits_for_shared_cleanup_and_keeps_cancellation(self):
        scope = ResourceScope()
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def dispose():
            started.set()
            await release.wait()
            calls.append("done")
            raise ValueError("secret cleanup error")

        scope.add(lambda: calls.append("earlier"))
        scope.add(dispose)
        first = asyncio.create_task(scope.close())
        await started.wait()
        second = asyncio.create_task(scope.close())
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.sleep(0)
        self.assertFalse(first.done())
        self.assertFalse(second.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await first
        with self.assertRaises(ExceptionGroup) as caught:
            await second
        self.assertNotIn("secret", "".join(traceback.format_exception(caught.exception)))
        with self.assertRaises(ExceptionGroup):
            await scope.close()
        self.assertEqual(calls, ["done", "earlier"])

    async def test_cancelled_waiter_can_close_again_after_success(self):
        scope = ResourceScope()
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def dispose():
            started.set()
            await release.wait()
            calls.append("done")

        scope.add(dispose)
        closer = asyncio.create_task(scope.close())
        await started.wait()
        closer.cancel()
        await asyncio.sleep(0)
        self.assertFalse(closer.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await closer
        await scope.close()
        self.assertEqual(calls, ["done"])

    async def test_disposer_cancellation_does_not_stop_cleanup(self):
        scope = ResourceScope()
        calls = []
        scope.add(lambda: calls.append("earlier"))

        async def cancelled_disposer():
            raise asyncio.CancelledError("secret cancellation")

        scope.add(cancelled_disposer)
        with self.assertRaises(ExceptionGroup) as caught:
            await scope.close()
        self.assertNotIn("secret", "".join(traceback.format_exception(caught.exception)))
        self.assertEqual(calls, ["earlier"])

    async def test_finished_tasks_are_removed_and_failures_logged_safely(self):
        scope = ResourceScope("plugin/instance")

        async def fail():
            raise ValueError("secret task error")

        async def succeed():
            return 42

        with self.assertLogs("jllm.plugin", level="ERROR") as logs:
            failed = scope.create_task(fail())
            succeeded = scope.create_task(succeed())
            await asyncio.wait([failed, succeeded])
            await asyncio.sleep(0)

        self.assertEqual(scope._tasks, set())
        self.assertEqual(
            logs.output, ["ERROR:jllm.plugin:background task failed for 'plugin/instance'"]
        )
        self.assertFalse(failed._log_traceback)
        await scope.close()

    async def test_close_cancels_and_awaits_tasks_before_disposers(self):
        scope = ResourceScope()
        started = asyncio.Event()
        calls = []

        async def worker():
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                calls.append("task finished")

        task = scope.create_task(worker())
        scope.add(lambda: calls.append("disposed"))
        await started.wait()
        await scope.close()
        self.assertTrue(task.cancelled())
        self.assertEqual(calls, ["task finished", "disposed"])
