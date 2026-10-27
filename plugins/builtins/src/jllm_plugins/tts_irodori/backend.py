"""Irodori-TTS-Server を使う backend。

設定は環境変数への暗黙依存を避け、plugin の instance config から注入する。
"""

import base64
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import quote

import aiohttp
from jllm_tts import TTSBackend, TTSBackendError, Voice


def _load_map(path_str: str) -> dict[str, str]:
    path = Path(path_str)

    if not path.exists():
        return {}

    try:
        data = json.loads(path.read_text("utf-8"))

    except OSError, ValueError:
        return {}

    if not isinstance(data, dict):
        return {}

    return {str(key): str(value) for key, value in data.items()}


#: ``stream_format=sse`` の応答（公式 Irodori-TTS-Server のチャンク単位ストリーミング）。
SSE_CONTENT_TYPES = {"text/event-stream"}

#: チャンクの音声が「生PCM」であることを示す media_type。
PCM_MEDIA_TYPES = {"audio/pcm", "audio/l16", "audio/raw", "application/octet-stream"}


class IrodoriBackend(TTSBackend):
    name = "irodori"
    display_name = "Irodori"
    supports_voice_management = True
    #: チャンク（文）単位のSSEストリーミングに対応（公式の ``stream_format=sse``）。
    supports_streaming = True
    #: ストリーミングで返る生PCMのサンプルレート（Irodori は 48kHz モノラル）。
    stream_rate = 48000
    #: 速度はサーバ側で掛けられる（``speed`` を受け付ける）ので、等速以外でも
    #: 全体を待たずに鳴らせる。
    streams_any_speed = True

    async def _voice_request(self, method, path, **kwargs):
        try:
            async with self.session.request(
                method, f"{self.base_url}/v1/audio/voices{path}", headers=self._headers(), **kwargs
            ) as response:
                payload = await response.json()
                if response.status >= 400:
                    if response.status == 409:
                        raise TTSBackendError("同名の話者が既に存在します。")
                    raise TTSBackendError("TTSサーバで話者を操作できませんでした。")
                return payload
        except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
            raise TTSBackendError("TTSサーバに接続できません。") from exc

    async def voice_library(self):
        payload = await self._voice_request("GET", "")
        return [
            {
                "name": item["id"],
                "enabled": item["id"] not in self.disabled_voices,
                "deletable": bool(item.get("ref_wav"))
                and Path(item["ref_wav"]).stem == item["id"]
                and not item.get("no_ref"),
            }
            for item in payload.get("data", [])
        ]

    async def add_voice(self, name, audio):
        form = aiohttp.FormData()
        form.add_field("voice_id", name)
        form.add_field("file", audio, filename="reference.wav", content_type="audio/wav")
        await self._voice_request("POST", "", data=form)
        self._voices = None
        await self.list_voices()

    async def delete_voice(self, name):
        await self._voice_request("DELETE", "/" + quote(name, safe=""))
        if name in self.disabled_voices:
            self._save_disabled_voices(self.disabled_voices - {name})
        self._voices = None
        await self.list_voices()

    async def set_voice_enabled(self, name, enabled):
        voices = await self.voice_library()
        voice = next((item for item in voices if item["name"] == name), None)
        if not voice or not voice["deletable"]:
            raise TTSBackendError("この話者は有効・無効の変更対象ではありません。")
        disabled = self.disabled_voices - {name} if enabled else self.disabled_voices | {name}
        self._save_disabled_voices(disabled)

    def _save_disabled_voices(self, disabled):
        if self.disabled_voices_path is not None:
            path = self.disabled_voices_path
            temporary = None
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=path.parent, delete=False
                ) as handle:
                    temporary = Path(handle.name)
                    json.dump(sorted(disabled), handle, ensure_ascii=False)
                os.replace(temporary, path)
            except OSError as exc:
                raise TTSBackendError("話者の有効・無効設定を保存できませんでした。") from exc
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        self.disabled_voices = set(disabled)

    def __init__(
        self,
        base_url: str,
        *,
        default_voice: str = "none",
        caption: str = "",
        api_key: str = "",
        model_name: str = "irodori-tts",
        voice_order: list[str] | None = None,
        lora_map: dict[str, str] | None = None,
        voices_cache: str = "",
        disabled_voices_path: str = "",
        streaming: bool = True,
        chunk_min_chars: int = 80,
        first_chunk_min_chars: int = 12,
        timeout: float = 120.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.default_voice = default_voice or "none"
        self.caption = caption
        self.api_key = api_key
        self.model_name = model_name
        self.voice_order = list(voice_order or [])
        # ストリーミング（公式の stream_format=sse）。合わない環境では env で切れる。
        self.supports_streaming = bool(streaming)
        # チャンクの切り方。既定 80 文字はサーバ既定と同じ＝音は wav 経路と同一。
        self.chunk_min_chars = max(1, int(chunk_min_chars))
        # 最初の文だけ小さく切って初音を早める（1チャンク≒1.4秒かかるため 10〜15 が安全）。
        self.first_chunk_min_chars = max(1, int(first_chunk_min_chars))
        self.lora_map = dict(lora_map or {})
        self.voices_cache = Path(voices_cache) if voices_cache else None

        self._voices: list[str] | None = None
        self.disabled_voices_path = Path(disabled_voices_path) if disabled_voices_path else None
        self.disabled_voices = set()
        if self.disabled_voices_path is not None and self.disabled_voices_path.exists():
            try:
                disabled = json.loads(self.disabled_voices_path.read_text("utf-8"))
                if not isinstance(disabled, list) or any(
                    not isinstance(name, str) for name in disabled
                ):
                    raise ValueError("invalid voice state")
                self.disabled_voices = set(disabled)
            except (OSError, ValueError) as exc:
                raise TTSBackendError("話者の有効・無効設定を読み込めませんでした。") from exc

        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout),
        )

        self._load_cache()

    def describe(self) -> str:
        return f"{self.name} ({self.base_url})"

    async def health(self) -> None:
        """``/health`` で疎通を見る（合成より軽い）。"""

        try:
            async with self.session.get(
                f"{self.base_url}/health",
                headers=self._headers(),
            ) as response:
                if response.status >= 500:
                    raise TTSBackendError(f"TTSサーバが応答しません ({response.status})")

        except (TimeoutError, aiohttp.ClientError) as exc:
            raise TTSBackendError("TTSサーバに接続できません。") from exc

    async def close(self) -> None:
        await self.session.close()

    def _headers(self) -> dict[str, str]:
        if self.api_key:
            return {"Authorization": f"Bearer {self.api_key}"}

        return {}

    def _load_cache(self):
        if self.voices_cache is None:
            return

        try:
            data = json.loads(self.voices_cache.read_text("utf-8"))

        except OSError, ValueError:
            return

        if isinstance(data, list):
            self._voices = [str(item) for item in data]

    def _save_cache(self, names: list[str]):
        if self.voices_cache is None:
            return

        try:
            self.voices_cache.parent.mkdir(
                parents=True,
                exist_ok=True,
            )
            self.voices_cache.write_text(
                json.dumps(names, ensure_ascii=False),
                encoding="utf-8",
            )

        except OSError:
            pass

    async def list_voices(self) -> list[Voice]:
        try:
            async with self.session.get(
                f"{self.base_url}/v1/audio/voices",
                headers=self._headers(),
            ) as response:
                response.raise_for_status()
                payload = await response.json()

        except (TimeoutError, aiohttp.ClientError) as exc:
            # サーバが応答しないときも、前回の一覧で読み上げを続けさせる
            if not self._voices:
                raise TTSBackendError("話者一覧を取得する際にError") from exc

            names = list(self._voices)

        else:
            entries = []

            if isinstance(payload, dict):
                entries = payload.get("data") or []

            names = []

            for entry in entries:
                if isinstance(entry, dict):
                    voice_id = entry.get("id")
                else:
                    voice_id = entry

                if voice_id:
                    names.append(str(voice_id))

            self._save_cache(names)

        if self.voice_order:
            ordered = [name for name in self.voice_order if name in names]
            ordered += [name for name in names if name not in self.voice_order]
            names = ordered

        self._voices = names

        return [
            Voice(id=index, name=name)
            for index, name in enumerate(names, start=1)
            if name not in self.disabled_voices
        ]

    def _resolve_voice(self, speaker_id: int | str) -> str:
        voices = self._voices or []

        if isinstance(speaker_id, str):
            if speaker_id and speaker_id in voices:
                if speaker_id not in self.disabled_voices:
                    return speaker_id

        elif speaker_id and 1 <= speaker_id <= len(voices):
            selected = voices[speaker_id - 1]
            if selected not in self.disabled_voices:
                return selected

        return self.default_voice if self.default_voice not in self.disabled_voices else "none"

    async def synthesize(
        self,
        text: str,
        speaker_id: int | str,
        speed: float = 1.0,
    ) -> bytes:
        if self._voices is None:
            try:
                await self.list_voices()

            except TTSBackendError:
                self._voices = self._voices or []

        voice = self._resolve_voice(speaker_id)

        payload = {
            "model": self.model_name,
            "input": text,
            "voice": voice,
            "response_format": "wav",
            "speed": float(speed),
        }

        options: dict[str, object] = {}

        if self.caption:
            options["caption"] = self.caption

        adapter = self.lora_map.get(voice)

        if adapter:
            options["lora_adapter"] = adapter

        if options:
            payload["irodori"] = options

        try:
            async with self.session.post(
                f"{self.base_url}/v1/audio/speech",
                headers=self._headers(),
                json=payload,
            ) as response:
                response.raise_for_status()
                return await response.read()

        except (TimeoutError, aiohttp.ClientError) as exc:
            raise TTSBackendError("音声合成でError") from exc

    async def stream(
        self,
        text: str,
        speaker_id: int | str,
        speed: float = 1.0,
    ):
        """``stream_format=sse`` で受け取り、チャンク（文）ごとに届いた順へ返す。

        返すのは**生 PCM（s16le 48kHz モノラル）**。公式の Irodori-TTS-Server は
        ``stream_format: "sse"`` を付けると、テキストチャンクを1つ合成し終えるたびに
        ``audio_chunk`` イベント（base64 の音声）を逐次返す。全部を待たずに
        鳴らし始められる。

        ストリーミング非対応・pcm 非対応のサーバに当たったときは
        :class:`TTSBackendError` を投げ、呼び出し側は wav の経路へ戻る。
        """

        if not text.strip():
            return

        if self._voices is None:
            try:
                await self.list_voices()

            except TTSBackendError:
                self._voices = self._voices or []

        voice = self._resolve_voice(speaker_id)
        options: dict[str, object] = {
            "chunking_enabled": True,
            "chunk_min_chars": self.chunk_min_chars,
            "first_sentence_chunk_min_chars": self.first_chunk_min_chars,
        }

        if self.caption:
            options["caption"] = self.caption

        adapter = self.lora_map.get(voice)

        if adapter:
            options["lora_adapter"] = adapter

        payload = {
            "model": self.model_name,
            "input": text,
            "voice": voice,
            "response_format": "pcm",
            "speed": float(speed),
            "stream_format": "sse",
            "irodori": options,
        }
        headers = {**self._headers(), "Accept": "text/event-stream"}

        try:
            async with self.session.post(
                f"{self.base_url}/v1/audio/speech",
                headers=headers,
                json=payload,
            ) as response:
                if response.status != 200:
                    detail = (await response.text())[:200]
                    raise TTSBackendError(
                        f"ストリーミングに失敗しました: {response.status} {detail}"
                    )

                content_type = (
                    str(getattr(response, "content_type", "") or "").split(";")[0].strip().lower()
                )

                if content_type and content_type not in SSE_CONTENT_TYPES:
                    # sse を知らないサーバ（wav を一括で返す等）。wav の経路へ戻す。
                    raise TTSBackendError(f"ストリーミング非対応の応答です（{content_type}）")

                buffer = b""

                # 行単位で読むと aiohttp の行長上限（512KiB）に当たる。チャンクの
                # base64 は数秒ぶんで軽く超えるので、**バイト列のまま読んで
                # 空行で区切る**。
                async for raw in response.content.iter_any():
                    buffer += raw

                    if b"\r\n" in buffer:
                        buffer = buffer.replace(b"\r\n", b"\n")

                    while b"\n\n" in buffer:
                        block, buffer = buffer.split(b"\n\n", 1)
                        chunk = self._sse_block(block)

                        if chunk:
                            yield chunk

                if buffer.strip():
                    chunk = self._sse_block(buffer)

                    if chunk:
                        yield chunk

        except (TimeoutError, aiohttp.ClientError, OSError) as exc:
            raise TTSBackendError("音声合成でサーバに繋がりませんでした。") from exc

    def _sse_block(self, block: bytes) -> bytes | None:
        """SSE の1ブロックを解釈して、音声（生PCM）があれば返す。"""

        event = ""
        data: list[str] = []

        for line in block.decode("utf-8", "replace").splitlines():
            if line.startswith(":"):
                continue

            if line.startswith("event:"):
                event = line[len("event:") :].strip()

            elif line.startswith("data:"):
                data.append(line[len("data:") :].strip())

        if not data:
            return None

        if event == "audio_chunk":
            return self._decode_chunk("".join(data))

        if event == "error":
            raise TTSBackendError(f"サーバがエラーを返しました: {''.join(data)[:200]}")

        return None

    def _decode_chunk(self, payload: str) -> bytes:
        """``audio_chunk`` の data から生PCMを取り出す（pcm 以外は諦める）。"""

        try:
            chunk = json.loads(payload)

        except ValueError as exc:
            raise TTSBackendError("ストリーミングの応答を解釈できませんでした。") from exc

        media = str(chunk.get("media_type") or "").split(";")[0].strip().lower()
        fmt = str(chunk.get("format") or "").strip().lower()

        if media and media not in PCM_MEDIA_TYPES:
            raise TTSBackendError(f"ストリーミングが pcm を返しません（{media}）")

        if fmt and fmt not in ("pcm", "s16le"):
            raise TTSBackendError(f"ストリーミングが pcm を返しません（{fmt}）")

        try:
            return base64.b64decode(chunk.get("audio_base64") or "")

        except (ValueError, TypeError) as exc:
            raise TTSBackendError("ストリーミングの音声を復号できませんでした。") from exc
