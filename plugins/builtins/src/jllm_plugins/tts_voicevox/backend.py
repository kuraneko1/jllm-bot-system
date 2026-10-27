"""VOICEVOX エンジン用バックエンド。

plugin config: ``url``（既定 ``http://127.0.0.1:50021``）、
``default_speaker``（一覧に無い話者が来たときに使うスタイルID。既定は一覧の先頭）

多重化して退避先にしたとき、他エンジンの話者名（クローン名など）がそのまま来る。
VOICEVOX はスタイルID以外を 422 で弾くので、名前が一致すればそのID、
無ければ既定スタイルで読む。
"""


import aiohttp
from jllm_tts import TTSBackend, TTSBackendError, Voice


class VoicevoxBackend(TTSBackend):
    name = "voicevox"
    display_name = "VOICEVOX"

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 90.0,
        default_speaker: int | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.default_speaker = default_speaker
        self._voices: list[Voice] | None = None

        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout),
        )

    def describe(self) -> str:
        return f"{self.name} ({self.base_url})"

    async def health(self) -> None:
        """``/version`` で疎通を見る。"""

        try:
            async with self.session.get(f"{self.base_url}/version") as response:
                if response.status >= 500:
                    raise TTSBackendError(f"VOICEVOXが応答しません ({response.status})")

        except (TimeoutError, aiohttp.ClientError) as exc:
            raise TTSBackendError("VOICEVOXに接続できません。") from exc

    async def close(self) -> None:
        await self.session.close()

    async def list_voices(self) -> list[Voice]:
        try:
            async with self.session.get(f"{self.base_url}/speakers") as response:
                response.raise_for_status()
                payload = await response.json()

        except (TimeoutError, aiohttp.ClientError) as exc:
            raise TTSBackendError("話者一覧を取得する際にError") from exc

        voices: list[Voice] = []

        for speaker in payload:
            for style in speaker.get("styles", []):
                style_id = style.get("id")

                if style_id is None:
                    continue

                voices.append(
                    Voice(
                        id=style_id,
                        name=str(style.get("name", style_id)),
                    ),
                )

        self._voices = voices

        return voices

    async def _resolve_speaker(self, speaker_id: int | str) -> int:
        """スタイルIDに直す（名前一致 → 既定 → 一覧の先頭）。"""

        if self._voices is None:
            await self.list_voices()

        voices = self._voices or []
        ids = {voice.id for voice in voices}

        if isinstance(speaker_id, int) and (speaker_id in ids or not voices):
            return speaker_id

        if isinstance(speaker_id, str):
            text = speaker_id.strip()

            if text.isdigit() and (int(text) in ids or not voices):
                return int(text)

            for voice in voices:
                if voice.name == text:
                    return voice.id

        if self.default_speaker is not None:
            return self.default_speaker

        if voices:
            return voices[0].id

        raise TTSBackendError("VOICEVOX の話者を決められません。")

    async def synthesize(
        self,
        text: str,
        speaker_id: int | str,
        speed: float = 1.0,
    ) -> bytes:
        speaker_id = await self._resolve_speaker(speaker_id)

        try:
            async with self.session.post(
                f"{self.base_url}/audio_query",
                params={
                    "text": text,
                    "speaker": speaker_id,
                },
            ) as response:
                response.raise_for_status()
                query = await response.json()

            query["speedScale"] = speed

            async with self.session.post(
                f"{self.base_url}/synthesis",
                params={"speaker": speaker_id},
                json=query,
            ) as response:
                response.raise_for_status()
                return await response.read()

        except (TimeoutError, aiohttp.ClientError) as exc:
            raise TTSBackendError("音声合成でError") from exc
