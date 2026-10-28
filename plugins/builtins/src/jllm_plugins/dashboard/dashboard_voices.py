# ruff: noqa: E501
import asyncio
import logging
from pathlib import Path

from aiohttp import web
from jllm_tts import TTSBackendError

from jllm_plugins.voice_settings.speeds import check_speed
from jllm_plugins.voice_settings.volumes import check_volume

from .voice_cloning import MAX_VOICE_UPLOAD_BYTES, VOICE_EXTENSIONS, normalize_voice

logger = logging.getLogger(__name__)


class VoiceAdmin:
    async def _selected_voice_names(self):
        if self.reader is None or not getattr(self.reader, 'players', None):
            return {}
        voices = await self.tts.list_voices()
        names = {voice.id: voice.name for voice in voices}
        return {guild: names.get(player.speaker_id, player.speaker_id)
                for guild, player in self.reader.players.items()}

    async def _restore_selected_voices(self, names):
        if not names:
            return
        voices = await self.tts.list_voices()
        ids = {voice.name: voice.id for voice in voices}
        fallback = ids.get(getattr(self.tts, 'default_voice', 'none'), voices[0].id if voices else 1)
        for guild, name in names.items():
            player = self.reader.players.get(guild)
            if player:
                player.speaker_id = ids.get(name, fallback)

    async def _voice_library(self, request):
        if not getattr(self.tts, 'supports_voice_management', False):
            return web.json_response({'supported': False, 'voices': []})
        try:
            voices = await self.tts.voice_library()
        except TTSBackendError as exc:
            return self._json_error(str(exc), 502)
        store = getattr(getattr(self, 'reader', None), 'voice_volumes', None)
        speeds = getattr(getattr(self, 'reader', None), 'voice_speeds', None)
        if store is not None or speeds is not None:
            merged = []
            for voice in voices:
                item = dict(voice)
                if store is not None:
                    item['volume'] = store.get(voice['name'], 1.0)
                if speeds is not None:
                    speed = speeds.get(voice['name'])
                    if speed is not None:
                        item['speed'] = speed
                merged.append(item)
            voices = merged
        return web.json_response({'supported': True, 'voices': voices})

    async def _voice_catalog(self, request):
        """全バックエンドの話者をエンジンごとに返す（話者クローンページ用）。

        Irodori 以外（z170 / drama3 / ming など）の話者も、ここで有効・無効を
        切り替えられる。登録・削除は ``/api/voice-library``（Irodori のみ）。
        """

        catalog = getattr(self.tts, 'voice_catalog', None)
        if catalog is None:
            try:
                voices = await self.tts.list_voices()
            except TTSBackendError as exc:
                return self._json_error(str(exc), 502)
            disabled = getattr(self.tts, 'disabled_voices', set()) or set()
            return web.json_response({'supported': False, 'engines': [{
                'id': getattr(self.tts, 'name', 'active'),
                'display': getattr(self.tts, 'display_name', '') or '読み上げエンジン',
                'kind': getattr(self.tts, 'name', ''),
                'active': True,
                'manageable': bool(getattr(self.tts, 'supports_voice_management', False)),
                'voices': [
                    {'name': voice.name, 'id': voice.id,
                     'enabled': voice.name not in disabled}
                    for voice in voices
                ],
                'error': '',
            }]})
        try:
            engines = await catalog()
        except TTSBackendError as exc:
            return self._json_error(str(exc), 502)
        return web.json_response({'supported': True, 'engines': engines})

    async def _set_catalog_voice(self, request):
        """名前だけで有効・無効を切り替える（全エンジン共通の ``disabled_voices.json``）。"""

        try:
            data = await self._json_body(request)
        except web.HTTPException as exc:
            return self._json_error(exc.text, exc.status)
        name, enabled = data.get('name'), data.get('enabled')
        if not isinstance(name, str) or not name.strip() or not isinstance(enabled, bool):
            return self._json_error('話者と有効・無効を指定してください。', 400)
        setter = getattr(self.tts, 'set_shared_voice_enabled', None)
        if setter is None:
            return self._json_error('この構成では話者の有効・無効を切り替えられません。', 400)
        try:
            setter(name, enabled)
        except TTSBackendError as exc:
            return self._json_error(str(exc), 400)
        logger.info('話者「%s」を%sにしました（全エンジン共通）', name, '有効' if enabled else '無効')
        return web.json_response({'ok': True, 'name': name.strip(), 'enabled': enabled})

    async def _set_voice_volume(self, request):
        try:
            data = await self._json_body(request)
        except web.HTTPException as exc:
            return self._json_error(exc.text, exc.status)
        name, raw_volume = data.get('name'), data.get('volume')
        if not isinstance(name, str) or not name.strip():
            return self._json_error('話者を選択してください。', 400)
        try:
            volume = check_volume(raw_volume)
        except ValueError as exc:
            return self._json_error(str(exc), 400)
        store = getattr(getattr(self, 'reader', None), 'voice_volumes', None)
        if store is None:
            return self._json_error('Botが起動していないため音量を保存できません。', 503)
        if getattr(self.tts, 'supports_voice_management', False):
            try:
                voices = await self.tts.voice_library()
            except TTSBackendError as exc:
                return self._json_error(str(exc), 502)
            if all(item['name'] != name for item in voices):
                return self._json_error('話者が見つかりません。', 404)
        try:
            store.set(name, volume)
        except (OSError, ValueError) as exc:
            logger.exception('話者音量の保存に失敗')
            return self._json_error(str(exc) if isinstance(exc, ValueError) else '音量を保存できませんでした。', 400 if isinstance(exc, ValueError) else 500)
        return web.json_response({'ok': True, 'name': name, 'volume': volume})

    async def _set_voice_speed(self, request):
        try:
            data = await self._json_body(request)
        except web.HTTPException as exc:
            return self._json_error(exc.text, exc.status)
        name, raw_speed = data.get('name'), data.get('speed')
        if not isinstance(name, str) or not name.strip():
            return self._json_error('話者を選択してください。', 400)
        try:
            speed = check_speed(raw_speed)
        except ValueError as exc:
            return self._json_error(str(exc), 400)
        store = getattr(getattr(self, 'reader', None), 'voice_speeds', None)
        if store is None:
            return self._json_error('Botが起動していないため速度を保存できません。', 503)
        if getattr(self.tts, 'supports_voice_management', False):
            try:
                voices = await self.tts.voice_library()
            except TTSBackendError as exc:
                return self._json_error(str(exc), 502)
            if all(item['name'] != name for item in voices):
                return self._json_error('話者が見つかりません。', 404)
        try:
            store.set(name, speed)
        except OSError:
            logger.exception('話者速度の保存に失敗')
            return self._json_error('速度を保存できませんでした。', 500)
        return web.json_response({'ok': True, 'name': name, 'speed': speed})

    async def _set_voice_enabled(self, request):
        if not getattr(self.tts, 'supports_voice_management', False):
            return self._json_error('現在のTTSエンジンは話者設定に対応していません。', 400)
        try:
            data = await self._json_body(request)
        except web.HTTPException as exc:
            return self._json_error(exc.text, exc.status)
        name, enabled = data.get('name'), data.get('enabled')
        if not isinstance(name, str) or not name.strip() or not isinstance(enabled, bool):
            return self._json_error('話者と有効・無効を指定してください。', 400)
        async with self._voice_admin_lock:
            try:
                voices = await self.tts.voice_library()
                voice = next((item for item in voices if item['name'] == name), None)
                if voice is None:
                    return self._json_error('話者が見つかりません。', 404)
                if not voice['deletable']:
                    return self._json_error('標準・設定話者は無効化できません。', 400)
                selected = await self._selected_voice_names()
                await self.tts.set_voice_enabled(name, enabled)
                await self._restore_selected_voices(selected)
            except TTSBackendError as exc:
                return self._json_error(str(exc), 502)
        return web.json_response({'ok': True, 'name': name, 'enabled': enabled})

    async def _add_voice(self, request):
        if not getattr(self.tts, 'supports_voice_management', False):
            return self._json_error('現在のTTSエンジンは話者登録に対応していません。', 400)
        try:
            form = await request.post()
        except web.HTTPRequestEntityTooLarge:
            return self._json_error('参照音声は20MB以内にしてください。', 413)
        except (ValueError, web.HTTPException):
            return self._json_error('音声ファイルと話者名を選択してください。', 400)
        name, upload = form.get('name'), form.get('file')
        if not isinstance(name, str) or not name.strip() or len(name.strip().encode('utf-8')) > 200:
            return self._json_error('話者名を入力してください（UTF-8で200バイト以内）。', 400)
        name = name.strip()
        if name in {'.', '..', 'none', 'no_ref', 'no-ref', 'null', 'text-only'} or any(
            char in '/\\' or ord(char) < 32 or ord(char) == 127 for char in name
        ):
            return self._json_error('話者名に使えない文字・名前が含まれています。', 400)
        if not isinstance(upload, web.FileField):
            return self._json_error('音声ファイルを選択してください。', 400)
        suffix = Path(upload.filename or '').suffix.lower()
        if suffix not in VOICE_EXTENSIONS:
            return self._json_error('WAV・MP3・WebMなどの音声ファイルを選択してください。', 400)
        data = upload.file.read(MAX_VOICE_UPLOAD_BYTES+1)
        if not data or len(data) > MAX_VOICE_UPLOAD_BYTES:
            return self._json_error('参照音声は空でない20MB以内のファイルにしてください。', 413)
        async with self._voice_admin_lock:
            try:
                voices = await self.tts.voice_library()
                if any(item['name'] == name for item in voices):
                    return self._json_error('同名の話者が存在します。別の名前を指定してください。', 409)
                selected = await self._selected_voice_names()
                audio, stats = await asyncio.to_thread(normalize_voice, data, suffix)
                await self.tts.add_voice(name, audio)
                await self._restore_selected_voices(selected)
            except (ValueError, TimeoutError) as exc:
                return self._json_error(str(exc), 400)
            except TTSBackendError as exc:
                return self._json_error(str(exc), 502)
            except Exception:
                logger.exception('話者クローン登録に失敗')
                return self._json_error('話者登録に失敗しました。時間をおいて再度お試しください。', 500)
        return web.json_response({'ok': True, 'name': name, **stats}, status=201)

    async def _delete_voice(self, request):
        if not getattr(self.tts, 'supports_voice_management', False):
            return self._json_error('現在のTTSエンジンは話者削除に対応していません。', 400)
        try:
            data = await self._json_body(request)
        except web.HTTPException as exc:
            return self._json_error(exc.text, exc.status)
        name = data.get('name')
        if not isinstance(name, str) or not name.strip():
            return self._json_error('削除する話者を選択してください。', 400)
        async with self._voice_admin_lock:
            try:
                voices = await self.tts.voice_library()
                voice = next((v for v in voices if v['name'] == name), None)
                if voice is None:
                    return self._json_error('話者が見つかりません。', 404)
                if not voice['deletable']:
                    return self._json_error('この話者は参照音声ファイルの削除対象ではありません。', 400)
                selected = await self._selected_voice_names()
                await self.tts.delete_voice(name)
                await self._restore_selected_voices(selected)
            except TTSBackendError as exc:
                return self._json_error(str(exc), 502)
            if self.reader is not None:
                keys = [key for key, value in self.reader.user_voices.items() if value == name]
                for key in keys:
                    del self.reader.user_voices[key]
                if keys:
                    self.reader._save_user_voices()
                forced = getattr(self.reader, 'forced_voices', None)
                if forced is not None:
                    dropped = [key for key, value in forced.items() if value == name]
                    for key in dropped:
                        del forced[key]
                    if dropped and hasattr(self.reader, '_save_forced_voices'):
                        self.reader._save_forced_voices()
                store = getattr(self.reader, 'voice_volumes', None)
                if store is not None:
                    store.remove(name)
                speeds = getattr(self.reader, 'voice_speeds', None)
                if speeds is not None:
                    speeds.remove(name)
        return web.json_response({'ok': True, 'name': name})
