# ruff: noqa: E501
"""実験場（プレイグラウンド）。

一般ユーザーが好きな文章に好きな音声効果を付けて試せるようにする。
読み上げ本体と**同じ規約**で処理する:

- インライントークン（``f2`` / ``ec3`` / ``text1 text2 ec3`` / ``(text)ec3`` など）
- 辞書 wav の差し替え（登録済みの語句は TTS せずその wav を使う）
- 適用順は「速度 → 効果」（player.py と同じ）
"""
import asyncio
import logging
import subprocess
import tempfile
from pathlib import Path

from jllm_plugins.playback.player import (
    TTS_SERVER_MAX_SPEED,
    TTS_SERVER_MIN_SPEED,
    SpeechPlayer,
)
from jllm_plugins.speech_text import (
    _apply_trailing_speed,
    _parse_pieces,
    parse_commands,
)

logger = logging.getLogger(__name__)

MAX_TEXT_LENGTH = 300
MAX_PIECES = 40
OUTPUT_RATE = 48000


class PlaygroundError(Exception):
    """利用者にそのまま見せてよいエラー。"""


class Playground:
    def __init__(self, tts, dictionary, readings, effects=None):
        self.tts = tts
        self.dictionary = dictionary
        self.readings = readings
        #: ``speech.effects`` の service。無い場合は既定の IR パスを使う。
        self.effects = effects

    def plan(self, text: str, speed: float = 1.0):
        """本文を (チャンク, 速度, 効果) に分解する。

        読み上げ本体と同じ手順（特殊読み → 文末コマンド → インライントークン）。
        """
        processed = self.readings.apply(text) if self.readings is not None else text

        command = parse_commands(processed)
        base = processed
        trailing = None
        if command is not None:
            base, trailing, _stop = command

        pieces = _parse_pieces(base, speed)
        pieces = _apply_trailing_speed(pieces, trailing)

        if not pieces:
            pieces = [(base, speed, ())]

        return [
            (chunk, piece_speed, effects)
            for chunk, piece_speed, effects in pieces
            if chunk.strip() or effects
        ]

    async def synthesize(self, text: str, speaker, speed: float = 1.0) -> bytes:
        pieces = self.plan(text, speed)

        if len(pieces) > MAX_PIECES:
            raise PlaygroundError(
                "区間が多すぎます。効果や括弧を減らしてください。"
            )

        chunks: list[bytes] = []

        for chunk, piece_speed, effects in pieces:
            for segment in self.dictionary.split(chunk):
                if segment.kind == "sound":
                    data = await asyncio.to_thread(
                        self._from_dictionary, segment.value, piece_speed, effects
                    )
                else:
                    data = await self._from_tts(
                        segment.value, speaker, piece_speed, effects
                    )

                if data:
                    chunks.append(data)

        if not chunks:
            raise PlaygroundError("読み上げられる内容がありません。")

        joined = await asyncio.to_thread(self._concat, chunks)
        if joined is None:
            raise PlaygroundError("音声の組み立てに失敗しました。")
        return joined

    async def _from_tts(self, text, speaker, speed, effects) -> bytes | None:
        if not text.strip():
            return None

        # サーバーの受付範囲に丸め、はみ出し分は手元でかける（player.py と同じ）。
        server_speed = min(TTS_SERVER_MAX_SPEED, max(TTS_SERVER_MIN_SPEED, speed))
        audio = await self.tts.synthesize(
            text=text, speaker_id=speaker, speed=server_speed,
        )

        remainder = speed / server_speed
        if abs(remainder - 1.0) > 1e-6:
            slowed = await asyncio.to_thread(
                SpeechPlayer._apply_speed_bytes, audio, remainder
            )
            if slowed is not None:
                audio = slowed

        if effects:
            audio = await asyncio.to_thread(
                SpeechPlayer._apply_effects, audio, effects,
                effect_service=self.effects,
            )

        return audio

    def _from_dictionary(self, path, speed, effects) -> bytes | None:
        audio = SpeechPlayer._read_sound(path)
        if audio is None:
            return None

        if abs(speed - 1.0) > 1e-6:
            adjusted = SpeechPlayer._apply_speed(path, speed)
            if adjusted is None:
                return None
            audio = adjusted

        if effects:
            audio = SpeechPlayer._apply_effects(audio, effects, effect_service=self.effects)

        return audio

    @staticmethod
    def _concat(chunks: list[bytes]) -> bytes | None:
        """別々に作った wav を 1 本に連結する（48kHz ステレオに揃える）。"""
        if not chunks:
            return None
        if len(chunks) == 1:
            return chunks[0]

        with tempfile.TemporaryDirectory(prefix="playground-") as tmp:
            root = Path(tmp)
            paths = []
            for index, data in enumerate(chunks):
                path = root / f"{index:03d}.wav"
                path.write_bytes(data)
                paths.append(path)

            args = ["ffmpeg", "-v", "error", "-y"]
            for path in paths:
                args += ["-i", str(path)]

            labels = "".join(
                f"[{index}:a]aresample={OUTPUT_RATE},aformat=channel_layouts=stereo[p{index}];"
                for index in range(len(paths))
            )
            graph = (
                labels
                + "".join(f"[p{index}]" for index in range(len(paths)))
                + f"concat=n={len(paths)}:v=0:a=1[out]"
            )
            output = root / "joined.wav"
            args += ["-filter_complex", graph, "-map", "[out]", str(output)]

            result = subprocess.run(args, capture_output=True)
            if result.returncode != 0:
                logger.warning(
                    "実験場の連結に失敗: %s", result.stderr[-200:].decode("utf-8", "replace")
                )
                return None

            try:
                return output.read_bytes()
            except OSError:
                logger.exception("実験場の連結結果を読めません")
                return None
