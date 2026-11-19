"""Discord ボイスチャンネルと Haru WebSocket の双方向リアルタイム音声ブリッジ。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Awaitable, Callable

import aiohttp
import discord
import numpy as np
import sphn

from jllm_plugins.ducking import attach_pcm_tap, detach_pcm_tap
from jllm_plugins.playback.mixer import FRAME_BYTES, SILENCE_FRAME, GuildAudioMixer

logger = logging.getLogger(__name__)

HARU_SAMPLE_RATE = 24000
HARU_FRAME_SAMPLES = 3200
HARU_TICK_SECONDS = HARU_FRAME_SAMPLES / HARU_SAMPLE_RATE

KIND_HANDSHAKE = 0x00
KIND_AUDIO = 0x01
KIND_TEXT = 0x02
KIND_PING = 0x06


class HaruAudioSource(discord.AudioSource):
    """Haru の 24kHz mono float32 音声を Discord 用 48kHz stereo s16le に変換・供給する。"""

    MAX_BUFFER_BYTES = 48000 * 2 * 2 * 3

    def __init__(self, *, volume: float = 1.0):
        super().__init__()
        self._lock = threading.Lock()
        self._buf = bytearray()
        self._last_sample = 0.0
        self._closed = False
        self.volume = min(3.0, max(0.0, float(volume)))

    def push_24k_float(self, pcm_24k: np.ndarray) -> None:
        if pcm_24k is None or pcm_24k.size == 0:
            return
        mono = np.asarray(pcm_24k, dtype=np.float32).reshape(-1)
        n = mono.shape[0]
        padded = np.empty(n + 1, dtype=np.float32)
        padded[0] = self._last_sample
        padded[1:] = mono
        self._last_sample = float(mono[-1])

        up = np.empty(n * 2, dtype=np.float32)
        up[0::2] = 0.5 * (padded[:-1] + padded[1:])
        up[1::2] = padded[1:]

        scaled = np.clip(up * (32767.0 * self.volume), -32768.0, 32767.0).astype("<i2")
        stereo = np.column_stack((scaled, scaled)).astype("<i2").tobytes()

        with self._lock:
            if self._closed:
                return
            self._buf.extend(stereo)
            overflow = len(self._buf) - self.MAX_BUFFER_BYTES
            if overflow > 0:
                drop = ((overflow + 3) // 4) * 4
                del self._buf[:drop]

    def read(self) -> bytes:
        with self._lock:
            if self._closed:
                return b""
            if len(self._buf) < FRAME_BYTES:
                return SILENCE_FRAME
            chunk = bytes(self._buf[:FRAME_BYTES])
            del self._buf[:FRAME_BYTES]
            return chunk

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        with self._lock:
            self._closed = True
            self._buf.clear()


class HaruInputMixer:
    """Discord 48kHz stereo s16le を話者フォーカス付きで 24kHz mono float32 に合成する。"""

    MAX_USER_SAMPLES = HARU_SAMPLE_RATE

    def __init__(self, *, focus_user_id: int | None = None):
        self._lock = threading.Lock()
        self._buffers: dict[int, np.ndarray] = {}
        self.focus_user_id: int | None = focus_user_id

    def set_focus(self, user_id: int | None) -> None:
        with self._lock:
            self.focus_user_id = int(user_id) if user_id is not None else None
            self._buffers.clear()

    def push_discord_pcm(self, user_id: int | None, pcm_bytes: bytes) -> None:
        if user_id is None or not pcm_bytes:
            return
        usable_bytes = (len(pcm_bytes) // 4) * 4
        if usable_bytes <= 0:
            return

        with self._lock:
            focus = self.focus_user_id
            if focus is not None and user_id != focus:
                return

        stereo = np.frombuffer(pcm_bytes[:usable_bytes], dtype="<i2").reshape(-1, 2)
        mono_48k = stereo.mean(axis=1, dtype=np.float32) * (1.0 / 32768.0)
        usable_samples = (mono_48k.shape[0] // 2) * 2
        if usable_samples <= 0:
            return
        mono_24k = mono_48k[:usable_samples].reshape(-1, 2).mean(axis=1)

        with self._lock:
            if self.focus_user_id is not None and user_id != self.focus_user_id:
                return
            prev = self._buffers.get(user_id)
            if prev is None or prev.size == 0:
                combined = mono_24k
            else:
                combined = np.concatenate((prev, mono_24k))
            if combined.size > self.MAX_USER_SAMPLES:
                combined = combined[-self.MAX_USER_SAMPLES :]
            self._buffers[user_id] = combined

    def pop_frame(self, samples: int = HARU_FRAME_SAMPLES) -> np.ndarray:
        mixed = np.zeros(samples, dtype=np.float32)
        with self._lock:
            stale: list[int] = []
            for uid, buf in self._buffers.items():
                take = min(samples, buf.size)
                if take > 0:
                    mixed[:take] += buf[:take]
                if buf.size <= take:
                    stale.append(uid)
                else:
                    self._buffers[uid] = buf[take:]
            for uid in stale:
                del self._buffers[uid]
        np.clip(mixed, -1.0, 1.0, out=mixed)
        return mixed


class HaruConversationSession:
    """1 ギルド分の Haru リアルタイム双方向会話セッション。"""

    def __init__(
        self,
        *,
        guild_id: int,
        voice_channel_id: int,
        text_channel_id: int,
        persona: dict,
        voice_client,
        ws: aiohttp.ClientWebSocketResponse,
        focus_user_id: int | None = None,
        output_volume: float = 1.0,
        show_transcript: bool = True,
        on_transcript: Callable[[int, str], Awaitable[None]] | None = None,
        on_ended: Callable[[int, str | None], Awaitable[None]] | None = None,
    ):
        self.guild_id = guild_id
        self.voice_channel_id = voice_channel_id
        self.text_channel_id = text_channel_id
        self.persona = dict(persona)
        self.voice_client = voice_client
        self.ws = ws
        self.show_transcript = show_transcript
        self._on_transcript = on_transcript
        self._on_ended = on_ended

        self.input_mixer = HaruInputMixer(focus_user_id=focus_user_id)
        self.output_source = HaruAudioSource(volume=output_volume)
        self.writer = sphn.OpusStreamWriter(HARU_SAMPLE_RATE)
        self.reader = sphn.OpusStreamReader(HARU_SAMPLE_RATE)

        self._handshake = asyncio.Event()
        self._closed = False
        self._mixer: GuildAudioMixer | None = None
        self._send_task: asyncio.Task | None = None
        self._recv_task: asyncio.Task | None = None
        self._flush_task: asyncio.Task | None = None
        self._text_buf: list[str] = []
        self._last_text_at = 0.0
        self._error_reason: str | None = None

    @property
    def focus_user_id(self) -> int | None:
        return self.input_mixer.focus_user_id

    def set_focus(self, user_id: int | None) -> None:
        self.input_mixer.set_focus(user_id)

    def start(self) -> None:
        self._attach_audio()
        self._recv_task = asyncio.create_task(self._recv_loop(), name=f"haru-recv-{self.guild_id}")
        self._send_task = asyncio.create_task(self._send_loop(), name=f"haru-send-{self.guild_id}")
        self._flush_task = asyncio.create_task(
            self._flush_loop(), name=f"haru-flush-{self.guild_id}"
        )

    def _attach_audio(self) -> None:
        vc = self.voice_client
        attach_pcm_tap(vc, "haru", self.input_mixer.push_discord_pcm)
        existing_mixer = getattr(vc, "_jllm_mixer", None)
        if existing_mixer is not None and getattr(existing_mixer, "active", False):
            existing_mixer.set_primary(self.output_source)
            self._mixer = existing_mixer
            return

        if getattr(vc, "is_playing", lambda: False)():
            stop_playing = getattr(vc, "stop_playing", None)
            if callable(stop_playing):
                with contextlib.suppress(Exception):
                    stop_playing()
            else:
                with contextlib.suppress(Exception):
                    vc.stop()

        mixer = GuildAudioMixer(self.output_source)
        vc._jllm_mixer = mixer
        self._mixer = mixer
        vc.play(mixer)

    def _detach_audio(self) -> None:
        vc = self.voice_client
        detach_pcm_tap(vc, "haru")
        if self._mixer is not None:
            if getattr(vc, "_jllm_mixer", None) is self._mixer:
                vc._jllm_mixer = None
            stop_playing = getattr(vc, "stop_playing", None)
            if callable(stop_playing):
                with contextlib.suppress(Exception):
                    stop_playing()
            elif getattr(vc, "is_playing", lambda: False)():
                with contextlib.suppress(Exception):
                    vc.stop()
            self._mixer.close()
            self._mixer = None
        self.output_source.cleanup()

    async def _flush_text(self, force: bool = False) -> None:
        if not self.show_transcript or self._on_transcript is None or not self._text_buf:
            if not self.show_transcript:
                self._text_buf.clear()
            return
        text = "".join(self._text_buf).strip()
        if not text:
            self._text_buf.clear()
            return
        should_flush = (
            force
            or len(text) >= 180
            or text.endswith(("。", "！", "？", "\n", "!", "?"))
            or (self._last_text_at > 0 and time.monotonic() - self._last_text_at >= 1.1)
        )
        if not should_flush:
            return
        self._text_buf.clear()
        self._last_text_at = 0.0
        try:
            await self._on_transcript(self.text_channel_id, text)
        except Exception:  # noqa: BLE001
            logger.debug("Haru 字幕投稿に失敗", exc_info=True)

    async def _flush_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(0.35)
                await self._flush_text(force=False)
        except asyncio.CancelledError:
            raise

    async def _recv_loop(self) -> None:
        try:
            async for msg in self.ws:
                if self._closed:
                    break
                if msg.type != aiohttp.WSMsgType.BINARY or not msg.data:
                    continue
                kind, payload = msg.data[0], msg.data[1:]
                if kind == KIND_HANDSHAKE:
                    self._handshake.set()
                elif kind == KIND_AUDIO and payload:
                    pcm = self.reader.append_bytes(payload)
                    if pcm is not None and pcm.size > 0:
                        self.output_source.push_24k_float(pcm)
                elif kind == KIND_TEXT and payload:
                    chunk = payload.decode("utf-8", errors="replace")
                    if chunk.startswith("[error]"):
                        self._error_reason = chunk.removeprefix("[error]").strip()
                        logger.warning("Haru サーバからエラー応答: %s", self._error_reason)
                        break
                    self._text_buf.append(chunk)
                    self._last_text_at = time.monotonic()
                    await self._flush_text(force=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if not self._closed:
                self._error_reason = str(exc) or "WebSocket 受信エラー"
                logger.warning("Haru 受信ループ終了: %s", self._error_reason)
        finally:
            self._handshake.set()
            await self._flush_text(force=True)
            if not self._closed and self._on_ended is not None:
                asyncio.create_task(self._on_ended(self.guild_id, self._error_reason))

    async def _send_loop(self) -> None:
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._handshake.wait(), timeout=15.0)
            loop = asyncio.get_running_loop()
            next_tick = loop.time()
            while not self._closed and not self.ws.closed:
                frame = self.input_mixer.pop_frame(HARU_FRAME_SAMPLES)
                pages = self.writer.append_pcm(frame)
                if pages:
                    await self.ws.send_bytes(bytes([KIND_AUDIO]) + pages)
                next_tick += HARU_TICK_SECONDS
                delay = next_tick - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                elif delay < -HARU_TICK_SECONDS * 4:
                    next_tick = loop.time()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if not self._closed:
                self._error_reason = self._error_reason or str(exc) or "WebSocket 送信エラー"
                logger.warning("Haru 送信ループ終了: %s", self._error_reason)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._handshake.set()
        for task in (self._send_task, self._recv_task, self._flush_task):
            if task is not None and task is not asyncio.current_task():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if not self.ws.closed:
            with contextlib.suppress(Exception):
                await self.ws.close()
        self._detach_audio()
