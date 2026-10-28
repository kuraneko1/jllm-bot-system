# ruff: noqa: E501
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from jllm_plugins.imagegen.control import ImageGenController
from jllm_plugins.imagegen.service import ImageGenError
from jllm_plugins.reader.core import ReaderCog


class ImageGenToggleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = ReaderCog.__new__(ReaderCog)
        self.cog.toggles = {'img': True, 'wav': True}
        self.cog._save_toggles = Mock()
        self.cog.imagegen_controller = SimpleNamespace(lock=asyncio.Lock(), set_enabled=AsyncMock())
        self.interaction = SimpleNamespace(
            user=SimpleNamespace(id=42),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        self.cog.config = SimpleNamespace(owner_id=42)
        owner = patch.object(self.cog.config, 'owner_id', 42)
        owner.start()
        self.addCleanup(owner.stop)

    async def test_off_blocks_generation_before_stopping(self):
        async def stop(enabled):
            self.assertFalse(enabled)
            self.assertFalse(self.cog.toggles['img'])
            self.interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        self.cog.imagegen_controller.set_enabled.side_effect = stop
        await self.cog._toggle_feature(self.interaction, 'img', False)
        self.assertFalse(self.cog.toggles['img'])
        self.assertTrue(self.cog.toggles['wav'])

    async def test_enable_waits_for_server(self):
        async def start(enabled):
            self.assertTrue(enabled)
            self.assertFalse(self.cog.toggles['img'])
        self.cog.imagegen_controller.set_enabled.side_effect = start
        await self.cog._toggle_feature(self.interaction, 'img', True)
        self.assertTrue(self.cog.toggles['img'])

    async def test_failed_control_leaves_generation_off(self):
        self.cog.imagegen_controller.set_enabled.side_effect = ImageGenError('failed')
        await self.cog._toggle_feature(self.interaction, 'img', True)
        self.assertFalse(self.cog.toggles['img'])
        self.assertIn('完了できません', self.interaction.followup.send.call_args.args[0])

    async def test_non_owner_cannot_control_server(self):
        self.interaction.user.id = 99
        await self.cog._toggle_feature(self.interaction, 'img', False)
        self.cog.imagegen_controller.set_enabled.assert_not_awaited()
        self.assertTrue(self.cog.toggles['img'])

    async def test_status_does_not_change_server(self):
        await self.cog._toggle_feature(self.interaction, 'img', None)
        self.cog.imagegen_controller.set_enabled.assert_not_awaited()


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_response_must_confirm_requested_state(self):
        controller = ImageGenController('host', 'key', 'known-hosts')
        controller.run = AsyncMock(return_value={'services': {'comfy': True}})
        with self.assertRaises(ImageGenError):
            await controller.set_enabled(False)
        controller.run.assert_awaited_once_with('stop')

    async def test_unconfigured_control_fails(self):
        controller = ImageGenController('', '', '')
        with self.assertRaises(ImageGenError):
            await controller.set_enabled(False)
