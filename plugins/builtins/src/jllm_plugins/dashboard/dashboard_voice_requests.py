# ruff: noqa: E501
"""Public clone submissions; only authenticated administrators can review or register them."""
import asyncio
import json
import logging
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path

from aiohttp import web
from jllm_tts import TTSBackendError

from .voice_cloning import MAX_VOICE_UPLOAD_BYTES, VOICE_EXTENSIONS, normalize_voice

logger = logging.getLogger(__name__)


class VoiceRequests:
    def _init_voice_requests(self):
        self._voice_request_dir = self.password_path.parent / (self.password_path.stem + '-voice-requests')
        self._voice_request_manifest = self._voice_request_dir / 'requests.json'
        self._voice_request_lock = asyncio.Lock()
        if self._voice_request_manifest.exists():
            try:
                self._voice_requests = json.loads(self._voice_request_manifest.read_text())
                if not isinstance(self._voice_requests, dict):
                    raise ValueError('invalid manifest')
                for key, item in self._voice_requests.items():
                    if len(key) != 32 or any(c not in '0123456789abcdef' for c in key):
                        raise ValueError('invalid request id')
                    if item['status'] not in {'pending', 'approved', 'rejected'}:
                        raise ValueError('invalid status')
                    for field in ('name', 'requester', 'note', 'created_at'):
                        if not isinstance(item[field], str):
                            raise ValueError('invalid record')
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise RuntimeError('話者登録申請を読み込めません。') from exc
        else:
            self._voice_requests = {}

    def _save_voice_requests(self, records):
        self._voice_request_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self._voice_request_dir / ('.requests-' + secrets.token_hex(8))
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as handle:
                json.dump(records, handle, ensure_ascii=False)
            os.replace(temporary, self._voice_request_manifest)
        finally:
            temporary.unlink(missing_ok=True)
        self._voice_requests = records

    @staticmethod
    def _request_text(value, limit):
        return isinstance(value, str) and len(value.strip()) <= limit and not any(ord(c) < 32 and c not in '\n\t' for c in value)

    async def _submit_voice_request(self, request):
        if not getattr(self.tts, 'supports_voice_management', False):
            return self._json_error('現在のTTSエンジンは話者登録に対応していません。', 400)
        try:
            form = await request.post()
        except web.HTTPRequestEntityTooLarge:
            return self._json_error('参照音声は20MB以内にしてください。', 413)
        except (ValueError, web.HTTPException):
            return self._json_error('音声ファイルと話者名を入力してください。', 400)
        name, requester, note = form.get('name', ''), form.get('requester', ''), form.get('note', '')
        if not self._request_text(name, 80) or not name.strip() or len(name.strip().encode()) > 200:
            return self._json_error('希望する話者名を入力してください（80文字・200バイト以内）。', 400)
        name = name.strip()
        if name in {'.', '..', 'none', 'no_ref', 'no-ref', 'null', 'text-only'} or any(c in '/\\' or ord(c) < 32 or ord(c) == 127 for c in name):
            return self._json_error('話者名に使えない文字が含まれています。', 400)
        if not self._request_text(requester, 80) or not requester.strip() or not self._request_text(note, 500):
            return self._json_error('申請者名は1〜80文字、メモは500文字以内で入力してください。', 400)
        upload = form.get('file')
        if not isinstance(upload, web.FileField):
            return self._json_error('録音ファイルを選択してください。', 400)
        suffix = Path(upload.filename or '').suffix.lower()
        if suffix not in VOICE_EXTENSIONS:
            return self._json_error('WAV・WebM・MP3などの音声ファイルを選択してください。', 400)
        data = upload.file.read(MAX_VOICE_UPLOAD_BYTES + 1)
        if not data or len(data) > MAX_VOICE_UPLOAD_BYTES:
            return self._json_error('音声は空でない20MB以内のファイルにしてください。', 413)
        async with self._voice_request_lock:
            if sum(item['status'] == 'pending' for item in self._voice_requests.values()) >= 100:
                return self._json_error('申請がいっぱいです。管理者の確認後に再申請してください。', 429)
            if any(item['status'] == 'pending' and item['name'] == name for item in self._voice_requests.values()):
                return self._json_error('同名の話者が申請中です。承認をお待ちください。', 409)
            try:
                audio, stats = await asyncio.to_thread(normalize_voice, data, suffix)
            except (ValueError, TimeoutError) as exc:
                return self._json_error(str(exc), 400)
            request_id = secrets.token_hex(16)
            target = self._voice_request_dir / (request_id + '.wav')
            try:
                self._voice_request_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(audio)
                record = {'name': name, 'requester': requester.strip(), 'note': note.strip(),
                          'status': 'pending', 'created_at': datetime.now(UTC).isoformat(), **stats}
                self._save_voice_requests({**self._voice_requests, request_id: record})
            except OSError:
                target.unlink(missing_ok=True)
                logger.exception('話者登録申請の保存に失敗')
                return self._json_error('申請を保存できませんでした。再度お試しください。', 500)
        return web.json_response({'ok': True, 'id': request_id, 'status': 'pending'}, status=201)

    async def _list_voice_requests(self, request):
        records = [{'id': key, **item} for key, item in self._voice_requests.items()]
        records.sort(key=lambda item: item['created_at'], reverse=True)
        return web.json_response({'requests': records})

    async def _voice_request_file(self, request):
        request_id = request.match_info['request_id']
        record = self._voice_requests.get(request_id)
        if record is None or record['status'] != 'pending':
            return self._json_error('申請中の音声が見つかりません。', 404)
        path = self._voice_request_dir / (request_id + '.wav')
        if not path.is_file() or path.is_symlink():
            return self._json_error('申請音声が見つかりません。', 404)
        return web.FileResponse(path, headers={'Content-Type': 'audio/wav'})

    async def _review_voice_request(self, request):
        data = await self._body(request)
        if isinstance(data, web.Response):
            return data
        action = data.get('action')
        if action not in {'approve', 'reject'}:
            return self._json_error('承認または拒否を指定してください。', 400)
        request_id = request.match_info['request_id']
        async with self._voice_request_lock:
            record = self._voice_requests.get(request_id)
            if record is None:
                return self._json_error('申請が見つかりません。', 404)
            if record['status'] != 'pending':
                return self._json_error('この申請は確認済みです。', 409)
            source = self._voice_request_dir / (request_id + '.wav')
            if action == 'approve':
                if not source.is_file() or source.is_symlink():
                    return self._json_error('申請音声が見つかりません。', 404)
                async with self._voice_admin_lock:
                    try:
                        voices = await self.tts.voice_library()
                        if any(voice['name'] == record['name'] for voice in voices):
                            return self._json_error('同名の話者が既に存在します。上書きせず、申請を保留しています。', 409)
                        selected = await self._selected_voice_names()
                        await self.tts.add_voice(record['name'], source.read_bytes())
                        await self._restore_selected_voices(selected)
                    except TTSBackendError as exc:
                        return self._json_error(str(exc), 502)
            reviewed = {**record, 'status': 'approved' if action == 'approve' else 'rejected',
                        'reviewed_at': datetime.now(UTC).isoformat()}
            try:
                self._save_voice_requests({**self._voice_requests, request_id: reviewed})
            except OSError:
                logger.exception('申請確認結果の保存に失敗')
                return self._json_error('確認結果を保存できませんでした。申請一覧と話者一覧を確認してください。', 500)
            source.unlink(missing_ok=True)
        return web.json_response({'ok': True, 'status': reviewed['status'], 'name': record['name']})
