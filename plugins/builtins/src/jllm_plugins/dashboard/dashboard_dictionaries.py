# ruff: noqa: E501
"""Authenticated dictionary administration for the bot's existing in-memory dictionaries.

The public HTML viewer is deliberately not served or rewritten here. Sound edits
invoke its existing rebuild hook so its read-only listing stays in sync.
"""

import asyncio
import logging
import math
import secrets
from pathlib import Path

from aiohttp import web

from jllm_plugins.dictionaries.dictionary import normalize, transcode_to_wav

logger = logging.getLogger(__name__)
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


class DictionaryAdmin:
    def _error(self, message, status):
        return self._json_error(message, status)

    @staticmethod
    def _valid_name(value, limit):
        return (
            isinstance(value, str)
            and bool(value.strip())
            and len(value.strip()) <= limit
            and not any(ord(char) < 32 for char in value)
        )

    async def _body(self, request):
        try:
            return await self._json_body(request)
        except web.HTTPException as exc:
            return self._error(exc.text or "JSON形式のリクエストが必要です。", exc.status)

    @staticmethod
    def _safe_file(directory: Path, filename: str):
        if not isinstance(filename, str) or Path(filename).name != filename or filename in {".", ".."}:
            return None
        path = directory / filename
        if not path.is_file() or path.is_symlink():
            return None
        return path

    async def _upload(self, request):
        if not request.content_type.startswith("multipart/"):
            return self._error("ファイルを含むフォームで送信してください。", 400)
        try:
            form = await request.post()
        except (ValueError, web.HTTPException):
            return self._error("アップロードを読み込めません。", 400)
        upload = form.get("file")
        if not isinstance(upload, web.FileField):
            return self._error("ファイルを選択してください。", 400)
        data = upload.file.read(MAX_UPLOAD_BYTES + 1)
        if not data or len(data) > MAX_UPLOAD_BYTES:
            return self._error("ファイルは10MB以内にしてください。", 413)
        return form, upload, data

    @staticmethod
    def _rename_mapping(mapping, old, new, meta=None):
        value = mapping.pop(old)
        author = meta.pop(old, None) if meta is not None else None
        mapping[new] = value
        if author is not None:
            meta[new] = author

    def _readings_item(self, key):
        return next((existing for existing in self.readings.entries if normalize(existing) == normalize(key)), None)

    async def _edit_reading(self, request):
        data = await self._body(request)
        if isinstance(data, web.Response):
            return data
        original, trigger, reading = data.get("original"), data.get("trigger"), data.get("reading")
        if not self._valid_name(original, 100) or not self._valid_name(trigger, 100) or not self._valid_name(reading, 200):
            return self._error("登録語と読みを確認してください。", 400)
        old = self._readings_item(original)
        if old is None:
            return self._error("登録語が見つかりません。", 404)
        trigger, reading = trigger.strip(), reading.strip()
        collision = self._readings_item(trigger)
        if collision is not None and collision != old:
            return self._error("変更先の登録語は既に存在します。", 409)
        author = self.readings.meta.get(old)
        if old != trigger:
            del self.readings.entries[old]
            self.readings.meta.pop(old, None)
        self.readings.entries[trigger] = reading
        if author:
            self.readings.meta[trigger] = author
        self.readings._rebuild()
        self.readings.save()
        return web.json_response({"ok": True})

    def _sound(self):
        if self.reader is None:
            raise web.HTTPServiceUnavailable(text="音声辞書はこのBotでは利用できません。")
        return self.reader.dictionary

    async def _list_sounds(self, request):
        dictionary = self._sound()
        entries = [{"phrase": phrase, "files": files, "author": dictionary.author_of(phrase),
                    "volumes": dict(dictionary.volumes.get(phrase, {}))}
                   for phrase, files in dictionary.items()]
        return web.json_response({"entries": entries, "total": len(entries), "enabled": dictionary.enabled})

    async def _sound_file(self, request):
        dictionary = self._sound()
        phrase, filename = request.query.get("phrase", ""), request.query.get("file", "")
        key = dictionary._find_key(phrase)
        if key is None or filename not in dictionary.entries[key]:
            return self._error("登録ファイルが見つかりません。", 404)
        path = self._safe_file(dictionary.sounds_dir, filename)
        if path is None:
            return self._error("登録ファイルが見つかりません。", 404)
        return web.FileResponse(path, headers={"Content-Type": "audio/wav", "Cache-Control": "no-store"})

    async def _edit_sound(self, request):
        dictionary = self._sound()
        data = await self._body(request)
        if isinstance(data, web.Response):
            return data
        original, phrase = data.get("original"), data.get("phrase")
        if not self._valid_name(original, 100) or not self._valid_name(phrase, 100):
            return self._error("語句を確認してください。", 400)
        old = dictionary._find_key(original)
        if old is None:
            return self._error("語句が見つかりません。", 404)
        phrase = phrase.strip()
        collision = dictionary._find_key(phrase)
        if collision is not None and collision != old:
            return self._error("変更先の語句は既に存在します。", 409)
        files = data.get("files", dictionary.entries[old])
        if (
            not isinstance(files, list) or not files
            or any(not isinstance(name, str) or name not in dictionary.entries[old] for name in files)
            or len(set(files)) != len(files)
        ):
            return self._error("登録中の音声ファイルを一つ以上選択してください。", 400)
        author = dictionary.meta.get(old)
        if old != phrase:
            del dictionary.entries[old]
            dictionary.meta.pop(old, None)
            moved = dictionary.volumes.pop(old, None)
            if moved:
                dictionary.volumes[phrase] = moved
        dictionary.entries[phrase] = files
        if author:
            dictionary.meta[phrase] = author
        raw_volumes = data.get("volumes")
        if raw_volumes is not None:
            if (
                not isinstance(raw_volumes, dict)
                or any(
                    not isinstance(name, str) or name not in files
                    or not isinstance(gain, (int, float)) or isinstance(gain, bool)
                     or not math.isfinite(float(gain)) or not 0.0 <= float(gain) <= 3.0
                    for name, gain in raw_volumes.items()
                )
            ):
                return self._error("音量は0〜300%の数値で指定してください。", 400)
            dictionary.set_volumes(phrase, {name: float(gain) for name, gain in raw_volumes.items()})
        else:
            pruned = {name: gain for name, gain in dictionary.volumes.get(phrase, {}).items() if name in files}
            if pruned:
                dictionary.volumes[phrase] = pruned
            else:
                dictionary.volumes.pop(phrase, None)
        dictionary._rebuild()
        dictionary.save()
        self.reader._rebuild_sound_dashboard()
        return web.json_response({"ok": True, "volumes": dict(dictionary.volumes.get(phrase, {}))})

    async def _remove_sound(self, request):
        dictionary = self._sound()
        data = await self._body(request)
        if isinstance(data, web.Response):
            return data
        phrase = data.get("phrase")
        if not self._valid_name(phrase, 100):
            return self._error("削除する語句を指定してください。", 400)
        if not dictionary.remove(phrase):
            return self._error("語句が見つかりません。", 404)
        self.reader._rebuild_sound_dashboard()
        return web.json_response({"ok": True})

    async def _upload_sound(self, request):
        dictionary = self._sound()
        parsed = await self._upload(request)
        if isinstance(parsed, web.Response):
            return parsed
        form, upload, content = parsed
        phrase = form.get("phrase")
        if not self._valid_name(phrase, 100):
            return self._error("語句は100文字以内で入力してください。", 400)
        phrase = phrase.strip()
        existing = dictionary._find_key(phrase)
        append = form.get("append") == "true"
        if append and existing is None:
            return self._error("音声追加先の語句がありません。", 404)
        suffix = Path(upload.filename or "").suffix.lower()
        if suffix not in {".wav", ".mp3", ".m4a", ".ogg", ".flac"}:
            return self._error("wav/mp3/m4a/ogg/flac を選択してください。", 400)
        filename = f"dashboard-{secrets.token_hex(12)}.wav"
        target = dictionary.sounds_dir / filename
        try:
            converted = await asyncio.to_thread(
                transcode_to_wav, content, suffix, target, getattr(dictionary, "audio_config", None)
            )
            if not converted or not target.is_file() or target.stat().st_size == 0:
                return self._error("音声を変換できませんでした。", 422)
            if append:
                dictionary.entries[existing].append(filename)
                dictionary._rebuild()
                dictionary.save()
            else:
                dictionary.add(phrase, filename, author="ダッシュボード")
            self.reader._rebuild_sound_dashboard()
            return web.json_response({"ok": True, "file": filename}, status=201)
        except OSError:
            logger.exception("音声ファイルの保存に失敗")
            return self._error("音声ファイルを保存できませんでした。", 500)
        finally:
            if not target.is_file() or target.stat().st_size == 0:
                target.unlink(missing_ok=True)

    def _media(self):
        if self.reader is None:
            raise web.HTTPServiceUnavailable(text="GIF辞書はこのBotでは利用できません。")
        return self.reader.media

    async def _list_gifs(self, request):
        media = self._media()
        entries = [{"trigger": trigger, "file": filename}
                   for trigger, filename in sorted(media.items())]
        return web.json_response({"entries": entries, "total": len(entries)})

    async def _gif_file(self, request):
        media = self._media()
        key = self.reader._media_key(request.query.get("trigger", ""))
        if key is None:
            return self._error("登録GIFが見つかりません。", 404)
        path = self._safe_file(self.reader.media_dir, media[key])
        if path is None or path.suffix.lower() != ".gif":
            return self._error("登録GIFが見つかりません。", 404)
        return web.FileResponse(path, headers={"Content-Type": "image/gif", "Cache-Control": "no-store"})

    async def _edit_gif(self, request):
        media = self._media()
        data = await self._body(request)
        if isinstance(data, web.Response):
            return data
        old, trigger = data.get("original"), data.get("trigger")
        if not self._valid_name(old, 80) or not self._valid_name(trigger, 80):
            return self._error("登録語句を確認してください。", 400)
        key = self.reader._media_key(old)
        if key is None:
            return self._error("登録GIFが見つかりません。", 404)
        trigger = trigger.strip()
        collision = self.reader._media_key(trigger)
        if collision is not None and collision != key:
            return self._error("変更先の語句は既に存在します。", 409)
        self._rename_mapping(media, key, trigger)
        self.reader._save_media()
        return web.json_response({"ok": True})

    async def _remove_gif(self, request):
        media = self._media()
        data = await self._body(request)
        if isinstance(data, web.Response):
            return data
        key = self.reader._media_key(data.get("trigger", ""))
        if key is None:
            return self._error("登録GIFが見つかりません。", 404)
        del media[key]
        self.reader._save_media()
        return web.json_response({"ok": True})

    async def _upload_gif(self, request):
        media = self._media()
        parsed = await self._upload(request)
        if isinstance(parsed, web.Response):
            return parsed
        form, _, content = parsed
        trigger = form.get("trigger")
        if not self._valid_name(trigger, 80):
            return self._error("登録語句は80文字以内で入力してください。", 400)
        if not content.startswith((b"GIF87a", b"GIF89a")):
            return self._error("GIF形式のファイルを選択してください。", 400)
        trigger = trigger.strip()
        existing = self.reader._media_key(trigger)
        filename = f"dashboard-{secrets.token_hex(12)}.gif"
        source = self.reader.media_dir / f".{filename}.input.gif"
        target = self.reader.media_dir / filename
        try:
            source.write_bytes(content)
            await asyncio.to_thread(self.reader._compress_gif, source, target)
            if not target.is_file() or target.stat().st_size == 0:
                return self._error("GIFを圧縮できませんでした。", 422)
            if existing is not None:
                del media[existing]
            media[trigger] = filename
            self.reader._save_media()
            return web.json_response({"ok": True, "file": filename}, status=201)
        except (OSError, RuntimeError):
            logger.exception("GIFファイルの保存に失敗")
            target.unlink(missing_ok=True)
            return self._error("GIFを登録できませんでした。", 422)
        finally:
            source.unlink(missing_ok=True)
