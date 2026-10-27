"""speech.cpp の Irodori-TTS（GGUF）を OpenAI 互換 API で使う backend。

設定は環境変数ではなく plugin の instance config から注入する。
"""

import io
import json
import logging
import wave
from pathlib import Path

import aiohttp
from jllm_tts import TTSBackend, TTSBackendError, Voice

logger = logging.getLogger(__name__)

#: 1リクエストに渡す本文の上限（モデルは256トークンまで。余裕を見る）
MAX_CHARS_PER_REQUEST = 200
#: 文の区切り
SENTENCE_END = "。．！？!?\n"


class SpeechCppBackend(TTSBackend):
    """speech.cpp の Irodori-TTS（GGUF）。"""

    name = "speechcpp"

    def __init__(
        self,
        base_url: str,
        *,
        voices_dir: str = "",
        default_voice: str = "none",
        model_name: str = "",
        timeout: float = 120.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.voices_dir = Path(voices_dir) if voices_dir else None
        self.default_voice = default_voice or "none"
        self.model_name = model_name
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout),
        )
        self._voices: list[str] | None = None

    def describe(self) -> str:
        return f"{self.name} ({self.base_url})"

    # ---------------------------------------------------------------- 話者

    def _scan_voices(self) -> list[str]:
        """``*.voice.gguf`` から話者名を作る（``none`` は常に先頭）。"""

        names = ["none"]

        if self.voices_dir is not None and self.voices_dir.is_dir():
            for path in sorted(self.voices_dir.glob("*.voice.gguf")):
                name = path.name[: -len(".voice.gguf")]

                if name and name not in names:
                    names.append(name)

        return names

    async def _server_voices(self) -> list[str]:
        """サーバが持っている話者名（``/v1/models`` の ``speech.voices``）。"""

        try:
            async with self.session.get(f"{self.base_url}/v1/models") as response:
                response.raise_for_status()
                payload = json.loads(await response.read())
        except (TimeoutError, aiohttp.ClientError, OSError, ValueError):
            return []

        names: list[str] = []

        for model in payload.get("data") or []:
            for voice in (model.get("speech") or {}).get("voices") or []:
                name = voice.get("name")

                if name and name not in names:
                    names.append(name)

        return names

    async def list_voices(self) -> list[Voice]:
        names = await self._server_voices() or self._scan_voices()
        self._voices = names

        return [Voice(id=name, name=name) for name in names]

    def _resolve_voice(self, speaker_id: int | str) -> str:
        voices = self._voices if self._voices is not None else self._scan_voices()

        if isinstance(speaker_id, str) and speaker_id in voices:
            return speaker_id

        if isinstance(speaker_id, int) and 1 <= speaker_id <= len(voices):
            return voices[speaker_id - 1]

        return self.default_voice if self.default_voice in voices else "none"

    # ---------------------------------------------------------------- 合成

    @staticmethod
    def split_text(text: str, limit: int = MAX_CHARS_PER_REQUEST) -> list[str]:
        """長い本文を、1リクエストの上限に収まるかたまりに分ける。"""

        pieces: list[str] = []
        current = ""

        for char in text:
            current += char

            if char in SENTENCE_END and len(current) >= limit // 2:
                pieces.append(current)
                current = ""

        if current:
            pieces.append(current)

        chunks: list[str] = []

        for piece in pieces:
            while len(piece) > limit:
                chunks.append(piece[:limit])
                piece = piece[limit:]

            if not piece:
                continue

            if chunks and len(chunks[-1]) + len(piece) <= limit:
                chunks[-1] += piece
            else:
                chunks.append(piece)

        return [chunk for chunk in chunks if chunk.strip()]

    @staticmethod
    def combine_wavs(chunks: list[bytes]) -> bytes:
        """同じ形式のWAVをつなげて1つにする。"""

        if not chunks:
            return b""

        if len(chunks) == 1:
            return chunks[0]

        output = io.BytesIO()
        writer = None

        try:
            for chunk in chunks:
                with wave.open(io.BytesIO(chunk), "rb") as reader:
                    if writer is None:
                        writer = wave.open(output, "wb")
                        writer.setnchannels(reader.getnchannels())
                        writer.setsampwidth(reader.getsampwidth())
                        writer.setframerate(reader.getframerate())

                    writer.writeframes(reader.readframes(reader.getnframes()))
        finally:
            if writer is not None:
                writer.close()

        return output.getvalue()

    async def _speak(self, text: str, voice: str, speed: float) -> bytes:
        payload: dict[str, object] = {
            "input": text,
            "voice": voice,
            "response_format": "wav",
            "speed": float(speed),
        }

        if self.model_name:
            payload["model"] = self.model_name

        try:
            async with self.session.post(
                f"{self.base_url}/v1/audio/speech",
                json=payload,
            ) as response:
                if response.status != 200:
                    detail = (await response.text())[:200]
                    raise TTSBackendError(f"音声合成に失敗しました: {response.status} {detail}")

                return await response.read()

        except (TimeoutError, aiohttp.ClientError, OSError) as exc:
            raise TTSBackendError("音声合成でサーバに繋がりませんでした。") from exc

    async def synthesize(
        self,
        text: str,
        speaker_id: int | str,
        speed: float = 1.0,
    ) -> bytes:
        if self._voices is None:
            await self.list_voices()

        voice = self._resolve_voice(speaker_id)
        chunks = self.split_text(text)

        if not chunks:
            return b""

        wavs = [await self._speak(chunk, voice, speed) for chunk in chunks]

        return self.combine_wavs(wavs)

    async def health(self) -> None:
        try:
            async with self.session.get(f"{self.base_url}/health") as response:
                response.raise_for_status()
        except (TimeoutError, aiohttp.ClientError, OSError) as exc:
            raise TTSBackendError("疎通できませんでした。") from exc

    async def close(self) -> None:
        await self.session.close()
