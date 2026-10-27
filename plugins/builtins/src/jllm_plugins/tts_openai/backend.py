"""OpenAI 互換の音声合成サーバを使う TTS backend。

話者一覧・速度・無音 retry・PCM streaming・WAV fallback は旧実装と同じ規約とし、
設定は環境変数ではなく plugin の instance config から注入する。
"""

import array
import asyncio
import io
import json
import logging
import math
import os
import subprocess
import sys
import tempfile
import wave

import aiohttp
from jllm_tts import TTSBackend, TTSBackendError, TTSContentError, Voice

logger = logging.getLogger(__name__)

#: 無音だったときの再試行で使う seed（実測で正常な音声が返った値）。
#: 設定の ``seed`` と重複するものは飛ばして先頭から順に使う。
SILENCE_RETRY_SEEDS = (42, 2, 3, 555, 1234)

#: 「無音」とみなす RMS の既定しきい値（dBFS）。
#: 実測で正常な音声は -14〜-25 dBFS、無音は -53〜-60 dBFS だったので、
#: その中間を取る（DRAMA3: Clone Ryu の長文は seed 1/7 で無音、42/2/3/1234 で正常）。
DEFAULT_SILENCE_RMS_DBFS = -45.0

#: 「音声が 1 つも出なかった」をサーバが返してきたときの目印。
#: DRAMA3 は seed によってこのエラー（HTTP 502）を返すので、無音と同じ扱いで
#: seed を変えて再試行する。放っておくとバックエンドごと退避してしまう
#: （= 別の声に切り替わり、そのうえ常駐モデルが捨てられて次が 1 分止まる）。
EMPTY_TAKE_MARKERS = (
    "ended before producing audio",
    "produced no audio",
    "no audio",
)

#: ストリーミングで生 PCM とみなす Content-Type。
PCM_CONTENT_TYPES = ("audio/pcm", "application/octet-stream")

#: ストリーミングの無音判定に使う先頭バイト数（44.1kHz モノラル s16le で 1.5 秒）。
#:
#: **貯めずに**届いた順に返しながら、先頭のこれだけで無音かを判定する（無音なら途中で
#: :class:`TTSSilentStreamError`。プレイヤーは止めて wav の経路で作り直す）。無音のテイクは
#: 鳴らしても聞こえないので、先に流し始めても損はない。
#: 以前は判定のぶん貯めてから鳴らしていたため、初音が「最初のブロック ＋ 判定ぶん」遅れた
#: （2.0 秒の閾値が 43 フレームのブロックに 272 バイト足りず 2 ブロック待っていた時期もある）。
STREAM_SILENCE_BYTES = 44100 * 2 * 3 // 2


def _voice_names(payload) -> list[str]:
    """``/v1/audio/voices`` の返り値から話者名を取り出す。

    ``{"data": ["a", "b"]}``（drama3）でも ``{"data": [{"name": "a"}]}``
    （OpenAI風）でも、素の配列でも受ける。
    """

    if isinstance(payload, dict):
        items = payload.get("data")

        if items is None:
            items = payload.get("voices")

    else:
        items = payload

    if not isinstance(items, list):
        return []

    names: list[str] = []

    for item in items:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = str(item.get("name") or item.get("id") or "").strip()
        else:
            name = ""

        if name and name not in names:
            names.append(name)

    return names


class TTSSilentStreamError(TTSContentError):
    """ストリーミングの先頭が無音だった（鳴らす前に wav 経路へ戻すための合図）。"""


class OpenAITTSBackend(TTSBackend):
    """OpenAI 互換の ``/v1/audio/speech``。"""

    name = "openai"

    display_name = "OpenAI互換TTS"

    #: ``response_format=pcm`` + ``streaming=true`` で逐次受け取れるか。
    #: 対応していないサーバに当たった場合は Content-Type で見分けて wav へ戻す。
    supports_streaming = True

    def __init__(
        self,
        base_url: str,
        *,
        default_voice: str = "",
        voices: str = "",
        voices_path: str = "/v1/audio/voices",
        health_path: str = "/health",
        model_name: str = "",
        speed_mode: str = "atempo",
        default_speed: float = 1.0,
        ignore_speaker_speed: bool = False,
        seed: int | None = None,
        silence_retries: int = 1,
        silence_rms_dbfs: float = DEFAULT_SILENCE_RMS_DBFS,
        timeout: float = 120.0,
        streaming: bool = True,
    ):
        self.base_url = base_url.rstrip("/")
        self.default_voice = default_voice or ""
        self.voices = [name.strip() for name in str(voices).split(",") if name.strip()]
        self.voices_path = voices_path or ""
        self.health_path = health_path or ""
        self.model_name = model_name
        # サーバが speed を見ない前提で、既定は手元の atempo に任せる。
        # speed_mode="fixed" は「常に default_speed で読む」の別名。
        mode = str(speed_mode).strip().lower()

        if mode == "fixed":
            ignore_speaker_speed = True
            mode = "atempo"

        self.speed_mode = "server" if mode == "server" else "atempo"

        try:
            default_speed = float(default_speed)
        except TypeError, ValueError:
            default_speed = 1.0

        # 0 以下や非有限は atempo が扱えないので 1.0 に落とす
        self.default_speed = (
            default_speed if math.isfinite(default_speed) and default_speed > 0 else 1.0
        )
        self.ignore_speaker_speed = bool(ignore_speaker_speed)
        self.seed = int(seed) if seed is not None else None
        self.silence_retries = max(0, int(silence_retries))
        self.silence_rms_dbfs = float(silence_rms_dbfs)
        # ストリーミングが合わない環境（プロキシ等）では TTS_OPENAI_STREAM=0 で切れる。
        self.supports_streaming = bool(streaming)
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout),
        )
        self._voices: list[str] | None = None

    def describe(self) -> str:
        return f"{self.name} ({self.base_url})"

    # ---------------------------------------------------------------- 話者

    async def _api_voices(self) -> list[str]:
        if not self.voices_path:
            return []

        try:
            async with self.session.get(
                f"{self.base_url}{self.voices_path}",
            ) as response:
                if response.status != 200:
                    return []

                payload = json.loads(await response.read())

        except (TimeoutError, aiohttp.ClientError, OSError, ValueError):
            return []

        return _voice_names(payload)

    async def list_voices(self) -> list[Voice]:
        names = list(self.voices)

        if not names:
            names = await self._api_voices()

        if not names and self.default_voice:
            # 一覧が空だとUIで選べず詰むので、既定話者だけは必ず出す
            names = [self.default_voice]

        self._voices = names

        return [Voice(id=name, name=name) for name in names]

    def _resolve_voice(self, speaker_id: int | str) -> str:
        voices = self._voices if self._voices is not None else list(self.voices)

        if isinstance(speaker_id, str) and speaker_id in voices:
            return speaker_id

        if isinstance(speaker_id, int) and 1 <= speaker_id <= len(voices):
            return voices[speaker_id - 1]

        return self.default_voice or (voices[0] if voices else "")

    # ---------------------------------------------------------------- 速度

    @staticmethod
    def _atempo_filter(speed: float) -> str:
        """ffmpeg の atempo は 0.5〜2.0 しか受けないので、範囲内に割って繋ぐ。"""

        filters = []
        value = speed

        while value > 2.0:
            filters.append("atempo=2.0")
            value /= 2.0

        while value < 0.5:
            filters.append("atempo=0.5")
            value /= 0.5

        filters.append(f"atempo={value:.6f}")

        return ",".join(filters)

    @classmethod
    def _apply_tempo(cls, audio: bytes, speed: float) -> bytes | None:
        """合成済みの wav に atempo をかける（失敗したら None）。"""

        try:
            with tempfile.TemporaryDirectory() as directory:
                source = os.path.join(directory, "in.wav")
                target = os.path.join(directory, "out.wav")

                with open(source, "wb") as handle:
                    handle.write(audio)

                result = subprocess.run(
                    [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-i",
                        source,
                        "-filter:a",
                        cls._atempo_filter(speed),
                        "-y",
                        target,
                    ],
                    capture_output=True,
                )

                if result.returncode != 0:
                    logger.warning("atempo に失敗しました: %s", result.stderr[-200:])
                    return None

                with open(target, "rb") as handle:
                    return handle.read()

        except OSError:
            logger.exception("速度変更に失敗しました")
            return None

    # ---------------------------------------------------------------- 無音対策

    @staticmethod
    def _rms_dbfs(audio: bytes) -> float | None:
        """wav の RMS を dBFS で返す（読めなければ ``None``＝判定しない）。"""

        try:
            with wave.open(io.BytesIO(audio)) as reader:
                width = reader.getsampwidth()
                frames = reader.readframes(reader.getnframes())

        except wave.Error, EOFError, OSError, ValueError:
            return None

        if width not in (1, 2, 4) or not frames:
            return None

        samples = array.array({1: "b", 2: "h", 4: "i"}[width])
        usable = len(frames) - (len(frames) % samples.itemsize)
        samples.frombytes(frames[:usable])

        if not samples:
            return None

        scale = float(1 << (8 * width - 1))
        mean = sum(float(sample) * float(sample) for sample in samples) / len(samples)
        rms = math.sqrt(mean)

        if rms <= 0:
            return -999.0

        return 20 * math.log10(rms / scale)

    @staticmethod
    def _pcm_rms_dbfs(pcm: bytes) -> float | None:
        """生 PCM（s16le）の RMS を dBFS で返す。読めなければ ``None``。"""

        usable = len(pcm) - (len(pcm) % 2)

        if usable <= 0:
            return None

        samples = array.array("h")
        samples.frombytes(pcm[:usable])

        if sys.byteorder != "little":
            samples.byteswap()

        if not samples:
            return None

        mean = sum(float(sample) * float(sample) for sample in samples) / len(samples)
        rms = math.sqrt(mean)

        if rms <= 0:
            return -999.0

        return 20 * math.log10(rms / 32768.0)

    def _is_silent(self, audio: bytes) -> float | None:
        """無音なら RMS（dBFS）を返す。判定できなければ ``None``。"""

        if not audio:
            return None

        decibels = self._rms_dbfs(audio)

        if decibels is None or decibels >= self.silence_rms_dbfs:
            return None

        return decibels

    def _next_retry_seed(self, used: set[int]) -> int | None:
        for candidate in SILENCE_RETRY_SEEDS:
            if candidate not in used:
                return candidate

        return None

    # ---------------------------------------------------------------- 合成

    async def _speak(
        self,
        text: str,
        voice: str,
        speed: float,
        seed: int | None = None,
    ) -> bytes:
        payload: dict[str, object] = {
            "input": text,
            "voice": voice,
            "response_format": "wav",
        }

        if self.model_name:
            payload["model"] = self.model_name

        if seed is not None:
            payload["seed"] = int(seed)

        if self.speed_mode == "server":
            payload["speed"] = float(speed)

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

    async def stream(
        self,
        text: str,
        speaker_id: int | str,
        speed: float = 1.0,
    ):
        """``response_format=pcm`` ＋ ``streaming=true`` で受け取り、届いた順に返す。

        返すのは**生 PCM（s16le 44.1kHz モノラル）**。ヘッダはエンジンが最初の
        ブロックを確定するまで来ないので、``await response.read()`` で全部待つより
        ずっと早く鳴らし始められる（実測 10.8s → 2.5s）。

        最初のブロックが無音だったときは、seed を変えて作り直す価値があるので
        **何も鳴らさずに** :class:`TTSSilentStreamError` を投げる（呼び出し側は
        wav の経路＝seed 再試行つきへ戻す）。
        """

        if not text.strip():
            return

        if self._voices is None:
            await self.list_voices()

        voice = self._resolve_voice(speaker_id)
        payload: dict[str, object] = {
            "input": text,
            "voice": voice,
            "response_format": "pcm",
            "streaming": True,
        }

        if self.model_name:
            payload["model"] = self.model_name

        if self.seed is not None:
            payload["seed"] = int(self.seed)

        buffered = bytearray()
        checked = False

        try:
            async with self.session.post(
                f"{self.base_url}/v1/audio/speech",
                json=payload,
            ) as response:
                if response.status != 200:
                    detail = (await response.text())[:200]
                    raise TTSBackendError(f"音声合成に失敗しました: {response.status} {detail}")

                content_type = (
                    str(getattr(response, "content_type", "") or "").split(";")[0].strip().lower()
                )

                if content_type and content_type not in PCM_CONTENT_TYPES:
                    # pcm を知らないサーバ（wav を返す等）に当たった。生 PCM として
                    # 渡すとノイズになるので、黙って wav の経路へ戻す。
                    raise TTSBackendError(f"ストリーミング非対応の応答です（{content_type}）")

                async for chunk in response.content.iter_chunked(65536):
                    if not chunk:
                        continue

                    if not checked:
                        # 先頭 1.5 秒ぶんが揃った時点で、それまでの全体で無音かを見る。
                        # 揃うまでも貯めずに返す（判定は鳴らしながら）。
                        buffered += chunk

                        if len(buffered) >= STREAM_SILENCE_BYTES:
                            checked = True
                            self._raise_if_silent(bytes(buffered))
                            buffered.clear()

                    yield chunk

                # 1.5 秒に満たないまま終わった（短い相づちなど）
                if not checked and buffered:
                    self._raise_if_silent(bytes(buffered))

        except (TimeoutError, aiohttp.ClientError, OSError) as exc:
            raise TTSBackendError("音声合成でサーバに繋がりませんでした。") from exc

    def _raise_if_silent(self, head: bytes) -> None:
        decibels = self._pcm_rms_dbfs(head)

        if decibels is not None and decibels < self.silence_rms_dbfs:
            raise TTSSilentStreamError(f"先頭が無音です（{decibels:.1f} dBFS）")

    @staticmethod
    def _is_empty_take(error: TTSBackendError) -> bool:
        """「音声が 1 つも出なかった」というサーバの失敗か。"""

        message = str(error).lower()

        return any(marker in message for marker in EMPTY_TAKE_MARKERS)

    async def synthesize(
        self,
        text: str,
        speaker_id: int | str,
        speed: float = 1.0,
    ) -> bytes:
        if not text.strip():
            return b""

        if self._voices is None:
            await self.list_voices()

        voice = self._resolve_voice(speaker_id)
        # バックエンド側の速度ポリシー（話者ごとの設定を無視／既定速度）
        speed = self.resolve_speed(speed)

        seed = self.seed
        attempts = self.silence_retries + 1
        used = {seed} if seed is not None else set()
        audio = b""

        for attempt in range(attempts):
            try:
                audio = await self._speak(text, voice, speed, seed)

            except TTSBackendError as exc:
                # 「音声が 1 つも出なかった」は seed 次第のことがある（DRAMA3）。
                # ここで諦めるとエンジンごと退避して別の声になってしまうので、
                # 無音と同じく別 seed で作り直す。
                next_seed = self._next_retry_seed(used)

                if not self._is_empty_take(exc):
                    raise

                if attempt >= attempts - 1 or next_seed is None:
                    # seed を使い切った。エンジンは生きているので「この 1 件だけ失敗」
                    # として返し、ルーターに不調扱い・切り替えをさせない。
                    raise TTSContentError(str(exc)) from exc

                logger.warning("音声が出ませんでした（%s）。seed=%s で再試行します", exc, next_seed)
                used.add(next_seed)
                seed = next_seed
                continue

            silent = self._is_silent(audio) if attempt < attempts - 1 else None

            if silent is None:
                break

            next_seed = self._next_retry_seed(used)

            if next_seed is None:
                logger.warning("無音（%.1f dBFS）ですが別seedが無いので返します", silent)
                break

            logger.warning(
                "無音（%.1f dBFS / seed=%s）なので seed=%s で再試行します", silent, seed, next_seed
            )
            used.add(next_seed)
            seed = next_seed

        if self.speed_mode == "atempo" and abs(speed - 1.0) > 1e-3 and audio:
            adjusted = await asyncio.to_thread(self._apply_tempo, audio, speed)

            if adjusted is not None:
                return adjusted

            logger.warning("等速のまま返します（speed=%s）", speed)

        return audio

    # ---------------------------------------------------------------- 疎通

    async def health(self) -> None:
        last = ""

        for path in [path for path in (self.health_path, self.voices_path) if path]:
            try:
                async with self.session.get(f"{self.base_url}{path}") as response:
                    if response.status < 400:
                        return

                    last = str(response.status)

            except (TimeoutError, aiohttp.ClientError, OSError) as exc:
                # 繋がらないなら別のパスでも同じ
                last = str(exc) or type(exc).__name__
                break

        raise TTSBackendError(f"疎通できませんでした（{last}）")

    async def close(self) -> None:
        await self.session.close()
