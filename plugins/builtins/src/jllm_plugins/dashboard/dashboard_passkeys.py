# ruff: noqa: E501
"""WebAuthn for the single dashboard administrator."""
import asyncio
import json
import os
import secrets
import time
from urllib.parse import urlsplit

from aiohttp import web
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url, options_to_json
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

CEREMONY_COOKIE = 'dashboard_passkey_ceremony'
CEREMONY_TTL = 300


class PasskeyAdmin:
    def _init_passkeys(self, public_url):
        origins = []
        for part in str(public_url).split(','):
            part = part.strip()
            if not part:
                continue
            parsed = urlsplit(part)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError('Invalid dashboard public URL')
            origins.append((f'{parsed.scheme}://{parsed.netloc}', parsed.hostname))
        if not origins:
            raise ValueError('Invalid dashboard public URL')
        # 先頭が既定。複数ドメインで公開する場合、リクエストのHostに一致する
        # origin/RP IDを使い、一致がなければ既定にフォールバックする。
        self._passkey_origins = origins
        self._passkey_origin, self._passkey_rp_id = origins[0]
        self._passkey_path = self.password_path.with_suffix('.passkeys.json')
        self._passkey_lock = asyncio.Lock()
        self._ceremonies = {}
        if self._passkey_path.exists():
            try:
                record = json.loads(self._passkey_path.read_text())
                assert isinstance(record['user_id'], str)
                assert len(base64url_to_bytes(record['user_id'])) == 32
                assert isinstance(record['credentials'], list)
                for key in record['credentials']:
                    assert base64url_to_bytes(key['id'])
                    assert base64url_to_bytes(key['public_key'])
                    assert isinstance(key['sign_count'], int) and key['sign_count'] >= 0
                    assert isinstance(key['name'], str)
                self._passkeys = record
            except (OSError, ValueError, KeyError, TypeError, AssertionError) as exc:
                raise RuntimeError('パスキー設定ファイルを読み込めません。') from exc
        else:
            self._passkeys = {'user_id': bytes_to_base64url(secrets.token_bytes(32)), 'credentials': []}

    def _save_passkeys(self, record):
        self._passkey_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._passkey_path.with_name('.' + self._passkey_path.name + secrets.token_hex(6))
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as handle:
                json.dump(record, handle, ensure_ascii=False)
            os.replace(temporary, self._passkey_path)
        finally:
            temporary.unlink(missing_ok=True)
        self._passkeys = record

    async def _list_passkeys(self, request):
        return web.json_response({'credentials': [
            {'id': key['id'], 'name': key['name']} for key in self._passkeys['credentials']
        ]})

    def _passkey_rp_for(self, request):
        host = (request.host or '').lower()
        for origin, rp_id in self._passkey_origins:
            if urlsplit(origin).netloc.lower() == host:
                return origin, rp_id
        hostname = host.split(':')[0]
        for origin, rp_id in self._passkey_origins:
            if urlsplit(origin).hostname.lower() == hostname:
                return origin, rp_id
        return self._passkey_origin, self._passkey_rp_id

    def _new_ceremony(self, request, options, kind, rp):
        now = time.monotonic()
        self._ceremonies = {k: v for k, v in self._ceremonies.items() if v['expires'] > now}
        if len(self._ceremonies) >= 1000:
            return self._json_error('時間を置いて再試行してください。', 429)
        token = secrets.token_urlsafe(32)
        self._ceremonies[token] = {
            'kind': kind, 'expires': now + CEREMONY_TTL, 'challenge': options.challenge,
            'origin': rp[0], 'rp_id': rp[1],
            'session': self._session_token(request) if kind == 'register' else None,
        }
        response = web.Response(text=options_to_json(options), content_type='application/json')
        response.set_cookie(CEREMONY_COOKIE, token, max_age=CEREMONY_TTL,
                            httponly=True, secure=self._secure_cookie(request), samesite='Strict', path='/api/passkeys')
        return response

    def _take_ceremony(self, request, kind):
        value = self._ceremonies.pop(request.cookies.get(CEREMONY_COOKIE, ''), None)
        if not value or value['kind'] != kind or value['expires'] <= time.monotonic():
            return None
        # パスキー未登録の初回だけ、管理セッションなしの登録を許可する。
        # 登録済み後は従来通りセッション必須。
        if kind == 'register' and self._passkeys['credentials']:
            if value['session'] != self._session_token(request):
                return None
        return value

    def _credential_descriptors(self):
        return [PublicKeyCredentialDescriptor(id=base64url_to_bytes(key['id']))
                for key in self._passkeys['credentials']]

    async def _passkey_register_options(self, request):
        if self._passkeys['credentials'] and self._session_token(request) is None:
            return self._json_error("ログインしてください。", 401)
        rp = self._passkey_rp_for(request)
        options = generate_registration_options(
            rp_id=rp[1], rp_name='Irodori Voice', user_name='administrator',
            user_display_name='Irodori 管理者', user_id=base64url_to_bytes(self._passkeys['user_id']),
            exclude_credentials=self._credential_descriptors(),
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                require_resident_key=True, user_verification=UserVerificationRequirement.REQUIRED,
            ),
        )
        return self._new_ceremony(request, options, 'register', rp)

    async def _passkey_register_verify(self, request):
        if self._passkeys['credentials'] and self._session_token(request) is None:
            return self._json_error("ログインしてください。", 401)
        ceremony = self._take_ceremony(request, 'register')
        if ceremony is None:
            return self._json_error('登録の期限切れです。もう一度開始してください。', 400)
        data = await self._body(request)
        if isinstance(data, web.Response):
            return data
        name = data.get('name', 'パスキー')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 64:
            return self._json_error('パスキー名は1〜64文字で入力してください。', 400)
        try:
            result = verify_registration_response(
                credential=data.get('credential', {}), expected_challenge=ceremony['challenge'],
                expected_rp_id=ceremony['rp_id'], expected_origin=ceremony['origin'],
                require_user_verification=True,
            )
        except (WebAuthnException, ValueError, TypeError, KeyError):
            return self._json_error('パスキーを確認できませんでした。再登録してください。', 400)
        async with self._passkey_lock:
            credential_id = bytes_to_base64url(result.credential_id)
            if any(key['id'] == credential_id for key in self._passkeys['credentials']):
                return self._json_error('このパスキーは登録済みです。', 409)
            record = {**self._passkeys, 'credentials': self._passkeys['credentials'] + [{
                'id': credential_id, 'public_key': bytes_to_base64url(result.credential_public_key),
                'sign_count': result.sign_count, 'name': name.strip(),
            }]}
            try:
                self._save_passkeys(record)
            except OSError:
                return self._json_error('パスキーを保存できませんでした。', 500)
        return web.json_response({'ok': True}, status=201)

    async def _passkey_login_options(self, request):
        if not self._passkeys['credentials']:
            return self._json_error('パスキーは未登録です。パスワードでログインして登録してください。', 404)
        rp = self._passkey_rp_for(request)
        options = generate_authentication_options(
            rp_id=rp[1], allow_credentials=self._credential_descriptors(),
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return self._new_ceremony(request, options, 'login', rp)

    async def _passkey_login_verify(self, request):
        ceremony = self._take_ceremony(request, 'login')
        if ceremony is None:
            return self._json_error('ログインの期限切れです。もう一度開始してください。', 401)
        data = await self._body(request)
        if isinstance(data, web.Response):
            return data
        credential = data.get('credential')
        if not isinstance(credential, dict):
            return self._json_error('パスキーを確認できませんでした。', 401)
        async with self._passkey_lock:
            key = next((key for key in self._passkeys['credentials'] if key['id'] == credential.get('id')), None)
            if key is None:
                return self._json_error('未登録のパスキーです。', 401)
            try:
                result = verify_authentication_response(
                    credential=credential, expected_challenge=ceremony['challenge'],
                    expected_rp_id=ceremony['rp_id'], expected_origin=ceremony['origin'],
                    credential_public_key=base64url_to_bytes(key['public_key']),
                    credential_current_sign_count=key['sign_count'], require_user_verification=True,
                )
            except (WebAuthnException, ValueError, TypeError, KeyError):
                return self._json_error('パスキーを確認できませんでした。', 401)
            record = {**self._passkeys, 'credentials': [
                {**item, 'sign_count': result.new_sign_count} if item['id'] == key['id'] else item
                for item in self._passkeys['credentials']
            ]}
            try:
                self._save_passkeys(record)
            except OSError:
                return self._json_error('認証結果を保存できませんでした。', 500)
        return self._create_session(request)

    async def _delete_passkey(self, request):
        data = await self._body(request)
        if isinstance(data, web.Response):
            return data
        async with self._passkey_lock:
            credentials = [key for key in self._passkeys['credentials'] if key['id'] != data.get('id')]
            if len(credentials) == len(self._passkeys['credentials']):
                return self._json_error('パスキーが見つかりません。', 404)
            if self.require_existing_passkey and not credentials:
                return self._json_error('最後の管理者パスキーは削除できません。', 409)
            try:
                self._save_passkeys({**self._passkeys, 'credentials': credentials})
            except OSError:
                return self._json_error('パスキーを削除できませんでした。', 500)
        return web.json_response({'ok': True})
