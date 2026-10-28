# ruff: noqa: E501
import io
import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
from jllm_plugins.dashboard.dashboard import ReadingDashboard
from jllm_plugins.dashboard.voice_cloning import normalize_voice
from jllm_plugins.dictionaries.readings import ReadingDictionary
from jllm_tts import Voice

from tests.passkey_auth import ensure_admin_session


class FakeVoiceTTS:
    supports_voice_management = True

    def __init__(self):
        self.voices = [{'name': 'none', 'deletable': False}, {'name': 'Clone Ryu', 'deletable': True}]
        self.added = []
        self.disabled_voices = set()

    async def voice_library(self):
        return [dict(item, enabled=item['name'] not in self.disabled_voices) for item in self.voices]

    async def list_voices(self):
        return [Voice(id=index, name=item['name']) for index, item in enumerate(sorted(self.voices, key=lambda item: item['name']), 1) if item['name'] not in self.disabled_voices]

    async def speakers(self):
        return [{'name': 'Irodori', 'styles': [{'id': v.id, 'name': v.name} for v in await self.list_voices()]}]

    async def set_voice_enabled(self, name, enabled):
        if enabled:
            self.disabled_voices.discard(name)
        else:
            self.disabled_voices.add(name)

    async def add_voice(self, name, audio):
        self.added.append((name, audio))
        self.voices.append({'name': name, 'deletable': True})

    async def delete_voice(self, name):
        self.voices = [voice for voice in self.voices if voice['name'] != name]


class VoiceAdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.tts = FakeVoiceTTS()
        self.dashboard = ReadingDashboard(ReadingDictionary(str(Path(self.directory.name)/'readings.json')),
                                         self.tts,
                                         password_file=str(Path(self.directory.name)/'auth.json'))
        self.client = TestClient(TestServer(self.dashboard.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.directory.cleanup()

    async def login(self):
        await ensure_admin_session(self.client, self.dashboard)

    def upload(self, name, filename='ref.wav'):
        form = FormData()
        form.add_field('name', name)
        form.add_field('file', b'original audio', filename=filename, content_type='audio/wav')
        return form

    async def test_management_requires_login(self):
        for method, path in [('get', '/api/voice-library'), ('post', '/api/voice-library/upload'), ('delete', '/api/voice-library'), ('put', '/api/voice-library')]:
            self.assertEqual((await getattr(self.client, method)(path)).status, 401)

    async def test_upload_normalizes_and_adds_unicode_name(self):
        await self.login()
        with patch('jllm_plugins.dashboard.dashboard_voices.normalize_voice', return_value=(b'normalized', {'lufs': -16, 'peak_dbtp': -1.7, 'seconds': 12})) as normalize:
            response = await self.client.post('/api/voice-library/upload', data=self.upload('clone こしあん'))
        self.assertEqual(response.status, 201)
        normalize.assert_called_once_with(b'original audio', '.wav')
        self.assertEqual(self.tts.added, [('clone こしあん', b'normalized')])

    async def test_duplicate_or_unsafe_names_do_not_normalize(self):
        await self.login()
        for name, status in [('Clone Ryu', 409), ('../bad', 400), ('none', 400)]:
            with patch('jllm_plugins.dashboard.dashboard_voices.normalize_voice') as normalize:
                response = await self.client.post('/api/voice-library/upload', data=self.upload(name))
            self.assertEqual(response.status, status)
            normalize.assert_not_called()

    async def test_invalid_audio_cannot_be_registered(self):
        await self.login()
        with patch('jllm_plugins.dashboard.dashboard_voices.normalize_voice', side_effect=ValueError('invalid recording')):
            response = await self.client.post('/api/voice-library/upload', data=self.upload('new voice'))
        self.assertEqual(response.status, 400)
        self.assertFalse(self.tts.added)

    async def test_delete_clone_and_protect_builtin(self):
        await self.login()
        response = await self.client.delete('/api/voice-library', json={'name': 'none'})
        self.assertEqual(response.status, 400)
        response = await self.client.delete('/api/voice-library', json={'name': 'Clone Ryu'})
        self.assertEqual(response.status, 200)
        self.assertEqual([v['name'] for v in self.tts.voices], ['none'])

    async def test_cross_origin_upload_rejected(self):
        await self.login()
        response = await self.client.post('/api/voice-library/upload', data=self.upload('new voice'), headers={'Origin': 'https://evil.example'})
        self.assertEqual(response.status, 403)

    async def test_new_voice_does_not_change_active_selected_voice(self):
        await self.login()
        player = SimpleNamespace(speaker_id=1)
        self.dashboard.reader = SimpleNamespace(players={1: player})
        with patch('jllm_plugins.dashboard.dashboard_voices.normalize_voice', return_value=(b'normalized', {})):
            response = await self.client.post('/api/voice-library/upload', data=self.upload('A new voice'))
        self.assertEqual(response.status, 201)
        self.assertEqual(player.speaker_id, 2)  # Clone Ryu moved from ID 1 to ID 2.

    async def test_disabled_voice_retained_but_not_selectable_or_previewable(self):
        await self.login()
        response = await self.client.put('/api/voice-library', json={'name': 'Clone Ryu', 'enabled': False})
        self.assertEqual(response.status, 200)
        library = await (await self.client.get('/api/voice-library')).json()
        self.assertFalse(next(v for v in library['voices'] if v['name'] == 'Clone Ryu')['enabled'])
        selectable = await (await self.client.get('/api/voices')).json()
        self.assertEqual([v['name'] for v in selectable['speakers'][0]['styles']], ['none'])
        preview = await self.client.post('/api/generate', json={'voice_name': 'Clone Ryu', 'text': 'test'})
        self.assertEqual(preview.status, 400)
        response = await self.client.put('/api/voice-library', json={'name': 'Clone Ryu', 'enabled': True})
        self.assertEqual(response.status, 200)
        self.assertNotIn('Clone Ryu', self.tts.disabled_voices)

    async def test_toggle_rejects_builtin_unknown_and_nonboolean(self):
        await self.login()
        for name, enabled, status in [('none', False, 400), ('missing', False, 404), ('Clone Ryu', 'false', 400)]:
            response = await self.client.put('/api/voice-library', json={'name': name, 'enabled': enabled})
            self.assertEqual(response.status, status)

    def attach_volumes(self):
        from jllm_plugins.voice_settings.speeds import VoiceSpeeds
        from jllm_plugins.voice_settings.volumes import VoiceVolumes
        self.dashboard.reader = SimpleNamespace(
            players={},
            user_voices={},
            voice_volumes=VoiceVolumes(str(Path(self.directory.name) / 'voice_volumes.json')),
            voice_speeds=VoiceSpeeds(str(Path(self.directory.name) / 'voice_speeds.json')),
        )

    async def test_voice_volume_round_trip_and_caps(self):
        await self.login()
        self.attach_volumes()
        library = await (await self.client.get('/api/voice-library')).json()
        self.assertEqual(next(v for v in library['voices'] if v['name'] == 'Clone Ryu')['volume'], 1.0)
        response = await self.client.put('/api/voice-library/volume', json={'name': 'Clone Ryu', 'volume': 3.0})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['volume'], 3.0)
        library = await (await self.client.get('/api/voice-library')).json()
        self.assertEqual(next(v for v in library['voices'] if v['name'] == 'Clone Ryu')['volume'], 3.0)
        for volume, status in [(3.1, 400), (-0.1, 400), ('loud', 400), (None, 400)]:
            response = await self.client.put('/api/voice-library/volume', json={'name': 'Clone Ryu', 'volume': volume})
            self.assertEqual(response.status, status, msg=repr(volume))
        response = await self.client.put('/api/voice-library/volume', json={'name': 'missing', 'volume': 1.5})
        self.assertEqual(response.status, 404)

    async def test_voice_volume_requires_login_and_reader(self):
        response = await self.client.put('/api/voice-library/volume', json={'name': 'Clone Ryu', 'volume': 1.5})
        self.assertEqual(response.status, 401)
        await self.login()
        response = await self.client.put('/api/voice-library/volume', json={'name': 'Clone Ryu', 'volume': 1.5})
        self.assertEqual(response.status, 503)

    async def test_delete_voice_prunes_volume(self):
        await self.login()
        self.attach_volumes()
        self.dashboard.reader.voice_volumes.set('Clone Ryu', 2.0)
        response = await self.client.delete('/api/voice-library', json={'name': 'Clone Ryu'})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.dashboard.reader.voice_volumes.get('Clone Ryu'), 1.0)

    async def test_voice_speed_round_trip_and_caps(self):
        await self.login()
        self.attach_volumes()
        library = await (await self.client.get('/api/voice-library')).json()
        self.assertNotIn('speed', next(v for v in library['voices'] if v['name'] == 'Clone Ryu'))
        response = await self.client.put('/api/voice-library/speed', json={'name': 'Clone Ryu', 'speed': 1.5})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['speed'], 1.5)
        library = await (await self.client.get('/api/voice-library')).json()
        self.assertEqual(next(v for v in library['voices'] if v['name'] == 'Clone Ryu')['speed'], 1.5)
        for speed, status in [(0.4, 400), (2.1, 400), ('fast', 400), (None, 400)]:
            response = await self.client.put('/api/voice-library/speed', json={'name': 'Clone Ryu', 'speed': speed})
            self.assertEqual(response.status, status, msg=repr(speed))
        response = await self.client.put('/api/voice-library/speed', json={'name': 'missing', 'speed': 1.5})
        self.assertEqual(response.status, 404)

    async def test_voice_speed_requires_login_and_reader(self):
        response = await self.client.put('/api/voice-library/speed', json={'name': 'Clone Ryu', 'speed': 1.5})
        self.assertEqual(response.status, 401)
        await self.login()
        response = await self.client.put('/api/voice-library/speed', json={'name': 'Clone Ryu', 'speed': 1.5})
        self.assertEqual(response.status, 503)

    async def test_delete_voice_prunes_speed(self):
        await self.login()
        self.attach_volumes()
        self.dashboard.reader.voice_speeds.set('Clone Ryu', 1.5)
        response = await self.client.delete('/api/voice-library', json={'name': 'Clone Ryu'})
        self.assertEqual(response.status, 200)
        self.assertIsNone(self.dashboard.reader.voice_speeds.get('Clone Ryu'))


class VoiceNormalizationTests(unittest.TestCase):
    def test_normalized_wav_format_and_loudness(self):
        data = io.BytesIO()
        with wave.open(data, 'wb') as audio:
            audio.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b''.join(struct.pack('<h', int(1000*math.sin(2*math.pi*220*i/48000))) for i in range(48000*2)))
        normalized, stats = normalize_voice(data.getvalue(), '.wav')
        self.assertLessEqual(abs(stats['lufs']+16), .1)
        self.assertLessEqual(stats['peak_dbtp'], -1.5)
        with wave.open(io.BytesIO(normalized)) as audio:
            self.assertEqual((audio.getnchannels(), audio.getframerate()), (1, 48000))

    def test_invalid_audio_rejected(self):
        with self.assertRaises(ValueError):
            normalize_voice(b'not audio', '.wav')
