"""Haru Server (HTTP + WebSocket) クライアント。"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class HaruConfig:
    url: str = "http://127.0.0.1:8998"
    personas_path: str = "haru_personas.json"
    voices_dir: str = "haru_voices"
    focus_path: str = "haru_focus.json"
    guidance_scale: float = 1.5
    output_volume: float = 1.0
    connect_timeout_seconds: float = 20.0


class HaruApiError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


def _ws_url(base_url: str, path_and_query: str) -> str:
    parsed = urlsplit(base_url.rstrip("/"))
    scheme = "wss" if parsed.scheme == "https" else "ws"
    base_path = parsed.path.rstrip("/")
    return urlunsplit((scheme, parsed.netloc, f"{base_path}{path_and_query}", "", ""))


class HaruApiClient:
    """``API_SPEC.md`` 準拠の Haru-4B API クライアント。"""

    def __init__(self, base_url: str, *, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self._session: aiohttp.ClientSession | None = None
        #: ``(persona_id, sha256) -> reference_audio_id`` のキャッシュ。
        self._ref_cache: dict[tuple[str, str], str] = {}

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def ready(self) -> bool:
        try:
            async with self._http().get(f"{self.base_url}/api/ready") as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()
                return bool(isinstance(data, dict) and data.get("ready"))
        except (TimeoutError, aiohttp.ClientError, ValueError):
            return False

    async def config(self) -> dict:
        try:
            async with self._http().get(f"{self.base_url}/api/config") as resp:
                if resp.status != 200:
                    raise HaruApiError(
                        f"Haru サーバ設定の取得に失敗しました（HTTP {resp.status}）。",
                        status=resp.status,
                    )
                data = await resp.json()
                return data if isinstance(data, dict) else {}
        except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
            raise HaruApiError("Haru サーバへ接続できません。") from exc

    async def _parse_error(self, resp: aiohttp.ClientResponse, fallback: str) -> HaruApiError:
        code = ""
        message = fallback
        try:
            payload = await resp.json()
            if isinstance(payload, dict):
                code = str(payload.get("code") or "")
                if payload.get("error"):
                    message = str(payload["error"])
        except Exception:  # noqa: BLE001
            pass
        return HaruApiError(message, status=resp.status, code=code)

    async def upload_reference(self, audio: bytes) -> dict:
        try:
            async with self._http().post(
                f"{self.base_url}/api/reference_audio",
                data=audio,
                headers={"Content-Type": "application/octet-stream"},
            ) as resp:
                if resp.status >= 400:
                    raise await self._parse_error(resp, "リファレンス音声の登録に失敗しました。")
                data = await resp.json()
                if not isinstance(data, dict) or not data.get("id"):
                    raise HaruApiError("リファレンス音声IDを取得できませんでした。")
                return data
        except HaruApiError:
            raise
        except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
            raise HaruApiError("Haru サーバへ音声を送信できませんでした。") from exc

    async def ensure_reference(
        self, persona_id: str, audio: bytes | None, *, force_upload: bool = False
    ) -> str:
        if audio:
            digest = hashlib.sha256(audio).hexdigest()
            key = (persona_id, digest)
            if not force_upload and key in self._ref_cache:
                return self._ref_cache[key]
            uploaded = await self.upload_reference(audio)
            ref_id = str(uploaded["id"])
            self._ref_cache[key] = ref_id
            return ref_id

        cfg = await self.config()
        defaults = cfg.get("default_references") or {}
        if isinstance(defaults, dict) and defaults:
            if "ja" in defaults:
                return "default_ja"
            return "default"
        raise HaruApiError(
            "このキャラクターにはリファレンス音声（3〜180秒）が設定されていません。"
            "WebUI の「Haru会話」タブから音声ファイルを登録してください。"
        )

    async def create_session(
        self,
        *,
        persona_id: str,
        system_prompt: str,
        audio: bytes | None,
        guidance_scale: float = 1.5,
    ) -> str:
        ref_id = await self.ensure_reference(persona_id, audio)
        payload = {
            "system_prompt": system_prompt,
            "reference_audio_id": ref_id,
            "guidance_scale": guidance_scale,
        }
        for attempt in (1, 2):
            try:
                async with self._http().post(f"{self.base_url}/api/session", json=payload) as resp:
                    if resp.status < 400:
                        data = await resp.json()
                        sid = str(data.get("id") or "")
                        if not sid:
                            raise HaruApiError("Haru セッションIDを取得できませんでした。")
                        return sid
                    err = await self._parse_error(resp, "Haru セッションの作成に失敗しました。")
            except HaruApiError:
                raise
            except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
                raise HaruApiError("Haru サーバへセッション作成要求を送れませんでした。") from exc

            if attempt == 1 and err.code == "reference_not_found" and audio:
                ref_id = await self.ensure_reference(persona_id, audio, force_upload=True)
                payload["reference_audio_id"] = ref_id
                continue
            raise err

        raise HaruApiError("Haru セッションを作成できませんでした。")

    async def connect_chat(self, session_id: str) -> aiohttp.ClientWebSocketResponse:
        ws_endpoint = _ws_url(self.base_url, f"/api/chat?session_id={quote(session_id, safe='')}")
        try:
            return await self._http().ws_connect(ws_endpoint, heartbeat=None)
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise HaruApiError("Haru 会話ストリームへの接続に失敗しました。") from exc
