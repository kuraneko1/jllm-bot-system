# ruff: noqa: E501
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from jllm_plugins.dictionaries.dictionary import SoundDictionary
from jllm_plugins.reader.core import ReaderCog


class SoundApprovalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.cog = ReaderCog.__new__(ReaderCog)
        self.cog.dictionary = SoundDictionary(root / 'sounds.json', root / 'sounds')
        self.cog.pending_sound_dir = root / 'pending'
        self.cog.pending_sound_dir.mkdir()
        self.cog.pending_sound = {}
        self.cog._rebuild_sound_dashboard = Mock()
        self.cog.config.owner_id = 42
        owner = patch.object(self.cog.config, 'owner_id', 42)
        owner.start()
        self.addCleanup(owner.stop)
        def convert(data, suffix, target):
            target.write_bytes(b'converted WAV')
            return True
        converter = patch('jllm_plugins.reader.core.transcode_to_wav', side_effect=convert)
        converter.start()
        self.addCleanup(converter.stop)

    def interaction(self, user_id):
        return SimpleNamespace(
            user=SimpleNamespace(id=user_id, display_name=f'User {user_id}'),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), edit_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            channel=SimpleNamespace(send=AsyncMock()),
        )

    async def submit(self, user_id):
        interaction = self.interaction(user_id)
        attachment = SimpleNamespace(filename='upload.wav', read=AsyncMock(return_value=b'WAV'))
        await ReaderCog.dict_add.callback(self.cog, interaction, '語句', attachment)
        return interaction

    async def test_owner_registers_directly(self):
        interaction = await self.submit(42)
        self.assertTrue(self.cog.dictionary.items())
        self.assertFalse(self.cog.pending_sound)
        interaction.channel.send.assert_not_awaited()
        self.cog._rebuild_sound_dashboard.assert_called_once()

    async def test_other_user_needs_owner_approval_and_preserves_author(self):
        interaction = await self.submit(99)
        self.assertFalse(self.cog.dictionary.items())
        self.cog._rebuild_sound_dashboard.assert_not_called()
        kwargs = interaction.channel.send.call_args.kwargs
        self.assertIn('<@42>', kwargs['content'])
        self.assertEqual(kwargs['file'].filename, 'preview.wav')
        view = kwargs['view']
        self.addCleanup(view.stop)
        unauthorized = self.interaction(99)
        await view.approve.callback(unauthorized)
        self.assertFalse(self.cog.dictionary.items())
        self.assertFalse(view.finished)
        unauthorized.response.send_message.assert_awaited_once()
        await view.approve.callback(self.interaction(42))
        self.assertTrue(self.cog.dictionary.items())
        self.assertEqual(self.cog.dictionary.author_of('語句'), 'User 99')
        self.assertFalse(self.cog.pending_sound)
        self.assertTrue(view.finished)

    async def test_rejection_and_expiry_discard_without_registration(self):
        for expire in (False, True):
            interaction = await self.submit(99)
            view = interaction.channel.send.call_args.kwargs['view']
            self.addCleanup(view.stop)
            request = next(iter(self.cog.pending_sound.values()))
            path = Path(request['path'])
            if expire:
                await view.on_timeout()
            else:
                await view.reject.callback(self.interaction(42))
            self.assertFalse(self.cog.pending_sound)
            self.assertFalse(await asyncio.to_thread(path.exists))
            self.assertFalse(self.cog.dictionary.items())

    async def test_failed_request_delivery_cleans_up(self):
        interaction = self.interaction(99)
        interaction.channel.send.side_effect = OSError('cannot send')
        with self.assertLogs('jllm_plugins.reader.core', level='ERROR'):
            await ReaderCog.dict_add.callback(self.cog, interaction, '語句',
                SimpleNamespace(filename='a.wav', read=AsyncMock(return_value=b'WAV')))
        self.assertFalse(self.cog.pending_sound)
        self.assertFalse(list(self.cog.pending_sound_dir.iterdir()))
        self.assertFalse(self.cog.dictionary.items())

    async def test_missing_owner_does_not_allow_registration(self):
        with patch.object(self.cog.config, 'owner_id', 0):
            await self.submit(99)
        self.assertFalse(self.cog.dictionary.items())
        self.assertFalse(self.cog.pending_sound)

    async def test_help_displays_configured_admin_url(self):
        interaction = self.interaction(99)
        with patch.object(self.cog.config, 'dashboard_url', 'https://admin.example:8443'):
            await ReaderCog.help.callback(self.cog, interaction)
        self.assertIn('https://admin.example:8443/', interaction.followup.send.call_args.args[0])
        message = interaction.followup.send.call_args.args[0]
        self.assertTrue(message.startswith('## '))
        self.assertIn('### 読み上げ', message)
        self.assertIn('- `/join`', message)
        self.assertLess(len(message), 1900)

    async def test_rebuild_removes_retired_public_sound_page(self):
        self.cog.html_dir = self.cog.pending_sound_dir.parent / 'html'
        sounds_dir = self.cog.html_dir / 'sounds'
        sounds_dir.mkdir(parents=True)
        (sounds_dir / 'index.html').write_text('old board', encoding='utf-8')
        audio_dir = self.cog.html_dir / 'audio'
        audio_dir.mkdir()
        (audio_dir / 'sound-0001.wav').write_bytes(b'old audio')
        ReaderCog._rebuild_sound_dashboard(self.cog)
        self.assertFalse((sounds_dir / 'index.html').exists())
        self.assertFalse(sounds_dir.exists())
        self.assertFalse(audio_dir.exists())
