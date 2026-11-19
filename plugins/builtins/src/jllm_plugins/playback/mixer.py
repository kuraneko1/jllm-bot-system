import contextlib
import logging
import threading
from collections.abc import Callable

import audioop
import discord

logger = logging.getLogger(__name__)

#: Discord AudioSource が 20ms ごとに要求する PCM サイズ（48kHz / 2ch / s16le）。
FRAME_BYTES = 3840
SILENCE_FRAME = b"\x00" * FRAME_BYTES


def _pad_frame(data: bytes) -> bytes:
    if len(data) == FRAME_BYTES:
        return data
    if len(data) > FRAME_BYTES:
        return data[:FRAME_BYTES]
    return data + b"\x00" * (FRAME_BYTES - len(data))


class GuildAudioMixer(discord.AudioSource):
    """常時再生ストリーム（Haru 等）と単発オーバーレイ再生（読み上げ）を同時に合成する。"""

    def __init__(self, primary: discord.AudioSource | None = None):
        super().__init__()
        self._lock = threading.Lock()
        self._primary: discord.AudioSource | None = primary
        self._overlay: (
            tuple[discord.AudioSource, Callable[[Exception | None], None] | None] | None
        ) = None
        self.active = True

    def set_primary(self, source: discord.AudioSource | None) -> None:
        with self._lock:
            old = self._primary
            self._primary = source
        if old is not None and old is not source:
            with contextlib.suppress(Exception):
                old.cleanup()

    def play_overlay(
        self,
        source: discord.AudioSource,
        after: Callable[[Exception | None], None] | None = None,
    ) -> None:
        previous = None
        with self._lock:
            if not self.active:
                if after is not None:
                    after(RuntimeError("mixer is closed"))
                return
            previous = self._overlay
            self._overlay = (source, after)
        if previous is not None:
            prev_src, prev_after = previous
            with contextlib.suppress(Exception):
                prev_src.cleanup()
            if prev_after is not None:
                with contextlib.suppress(Exception):
                    prev_after(None)

    def stop_overlay(self) -> bool:
        with self._lock:
            current = self._overlay
            self._overlay = None
        if current is None:
            return False
        src, after = current
        with contextlib.suppress(Exception):
            src.cleanup()
        if after is not None:
            with contextlib.suppress(Exception):
                after(None)
        return True

    def has_overlay(self) -> bool:
        with self._lock:
            return self._overlay is not None

    def read(self) -> bytes:
        with self._lock:
            if not self.active:
                return b""
            primary = self._primary
            overlay = self._overlay

        base = SILENCE_FRAME
        if primary is not None:
            try:
                chunk = primary.read()
            except Exception:  # noqa: BLE001
                logger.debug("mixer primary read failed", exc_info=True)
                chunk = b""
            if chunk:
                base = _pad_frame(chunk)

        if overlay is not None:
            ov_src, ov_after = overlay
            ov_err: Exception | None = None
            ov_chunk = b""
            try:
                ov_chunk = ov_src.read()
            except Exception as exc:  # noqa: BLE001
                ov_err = exc
                ov_chunk = b""
            if not ov_chunk:
                with self._lock:
                    if self._overlay is overlay:
                        self._overlay = None
                with contextlib.suppress(Exception):
                    ov_src.cleanup()
                if ov_after is not None:
                    with contextlib.suppress(Exception):
                        ov_after(ov_err)
            else:
                base = audioop.add(base, _pad_frame(ov_chunk), 2)

        return base

    def is_opus(self) -> bool:
        return False

    def close(self) -> None:
        with self._lock:
            self.active = False
            primary = self._primary
            self._primary = None
            overlay = self._overlay
            self._overlay = None
        if overlay is not None:
            ov_src, ov_after = overlay
            with contextlib.suppress(Exception):
                ov_src.cleanup()
            if ov_after is not None:
                with contextlib.suppress(Exception):
                    ov_after(None)
        if primary is not None:
            with contextlib.suppress(Exception):
                primary.cleanup()

    def cleanup(self) -> None:
        self.close()
