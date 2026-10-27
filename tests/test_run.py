import asyncio
import os
import signal
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from jllm.cli import _activate, _run


class RunTests(unittest.IsolatedAsyncioTestCase):
    async def test_activate_calls_only_running_plugins(self):
        active = SimpleNamespace(state="RUNNING", plugin=SimpleNamespace(activate=AsyncMock()))
        blocked = SimpleNamespace(state="BLOCKED", plugin=SimpleNamespace(activate=AsyncMock()))
        await _activate(SimpleNamespace(nodes={"active": active, "blocked": blocked}))
        active.plugin.activate.assert_awaited_once()
        blocked.plugin.activate.assert_not_called()

    async def test_run_starts_and_stops_without_long_running_plugin(self):
        plan = SimpleNamespace(nodes={})
        kernel = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
        with patch("jllm.cli.Kernel", return_value=kernel):
            self.assertEqual(await _run(plan), 0)
        kernel.start.assert_awaited_once()
        kernel.stop.assert_awaited_once()

    async def test_activation_failure_is_safe_and_still_stops(self):
        plugin = SimpleNamespace(activate=AsyncMock(side_effect=ValueError("secret-token")))
        plan = SimpleNamespace(nodes={"x": SimpleNamespace(state="RUNNING", plugin=plugin)})
        kernel = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
        with (
            patch("jllm.cli.Kernel", return_value=kernel),
            patch("jllm.cli.print") as output,
        ):
            self.assertEqual(await _run(plan), 1)
        kernel.stop.assert_awaited_once()
        self.assertNotIn("secret-token", str(output.mock_calls))

    async def test_sigint_cancels_activation_and_stops_cleanly(self):
        async def interrupt():
            os.kill(os.getpid(), signal.SIGINT)
            await asyncio.sleep(60)

        plan = SimpleNamespace(
            nodes={
                "x": SimpleNamespace(state="RUNNING", plugin=SimpleNamespace(activate=interrupt))
            }
        )
        kernel = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
        with patch("jllm.cli.Kernel", return_value=kernel):
            self.assertEqual(await _run(plan), 0)
        kernel.stop.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
