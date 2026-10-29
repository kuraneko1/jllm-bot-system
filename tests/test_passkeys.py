# ruff: noqa: E501
import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import cbor2
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from jllm_plugins.dashboard.dashboard import ReadingDashboard
from jllm_plugins.dictionaries.readings import ReadingDictionary
from webauthn.helpers import bytes_to_base64url as encode


class PasskeyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.dashboard = self.build_dashboard()
        self.client = TestClient(TestServer(self.dashboard.app))
        await self.client.start_server()
        self.private = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = b'test-credential-id'

    def build_dashboard(self):
        return ReadingDashboard(ReadingDictionary(str(self.root / 'readings.json')), SimpleNamespace(),
            password_file=str(self.root / 'auth.json'), public_url='http://localhost')

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    def client_data(self, challenge, kind, origin='http://localhost'):
        return json.dumps({'type': kind, 'challenge': challenge, 'origin': origin, 'crossOrigin': False}).encode()

    def registration(self, challenge, origin='http://localhost', uv=True):
        numbers = self.private.public_key().public_numbers()
        public_key = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, 'big'), -3: numbers.y.to_bytes(32, 'big')})
        data = (hashlib.sha256(b'localhost').digest() + bytes([0x45 if uv else 0x41]) + struct.pack('>I', 0)
                + bytes(16) + struct.pack('>H', len(self.credential_id)) + self.credential_id + public_key)
        return {'id': encode(self.credential_id), 'rawId': encode(self.credential_id), 'type': 'public-key',
                'response': {'clientDataJSON': encode(self.client_data(challenge, 'webauthn.create', origin)),
                             'attestationObject': encode(cbor2.dumps({'fmt': 'none', 'attStmt': {}, 'authData': data})),
                             'transports': ['internal']}}

    def assertion(self, challenge, origin='http://localhost', uv=True, valid_signature=True, count=1):
        client = self.client_data(challenge, 'webauthn.get', origin)
        data = hashlib.sha256(b'localhost').digest() + bytes([5 if uv else 1]) + struct.pack('>I', count)
        signature = self.private.sign(data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256())) if valid_signature else b'bad'
        return {'id': encode(self.credential_id), 'rawId': encode(self.credential_id), 'type': 'public-key',
                'response': {'clientDataJSON': encode(client), 'authenticatorData': encode(data),
                             'signature': encode(signature), 'userHandle': self.dashboard._passkeys['user_id']}}

    async def register(self):
        # 初回はセッションなしで登録できる（ブートストラップ）。
        options = await (await self.client.post('/api/passkeys/register/options')).json()
        response = await self.client.post('/api/passkeys/register/verify', json={
            'name': 'Test device', 'credential': self.registration(options['challenge'])})
        self.assertEqual(response.status, 201, await response.text())
        return options

    async def login_passkey(self, count=1):
        options = await (await self.client.post('/api/passkeys/login/options')).json()
        response = await self.client.post('/api/passkeys/login/verify', json={
            'credential': self.assertion(options['challenge'], count=count)})
        self.assertEqual(response.status, 200, await response.text())
        return options

    async def test_registration_login_persistence_and_replay_protection(self):
        await self.register()
        self.assertEqual(self.dashboard._passkey_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.build_dashboard()._passkeys, self.dashboard._passkeys)
        await self.client.post('/api/logout')
        options = await (await self.client.post('/api/passkeys/login/options')).json()
        payload = {'credential': self.assertion(options['challenge'])}
        response = await self.client.post('/api/passkeys/login/verify', json=payload)
        self.assertEqual(response.status, 200, await response.text())
        self.assertTrue((await (await self.client.get('/api/session')).json())['authenticated'])
        self.assertEqual(self.dashboard._passkeys['credentials'][0]['sign_count'], 1)
        self.assertEqual((await self.client.post('/api/passkeys/login/verify', json=payload)).status, 401)
        listed = await (await self.client.get('/api/passkeys')).json()
        self.assertEqual(listed['credentials'][0]['name'], 'Test device')
        self.assertNotIn('public_key', listed['credentials'][0])

    async def test_registration_requires_admin_once_enrolled(self):
        await self.register()
        await self.client.post('/api/logout')
        for path in ('/api/passkeys', '/api/passkeys/register/options', '/api/passkeys/register/verify'):
            method = 'get' if path == '/api/passkeys' else 'post'
            self.assertEqual((await getattr(self.client, method)(path)).status, 401)
        await self.login_passkey()
        options = await (await self.client.post('/api/passkeys/register/options')).json()
        await self.client.post('/api/logout')
        await self.login_passkey(count=2)
        denied = await self.client.post('/api/passkeys/register/verify', json={'credential': self.registration(options['challenge'])})
        self.assertEqual(denied.status, 400)
        self.assertEqual(len(self.dashboard._passkeys['credentials']), 1)

    async def test_invalid_origin_or_missing_verification_not_registered(self):
        for fields in ({'origin': 'https://evil.example'}, {'uv': False}):
            options = await (await self.client.post('/api/passkeys/register/options')).json()
            response = await self.client.post('/api/passkeys/register/verify', json={'credential': self.registration(options['challenge'], **fields)})
            self.assertEqual(response.status, 400)
        self.assertFalse(self.dashboard._passkeys['credentials'])

    async def test_invalid_login_never_grants_admin_session(self):
        await self.register()
        await self.client.post('/api/logout')
        for fields in ({'origin': 'https://evil.example'}, {'uv': False}, {'valid_signature': False}):
            options = await (await self.client.post('/api/passkeys/login/options')).json()
            response = await self.client.post('/api/passkeys/login/verify', json={'credential': self.assertion(options['challenge'], **fields)})
            self.assertEqual(response.status, 401)
            self.assertFalse((await (await self.client.get('/api/session')).json())['authenticated'])

    async def test_expired_challenge_rejected_and_key_removal_persists(self):
        await self.register()
        await self.login_passkey()
        options = await (await self.client.post('/api/passkeys/login/options')).json()
        for ceremony in self.dashboard._ceremonies.values():
            ceremony['expires'] = 0
        response = await self.client.post('/api/passkeys/login/verify', json={'credential': self.assertion(options['challenge'])})
        self.assertEqual(response.status, 401)
        self.assertEqual((await self.client.delete('/api/passkeys', json={'id': encode(self.credential_id)})).status, 200)
        self.assertFalse(self.build_dashboard()._passkeys['credentials'])
        await self.client.post('/api/logout')
        self.assertEqual((await self.client.post('/api/passkeys/login/options')).status, 404)

    async def test_existing_passkey_mode_refuses_missing_and_empty_store(self):
        def build():
            return ReadingDashboard(
                ReadingDictionary(str(self.root / 'readings.json')), SimpleNamespace(),
                password_file=str(self.root / 'auth.json'), public_url='http://localhost',
                require_existing_passkey=True,
            )

        with self.assertRaisesRegex(RuntimeError, '既存の管理者パスキー'):
            build()
        self.dashboard._save_passkeys(self.dashboard._passkeys)
        with self.assertRaisesRegex(RuntimeError, '既存の管理者パスキー'):
            build()
        await self.register()
        secured = build()
        self.assertEqual(len(secured._passkeys['credentials']), 1)

    async def test_existing_passkey_mode_cannot_delete_last_admin(self):
        await self.register()
        await self.login_passkey()
        self.dashboard.require_existing_passkey = True
        response = await self.client.delete('/api/passkeys', json={'id': encode(self.credential_id)})
        self.assertEqual(response.status, 409)
        self.assertEqual(len(self.build_dashboard()._passkeys['credentials']), 1)
        await self.client.post('/api/logout')
        self.assertEqual((await self.client.post('/api/passkeys/register/options')).status, 401)

    async def test_cross_origin_registration_blocked(self):
        response = await self.client.post('/api/passkeys/register/options', headers={'Origin': 'https://evil.example'})
        self.assertEqual(response.status, 403)


class MultiOriginPasskeyTests(unittest.IsolatedAsyncioTestCase):
    HOST = 'example.com'
    ORIGIN = 'http://example.com'

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.dashboard = ReadingDashboard(ReadingDictionary(str(self.root / 'readings.json')), SimpleNamespace(),
            password_file=str(self.root / 'auth.json'),
            public_url='http://localhost,http://example.com')
        self.client = TestClient(TestServer(self.dashboard.app))
        await self.client.start_server()
        self.private = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = b'second-origin-credential'

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    def host_headers(self):
        return {'Host': self.HOST}

    def client_data(self, challenge, kind):
        return json.dumps({'type': kind, 'challenge': challenge, 'origin': self.ORIGIN, 'crossOrigin': False}).encode()

    def rp_hash(self):
        return hashlib.sha256(self.HOST.encode()).digest()

    def registration(self, challenge):
        numbers = self.private.public_key().public_numbers()
        public_key = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, 'big'), -3: numbers.y.to_bytes(32, 'big')})
        data = (self.rp_hash() + bytes([0x45]) + struct.pack('>I', 0)
                + bytes(16) + struct.pack('>H', len(self.credential_id)) + self.credential_id + public_key)
        return {'id': encode(self.credential_id), 'rawId': encode(self.credential_id), 'type': 'public-key',
                'response': {'clientDataJSON': encode(self.client_data(challenge, 'webauthn.create')),
                             'attestationObject': encode(cbor2.dumps({'fmt': 'none', 'attStmt': {}, 'authData': data})),
                             'transports': ['internal']}}

    def assertion(self, challenge, count=1):
        client = self.client_data(challenge, 'webauthn.get')
        data = self.rp_hash() + bytes([5]) + struct.pack('>I', count)
        signature = self.private.sign(data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return {'id': encode(self.credential_id), 'rawId': encode(self.credential_id), 'type': 'public-key',
                'response': {'clientDataJSON': encode(client), 'authenticatorData': encode(data),
                             'signature': encode(signature), 'userHandle': self.dashboard._passkeys['user_id']}}

    async def test_second_origin_issues_its_own_rp_id(self):
        options = await (await self.client.post('/api/passkeys/register/options', headers=self.host_headers())).json()
        self.assertEqual(options['rp']['id'], self.HOST)
        fallback = await (await self.client.post('/api/passkeys/register/options')).json()
        self.assertEqual(fallback['rp']['id'], 'localhost')

    async def test_register_and_login_on_second_origin(self):
        options = await (await self.client.post('/api/passkeys/register/options', headers=self.host_headers())).json()
        response = await self.client.post('/api/passkeys/register/verify',
            json={'name': 'Second host', 'credential': self.registration(options['challenge'])},
            headers=self.host_headers())
        self.assertEqual(response.status, 201, await response.text())
        await self.client.post('/api/logout')
        options = await (await self.client.post('/api/passkeys/login/options', headers=self.host_headers())).json()
        response = await self.client.post('/api/passkeys/login/verify',
            json={'credential': self.assertion(options['challenge'])}, headers=self.host_headers())
        self.assertEqual(response.status, 200, await response.text())
        self.assertTrue((await (await self.client.get('/api/session')).json())['authenticated'])

    async def test_credential_bound_to_other_origin_is_rejected(self):
        options = await (await self.client.post('/api/passkeys/register/options')).json()
        self.assertEqual(options['rp']['id'], 'localhost')
        response = await self.client.post('/api/passkeys/register/verify',
            json={'name': 'Wrong host', 'credential': self.registration(options['challenge'])},
            headers=self.host_headers())
        self.assertEqual(response.status, 400)
