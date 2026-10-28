# ruff: noqa: E501
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
from jllm_plugins.dashboard.dashboard import ReadingDashboard
from jllm_plugins.dictionaries.readings import ReadingDictionary
from jllm_tts import TTSBackendError

from tests.passkey_auth import ensure_admin_session
from tests.test_voice_admin import FakeVoiceTTS


class VoiceRequestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.tts = FakeVoiceTTS()
        self.dashboard = self.build_dashboard()
        self.client = TestClient(TestServer(self.dashboard.app))
        await self.client.start_server()

    def build_dashboard(self):
        return ReadingDashboard(ReadingDictionary(str(self.root/'readings.json')), self.tts,
            password_file=str(self.root/'auth.json'))

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    def form(self, name='Clone Applicant', requester='Applicant'):
        form = FormData()
        form.add_field('name', name)
        form.add_field('requester', requester)
        form.add_field('note', 'please review')
        form.add_field('file', b'original audio', filename='ref.wav', content_type='audio/wav')
        return form

    async def submit(self):
        with patch('jllm_plugins.dashboard.dashboard_voice_requests.normalize_voice', return_value=(b'normalized WAV', {'seconds':12,'lufs':-16,'peak_dbtp':-1.6})):
            response = await self.client.post('/api/voice-requests', data=self.form())
        self.assertEqual(response.status, 201, await response.text())
        request_id = (await response.json())['id']
        self.assertFalse(self.tts.added)
        return request_id

    async def login(self):
        await ensure_admin_session(self.client, self.dashboard)

    async def test_public_recorder_but_review_and_audio_are_admin_only(self):
        page = await self.client.get('/recorder')
        self.assertEqual(page.status,200)
        self.assertIn('台本ボイスレコーダー',await page.text())
        self.assertEqual(page.headers['X-Frame-Options'],'SAMEORIGIN')
        request_id = await self.submit()
        for path in ('/api/voice-requests', f'/api/voice-requests/{request_id}/file'):
            self.assertEqual((await self.client.get(path)).status,401)
        denied = await self.client.post(f'/api/voice-requests/{request_id}/review',json={'action':'approve'})
        self.assertEqual(denied.status,401)
        self.assertEqual((await self.client.post('/api/voice-library/upload',data=self.form())).status,401)
        self.assertFalse(self.tts.added)

    async def test_approved_request_registers_once_and_survives_restart(self):
        request_id=await self.submit()
        restored=self.build_dashboard()
        self.assertEqual(restored._voice_requests[request_id]['status'],'pending')
        self.assertEqual(restored._voice_request_manifest.stat().st_mode & 0o777,0o600)
        await self.login()
        listed=await (await self.client.get('/api/voice-requests')).json()
        self.assertEqual(listed['requests'][0]['requester'],'Applicant')
        preview=await self.client.get(f'/api/voice-requests/{request_id}/file')
        self.assertEqual(await preview.read(),b'normalized WAV')
        approved=await self.client.post(f'/api/voice-requests/{request_id}/review',json={'action':'approve'})
        self.assertEqual(approved.status,200,await approved.text())
        self.assertEqual(self.tts.added,[('Clone Applicant',b'normalized WAV')])
        self.assertEqual(self.build_dashboard()._voice_requests[request_id]['status'],'approved')
        self.assertFalse((self.dashboard._voice_request_dir/(request_id+'.wav')).exists())
        repeated=await self.client.post(f'/api/voice-requests/{request_id}/review',json={'action':'approve'})
        self.assertEqual(repeated.status,409)
        self.assertEqual(len(self.tts.added),1)

    async def test_rejection_deletes_audio_without_registering(self):
        request_id=await self.submit()
        await self.login()
        response=await self.client.post(f'/api/voice-requests/{request_id}/review',json={'action':'reject'})
        self.assertEqual(response.status,200)
        self.assertEqual(self.build_dashboard()._voice_requests[request_id]['status'],'rejected')
        self.assertFalse((self.dashboard._voice_request_dir/(request_id+'.wav')).exists())
        self.assertFalse(self.tts.added)

    async def test_invalid_audio_and_unsafe_names_never_saved(self):
        with patch('jllm_plugins.dashboard.dashboard_voice_requests.normalize_voice',side_effect=ValueError('invalid recording')):
            self.assertEqual((await self.client.post('/api/voice-requests',data=self.form())).status,400)
        for name in ('../bad', 'none', ''):
            response=await self.client.post('/api/voice-requests',data=self.form(name=name))
            self.assertEqual(response.status,400)
        self.assertFalse(self.dashboard._voice_requests)
        self.assertFalse(self.tts.added)

    async def test_same_name_and_backend_failure_preserve_request(self):
        request_id=await self.submit()
        with patch('jllm_plugins.dashboard.dashboard_voice_requests.normalize_voice') as normalize:
            self.assertEqual((await self.client.post('/api/voice-requests',data=self.form())).status,409)
            normalize.assert_not_called()
        await self.login()
        with patch.object(self.tts,'add_voice',side_effect=TTSBackendError('unavailable')):
            response=await self.client.post(f'/api/voice-requests/{request_id}/review',json={'action':'approve'})
        self.assertEqual(response.status,502)
        self.assertEqual(self.dashboard._voice_requests[request_id]['status'],'pending')
        self.tts.voices.append({'name':'Clone Applicant','deletable':True})
        response=await self.client.post(f'/api/voice-requests/{request_id}/review',json={'action':'approve'})
        self.assertEqual(response.status,409)
        self.assertFalse(self.tts.added)

    async def test_cross_origin_public_submission_blocked(self):
        response=await self.client.post('/api/voice-requests',data=self.form(),headers={'Origin':'https://evil.example'})
        self.assertEqual(response.status,403)
        self.assertFalse(self.dashboard._voice_requests)
