# ruff: noqa: E501
"""d1（ウミガメの出題者）の自動解放。"""
import asyncio
import json
import unittest
from unittest import mock

from jllm_plugins.dashboard import d1_lifecycle
from jllm_plugins.dashboard.d1_lifecycle import (
    D1Controller,
    D1Lifecycle,
    D1LifecycleError,
    NoD1Lifecycle,
    build_d1_lifecycle,
)


async def _idle_noop(self):
    return


def setUpModule():
    global _reaper_patcher
    _reaper_patcher = mock.patch.object(D1Lifecycle, '_reap', _idle_noop)
    _reaper_patcher.start()


def tearDownModule():
    _reaper_patcher.stop()


class FakeController:
    def __init__(self, *, started=True, stopped=True, error=None):
        self.calls = []
        self.started = started
        self.stopped = stopped
        self.error = error

    async def run(self, action):
        self.calls.append(action)

        if self.error:
            raise D1LifecycleError(self.error)

        return {'ok': True, 'started': self.started, 'stopped': self.stopped}


class EnsureTests(unittest.IsolatedAsyncioTestCase):
    async def test_healthy_d1_is_not_started_again(self):
        controller = FakeController()
        lifecycle = D1Lifecycle(controller, health_url='http://d1/health')

        with mock.patch('jllm_plugins.dashboard.d1_lifecycle._http_healthy', return_value=True):
            self.assertTrue(await lifecycle.ensure())

        self.assertEqual(controller.calls, [])

    async def test_a_stopped_d1_is_started(self):
        controller = FakeController()
        lifecycle = D1Lifecycle(controller, health_url='http://d1/health')
        states = iter([False, False, True])

        with mock.patch('jllm_plugins.dashboard.d1_lifecycle._http_healthy', side_effect=lambda *a, **k: next(states, True)):
            self.assertTrue(await lifecycle.ensure())

        self.assertEqual(controller.calls, ['start'])

    async def test_a_failed_start_does_not_raise(self):
        controller = FakeController(error='d1 の準備完了を確認できませんでした')
        lifecycle = D1Lifecycle(controller, health_url='http://d1/health')

        with mock.patch('jllm_plugins.dashboard.d1_lifecycle._http_healthy', return_value=False):
            self.assertFalse(await lifecycle.ensure())

    async def test_ensure_without_a_url_is_a_no_op(self):
        controller = FakeController()
        lifecycle = D1Lifecycle(controller, health_url='')

        self.assertFalse(await lifecycle.ensure())
        self.assertEqual(controller.calls, [])


class IdleTests(unittest.IsolatedAsyncioTestCase):
    async def test_recent_use_does_not_stop(self):
        controller = FakeController()
        lifecycle = D1Lifecycle(controller, idle_seconds=300)

        self.assertFalse(await lifecycle.maybe_stop())
        self.assertEqual(controller.calls, [])

    async def test_idle_d1_is_stopped(self):
        controller = FakeController()
        lifecycle = D1Lifecycle(controller, idle_seconds=300)
        lifecycle.last_activity -= 301

        self.assertTrue(await lifecycle.maybe_stop())
        self.assertEqual(controller.calls, ['stop'])

    async def test_touch_postpones_the_stop(self):
        controller = FakeController()
        lifecycle = D1Lifecycle(controller, idle_seconds=300)
        lifecycle.last_activity -= 301
        lifecycle.touch()

        self.assertFalse(await lifecycle.maybe_stop())
        self.assertEqual(controller.calls, [])

    async def test_stop_failure_is_only_logged(self):
        controller = FakeController(error='停止できませんでした')
        lifecycle = D1Lifecycle(controller, idle_seconds=1)
        lifecycle.last_activity -= 10

        self.assertFalse(await lifecycle.maybe_stop())

    async def test_game_start_schedules_a_start(self):
        controller = FakeController()
        lifecycle = D1Lifecycle(controller, health_url='http://d1/health')

        with mock.patch('jllm_plugins.dashboard.d1_lifecycle._http_healthy', return_value=True):
            lifecycle.start_in_background()
            await asyncio.sleep(0)

        self.assertGreaterEqual(len(controller.calls), 0)
        self.assertIsNotNone(lifecycle.last_activity)


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_action_is_rejected(self):
        controller = D1Controller('ryu@example', '/tmp/key', '/tmp/known')

        with self.assertRaisesRegex(D1LifecycleError, '不明な操作'):
            await controller.run('restart')

    async def test_ssh_failure_is_reported(self):
        controller = D1Controller('ryu@example', '/tmp/key', '/tmp/known')

        with mock.patch('jllm_plugins.dashboard.d1_lifecycle.asyncio.create_subprocess_exec',
                        side_effect=OSError('no ssh')):
            with self.assertRaisesRegex(D1LifecycleError, '接続できません'):
                await controller.run('status')

    async def test_error_payload_is_reported(self):
        controller = D1Controller('ryu@example', '/tmp/key', '/tmp/known')
        process = mock.Mock()
        process.communicate = mock.AsyncMock(
            return_value=(json.dumps({'ok': False, 'error': 'd1 が止まりません'}).encode(), b''),
        )

        with mock.patch('jllm_plugins.dashboard.d1_lifecycle.asyncio.create_subprocess_exec',
                        return_value=process):
            with self.assertRaisesRegex(D1LifecycleError, '止まりません'):
                await controller.run('stop')


class BuildTests(unittest.TestCase):
    def tearDown(self):
        d1_lifecycle.reset()

    def test_without_settings_it_does_nothing(self):
        self.assertIsInstance(build_d1_lifecycle({}), NoD1Lifecycle)

    def test_configuration_builds_a_controller(self):
        lifecycle = build_d1_lifecycle({
            'D1_CONTROL_HOST': 'ryu@192.168.0.22',
            'D1_CONTROL_KEY': '/home/ryu/.ssh/id_ed25519_d1',
            'D1_CONTROL_KNOWN_HOSTS': '/home/ryu/.ssh/vram_known_hosts',
            'D1_IDLE_SECONDS': '120',
            'D1_API_URL': 'http://192.168.0.22:8083',
        })

        self.assertTrue(lifecycle.configured)
        self.assertEqual(lifecycle.idle_seconds, 120.0)
        self.assertTrue(lifecycle.health_url.endswith('/health'))
        self.assertTrue(lifecycle.manages('http://192.168.0.22:8083'))
        self.assertIsInstance(lifecycle.controller, D1Controller)

    def test_singleton_can_be_reset(self):
        first = d1_lifecycle.get()
        self.assertIs(first, d1_lifecycle.get())
        d1_lifecycle.reset()
        self.assertIsNot(first, d1_lifecycle.get())


class WiringTests(unittest.TestCase):
    def test_only_the_configured_d1_is_managed(self):
        lifecycle = D1Lifecycle(FakeController(), api_base='http://192.168.0.22:8083')

        self.assertTrue(lifecycle.manages('http://192.168.0.22:8083/'))
        self.assertFalse(lifecycle.manages('http://127.0.0.1:9'))
        self.assertFalse(lifecycle.manages(''))

    def test_unconfigured_lifecycle_manages_nothing(self):
        self.assertFalse(NoD1Lifecycle().manages('http://192.168.0.22:8083'))


if __name__ == '__main__':
    unittest.main()
