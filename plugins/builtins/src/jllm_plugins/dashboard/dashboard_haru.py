# ruff: noqa: E501
"""Haru 会話の話せるひと管理・リアルタイムフォーカス制御（WebUI 管理者向け）。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiohttp import web

from jllm_plugins.haru.client import HaruApiError
from jllm_plugins.haru.personas import (
    HARU_VOICE_EXTENSIONS,
    MAX_HARU_VOICE_BYTES,
    normalize_haru_voice,
)

logger = logging.getLogger(__name__)


def _serialize_persona(item: dict) -> dict:
    out = dict(item)
    out["has_voice"] = bool(item.get("voice_file"))
    out["voice_url"] = f"/api/haru/personas/{item['id']}/voice" if item.get("voice_file") else ""
    return out


class HaruAdmin:
    async def _haru_state(self, request: web.Request) -> web.Response:
        if self.haru is None:
            return web.json_response(
                {"supported": False, "server_ready": False, "personas": [], "guilds": []}
            )
        snap = await self.haru.status_snapshot()
        return web.json_response(
            {
                "supported": True,
                "server_ready": bool(snap.get("server_ready")),
                "personas": [_serialize_persona(p) for p in snap.get("personas", [])],
                "guilds": snap.get("guilds", []),
            }
        )

    async def _haru_save_persona(
        self, request: web.Request, *, persona_id: str | None = None
    ) -> web.Response:
        if self.haru is None:
            return self._json_error("Haru プラグインが無効です。", 400)
        try:
            form = await request.post()
        except web.HTTPRequestEntityTooLarge:
            return self._json_error("音声ファイルは50MB以内にしてください。", 413)
        except (ValueError, web.HTTPException):
            return self._json_error("フォーム形式のリクエストが必要です。", 400)

        def text_field(key: str, default: str = "") -> str:
            val = form.get(key)
            return str(val) if isinstance(val, str) else default

        upload = form.get("voice")
        voice_wav: bytes | None = None
        voice_seconds: float | None = None
        if isinstance(upload, web.FileField) and (upload.filename or "").strip():
            suffix = Path(upload.filename or "").suffix.lower()
            if suffix not in HARU_VOICE_EXTENSIONS:
                return self._json_error(
                    "WAV / FLAC / Ogg / MP3 / WebM などの音声ファイルを選択してください。", 400
                )
            raw = upload.file.read(MAX_HARU_VOICE_BYTES + 1)
            if not raw or len(raw) > MAX_HARU_VOICE_BYTES:
                return self._json_error("音声ファイルは空でない50MB以内のものにしてください。", 413)
            try:
                voice_wav, voice_seconds = await asyncio.to_thread(
                    normalize_haru_voice, raw, suffix
                )
            except ValueError as exc:
                return self._json_error(str(exc), 400)

        try:
            guidance = float(text_field("guidance_scale", "1.5") or "1.5")
        except ValueError:
            return self._json_error("guidance_scale は数値で指定してください。", 400)

        try:
            saved = await asyncio.to_thread(
                self.haru.store.save_persona,
                persona_id=persona_id,
                name=text_field("name"),
                persona=text_field("persona"),
                gender=text_field("gender"),
                backchannel=text_field("backchannel", "mid"),
                other_name=text_field("other_name"),
                relationship=text_field("relationship"),
                situation=text_field("situation"),
                custom_prompt=text_field("custom_prompt"),
                guidance_scale=guidance,
                voice_wav=voice_wav,
                voice_seconds=voice_seconds,
            )
        except KeyError as exc:
            return self._json_error(str(exc), 404)
        except ValueError as exc:
            return self._json_error(str(exc), 400)
        except Exception:
            logger.exception("Haru 話者設定の保存に失敗")
            return self._json_error("話者設定の保存に失敗しました。", 500)

        return web.json_response(
            {"ok": True, "persona": _serialize_persona(saved)},
            status=200 if persona_id else 201,
        )

    async def _haru_create_persona(self, request: web.Request) -> web.Response:
        return await self._haru_save_persona(request, persona_id=None)

    async def _haru_update_persona(self, request: web.Request) -> web.Response:
        persona_id = request.match_info.get("persona_id", "").strip()
        if not persona_id:
            return self._json_error("話者IDが必要です。", 400)
        return await self._haru_save_persona(request, persona_id=persona_id)

    async def _haru_delete_persona(self, request: web.Request) -> web.Response:
        if self.haru is None:
            return self._json_error("Haru プラグインが無効です。", 400)
        persona_id = request.match_info.get("persona_id", "").strip()
        deleted = await asyncio.to_thread(self.haru.store.delete_persona, persona_id)
        if not deleted:
            return self._json_error("指定された話者が見つかりません。", 404)
        return web.json_response({"ok": True, "id": persona_id})

    async def _haru_persona_voice(self, request: web.Request) -> web.StreamResponse:
        if self.haru is None:
            return self._json_error("Haru プラグインが無効です。", 400)
        persona_id = request.match_info.get("persona_id", "").strip()
        persona = self.haru.store.get(persona_id)
        if persona is None:
            return self._json_error("話者が見つかりません。", 404)
        vpath = self.haru.store.voice_path(persona)
        if vpath is None:
            return self._json_error("リファレンス音声が未登録です。", 404)
        return web.FileResponse(vpath, headers={"Content-Type": "audio/wav"})

    async def _haru_set_focus(self, request: web.Request) -> web.Response:
        if self.haru is None:
            return self._json_error("Haru プラグインが無効です。", 400)
        try:
            data = await self._json_body(request)
        except web.HTTPException as exc:
            return self._json_error(exc.text or "JSON形式のリクエストが必要です。", exc.status)
        try:
            guild_id = int(data.get("guild_id"))
        except (TypeError, ValueError):
            return self._json_error("有効な guild_id を指定してください。", 400)
        raw_user = data.get("user_id")
        if raw_user in (None, "", "all"):
            user_id = None
        else:
            try:
                user_id = int(raw_user)
            except (TypeError, ValueError):
                return self._json_error("有効な user_id を指定してください。", 400)
        saved = self.haru.set_guild_focus(guild_id, user_id)
        return web.json_response(
            {"ok": True, "guild_id": str(guild_id), "user_id": str(saved) if saved else None}
        )

    async def _haru_switch_persona(self, request: web.Request) -> web.Response:
        if self.haru is None:
            return self._json_error("Haru プラグインが無効です。", 400)
        try:
            data = await self._json_body(request)
        except web.HTTPException as exc:
            return self._json_error(exc.text or "JSON形式のリクエストが必要です。", exc.status)
        try:
            guild_id = int(data.get("guild_id"))
        except (TypeError, ValueError):
            return self._json_error("有効な guild_id を指定してください。", 400)
        persona_id = str(data.get("persona_id") or "").strip()
        if not persona_id:
            return self._json_error("話者を選択してください。", 400)
        try:
            await self.haru.switch_active_persona(guild_id, persona_id)
        except HaruApiError as exc:
            return self._json_error(str(exc), 400)
        return web.json_response({"ok": True, "guild_id": str(guild_id), "persona_id": persona_id})

    async def _haru_stop_session(self, request: web.Request) -> web.Response:
        if self.haru is None:
            return self._json_error("Haru プラグインが無効です。", 400)
        try:
            data = await self._json_body(request)
        except web.HTTPException as exc:
            return self._json_error(exc.text or "JSON形式のリクエストが必要です。", exc.status)
        try:
            guild_id = int(data.get("guild_id"))
        except (TypeError, ValueError):
            return self._json_error("有効な guild_id を指定してください。", 400)
        stopped = await self.haru.stop_guild(guild_id, keep_voice_if_reader=True)
        return web.json_response({"ok": True, "stopped": stopped})
